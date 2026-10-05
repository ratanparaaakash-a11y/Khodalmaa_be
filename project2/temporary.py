"""Cloud-confirmed temporary Project10 rows, outside real-machine History."""

import asyncio
import copy
import json
import math
import os
import re
import tempfile
import threading
import time
from uuid import uuid4

from fastapi import HTTPException
from history import capture
from history import session_history as history
from project2 import temporary_cloud as cloud

_entries = {}
_retired = {}
_blocked_names = set()
_real_generations = {}
_lock = threading.RLock()
# Cloud requests never hold _lock: real-machine updates can always retire a row.
_cloud_lock = threading.Lock()
_last_cloud_error = None
_last_local_error = None


def storage_path():
    return history._file_store_dir / "temporary" / "project10.json"


def _save(entries):
    path = storage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".project10-", suffix=".tmp", delete=False) as stream:
            temporary_path = stream.name
            json.dump({"version": 2, "entries": entries, "retired": _retired,
                       "blocked_names": sorted(_blocked_names)}, stream, allow_nan=False,
                      separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)


def _cache():
    global _last_local_error
    try:
        _save(_entries)
        _last_local_error = None
    except Exception as error:
        # Cloud is the durable creation source; a disposable cache cannot undo it.
        _last_local_error = type(error).__name__


def _now(now):
    return time.time() if now is None else float(now)


def _checkpoint(value):
    if value is None:
        return {"version": 1, "entries": {}, "retired": {}}
    if (not isinstance(value, dict) or value.get("version") != 1
            or not isinstance(value.get("entries"), dict)
            or not isinstance(value.get("retired"), dict)):
        raise cloud.CloudUnavailable("Invalid temporary machine cloud checkpoint")
    result = {"version": 1, "entries": {}, "retired": {}}
    for name, entry in value["entries"].items():
        if (not isinstance(name, str) or not re.fullmatch(r"machine\d+", name)
                or not isinstance(entry, dict)):
            raise cloud.CloudUnavailable("Invalid temporary machine cloud checkpoint")
        try:
            values = capture.validate_data("project10", {name: entry.get("values")})[name]
        except HTTPException:
            raise cloud.CloudUnavailable("Invalid temporary machine cloud checkpoint") from None
        created, expires = entry.get("created_at"), entry.get("expires_at")
        lease = entry.get("lease_id")
        if (any(isinstance(v, bool) or not isinstance(v, (int, float))
                or not math.isfinite(v) for v in (created, expires))
                or not 0 < expires - created <= 1800
                or not isinstance(lease, str) or not re.fullmatch(r"[a-f0-9]{32}", lease)):
            raise cloud.CloudUnavailable("Invalid temporary machine cloud checkpoint")
        result["entries"][name] = {"values": values, "created_at": created,
                                   "expires_at": expires, "lease_id": lease}
    for lease, expires in value["retired"].items():
        if (not isinstance(lease, str) or not re.fullmatch(r"[a-f0-9]{32}", lease)
                or isinstance(expires, bool) or not isinstance(expires, (int, float))
                or not math.isfinite(expires)):
            raise cloud.CloudUnavailable("Invalid temporary machine cloud checkpoint")
        result["retired"][lease] = expires
    return result


def _cloud_update(instant, addition=None):
    """Read/conditional-write under cloud lock only. Caller handles visibility."""
    global _last_cloud_error
    try:
        for attempt in range(2):
            raw, revision = cloud.load()
            previous = _checkpoint(raw)
            with _lock:
                retired = {**previous["retired"], **_retired}
                blocked = set(_blocked_names)
            entries = copy.deepcopy(previous["entries"])
            for name, entry in entries.items():
                if name in blocked:
                    retired[entry["lease_id"]] = entry["expires_at"]
            retired = {lease: expiry for lease, expiry in retired.items() if expiry > instant}
            entries = {name: entry for name, entry in entries.items()
                       if entry["expires_at"] > instant and entry["lease_id"] not in retired}
            if addition:
                name, entry = addition
                if name in entries:
                    raise HTTPException(status_code=409, detail="This machine name is already in use")
                entries[name] = entry
            updated = {"version": 1, "entries": entries, "retired": retired}
            try:
                if updated != previous:
                    cloud.save(updated, revision)
            except cloud.CloudConflict:
                if attempt == 0:
                    continue
                raise cloud.CloudUnavailable("Temporary machine checkpoint is busy") from None
            _last_cloud_error = None
            return updated
    except cloud.CloudUnavailable as error:
        _last_cloud_error = str(error)
        raise


def _publish(checkpoint, instant):
    """Apply confirmed remote state, preserving retirements arriving during I/O."""
    with _lock:
        previous_retirements = dict(_retired)
        for name, entry in checkpoint["entries"].items():
            if name in _blocked_names:
                _retired[entry["lease_id"]] = entry["expires_at"]
        visible = {name: entry for name, entry in checkpoint["entries"].items()
                   if entry["expires_at"] > instant and name not in _blocked_names
                   and entry["lease_id"] not in _retired}
        changed = visible != _entries
        _entries.clear()
        _entries.update(copy.deepcopy(visible))
        for lease, expiry in list(_retired.items()):
            if expiry <= instant or lease in checkpoint["retired"]:
                _retired.pop(lease, None)
        if changed or previous_retirements != _retired:
            _cache()
        return changed


def restore(now=None, occupied=()):
    """Never promote an unverified disk cache into the live view after restart."""
    instant = _now(now)
    with _lock:
        _entries.clear()
        path = storage_path()
        if path.exists():
            try:
                with path.open(encoding="utf-8") as stream:
                    local = json.load(stream)
                if local.get("version") == 2:
                    retired = _checkpoint({"version": 1, "entries": {},
                                           "retired": local.get("retired", {})})["retired"]
                    _retired.update(retired)
                    _blocked_names.update(name for name in local.get("blocked_names", [])
                                          if isinstance(name, str) and re.fullmatch(r"machine\d+", name))
            except Exception:
                # A valid cloud response is still required; local rows are never used.
                pass
        _blocked_names.update(name.lower() for name in occupied)
    sync_cloud(now=instant)
    return live_entries(now=instant)


def add(data, duration, occupied=(), now=None):
    if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= 1800:
        raise HTTPException(status_code=400, detail="Temporary duration must be 1 to 1800 whole seconds")
    if (not isinstance(data, dict) or len(data) != 1
            or any(not isinstance(name, str) or not re.fullmatch(r"machine\d+", name, re.I)
                   for name in data)):
        raise HTTPException(status_code=400, detail="Exactly one complete temporary machine is required")
    clean = capture.validate_data("project10", data)
    name, values = next(iter(clean.items()))
    instant = _now(now)
    with _lock:
        if name in {key.lower() for key in occupied} or (
                name in _entries and _entries[name]["expires_at"] > instant):
            raise HTTPException(status_code=409, detail="This machine name is already in use")
        generation = _real_generations.get(name, 0)
    entry = {"values": values, "created_at": instant, "expires_at": instant + duration,
             "lease_id": uuid4().hex}
    with _cloud_lock:
        checkpoint = _cloud_update(instant, (name, entry))
        with _lock:
            if _real_generations.get(name, 0) != generation:
                _retired[entry["lease_id"]] = entry["expires_at"]
                _cache()
                raise HTTPException(status_code=409, detail="A live machine now uses this name")
            _blocked_names.discard(name)
        _publish(checkpoint, _now(now))
    return {"name": name, **copy.deepcopy(entry)}


def remove_for_real(names):
    """Retire locally without waiting for cloud I/O; background sync tombstones it."""
    names = {name.lower() for name in names}
    with _lock:
        changed = False
        block_changed = bool(names - _blocked_names)
        _blocked_names.update(names)
        for name in names:
            _real_generations[name] = _real_generations.get(name, 0) + 1
            entry = _entries.pop(name, None)
            if entry:
                _retired[entry["lease_id"]] = entry["expires_at"]
                changed = True
        if changed or block_changed:
            _cache()
        return changed


def expire(now=None):
    instant = _now(now)
    with _lock:
        expired = [name for name, entry in _entries.items() if entry["expires_at"] <= instant]
        for name in expired:
            _entries.pop(name)
        if expired:
            _cache()
        return bool(expired)


def sync_cloud(now=None):
    with _cloud_lock:
        checkpoint = _cloud_update(_now(now))
        return _publish(checkpoint, _now(now))


async def cloud_sync_loop(on_change=None):
    while True:
        try:
            changed = await asyncio.to_thread(sync_cloud)
            if changed and on_change:
                await on_change()
        except cloud.CloudUnavailable:
            pass  # Last confirmed rows still expire locally at the original deadline.
        # Retirement/outage recovery is prompt; idle polling avoids excess reads.
        with _lock:
            delay = 5 if _retired or _last_cloud_error else 30
        await asyncio.sleep(delay)


def live_entries(now=None):
    instant = _now(now)
    with _lock:
        return copy.deepcopy({name: entry for name, entry in _entries.items()
                              if entry["expires_at"] > instant})


def live_data(now=None):
    return {name: entry["values"] for name, entry in live_entries(now).items()}


def next_deadline():
    with _lock:
        return min((entry["expires_at"] for entry in _entries.values()), default=None)


def storage_status():
    return {"cloud_error": _last_cloud_error, "local_cache_error": _last_local_error,
            "pending_retirements": len(_retired)}
