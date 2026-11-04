import asyncio

from fastapi import Request, APIRouter, BackgroundTasks, HTTPException, WebSocket
from history import capture
from live import LiveChannel
from request_data import json_object
from security import authenticate_websocket, authorize_machine_request, receive_authenticated_text
import time

Project1Router = APIRouter(prefix="/api/v1")

# Store latest data for project1 machines
latest_project1_data = {}
live_project1 = LiveChannel("Project1 WS")
connections_project1 = live_project1.sockets
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

def publish_project1_data(payload, revision):
    """Publish without waiting on any viewer; a superseded revision is never sent."""
    if revision != _broadcast_revision:
        return False
    live_project1.publish(payload)
    return True


async def broadcast_project1_data(payload, revision):
    return publish_project1_data(payload, revision)


@Project1Router.post("/project1_data")
async def get_p1_data(req: Request, background_tasks: BackgroundTasks):
    global last_project1_hit_at, project1_session_started_at, _broadcast_revision

    try:
        data = await json_object(req)
        authorize_machine_request(req, "project220", data)
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
            # Publishing is non-blocking, so do it now instead of after this
            # producer's (possibly slow) response has been fully sent.
            publish_project1_data(with_project1_meta(latest_project1_data, session_reset), _broadcast_revision)
        return {"status": "success", "data": normalized_data}
    except HTTPException:
        raise
    except Exception as e:
        print(f"An Error occurred on our site project1 {str(e)}")
        raise HTTPException(status_code=503, detail="Project220 data could not be durably accepted")

@Project1Router.websocket("/ws_project1")
async def ws_project1(websocket: WebSocket):
    claims = await authenticate_websocket(websocket)
    if claims is None:
        return
    print("Frontend connected to Project1 WS", flush=True)
    try:
        # attach() queues the current full state and registers the viewer in
        # one synchronous step, so a newer live update always arrives after it.
        live_project1.attach(websocket, with_project1_meta(latest_project1_data))
        while True:
            await receive_authenticated_text(websocket, claims)
    except Exception:
        print("Frontend disconnected from Project1 WS", flush=True)
    finally:
        await live_project1.detach(websocket)
