import asyncio
import copy
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException
import httpx

from history.rules import (
    ALL_COLUMNS,
    HARDCODED_NUM1,
    PROJECT10_LOW_COUNT,
    PROJECT220_FIRST_HALF_LOW_COUNT,
    PROJECT220_FIRST_HALF_RANGE,
    PROJECT220_SECOND_HALF_LOW_COUNT,
    PROJECT220_SECOND_HALF_RANGE,
    display_column,
    display_project220_number,
)


COLLECTION_NAME = "session_low_history"
PROJECT220_SENT_LOW_PROJECT = "project220_sent_low"
BUSINESS_DAY_RESET_HOUR = 4
AUTO_FINALIZE_SECONDS = 90
MAX_SESSION_PER_DAY = 2
INDIA_TZ = timezone(timedelta(hours=5, minutes=30))
USE_FIRESTORE_ADMIN = os.getenv("HISTORY_FIRESTORE_ADMIN") == "1"

_finalize_tasks = {}
_session_number_cache = {}
_memory_docs = {}
_cached_token = None
_cached_token_until = 0
_firestore_client = None
_firestore_admin_disabled_until = 0
_firestore_disabled_until = 0
_last_firestore_error = None
_last_firestore_success_at = None
_pending_cloud_docs = {}
_cloud_reconciled = False
_storage_lock = threading.RLock()
_file_store_dir = Path(os.getenv("HISTORY_STORE_DIR") or Path(tempfile.gettempdir()) / "khodalmaa_history")


def get_business_date(now=None):
    current = now or datetime.now(INDIA_TZ)
    if current.hour < BUSINESS_DAY_RESET_HOUR:
        current = current - timedelta(days=1)
    return current.date().isoformat()


def get_now_iso():
    return datetime.now(INDIA_TZ).isoformat(timespec="seconds")


def get_doc_id(project, business_date, session):
    return f"{business_date}-s{session}-{project}"


def get_file_path(doc_id):
    return _file_store_dir / f"{doc_id}.json"


def load_file_doc(doc_id):
    try:
        path = get_file_path(doc_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except Exception as error:
        print(f"History file read fallback failed for {doc_id}: {error}")
    return None


def save_file_doc(doc_id, data):
    try:
        _file_store_dir.mkdir(parents=True, exist_ok=True)
        path = get_file_path(doc_id)
        temp_path = path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(data, file, separators=(",", ":"))
        temp_path.replace(path)
    except Exception as error:
        print(f"History file write fallback failed for {doc_id}: {error}")


def firestore_value(value):
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, list):
        return {"arrayValue": {"values": [firestore_value(item) for item in value]}}
    if isinstance(value, dict):
        return {"mapValue": {"fields": {str(key): firestore_value(item) for key, item in value.items()}}}
    return {"stringValue": str(value)}


def firestore_fields(data):
    return {str(key): firestore_value(value) for key, value in data.items()}


def plain_value(value):
    if "nullValue" in value:
        return None
    if "booleanValue" in value:
        return value["booleanValue"]
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "stringValue" in value:
        return value["stringValue"]
    if "arrayValue" in value:
        return [plain_value(item) for item in value.get("arrayValue", {}).get("values", [])]
    if "mapValue" in value:
        return {key: plain_value(item) for key, item in value.get("mapValue", {}).get("fields", {}).items()}
    return None


def plain_fields(fields):
    return {key: plain_value(value) for key, value in (fields or {}).items()}


def get_access_token():
    global _cached_token, _cached_token_until

    now = time.time()
    if _cached_token and now < _cached_token_until:
        return _cached_token

    from constant import service_account_key
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    class TimeoutRequest(Request):
        def __call__(self, url, method="GET", body=None, headers=None, timeout=3, **kwargs):
            return super().__call__(url, method=method, body=body, headers=headers, timeout=3, **kwargs)

    credentials = service_account.Credentials.from_service_account_info(
        service_account_key,
        scopes=["https://www.googleapis.com/auth/datastore"],
    )
    credentials.refresh(TimeoutRequest())
    _cached_token = credentials.token
    _cached_token_until = now + 45 * 60
    return _cached_token


def firestore_url(doc_id):
    from constant import service_account_key

    project_id = service_account_key.get("project_id")
    safe_doc_id = quote(doc_id, safe="")
    return (
        f"https://firestore.googleapis.com/v1/projects/{project_id}"
        f"/databases/(default)/documents/{COLLECTION_NAME}/{safe_doc_id}"
    )


def firestore_collection_url():
    from constant import service_account_key

    project_id = service_account_key.get("project_id")
    return (
        f"https://firestore.googleapis.com/v1/projects/{project_id}"
        f"/databases/(default)/documents/{COLLECTION_NAME}"
    )


def firestore_is_disabled():
    return time.time() < _firestore_disabled_until


def firestore_admin_is_disabled():
    return not USE_FIRESTORE_ADMIN or time.time() < _firestore_admin_disabled_until


def disable_firestore_admin_temporarily(error, context="admin"):
    global _firestore_admin_disabled_until, _last_firestore_error
    _firestore_admin_disabled_until = time.time() + 5 * 60
    _last_firestore_error = f"{context}: {summarize_firestore_error(error)}"
    print(f"History Firestore admin temporarily disabled: {_last_firestore_error}")


def summarize_firestore_error(error):
    if isinstance(error, httpx.HTTPStatusError) and error.response is not None:
        try:
            detail = error.response.json().get("error", {})
            message = detail.get("message") or error.response.text
            status_text = detail.get("status") or error.response.reason_phrase
            return f"{error.response.status_code} {status_text}: {message}"[:500]
        except Exception:
            return f"{error.response.status_code}: {error.response.text}"[:500]
    return f"{type(error).__name__}: {error}"[:500]


def disable_firestore_temporarily(error, context="Firestore"):
    global _firestore_disabled_until, _last_firestore_error
    _firestore_disabled_until = time.time() + 5 * 60
    _last_firestore_error = f"{context}: {summarize_firestore_error(error)}"
    print(f"History Firestore temporarily disabled: {_last_firestore_error}")


def mark_firestore_success():
    global _last_firestore_error, _last_firestore_success_at, _firestore_disabled_until
    _last_firestore_error = None
    _last_firestore_success_at = get_now_iso()
    _firestore_disabled_until = 0


def document_version(doc):
    value = doc.get("updated_at") or doc.get("saved_at")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
    except (TypeError, ValueError):
        return 0


def remember_newest(by_id, doc_id, doc):
    previous = by_id.get(doc_id)
    if previous is None or document_version(doc) > document_version(previous):
        by_id[doc_id] = doc


def memory_docs_snapshot():
    with _storage_lock:
        return list(_memory_docs.values())


def get_firestore_client():
    global _firestore_client
    if _firestore_client is not None:
        return _firestore_client

    from firebase.config import firebase_app
    from firebase_admin import firestore

    _firestore_client = firestore.client(app=firebase_app)
    return _firestore_client


def load_firestore_doc_admin(doc_id):
    doc = get_firestore_client().collection(COLLECTION_NAME).document(doc_id).get(timeout=3)
    if not doc.exists:
        return None
    return doc.to_dict()


def load_firestore_docs_admin():
    docs = []
    for doc in get_firestore_client().collection(COLLECTION_NAME).stream(timeout=4):
        data = doc.to_dict()
        if isinstance(data, dict):
            docs.append(data)
    return docs


def save_firestore_doc_admin(doc_id, data):
    get_firestore_client().collection(COLLECTION_NAME).document(doc_id).set(data, timeout=3)


def parse_arrow_entry(entry):
    if not isinstance(entry, str) or "->" not in entry:
        return None

    raw_number, raw_amount = entry.split("->", 1)
    try:
        return int(raw_number.strip()), float(raw_amount.strip())
    except ValueError:
        return None


def normalize_project(project):
    text = str(project or "").strip().lower()
    if text in {"project10", "p10", "project2"}:
        return "project10"
    if text in {"project220", "p220", "project1"}:
        return "project220"
    raise HTTPException(status_code=400, detail="Invalid project")


def safe_session(session):
    try:
        value = int(session)
    except (TypeError, ValueError):
        value = 1
    return 1 if value <= 1 else 2


def safe_days(days):
    try:
        value = int(days)
    except (TypeError, ValueError):
        value = 7
    return max(1, min(14, value))


def normalize_project10_data(data):
    normalized = {}
    for machine, values in (data or {}).items():
        if isinstance(machine, str) and machine.startswith("__"):
            continue
        if not isinstance(values, list):
            continue
        row = []
        for value in values[:10]:
            try:
                row.append(float(value))
            except (TypeError, ValueError):
                row.append(0.0)
        row.extend([0.0] * max(0, 10 - len(row)))
        normalized[str(machine)] = row[:10]
    return normalized


def normalize_project220_data(data):
    normalized = {}
    for machine, machine_data in (data or {}).items():
        if isinstance(machine, str) and machine.startswith("__"):
            continue
        if not isinstance(machine_data, dict):
            continue
        clean_machine = {}
        for column in ALL_COLUMNS:
            entries = machine_data.get(str(column)) or machine_data.get(column)
            clean_machine[str(column)] = entries if isinstance(entries, list) else []
        normalized[str(machine)] = clean_machine
    return normalized


def build_project10_entries(data):
    normalized = normalize_project10_data(data)
    summed = [0.0] * 10
    for values in normalized.values():
        for index, value in enumerate(values[:10]):
            summed[index] += value

    ranked = sorted(
        [{"number": display_column(index + 1), "sort_value": value} for index, value in enumerate(summed)],
        key=lambda item: item["sort_value"],
    )[:PROJECT10_LOW_COUNT]

    return [
        {
            "number": item["number"],
            "rank": index + 1,
            "key": f"project10:{item['number']}",
        }
        for index, item in enumerate(ranked)
    ]


def build_project220_summed(data):
    normalized = normalize_project220_data(data)
    summed = {}
    for column in ALL_COLUMNS:
        keys = HARDCODED_NUM1[column]
        totals = [0.0] * len(keys)
        key_to_index = {number: index for index, number in enumerate(keys)}

        for machine_data in normalized.values():
            entries = machine_data.get(str(column))
            for entry in entries:
                parsed = parse_arrow_entry(entry)
                if not parsed:
                    continue
                number, amount = parsed
                index = key_to_index.get(number)
                if index is not None:
                    totals[index] += amount

        summed[column] = [
            {"number": keys[index], "amount": amount, "row_index": index}
            for index, amount in enumerate(totals)
        ]

    return summed


def build_project220_half_entries(summed, half, start, end, limit):
    entries = []
    for column in ALL_COLUMNS:
        ranked = sorted(summed[column][start:end], key=lambda item: item["amount"])[:limit]
        for rank, item in enumerate(ranked, start=1):
            number = display_project220_number(item["number"])
            display_half = "First Half" if half == "first" else "Second Half"
            display_col = display_column(column)
            entries.append({
                "number": number,
                "column": display_col,
                "half": display_half,
                "half_key": half,
                "rank": rank,
                "row_index": item["row_index"],
                "key": f"project220:{half}:{display_col}:{number}",
            })
    return entries


def build_project220_entries(data):
    summed = build_project220_summed(data)
    return [
        *build_project220_half_entries(
            summed,
            "first",
            PROJECT220_FIRST_HALF_RANGE[0],
            PROJECT220_FIRST_HALF_RANGE[1],
            PROJECT220_FIRST_HALF_LOW_COUNT,
        ),
        *build_project220_half_entries(
            summed,
            "second",
            PROJECT220_SECOND_HALF_RANGE[0],
            PROJECT220_SECOND_HALF_RANGE[1],
            PROJECT220_SECOND_HALF_LOW_COUNT,
        ),
    ]


def build_snapshot(project, data, business_date, session, session_started_at, source):
    entries = build_project10_entries(data) if project == "project10" else build_project220_entries(data)
    first_count = sum(1 for entry in entries if entry.get("half_key") == "first")
    second_count = sum(1 for entry in entries if entry.get("half_key") == "second")
    return {
        "project": project,
        "business_date": business_date,
        "session": session,
        "session_key": f"S{session}",
        "session_started_at": float(session_started_at or time.time()),
        "source": source,
        "saved_at": get_now_iso(),
        "entry_count": len(entries),
        "first_half_count": first_count,
        "second_half_count": second_count,
        "entries": entries,
    }


def load_doc(project, business_date, session):
    doc_id = get_doc_id(project, business_date, session)
    local_doc = _memory_docs.get(doc_id) or load_file_doc(doc_id)
    if local_doc:
        return local_doc

    if firestore_is_disabled():
        return None

    if not firestore_admin_is_disabled():
        try:
            doc = load_firestore_doc_admin(doc_id)
            mark_firestore_success()
            return doc
        except Exception as error:
            print(f"History Firestore admin read fallback for {doc_id}: {error}")
            disable_firestore_admin_temporarily(error, "admin read")

    try:
        response = httpx.get(
            firestore_url(doc_id),
            headers={"Authorization": f"Bearer {get_access_token()}"},
            timeout=3,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        mark_firestore_success()
        return plain_fields(response.json().get("fields", {}))
    except Exception as error:
        print(f"History Firestore read fallback for {doc_id}: {error}")
        disable_firestore_temporarily(error, "read")
    return None


def load_file_docs():
    docs = []
    try:
        if not _file_store_dir.exists():
            return docs
        for path in _file_store_dir.glob("*.json"):
            try:
                with path.open("r", encoding="utf-8") as file:
                    docs.append(json.load(file))
            except Exception as error:
                print(f"History file list read fallback failed for {path.name}: {error}")
    except Exception as error:
        print(f"History file list fallback failed: {error}")
    return docs


def load_firestore_docs():
    if firestore_is_disabled():
        return []

    if not firestore_admin_is_disabled():
        try:
            docs = load_firestore_docs_admin()
            mark_firestore_success()
            return docs
        except Exception as error:
            print(f"History Firestore admin list fallback: {error}")
            disable_firestore_admin_temporarily(error, "admin list")

    docs = []
    page_token = None
    try:
        while True:
            params = {"pageSize": 300}
            if page_token:
                params["pageToken"] = page_token
            response = httpx.get(
                firestore_collection_url(),
                headers={"Authorization": f"Bearer {get_access_token()}"},
                params=params,
                timeout=4,
            )
            response.raise_for_status()
            payload = response.json()
            docs.extend(plain_fields(doc.get("fields", {})) for doc in payload.get("documents", []))
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        mark_firestore_success()
    except Exception as error:
        print(f"History Firestore list fallback: {error}")
        disable_firestore_temporarily(error, "list")
    return docs


def load_all_docs(project):
    normalized_project = normalize_project(project)
    by_id = {}

    for doc in [*memory_docs_snapshot(), *load_file_docs(), *load_firestore_docs()]:
        if not isinstance(doc, dict):
            continue
        if doc.get("project") != normalized_project:
            continue
        if not isinstance(doc.get("entries"), list) or not doc.get("entries"):
            continue

        session = safe_session(doc.get("session"))
        business_date = str(doc.get("business_date") or "")
        doc_id = doc.get("doc_id") or get_doc_id(normalized_project, business_date, session)
        remember_newest(by_id, doc_id, {**doc, "doc_id": doc_id, "session": session})

    return sorted(
        by_id.values(),
        key=lambda doc: (str(doc.get("business_date") or ""), int(doc.get("session") or 0)),
    )


def save_doc(snapshot):
    doc_id = get_doc_id(snapshot["project"], snapshot["business_date"], snapshot["session"])
    stored = {**snapshot, "doc_id": doc_id, "updated_at": get_now_iso()}
    return save_raw_doc(doc_id, stored)


def save_raw_doc(doc_id, data):
    stored = {**data, "doc_id": doc_id, "updated_at": get_now_iso()}
    with _storage_lock:
        _memory_docs[doc_id] = stored
        save_file_doc(doc_id, stored)
        # The snapshot file is the durable retry source after a process restart.
        _pending_cloud_docs[doc_id] = stored
        if write_cloud_doc(doc_id, stored):
            _pending_cloud_docs.pop(doc_id, None)
    return stored


def write_cloud_doc(doc_id, stored):

    if firestore_is_disabled():
        return False

    if not firestore_admin_is_disabled():
        try:
            save_firestore_doc_admin(doc_id, stored)
            mark_firestore_success()
            return True
        except Exception as error:
            print(f"History Firestore admin write fallback for {doc_id}: {error}")
            disable_firestore_admin_temporarily(error, "admin write")

    try:
        response = httpx.patch(
            firestore_url(doc_id),
            headers={"Authorization": f"Bearer {get_access_token()}"},
            json={"fields": firestore_fields(stored)},
            timeout=3,
        )
        response.raise_for_status()
        mark_firestore_success()
        return True
    except Exception as error:
        print(f"History Firestore write fallback for {doc_id}: {error}")
        disable_firestore_temporarily(error, "write")
    return False


def sync_history_storage(batch_size=25):
    global _cloud_reconciled
    if firestore_is_disabled():
        return

    if not _cloud_reconciled:
        remote_docs = load_firestore_docs()
        if firestore_is_disabled() or _last_firestore_error:
            return
        with _storage_lock:
            local = {}
            for doc in [*memory_docs_snapshot(), *load_file_docs()]:
                if isinstance(doc, dict) and doc.get("doc_id"):
                    remember_newest(local, doc["doc_id"], doc)
            remote = {doc["doc_id"]: doc for doc in remote_docs if doc.get("doc_id")}
            combined = dict(local)
            for doc_id, doc in remote.items():
                remember_newest(combined, doc_id, doc)
            for doc_id, doc in combined.items():
                if local.get(doc_id) != doc:
                    save_file_doc(doc_id, doc)
                _memory_docs[doc_id] = doc
                if remote.get(doc_id) != doc:
                    _pending_cloud_docs[doc_id] = doc
                else:
                    _pending_cloud_docs.pop(doc_id, None)
            _cloud_reconciled = True

    with _storage_lock:
        pending_ids = list(_pending_cloud_docs)[:batch_size]
    for doc_id in pending_ids:
        with _storage_lock:
            doc = _pending_cloud_docs.get(doc_id)
            if doc is None:
                continue
            if not write_cloud_doc(doc_id, doc):
                break
            _pending_cloud_docs.pop(doc_id, None)


async def history_storage_sync_loop():
    while True:
        try:
            await asyncio.to_thread(sync_history_storage)
        except Exception as error:
            disable_firestore_temporarily(error, "recovery")
        await asyncio.sleep(30)


def load_docs_by_project_name(project_name):
    by_id = {}
    for doc in [*memory_docs_snapshot(), *load_file_docs(), *load_firestore_docs()]:
        if not isinstance(doc, dict):
            continue
        if doc.get("project") != project_name:
            continue
        doc_id = doc.get("doc_id")
        if not doc_id:
            continue
        remember_newest(by_id, doc_id, doc)
    return list(by_id.values())


def get_storage_status():
    try:
        file_docs = len(list(_file_store_dir.glob("*.json"))) if _file_store_dir.exists() else 0
    except Exception:
        file_docs = 0

    return {
        "cloud_state": "degraded" if _last_firestore_error else "connected" if _last_firestore_success_at else "unverified",
        "cloud_last_success_at": _last_firestore_success_at,
        "cloud_reconciled": _cloud_reconciled,
        "pending_cloud_docs": len(_pending_cloud_docs),
        "memory_docs": len(_memory_docs),
        "file_docs": file_docs,
        "firestore_admin_enabled": USE_FIRESTORE_ADMIN,
        "firestore_disabled_seconds": max(0, int(_firestore_disabled_until - time.time())),
        "firestore_admin_disabled_seconds": max(0, int(_firestore_admin_disabled_until - time.time())),
        "firestore_last_error": _last_firestore_error,
    }


def get_existing_sessions(project, business_date):
    sessions = []
    for session in range(1, MAX_SESSION_PER_DAY + 1):
        if load_doc(project, business_date, session):
            sessions.append(session)
    return sessions


def get_session_number(project, business_date, session_started_at, session_override=None):
    if session_override:
        return safe_session(session_override)

    cache_key = f"{project}:{business_date}:{session_started_at or 0}"
    if cache_key in _session_number_cache:
        return _session_number_cache[cache_key]

    existing_sessions = get_existing_sessions(project, business_date)
    for session in existing_sessions:
        doc = load_doc(project, business_date, session)
        if doc and float(doc.get("session_started_at") or 0) == float(session_started_at or 0):
            _session_number_cache[cache_key] = session
            return session

    session = min(MAX_SESSION_PER_DAY, (max(existing_sessions) + 1) if existing_sessions else 1)
    _session_number_cache[cache_key] = session
    return session


def save_session_snapshot(project, data, session_started_at=None, source="auto", session_override=None):
    normalized_project = normalize_project(project)
    if not data:
        return {"project": normalized_project, "saved": False, "reason": "No live data available"}

    business_date = get_business_date()
    session = get_session_number(normalized_project, business_date, session_started_at, session_override)
    snapshot = build_snapshot(normalized_project, copy.deepcopy(data), business_date, session, session_started_at, source)
    saved = save_doc(snapshot)
    print(f"Saved {normalized_project} history {business_date} S{session}: {saved['entry_count']} entries")
    return {
        "project": normalized_project,
        "saved": True,
        "business_date": business_date,
        "session": session,
        "entry_count": saved["entry_count"],
        "doc_id": saved["doc_id"],
    }


def save_built_session_snapshot(project, business_date, session, entries, saved_at=None, source="restore"):
    normalized_project = normalize_project(project)
    safe_entries = [copy.deepcopy(entry) for entry in entries if isinstance(entry, dict)]
    session_number = safe_session(session)
    first_count = sum(1 for entry in safe_entries if entry.get("half_key") == "first")
    second_count = sum(1 for entry in safe_entries if entry.get("half_key") == "second")
    snapshot = {
        "project": normalized_project,
        "business_date": str(business_date or get_business_date()),
        "session": session_number,
        "session_key": f"S{session_number}",
        "session_started_at": 0,
        "source": source,
        "saved_at": saved_at or get_now_iso(),
        "entry_count": len(safe_entries),
        "first_half_count": first_count,
        "second_half_count": second_count,
        "entries": safe_entries,
    }
    saved = save_doc(snapshot)
    return {
        "project": normalized_project,
        "saved": True,
        "business_date": saved["business_date"],
        "session": saved["session"],
        "entry_count": saved["entry_count"],
        "doc_id": saved["doc_id"],
    }


def normalize_sent_low_entry(entry):
    if not isinstance(entry, dict):
        return None

    parsed = parse_arrow_entry(entry.get("entry"))
    try:
        number = int(str(entry.get("number") or (parsed[0] if parsed else "")).strip())
    except (TypeError, ValueError):
        return None

    try:
        amount = float(entry.get("amount") if entry.get("amount") is not None else parsed[1])
    except (TypeError, ValueError, IndexError):
        return None

    raw_column = entry.get("column")
    try:
        column = 10 if str(raw_column) == "0" else int(raw_column)
    except (TypeError, ValueError):
        return None

    half_key = "second" if str(entry.get("half") or "").lower().startswith("second") else "first"
    try:
        rank = int(entry.get("rank") or 0)
    except (TypeError, ValueError):
        rank = 0

    display_number = display_project220_number(number)
    display_col = display_column(column)
    return {
        "number": display_number,
        "column": display_col,
        "half_key": half_key,
        "half": "Second Half" if half_key == "second" else "First Half",
        "rank": rank,
        "amount": amount,
        "entry": f"{display_number}->{amount:g}",
        "key": f"project220-sent:{half_key}:{display_col}:{display_number}",
    }


def save_project220_sent_low_snapshot(button, entries, session_started_at=None):
    safe_entries = [
        item for item in (normalize_sent_low_entry(entry) for entry in (entries or []))
        if item is not None
    ]
    if not safe_entries:
        return {"saved": False, "reason": "No sent low entries"}

    business_date = get_business_date()
    session = get_session_number("project220", business_date, session_started_at, None)
    now_epoch_ms = int(time.time() * 1000)
    doc_id = f"{business_date}-s{session}-project220-sent-low-{now_epoch_ms}"
    button_key = "second" if str(button or "").lower().startswith("second") else "first"
    snapshot = {
        "project": PROJECT220_SENT_LOW_PROJECT,
        "business_date": business_date,
        "session": session,
        "session_key": f"S{session}",
        "session_started_at": float(session_started_at or time.time()),
        "button": button_key,
        "button_label": "3 Low Second" if button_key == "second" else "4 Low First",
        "sent_at": get_now_iso(),
        "sent_at_epoch_ms": now_epoch_ms,
        "entry_count": len(safe_entries),
        "entries": safe_entries,
    }
    saved = save_raw_doc(doc_id, snapshot)
    print(f"Saved project220 sent-low snapshot {doc_id}: {len(safe_entries)} entries")
    return {
        "saved": True,
        "doc_id": saved["doc_id"],
        "business_date": saved["business_date"],
        "session": saved["session"],
        "button": saved["button"],
        "entry_count": saved["entry_count"],
    }


def load_project220_sent_low_snapshots(limit=20, business_date=None, session=None):
    docs = []
    for doc in load_docs_by_project_name(PROJECT220_SENT_LOW_PROJECT):
        if business_date and doc.get("business_date") != business_date:
            continue
        if session and safe_session(doc.get("session")) != safe_session(session):
            continue
        docs.append(doc)

    return sorted(
        docs,
        key=lambda doc: int(doc.get("sent_at_epoch_ms") or 0),
        reverse=True,
    )[:max(1, min(100, int(limit or 20)))]


def find_project220_current_amount(summed, entry):
    column = 10 if str(entry.get("column")) == "0" else int(entry.get("column") or 0)
    target_number = 0 if str(entry.get("number")) == "000" else int(entry.get("number") or 0)
    for item in summed.get(column, []):
        if int(item.get("number") or -1) == target_number:
            return float(item.get("amount") or 0)
    return 0.0


def analyze_project220_sent_low_report(current_data, business_date=None, session=None):
    snapshots = load_project220_sent_low_snapshots(
        limit=1,
        business_date=business_date,
        session=session,
    )
    if not snapshots:
        return {"status": "empty", "reason": "No sent low snapshot saved yet"}

    latest = snapshots[0]
    if not current_data:
        return {
            "status": "no_current_data",
            "reason": "No current project220 live data available",
            "latest_snapshot": {
                "doc_id": latest.get("doc_id"),
                "button_label": latest.get("button_label"),
                "sent_at": latest.get("sent_at"),
                "entry_count": latest.get("entry_count"),
            },
        }

    summed = build_project220_summed(current_data)
    report_entries = []
    for entry in latest.get("entries", []):
        sent_amount = float(entry.get("amount") or 0)
        current_amount = find_project220_current_amount(summed, entry)
        report_entries.append({
            **entry,
            "sent_amount": sent_amount,
            "current_amount": current_amount,
            "increase": current_amount - sent_amount,
        })

    sent_total = sum(entry["sent_amount"] for entry in report_entries)
    current_total = sum(entry["current_amount"] for entry in report_entries)
    increase_total = current_total - sent_total

    return {
        "status": "success",
        "latest_snapshot": {
            "doc_id": latest.get("doc_id"),
            "business_date": latest.get("business_date"),
            "session": latest.get("session"),
            "button": latest.get("button"),
            "button_label": latest.get("button_label"),
            "sent_at": latest.get("sent_at"),
            "entry_count": latest.get("entry_count"),
        },
        "totals": {
            "sent_amount": sent_total,
            "current_amount": current_total,
            "increase": increase_total,
            "increased_count": sum(1 for entry in report_entries if entry["increase"] > 0),
            "same_or_down_count": sum(1 for entry in report_entries if entry["increase"] <= 0),
        },
        "entries": report_entries,
    }


async def delayed_finalize(project, data_ref, session_started_at, token):
    try:
        await asyncio.sleep(AUTO_FINALIZE_SECONDS)
        current = _finalize_tasks.get(project)
        if not current or current.get("token") != token:
            return
        await asyncio.to_thread(save_session_snapshot, project, copy.deepcopy(data_ref), session_started_at, "auto", None)
    except asyncio.CancelledError:
        return
    except Exception as error:
        print(f"History auto finalize error for {project}: {error}")


def schedule_session_finalize(project, data_ref, session_started_at=None):
    try:
        normalized_project = normalize_project(project)
    except HTTPException:
        return

    current = _finalize_tasks.get(normalized_project)
    if current and current.get("task"):
        current["task"].cancel()

    token = time.time()
    task = asyncio.create_task(delayed_finalize(normalized_project, copy.deepcopy(data_ref), session_started_at, token))
    _finalize_tasks[normalized_project] = {"task": task, "token": token}


def date_range(days):
    today = datetime.fromisoformat(get_business_date()).date()
    return [(today - timedelta(days=offset)).isoformat() for offset in range(days)]


def load_snapshots(project, session, days):
    snapshots = []
    for business_date in date_range(days):
        doc = load_doc(project, business_date, session)
        if doc:
            snapshots.append(doc)
    return snapshots


def entry_key(entry):
    return entry.get("key") or ":".join(str(part) for part in [
        entry.get("half_key"),
        entry.get("column"),
        entry.get("number"),
    ] if part is not None)


def with_history_stats(entries, snapshots):
    snapshot_sets = [set(entry_key(entry) for entry in snapshot.get("entries", [])) for snapshot in snapshots]
    result = []
    for entry in entries:
        key = entry_key(entry)
        running = 0
        for keys in snapshot_sets:
            if key not in keys:
                break
            running += 1
        range_count = sum(1 for keys in snapshot_sets if key in keys)
        result.append({**entry, "days_running": running, "range_count": range_count, "status": "running" if running > 1 else "today"})
    return result


def build_break_today(snapshots):
    if len(snapshots) < 2:
        return []

    current_keys = {entry_key(entry) for entry in snapshots[0].get("entries", [])}
    previous_entries = snapshots[1].get("entries", [])
    previous_sets = [set(entry_key(entry) for entry in snapshot.get("entries", [])) for snapshot in snapshots[1:]]
    breaks = []
    for entry in previous_entries:
        key = entry_key(entry)
        if key in current_keys:
            continue
        previous_running = 0
        for keys in previous_sets:
            if key not in keys:
                break
            previous_running += 1
        breaks.append({**entry, "days_running": previous_running, "range_count": sum(1 for keys in previous_sets if key in keys), "status": "break"})
    return breaks


def combined_entry_candidates(project, docs):
    session_total = len(docs)
    grouped = {}

    for doc in docs:
        session = safe_session(doc.get("session"))
        doc_ref = doc.get("doc_id") or f"{doc.get('business_date')}-s{session}-{project}"
        seen_in_session = set()
        for entry in doc.get("entries", []):
            key = entry_key(entry)
            if key in seen_in_session:
                continue
            seen_in_session.add(key)

            if key not in grouped:
                grouped[key] = {
                    "entry": copy.deepcopy(entry),
                    "hit_count": 0,
                    "sessions": [],
                    "doc_refs": [],
                    "ranks": [],
                }

            grouped[key]["hit_count"] += 1
            grouped[key]["sessions"].append(session)
            grouped[key]["doc_refs"].append(doc_ref)
            try:
                grouped[key]["ranks"].append(float(entry.get("rank") or 999))
            except (TypeError, ValueError):
                grouped[key]["ranks"].append(999.0)

    candidates = []
    for group in grouped.values():
        entry = group["entry"]
        session_hits = group["hit_count"]
        avg_rank = sum(group["ranks"]) / len(group["ranks"]) if group["ranks"] else 999.0
        average_percent = round((session_hits / session_total) * 100) if session_total else 0
        candidates.append({
            **entry,
            "session_hits": session_hits,
            "session_total": session_total,
            "average_score": round(session_hits / session_total, 3) if session_total else 0,
            "average_percent": average_percent,
            "average_label": f"{average_percent}%",
            "avg_rank": round(avg_rank, 2),
            "source_sessions": group["sessions"],
            "source_docs": group["doc_refs"],
        })

    return candidates


def combined_sort_key(entry):
    return (
        -float(entry.get("average_score") or 0),
        float(entry.get("avg_rank") or 999),
        int(entry.get("row_index") or 999),
        str(entry.get("number") or ""),
    )


def select_combined_project10_entries(candidates):
    selected = sorted(candidates, key=combined_sort_key)[:PROJECT10_LOW_COUNT]
    return [{**entry, "rank": index + 1} for index, entry in enumerate(selected)]


def select_combined_project220_entries(candidates):
    selected = []
    for half_key, limit in [
        ("first", PROJECT220_FIRST_HALF_LOW_COUNT),
        ("second", PROJECT220_SECOND_HALF_LOW_COUNT),
    ]:
        for column in [display_column(col) for col in ALL_COLUMNS]:
            column_entries = [
                entry
                for entry in candidates
                if entry.get("half_key") == half_key and str(entry.get("column")) == column
            ]
            ranked = sorted(column_entries, key=combined_sort_key)[:limit]
            selected.extend({**entry, "rank": index + 1} for index, entry in enumerate(ranked))
    return selected


def build_combined_snapshot(project, business_date, docs):
    if not docs:
        return None

    candidates = combined_entry_candidates(project, docs)
    if project == "project10":
        entries = select_combined_project10_entries(candidates)
    else:
        entries = select_combined_project220_entries(candidates)

    first_count = sum(1 for entry in entries if entry.get("half_key") == "first")
    second_count = sum(1 for entry in entries if entry.get("half_key") == "second")
    saved_values = [doc.get("saved_at") for doc in docs if doc.get("saved_at")]
    saved_at = max(saved_values) if saved_values else get_now_iso()

    return {
        "project": project,
        "business_date": business_date,
        "session": "average",
        "session_key": "Average",
        "combined_sessions": len(docs),
        "source_sessions": [safe_session(doc.get("session")) for doc in docs],
        "saved_at": saved_at,
        "entry_count": len(entries),
        "first_half_count": first_count,
        "second_half_count": second_count,
        "entries": entries,
    }


def load_combined_snapshots(project, days):
    snapshots = []
    for business_date in date_range(days):
        docs = []
        for session in range(1, MAX_SESSION_PER_DAY + 1):
            doc = load_doc(project, business_date, session)
            if doc:
                docs.append(doc)
        snapshot = build_combined_snapshot(project, business_date, docs)
        if snapshot:
            snapshots.append(snapshot)
    return snapshots


def build_average_metrics(current_entries, break_today, latest):
    session_total = int(latest.get("combined_sessions") or 0) if latest else 0
    average_percent = (
        sum(float(entry.get("average_percent") or 0) for entry in current_entries) / len(current_entries)
        if current_entries
        else 0
    )
    strong_match = max([int(entry.get("average_percent") or 0) for entry in current_entries], default=0)

    return {
        "total_lows": len(current_entries),
        "running": sum(1 for entry in current_entries if entry.get("days_running", 0) > 1),
        "break_today": len(break_today),
        "combined_sessions": session_total,
        "strong_match": strong_match,
        "strong_match_label": f"{strong_match}%",
        "average_percent": round(average_percent),
        "average_label": f"{round(average_percent)}%",
    }


def group_project220(entries):
    grouped = {
        "first_half": {"total": 0, "columns": []},
        "second_half": {"total": 0, "columns": []},
    }
    for half_key, target in [("first", "first_half"), ("second", "second_half")]:
        half_entries = [entry for entry in entries if entry.get("half_key") == half_key]
        grouped[target]["total"] = len(half_entries)
        for column in [display_column(col) for col in ALL_COLUMNS]:
            grouped[target]["columns"].append({
                "column": column,
                "entries": [entry for entry in half_entries if str(entry.get("column")) == column],
            })
    return grouped


def analyze_history(project, session, days):
    normalized_project = normalize_project(project)
    snapshots = load_snapshots(normalized_project, safe_session(session), safe_days(days))
    latest = snapshots[0] if snapshots else None
    current_entries = with_history_stats(latest.get("entries", []) if latest else [], snapshots)
    break_today = build_break_today(snapshots)

    response = {
        "project": normalized_project,
        "session": safe_session(session),
        "session_key": f"S{safe_session(session)}",
        "days": safe_days(days),
        "snapshots_found": len(snapshots),
        "latest_snapshot": {
            "business_date": latest.get("business_date"),
            "session": latest.get("session"),
            "entry_count": latest.get("entry_count"),
            "saved_at": latest.get("saved_at"),
        } if latest else None,
        "metrics": {
            "total_lows": len(current_entries),
            "running": sum(1 for entry in current_entries if entry.get("days_running", 0) > 1),
            "break_today": len(break_today),
        },
        "entries": current_entries,
        "break_today": break_today,
    }

    if normalized_project == "project220":
        response["project220"] = group_project220(current_entries)
        response["top_running"] = sorted(
            current_entries,
            key=lambda entry: (entry.get("days_running", 0), entry.get("range_count", 0)),
            reverse=True,
        )[:12]
    else:
        response["project10"] = {"entries": current_entries}

    return response


def analyze_average_history(project, days):
    normalized_project = normalize_project(project)
    source_docs = load_all_docs(normalized_project)
    latest = build_combined_snapshot(normalized_project, "all", source_docs)
    current_entries = latest.get("entries", []) if latest else []
    break_today = []
    source_dates = sorted({str(doc.get("business_date")) for doc in source_docs if doc.get("business_date")})

    response = {
        "project": normalized_project,
        "mode": "average",
        "session": "average",
        "session_key": "Average",
        "days": "all",
        "snapshots_found": len(source_docs),
        "latest_snapshot": {
            "business_date": latest.get("business_date"),
            "session": "average",
            "session_key": "Average",
            "entry_count": latest.get("entry_count"),
            "combined_sessions": latest.get("combined_sessions"),
            "source_sessions": latest.get("source_sessions"),
            "source_days": len(source_dates),
            "first_date": source_dates[0] if source_dates else None,
            "last_date": source_dates[-1] if source_dates else None,
            "saved_at": latest.get("saved_at"),
        } if latest else None,
        "metrics": build_average_metrics(current_entries, break_today, latest),
        "entries": current_entries,
        "break_today": break_today,
    }

    if normalized_project == "project220":
        response["project220"] = group_project220(current_entries)
        response["top_running"] = sorted(
            current_entries,
            key=lambda entry: (
                entry.get("average_score", 0),
                -float(entry.get("avg_rank") or 999),
            ),
            reverse=True,
        )[:12]
    else:
        response["project10"] = {"entries": current_entries}

    return response
