import asyncio

from fastapi import Request, APIRouter, BackgroundTasks, HTTPException, WebSocket
import time

Project1Router = APIRouter(prefix="/api/v1")

# Store latest data for project1 machines
latest_project1_data = {}
connections_project1 = []
last_project1_hit_at = None
project1_session_started_at = None
SESSION_IDLE_RESET_SECONDS = 45 * 60

def with_project1_meta(data, session_reset=False):
    payload = dict(data)
    if project1_session_started_at is not None:
        payload["__session_started_at"] = project1_session_started_at
    if session_reset:
        payload["__session_reset"] = True
    return payload

async def broadcast_project1_data(data, session_reset=False):
    payload = with_project1_meta(data, session_reset)

    async def send_to_connection(conn):
        try:
            await asyncio.wait_for(conn.send_json(payload), timeout=1)
            return None
        except Exception as e:
            print(f"Error sending to Project1 WebSocket: {e}")
            return conn

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
    global last_project1_hit_at, project1_session_started_at

    try:
        data = await req.json()
        now = time.time()
        session_reset = (
            last_project1_hit_at is not None and
            now - last_project1_hit_at > SESSION_IDLE_RESET_SECONDS
        )
        if session_reset:
            latest_project1_data.clear()
            project1_session_started_at = now
            print("Project1 idle session reset")
        elif project1_session_started_at is None:
            project1_session_started_at = now

        last_project1_hit_at = now
        print(f"project1 machines: {list(data.keys())} at {now}")
        normalized_data = {}

        # Merge arrays per machine
        for machine_id, values in data.items():
            if not isinstance(machine_id, str) or not machine_id.lower().startswith("machine"):
                print(f"Skipping invalid project1 key: {machine_id}")
                continue

            normalized_machine_id = machine_id.lower()
            latest_project1_data[normalized_machine_id] = values
            normalized_data[normalized_machine_id] = values

        background_tasks.add_task(broadcast_project1_data, normalized_data, session_reset)

        return {"status": "success", "data": normalized_data}

    except Exception as e:
        print(f"An Error occurred on our site project1 {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@Project1Router.websocket("/ws_project1")
async def ws_project1(websocket: WebSocket):
    await websocket.accept()
    connections_project1.append(websocket)
    print("Frontend connected to Project1 WS")

    # Send current state immediately on connect
    await websocket.send_json(with_project1_meta(latest_project1_data))

    try:
        while True:
            await websocket.receive_text()
    except Exception:
        print("Frontend disconnected from Project1 WS")
    finally:
        if websocket in connections_project1:
            connections_project1.remove(websocket)
