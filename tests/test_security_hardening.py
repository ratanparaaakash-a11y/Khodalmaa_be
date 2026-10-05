import asyncio
import hashlib
import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import security
from history import capture
from history.history import HistoryRouter
from project1 import project1
from project2 import project2
from request_data import MAX_JSON_BYTES, json_object


class SecurityHardeningTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        for router in (HistoryRouter, project1.Project1Router, project2.Project2Router):
            self.app.include_router(router)
        environment = patch.dict(os.environ, {
            "MACHINE_INGESTION_MODE": "legacy", "MACHINE_INGESTION_CREDENTIALS_JSON": "[]",
            "EXTRA_BROWSER_ORIGINS": "",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def request(self, path, method="GET", data=None, headers=None):
        async def execute():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url="http://test") as client:
                return await client.request(method, path, json=data, headers=headers or {})
        return asyncio.run(execute())

    def test_history_and_calculation_reads_require_sign_in(self):
        for path in ("/api/v1/history", "/api/v1/history/health", "/api/v1/project220/sent-low-calculation"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path).status_code, 401)

    def test_authenticated_history_read_preserves_response(self):
        with patch.object(security, "verify_browser_token", return_value={"uid": "user"}), \
                patch("history.history.analyze_history", return_value={"records": []}) as analyze:
            response = self.request("/api/v1/history?project=project10&session=2&days=7",
                                    headers={"Authorization": "Bearer valid-test"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"records": []})
        analyze.assert_called_once_with("project10", 2, 7)

    def test_legacy_temporary_metadata_cannot_reach_either_storage_path(self):
        with patch.object(project2.temporary, "add") as temporary, patch.object(capture, "accept_data") as real:
            response = self.request("/api/v1/project2_data", "POST", {
                "machine4": [0] * 10, "__temporary_for_seconds": 1800,
            })
        self.assertEqual(response.status_code, 400)
        temporary.assert_not_called()
        real.assert_not_called()

    def test_temporary_endpoint_rejects_unauthenticated_requests_before_storage(self):
        with patch.object(project2.temporary, "add") as add, patch.object(capture, "accept_data") as real:
            response = self.request("/api/v1/project2_temporary_data", "POST", {
                "machine4": [0] * 10, "__temporary_for_seconds": 1800,
            })
        self.assertEqual(response.status_code, 401)
        add.assert_not_called()
        real.assert_not_called()

    def test_required_machine_auth_gates_both_real_routes_before_capture(self):
        credential = MachineCredentialTests().credential()
        with patch.dict(os.environ, {"MACHINE_INGESTION_MODE": "required",
                                   "MACHINE_INGESTION_CREDENTIALS_JSON": json.dumps([credential])}), \
                patch.object(capture, "accept_data") as real:
            for path in ("/api/v1/project1_data", "/api/v1/project2_data"):
                with self.subTest(path=path):
                    self.assertEqual(self.request(path, "POST", {"machine1": [0] * 10}).status_code, 401)
            real.assert_not_called()

    def test_websocket_without_or_with_invalid_auth_never_receives_snapshot(self):
        for route in ("ws_project1", "ws_project2"):
            for frame in ({"type": "hello"}, {"type": "authenticate", "token": "bad"},
                          {"type": "authenticate", "token": "a" * 17000}):
                with self.subTest(route=route, frame=frame["type"]), \
                        patch.object(security, "verify_browser_token", side_effect=ValueError("secret")):
                    with TestClient(self.app) as client, client.websocket_connect(f"/api/v1/{route}") as ws:
                        ws.send_json(frame)
                        with self.assertRaises(WebSocketDisconnect) as disconnected:
                            ws.receive_json()
                        self.assertEqual(disconnected.exception.code, 4401)
        self.assertEqual(project1.connections_project1, [])
        self.assertEqual(project2.connections_project2, [])

    def test_both_authenticated_websockets_receive_full_snapshot(self):
        claims = {"uid": "user", "exp": time.time() + 60}
        for route, module in (("ws_project1", project1), ("ws_project2", project2)):
            with self.subTest(route=route), patch.object(security, "verify_browser_token", return_value=claims):
                with TestClient(self.app) as client, client.websocket_connect(
                        f"/api/v1/{route}", headers={"Origin": "https://khodalmaa.in"}) as ws:
                    ws.send_json({"type": "authenticate", "token": "valid-test"})
                    self.assertTrue(ws.receive_json()["__full_snapshot"])

    def test_auth_service_outage_closes_websocket_without_data(self):
        with patch.object(security, "verify_browser_token", side_effect=RuntimeError("private-key")):
            with TestClient(self.app) as client, client.websocket_connect("/api/v1/ws_project1") as ws:
                ws.send_json({"type": "authenticate", "token": "valid-test"})
                with self.assertRaises(WebSocketDisconnect) as disconnected:
                    ws.receive_json()
                self.assertEqual(disconnected.exception.code, 1013)

    def test_missing_or_expired_claim_cannot_publish_initial_snapshot(self):
        for expiry in (None, 0, float("nan"), True):
            with self.subTest(expiry=expiry), patch.object(security, "verify_browser_token",
                                                         return_value={"uid": "user", "exp": expiry}):
                with TestClient(self.app) as client, client.websocket_connect("/api/v1/ws_project1") as ws:
                    ws.send_json({"type": "authenticate", "token": "test-token"})
                    with self.assertRaises(WebSocketDisconnect) as disconnected:
                        ws.receive_json()
                    self.assertEqual(disconnected.exception.code, 4401)

    def test_websocket_origin_gate_and_auth_deadline(self):
        async def exercise():
            blocked = AsyncMock(headers={"origin": "https://untrusted.example"})
            self.assertIsNone(await security.authenticate_websocket(blocked))
            blocked.accept.assert_not_awaited()
            blocked.close.assert_awaited_once_with(code=4403)
            stalled = AsyncMock(headers={})
            stalled.receive_text.side_effect = asyncio.TimeoutError
            self.assertIsNone(await security.authenticate_websocket(stalled))
            stalled.send_json.assert_not_awaited()
            stalled.close.assert_awaited_once_with(code=4401)
        asyncio.run(exercise())

    def test_expired_websocket_token_closes_before_more_frames(self):
        async def exercise():
            ws = AsyncMock()
            with self.assertRaises(ValueError):
                await security.receive_authenticated_text(ws, {"uid": "user", "exp": 0})
            ws.receive_text.assert_not_awaited()
            ws.close.assert_awaited_once_with(code=4401)
        asyncio.run(exercise())


class RequestBodyLimitTests(unittest.TestCase):
    def parse(self, chunks, headers=None, allow_empty=False):
        class Request:
            def __init__(self):
                self.headers = headers or {}
                self.chunks_read = 0
            async def stream(self):
                for chunk in chunks:
                    self.chunks_read += 1
                    yield chunk
        request = Request()
        return request, json_object(request, allow_empty=allow_empty)

    def test_content_length_rejects_oversize_before_streaming(self):
        request, operation = self.parse([b"{}"], {"content-length": str(MAX_JSON_BYTES + 1)})
        with self.assertRaises(HTTPException) as error:
            asyncio.run(operation)
        self.assertEqual(error.exception.status_code, 413)
        self.assertEqual(request.chunks_read, 0)

    def test_chunked_and_dishonest_content_length_cannot_bypass_limit(self):
        for headers in ({}, {"content-length": "2"}):
            with self.subTest(headers=headers):
                _, operation = self.parse([b" " * MAX_JSON_BYTES, b"{}"], headers)
                with self.assertRaises(HTTPException) as error:
                    asyncio.run(operation)
                self.assertEqual(error.exception.status_code, 413)

    def test_valid_chunked_object_empty_and_bad_json(self):
        _, operation = self.parse([b'{"machine', b'1": [0]}'])
        self.assertEqual(asyncio.run(operation), {"machine1": [0]})
        _, operation = self.parse([], allow_empty=True)
        self.assertEqual(asyncio.run(operation), {})
        for body in (b"[]", b"broken", b'{"x":' + b"[" * 2000):
            _, operation = self.parse([body])
            with self.assertRaises(HTTPException) as error:
                asyncio.run(operation)
            self.assertEqual(error.exception.status_code, 400)


class MachineCorsCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.add_middleware(security.MachineCompatibleCORSMiddleware)
        for router in (HistoryRouter, project1.Project1Router, project2.Project2Router):
            self.app.include_router(router)

    def options(self, path, origin="https://external-machine.example", mode="legacy", keys="[]", method="POST"):
        with patch.dict(os.environ, {"MACHINE_INGESTION_MODE": mode,
                                    "MACHINE_INGESTION_CREDENTIALS_JSON": keys,
                                    "EXTRA_BROWSER_ORIGINS": ""}), TestClient(self.app) as client:
            return client.options(path, headers={"Origin": origin, "Access-Control-Request-Method": method,
                                                "Access-Control-Request-Headers": "Content-Type"})

    def test_legacy_real_producer_preflights_preserve_unknown_origins(self):
        for path in ("/api/v1/project1_data", "/api/v1/project2_data"):
            response = self.options(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["access-control-allow-origin"], "*")
            self.assertNotIn("access-control-allow-credentials", response.headers)
            self.assertEqual(self.options(path, method="DELETE").status_code, 400)

    def test_legacy_actual_machine_post_preserves_origin_response(self):
        with patch.dict(os.environ, {"MACHINE_INGESTION_MODE": "legacy", "MACHINE_INGESTION_CREDENTIALS_JSON": "[]"}), \
                TestClient(self.app) as client:
            response = client.post("/api/v1/project2_data", json={}, headers={"Origin": "https://external-machine.example"})
        self.assertEqual(response.status_code, 400)  # Invalid data is still rejected.
        self.assertEqual(response.headers["access-control-allow-origin"], "*")

    def test_protected_routes_remain_exact_origin_in_legacy_mode(self):
        for path in ("/api/v1/history", "/api/v1/history/health", "/api/v1/project2_temporary_data"):
            self.assertEqual(self.options(path).status_code, 400)
            trusted = self.options(path, origin="https://khodalmaa.in")
            self.assertEqual(trusted.status_code, 200)
            self.assertEqual(trusted.headers["access-control-allow-origin"], "https://khodalmaa.in")

    def test_required_or_invalid_configuration_never_gets_legacy_cors(self):
        keys = json.dumps([MachineCredentialTests().credential()])
        for mode, configuration in (("required", keys), ("required", "[]"), ("legacy", "invalid-json")):
            for path in ("/api/v1/project1_data", "/api/v1/project2_data"):
                response = self.options(path, mode=mode, keys=configuration)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("access-control-allow-origin", response.headers)


class MachineCredentialTests(unittest.TestCase):
    secret = "test-only-secret-" + "x" * 32
    other_secret = "test-only-rotated-secret-" + "y" * 32

    def credential(self, identity="sender-a", secret=None):
        return {"id": identity, "sha256": hashlib.sha256((secret or self.secret).encode()).hexdigest(),
                "projects": ["project10"], "machines": ["machine1"]}

    def authorize(self, header=None, mode="legacy", credentials=None, project="project10", data=None):
        request = SimpleNamespace(headers={"authorization": header} if header else {})
        with patch.dict(os.environ, {"MACHINE_INGESTION_MODE": mode,
                                   "MACHINE_INGESTION_CREDENTIALS_JSON": json.dumps(credentials or [])}):
            return security.authorize_machine_request(request, project, data or {"machine1": [0] * 10})

    def test_legacy_existing_producers_keep_contract_and_bad_machine_key_never_falls_back(self):
        self.assertFalse(self.authorize()["authenticated"])
        self.assertFalse(self.authorize("Bearer historical-other-header")["authenticated"])
        for token in ("Machine bad", "Machine sender-a.wrong", "Machine missing." + self.secret):
            with self.subTest(token=token.split(".")[0]), self.assertRaises(HTTPException) as error:
                self.authorize(token, credentials=[self.credential()])
            self.assertEqual(error.exception.status_code, 401)

    def test_required_credentials_validate_hash_and_scope(self):
        key = "Machine sender-a." + self.secret
        self.assertTrue(self.authorize(key, "required", [self.credential()])["authenticated"])
        for project, data in (("project220", None), ("project10", {"machine2": [0] * 10}),
                              ("project10", {"machine1": [0] * 10, "machine2": [0] * 10})):
            with self.subTest(project=project), self.assertRaises(HTTPException) as error:
                self.authorize(key, "required", [self.credential()], project, data)
            self.assertEqual(error.exception.status_code, 403)
        with self.assertRaises(HTTPException) as error:
            self.authorize(mode="required", credentials=[self.credential()])
        self.assertEqual(error.exception.status_code, 401)

    def test_rotation_accepts_two_scoped_keys_then_revokes_removed_key(self):
        original, rotated = self.credential(), self.credential("sender-b", self.other_secret)
        for name, secret in (("sender-a", self.secret), ("sender-b", self.other_secret)):
            self.assertTrue(self.authorize(f"Machine {name}.{secret}", "required", [original, rotated])["authenticated"])
        with self.assertRaises(HTTPException) as error:
            self.authorize("Machine sender-a." + self.secret, "required", [rotated])
        self.assertEqual(error.exception.status_code, 401)

    def test_invalid_configuration_and_required_without_keys_fail_closed(self):
        for mode, credentials in (("required", []), ("misspelled", []), ("legacy", [{"id": "invalid"}]),
                                  ("legacy", [self.credential(), self.credential()])):
            with self.subTest(mode=mode), self.assertRaises(HTTPException) as error:
                self.authorize(mode=mode, credentials=credentials)
            self.assertEqual(error.exception.status_code, 503)
        with patch.dict(os.environ, {"MACHINE_INGESTION_CREDENTIALS_JSON": "{invalid"}):
            with self.assertRaises(HTTPException) as error:
                security.machine_security_status()
            self.assertEqual(error.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
