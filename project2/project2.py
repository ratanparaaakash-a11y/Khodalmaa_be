import asyncio

from fastapi import Request, APIRouter, BackgroundTasks, HTTPException, WebSocket
import time
Project2Router = APIRouter(prefix="/api/v1")


latest_project2_data = {}
connections_project2 = []


async def broadcast_project2_data(data):
    stale_connections = []

    for conn in connections_project2.copy():
        try:
            await asyncio.wait_for(conn.send_json(data), timeout=1)
        except Exception as e:
            print(f"Error sending to Project2 WebSocket: {e}")
            stale_connections.append(conn)

    for conn in stale_connections:
        if conn in connections_project2:
            connections_project2.remove(conn)


@Project2Router.post("/project2_data")
async def get_p2_data(req: Request, background_tasks: BackgroundTasks):
    try:
        data = await req.json()
        print(f"project2 machines: {list(data.keys())} at {time.time()}")
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

        background_tasks.add_task(broadcast_project2_data, filtered_data)

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
        await websocket.send_json(latest_project2_data)

    try:
        while True:
            await websocket.receive_text() 
    except Exception:
        print("Frontend disconnected from Project2 WS")
    finally:
        if websocket in connections_project2:
            connections_project2.remove(websocket)
