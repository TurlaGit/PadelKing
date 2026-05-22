import os
from dotenv import load_dotenv

load_dotenv()


def _int_or_none(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "PadelFirst_bot").strip().lstrip("@")

ADMIN_CHAT_ID = _int_or_none("ADMIN_CHAT_ID") or 212659753
GROUP_CHAT_ID = _int_or_none("GROUP_CHAT_ID")

DB_PATH = os.getenv("DB_PATH", "padel.db")

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()

PORT = _int_or_none("PORT")


def validate() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в .env")
