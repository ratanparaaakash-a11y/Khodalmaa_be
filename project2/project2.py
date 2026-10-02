import asyncio

from fastapi import Request, APIRouter, BackgroundTasks, HTTPException, WebSocket
from history import capture
from project2 import temporary
from request_data import json_object
import time
Project2Router = APIRouter(prefix="/api/v1")


latest_project2_data = {}
connections_project2 = []
last_project2_hit_at = None
project2_session_started_at = None
_data_lock = asyncio.Lock()
_broadcast_lock = asyncio.Lock()
_broadcast_revision = 0
_temporary_wake = asyncio.Event()


def restore_project2_capture():
    global project2_session_started_at, last_project2_hit_at
    checkpoint = capture.active_capture("project10")
    if checkpoint:
        latest_project2_data.clear()
        latest_project2_data.update(checkpoint["data"])
        project2_session_started_at = checkpoint["session_started_at"]
        last_project2_hit_at = checkpoint["last_received_at"]


def restore_project2_temporary():
    return temporary.restore(occupied=latest_project2_data)


def with_project2_meta(data, session_reset=False):
    payload = dict(data)
    occupied = {name.lower() for name in data}
    temporary_meta = {}
    for name, entry in temporary.live_entries().items():
        if name not in occupied:
            payload[name] = entry["values"]
            temporary_meta[name] = {key: entry[key] for key in ("created_at", "expires_at")}
    if temporary_meta:
        payload["__temporary_machines"] = temporary_meta
    payload["__full_snapshot"] = True
    if project2_session_started_at is not None:
        payload["__session_started_at"] = project2_session_started_at
    if session_reset:
        payload["__session_reset"] = True
    return payload


async def broadcast_project2_data(payload, revision):
    async def send_to_connection(conn):
        try:
            await asyncio.wait_for(conn.send_json(payload), timeout=1)
            return None
        except Exception as e:
            print(f"Error sending to Project2 WebSocket: {e}")
            return conn

    async with _broadcast_lock:
        if revision != _broadcast_revision:
            return
        # A background send can wait behind another socket past its deadline.
        # Never send an expired temporary row from the earlier captured payload.
        payload = dict(payload)
        temporary_meta = dict(payload.get("__temporary_machines", {}))
        for name, entry in list(temporary_meta.items()):
            if entry["expires_at"] <= time.time():
                payload.pop(name, None)
                temporary_meta.pop(name)
        if temporary_meta:
            payload["__temporary_machines"] = temporary_meta
        else:
            payload.pop("__temporary_machines", None)
        stale_connections = [
            conn for conn in await asyncio.gather(
                *(send_to_connection(conn) for conn in connections_project2.copy())
            )
            if conn is not None
        ]
        for conn in stale_connections:
            if conn in connections_project2:
                connections_project2.remove(conn)


async def temporary_expiry_loop():
    global _broadcast_revision
    while True:
        _temporary_wake.clear()
        deadline = temporary.next_deadline()
        if deadline is None:
            await _temporary_wake.wait()
            continue
        delay = max(0, deadline - time.time())
        if delay:
            try:
                await asyncio.wait_for(_temporary_wake.wait(), timeout=delay)
                continue
            except asyncio.TimeoutError:
                pass
        try:
            cleanup_failed = False
            async with _data_lock:
                try:
                    changed = await asyncio.to_thread(temporary.expire)
                except Exception as error:
                    # The visible deadline must not depend on disk availability.
                    print(f"Temporary Project10 expiry cleanup failed: {type(error).__name__}")
                    cleanup_failed = True
                    changed = True
                if not changed:
                    continue
                _broadcast_revision += 1
                revision = _broadcast_revision
                payload = with_project2_meta(latest_project2_data)
            await broadcast_project2_data(payload, revision)
            if cleanup_failed:
                await asyncio.sleep(1)
        except Exception as error:
            # Expired rows remain filtered from all fresh snapshots during a disk retry.
            print(f"Temporary Project10 expiry cleanup failed: {type(error).__name__}")
            await asyncio.sleep(1)


async def accept_project2_data(data, background_tasks):
    global last_project2_hit_at, project2_session_started_at, _broadcast_revision

    try:
        now = time.time()
        async with _data_lock:
            if "__temporary_for_seconds" in data:
                duration = data.pop("__temporary_for_seconds")
                entry = await asyncio.to_thread(temporary.add, data, duration,
                                                occupied=latest_project2_data, now=now)
                _temporary_wake.set()
                _broadcast_revision += 1
                payload = with_project2_meta(latest_project2_data)
                background_tasks.add_task(broadcast_project2_data, payload, _broadcast_revision)
                return {"status": "success", "data": {entry["name"]: entry["values"]},
                        "temporary": {"name": entry["name"], "created_at": entry["created_at"],
                                      "expires_at": entry["expires_at"], "duration_seconds": duration}}
            accepted = await asyncio.to_thread(capture.accept_data, "project10", data, now)
            # Only a valid, durably accepted real producer can supersede an overlay.
            if await asyncio.to_thread(temporary.remove_for_real, accepted["data"]):
                _temporary_wake.set()
            session_reset = False
            if accepted["recording"]:
                session_reset = project2_session_started_at != accepted["session_started_at"]
                project2_session_started_at = accepted["session_started_at"]
                latest_project2_data.clear()
                capture.schedule_capture(accepted["key"])
            latest_project2_data.update(accepted["data"])
            last_project2_hit_at = now
            filtered_data = dict(accepted["data"])
            _broadcast_revision += 1
            payload = with_project2_meta(latest_project2_data, session_reset)
            background_tasks.add_task(broadcast_project2_data, payload, _broadcast_revision)
        return {"status": "success", "data": filtered_data}
    except HTTPException:
        raise
    except Exception as e:
        print(f"An error occurred in project2_data: {str(e)}")
        raise HTTPException(status_code=503, detail="Project10 data could not be durably accepted")


@Project2Router.post("/project2_data")
async def get_p2_data(req: Request, background_tasks: BackgroundTasks):
    return await accept_project2_data(await json_object(req), background_tasks)


@Project2Router.post("/project2_temporary_data")
async def get_p2_temporary_data(req: Request, background_tasks: BackgroundTasks):
    data = await json_object(req)
    if "__temporary_for_seconds" not in data:
        raise HTTPException(status_code=400, detail="Temporary duration is required")
    return await accept_project2_data(data, background_tasks)


@Project2Router.websocket("/ws_project2")
async def ws_project2(websocket: WebSocket):
    await websocket.accept()
    print("Frontend connected to Project2 WS")
    try:
        async with _broadcast_lock:
            await asyncio.wait_for(websocket.send_json(with_project2_meta(latest_project2_data)), timeout=1)
            connections_project2.append(websocket)
        while True:
            await websocket.receive_text() 
    except Exception:
        print("Frontend disconnected from Project2 WS")
    finally:
        if websocket in connections_project2:
            connections_project2.remove(websocket)
