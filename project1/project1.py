import asyncio

from fastapi import Request, APIRouter, BackgroundTasks, HTTPException, WebSocket
import time

Project1Router = APIRouter(prefix="/api/v1")

# Store latest data for project1 machines
latest_project1_data = {}
connections_project1 = []

async def broadcast_project1_data(data):
    stale_connections = []

    for conn in connections_project1.copy():
        try:
            await asyncio.wait_for(conn.send_json(data), timeout=1)
        except Exception as e:
            print(f"Error sending to Project1 WebSocket: {e}")
            stale_connections.append(conn)

    for conn in stale_connections:
        if conn in connections_project1:
            connections_project1.remove(conn)


@Project1Router.post("/project1_data")
async def get_p1_data(req: Request, background_tasks: BackgroundTasks):
    try:
        data = await req.json()
        print(f"project1 machines: {list(data.keys())} at {time.time()}")
        normalized_data = {}

        # Merge arrays per machine
        for machine_id, values in data.items():
            if not isinstance(machine_id, str) or not machine_id.lower().startswith("machine"):
                print(f"Skipping invalid project1 key: {machine_id}")
                continue

            normalized_machine_id = machine_id.lower()
            latest_project1_data[normalized_machine_id] = values
            normalized_data[normalized_machine_id] = values

        background_tasks.add_task(broadcast_project1_data, normalized_data)

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
    await websocket.send_json(latest_project1_data)

    try:
        while True:
            await websocket.receive_text()
    except Exception:
        print("Frontend disconnected from Project1 WS")
    finally:
        if websocket in connections_project1:
            connections_project1.remove(websocket)
