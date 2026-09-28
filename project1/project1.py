import asyncio

from fastapi import Request, APIRouter, BackgroundTasks, HTTPException, WebSocket
from history import capture
from request_data import json_object
import time

Project1Router = APIRouter(prefix="/api/v1")

# Store latest data for project1 machines
latest_project1_data = {}
connections_project1 = []
last_project1_hit_at = None
project1_session_started_at = None
_data_lock = asyncio.Lock()
_broadcast_lock = asyncio.Lock()
_broadcast_revision = 0


def restore_project1_capture():
    global project1_session_started_at, last_project1_hit_at
    checkpoint = capture.active_capture("project220")
    if checkpoint:
        latest_project1_data.clear()
        latest_project1_data.update(checkpoint["data"])
        project1_session_started_at = checkpoint["session_started_at"]
        last_project1_hit_at = checkpoint["last_received_at"]

def with_project1_meta(data, session_reset=False):
    payload = dict(data)
    payload["__full_snapshot"] = True
    if project1_session_started_at is not None:
        payload["__session_started_at"] = project1_session_started_at
    if session_reset:
        payload["__session_reset"] = True
    return payload

async def broadcast_project1_data(payload, revision):
    async def send_to_connection(conn):
        try:
            await asyncio.wait_for(conn.send_json(payload), timeout=1)
            return None
        except Exception as e:
            print(f"Error sending to Project1 WebSocket: {e}")
            return conn

    async with _broadcast_lock:
        if revision != _broadcast_revision:
            return
        stale_connections = [
            conn for conn in await asyncio.gather(
                *(send_to_connection(conn) for conn in connections_project1.copy())
            )
            if conn is not None
        ]
        for conn in stale_connections:
            if conn in connections_project1:
                connections_project1.remove(conn)


@Project1Router.post("/project1_data")
async def get_p1_data(req: Request, background_tasks: BackgroundTasks):
    global last_project1_hit_at, project1_session_started_at, _broadcast_revision

    try:
        data = await json_object(req)
        now = time.time()
        async with _data_lock:
            accepted = await asyncio.to_thread(capture.accept_data, "project220", data, now)
            session_reset = False
            if accepted["recording"]:
                session_reset = project1_session_started_at != accepted["session_started_at"]
                project1_session_started_at = accepted["session_started_at"]
                latest_project1_data.clear()
                capture.schedule_capture(accepted["key"])
            latest_project1_data.update(accepted["data"])
            last_project1_hit_at = now
            # Full merged payload restores earlier machines after a restart.
            normalized_data = dict(accepted["data"])
            _broadcast_revision += 1
            payload = with_project1_meta(latest_project1_data, session_reset)
            background_tasks.add_task(broadcast_project1_data, payload, _broadcast_revision)
        return {"status": "success", "data": normalized_data}
    except HTTPException:
        raise
    except Exception as e:
        print(f"An Error occurred on our site project1 {str(e)}")
        raise HTTPException(status_code=503, detail="Project220 data could not be durably accepted")

@Project1Router.websocket("/ws_project1")
async def ws_project1(websocket: WebSocket):
    await websocket.accept()
    print("Frontend connected to Project1 WS")
    try:
        # Serialize the initial full state with subsequent broadcasts so a slow
        # initial send cannot arrive after a newer live update.
        async with _broadcast_lock:
            await asyncio.wait_for(websocket.send_json(with_project1_meta(latest_project1_data)), timeout=1)
            connections_project1.append(websocket)
        while True:
            await websocket.receive_text()
    except Exception:
        print("Frontend disconnected from Project1 WS")
    finally:
        if websocket in connections_project1:
            connections_project1.remove(websocket)
