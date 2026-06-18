import html
import json
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
    s = str(date_str).strip()
    # Поддержка как 'YYYY-MM-DD', так и ISO-datetime 'YYYY-MM-DDTHH:MM...'
    try:
        return _date.fromisoformat(s[:10])
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
    date_src = t.get("date") or t.get("start_at")
    ctx = {
        "title": t.get("title", ""),
        "date": short_date(date_src),
        "weekday": weekday_ru(date_src),
        "time": (t.get("time_start") or t.get("time") or ""),
        "price": _price_text(t),
        "location": t.get("location", "") or "",
    }
    try:
        return template.format_map(_SafeDict(ctx))
    except Exception:
        return template


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _at(username) -> str:
    return f" @{esc(username)}" if username else ""


def _levels_text(t: dict) -> str:
    items = _levels_list(t)
    return ", ".join(items)


def _levels_list(t: dict) -> list[str]:
    raw = t.get("levels")
    if not raw:
        return []
    try:
        items = json.loads(raw) if isinstance(raw, str) else list(raw)
    except (ValueError, TypeError):
        return [str(raw)]
    return [str(x) for x in items]


def level_emoji(t: dict) -> str:
    """Эмодзи по уровню турнира: берём высший из выбранных."""
    levels = " ".join(_levels_list(t)).lower()
    if "gold" in levels:
        return "🥇"
    if "silver" in levels:
        return "🩶"
    if "bronze" in levels:
        return "🥉"
    if "beginner" in levels:
        return "🟢"
    return "🏆"


def _price_text(t: dict) -> str:
    if t.get("price_amount") is not None and t.get("currency"):
        amount = t["price_amount"]
        if float(amount).is_integer():
            # 350000 → "350.000" (формат, привычный для Бали)
            amount = f"{int(amount):,}".replace(",", ".")
        return f"{amount} {t['currency']}"
    return str(t.get("price") or "")


def reg_line(reg: dict) -> str:
    """Строка пары/одиночки в составе."""
    a = esc(reg.get("player_name") or "—") + _at(reg.get("player_username"))
    if reg.get("status") == "looking":
        return f"{a} + 🔍 ищет партнёра"
    if reg.get("partner_user_id"):
        b = esc(reg.get("partner_name") or "—") + _at(reg.get("partner_username"))
        return f"{a} + {b}"
    if reg.get("partner_username"):
        return f"{a} + @{esc(reg['partner_username'])} ⏳"
    return a


def tournament_header(t: dict, location: dict | None = None) -> str:
    lines = [f"{level_emoji(t)} <b>{esc(t.get('title'))}</b>"]
    fmt = t.get("format_type")
    if fmt:
        lines.append(f"Формат: <b>{esc(fmt)}</b>")
    lines.append("")

    date = t.get("date") or short_date(t.get("start_at"))
    time = " – ".join(filter(None, [t.get("time_start"), t.get("time_end")])) or t.get("time")
    when = " ".join(filter(None, [esc(date), esc(time)])).strip()
    if when:
        lines.append(f"📅 {when}")

    loc_title = (location or {}).get("title") or t.get("location")
    loc_url = (location or {}).get("maps_url")
    if loc_title:
        lines.append(f"📍 {esc(loc_title)}")
    if loc_url:
        lines.append(f"🔗 {esc(loc_url)}")

    game_format = t.get("game_format")
    if game_format:
        lines.append("")
        lines.append("🎮 <b>Формат игры:</b>")
        lines.append(esc(game_format).strip())

    levels = _levels_text(t)
    if levels:
        lines.append(f"🎯 Уровень: {esc(levels)}")

    price = _price_text(t)
    if price:
        lines.append(f"💰 Стоимость: {esc(price)}")

    if t.get("min_pairs") and t.get("max_pairs"):
        lines.append(f"👥 Участников: {t['min_pairs']*2}–{t['max_pairs']*2}")
    elif t.get("max_pairs"):
        lines.append(f"👥 Максимум участников: {t['max_pairs']*2}")

    note = t.get("extra_note") or t.get("description")
    if note:
        lines.append("")
        lines.append(esc(note).strip())

    lines.append("")
    lines.append("‼️ <i>Участие подтверждается после оплаты</i>")
    return "\n".join(lines)


# Сколько пар максимум показываем в блоке (защита от лимита 4096 Telegram).
_MAX_SHOWN = 30


def _list_block(regs: list[dict]) -> list[str]:
    lines = []
    for i, r in enumerate(regs[:_MAX_SHOWN], 1):
        lines.append(f"{i}. {reg_line(r)}")
    extra = len(regs) - _MAX_SHOWN
    if extra > 0:
        lines.append(f"<i>…и ещё {extra}</i>")
    return lines


def render_tournament(
    t: dict,
    location: dict | None,
    main_regs: list[dict],
    waitlist_regs: list[dict],
) -> str:
    lines = [tournament_header(t, location), ""]

    cap = t.get("max_pairs")
    cap_str = f"/{cap}" if cap else ""
    lines.append(f"<b>Участники ({len(main_regs)}{cap_str} пар):</b>")
    if main_regs:
        lines += _list_block(main_regs)
    else:
        lines.append("<i>— пока никого нет</i>")

    if waitlist_regs:
        lines.append("")
        lines.append(f"<b>📋 Лист ожидания ({len(waitlist_regs)}):</b>")
        lines += _list_block(waitlist_regs)

    return "\n".join(lines)
