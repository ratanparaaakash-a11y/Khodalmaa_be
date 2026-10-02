"""Temporary Project10 rows, kept outside the real-machine History capture."""

import copy
import json
import math
import os
import re
import tempfile
import threading
import time

from fastapi import HTTPException
from history import capture
from history import session_history as history


_entries = {}
_lock = threading.RLock()


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
            json.dump({"version": 1, "entries": entries}, stream, allow_nan=False,
                      separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)


def _replace(entries):
    _save(entries)
    _entries.clear()
    _entries.update(entries)


def _now(now):
    return time.time() if now is None else float(now)


def restore(now=None, occupied=()):
    instant = _now(now)
    occupied = {name.lower() for name in occupied}
    with _lock:
        path = storage_path()
        if not path.exists():
            _entries.clear()
            return {}
        with path.open(encoding="utf-8") as stream:
            stored = json.load(stream)
        if stored.get("version") != 1 or not isinstance(stored.get("entries"), dict):
            raise ValueError("Invalid temporary Project10 storage")
        active = {}
        for name, entry in stored["entries"].items():
            if (not isinstance(name, str) or not re.fullmatch(r"machine\d+", name)
                    or not isinstance(entry, dict)):
                raise ValueError("Invalid temporary Project10 machine")
            values = capture.validate_data("project10", {name: entry.get("values")})[name]
            created = entry.get("created_at")
            expires = entry.get("expires_at")
            if (any(isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) for value in (created, expires))
                    or not 0 < expires - created <= 1800):
                raise ValueError("Invalid temporary Project10 deadline")
            if expires > instant and name not in occupied:
                active[name] = {"values": values, "created_at": created, "expires_at": expires}
        if active != stored["entries"]:
            _save(active)
        _entries.clear()
        _entries.update(active)
        return copy.deepcopy(active)


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
        active = {key: entry for key, entry in _entries.items() if entry["expires_at"] > instant}
        if name in {key.lower() for key in occupied} or name in active:
            raise HTTPException(status_code=409, detail="This machine name is already in use")
        entry = {"values": values, "created_at": instant, "expires_at": instant + duration}
        active[name] = entry
        _replace(active)
        return {"name": name, **copy.deepcopy(entry)}


def remove_for_real(names):
    names = {name.lower() for name in names}
    with _lock:
        remaining = {name: entry for name, entry in _entries.items() if name not in names}
        if len(remaining) == len(_entries):
            return False
        _replace(remaining)
        return True


def expire(now=None):
    instant = _now(now)
    with _lock:
        remaining = {name: entry for name, entry in _entries.items() if entry["expires_at"] > instant}
        if len(remaining) == len(_entries):
            return False
        _replace(remaining)
        return True


def live_entries(now=None):
    instant = _now(now)
    with _lock:
        # Filtering here also protects snapshots if expiry cleanup is delayed.
        return copy.deepcopy({name: entry for name, entry in _entries.items()
                              if entry["expires_at"] > instant})


def live_data(now=None):
    return {name: entry["values"] for name, entry in live_entries(now).items()}


def next_deadline():
    with _lock:
        return min((entry["expires_at"] for entry in _entries.values()), default=None)
