"""Firebase authentication for browser actions; machine ingestion is separate."""

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
import time
from functools import lru_cache
from typing import Optional
from urllib.parse import urlsplit

from fastapi import Depends, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware


def verify_browser_token(token):
    from firebase.config import auth
    return auth.verify_id_token(token, check_revoked=True)


async def require_user(authorization: Optional[str] = Header(None)):
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Sign in is required",
                            headers={"WWW-Authenticate": "Bearer"})
    return await authenticate_browser_token(token.strip())


async def authenticate_browser_token(token):
    try:
        claims = await asyncio.wait_for(asyncio.to_thread(verify_browser_token, token), timeout=10)
    except Exception as error:
        # Never log or echo SDK exceptions: they can include supplied credentials.
        from firebase_admin import auth
        if isinstance(error, (auth.InvalidIdTokenError, auth.ExpiredIdTokenError,
                              auth.RevokedIdTokenError, auth.UserDisabledError,
                              auth.UserNotFoundError, ValueError)):
            raise HTTPException(status_code=401, detail="Sign in again to continue",
                                headers={"WWW-Authenticate": "Bearer"}) from None
        raise HTTPException(status_code=503, detail="Sign-in verification is temporarily unavailable") from None
    if not isinstance(claims, dict) or not claims.get("uid"):
        raise HTTPException(status_code=401, detail="Invalid sign-in token",
                            headers={"WWW-Authenticate": "Bearer"})
    return claims


async def require_admin(claims=Depends(require_user)):
    if claims.get("admin") is not True:
        raise HTTPException(status_code=403, detail="Administrator access is required")
    return claims


def browser_origins():
    origins = ["https://khodalmaa.in", "https://www.khodalmaa.in"]
    for value in os.getenv("EXTRA_BROWSER_ORIGINS", "").split(","):
        value = value.strip()
        if not value:
            continue
        parsed = urlsplit(value)
        if (parsed.scheme not in {"https", "http"} or not parsed.netloc
                or parsed.username or parsed.password or parsed.path
                or parsed.query or parsed.fragment or "*" in value):
            raise ValueError("EXTRA_BROWSER_ORIGINS must contain exact HTTP origins")
        origins.append(value)
    return list(dict.fromkeys(origins))


class MachineCompatibleCORSMiddleware:
    """Keep unknown existing producer clients working only during legacy rollout."""
    machine_paths = {"/api/v1/project1_data", "/api/v1/project2_data"}

    def __init__(self, app):
        self.restricted = CORSMiddleware(app, allow_origins=browser_origins(), allow_credentials=False,
                                         allow_methods=["*"], allow_headers=["*"])
        self.legacy = CORSMiddleware(app, allow_origins=["*"], allow_credentials=False,
                                    allow_methods=["POST"], allow_headers=["*"])

    async def __call__(self, scope, receive, send):
        use_legacy = False
        if (scope["type"] == "http" and scope.get("path") in self.machine_paths
                and scope.get("method") in {"POST", "OPTIONS"}):
            try:
                mode, _ = machine_security_status()
                use_legacy = mode == "legacy"
            except HTTPException:
                pass  # Invalid credential configuration must not relax origins.
        await (self.legacy if use_legacy else self.restricted)(scope, receive, send)


async def _close_websocket(websocket, code):
    try:
        await websocket.close(code=code)
    except Exception:
        pass  # The peer may have disconnected while token verification ran.


async def authenticate_websocket(websocket):
    """Accept only to receive an auth frame; never publish data before verification."""
    origin = websocket.headers.get("origin")
    if origin and origin not in browser_origins():
        await _close_websocket(websocket, 4403)
        return None
    await websocket.accept()
    try:
        async def receive_auth():
            raw = await websocket.receive_text()
            if len(raw.encode("utf-8")) > 16384:
                raise ValueError("Authentication frame is too large")
            frame = json.loads(raw)
            if (not isinstance(frame, dict) or frame.get("type") != "authenticate"
                    or not isinstance(frame.get("token"), str) or not frame["token"].strip()):
                raise ValueError("Authentication frame is required")
            claims = await authenticate_browser_token(frame["token"].strip())
            expires = claims.get("exp")
            if (isinstance(expires, bool) or not isinstance(expires, (int, float))
                    or not math.isfinite(expires) or expires <= time.time()):
                raise ValueError("WebSocket sign-in expired")
            return claims
        return await asyncio.wait_for(receive_auth(), timeout=10)
    except HTTPException as error:
        await _close_websocket(websocket, 1013 if error.status_code == 503 else 4401)
    except Exception:
        await _close_websocket(websocket, 4401)
    return None


async def receive_authenticated_text(websocket, claims):
    # Reconnect with a fresh token when this verified credential expires.
    expires_at = claims.get("exp", time.time() + 3600)
    remaining = max(0, min(3600, float(expires_at) - time.time()))
    if remaining <= 0:
        await _close_websocket(websocket, 4401)
        raise ValueError("WebSocket sign-in expired")
    try:
        return await asyncio.wait_for(websocket.receive_text(), timeout=remaining)
    except asyncio.TimeoutError:
        await _close_websocket(websocket, 4401)
        raise ValueError("WebSocket sign-in expired") from None


@lru_cache(maxsize=8)
def _machine_configuration(mode, raw):
    if mode not in {"legacy", "required"}:
        raise ValueError("Invalid machine authentication mode")
    entries = json.loads(raw)
    if not isinstance(entries, list):
        raise ValueError("Machine credentials must be a list")
    credentials = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid machine credential")
        identity, digest = entry.get("id"), entry.get("sha256")
        projects, machines = entry.get("projects"), entry.get("machines")
        if (not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identity)
                or identity in credentials or not isinstance(digest, str)
                or not re.fullmatch(r"[a-fA-F0-9]{64}", digest)
                or not isinstance(projects, list) or not projects
                or any(p not in {"project10", "project220"} for p in projects)
                or not isinstance(machines, list) or not machines
                or any(not isinstance(m, str) or not re.fullmatch(r"machine\d+", m) for m in machines)):
            raise ValueError("Invalid machine credential scope")
        credentials[identity] = {"sha256": digest.lower(), "projects": set(projects), "machines": set(machines)}
    if mode == "required" and not credentials:
        raise ValueError("Required machine authentication needs credentials")
    return mode, credentials


def machine_security_status():
    try:
        mode, credentials = _machine_configuration(
            os.getenv("MACHINE_INGESTION_MODE", "legacy").strip().lower(),
            os.getenv("MACHINE_INGESTION_CREDENTIALS_JSON", "[]"))
    except (ValueError, TypeError):
        raise HTTPException(status_code=503, detail="Machine authentication configuration is unavailable") from None
    return mode, credentials


_legacy_log_at = {}
_legacy_names = {}


def authorize_machine_request(request, project, data):
    """Stage credentials without stopping existing producers in explicit legacy mode."""
    mode, credentials = machine_security_status()
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "machine":
        if mode == "required":
            raise HTTPException(status_code=401, detail="Machine authentication is required")
        now = time.monotonic()
        names = {name.lower() for name in data if isinstance(name, str)
                 and re.fullmatch(r"machine\d{1,9}", name, re.I)}
        _legacy_names[project] = set(sorted(_legacy_names.get(project, set()) | names)[:64])
        if now - _legacy_log_at.get(project, -300) >= 300:
            _legacy_log_at[project] = now
            logging.getLogger(__name__).warning(
                "Legacy unauthenticated machine requests: project=%s machines=%s", project,
                ",".join(sorted(_legacy_names[project])) or "unrecognized")
            _legacy_names[project].clear()
        return {"authenticated": False, "mode": "legacy"}
    identity, _, secret = supplied.strip().partition(".")
    credential = credentials.get(identity)
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    expected = credential["sha256"] if credential else "0" * 64
    matched = hmac.compare_digest(digest, expected)
    if not credential or not matched or not 32 <= len(secret) <= 256:
        raise HTTPException(status_code=401, detail="Invalid machine credential")
    names = {name.lower() for name in data if isinstance(name, str) and not name.startswith("__")}
    if project not in credential["projects"] or not names or not names.issubset(credential["machines"]):
        raise HTTPException(status_code=403, detail="Machine credential does not allow this data")
    return {"authenticated": True, "mode": mode, "credential_id": identity}
