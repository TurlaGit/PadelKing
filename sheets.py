import asyncio
import json
import logging
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

from config import GOOGLE_CREDENTIALS_JSON, GOOGLE_CREDENTIALS_PATH, GOOGLE_SHEET_ID

log = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_HEADERS = [
    "Время записи (UTC)",
    "Турнир ID",
    "Турнир",
    "User ID",
    "Username",
    "Имя",
    "Статус",
]

_client = None
_worksheet = None


def _build_client():
    if GOOGLE_CREDENTIALS_JSON:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(info, scopes=_SCOPES)
    else:
        creds = Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS_PATH, scopes=_SCOPES
        )
    return gspread.authorize(creds)


def _get_worksheet():
    global _client, _worksheet
    if _worksheet is not None:
        return _worksheet
    if not GOOGLE_SHEET_ID:
        return None
    if _client is None:
        _client = _build_client()
    sh = _client.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet("Registrations")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Registrations", rows=2000, cols=len(_HEADERS))
        ws.append_row(_HEADERS)
    _worksheet = ws
    return ws


def _append_row_sync(row: list) -> None:
    ws = _get_worksheet()
    if ws is None:
        return
    ws.append_row(row, value_input_option="USER_ENTERED")


async def append_registration(
    tournament_id: str,
    tournament_title: str,
    user_id: int,
    username: str,
    full_name: str,
    status: str,
) -> None:
    if not GOOGLE_SHEET_ID:
        return
    row = [
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        tournament_id,
        tournament_title,
        user_id,
        f"@{username}" if username else "",
        full_name,
        status,
    ]
    try:
        await asyncio.to_thread(_append_row_sync, row)
    except Exception as e:
        log.warning("Не получилось записать в Google Sheets: %s", e)
