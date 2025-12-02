from fastapi import Request, APIRouter, HTTPException, WebSocket

Project1Router = APIRouter(prefix="/api/v1")

# Store latest data for project1 machines
latest_project1_data = {}
connections_project1 = []

@Project1Router.post("/project1_data")
async def get_p1_data(req: Request):
    try:
        data = await req.json()
        print(f"project1 data: {data}")

        # Merge arrays per machine
        for machine_id, values in data.items():
            latest_project1_data[machine_id] = values

        for conn in connections_project1:
            try:
                await conn.send_json(data)
            except Exception as e:
                print(f"Error sending to WebSocket: {e}")

        return {"status": "success", "data": data}

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
        connections_project1.remove(websocket)
