from json import JSONDecodeError, loads

from fastapi import HTTPException


MAX_JSON_BYTES = 2 * 1024 * 1024


async def json_object(request, allow_empty=False):
    length = request.headers.get("content-length")
    if length is not None:
        try:
            size = int(length)
            if size < 0:
                raise ValueError
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid request body length") from None
        if size > MAX_JSON_BYTES:
            raise HTTPException(status_code=413, detail="Request body is too large")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_JSON_BYTES:
            raise HTTPException(status_code=413, detail="Request body is too large")
        raw.extend(chunk)
    if not raw and allow_empty:
        return {}
    try:
        data = loads(raw)
    except (JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object") from None
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    return data
