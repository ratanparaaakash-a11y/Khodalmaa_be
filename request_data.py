from json import JSONDecodeError

from fastapi import HTTPException


async def json_object(request, allow_empty=False):
    raw = await request.body()
    if not raw and allow_empty:
        return {}
    try:
        data = await request.json()
    except (JSONDecodeError, UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object") from None
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    return data
