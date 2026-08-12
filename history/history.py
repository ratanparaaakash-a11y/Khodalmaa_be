import asyncio
import copy
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from firebase_admin import firestore

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


HistoryRouter = APIRouter(prefix="/api/v1")

COLLECTION_NAME = "session_low_history"
BUSINESS_DAY_RESET_HOUR = 4
AUTO_FINALIZE_SECONDS = 90
MAX_SESSION_PER_DAY = 2
INDIA_TZ = ZoneInfo("Asia/Kolkata")

_finalize_tasks = {}
_session_number_cache = {}
_memory_docs = {}


def get_business_date(now=None):
    current = now or datetime.now(INDIA_TZ)
    if current.hour < BUSINESS_DAY_RESET_HOUR:
        current = current - timedelta(days=1)
    return current.date().isoformat()


def get_now_iso():
    return datetime.now(INDIA_TZ).isoformat(timespec="seconds")


def get_doc_id(project, business_date, session):
    return f"{business_date}-s{session}-{project}"


def get_firestore_collection():
    return firestore.client().collection(COLLECTION_NAME)


def parse_arrow_entry(entry):
    if not isinstance(entry, str) or "->" not in entry:
        return None

    raw_number, raw_amount = entry.split("->", 1)
    try:
        return int(raw_number.strip()), float(raw_amount.strip())
    except ValueError:
        return None


def normalize_machine_key(machine):
    text = str(machine)
    if text.lower().startswith("machine"):
        suffix = text[7:]
        if suffix.isdigit():
            return f"Machine{int(suffix)}"
    return text


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


def build_project10_entries(data):
    summed = [0] * 10
    for values in (data or {}).values():
        if not isinstance(values, list):
            continue
        for index, value in enumerate(values[:10]):
            try:
                summed[index] += float(value)
            except (TypeError, ValueError):
                continue

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
    summed = {}
    for column in ALL_COLUMNS:
        keys = HARDCODED_NUM1[column]
        totals = [0.0] * len(keys)
        key_to_index = {number: index for index, number in enumerate(keys)}

        for machine_data in (data or {}).values():
            if not isinstance(machine_data, dict):
                continue
            entries = machine_data.get(str(column)) or machine_data.get(column)
            if not isinstance(entries, list):
                continue

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
            entries.append(
                {
                    "number": number,
                    "column": display_column(column),
                    "half": display_half,
                    "half_key": half,
                    "rank": rank,
                    "row_index": item["row_index"],
                    "key": f"project220:{half}:{display_column(column)}:{number}",
                }
            )
    return entries


def build_project220_entries(data):
    summed = build_project220_summed(data)
    first = build_project220_half_entries(
        summed,
        "first",
        PROJECT220_FIRST_HALF_RANGE[0],
        PROJECT220_FIRST_HALF_RANGE[1],
        PROJECT220_FIRST_HALF_LOW_COUNT,
    )
    second = build_project220_half_entries(
        summed,
        "second",
        PROJECT220_SECOND_HALF_RANGE[0],
        PROJECT220_SECOND_HALF_RANGE[1],
        PROJECT220_SECOND_HALF_LOW_COUNT,
    )
    return first + second


def build_snapshot(project, data, business_date, session, session_started_at, source):
    if project == "project10":
        entries = build_project10_entries(data)
    else:
        entries = build_project220_entries(data)

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
    try:
        snapshot = get_firestore_collection().document(doc_id).get()
        if snapshot.exists:
            return snapshot.to_dict()
    except Exception as error:
        print(f"History Firestore read fallback for {doc_id}: {error}")
    return _memory_docs.get(doc_id)


def save_doc(snapshot):
    doc_id = get_doc_id(snapshot["project"], snapshot["business_date"], snapshot["session"])
    stored_snapshot = {
        **snapshot,
        "doc_id": doc_id,
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    try:
        get_firestore_collection().document(doc_id).set(stored_snapshot, merge=True)
    except Exception as error:
        print(f"History Firestore write fallback for {doc_id}: {error}")
        _memory_docs[doc_id] = {**snapshot, "doc_id": doc_id}
    return {**snapshot, "doc_id": doc_id}


def get_existing_sessions(project, business_date):
    sessions = []
    for session in range(1, MAX_SESSION_PER_DAY + 1):
        if load_doc(project, business_date, session):
            sessions.append(session)
    return sessions


def get_session_number(project, business_date, session_started_at):
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


def save_session_snapshot(project, data, session_started_at=None, source="auto"):
    normalized_project = normalize_project(project)
    if not data:
        return {
            "project": normalized_project,
            "saved": False,
            "reason": "No live data available",
        }

    business_date = get_business_date()
    session = get_session_number(normalized_project, business_date, session_started_at)
    snapshot = build_snapshot(normalized_project, copy.deepcopy(data), business_date, session, session_started_at, source)
    saved_snapshot = save_doc(snapshot)
    print(
        f"Saved {normalized_project} history {business_date} S{session}: "
        f"{saved_snapshot['entry_count']} entries ({source})"
    )
    return {
        "project": normalized_project,
        "saved": True,
        "business_date": business_date,
        "session": session,
        "entry_count": saved_snapshot["entry_count"],
        "doc_id": saved_snapshot["doc_id"],
    }


async def delayed_finalize(project, data_ref, session_started_at, token):
    await asyncio.sleep(AUTO_FINALIZE_SECONDS)
    current = _finalize_tasks.get(project)
    if not current or current.get("token") != token:
        return
    save_session_snapshot(project, copy.deepcopy(data_ref), session_started_at, source="auto")


def schedule_session_finalize(project, data_ref, session_started_at=None):
    try:
        normalized_project = normalize_project(project)
    except HTTPException:
        return

    current = _finalize_tasks.get(normalized_project)
    if current and current.get("task"):
        current["task"].cancel()

    token = time.time()
    task = asyncio.create_task(delayed_finalize(normalized_project, data_ref, session_started_at, token))
    _finalize_tasks[normalized_project] = {"task": task, "token": token}


def date_range(days):
    today = datetime.fromisoformat(get_business_date()).date()
    return [(today - timedelta(days=offset)).isoformat() for offset in range(days)]


def load_snapshots(project, session, days):
    return [
        snapshot
        for business_date in date_range(days)
        for snapshot in [load_doc(project, business_date, session)]
        if snapshot
    ]


def entry_key(entry):
    return entry.get("key") or ":".join(
        str(part) for part in [
            entry.get("half_key"),
            entry.get("column"),
            entry.get("number"),
        ] if part is not None
    )


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
        result.append({
            **entry,
            "days_running": running,
            "range_count": range_count,
            "status": "running" if running > 1 else "today",
        })
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

        breaks.append({
            **entry,
            "days_running": previous_running,
            "range_count": sum(1 for keys in previous_sets if key in keys),
            "status": "break",
        })
    return breaks


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
    snapshots = load_snapshots(project, session, days)
    latest = snapshots[0] if snapshots else None
    current_entries = with_history_stats(latest.get("entries", []) if latest else [], snapshots)
    break_today = build_break_today(snapshots)
    running_count = sum(1 for entry in current_entries if entry.get("days_running", 0) > 1)

    response = {
        "project": project,
        "session": session,
        "session_key": f"S{session}",
        "days": days,
        "snapshots_found": len(snapshots),
        "latest_snapshot": {
            "business_date": latest.get("business_date"),
            "session": latest.get("session"),
            "entry_count": latest.get("entry_count"),
            "saved_at": latest.get("saved_at"),
        } if latest else None,
        "metrics": {
            "total_lows": len(current_entries),
            "running": running_count,
            "break_today": len(break_today),
        },
        "entries": current_entries,
        "break_today": break_today,
    }

    if project == "project220":
        response["project220"] = group_project220(current_entries)
        response["top_running"] = sorted(
            current_entries,
            key=lambda entry: (entry.get("days_running", 0), entry.get("range_count", 0)),
            reverse=True,
        )[:12]
    else:
        response["project10"] = {"entries": current_entries}

    return response


@HistoryRouter.get("/history")
async def get_history(
    project: str = Query("project220"),
    session: int = Query(1),
    days: int = Query(7),
):
    normalized_project = normalize_project(project)
    return analyze_history(normalized_project, safe_session(session), safe_days(days))


@HistoryRouter.post("/history/snapshot-current")
async def snapshot_current(req: Request):
    body = await req.json() if req.headers.get("content-length") else {}
    requested_project = str(body.get("project") or "both").lower()
    results = []

    if requested_project in {"both", "project220", "p220", "project1"}:
        from project1 import project1 as p220_module
        results.append(save_session_snapshot(
            "project220",
            p220_module.latest_project1_data,
            p220_module.project1_session_started_at,
            source="manual",
        ))

    if requested_project in {"both", "project10", "p10", "project2"}:
        from project2 import project2 as p10_module
        results.append(save_session_snapshot(
            "project10",
            p10_module.latest_project2_data,
            p10_module.project2_session_started_at,
            source="manual",
        ))

    if not results:
        raise HTTPException(status_code=400, detail="Invalid project")

    return {"status": "success", "results": results}

