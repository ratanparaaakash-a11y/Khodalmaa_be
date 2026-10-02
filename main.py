import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from security import require_user
from fastapi.middleware.cors import CORSMiddleware
from firebase.firebase import FirebaseRouter
from history.history import HistoryRouter
from history.session_history import history_storage_sync_loop
from history import capture
from project1.project1 import Project1Router, restore_project1_capture
from project2.project2 import (Project2Router, restore_project2_capture,
                               restore_project2_temporary, temporary_expiry_loop)
from telegram.telegram import TelegramRouter
import uvicorn


@asynccontextmanager
async def lifespan(app):
    pending = await asyncio.to_thread(capture.recover_pending)
    for key in pending:
        capture.schedule_capture(key)
    for restore in (restore_project1_capture, restore_project2_capture, restore_project2_temporary):
        try:
            await asyncio.to_thread(restore)
        except Exception as error:
            print(f"Active capture restore failed: {error}")
    sync_task = asyncio.create_task(history_storage_sync_loop())
    recording_task = asyncio.create_task(capture.recording_loop())
    temporary_task = asyncio.create_task(temporary_expiry_loop())
    try:
        yield
    finally:
        sync_task.cancel()
        recording_task.cancel()
        temporary_task.cancel()
        try:
            await sync_task
        except asyncio.CancelledError:
            pass
        try:
            await recording_task
        except asyncio.CancelledError:
            pass
        try:
            await temporary_task
        except asyncio.CancelledError:
            pass
        await capture.shutdown_recording()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],    
    allow_headers=["*"],
) 

app.include_router(FirebaseRouter)
app.include_router(HistoryRouter)
app.include_router(Project1Router)
app.include_router(Project2Router)
app.include_router(TelegramRouter)


@app.get("/")
async def ping():
    return {"status": "ok", "release": "2026-09-28-audit-1",
            "features": {"project10_temporary_machines": True}}


@app.get("/api/v1/auth/verify", dependencies=[Depends(require_user)])
async def verify_session():
    return {"authenticated": True}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000)

