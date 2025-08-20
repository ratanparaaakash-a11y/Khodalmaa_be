from fastapi import Request, APIRouter, HTTPException, WebSocket

Project2Router = APIRouter(prefix="/api/v1")

latest_project2_data = {}
connections_project2 = []

@Project2Router.post("/project2_data")
async def get_p2_data(req: Request):
    try:
        data = await req.json()
        print(f"project2 data: {data}")

        # Merge arrays per machine
        for machine_id, values in data.items():
            latest_project2_data[machine_id] = values

        for conn in connections_project2:
            try:
                await conn.send_json(data)
            except Exception as e:
                print(f"Error sending to WebSocket: {e}")

        return {"status": "success", "data": data}
    except Exception as e:
        print(f"An Error occurred on our site project1 {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    
@Project2Router.websocket("/ws_project2")
async def ws_project1(websocket: WebSocket):
    await websocket.accept()
    connections_project2.append(websocket)
    print("Frontend connected to Project2 WS")

    await websocket.send_json(latest_project2_data)

    try:
        while True:
            await websocket.receive_text()
    except Exception:
        print("Frontend disconnected from Project2 WS")
        connections_project2.remove(websocket)