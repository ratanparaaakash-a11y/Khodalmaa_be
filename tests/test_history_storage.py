import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from history import session_history as history
from history.history import history_health


class HistoryStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        replacements = {
            "_file_store_dir": Path(self.temp.name),
            "_memory_docs": {},
            "_pending_cloud_docs": {},
            "_cloud_reconciled": False,
            "_last_firestore_error": None,
            "_last_firestore_success_at": None,
            "_firestore_disabled_until": 0,
            "USE_FIRESTORE_ADMIN": False,
        }
        for name, value in replacements.items():
            mock = patch.object(history, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        self.cloud = {}
        mock = patch.object(history, "load_firestore_docs", self.read_cloud)
        mock.start()
        self.addCleanup(mock.stop)
        mock = patch.object(history, "write_cloud_doc", self.write_cloud)
        mock.start()
        self.addCleanup(mock.stop)

    def read_cloud(self):
        history.mark_firestore_success()
        return copy.deepcopy(list(self.cloud.values()))

    def write_cloud(self, doc_id, doc):
        self.cloud[doc_id] = copy.deepcopy(doc)
        history.mark_firestore_success()
        return True

    def doc(self, number="1", updated_at="2026-09-07T10:00:00+05:30"):
        return {
            "doc_id": "2026-09-07-s1-project10",
            "project": "project10",
            "business_date": "2026-09-07",
            "session": 1,
            "saved_at": updated_at,
            "updated_at": updated_at,
            "entries": [{"number": number, "rank": 1}],
        }

    def test_failed_write_survives_restart_and_recovers_without_new_session(self):
        doc = self.doc()
        with patch.object(history, "write_cloud_doc", return_value=False):
            saved = history.save_raw_doc(doc["doc_id"], doc)
        self.assertEqual(history.load_file_doc(doc["doc_id"]), saved)
        self.assertEqual(len(history._pending_cloud_docs), 1)
        history._memory_docs.clear()
        history._pending_cloud_docs.clear()
        history.sync_history_storage()
        self.assertEqual(self.cloud[doc["doc_id"]], saved)
        self.assertEqual(history._pending_cloud_docs, {})
        self.assertTrue(history._cloud_reconciled)

    def test_average_uses_latest_local_version_while_cloud_is_stale(self):
        old = self.doc("1")
        latest = self.doc("2", "2026-09-07T11:00:00+05:30")
        self.cloud[old["doc_id"]] = old
        history.save_file_doc(latest["doc_id"], latest)
        docs = history.load_all_docs("project10")
        self.assertEqual(docs[0]["entries"], latest["entries"])
        before = history.analyze_average_history("project10", 7)
        history.sync_history_storage()
        self.assertEqual(self.cloud[latest["doc_id"]], latest)
        self.assertEqual(before, history.analyze_average_history("project10", 7))

    def test_newer_cloud_record_is_preserved_and_restored_to_local_disk(self):
        old = self.doc()
        latest = self.doc("2", "2026-09-07T11:00:00+05:30")
        history.save_file_doc(old["doc_id"], old)
        self.cloud[latest["doc_id"]] = latest
        history.sync_history_storage()
        self.assertEqual(history.load_file_doc(old["doc_id"]), latest)
        self.assertEqual(self.cloud[old["doc_id"]], latest)
        self.assertEqual(history._pending_cloud_docs, {})

    def test_repeated_sync_does_not_write_unchanged_documents(self):
        doc = self.doc()
        history.save_file_doc(doc["doc_id"], doc)
        self.cloud[doc["doc_id"]] = doc
        with patch.object(history, "write_cloud_doc", wraps=self.write_cloud) as writer:
            history.sync_history_storage()
            history.sync_history_storage()
            writer.assert_not_called()

    def test_retry_is_bounded_and_keeps_failed_records(self):
        for index in range(4):
            doc = {**self.doc(), "doc_id": "sent-{}".format(index), "project": "project220_sent_low"}
            history.save_file_doc(doc["doc_id"], doc)
        history.sync_history_storage(batch_size=2)
        self.assertEqual(len(self.cloud), 2)
        self.assertEqual(len(history._pending_cloud_docs), 2)
        with patch.object(history, "write_cloud_doc", return_value=False):
            history.sync_history_storage()
        self.assertEqual(len(history._pending_cloud_docs), 2)
        history.sync_history_storage()
        self.assertEqual(len(self.cloud), 4)

    def test_no_reconciliation_on_cloud_read_failure(self):
        doc = self.doc()
        history.save_file_doc(doc["doc_id"], doc)

        def fail():
            history.disable_firestore_temporarily(RuntimeError("unavailable"), "list")
            return []

        with patch.object(history, "load_firestore_docs", side_effect=fail):
            history.sync_history_storage()
        self.assertFalse(history._cloud_reconciled)
        self.assertEqual(history.load_file_doc(doc["doc_id"]), doc)
        self.assertEqual(self.cloud, {})

    def test_health_reports_cloud_error_and_clears_after_success(self):
        history.disable_firestore_temporarily(RuntimeError("invalid credential"))
        self.assertEqual(asyncio.run(history_health())["status"], "degraded")
        history.mark_firestore_success()
        self.assertEqual(asyncio.run(history_health())["status"], "ok")
        self.assertIsNone(history.get_storage_status()["firestore_last_error"])

    def test_document_versions_compare_timezones(self):
        self.assertGreater(
            history.document_version(self.doc(updated_at="2026-09-07T05:00:00Z")),
            history.document_version(self.doc(updated_at="2026-09-07T10:00:00+05:30")),
        )


class FirestoreReadTests(unittest.TestCase):
    def test_more_than_five_pages_are_loaded(self):
        responses = [httpx.Response(200, json={"documents": [], "nextPageToken": str(i)},
                                    request=httpx.Request("GET", "https://example.test")) for i in range(6)]
        responses.append(httpx.Response(200, json={"documents": []},
                                        request=httpx.Request("GET", "https://example.test")))
        with patch.object(history, "firestore_is_disabled", return_value=False), \
             patch.object(history, "firestore_admin_is_disabled", return_value=True), \
             patch.object(history, "firestore_collection_url", return_value="https://example.test"), \
             patch.object(history, "get_access_token", return_value="test-token"), \
             patch.object(history.httpx, "get", side_effect=responses) as get, \
             patch.object(history, "mark_firestore_success"):
            history.load_firestore_docs()
        self.assertEqual(get.call_count, 7)


if __name__ == "__main__":
    unittest.main()
