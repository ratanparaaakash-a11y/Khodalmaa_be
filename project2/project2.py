import asyncio

from fastapi import Request, APIRouter, BackgroundTasks, HTTPException, WebSocket
import time
Project2Router = APIRouter(prefix="/api/v1")


latest_project2_data = {}
connections_project2 = []
last_project2_hit_at = None
project2_session_started_at = None
SESSION_IDLE_RESET_SECONDS = 45 * 60

def with_project2_meta(data, session_reset=False):
    payload = dict(data)
    if project2_session_started_at is not None:
        payload["__session_started_at"] = project2_session_started_at
    if session_reset:
        payload["__session_reset"] = True
    return payload


async def broadcast_project2_data(data, session_reset=False):
    payload = with_project2_meta(data, session_reset)

    async def send_to_connection(conn):
        try:
            await asyncio.wait_for(conn.send_json(payload), timeout=1)
            return None
        except Exception as e:
            print(f"Error sending to Project2 WebSocket: {e}")
            return conn

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
    global last_project2_hit_at, project2_session_started_at

    try:
        data = await req.json()
        now = time.time()
        session_reset = (
            last_project2_hit_at is not None and
            now - last_project2_hit_at > SESSION_IDLE_RESET_SECONDS
        )
        if session_reset:
            latest_project2_data.clear()
            project2_session_started_at = now
            print("Project2 idle session reset")
        elif project2_session_started_at is None:
            project2_session_started_at = now

        last_project2_hit_at = now
        print(f"project2 machines: {list(data.keys())} at {now}")
        filtered_data = {}

        for machine_id, values in data.items():
            if not isinstance(values, list):
                print(f"Skipping {machine_id}: values is not a list")
                continue

            if len(values) != 10:
                print(f"Skipping {machine_id}: invalid length {len(values)}")
                continue

            if not all(isinstance(v, (int, float)) for v in values):
                print(f"Skipping {machine_id}: contains non-numeric values")
                continue

            if any(v > 100000 for v in values):
                print(f"Skipping {machine_id}: contains large value")
                continue

            filtered_data[machine_id] = values
            latest_project2_data[machine_id] = values

        background_tasks.add_task(broadcast_project2_data, filtered_data, session_reset)

        return {"status": "success", "data": filtered_data}

    except Exception as e:
        print(f"An error occurred in project2_data: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")


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
