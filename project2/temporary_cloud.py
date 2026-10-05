"""Private Firestore checkpoint for temporary rows; never a History document."""

from urllib.parse import quote
import httpx
from history import session_history as history

COLLECTION = "temporary_machine_state"
DOCUMENT = "project10"


class CloudUnavailable(RuntimeError):
    pass


class CloudConflict(RuntimeError):
    pass


def _url():
    from constant import service_account_key
    project = quote(service_account_key["project_id"], safe="")
    return (f"https://firestore.googleapis.com/v1/projects/{project}"
            f"/databases/(default)/documents/{COLLECTION}/{DOCUMENT}")


def _headers():
    return {"Authorization": f"Bearer {history.get_access_token()}"}


def load():
    """Return (checkpoint, updateTime); None denotes an absent cloud document."""
    try:
        response = httpx.get(_url(), headers=_headers(), timeout=3)
        if response.status_code == 404:
            return None, None
        response.raise_for_status()
        document = response.json()
        revision = document.get("updateTime")
        if not isinstance(revision, str) or not revision:
            raise ValueError("Missing checkpoint version")
        return history.plain_fields(document.get("fields", {})), revision
    except Exception:
        raise CloudUnavailable("Temporary machine cloud storage is unavailable") from None


def save(checkpoint, revision):
    """Replace only the version read, preventing a stale writer from overwriting."""
    try:
        params = ({"currentDocument.updateTime": revision} if revision else
                  {"currentDocument.exists": "false"})
        response = httpx.patch(_url(), headers=_headers(), params=params,
                               json={"fields": history.firestore_fields(checkpoint)}, timeout=3)
        if response.status_code in (409, 412):
            raise CloudConflict("Temporary machine checkpoint changed")
        response.raise_for_status()
    except CloudConflict:
        raise
    except Exception:
        # Never expose credentials, request details, or an upstream response body.
        raise CloudUnavailable("Temporary machine cloud storage is unavailable") from None

