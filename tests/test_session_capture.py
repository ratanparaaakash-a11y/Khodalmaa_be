import asyncio
import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException

from history import capture
from history import session_history as history
from history.history import HistoryRouter, history_health
from history.rules import HARDCODED_NUM1
from project1 import project1
from project2 import project2


def epoch(iso):
    return datetime.fromisoformat(iso + "+05:30").timestamp()


def p10(amount=1, machine="machine1"):
    return {machine: [amount + i for i in range(10)]}


def p220():
    return {"machine1": {str(column): [f" {number} -> {index + 1.125} "
                                         for index, number in enumerate(numbers)]
                         for column, numbers in HARDCODED_NUM1.items()}}


class CaptureTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        replacements = {
            "_file_store_dir": Path(self.temp.name), "_memory_docs": {},
            "_pending_cloud_docs": {}, "_cloud_reconciled": False,
            "_last_firestore_error": None, "_last_firestore_success_at": None,
            "_firestore_disabled_until": 0,
        }
        for name, value in replacements.items():
            mocked = patch.object(history, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        for name, value in {"_tasks": {}, "_errors": {}}.items():
            mocked = patch.object(capture, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        for name, value in (("write_cloud_doc", False), ("load_firestore_docs", [])):
            mocked = patch.object(history, name, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.s1 = epoch("2026-09-28T21:30:00")
        self.s1_end = epoch("2026-09-28T22:05:00")
        self.s2 = epoch("2026-09-28T23:30:00")
        self.s2_end = epoch("2026-09-29T00:15:00")


class ScheduleTests(CaptureTestCase):
    def test_exact_boundaries_and_midnight_date(self):
        for timestamp, expected in (
            (self.s1 - .001, None), (self.s1, 1), (self.s1_end - .001, 1),
            (self.s1_end, None), (self.s2 - .001, None), (self.s2, 2),
            (epoch("2026-09-29T00:00:00"), 2), (self.s2_end - .001, 2),
            (self.s2_end, None), (epoch("2026-09-29T12:00:00"), None),
        ):
            with self.subTest(timestamp=timestamp):
                window = capture.scheduled_session(timestamp)
                self.assertEqual(window["session"] if window else None, expected)
                if window:
                    self.assertEqual(window["business_date"], "2026-09-28")

    def test_identity_is_scheduled_start_not_machine_start(self):
        first = capture.accept_data("project10", p10(), self.s1 + 500)
        later = capture.accept_data("project10", p10(5), self.s1 + 1700)
        self.assertEqual(first["session_started_at"], self.s1)
        self.assertEqual(first["session_started_at"], later["session_started_at"])

    def test_only_two_canonical_slots_and_outside_updates_do_not_overwrite(self):
        first = capture.accept_data("project10", p10(), self.s1)
        capture.flush_capture(first["key"], now=self.s1_end)
        second = capture.accept_data("project10", p10(4), self.s2)
        capture.flush_capture(second["key"], now=self.s2_end)
        originals = {path.name: path.read_bytes() for path in Path(self.temp.name).glob("*.json")}
        outside = capture.accept_data("project10", p10(900), self.s2_end + 4000)
        self.assertFalse(outside["recording"])
        self.assertEqual(len(originals), 2)
        self.assertEqual(originals, {path.name: path.read_bytes() for path in Path(self.temp.name).glob("*.json")})

    def test_current_snapshot_outside_window_cannot_save_stale_data(self):
        capture.accept_data("project10", p10(), self.s1)
        result = capture.snapshot_current("project10", now=self.s1_end)
        self.assertFalse(result["saved"])
        self.assertIn("Outside", result["reason"])
        self.assertFalse(list(Path(self.temp.name).glob("*.json")))

    def test_current_snapshot_without_current_window_capture_does_not_copy_old_session(self):
        capture.accept_data("project10", p10(), self.s1)
        result = capture.snapshot_current("project10", now=self.s2)
        self.assertFalse(result["saved"])

    def test_mutation_session_3_or_wrong_window_rejected(self):
        for invalid in (0, 3, -1, True, 1.5, "3"):
            with self.subTest(invalid=invalid), self.assertRaises(HTTPException):
                capture.snapshot_current("project10", invalid, self.s1)
        with self.assertRaises(HTTPException):
            capture.snapshot_current("project10", 2, self.s1)
        with self.assertRaises(HTTPException):
            history.save_built_session_snapshot("project10", "2026-09-28", 3, [])
        self.assertEqual(history.safe_session(3), 2)  # Existing read API compatibility.

    def test_payload_ignores_old_start_token_and_uses_receipt_time(self):
        with patch.object(history.time, "time", return_value=self.s2 + 30):
            result = history.save_session_snapshot("project10", p10(), self.s1, "import")
        self.assertEqual(result["session"], 2)
        self.assertEqual(result["business_date"], "2026-09-28")


class DurabilityTests(CaptureTestCase):
    def test_restart_restores_all_machines_before_merge(self):
        first = capture.accept_data("project10", p10(machine="machine1"), self.s1)
        # There is no in-memory capture store: the next receipt must read disk.
        history._memory_docs.clear()
        second = capture.accept_data("project10", p10(5, "machine2"), self.s1 + 40)
        self.assertEqual(set(second["data"]), {"machine1", "machine2"})
        self.assertEqual(first["key"], second["key"])
        self.assertEqual(second["version"], 2)

    def test_pending_capture_recovers_after_restart_without_new_input(self):
        accepted = capture.accept_data("project220", p220(), self.s1_end - 30)
        self.assertFalse(list(Path(self.temp.name).glob("*.json")))
        history._memory_docs.clear()
        history._pending_cloud_docs.clear()
        capture.recover_pending(now=self.s1_end)
        saved = history.load_file_doc(accepted["key"])
        self.assertEqual(saved["entry_count"], 110)
        self.assertEqual(saved["business_date"], "2026-09-28")
        checkpoint = capture.read_checkpoint(accepted["key"])
        self.assertEqual(checkpoint["version"], checkpoint["flushed_version"])

    def test_midnight_capture_finalized_after_4am_keeps_original_business_date(self):
        accepted = capture.accept_data("project10", p10(), epoch("2026-09-29T00:14:59"))
        capture.recover_pending(now=epoch("2026-09-29T08:00:00"))
        self.assertEqual(history.load_file_doc(accepted["key"])["business_date"], "2026-09-28")
        self.assertEqual(accepted["key"], "2026-09-28-s2-project10")

    def test_continuous_traffic_flushes_by_fixed_close(self):
        for received in range(int(self.s1_end - 200), int(self.s1_end), 20):
            accepted = capture.accept_data("project10", p10(received - self.s1_end + 201), received)
        self.assertFalse(capture.flush_capture(accepted["key"], now=self.s1_end - 1)["saved"])
        self.assertTrue(capture.flush_capture(accepted["key"], now=self.s1_end)["saved"])
        self.assertEqual(capture.read_checkpoint(accepted["key"])["flushed_version"], 10)

    def test_debounce_still_saves_after_quiet_period(self):
        accepted = capture.accept_data("project10", p10(), self.s1)
        self.assertFalse(capture.flush_capture(accepted["key"], now=self.s1 + 89)["saved"])
        self.assertTrue(capture.flush_capture(accepted["key"], now=self.s1 + 90)["saved"])

    def test_cloud_failure_keeps_durable_snapshot_for_existing_retry(self):
        accepted = capture.accept_data("project10", p10(), self.s1)
        self.assertTrue(capture.flush_capture(accepted["key"], now=self.s1_end)["saved"])
        self.assertIn(accepted["key"], history._pending_cloud_docs)
        self.assertIsNotNone(history.load_file_doc(accepted["key"]))

    def test_disk_capture_failure_is_visible_and_not_acknowledged(self):
        with patch.object(capture, "atomic_checkpoint", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                capture.accept_data("project10", p10(), self.s1)
        self.assertEqual(capture.recording_status()["state"], "degraded")
        self.assertEqual(history._memory_docs, {})

    def test_canonical_disk_failure_retains_pending_capture(self):
        accepted = capture.accept_data("project10", p10(), self.s1)
        with patch.object(history, "save_file_doc", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                capture.flush_capture(accepted["key"], now=self.s1_end)
        self.assertEqual(capture.read_checkpoint(accepted["key"])["flushed_version"], 0)
        self.assertEqual(history._memory_docs, {})
        self.assertEqual(capture.recording_status()["pending_captures"], 1)
        capture.recover_pending(now=self.s1_end)
        self.assertEqual(capture.recording_status()["pending_captures"], 0)
        self.assertNotEqual(capture.recording_status()["state"], "degraded")

    def test_old_finalizer_cannot_acknowledge_newer_receipt(self):
        accepted = capture.accept_data("project10", p10(), self.s1)
        writing = threading.Event()
        release = threading.Event()

        def delayed_cloud(*args):
            writing.set()
            self.assertTrue(release.wait(3))
            return False

        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(history, "write_cloud_doc", side_effect=delayed_cloud):
            old_flush = pool.submit(capture.flush_capture, accepted["key"], True, self.s1 + 90)
            self.assertTrue(writing.wait(3))
            new_receipt = pool.submit(capture.accept_data, "project10", p10(9), self.s1 + 91)
            release.set()
            self.assertTrue(old_flush.result(timeout=3)["saved"])
            new_receipt.result(timeout=3)
        checkpoint = capture.read_checkpoint(accepted["key"])
        self.assertEqual(checkpoint["version"], 2)
        self.assertEqual(checkpoint["flushed_version"], 1)
        self.assertEqual(checkpoint["data"]["machine1"][0], 9)

    def test_corrupt_checkpoint_is_not_silently_replaced(self):
        accepted = capture.accept_data("project10", p10(), self.s1)
        path = capture.checkpoint_path(accepted["key"])
        path.write_text("broken-json", encoding="utf-8")
        with self.assertRaises(ValueError):
            capture.accept_data("project10", p10(6), self.s1 + 10)
        self.assertEqual(path.read_text(encoding="utf-8"), "broken-json")
        self.assertEqual(capture.recording_status()["state"], "degraded")

    def test_checkpoint_files_do_not_enter_history_averages(self):
        snapshot = history.build_snapshot("project10", p10(), "2026-09-25", 1, self.s1, "existing")
        history.save_doc(snapshot)
        before = history.analyze_average_history("project10", 7)
        existing = history.get_file_path(snapshot["business_date"] + "-s1-project10").read_bytes()
        capture.accept_data("project10", p10(10), self.s1)
        self.assertEqual(before, history.analyze_average_history("project10", 7))
        self.assertEqual(existing, history.get_file_path(snapshot["business_date"] + "-s1-project10").read_bytes())

    def test_health_exposes_schedule_and_recording_failure(self):
        capture.accept_data("project10", p10(), self.s1)
        capture.record_error("test", OSError("disk full"))
        result = asyncio.run(history_health())
        self.assertEqual(result["status"], "degraded")
        recording = result["storage"]["recording"]
        self.assertEqual(recording["schedule"]["S2"]["end"], "00:15")
        self.assertEqual(recording["pending_captures"], 1)


class ValidationTests(CaptureTestCase):
    def test_complete_project220_whitespace_zero_and_precision(self):
        data = p220()
        data["machine1"]["10"][-1] = "000 -> 1234567.125 "
        clean = capture.validate_data("project220", data)
        self.assertEqual(history.parse_arrow_entry(clean["machine1"]["10"][-1]), (0, 1234567.125))

    def test_near_equal_amounts_keep_original_ranking(self):
        data = p220()
        data["machine1"]["1"] = [f"{number}->1000" for number in HARDCODED_NUM1[1]]
        data["machine1"]["1"][0] = "128->100.123457"
        data["machine1"]["1"][1] = "137->100.123456"
        clean = capture.validate_data("project220", data)
        self.assertEqual(history.parse_arrow_entry(clean["machine1"]["1"][0])[1], 100.123457)
        self.assertEqual(history.parse_arrow_entry(clean["machine1"]["1"][1])[1], 100.123456)
        entries = history.build_project220_entries(clean)
        self.assertEqual([entry["number"] for entry in entries[:2]], ["137", "128"])

    def test_preexisting_finite_negative_amount_domain_is_preserved(self):
        self.assertEqual(capture.validate_data("project10", {"machine1": [-1] * 10})["machine1"][0], -1)
        data = p220()
        data["machine1"]["1"][0] = "128->-1.25"
        self.assertEqual(history.parse_arrow_entry(capture.validate_data("project220", data)["machine1"]["1"][0]), (128, -1.25))

    def test_empty_incomplete_nonfinite_or_duplicate_amounts_rejected(self):
        invalid220 = [None, {}, {"machine1": {}}, {"machine1": {"1": []}}]
        for mutation in ("missing", "nan", "duplicate"):
            data = p220()
            if mutation == "missing":
                data["machine1"]["4"].pop()
            elif mutation == "nan":
                data["machine1"]["4"][0] = "130->NaN"
            else:
                data["machine1"]["4"][0] = data["machine1"]["4"][1]
            invalid220.append(data)
        for data in invalid220:
            with self.subTest(data=str(data)[:50]), self.assertRaises(HTTPException):
                capture.accept_data("project220", data, self.s1)
        for values in ([], [1] * 9, [float("inf")] * 10, [True] * 10):
            with self.subTest(values=values), self.assertRaises(HTTPException):
                capture.accept_data("project10", {"machine1": values}, self.s1)
        self.assertFalse(capture.capture_directory().exists())


class RouteTests(CaptureTestCase):
    def setUp(self):
        super().setUp()
        self.app = FastAPI()
        self.app.include_router(project1.Project1Router)
        self.app.include_router(project2.Project2Router)
        self.app.include_router(HistoryRouter)
        for module, prefix in ((project1, "project1"), (project2, "project2")):
            for name, value in ((f"latest_{prefix}_data", {}), (f"{prefix}_session_started_at", None),
                                (f"last_{prefix}_hit_at", None), ("_data_lock", asyncio.Lock()),
                                ("_broadcast_lock", asyncio.Lock()), ("_broadcast_revision", 0),
                                (f"connections_{prefix}", [])):
                mocked = patch.object(module, name, value)
                mocked.start()
                self.addCleanup(mocked.stop)
        mocked = patch.object(capture, "schedule_capture")
        mocked.start()
        self.addCleanup(mocked.stop)

    def post(self, route, data, now):
        async def send():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
                return await client.post(route, json=data)
        with patch.object(capture.time, "time", return_value=now):
            return asyncio.run(send())

    def test_outside_update_displays_but_creates_no_history(self):
        result = self.post("/api/v1/project1_data", p220(), self.s1_end)
        self.assertEqual(result.status_code, 200)
        self.assertIn("machine1", project1.latest_project1_data)
        self.assertFalse(capture.capture_directory().exists())
        saved = self.post("/api/v1/history/snapshot-current", {"project": "project220"}, self.s1_end)
        self.assertFalse(saved.json()["results"][0]["saved"])

    def test_first_window_update_discards_pre_session_live_data(self):
        self.post("/api/v1/project2_data", p10(100, "machine2"), self.s1 - 30)
        result = self.post("/api/v1/project2_data", p10(), self.s1 + 10)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(set(project2.latest_project2_data), {"machine1"})
        self.assertEqual(project2.project2_session_started_at, self.s1)
        self.assertTrue(project2.with_project2_meta({}, True)["__session_reset"])

    def test_route_restores_capture_before_next_machine_arrives(self):
        capture.accept_data("project10", p10(), self.s1 + 20)
        result = self.post("/api/v1/project2_data", p10(4, "Machine2"), self.s1 + 50)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(set(result.json()["data"]), {"machine1", "machine2"})
        self.assertEqual(project2.project2_session_started_at, self.s1)

    def test_bad_machine_is_rejected_and_durability_error_returns_503(self):
        result = self.post("/api/v1/project2_data", {"machine1": []}, self.s1)
        self.assertEqual(result.status_code, 400)
        with patch.object(capture, "atomic_checkpoint", side_effect=OSError("disk full")):
            result = self.post("/api/v1/project2_data", p10(), self.s1)
        self.assertEqual(result.status_code, 503)
        self.assertEqual(project2.latest_project2_data, {})

    def test_reversed_background_tasks_cannot_send_stale_window_or_partial_data(self):
        class Request:
            def __init__(self, data):
                self.data = data

            async def json(self):
                return self.data

        class Socket:
            def __init__(self):
                self.messages = []

            async def send_json(self, payload):
                self.messages.append(copy.deepcopy(payload))

        async def exercise(module, endpoint, data1, data2, prefix):
            socket = Socket()
            getattr(module, f"connections_{prefix}").append(socket)
            first_tasks, second_tasks = BackgroundTasks(), BackgroundTasks()
            with patch.object(capture.time, "time", return_value=self.s1 + 20):
                await endpoint(Request(data1), first_tasks)
            with patch.object(capture.time, "time", return_value=self.s2 + 20):
                await endpoint(Request(data2), second_tasks)
            await second_tasks()
            await first_tasks()
            self.assertEqual(len(socket.messages), 1)
            self.assertEqual(socket.messages[0]["__session_started_at"], self.s2)
            self.assertEqual(socket.messages[0]["machine1"], capture.validate_data(
                "project220" if prefix == "project1" else "project10", data2)["machine1"])

        updated220 = p220()
        updated220["machine1"]["1"][0] = "128->999"
        asyncio.run(exercise(project1, project1.get_p1_data, p220(), updated220, "project1"))
        asyncio.run(exercise(project2, project2.get_p2_data, p10(), p10(9), "project2"))

    def test_skipped_outside_background_update_is_included_in_newest_full_payload(self):
        class Request:
            def __init__(self, data):
                self.data = data

            async def json(self):
                return self.data

        class Socket:
            messages = []

            async def send_json(self, payload):
                self.messages.append(copy.deepcopy(payload))

        async def exercise():
            socket = Socket()
            project2.connections_project2.append(socket)
            first_tasks, second_tasks = BackgroundTasks(), BackgroundTasks()
            with patch.object(capture.time, "time", return_value=self.s1 - 120):
                await project2.get_p2_data(Request(p10(machine="machine1")), first_tasks)
                await project2.get_p2_data(Request(p10(machine="machine2")), second_tasks)
            await second_tasks()
            await first_tasks()
            self.assertEqual(len(socket.messages), 1)
            self.assertEqual(set(socket.messages[0]), {"machine1", "machine2"})

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
