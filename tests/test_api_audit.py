import asyncio
import contextlib
import copy
import io
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException
from firebase_admin import auth

import security
from firebase.firebase import FirebaseRouter
from history.history import HistoryRouter
from history import capture
from history import session_history as history
from project1 import project1
from project2 import project2
from request_data import MAX_JSON_BYTES
from telegram import telegram
from test_session_capture import CaptureTestCase, p10, p220


API_CLIENT = httpx.AsyncClient


class ApiTestCase(CaptureTestCase):
    def setUp(self):
        super().setUp()
        self.app = FastAPI()
        for router in (HistoryRouter, FirebaseRouter, telegram.TelegramRouter,
                       project1.Project1Router, project2.Project2Router):
            self.app.include_router(router)

    def request(self, path, data=None, token=None, content=None):
        async def send():
            async with API_CLIENT(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                return await client.post(path, json=data, content=content, headers=headers)
        return asyncio.run(send())


class ApiAuditTests(ApiTestCase):
    def test_all_browser_mutators_require_auth_before_any_operation(self):
        for path in ("/api/v1/create_user", "/api/v1/send-alert", "/api/v1/history/snapshot",
                     "/api/v1/history/snapshot-built", "/api/v1/history/snapshot-current",
                     "/api/v1/project220/sent-low-snapshot", "/api/v1/project2_temporary_data"):
            with self.subTest(path=path), patch.object(security, "verify_browser_token") as verifier:
                response = self.request(path, {})
                self.assertEqual(response.status_code, 401)
                verifier.assert_not_called()

    def test_regular_user_cannot_create_accounts_or_import_history(self):
        for path in ("/api/v1/create_user", "/api/v1/history/snapshot", "/api/v1/history/snapshot-built"):
            with self.subTest(path=path), patch.object(security, "verify_browser_token", return_value={"uid": "user"}):
                response = self.request(path, {}, token="test-token")
                self.assertEqual(response.status_code, 403)

    def test_admin_claim_must_be_boolean_and_invalid_token_is_sanitized(self):
        with patch.object(security, "verify_browser_token", return_value={"uid": "user", "admin": "true"}):
            self.assertEqual(self.request("/api/v1/create_user", {}, token="test-token").status_code, 403)
        with patch.object(security, "verify_browser_token", side_effect=ValueError("secret-test-token")):
            response = self.request("/api/v1/send-alert", {}, token="secret-test-token")
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("secret-test-token", response.text)
        with patch.object(security, "verify_browser_token", side_effect=RuntimeError("private-credential")):
            response = self.request("/api/v1/send-alert", {}, token="test-token")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private-credential", response.text)

    def test_authenticated_current_save_outside_window_is_clear_no_save(self):
        with patch.object(security, "verify_browser_token", return_value={"uid": "user"}), \
                patch.object(capture.time, "time", return_value=self.s1_end):
            response = self.request("/api/v1/history/snapshot-current", {"project": "both"}, token="test-token")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(result["saved"] is False for result in response.json()["results"]))

    def test_admin_create_user_never_logs_or_echoes_password(self):
        output = io.StringIO()
        payload = {"email": "example@example.test", "password": "private-test-password"}
        with patch.object(security, "verify_browser_token", return_value={"uid": "admin-user", "admin": True}), \
                patch.object(auth, "create_user", return_value=object()) as create, contextlib.redirect_stdout(output):
            response = self.request("/api/v1/create_user", payload, token="test-token")
        self.assertEqual(response.status_code, 200)
        create.assert_called_once_with(**payload)
        self.assertNotIn(payload["password"], output.getvalue() + response.text)
        with patch.object(security, "verify_browser_token", return_value={"uid": "admin-user", "admin": True}), \
                patch.object(auth, "create_user", side_effect=RuntimeError(payload["password"])):
            response = self.request("/api/v1/create_user", payload, token="test-token")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(payload["password"], response.text)

    def test_json_arrays_and_invalid_json_are_rejected_cleanly(self):
        for path in ("/api/v1/history/snapshot-built", "/api/v1/history/snapshot-current",
                     "/api/v1/project220/sent-low-snapshot", "/api/v1/send-alert",
                     "/api/v1/project1_data", "/api/v1/project2_data"):
            with self.subTest(path=path), patch.object(security, "verify_browser_token", return_value={"uid": "user", "admin": True}):
                self.assertEqual(self.request(path, [], token="test-token").status_code, 400)
                self.assertEqual(self.request(path, token="test-token", content="{broken-json").status_code, 400)

    def test_account_creation_uses_bounded_json_before_creating_user(self):
        with patch.object(security, "verify_browser_token", return_value={"uid": "admin", "admin": True}), \
                patch.object(auth, "create_user") as create:
            response = self.request("/api/v1/create_user", token="test-token", content=" " * (MAX_JSON_BYTES + 1))
        self.assertEqual(response.status_code, 413)
        create.assert_not_called()

    def test_machine_ingestion_contract_remains_unauthenticated(self):
        with patch.object(capture, "accept_data", return_value={"recording": False, "data": p10()}), \
                patch.object(project2, "latest_project2_data", {}), \
                patch.object(project2, "_data_lock", asyncio.Lock()), \
                patch.object(project2, "_broadcast_lock", asyncio.Lock()):
            response = self.request("/api/v1/project2_data", p10())
        self.assertEqual(response.status_code, 200)


class TelegramAuditTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        for name, value in (("bot_token", "test-bot-token"), ("chat_id", "test-chat")):
            mocked = patch.object(telegram, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        mocked = patch.object(security, "verify_browser_token", return_value={"uid": "user"})
        mocked.start()
        self.addCleanup(mocked.stop)

    def send_with_upstream(self, upstream=None, error=None, message=" 167 \r\n \n 128 "):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.side_effect = error
        client.post.return_value = upstream
        with patch.object(telegram.httpx, "AsyncClient", return_value=client):
            response = self.request("/api/v1/send-alert", {"message": message}, token="test-token")
        return response, client

    def test_delivery_success_preserves_nested_confirmation_and_normalizes_lines(self):
        response, client = self.send_with_upstream(httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["telegram_response"]["ok"])
        self.assertEqual(client.post.call_args.kwargs["json"]["text"], "167\n128")

    def test_upstream_failure_cannot_be_reported_as_success(self):
        for upstream in (httpx.Response(400, json={"ok": False, "description": "private-test-token"}),
                         httpx.Response(200, json={"ok": False}), httpx.Response(502, text="not-json")):
            with self.subTest(status=upstream.status_code):
                response, _ = self.send_with_upstream(upstream)
                self.assertEqual(response.status_code, 502)
                self.assertNotIn("private-test-token", response.text)

    def test_transport_failure_never_exposes_bot_url_or_token(self):
        for error, status in ((httpx.ConnectError("https://api.telegram.org/bottest-bot-token/sendMessage"), 502),
                              (httpx.ReadTimeout("test-bot-token"), 504)):
            with self.subTest(error=type(error).__name__):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    response, _ = self.send_with_upstream(error=error)
                self.assertEqual(response.status_code, status)
                self.assertNotIn("test-bot-token", response.text + output.getvalue())

    def test_invalid_or_oversized_message_and_missing_config_make_no_request(self):
        for message in (None, [], {}, "", " " * 4, "x" * 4097):
            with self.subTest(message=str(message)[:10]):
                response, client = self.send_with_upstream(message=message)
                self.assertEqual(response.status_code, 400)
                client.post.assert_not_called()
        with patch.object(telegram, "bot_token", None):
            response, client = self.send_with_upstream()
        self.assertEqual(response.status_code, 503)
        client.post.assert_not_called()


class CalculationAuditTests(CaptureTestCase):
    def test_zero_number_amount_and_precision_survive_sent_snapshot(self):
        entry = {"number": 0, "column": "0", "half": "second", "amount": 100.123457, "rank": 6}
        clean = history.normalize_sent_low_entry(entry)
        self.assertEqual(clean["number"], "000")
        self.assertEqual(history.parse_arrow_entry(clean["entry"])[1], 100.123457)
        self.assertEqual(history.find_project220_current_amount({10: [{"number": 0, "amount": 125.25}]}, clean), 125.25)

    def test_same_millisecond_sent_actions_do_not_overwrite_each_other(self):
        entry = {"number": "128", "column": "1", "half": "first", "amount": 100, "rank": 4}
        with patch.object(capture.time, "time", return_value=self.s1 + 50):
            first = history.save_project220_sent_low_snapshot("first", [entry])
            second = history.save_project220_sent_low_snapshot("first", [entry])
        self.assertNotEqual(first["doc_id"], second["doc_id"])
        self.assertIsNotNone(history.load_file_doc(first["doc_id"]))
        self.assertIsNotNone(history.load_file_doc(second["doc_id"]))

    def test_row_zero_tie_break_retains_first_chart_row_without_changing_scores(self):
        common = {"average_score": .5, "avg_rank": 2, "rank": 1}
        entries = [{**common, "number": "137", "row_index": 1}, {**common, "number": "128", "row_index": 0}]
        before = copy.deepcopy(entries)
        self.assertEqual(sorted(entries, key=history.combined_sort_key)[0]["number"], "128")
        self.assertEqual(before, entries)
        missing = {**common, "number": "100"}
        self.assertLess(history.combined_sort_key(entries[1]), history.combined_sort_key(missing))

    def test_restore_rejects_invalid_dates_incomplete_and_duplicate_selections(self):
        valid = history.build_project10_entries(p10())
        for date in ("../escape", "2026-02-31", "2026/09/28"):
            with self.subTest(date=date), self.assertRaises(HTTPException):
                history.save_built_session_snapshot("project10", date, 1, valid)
        for entries in ([], valid[:3], [valid[0]] * 6):
            with self.subTest(entries=len(entries)), self.assertRaises(HTTPException):
                history.save_built_session_snapshot("project10", "2026-09-28", 1, entries)
        self.assertEqual(history._memory_docs, {})

    def test_complete_restores_keep_canonical_ids_and_counts(self):
        for project, entries in (("project10", history.build_project10_entries(p10())),
                                 ("project220", history.build_project220_entries(p220()))):
            with self.subTest(project=project):
                result = history.save_built_session_snapshot(project, "2026-09-28", 2, entries)
                self.assertEqual(result["doc_id"], f"2026-09-28-s2-{project}")
                self.assertEqual(history.load_file_doc(result["doc_id"])["entries"], entries)

    def test_nonfinite_unknown_or_duplicate_sent_entries_are_rejected(self):
        good = {"number": "128", "column": "1", "half": "first", "amount": 100, "rank": 4}
        for entries in ([{**good, "amount": float("inf")}], [{**good, "number": "999"}], [good, good]):
            with self.subTest(entries=str(entries)), self.assertRaises(HTTPException):
                history.save_project220_sent_low_snapshot("first", entries)


class RecoveryPerformanceTests(CaptureTestCase):
    def test_settled_capture_is_not_reparsed_every_poll_but_new_receipt_is_seen(self):
        accepted = capture.accept_data("project10", p10(), self.s1)
        capture.flush_capture(accepted["key"], now=self.s1 + 90)
        capture.recover_pending(now=self.s1 + 91)
        with patch.object(capture, "read_checkpoint", wraps=capture.read_checkpoint) as reader:
            capture.recover_pending(now=self.s1 + 100)
            capture.recording_status()
            reader.assert_not_called()
        capture.accept_data("project10", p10(5), self.s1 + 120)
        self.assertEqual(capture.recording_status()["pending_captures"], 1)
        capture.recover_pending(now=self.s1 + 210)
        self.assertEqual(capture.recording_status()["pending_captures"], 0)

    def test_changed_settled_file_is_validated_again_and_corruption_is_visible(self):
        accepted = capture.accept_data("project10", p10(), self.s1)
        capture.flush_capture(accepted["key"], now=self.s1 + 90)
        capture.recover_pending(now=self.s1 + 91)
        capture.checkpoint_path(accepted["key"]).write_text("broken-json", encoding="utf-8")
        self.assertEqual(capture.recording_status()["state"], "degraded")


class WebSocketAuditTests(unittest.TestCase):
    def setUp(self):
        # These tests isolate ordering after the auth gate; auth itself has
        # handshake/denial coverage in test_security_hardening.py.
        for module in (project1, project2):
            mocked = patch.object(module, "authenticate_websocket", new_callable=AsyncMock,
                                  return_value={"uid": "test-user"})
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_disconnect_during_initial_send_leaves_no_stale_connection(self):
        class DisconnectedSocket:
            async def accept(self):
                pass

            async def send_json(self, payload):
                raise RuntimeError("client disconnected")

        for module, handler, connections in ((project1, project1.ws_project1, "connections_project1"),
                                             (project2, project2.ws_project2, "connections_project2")):
            with self.subTest(module=module.__name__), patch.object(module, connections, []), \
                    patch.object(module, "_broadcast_lock", asyncio.Lock()):
                asyncio.run(handler(DisconnectedSocket()))
                self.assertEqual(getattr(module, connections), [])

    def test_slow_initial_snapshot_cannot_arrive_after_newer_broadcast(self):
        async def exercise(module, handler, broadcast, project):
            first_send, release_initial, disconnect = asyncio.Event(), asyncio.Event(), asyncio.Event()

            class Socket:
                def __init__(self):
                    self.messages = []

                async def accept(self):
                    pass

                async def send_json(self, payload):
                    if not first_send.is_set():
                        first_send.set()
                        await release_initial.wait()
                    self.messages.append(copy.deepcopy(payload))

                async def receive_text(self):
                    await disconnect.wait()
                    raise RuntimeError("client disconnected")

            socket = Socket()
            with patch.object(module, f"connections_{project}", []), \
                    patch.object(module, f"latest_{project}_data", {"machine1": ["old"]}), \
                    patch.object(module, "_broadcast_lock", asyncio.Lock()), \
                    patch.object(module, "_broadcast_revision", 1):
                connection_task = asyncio.create_task(handler(socket))
                await first_send.wait()
                update_task = asyncio.create_task(broadcast({"machine1": ["new"], "__full_snapshot": True}, 1))
                await asyncio.sleep(0)
                self.assertEqual(socket.messages, [])
                release_initial.set()
                await update_task
                disconnect.set()
                await connection_task
                self.assertEqual([message["machine1"] for message in socket.messages], [["old"], ["new"]])
                self.assertTrue(all(message["__full_snapshot"] for message in socket.messages))

        asyncio.run(exercise(project1, project1.ws_project1, project1.broadcast_project1_data, "project1"))
        asyncio.run(exercise(project2, project2.ws_project2, project2.broadcast_project2_data, "project2"))


if __name__ == "__main__":
    unittest.main()
