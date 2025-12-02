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
        raw_message = data.get("message", "")
        print("Raw message:", raw_message)

        # Convert CSV string to list
        numbers = [n.strip() for n in raw_message.split(",") if n.strip()]
        print("Parsed numbers:", numbers)

        total = len(numbers)
        half = total // 2

        group1 = numbers[:half]
        group2 = numbers[half:]

        print("Group 1:", group1)
        print("Group 2:", group2)

        # Build ASCII table block
        def build_table(group):
            if not group:
                return ""

            max_width = max(len(item) for item in group)
            col_width = max(max_width, 5)

            def border():
                return "+" + "+".join(["-" * (col_width + 2)] * 4) + "+"

            rows = [border()]
            for i in range(0, len(group), 4):
                row_items = group[i:i + 4]
                while len(row_items) < 4:
                    row_items.append("")

                row = "|" + "|".join(f" {item.center(col_width)} " for item in row_items) + "|"
                rows.append(row)
                rows.append(border())

            table_output = "\n".join(rows)
            print("Table created:\n" + table_output)
            return table_output

        table1 = build_table(group1)
        table2 = build_table(group2)

        formatted_message = f"<pre>{table1}\n\n{table2}</pre>"

        print("Final formatted message:\n", formatted_message)

        telegram_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": table1,
            "parse_mode": "HTML"
        }
        payload2 = {
            "chat_id": chat_id,
            "text": table2,
            "parse_mode": "HTML"
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(telegram_url, json=payload)
            print("Telegram response:", response.json())
            response2 = await client.post(telegram_url, json=payload2)
            print("Telegram response:", response2.json())

            return {
                "telegram_response_for_msg1": response.json(),
                "telegram_response_for_msg2": response2.json(),                
            }
            

    except Exception as e:
        print("Error:", str(e))
        raise HTTPException(status_code=500, detail=str(e))

