from fastapi import  Request,APIRouter,HTTPException
import httpx
import os

TelegramRouter = APIRouter(prefix="/api/v1")


bot_token = os.getenv("BOT_TOKEN")
chat_id = os.getenv("CHAT_ID")  # e.g., 123456789

@TelegramRouter.post("/send-alert")
async def send_telegram_alert(req: Request):
    try:
        print("\nIncoming request to /send-alert")

        data = await req.json()
        raw_message = str(data.get("message", "")).strip()
        telegram_text = "\n".join(
            line.strip()
            for line in raw_message.replace("\r\n", "\n").split("\n")
            if line.strip()
        )
        if not telegram_text:
            raise HTTPException(status_code=400, detail="message is required")

        print(f"Sending Telegram message with {len(telegram_text.splitlines())} line(s)")

        telegram_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": telegram_text,
        }

        timeout = httpx.Timeout(10.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(telegram_url, json=payload)
            print("Telegram response:", response.status_code)

            return {
                "telegram_response": response.json(),
            }
            

    except Exception as e:
        print("Error:", str(e))
        raise HTTPException(status_code=500, detail=str(e))

