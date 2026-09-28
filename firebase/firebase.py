import asyncio
from fastapi import APIRouter, Depends, HTTPException
from firebase_admin import auth
from security import require_admin


FirebaseRouter = APIRouter(prefix="/api/v1")


@FirebaseRouter.post("/create_user", dependencies=[Depends(require_admin)])
async def create_user(payload: dict):
    email = payload.get("email")
    password = payload.get("password")
    if not isinstance(email, str) or not email.strip():
        raise HTTPException(status_code=400, detail="email is required")
    if not isinstance(password, str) or not password:
        raise HTTPException(status_code=400, detail="password is required")
    try:
        await asyncio.to_thread(auth.create_user, email=email.strip(), password=password)
        return {"message":"User Created Successfully"}
    except auth.EmailAlreadyExistsError:
        raise HTTPException(status_code=409, detail="An account with this email already exists") from None
    except ValueError:
        raise HTTPException(status_code=400, detail="Email or password is invalid") from None
    except Exception:
        raise HTTPException(status_code=503, detail="Account creation is temporarily unavailable") from None

