import asyncio
import copy
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from history import capture
from request_data import json_object
from security import require_admin, require_user

from history.session_history import (
    analyze_history,
    analyze_average_history,
    analyze_project220_sent_low_report,
    get_storage_status,
    normalize_project,
    safe_days,
    safe_session,
    save_built_session_snapshot,
    save_project220_sent_low_snapshot,
    save_session_snapshot,
)


HistoryRouter = APIRouter(prefix="/api/v1")


@HistoryRouter.get("/history/health", dependencies=[Depends(require_user)])
async def history_health():
    storage = await asyncio.to_thread(get_storage_status)
    status = "degraded" if storage["recording"]["state"] == "degraded" else (
        "ok" if storage["cloud_state"] == "connected" else storage["cloud_state"]
    )
    return {"status": status, "feature": "session_history", "storage": storage}


@HistoryRouter.get("/history", dependencies=[Depends(require_user)])
async def get_history(
    project: str = Query("project220"),
    session: int = Query(1),
    days: int = Query(7),
    mode: str = Query("session"),
):
    if str(mode).strip().lower() in {"average", "avg", "combined"}:
        return await asyncio.to_thread(analyze_average_history, normalize_project(project), safe_days(days))
    return await asyncio.to_thread(analyze_history, normalize_project(project), safe_session(session), safe_days(days))


@HistoryRouter.post("/history/snapshot-current", dependencies=[Depends(require_user)])
async def snapshot_current(req: Request):
    body = await json_object(req, allow_empty=True)
    requested_project = str(body.get("project") or "both").lower()
    session_override = body.get("session")
    results = []

    if requested_project in {"both", "project220", "p220", "project1"}:
        results.append(await asyncio.to_thread(
            capture.snapshot_current,
            "project220",
            session_override,
        ))

    if requested_project in {"both", "project10", "p10", "project2"}:
        results.append(await asyncio.to_thread(
            capture.snapshot_current,
            "project10",
            session_override,
        ))

    if not results:
        raise HTTPException(status_code=400, detail="Invalid project")

    return {"status": "success", "results": results}


@HistoryRouter.post("/history/snapshot", dependencies=[Depends(require_admin)])
async def snapshot_payload(req: Request):
    body = await json_object(req)
    requested_project = normalize_project(body.get("project"))
    data = body.get("data") or {}
    result = await asyncio.to_thread(
        save_session_snapshot,
        requested_project,
        data,
        body.get("session_started_at"),
        body.get("source") or "import",
        body.get("session"),
    )
    return {"status": "success", "result": result}


@HistoryRouter.post("/history/snapshot-built", dependencies=[Depends(require_admin)])
async def snapshot_built(req: Request):
    body = await json_object(req)
    result = await asyncio.to_thread(
        save_built_session_snapshot,
        body.get("project"),
        body.get("business_date"),
        body.get("session"),
        body.get("entries") or [],
        body.get("saved_at"),
        body.get("source") or "restore",
    )
    return {"status": "success", "result": result}


@HistoryRouter.post("/project220/sent-low-snapshot", dependencies=[Depends(require_user)])
async def project220_sent_low_snapshot(req: Request):
    body = await json_object(req)
    from project1 import project1 as p220_module

    result = await asyncio.to_thread(
        save_project220_sent_low_snapshot,
        body.get("button"),
        body.get("entries") or [],
        p220_module.project1_session_started_at,
    )
    return {"status": "success", "result": result}


@HistoryRouter.get("/project220/sent-low-calculation", dependencies=[Depends(require_user)])
async def project220_sent_low_calculation(
    business_date: Optional[str] = Query(None),
    session: Optional[int] = Query(None),
):
    from project1 import project1 as p220_module

    return await asyncio.to_thread(
        analyze_project220_sent_low_report,
        copy.deepcopy(p220_module.latest_project1_data),
        business_date,
        session,
    )
