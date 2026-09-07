import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from firebase.service_account import load_service_account


class ServiceAccountTests(unittest.TestCase):
    def account(self):
        return {"project_id": "test-project", "client_email": "test@example.test",
                "private_key": "test-only-key\n", "private_key_id": "new-key-id",
                "token_uri": "https://oauth2.googleapis.com/token"}

    def test_complete_json_takes_precedence_over_old_split_values(self):
        account = self.account()
        with patch.dict(os.environ, {"FIREBASE_SERVICE_ACCOUNT_JSON": json.dumps(account),
                                     "FIREBASE_PRIVATE_KEY": "old", "FIREBASE_PRIVATE_KEY_ID": "old"}, clear=True):
            self.assertEqual(load_service_account(), account)

    def test_secret_file_is_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service-account.json"
            path.write_text(json.dumps(self.account()), encoding="utf-8")
            with patch.dict(os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": str(path)}, clear=True):
                self.assertEqual(load_service_account(), self.account())

    def test_legacy_escaped_key_still_works(self):
        with patch.dict(os.environ, {"FIREBASE_PRIVATE_KEY": "first\\nsecond"}, clear=True):
            self.assertEqual(load_service_account()["private_key"], "first\nsecond")

    def test_bad_json_does_not_fall_back_or_print_secret(self):
        with patch.dict(os.environ, {"FIREBASE_SERVICE_ACCOUNT_JSON": "secret-invalid-json",
                                     "FIREBASE_PRIVATE_KEY": "old-key"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "JSON could not be loaded") as error:
                load_service_account()
        self.assertNotIn("secret-invalid-json", str(error.exception))


if __name__ == "__main__":
    unittest.main()
