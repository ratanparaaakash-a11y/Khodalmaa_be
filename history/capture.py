"""Durable receipt-time recording for the two daily IST sessions.

Live display is independent of recording. Windows include their start and exclude
their close: S1 [21:30, 22:05), S2 [23:30, next day 00:15).
"""

import asyncio
import copy
import json
import math
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta

from fastapi import HTTPException

from history import session_history as history
from history.rules import ALL_COLUMNS, HARDCODED_NUM1


_lock = threading.RLock()
_tasks = {}
_errors = {}


def scheduled_session(received_at=None):
    instant = time.time() if received_at is None else float(received_at)
    current = datetime.fromtimestamp(instant, history.INDIA_TZ)
    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)
    for day in (midnight, midnight - timedelta(days=1)):
        for number, start, end in (
            (1, day + timedelta(hours=21, minutes=30), day + timedelta(hours=22, minutes=5)),
            (2, day + timedelta(hours=23, minutes=30), day + timedelta(days=1, minutes=15)),
        ):
            if start.timestamp() <= instant < end.timestamp():
                return {
                    "business_date": day.date().isoformat(),
                    "session": number,
                    "session_started_at": start.timestamp(),
                    "session_ends_at": end.timestamp(),
                }
    return None


def validate_session(session):
    if isinstance(session, bool) or str(session) not in {"1", "2"}:
        raise HTTPException(status_code=400, detail="Session must be exactly 1 or 2")
    return int(session)


def validate_data(project, data):
    """Reject incomplete matrices instead of silently ranking missing values as 0."""
    project = history.normalize_project(project)
    if not isinstance(data, dict) or not data:
        raise HTTPException(status_code=400, detail="At least one complete machine is required")
    clean = {}
    for name, values in data.items():
        if isinstance(name, str) and name.startswith("__"):
            continue
        if not isinstance(name, str) or not name.lower().startswith("machine"):
            raise HTTPException(status_code=400, detail="Invalid machine name")
        machine = name.lower()
        if machine in clean:
            raise HTTPException(status_code=400, detail="Duplicate machine name")
        if project == "project10":
            if not isinstance(values, list) or len(values) != 10 or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value > 100000
                for value in values
            ):
                raise HTTPException(status_code=400, detail="Project10 requires 10 finite machine amounts")
            clean[machine] = list(values)
            continue
        if not isinstance(values, dict):
            raise HTTPException(status_code=400, detail="Project220 requires complete machine columns")
        columns = {}
        for column in ALL_COLUMNS:
            entries = values.get(str(column), values.get(column))
            if not isinstance(entries, list):
                raise HTTPException(status_code=400, detail="Project220 machine column is missing")
            amounts = {}
            for entry in entries:
                parsed = history.parse_arrow_entry(entry)
                if (not parsed or parsed[0] not in HARDCODED_NUM1[column]
                        or parsed[0] in amounts or not math.isfinite(parsed[1])):
                    raise HTTPException(status_code=400, detail="Invalid or duplicate Project220 amount")
                amounts[parsed[0]] = parsed[1]
            if set(amounts) != set(HARDCODED_NUM1[column]):
                raise HTTPException(status_code=400, detail="Project220 machine column is incomplete")
            columns[str(column)] = [f"{number}->{amounts[number]}" for number in HARDCODED_NUM1[column]]
        clean[machine] = columns
    if not clean:
        raise HTTPException(status_code=400, detail="At least one complete machine is required")
    return clean


def capture_directory():
    return history._file_store_dir / "captures"


def capture_key(project, window):
    return history.get_doc_id(project, window["business_date"], window["session"])


def checkpoint_path(key):
    return capture_directory() / f"{key}.json"


def record_error(key, error):
    _errors[key] = f"{type(error).__name__}: {error}"[:300]


def atomic_checkpoint(key, checkpoint):
    path = checkpoint_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{key}-", suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            json.dump(checkpoint, stream, separators=(",", ":"), allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def read_checkpoint(key):
    path = checkpoint_path(key)
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as stream:
        checkpoint = json.load(stream)
    project = history.normalize_project(checkpoint.get("project"))
    window = scheduled_session(checkpoint.get("session_started_at"))
    if (not window or capture_key(project, window) != key
            or any(checkpoint.get(field) != value for field, value in window.items())
            or not isinstance(checkpoint.get("version"), int) or checkpoint["version"] < 1
            or not isinstance(checkpoint.get("flushed_version"), int)
            or not 0 <= checkpoint["flushed_version"] <= checkpoint["version"]):
        raise ValueError("Invalid capture checkpoint identity or version")
    checkpoint["data"] = validate_data(project, checkpoint.get("data"))
    receipt = float(checkpoint["last_received_at"])
    if scheduled_session(receipt) != window:
        raise ValueError("Capture receipt is outside its scheduled session")
    return checkpoint


def accept_data(project, data, received_at=None):
    """Persist a complete merged capture before acknowledging an in-window update."""
    project = history.normalize_project(project)
    clean = validate_data(project, data)
    receipt = time.time() if received_at is None else float(received_at)
    window = scheduled_session(receipt)
    if window is None:
        return {"project": project, "recording": False, "data": clean,
                "reason": "Outside the two scheduled recording windows"}
    key = capture_key(project, window)
    with _lock:
        try:
            previous = read_checkpoint(key)
            # A restarted process restores all previously received machines first.
            merged = copy.deepcopy(previous["data"]) if previous else {}
            merged.update(clean)
            checkpoint = {
                "project": project, **window, "data": merged,
                "last_received_at": max(receipt, previous["last_received_at"] if previous else receipt),
                "version": (previous["version"] if previous else 0) + 1,
                "flushed_version": previous["flushed_version"] if previous else 0,
            }
            atomic_checkpoint(key, checkpoint)
            _errors.pop(key, None)
        except Exception as error:
            record_error(key, error)
            raise
    return {"project": project, "recording": True, "key": key, **window,
            "data": copy.deepcopy(merged), "version": checkpoint["version"]}


def due_at(checkpoint):
    return min(checkpoint["last_received_at"] + history.AUTO_FINALIZE_SECONDS,
               checkpoint["session_ends_at"])


def flush_capture(key, force=False, now=None, source="auto"):
    instant = time.time() if now is None else float(now)
    with _lock:
        try:
            checkpoint = read_checkpoint(key)
            if not checkpoint:
                return {"saved": False, "reason": "No captured session data"}
            if checkpoint["version"] == checkpoint["flushed_version"]:
                return {"project": checkpoint["project"], "saved": False, "reason": "Latest capture already saved"}
            if not force and instant < due_at(checkpoint):
                return {"project": checkpoint["project"], "saved": False, "reason": "Capture is pending"}
            snapshot = history.build_snapshot(
                checkpoint["project"], checkpoint["data"], checkpoint["business_date"],
                checkpoint["session"], checkpoint["session_started_at"], source,
            )
            snapshot["captured_at"] = datetime.fromtimestamp(
                checkpoint["last_received_at"], history.INDIA_TZ).isoformat(timespec="seconds")
            saved = history.save_doc(snapshot)
            # The lock spans write + acknowledgement, so an old finalizer cannot
            # clear a more recent receipt. Local disk is the cloud retry source.
            checkpoint["flushed_version"] = checkpoint["version"]
            atomic_checkpoint(key, checkpoint)
            _errors.pop(key, None)
            return {"project": checkpoint["project"], "saved": True,
                    "business_date": checkpoint["business_date"], "session": checkpoint["session"],
                    "doc_id": saved["doc_id"], "entry_count": saved["entry_count"]}
        except Exception as error:
            record_error(key, error)
            raise


async def delayed_flush(key):
    try:
        while True:
            delay = await asyncio.to_thread(pending_delay, key)
            if delay is None:
                return
            if delay:
                await asyncio.sleep(delay)
            result = await asyncio.to_thread(flush_capture, key)
            if result.get("saved"):
                # Re-read: a receipt may have arrived while the thread completed.
                continue
    except asyncio.CancelledError:
        raise
    except Exception as error:
        record_error(key, error)
    finally:
        if _tasks.get(key) is asyncio.current_task():
            _tasks.pop(key, None)


def schedule_capture(key):
    # Do not cancel an in-flight thread. It serializes with newer receipts and
    # re-reads the checkpoint; the recovery loop also retries failed finalizers.
    task = _tasks.get(key)
    if task is None or task.done():
        _tasks[key] = asyncio.create_task(delayed_flush(key))


def pending_delay(key):
    with _lock:
        checkpoint = read_checkpoint(key)
        if not checkpoint or checkpoint["version"] == checkpoint["flushed_version"]:
            return None
        return max(0, due_at(checkpoint) - time.time())


def recover_pending(now=None, force=False):
    pending = []
    with _lock:
        paths = list(capture_directory().glob("*.json"))
    for path in paths:
        try:
            flush_capture(path.stem, force=force, now=now)
            if pending_delay(path.stem) is not None:
                pending.append(path.stem)
        except Exception as error:
            record_error(path.stem, error)
    return pending


def active_capture(project, now=None):
    project = history.normalize_project(project)
    window = scheduled_session(now)
    if not window:
        return None
    key = capture_key(project, window)
    with _lock:
        try:
            checkpoint = read_checkpoint(key)
            return copy.deepcopy(checkpoint)
        except Exception as error:
            record_error(key, error)
            raise


def snapshot_current(project, session_override=None, now=None):
    project = history.normalize_project(project)
    if session_override is not None:
        validate_session(session_override)
    window = scheduled_session(now)
    if not window:
        return {"project": project, "saved": False, "reason": "Outside the two scheduled recording windows"}
    if session_override is not None and int(session_override) != window["session"]:
        raise HTTPException(status_code=409, detail="Requested session does not match the recording window")
    return flush_capture(capture_key(project, window), force=True, now=now, source="manual")


def recording_status():
    pending = []
    with _lock:
        for path in capture_directory().glob("*.json"):
            try:
                checkpoint = read_checkpoint(path.stem)
                if checkpoint["version"] > checkpoint["flushed_version"]:
                    pending.append(path.stem)
            except Exception as error:
                record_error(path.stem, error)
        active = scheduled_session()
        return {
            "state": "degraded" if _errors else "recording" if active else "outside_window",
            "timezone": "Asia/Kolkata", "window_end_exclusive": True,
            "schedule": {"S1": {"start": "21:30", "end": "22:05"},
                         "S2": {"start": "23:30", "end": "00:15", "end_next_day": True}},
            "active_session": active, "pending_captures": len(pending),
            "pending_capture_ids": pending, "errors": dict(_errors),
        }


async def recording_loop():
    while True:
        pending = await asyncio.to_thread(recover_pending)
        for key in pending:
            schedule_capture(key)
        await asyncio.sleep(5)


async def shutdown_recording():
    tasks = list(_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.to_thread(recover_pending, None, True)
