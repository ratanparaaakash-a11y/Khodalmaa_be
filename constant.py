from dotenv import load_dotenv
from firebase.service_account import load_service_account

load_dotenv()

service_account_key = load_service_account()
