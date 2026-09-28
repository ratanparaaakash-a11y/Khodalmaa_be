from fastapi import Request, APIRouter, Depends, HTTPException
import httpx
import os
from request_data import json_object
from security import require_user

TelegramRouter = APIRouter(prefix="/api/v1")


bot_token = os.getenv("BOT_TOKEN")
chat_id = os.getenv("CHAT_ID")

@TelegramRouter.post("/send-alert", dependencies=[Depends(require_user)])
async def send_telegram_alert(req: Request):
    data = await json_object(req)
    message = data.get("message")
    if not isinstance(message, str):
        raise HTTPException(status_code=400, detail="message must be text")
    telegram_text = "\n".join(
        line.strip() for line in message.replace("\r\n", "\n").split("\n") if line.strip()
    )
    if not telegram_text:
        raise HTTPException(status_code=400, detail="message is required")
    if len(telegram_text) > 4096:
        raise HTTPException(status_code=400, detail="message exceeds Telegram's 4096 character limit")
    if not bot_token or not chat_id:
        raise HTTPException(status_code=503, detail="Telegram delivery is not configured")
    try:
        telegram_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        timeout = httpx.Timeout(10.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(telegram_url, json={"chat_id": chat_id, "text": telegram_text})
        result = response.json()
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Telegram delivery was not confirmed before timeout") from None
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=502, detail="Telegram delivery could not be confirmed") from None
    if not response.is_success or not isinstance(result, dict) or result.get("ok") is not True:
        # Upstream error bodies/URLs are deliberately not echoed to callers.
        raise HTTPException(status_code=502, detail="Telegram rejected the message")
    return {"telegram_response": result}

