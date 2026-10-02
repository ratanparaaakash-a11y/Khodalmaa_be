"""Temporary Project10 machines must never become permanent or recorded data."""

import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException

from history import capture
from project2 import project2, temporary
from test_session_capture import CaptureTestCase, p10


# Project10's ten positions represent 1,2,3,4,5,6,7,8,9,0, respectively.
MACHINE4_VALUES = [0, 0, 0, 4000, 4000, 4000, 0, 4000, 0, 4000]


class TemporaryProject10TestCase(CaptureTestCase):
    def setUp(self):
        super().setUp()
        for module, replacements in (
            (temporary, {"_entries": {}}),
            (project2, {
                "latest_project2_data": {}, "connections_project2": [],
                "last_project2_hit_at": None, "project2_session_started_at": None,
                "_data_lock": asyncio.Lock(), "_broadcast_lock": asyncio.Lock(),
                "_broadcast_revision": 0, "_temporary_wake": asyncio.Event(),
            }),
        ):
            for name, value in replacements.items():
                mocked = patch.object(module, name, value)
                mocked.start()
                self.addCleanup(mocked.stop)
        self.app = FastAPI()
        self.app.include_router(project2.Project2Router)
        self.now = self.s1 + 10

    async def post(self, data, path="/api/v1/project2_data"):
        # Content, rather than httpx's strict JSON encoder, lets validation tests
        # exercise incoming non-finite JSON values without bypassing the router.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        ) as client:
            return await client.post(
                path, content=json.dumps(data),
                headers={"Content-Type": "application/json"},
            )

    def request(self, data, now=None, path="/api/v1/project2_data"):
        instant = self.now if now is None else now
        with patch.object(project2.time, "time", return_value=instant):
            return asyncio.run(self.post(data, path))

    def snapshot(self, now=None):
        instant = self.now if now is None else now
        with patch.object(project2.time, "time", return_value=instant):
            return project2.with_project2_meta(project2.latest_project2_data)

    def create(self, **kwargs):
        return temporary.add(
            {"Machine4": MACHINE4_VALUES}, 1800, now=self.now, **kwargs
        )


class TemporaryProject10StorageTests(TemporaryProject10TestCase):
    def test_exact_values_and_absolute_deadline_are_durable_and_not_history(self):
        entry = self.create()
        self.assertEqual(entry["expires_at"], self.now + 1800)
        self.assertEqual(entry["created_at"], self.now)
        self.assertEqual(temporary.live_data(now=self.now), {"machine4": MACHINE4_VALUES})
        self.assertEqual(sum(temporary.live_data(now=self.now)["machine4"]), 20000)
        self.assertEqual(temporary.next_deadline(), self.now + 1800)
        self.assertEqual(list(Path(self.temp.name).glob("*.json")), [])
        self.assertFalse((Path(self.temp.name) / "captures").exists())
        self.assertTrue((Path(self.temp.name) / "temporary" / "project10.json").is_file())

    def test_restart_retains_original_deadline_and_exact_expiry_boundary(self):
        self.create()
        temporary._entries.clear()
        temporary.restore(now=self.now + 900)
        self.assertEqual(temporary.next_deadline(), self.now + 1800)
        self.assertEqual(temporary.live_data(now=self.now + 1799.999), {"machine4": MACHINE4_VALUES})
        self.assertEqual(temporary.live_data(now=self.now + 1800), {})
        self.assertTrue(temporary.expire(now=self.now + 1800))
        temporary._entries.clear()
        temporary.restore(now=self.now + 1801)
        self.assertEqual(temporary.live_data(now=self.now + 1801), {})
        self.assertIsNone(temporary.next_deadline())

    def test_restart_after_deadline_does_not_resurrect_machine(self):
        self.create()
        temporary._entries.clear()
        temporary.restore(now=self.now + 1800)
        self.assertEqual(temporary.live_data(now=self.now + 1800), {})
        self.assertIsNone(temporary.next_deadline())

    def test_duration_rejects_bool_noninteger_nonfinite_and_out_of_range(self):
        for duration in (True, False, None, "1800", 1.5, float("nan"), float("inf"), 0, -1, 1801):
            with self.subTest(duration=duration), self.assertRaises(HTTPException) as raised:
                temporary.add({"machine4": MACHINE4_VALUES}, duration, now=self.now)
            self.assertEqual(raised.exception.status_code, 400)
            self.assertEqual(temporary.live_data(now=self.now), {})

    def test_machine_requires_exactly_one_complete_numeric_ten_entry_list(self):
        invalid = [
            {}, {"machine4": MACHINE4_VALUES, "machine5": MACHINE4_VALUES},
            {"machine4": MACHINE4_VALUES[:-1]}, {"machine4": {"0": 4000}},
            {"machine4": MACHINE4_VALUES + [0]}, {"other4": MACHINE4_VALUES},
            {"machine": MACHINE4_VALUES}, {"machine4/../x": MACHINE4_VALUES},
        ]
        for bad in (True, None, "4000", float("nan"), float("inf"), -float("inf")):
            invalid.append({"machine4": [bad] + MACHINE4_VALUES[1:]})
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(HTTPException) as raised:
                temporary.add(data, 1800, now=self.now)
            self.assertEqual(raised.exception.status_code, 400)
            self.assertEqual(temporary.live_data(now=self.now), {})

    def test_real_machine_and_active_overlay_conflicts_preserve_existing_data(self):
        with self.assertRaises(HTTPException) as raised:
            self.create(occupied=("machine4",))
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(temporary.live_data(now=self.now), {})
        self.create()
        with self.assertRaises(HTTPException) as raised:
            temporary.add({"machine4": [999] * 10}, 1800, now=self.now + 500)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(temporary.next_deadline(), self.now + 1800)
        self.assertEqual(temporary.live_data(now=self.now + 500), {"machine4": MACHINE4_VALUES})

    def test_real_sender_supersedes_overlay_without_later_resurrection(self):
        self.create()
        self.assertTrue(temporary.remove_for_real(["machine4"]))
        self.assertEqual(temporary.live_data(now=self.now), {})
        temporary._entries.clear()
        temporary.restore(now=self.now + 1)
        self.assertEqual(temporary.live_data(now=self.now + 1), {})

    def test_restart_real_capture_wins_over_older_overlay(self):
        self.create()
        temporary._entries.clear()
        temporary.restore(now=self.now + 1, occupied=("machine4",))
        self.assertEqual(temporary.live_data(now=self.now + 1), {})
        temporary._entries.clear()
        temporary.restore(now=self.now + 2)
        self.assertEqual(temporary.live_data(now=self.now + 2), {})

    def test_failed_atomic_write_cannot_acknowledge_or_publish_undurable_overlay(self):
        with patch.object(temporary.os, "replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.create()
        self.assertEqual(temporary.live_data(now=self.now), {})
        temporary._entries.clear()
        temporary.restore(now=self.now + 1)
        self.assertEqual(temporary.live_data(now=self.now + 1), {})


class TemporaryProject10ApiTests(TemporaryProject10TestCase):
    def test_distinct_temporary_route_confirms_exact_values_and_expiry(self):
        with patch.object(capture, "accept_data") as accept:
            response = self.request(
                {"Machine4": MACHINE4_VALUES, "__temporary_for_seconds": 1800},
                path="/api/v1/project2_temporary_data",
            )
        self.assertEqual(response.status_code, 200, response.text)
        accept.assert_not_called()
        self.assertEqual(response.json()["data"], {"machine4": MACHINE4_VALUES})
        self.assertEqual(response.json()["temporary"], {
            "name": "machine4", "created_at": self.now,
            "expires_at": self.now + 1800, "duration_seconds": 1800,
        })

    def test_distinct_temporary_route_cannot_accept_a_permanent_machine_payload(self):
        with patch.object(capture, "accept_data") as accept:
            response = self.request(
                {"Machine4": MACHINE4_VALUES}, path="/api/v1/project2_temporary_data"
            )
        self.assertEqual(response.status_code, 400, response.text)
        accept.assert_not_called()
        self.assertEqual(project2.latest_project2_data, {})
        self.assertEqual(temporary.live_data(now=self.now), {})

    def test_temporary_request_broadcasts_complete_view_without_capture_or_session_change(self):
        real = {"machine1": [100] * 10, "machine2": [200] * 10}
        project2.latest_project2_data.update(copy.deepcopy(real))
        project2.project2_session_started_at = self.s1
        project2.last_project2_hit_at = self.now - 1
        with patch.object(capture, "accept_data") as accept, \
                patch.object(capture, "schedule_capture") as schedule, \
                patch.object(project2, "broadcast_project2_data", new_callable=AsyncMock) as broadcast:
            response = self.request({"Machine4": MACHINE4_VALUES, "__temporary_for_seconds": 1800})
        self.assertEqual(response.status_code, 200, response.text)
        accept.assert_not_called()
        schedule.assert_not_called()
        self.assertEqual(project2.latest_project2_data, real)
        self.assertEqual(project2.project2_session_started_at, self.s1)
        self.assertEqual(project2.last_project2_hit_at, self.now - 1)
        payload = broadcast.call_args.args[0]
        self.assertEqual({key: value for key, value in payload.items() if not key.startswith("__")},
                         {**real, "machine4": MACHINE4_VALUES})
        self.assertTrue(payload["__full_snapshot"])
        self.assertNotIn("__session_reset", payload)

    def test_invalid_temporary_request_never_falls_through_to_live_capture(self):
        for duration in (True, None, "1800", 0, 1801, float("nan")):
            with self.subTest(duration=duration), patch.object(capture, "accept_data") as accept:
                response = self.request({"machine4": MACHINE4_VALUES, "__temporary_for_seconds": duration})
                self.assertEqual(response.status_code, 400, response.text)
                accept.assert_not_called()
        self.assertEqual(project2.latest_project2_data, {})

    def test_conflict_is_visible_and_keeps_real_machine_unchanged(self):
        project2.latest_project2_data["machine4"] = [123] * 10
        with patch.object(capture, "accept_data") as accept:
            response = self.request({"Machine4": MACHINE4_VALUES, "__temporary_for_seconds": 1800})
        self.assertEqual(response.status_code, 409, response.text)
        accept.assert_not_called()
        self.assertEqual(self.snapshot()["machine4"], [123] * 10)
        self.assertEqual(temporary.live_data(now=self.now), {})

    def test_live_peer_updates_preserve_overlay_and_capture_only_real_machines(self):
        with patch.object(capture, "schedule_capture"):
            self.assertEqual(self.request(p10(100, "machine2")).status_code, 200)
            self.assertEqual(self.request({"machine4": MACHINE4_VALUES, "__temporary_for_seconds": 1800}).status_code, 200)
            self.assertEqual(self.request(p10(200, "machine1"), now=self.now + 5).status_code, 200)
        snapshot = self.snapshot(now=self.now + 5)
        self.assertEqual(snapshot["machine1"], p10(200)["machine1"])
        self.assertEqual(snapshot["machine2"], p10(100, "machine2")["machine2"])
        self.assertEqual(snapshot["machine4"], MACHINE4_VALUES)
        key = capture.capture_key("project10", capture.scheduled_session(self.now))
        checkpoint = capture.read_checkpoint(key)
        self.assertEqual(set(checkpoint["data"]), {"machine1", "machine2"})
        capture.flush_capture(key, now=self.s1_end)
        self.assertNotIn("machine4", json.dumps(checkpoint["data"]))

    def test_new_session_reset_does_not_delete_still_unexpired_overlay(self):
        before = self.s1 - 10
        project2.latest_project2_data.update(p10(50, "machine2"))
        self.assertEqual(self.request({"machine4": MACHINE4_VALUES, "__temporary_for_seconds": 1800}, now=before).status_code, 200)
        with patch.object(capture, "schedule_capture"), \
                patch.object(project2, "broadcast_project2_data", new_callable=AsyncMock) as broadcast:
            response = self.request(p10(200, "machine1"), now=self.s1)
        self.assertEqual(response.status_code, 200, response.text)
        snapshot = self.snapshot(now=self.s1)
        self.assertEqual(snapshot["machine4"], MACHINE4_VALUES)
        self.assertEqual(snapshot["machine1"], p10(200)["machine1"])
        self.assertEqual(temporary.next_deadline(), before + 1800)
        self.assertTrue(broadcast.call_args.args[0]["__session_reset"])

    def test_real_same_name_sender_survives_temporary_deadline_and_restart(self):
        self.assertEqual(self.request({"machine4": MACHINE4_VALUES, "__temporary_for_seconds": 1800}).status_code, 200)
        with patch.object(capture, "schedule_capture"):
            response = self.request(p10(900, "machine4"), now=self.now + 1)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(temporary.live_data(now=self.now + 1), {})
        temporary.expire(now=self.now + 1800)
        temporary._entries.clear()
        temporary.restore(now=self.now + 1801)
        self.assertEqual(self.snapshot(now=self.now + 1801)["machine4"], p10(900, "machine4")["machine4"])
        self.assertEqual(temporary.live_data(now=self.now + 1801), {})

    def test_deadline_loop_broadcasts_removal_without_any_further_machine_input(self):
        async def exercise():
            self.create()
            project2.latest_project2_data.update(p10(100, "machine1"))
            project2.project2_session_started_at = self.s1
            instant = [self.now]
            timeouts = []
            sent = asyncio.Event()
            payloads = []
            real_wait_for = asyncio.wait_for

            async def reach_deadline(waiter, timeout):
                timeouts.append(timeout)
                waiter.close()
                instant[0] += timeout
                raise asyncio.TimeoutError

            async def record_broadcast(payload, revision):
                payloads.append((copy.deepcopy(payload), revision))
                sent.set()

            with patch.object(project2.time, "time", side_effect=lambda: instant[0]), \
                    patch.object(project2.asyncio, "wait_for", side_effect=reach_deadline), \
                    patch.object(project2, "broadcast_project2_data", side_effect=record_broadcast), \
                    patch.object(capture, "accept_data") as accept:
                task = asyncio.create_task(project2.temporary_expiry_loop())
                try:
                    await real_wait_for(sent.wait(), timeout=2)
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                accept.assert_not_called()
            self.assertEqual(timeouts, [1800])
            self.assertEqual(len(payloads), 1)
            payload, revision = payloads[0]
            self.assertEqual(payload["machine1"], p10(100)["machine1"])
            self.assertNotIn("machine4", payload)
            self.assertNotIn("__temporary_machines", payload)
            self.assertNotIn("__session_reset", payload)
            self.assertTrue(payload["__full_snapshot"])
            self.assertEqual(revision, 1)
            self.assertEqual(temporary.live_data(now=instant[0]), {})
            temporary._entries.clear()
            temporary.restore(now=instant[0])
            self.assertEqual(temporary.live_data(now=instant[0]), {})

        asyncio.run(exercise())

    def test_delayed_broadcast_cannot_reintroduce_an_expired_temporary_machine(self):
        async def exercise():
            self.create()
            project2.latest_project2_data.update(p10(100, "machine1"))
            snapshot = self.snapshot()
            socket = AsyncMock()
            project2.connections_project2.append(socket)
            with patch.object(project2.time, "time", return_value=self.now + 1800):
                await project2.broadcast_project2_data(snapshot, project2._broadcast_revision)
            socket.send_json.assert_awaited_once()
            sent = socket.send_json.call_args.args[0]
            self.assertEqual(sent["machine1"], p10(100)["machine1"])
            self.assertNotIn("machine4", sent)
            self.assertNotIn("__temporary_machines", sent)

        asyncio.run(exercise())

    def test_expired_machine_is_removed_from_connected_clients_even_if_disk_cleanup_fails(self):
        async def exercise():
            self.create()
            project2.latest_project2_data.update(p10(100, "machine1"))
            sent = asyncio.Event()
            payloads = []

            async def record_broadcast(payload, revision):
                payloads.append(copy.deepcopy(payload))
                sent.set()

            with patch.object(project2.time, "time", return_value=self.now + 1800), \
                    patch.object(temporary, "expire", side_effect=OSError("disk unavailable")), \
                    patch.object(project2, "broadcast_project2_data", side_effect=record_broadcast):
                task = asyncio.create_task(project2.temporary_expiry_loop())
                try:
                    await asyncio.wait_for(sent.wait(), timeout=2)
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
            self.assertNotIn("machine4", payloads[0])
            self.assertEqual(payloads[0]["machine1"], p10(100)["machine1"])
            self.assertTrue(payloads[0]["__full_snapshot"])

        asyncio.run(exercise())


if __name__ == "__main__":
    import unittest
    unittest.main()
