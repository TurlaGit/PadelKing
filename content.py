import re
from pathlib import Path

import yaml

CONTENT_PATH = Path(__file__).parent / "content.md"

_DEFAULT_TEXTS = {
    "welcome": "Привет!",
    "no_tournaments": "Сейчас нет активных турниров.",
    "not_found": "Турнир не найден.",
    "list_header": "Ближайшие турниры:",
    "my_regs_empty": "У тебя нет активных записей.",
    "my_regs_header": "Твои записи:",
    "admin_contact": "Свяжитесь с админом.",
    "already_registered": "Ты уже записан.",
    "joined": "Ты записан!",
    "waitlist": "Ты в листе ожидания.",
    "cancelled": "Запись отменена.",
    "promoted": "Тебя перевели из листа ожидания в основной состав ({title}).",
    "payment": "",
    "announce_button": "Записаться",
}


def _parse(text: str) -> dict:
    m = re.search(r"```ya?ml\s*\n(.*?)\n```", text, re.DOTALL)
    payload = m.group(1) if m else text
    data = yaml.safe_load(payload) or {}
    if not isinstance(data, dict):
        raise ValueError("content.md: ожидался YAML-объект на верхнем уровне")
    return data


def load_content() -> dict:
    raw = CONTENT_PATH.read_text(encoding="utf-8")
    data = _parse(raw)
    texts = {**_DEFAULT_TEXTS, **(data.get("texts") or {})}
    tournaments = data.get("tournaments") or []
    if not isinstance(tournaments, list):
        raise ValueError("content.md: tournaments должен быть списком")
    return {"texts": texts, "tournaments": tournaments}
