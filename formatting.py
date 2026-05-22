import html


def esc(value) -> str:
    if value is None:
        return ""
    return html.escape(str(value))


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
