from fastapi import APIRouter, HTTPException, Query, Request

from history.session_history import (
    analyze_history,
    normalize_project,
    safe_days,
    safe_session,
    save_session_snapshot,
)


HistoryRouter = APIRouter(prefix="/api/v1")


@HistoryRouter.get("/history/health")
async def history_health():
    return {"status": "ok", "feature": "session_history"}


@HistoryRouter.get("/history")
async def get_history(
    project: str = Query("project220"),
    session: int = Query(1),
    days: int = Query(7),
):
    return analyze_history(normalize_project(project), safe_session(session), safe_days(days))


@HistoryRouter.post("/history/snapshot-current")
async def snapshot_current(req: Request):
    body = await req.json() if req.headers.get("content-length") else {}
    requested_project = str(body.get("project") or "both").lower()
    session_override = body.get("session")
    results = []

    if requested_project in {"both", "project220", "p220", "project1"}:
        from project1 import project1 as p220_module

        results.append(save_session_snapshot(
            "project220",
            p220_module.latest_project1_data,
            p220_module.project1_session_started_at,
            source="manual",
            session_override=session_override,
        ))

    if requested_project in {"both", "project10", "p10", "project2"}:
        from project2 import project2 as p10_module

        results.append(save_session_snapshot(
            "project10",
            p10_module.latest_project2_data,
            p10_module.project2_session_started_at,
            source="manual",
            session_override=session_override,
        ))

    if not results:
        raise HTTPException(status_code=400, detail="Invalid project")

    return {"status": "success", "results": results}


@HistoryRouter.post("/history/snapshot")
async def snapshot_payload(req: Request):
    body = await req.json()
    requested_project = normalize_project(body.get("project"))
    data = body.get("data") or {}
    result = save_session_snapshot(
        requested_project,
        data,
        body.get("session_started_at"),
        source=body.get("source") or "import",
        session_override=body.get("session"),
    )
    return {"status": "success", "result": result}

