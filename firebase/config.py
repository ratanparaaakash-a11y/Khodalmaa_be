from firebase_admin import credentials, initialize_app, auth
from constant import service_account_key

cred = credentials.Certificate(service_account_key)
firebase_app = initialize_app(cred)

