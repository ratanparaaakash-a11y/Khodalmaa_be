"""Firebase authentication for browser actions; machine ingestion is separate."""

import asyncio
from typing import Optional

from fastapi import Depends, Header, HTTPException


def verify_browser_token(token):
    from firebase.config import auth
    return auth.verify_id_token(token, check_revoked=True)


async def require_user(authorization: Optional[str] = Header(None)):
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Sign in is required",
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = await asyncio.to_thread(verify_browser_token, token.strip())
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
