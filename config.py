import os

SHEET_NAME = "POS系統資料庫"
CREDENTIALS_FILE = "credentials.json"
APP_PASSWORD = os.environ.get("POS_APP_PASSWORD", "1234").strip()
SECRET_KEY = os.environ.get("POS_SECRET_KEY", "pos-system-change-me")
