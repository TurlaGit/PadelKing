import html
from datetime import date as _date

_WEEKDAYS_RU = {
    0: "понедельник", 1: "вторник", 2: "среда", 3: "четверг",
    4: "пятница", 5: "суббота", 6: "воскресенье",
}


def esc(value) -> str:
    if value is None:
        return ""
    return html.escape(str(value))


def _parse_date(date_str):
    if not date_str:
        return None
    try:
        return _date.fromisoformat(str(date_str).strip())
    except ValueError:
        return None


def weekday_ru(date_str) -> str:
    d = _parse_date(date_str)
    return _WEEKDAYS_RU[d.weekday()] if d else ""


def short_date(date_str) -> str:
    d = _parse_date(date_str)
    return d.strftime("%d.%m") if d else (str(date_str) if date_str else "")


def render_payment(template: str, t: dict) -> str:
    """Подставляет {title}, {date}, {weekday}, {time}, {price}, {location} в шаблон.
    Если в шаблоне нет фигурных скобок — возвращает как есть."""
    if not template or "{" not in template:
        return template or ""
    ctx = {
        "title": t.get("title", ""),
        "date": short_date(t.get("date")),
        "weekday": weekday_ru(t.get("date")),
        "time": t.get("time", "") or "",
        "price": t.get("price", "") or "",
        "location": t.get("location", "") or "",
    }
    try:
        return template.format_map(_SafeDict(ctx))
    except Exception:
        return template


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def participant_line(p: dict) -> str:
    name = esc(p.get("full_name") or "—")
    uname = f" @{esc(p['username'])}" if p.get("username") else ""
    return f"{name}{uname}"


def format_tournament(t: dict, participants: list[dict] | None = None) -> str:
    participants = participants or []
    active = [p for p in participants if p["status"] == "active"]
    waitlist = [p for p in participants if p["status"] == "waitlist"]

    lines = [f"🏆 <b>{esc(t['title'])}</b>", ""]
    if t.get("date") or t.get("time"):
        lines.append(f"📅 {esc(t.get('date',''))} {esc(t.get('time',''))}".rstrip())
    if t.get("location"):
        lines.append(f"📍 {esc(t['location'])}")
    if t.get("price"):
        lines.append(f"💰 {esc(t['price'])}")
    lines.append("")
    if t.get("description"):
        lines.append(t["description"].strip())
        lines.append("")

    cap = int(t.get("max_participants") or 8)
    lines.append(f"<b>Участники ({len(active)}/{cap}):</b>")
    if active:
        for i, p in enumerate(active, 1):
            lines.append(f"{i}. {participant_line(p)}")
    else:
        lines.append("<i>— пока никого нет</i>")

    if waitlist:
        lines.append("")
        lines.append(f"<b>Лист ожидания ({len(waitlist)}):</b>")
        for i, p in enumerate(waitlist, 1):
            lines.append(f"{i}. {participant_line(p)}")

    return "\n".join(lines)
