import asyncio

from fastapi import Request, APIRouter, BackgroundTasks, HTTPException, WebSocket
from history import capture
import time
Project2Router = APIRouter(prefix="/api/v1")


latest_project2_data = {}
connections_project2 = []
last_project2_hit_at = None
project2_session_started_at = None
_data_lock = asyncio.Lock()
_broadcast_lock = asyncio.Lock()
_broadcast_revision = 0


def restore_project2_capture():
    global project2_session_started_at, last_project2_hit_at
    checkpoint = capture.active_capture("project10")
    if checkpoint:
        latest_project2_data.clear()
        latest_project2_data.update(checkpoint["data"])
        project2_session_started_at = checkpoint["session_started_at"]
        last_project2_hit_at = checkpoint["last_received_at"]

def with_project2_meta(data, session_reset=False):
    payload = dict(data)
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
        stale_connections = [
            conn for conn in await asyncio.gather(
                *(send_to_connection(conn) for conn in connections_project2.copy())
            )
            if conn is not None
        ]
        for conn in stale_connections:
            if conn in connections_project2:
                connections_project2.remove(conn)


@Project2Router.post("/project2_data")
async def get_p2_data(req: Request, background_tasks: BackgroundTasks):
    global last_project2_hit_at, project2_session_started_at, _broadcast_revision

    try:
        data = await req.json()
        now = time.time()
        async with _data_lock:
            accepted = await asyncio.to_thread(capture.accept_data, "project10", data, now)
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


@Project2Router.websocket("/ws_project2")
async def ws_project2(websocket: WebSocket):
    await websocket.accept()
    connections_project2.append(websocket)
    print("Frontend connected to Project2 WS")

    if latest_project2_data:
        await websocket.send_json(with_project2_meta(latest_project2_data))

    try:
        while True:
            await websocket.receive_text() 
    except Exception:
        print("Frontend disconnected from Project2 WS")
    finally:
        if websocket in connections_project2:
            connections_project2.remove(websocket)
