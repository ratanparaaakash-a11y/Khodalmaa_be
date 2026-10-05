"""Temporary cloud storage tests use fake checkpoints; never production writes."""

import asyncio
import sys
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx
from fastapi import HTTPException

from history import session_history as history
from project2 import project2, temporary, temporary_cloud
from test_temporary_project10 import TemporaryProject10TestCase, MACHINE4_VALUES


class TemporaryCloudPersistenceTests(TemporaryProject10TestCase):
    def reset_process(self, discard_cache=False):
        temporary._entries.clear()
        temporary._retired.clear()
        temporary._blocked_names.clear()
        temporary._real_generations.clear()
        if discard_cache:
            temporary.storage_path().unlink(missing_ok=True)

    def test_new_disk_restores_confirmed_cloud_values_and_original_deadline(self):
        created = self.create()
        self.reset_process(discard_cache=True)
        restored = temporary.restore(now=self.now + 900)
        self.assertEqual(restored["machine4"]["values"], MACHINE4_VALUES)
        self.assertEqual(restored["machine4"]["expires_at"], created["expires_at"])
        self.assertEqual(list(history._file_store_dir.glob("*.json")), [])
        self.assertFalse((history._file_store_dir / "captures").exists())

    def test_new_disk_never_resurrects_expired_cloud_row(self):
        self.create()
        self.reset_process(discard_cache=True)
        self.assertEqual(temporary.restore(now=self.now + 1800), {})
        self.assertEqual(self.cloud_checkpoint["entries"], {})
        self.assertIsNone(temporary.next_deadline())

    def test_cloud_read_or_write_failure_cannot_publish_creation(self):
        for operation in ("load", "save"):
            with self.subTest(operation=operation):
                with patch.object(temporary.cloud, operation,
                                  side_effect=temporary.cloud.CloudUnavailable("offline")):
                    with self.assertRaises(temporary.cloud.CloudUnavailable):
                        self.create()
                self.assertEqual(temporary.live_data(now=self.now), {})
                self.assertIsNone(self.cloud_checkpoint)
                self.assertFalse(temporary.storage_path().exists())

    def test_cached_rows_stay_hidden_if_startup_cloud_verification_fails(self):
        self.create()
        self.reset_process()
        with patch.object(temporary.cloud, "load",
                          side_effect=temporary.cloud.CloudUnavailable("offline")):
            with self.assertRaises(temporary.cloud.CloudUnavailable):
                temporary.restore(now=self.now + 1)
        self.assertEqual(temporary.live_entries(now=self.now + 1), {})
        self.assertEqual(temporary.storage_status()["cloud_error"], "offline")
        self.assertTrue(temporary.sync_cloud(now=self.now + 2))
        self.assertEqual(temporary.live_data(now=self.now + 2), {"machine4": MACHINE4_VALUES})

    def test_invalid_cloud_checkpoint_fails_closed_without_overwrite(self):
        for bad in ({}, {"version": 7, "entries": {}, "retired": {}},
                    {"version": 1, "entries": {"machine4": {}}, "retired": {}},
                    {"version": 1, "entries": {}, "retired": {"bad": 99}}):
            with self.subTest(bad=bad), patch.object(temporary.cloud, "load", return_value=(bad, "1")), \
                    patch.object(temporary.cloud, "save") as save:
                with self.assertRaises(temporary.cloud.CloudUnavailable):
                    temporary.restore(now=self.now)
                self.assertEqual(temporary.live_entries(now=self.now), {})
                save.assert_not_called()

    def test_conditional_conflict_retries_latest_record_without_losing_other_machine(self):
        first = True
        other = {"values": [12] * 10, "created_at": self.now,
                 "expires_at": self.now + 1800, "lease_id": "a" * 32}

        def save(checkpoint, revision):
            nonlocal first
            if first:
                first = False
                self.cloud_checkpoint = {"version": 1, "entries": {"machine5": other}, "retired": {}}
                self.cloud_revision = "1"
                raise temporary.cloud.CloudConflict("another writer")
            self.save_cloud(checkpoint, revision)

        with patch.object(temporary.cloud, "save", side_effect=save) as saved:
            self.create()
        self.assertEqual(saved.call_count, 2)
        self.assertEqual(set(self.cloud_checkpoint["entries"]), {"machine4", "machine5"})
        self.assertEqual(temporary.live_data(now=self.now)["machine5"], [12] * 10)

    def test_repeated_version_conflicts_are_bounded_and_do_not_publish(self):
        with patch.object(temporary.cloud, "save",
                          side_effect=temporary.cloud.CloudConflict("busy")) as save:
            with self.assertRaises(temporary.cloud.CloudUnavailable):
                self.create()
        self.assertEqual(save.call_count, 2)
        self.assertEqual(temporary.live_data(now=self.now), {})

    def test_retiring_real_machine_never_calls_cloud_and_survives_retained_cache_restart(self):
        created = self.create()
        temporary.cloud.load.reset_mock()
        temporary.cloud.save.reset_mock()
        self.assertTrue(temporary.remove_for_real(["Machine4"]))
        temporary.cloud.load.assert_not_called()
        temporary.cloud.save.assert_not_called()
        self.assertEqual(temporary.live_data(now=self.now), {})
        self.assertIn(created["lease_id"], temporary._retired)
        self.reset_process()
        self.assertEqual(temporary.restore(now=self.now + 1), {})
        self.assertIn(created["lease_id"], self.cloud_checkpoint["retired"])
        self.reset_process(discard_cache=True)
        self.assertEqual(temporary.restore(now=self.now + 2), {})

    def test_cloud_io_does_not_lock_out_real_machine_and_race_never_publishes(self):
        entered, release = threading.Event(), threading.Event()

        def waiting_save(checkpoint, revision):
            entered.set()
            if not release.wait(2):
                raise AssertionError("test did not release cloud request")
            self.save_cloud(checkpoint, revision)

        with patch.object(temporary.cloud, "save", side_effect=waiting_save), \
                ThreadPoolExecutor(max_workers=2) as pool:
            addition = pool.submit(self.create)
            self.assertTrue(entered.wait(1))
            retirement = pool.submit(temporary.remove_for_real, ["machine4"])
            try:
                self.assertFalse(retirement.result(timeout=0.5), "creation is not visible before cloud acknowledgement")
            finally:
                release.set()
            with self.assertRaises(HTTPException) as error:
                addition.result(timeout=1)
            self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(temporary.live_entries(now=self.now), {})
        temporary.sync_cloud(now=self.now + 1)
        self.assertEqual(self.cloud_checkpoint["entries"], {})

    def test_expiry_still_removes_visible_rows_when_cloud_is_down(self):
        self.create()
        with patch.object(temporary.cloud, "load",
                          side_effect=temporary.cloud.CloudUnavailable("offline")):
            with self.assertRaises(temporary.cloud.CloudUnavailable):
                temporary.sync_cloud(now=self.now + 1800)
            self.assertTrue(temporary.expire(now=self.now + 1800))
        self.assertEqual(temporary.live_data(now=self.now + 1800), {})

    def test_slow_temporary_cloud_creation_does_not_block_real_ingestion_route(self):
        entered, release = threading.Event(), threading.Event()

        def waiting_save(checkpoint, revision):
            entered.set()
            if not release.wait(3):
                raise AssertionError("test did not release cloud request")
            self.save_cloud(checkpoint, revision)

        async def scenario():
            with patch.object(temporary.cloud, "save", side_effect=waiting_save), \
                    patch.object(project2.capture, "schedule_capture"), \
                    patch.object(temporary.time, "time", return_value=self.now):
                creation = asyncio.create_task(self.post(
                    {"machine4": MACHINE4_VALUES, "__temporary_for_seconds": 1800},
                    path="/api/v1/project2_temporary_data"))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    real = await asyncio.wait_for(self.post(
                        {"machine4": [25] * 10}, path="/api/v1/project2_data"), timeout=0.5)
                    self.assertEqual(real.status_code, 200, real.text)
                finally:
                    release.set()
                created = await asyncio.wait_for(creation, timeout=1)
                self.assertEqual(created.status_code, 409, created.text)

        asyncio.run(scenario())
        self.assertEqual(project2.latest_project2_data["machine4"], [25] * 10)
        self.assertEqual(temporary.live_entries(now=self.now), {})

    def test_cloud_worker_notifies_recovered_visible_state(self):
        self.create()
        self.reset_process(discard_cache=True)

        async def scenario():
            notified = asyncio.Event()

            async def changed():
                notified.set()

            with patch.object(temporary.time, "time", return_value=self.now + 10):
                worker = asyncio.create_task(temporary.cloud_sync_loop(changed))
                try:
                    await asyncio.wait_for(notified.wait(), timeout=1)
                finally:
                    worker.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await worker

        asyncio.run(scenario())
        self.assertEqual(temporary.live_data(now=self.now + 10), {"machine4": MACHINE4_VALUES})


class TemporaryCloudTransportTests(unittest.TestCase):
    def setUp(self):
        fake = types.SimpleNamespace(service_account_key={"project_id": "test-project"})
        patched = patch.dict(sys.modules, {"constant": fake})
        patched.start()
        self.addCleanup(patched.stop)

    def response(self, status, data):
        return httpx.Response(status, json=data, request=httpx.Request("GET", "https://example.invalid/checkpoint"))

    def test_transport_uses_dedicated_collection_and_bounded_requests(self):
        record = {"version": 1, "entries": {}, "retired": {}}
        with patch.object(temporary_cloud.history, "get_access_token", return_value="test-token"), \
                patch.object(temporary_cloud.httpx, "get",
                             return_value=self.response(200, {"fields": history.firestore_fields(record),
                                                              "updateTime": "server-version"})) as get:
            loaded, revision = temporary_cloud.load()
        self.assertEqual(loaded, record)
        self.assertEqual(revision, "server-version")
        self.assertIn("/temporary_machine_state/project10", get.call_args.args[0])
        self.assertNotIn("/session_low_history/", get.call_args.args[0])
        self.assertEqual(get.call_args.kwargs["timeout"], 3)

    def test_transport_conditional_writes_existing_and_new_checkpoint(self):
        record = {"version": 1, "entries": {}, "retired": {}}
        for revision, expected in ((None, {"currentDocument.exists": "false"}),
                                   ("server-version", {"currentDocument.updateTime": "server-version"})):
            with self.subTest(revision=revision), \
                    patch.object(temporary_cloud.history, "get_access_token", return_value="test-token"), \
                    patch.object(temporary_cloud.httpx, "patch",
                                 return_value=self.response(200, {})) as write:
                temporary_cloud.save(record, revision)
                self.assertEqual(write.call_args.kwargs["params"], expected)
                self.assertEqual(write.call_args.kwargs["timeout"], 3)

    def test_transport_errors_are_sanitized_and_version_conflict_is_distinct(self):
        with patch.object(temporary_cloud.history, "get_access_token", return_value="secret-token"):
            with patch.object(temporary_cloud.httpx, "get",
                              side_effect=RuntimeError("secret-token")):
                with self.assertRaises(temporary_cloud.CloudUnavailable) as caught:
                    temporary_cloud.load()
                self.assertNotIn("secret-token", str(caught.exception))
            with patch.object(temporary_cloud.httpx, "patch",
                              return_value=self.response(412, {"error": "private detail"})):
                with self.assertRaises(temporary_cloud.CloudConflict):
                    temporary_cloud.save({"version": 1, "entries": {}, "retired": {}}, "old")
