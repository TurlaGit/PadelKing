"""Время в поясе Бали (WITA, UTC+8, без перехода на летнее время)."""
from datetime import date as _date
from datetime import datetime, time, timedelta, timezone

WITA = timezone(timedelta(hours=8))
PAYMENT_LEAD_HOURS = 24  # дедлайн оплаты = старт − 24ч


def now_wita() -> datetime:
    return datetime.now(WITA)


def parse_date(s: str, today: _date | None = None) -> _date | None:
    """'12.03' или '12.03.2026' → date. Без года: ближайший будущий."""
    s = (s or "").strip().replace("/", ".").replace("-", ".")
    parts = [p for p in s.split(".") if p != ""]
    today = today or now_wita().date()
    try:
        if len(parts) == 2:
            d, m = int(parts[0]), int(parts[1])
            year = today.year
            cand = _date(year, m, d)
            if cand < today:
                cand = _date(year + 1, m, d)
            return cand
        if len(parts) == 3:
            d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
            if y < 100:
                y += 2000
            return _date(y, m, d)
    except ValueError:
        return None
    return None


def parse_time(s: str) -> time | None:
    """'19:00' или '19' → time."""
    s = (s or "").strip().replace(".", ":").replace("-", ":")
    if not s:
        return None
    parts = s.split(":")
    try:
        h = int(parts[0])
        mn = int(parts[1]) if len(parts) > 1 and parts[1] != "" else 0
        if 0 <= h < 24 and 0 <= mn < 60:
            return time(h, mn)
    except ValueError:
        return None
    return None


def parse_time_range(s: str) -> tuple[str | None, str | None]:
    """'19:00-21:00' / '19:00 21:00' / '19:00' → ('19:00', '21:00'|None)."""
    s = (s or "").strip()
    for sep in ("-", "–", "—", " "):
        if sep in s:
            a, _, b = s.partition(sep)
            ta, tb = parse_time(a), parse_time(b)
            return (ta.strftime("%H:%M") if ta else None,
                    tb.strftime("%H:%M") if tb else None)
    ta = parse_time(s)
    return (ta.strftime("%H:%M") if ta else None, None)


def make_start_at(d: _date, time_start: str | None) -> datetime:
    t = parse_time(time_start or "") or time(0, 0)
    return datetime.combine(d, t, tzinfo=WITA)


def payment_deadline(start_at: datetime) -> datetime:
    return start_at - timedelta(hours=PAYMENT_LEAD_HOURS)


def to_iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def from_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None
