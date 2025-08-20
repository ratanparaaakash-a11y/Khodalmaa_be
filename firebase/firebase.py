from fastapi import APIRouter,HTTPException
from firebase.config import auth


FirebaseRouter = APIRouter(prefix="/api/v1")


required_fields = ["email","password"]

@FirebaseRouter.post("/create_user")
async def create_user(payload: dict):
    for req in required_fields:
        if not payload.get(req):
            return {"error":f"Field '{req}' is required"}
    try:
        auth.create_user(
            email=payload["email"],
            password=payload["password"]
        )
        print(f"User created Successfully payload:{payload}")
        return {"message":"User Created Successfully"}
    except Exception as e:
        print(f"Firebase Auth connection failed error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

