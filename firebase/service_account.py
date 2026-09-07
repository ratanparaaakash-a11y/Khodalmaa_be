import json
import os
from pathlib import Path


def load_service_account():
    raw_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
    credential_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if raw_json or credential_file:
        try:
            data = json.loads(raw_json) if raw_json else json.loads(
                Path(credential_file).read_text(encoding="utf-8-sig")
            )
        except (ValueError, OSError):
            raise RuntimeError("Firebase service account JSON could not be loaded") from None
        if not isinstance(data, dict) or any(
            not data.get(field) for field in ("project_id", "client_email", "private_key", "token_uri")
        ):
            raise RuntimeError("Firebase service account JSON is missing required fields")
        return data

    fields = {
        "type": "FIREBASE_ACCOUNT_TYPE",
        "project_id": "FIREBASE_PROJECT_ID",
        "private_key_id": "FIREBASE_PRIVATE_KEY_ID",
        "private_key": "FIREBASE_PRIVATE_KEY",
        "client_email": "FIREBASE_CLIENT_EMAIL",
        "client_id": "FIREBASE_CLIENT_ID",
        "auth_uri": "FIREBASE_AUTH_URI",
        "token_uri": "FIREBASE_TOKEN_URI",
        "auth_provider_x509_cert_url": "FIREBASE_AUTH_PROVIDER",
        "client_x509_cert_url": "FIREBASE_CLIENT_CERT",
        "universe_domain": "FIREBASE_UNIVERSAL_DOMAIN",
    }
    data = {field: os.getenv(variable) for field, variable in fields.items()}
    if not data["private_key"]:
        raise RuntimeError("Firebase service account credentials are not configured")
    data["private_key"] = data["private_key"].replace("\\n", "\n")
    return data
