"""A restart between sessions must not empty the live tables."""

import unittest
from unittest.mock import patch

from history import capture
from project1 import project1
from project2 import project2
from test_session_capture import CaptureTestCase, p10, p220


class RestartRestoreTests(CaptureTestCase):
    def setUp(self):
        super().setUp()
        for module, prefix in ((project1, "project1"), (project2, "project2")):
            for name, value in ((f"latest_{prefix}_data", {}), (f"{prefix}_session_started_at", None),
                                (f"last_{prefix}_hit_at", None)):
                mocked = patch.object(module, name, value)
                mocked.start()
                self.addCleanup(mocked.stop)

    def test_latest_capture_returns_newest_session(self):
        capture.accept_data("project10", p10(1), self.s1 + 20)
        capture.accept_data("project10", p10(2), self.s2 + 20)
        latest = capture.latest_capture("project10", now=self.s2_end + 3600)
        self.assertEqual(latest["session_started_at"], self.s2)
        self.assertEqual(latest["data"]["machine1"], p10(2)["machine1"])
        self.assertIsNone(capture.latest_capture("project220", now=self.s2_end + 3600))

    def test_corrupt_newest_file_falls_back_to_previous_valid_session(self):
        capture.accept_data("project10", p10(1), self.s1 + 20)
        second = capture.accept_data("project10", p10(2), self.s2 + 20)
        capture.checkpoint_path(second["key"]).write_text("{not json", encoding="utf-8")
        latest = capture.latest_capture("project10", now=self.s2_end + 3600)
        self.assertEqual(latest["session_started_at"], self.s1)
        self.assertEqual(latest["data"]["machine1"], p10(1)["machine1"])

    def test_capture_from_the_future_is_ignored(self):
        capture.accept_data("project10", p10(1), self.s1 + 20)
        capture.accept_data("project10", p10(2), self.s2 + 20)
        latest = capture.latest_capture("project10", now=self.s1_end + 60)
        self.assertEqual(latest["session_started_at"], self.s1)

    def test_restart_outside_window_refills_both_live_screens(self):
        capture.accept_data("project220", p220(), self.s2 + 20)
        capture.accept_data("project10", p10(7), self.s2 + 30)
        project1.restore_project1_capture()
        project2.restore_project2_capture()
        self.assertIn("machine1", project1.latest_project1_data)
        self.assertEqual(project1.project1_session_started_at, self.s2)
        self.assertEqual(project2.latest_project2_data["machine1"], p10(7)["machine1"])
        self.assertEqual(project2.project2_session_started_at, self.s2)
        payload = project2.with_project2_meta(project2.latest_project2_data)
        self.assertTrue(payload["__full_snapshot"])
        self.assertEqual(payload["__session_started_at"], self.s2)

    def test_active_window_capture_is_preferred_over_older_sessions(self):
        capture.accept_data("project10", p10(1), self.s1 + 20)
        capture.accept_data("project10", p10(2), self.s2 + 20)
        with patch.object(capture, "active_capture", return_value=capture.read_checkpoint(
                capture.capture_key("project10", capture.scheduled_session(self.s1 + 20)))):
            project2.restore_project2_capture()
        self.assertEqual(project2.latest_project2_data["machine1"], p10(1)["machine1"])
        self.assertEqual(project2.project2_session_started_at, self.s1)

    def test_restore_does_not_create_or_change_any_saved_file(self):
        accepted = capture.accept_data("project10", p10(3), self.s2 + 20)
        path = capture.checkpoint_path(accepted["key"])
        before = path.read_bytes()
        files_before = sorted(p.name for p in path.parent.iterdir())
        project2.restore_project2_capture()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), files_before)


if __name__ == "__main__":
    unittest.main()
