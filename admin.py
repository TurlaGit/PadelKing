"""Админ-панель PadelKing — всё через inline-кнопки.

Главное меню (/admin):
  ➕ Создать турнир
  📋 Список турниров → ветка по каждому турниру (состав, оплаты, итоги,
     добавить пару, закрыть/открыть, перенести, отменить)
  📊 Аналитика → топ-10, выручка за период, число турниров, уник. игроков
  🏟 Локации (справочник)
  👤 Режим игрока (UI-переключение)

Доступ только для ADMIN_IDS. Фильтр на роутере + проверка в каждом хэндлере.
"""
import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import db
from announcement import refresh_announcement
from config import ADMIN_IDS
from formatting import esc, level_emoji
from states import AddPair, LocationEdit, Reschedule
from timeutils import now_wita, to_iso

log = logging.getLogger(__name__)
router = Router()
# defense-in-depth: весь админ-роутер только для ADMIN_IDS
router.message.filter(F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

_ICON = {"paid": "✅", "paid_manual": "✅", "pending_review": "🧾",
         "rejected": "❌", "unpaid": "⏳"}
_RU = {"paid": "оплачено", "paid_manual": "оплачено (вручную)",
       "pending_review": "на проверке", "rejected": "отклонено", "unpaid": "не оплачено"}


def _is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def _tournaments_kb(tournaments: list[dict], prefix: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=t["title"][:60], callback_data=f"{prefix}:{t['id']}")]
        for t in tournaments
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _roster(tid: str):
    t = await db.get_tournament(tid)
    main = await db.get_main_registrations(tid)
    lines = [f"💳 <b>Оплаты — {esc(t['title'])}</b>", ""]
    kb: list[list[InlineKeyboardButton]] = []
    if not main:
        lines.append("— пока никого нет")
    if len(main) > 40:
        lines.append(f"<i>(показаны первые 40 из {len(main)})</i>")
        main = main[:40]
    for i, reg in enumerate(main, 1):
        a = esc(reg.get("player_name") or "—")
        if reg["status"] == "looking":
            lines.append(f"{i}. {a} + 🔍 ищет партнёра")
            continue
        b = esc(reg.get("partner_name") or "—")
        pays = {p["who"]: p for p in await db.get_pair_payments(reg["id"])}

        def cell(who):
            p = pays.get(who)
            if not p:
                return "—"
            return f"{_ICON.get(p['status'], '?')} {_RU.get(p['status'], p['status'])}"

        lines.append(f"{i}. {a} + {b}")
        lines.append(f"   • {a}: {cell('player')}\n   • {b}: {cell('partner')}")
        kb.append([InlineKeyboardButton(
            text=f"💳 {i}. {reg.get('player_name')} + {reg.get('partner_name')}"[:60],
            callback_data=f"apm:{reg['id']}",
        )])
    kb.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"apay:{tid}")])
    kb.append([InlineKeyboardButton(text="⬅️ К турниру", callback_data=f"admtour:{tid}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


@router.callback_query(F.data.startswith("apunpaid:"))
async def show_unpaid(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    tid = cb.data.split(":", 1)[1]
    t = await db.get_tournament(tid)
    if not t:
        await cb.answer("Не найдено.", show_alert=True)
        return
    main = await db.get_main_registrations(tid)
    rows = []
    for reg in main:
        if reg["status"] == "looking":
            continue
        pays = await db.get_pair_payments(reg["id"])
        for p in pays:
            if p["status"] in ("paid", "paid_manual"):
                continue
            name = (reg["player_name"] if p["who"] == "player"
                    else reg["partner_name"]) or "—"
            uname = (reg["player_username"] if p["who"] == "player"
                     else reg["partner_username"])
            mark = {"unpaid": "⏳", "pending_review": "🧾", "rejected": "❌"}.get(p["status"], "•")
            line = f"{mark} {esc(name)}" + (f" @{esc(uname)}" if uname else "")
            if reg["is_waitlist"]:
                line += " <i>(лист ожидания)</i>"
            rows.append(line)
    text = f"📋 <b>Не оплатили — {esc(t['title'])}</b>\n\n" + (
        "\n".join(rows) if rows else "<i>Все оплатили 🎉</i>"
    )
    back = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К турниру", callback_data=f"admtour:{tid}")]
    ])
    await _safe_edit(cb.message, text, back)
    await cb.answer()


async def _safe_edit(message: Message, text: str, kb) -> None:
    try:
        await message.edit_text(text, reply_markup=kb)
    except Exception:
        await message.answer(text, reply_markup=kb)


# ---------- /admin меню ----------

def _admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать турнир", callback_data="tw:start")],
        [InlineKeyboardButton(text="📋 Список турниров", callback_data="adm:tours")],
        [InlineKeyboardButton(text="📊 Аналитика", callback_data="adm:analytics")],
        [InlineKeyboardButton(text="🏟 Локации", callback_data="adm:locations")],
        [InlineKeyboardButton(text="👤 Режим игрока", callback_data="adm:playermode")],
    ])


def _admin_greeting(user) -> str:
    name = esc(user.first_name or "администратор")
    return (f"Привет, <b>{name}</b>! 👋\n"
            f"Вы вошли как <b>администратор</b>. Что нужно?")


from aiogram.filters import Command  # noqa: E402


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(_admin_greeting(message.from_user), reply_markup=_admin_menu_kb())


@router.callback_query(F.data == "adm:menu")
async def adm_menu(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    await state.clear()
    await _safe_edit(cb.message, _admin_greeting(cb.from_user), _admin_menu_kb())
    await cb.answer()


# ---------- 📋 Список турниров → ветка по каждому ----------

@router.callback_query(F.data == "adm:tours")
async def adm_tours(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    ts = await db.list_active_tournaments()
    rows = []
    if ts:
        for t in ts:
            label = f"{level_emoji(t)} {t['title']}"
            when = " ".join(filter(None, [t.get("date"), t.get("time_start") or t.get("time")])).strip()
            if when:
                label += f" · {when}"
            if t.get("is_closed"):
                label += " · 🔒"
            rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"admtour:{t['id']}")])
        text = "📋 <b>Список турниров</b>\nКликни на турнир для управления:"
    else:
        text = "Активных турниров нет. Создай первый!"
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="adm:menu")])
    await _safe_edit(cb.message, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


async def _tournament_card_text(t: dict) -> str:
    when = " ".join(filter(None, [t.get("date"), t.get("time_start") or t.get("time")])).strip()
    main = await db.get_main_registrations(t["id"])
    wl = await db.get_waitlist_registrations(t["id"])
    fin = await db.tournament_financials(t["id"])
    closed = "🔒 запись закрыта" if t.get("is_closed") else "🔓 запись открыта"
    lines = [
        f"{level_emoji(t)} <b>{esc(t['title'])}</b>",
        f"📅 {esc(when) if when else '—'}",
        f"{closed}",
        "",
        f"👥 Пар в составе: <b>{len(main)}</b>"
        + (f" / {t['max_pairs']}" if t.get("max_pairs") else ""),
        f"📋 Лист ожидания: <b>{len(wl)}</b>",
        f"💰 Собрано: <b>{fin['paid']}/{fin['total']} игроков</b>",
    ]
    return "\n".join(lines)


def _tournament_branch_kb(t: dict) -> InlineKeyboardMarkup:
    tid = t["id"]
    toggle = "🔓 Открыть запись" if t.get("is_closed") else "🔒 Закрыть запись"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Состав", callback_data=f"admroster:{tid}")],
        [InlineKeyboardButton(text="💳 Оплаты (карточки пар)", callback_data=f"apay:{tid}")],
        [InlineKeyboardButton(text="📋 Кто не оплатил", callback_data=f"apunpaid:{tid}")],
        [InlineKeyboardButton(text="💰 Итоги (выручка)", callback_data=f"ares:{tid}")],
        [InlineKeyboardButton(text="🗂 Снятые пары", callback_data=f"apcanc:{tid}")],
        [InlineKeyboardButton(text="➕ Добавить пару вручную", callback_data=f"aap:{tid}")],
        [InlineKeyboardButton(text=toggle, callback_data=f"aclose:{tid}")],
        [InlineKeyboardButton(text="🗓 Перенести турнир", callback_data=f"ares2:{tid}")],
        [InlineKeyboardButton(text="🗑 Отменить турнир", callback_data=f"actour:{tid}")],
        [InlineKeyboardButton(text="⬅️ К списку турниров", callback_data="adm:tours")],
    ])


@router.callback_query(F.data.startswith("admtour:"))
async def adm_tour_branch(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    tid = cb.data.split(":", 1)[1]
    t = await db.get_tournament(tid)
    if not t:
        await cb.answer("Не найдено.", show_alert=True)
        return
    await _safe_edit(cb.message, await _tournament_card_text(t), _tournament_branch_kb(t))
    await cb.answer()


# Компактный «Состав» (без кнопок оплат) — для быстрого просмотра
@router.callback_query(F.data.startswith("admroster:"))
async def adm_roster_view(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    tid = cb.data.split(":", 1)[1]
    t = await db.get_tournament(tid)
    if not t:
        await cb.answer("Не найдено.", show_alert=True)
        return
    main = await db.get_main_registrations(tid)
    wl = await db.get_waitlist_registrations(tid)
    lines = [f"👥 <b>Состав — {esc(t['title'])}</b>", ""]
    if not main:
        lines.append("<i>— пока никого нет</i>")
    for i, reg in enumerate(main, 1):
        a = esc(reg.get("player_name") or "—")
        if reg["status"] == "looking":
            lines.append(f"{i}. {a} + 🔍 ищет партнёра")
        else:
            b = esc(reg.get("partner_name") or "—")
            lines.append(f"{i}. {a} + {b}")
    if wl:
        lines.append("")
        lines.append(f"<b>📋 Лист ожидания ({len(wl)}):</b>")
        for i, reg in enumerate(wl, 1):
            a = esc(reg.get("player_name") or "—")
            b = esc(reg.get("partner_name") or "🔍")
            lines.append(f"{i}. {a} + {b}")
    back = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К турниру", callback_data=f"admtour:{tid}")]
    ])
    await _safe_edit(cb.message, "\n".join(lines), back)
    await cb.answer()


# ---------- 🏟 Локации ----------

@router.callback_query(F.data == "adm:locations")
async def adm_locations(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    locs = await db.list_locations()
    rows = [
        [InlineKeyboardButton(text=f"🏟 {loc['title'][:55]}", callback_data=f"admloc:{loc['id']}")]
        for loc in locs
    ]
    rows.append([InlineKeyboardButton(text="➕ Добавить локацию", callback_data="admloc:new")])
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="adm:menu")])
    text = "🏟 <b>Локации</b>\n"
    text += ("Список площадок клуба. Новые локации также можно добавить "
             "в шаге визарда «Локация».")
    await _safe_edit(cb.message, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data == "admloc:new")
async def adm_location_new(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    await state.clear()
    await state.set_state(LocationEdit.new_title)
    await cb.message.answer("➕ Название новой локации:")
    await cb.answer()


@router.message(LocationEdit.new_title, F.text)
async def adm_location_new_title(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.update_data(_new_title=message.text.strip()[:120])
    await state.set_state(LocationEdit.new_url)
    await message.answer("Ссылка на Google Maps (или «-» если нет):")


@router.message(LocationEdit.new_url, F.text)
async def adm_location_new_url(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    title = data.get("_new_title")
    url = message.text.strip()
    url = None if url in ("-", "—", "") else url
    await db.add_location(title, url)
    await state.clear()
    await message.answer(f"✅ Локация «{esc(title)}» добавлена.",
                          reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                              [InlineKeyboardButton(text="⬅️ К локациям", callback_data="adm:locations")]
                          ]))


@router.callback_query(F.data.startswith("admloce:"))
async def adm_location_edit_start(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    _, field, loc_id_s = cb.data.split(":")
    loc_id = int(loc_id_s)
    await state.clear()
    await state.update_data(_edit_loc_id=loc_id, _edit_field=field)
    if field == "title":
        await state.set_state(LocationEdit.title)
        await cb.message.answer("Новое название локации:")
    else:
        await state.set_state(LocationEdit.url)
        await cb.message.answer("Новая ссылка Google Maps (или «-» убрать):")
    await cb.answer()


@router.message(LocationEdit.title, F.text)
async def adm_location_edit_title(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    loc_id = data.get("_edit_loc_id")
    loc = await db.get_location(loc_id)
    if not loc:
        await state.clear()
        await message.answer("Локация не найдена.")
        return
    await db.update_location(loc_id, message.text.strip()[:120], loc.get("maps_url"))
    await state.clear()
    await message.answer("✅ Название обновлено.",
                          reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                              [InlineKeyboardButton(text="⬅️ К локации", callback_data=f"admloc:{loc_id}")]
                          ]))


@router.message(LocationEdit.url, F.text)
async def adm_location_edit_url(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    loc_id = data.get("_edit_loc_id")
    loc = await db.get_location(loc_id)
    if not loc:
        await state.clear()
        await message.answer("Локация не найдена.")
        return
    url = message.text.strip()
    url = None if url in ("-", "—", "") else url
    await db.update_location(loc_id, loc["title"], url)
    await state.clear()
    await message.answer("✅ Ссылка обновлена.",
                          reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                              [InlineKeyboardButton(text="⬅️ К локации", callback_data=f"admloc:{loc_id}")]
                          ]))


@router.callback_query(F.data.startswith("admloc:") & ~F.data.endswith(":new"))
async def adm_location_view(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    parts = cb.data.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        await cb.answer()
        return
    loc_id = int(parts[1])
    loc = await db.get_location(loc_id)
    if not loc:
        await cb.answer("Не найдено.", show_alert=True)
        return
    usage = await db.location_usage(loc_id)
    lines = [
        f"🏟 <b>{esc(loc['title'])}</b>",
        f"🔗 {esc(loc.get('maps_url') or '—')}",
        f"📊 Использована в турнирах: <b>{usage}</b>",
    ]
    rows = [
        [InlineKeyboardButton(text="✏️ Изменить название", callback_data=f"admloce:title:{loc_id}")],
        [InlineKeyboardButton(text="🔗 Изменить ссылку Maps", callback_data=f"admloce:url:{loc_id}")],
        [InlineKeyboardButton(text="⬅️ К локациям", callback_data="adm:locations")],
    ]
    await _safe_edit(cb.message, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


# ---------- 👤 Режим игрока ----------

@router.callback_query(F.data == "adm:playermode")
async def adm_player_mode(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    await state.clear()
    text = ("👤 <b>Режим игрока</b>\n"
            "Видишь бот глазами обычного участника. Можно записаться, "
            "посмотреть оплаты, отменить запись.")
    rows = [
        [InlineKeyboardButton(text="🏆 Ближайшие турниры", callback_data="list")],
        [InlineKeyboardButton(text="📋 Мои записи", callback_data="my_regs")],
        [InlineKeyboardButton(text="❓ Связь с админом", callback_data="contact")],
        [InlineKeyboardButton(text="🛡 Вернуться в админку", callback_data="adm:menu")],
    ]
    await _safe_edit(cb.message, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


def _fmt_money(amount, currency: str) -> str:
    if float(amount).is_integer():
        amount = f"{int(amount):,}".replace(",", ".")
    return f"{amount} {currency}".strip()


@router.callback_query(F.data == "adm:analytics")
async def adm_analytics(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏅 Топ-10 участников", callback_data="adm:top")],
        [InlineKeyboardButton(text="💰 Выручка (турнир/месяц/всё)", callback_data="adm:rev")],
        [InlineKeyboardButton(text="📈 Турниров за месяц", callback_data="adm:tcount")],
        [InlineKeyboardButton(text="👥 Уникальных игроков", callback_data="adm:uplayers")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="adm:menu")],
    ])
    await _safe_edit(cb.message, "📊 <b>Аналитика</b>", kb)
    await cb.answer()


@router.callback_query(F.data == "adm:rev")
async def adm_revenue_menu(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏆 По конкретному турниру", callback_data="adm:results")],
        [InlineKeyboardButton(text="📅 За этот месяц", callback_data="adm:revmonth")],
        [InlineKeyboardButton(text="📅 За всё время", callback_data="adm:revall")],
        [InlineKeyboardButton(text="⬅️ К аналитике", callback_data="adm:analytics")],
    ])
    await _safe_edit(cb.message, "💰 <b>Выручка</b> — выбери разрез:", kb)
    await cb.answer()


def _month_range(now) -> tuple[str, str, str]:
    """Возвращает (start_iso, end_iso, human-label) для текущего месяца."""
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return to_iso(start), to_iso(end), start.strftime("%m.%Y")


def _format_revenue(rows: list[dict]) -> str:
    if not rows:
        return "<i>— оплат не было</i>"
    parts = []
    for r in rows:
        amount = r["amount"] or 0
        if float(amount).is_integer():
            amount = f"{int(amount):,}".replace(",", ".")
        parts.append(f"• <b>{amount} {esc(r['cur'] or '')}</b> ({r['payers']} оплат)")
    return "\n".join(parts)


@router.callback_query(F.data == "adm:revmonth")
async def adm_revenue_month(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    start, end, label = _month_range(now_wita())
    rows = await db.revenue_by_currency(start, end)
    text = f"📅 <b>Выручка за {label}</b>\n\n" + _format_revenue(rows)
    back = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:rev")]
    ])
    await _safe_edit(cb.message, text, back)
    await cb.answer()


@router.callback_query(F.data == "adm:revall")
async def adm_revenue_all(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    rows = await db.revenue_by_currency()
    text = "📅 <b>Выручка за всё время</b>\n\n" + _format_revenue(rows)
    back = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:rev")]
    ])
    await _safe_edit(cb.message, text, back)
    await cb.answer()


@router.callback_query(F.data == "adm:tcount")
async def adm_tournaments_count(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    start, end, label = _month_range(now_wita())
    n = await db.tournaments_in_period(start, end)
    text = f"📈 <b>Турниров за {label}:</b> {n}"
    back = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К аналитике", callback_data="adm:analytics")]
    ])
    await _safe_edit(cb.message, text, back)
    await cb.answer()


@router.callback_query(F.data == "adm:uplayers")
async def adm_unique_players(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    n = await db.unique_players_total()
    text = f"👥 <b>Уникальных игроков за всё время:</b> {n}"
    back = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К аналитике", callback_data="adm:analytics")]
    ])
    await _safe_edit(cb.message, text, back)
    await cb.answer()


@router.callback_query(F.data == "adm:top")
async def adm_top(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    top = await db.top_participants(10)
    if not top:
        await cb.message.answer("Пока нет данных об участиях.")
        await cb.answer()
        return
    medals = ["🥇", "🥈", "🥉"] + ["▫️"] * 7
    lines = ["🏅 <b>Топ-10 участников</b>", ""]
    for i, r in enumerate(top):
        name = esc(r.get("nm") or "Игрок")
        un = f" @{esc(r['un'])}" if r.get("un") else ""
        lines.append(f"{medals[i]} {name}{un} — <b>{r['cnt']}</b>")
    await cb.message.answer("\n".join(lines))
    await cb.answer()


@router.callback_query(F.data == "adm:results")
async def adm_results(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    ts = await db.list_active_tournaments()
    if not ts:
        await cb.message.answer("Активных турниров нет.")
        await cb.answer()
        return
    await cb.message.answer("💰 Итоги — выбери турнир:", reply_markup=_tournaments_kb(ts, "ares"))
    await cb.answer()


@router.callback_query(F.data.startswith("ares:"))
async def adm_results_show(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    tid = cb.data.split(":", 1)[1]
    t = await db.get_tournament(tid)
    fin = await db.tournament_financials(tid)
    pairs = len(await db.get_main_registrations(tid))
    lines = [
        f"💰 <b>Итоги — {esc(t['title'])}</b>",
        "",
        f"Пар в составе: <b>{pairs}</b>",
        f"Оплатили: <b>{fin['paid']}</b> из {fin['total']} игроков",
        f"Собрано: <b>{_fmt_money(fin['amount'], fin['currency'])}</b>",
    ]
    await cb.message.answer("\n".join(lines))
    await cb.answer()


# Заметка: точки входа adm:payments / adm:addpair / adm:reschedule /
# adm:canceltour убраны — теперь все действия над турниром доступны
# через ветку «📋 Список турниров → выбрать турнир». Прямые callback'и
# (apay:/aap:/ares2:/actour:) сохранены, вызываются из ветки.


# ---------- /payments ----------

@router.message(Command("payments"))
async def cmd_payments(message: Message):
    if not _is_admin(message.from_user.id):
        return
    ts = await db.list_active_tournaments()
    if not ts:
        await message.answer("Активных турниров нет.")
        return
    await message.answer("💳 Оплаты — выбери турнир:", reply_markup=_tournaments_kb(ts, "apay"))


@router.callback_query(F.data.startswith("apay:"))
async def show_roster(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    tid = cb.data.split(":", 1)[1]
    text, kb = await _roster(tid)
    await _safe_edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(F.data.startswith("apm:"))
async def manage_pair(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    rid = int(cb.data.split(":", 1)[1])
    reg = await db.get_registration(rid)
    if not reg:
        await cb.answer("Не найдено.", show_alert=True)
        return
    a = reg.get("player_name") or "Игрок"
    b = reg.get("partner_name") or "Партнёр"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить обоих", callback_data=f"amc:{rid}:both")],
        [InlineKeyboardButton(text=f"✅ Только {a}"[:60], callback_data=f"amc:{rid}:player")],
        [InlineKeyboardButton(text=f"✅ Только {b}"[:60], callback_data=f"amc:{rid}:partner")],
        [InlineKeyboardButton(text=f"➖ Снять {a}"[:60], callback_data=f"apr:{rid}:player")],
        [InlineKeyboardButton(text=f"➖ Снять {b}"[:60], callback_data=f"apr:{rid}:partner")],
        [InlineKeyboardButton(text="❌ Снять пару", callback_data=f"apdel:{rid}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"apay:{reg['tournament_id']}")],
    ])
    await _safe_edit(cb.message, f"Пара: <b>{esc(a)} + {esc(b)}</b>\nОтметить оплату вручную:", kb)
    await cb.answer()


@router.callback_query(F.data.startswith("amc:"))
async def manual_confirm(cb: CallbackQuery, bot: Bot):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    _, rid_s, scope = cb.data.split(":")
    rid = int(rid_s)
    reg = await db.get_registration(rid)
    affected = await db.confirm_payment_manual(rid, scope, cb.from_user.id)
    t = await db.get_tournament(reg["tournament_id"])
    title = esc(t["title"]) if t else "турнир"
    for uid in affected:
        try:
            await bot.send_message(
                uid, f"✅ Оплата за турнир «{title}» подтверждена. Ждём на корте! 🎾"
            )
        except Exception:
            pass
    text, kb = await _roster(reg["tournament_id"])
    await _safe_edit(cb.message, text, kb)
    await cb.answer("Отмечено ✅")


# ---------- закрыть/открыть запись ----------

@router.callback_query(F.data.startswith("aclose:"))
async def toggle_close(cb: CallbackQuery, bot: Bot):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    tid = cb.data.split(":", 1)[1]
    t = await db.get_tournament(tid)
    if not t:
        await cb.answer("Не найдено.", show_alert=True)
        return
    new_closed = not bool(t.get("is_closed"))
    await db.set_registration_closed(tid, new_closed)
    await refresh_announcement(bot, tid)
    text, kb = await _roster(tid)
    await _safe_edit(cb.message, text, kb)
    await cb.answer("Запись закрыта 🔒" if new_closed else "Запись открыта 🔓")


# ---------- /mylist <id> — список участников + оплаты ----------

@router.message(Command("mylist"))
async def cmd_mylist(message: Message):
    if not _is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        ts = await db.list_active_tournaments()
        if not ts:
            await message.answer("Активных турниров нет.")
            return
        await message.answer("Выбери турнир:", reply_markup=_tournaments_kb(ts, "apay"))
        return
    tid = parts[1].strip()
    t = await db.get_tournament(tid)
    if not t:
        await message.answer("Турнир не найден.")
        return
    text, kb = await _roster(tid)
    await message.answer(text, reply_markup=kb)


# ---------- снятие пары админом ----------

@router.callback_query(F.data.startswith("apdel:"))
async def del_pair_confirm(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    rid = int(cb.data.split(":", 1)[1])
    reg = await db.get_registration(rid)
    if not reg:
        await cb.answer("Не найдено.", show_alert=True)
        return
    a = esc(reg.get("player_name") or "—")
    b = esc(reg.get("partner_name") or "—")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, снять", callback_data=f"apdel2:{rid}")],
        [InlineKeyboardButton(text="⬅️ Нет", callback_data=f"apm:{rid}")],
    ])
    await _safe_edit(cb.message, f"Снять пару <b>{a} + {b}</b> с турнира?", kb)
    await cb.answer()


@router.callback_query(F.data.startswith("apdel2:"))
async def del_pair_do(cb: CallbackQuery, bot: Bot):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    rid = int(cb.data.split(":", 1)[1])
    res = await db.cancel_registration_by_id(rid)
    if not res:
        await cb.answer("Пара уже снята.", show_alert=True)
        return
    t = await db.get_tournament(res["tid"])
    title = esc(t["title"]) if t else "турнир"
    for uid in res["member_ids"]:
        try:
            await bot.send_message(
                uid, f"❌ Организатор снял вашу запись на турнир «{title}»."
            )
        except Exception:
            pass
    if res["promoted"]:
        await _notify_promoted(bot, res["promoted"], title)
    await refresh_announcement(bot, res["tid"])
    text, kb = await _roster(res["tid"])
    await _safe_edit(cb.message, text, kb)
    await cb.answer("Пара снята")


async def _notify_promoted(bot: Bot, promoted: dict, title: str) -> None:
    text = (f"🎉 Освободилось место — вас подняли из листа ожидания на «{title}»!\n"
            "Скоро пришлём реквизиты на оплату.")
    for uid in (promoted.get("player_user_id"), promoted.get("partner_user_id")):
        if uid:
            try:
                await bot.send_message(uid, text)
            except Exception:
                pass


# ---------- снять одного игрока ----------

@router.callback_query(F.data.startswith("apr:"))
async def remove_player(cb: CallbackQuery, bot: Bot):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    _, rid_s, which = cb.data.split(":")
    rid = int(rid_s)
    res = await db.remove_one_player(rid, which)
    if not res:
        await cb.answer("Нельзя снять (пара уже не активна).", show_alert=True)
        return
    t = await db.get_tournament(res["tid"])
    title = esc(t["title"]) if t else "турнир"
    if res["removed_uid"]:
        try:
            await bot.send_message(res["removed_uid"],
                f"❌ Организатор снял твою запись на турнир «{title}».")
        except Exception:
            pass
    if res["remaining_uid"]:
        try:
            await bot.send_message(res["remaining_uid"],
                f"⚠️ Твой партнёр снят с турнира «{title}». Ты остался в списке как "
                "«ищет партнёра» — можешь найти нового напарника.")
        except Exception:
            pass
    await refresh_announcement(bot, res["tid"])
    text, kb = await _roster(res["tid"])
    await _safe_edit(cb.message, text, kb)
    await cb.answer("Игрок снят, второй ищет партнёра")


# ---------- снятые пары: вернуть ----------

@router.callback_query(F.data.startswith("apcanc:"))
async def cancelled_list(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    tid = cb.data.split(":", 1)[1]
    rows = await db.list_cancelled_registrations(tid)
    if not rows:
        await cb.answer("Снятых пар нет.", show_alert=True)
        return
    kb = []
    for r in rows:
        a = r.get("player_name") or "—"
        b = r.get("partner_name") or ("🔍" if r["is_looking_for_partner"] else "—")
        kb.append([InlineKeyboardButton(
            text=f"↩️ {a} + {b}"[:60], callback_data=f"arest:{r['id']}")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"apay:{tid}")])
    await _safe_edit(cb.message, "🗂 Снятые пары — нажми, чтобы вернуть:",
                     InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()


@router.callback_query(F.data.startswith("arest:"))
async def restore_pair(cb: CallbackQuery, bot: Bot):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    rid = int(cb.data.split(":", 1)[1])
    res = await db.restore_registration(rid)
    if not res:
        await cb.answer("Нельзя вернуть.", show_alert=True)
        return
    t = await db.get_tournament(res["tid"])
    title = esc(t["title"]) if t else "турнир"
    note = " (в лист ожидания)" if res["waitlisted"] else ""
    for uid in res["member_ids"]:
        try:
            await bot.send_message(uid, f"✅ Организатор вернул вашу запись на турнир «{title}»{note}.")
        except Exception:
            pass
    await refresh_announcement(bot, res["tid"])
    text, kb = await _roster(res["tid"])
    await _safe_edit(cb.message, text, kb)
    await cb.answer("Пара возвращена")


# ---------- перенос турнира (вход — кнопка в ветке турнира) ----------

@router.callback_query(F.data.startswith("ares2:"))
async def resched_start(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    tid = cb.data.split(":", 1)[1]
    t = await db.get_tournament(tid)
    if not t:
        await cb.answer("Не найдено.", show_alert=True)
        return
    await state.clear()
    await state.update_data(rs_tid=tid, rs_title=t["title"])
    await state.set_state(Reschedule.date)
    await cb.message.answer(
        f"<b>{esc(t['title'])}</b>\nТекущие дата/время: "
        f"{esc(t.get('date') or '?')} {esc(t.get('time_start') or '')}\n\n"
        f"Новая дата (ДД.ММ или ДД.ММ.ГГГГ):"
    )
    await cb.answer()


@router.message(Reschedule.date, F.text)
async def resched_date(message: Message, state: FSMContext):
    from timeutils import parse_date
    d = parse_date(message.text)
    if not d:
        await message.answer("Не понял дату. Формат ДД.ММ или ДД.ММ.ГГГГ:")
        return
    await state.update_data(rs_date_iso=d.isoformat(), rs_date_display=d.strftime("%d.%m"))
    await state.set_state(Reschedule.time)
    await message.answer("Новое время (например <code>19:00-21:00</code>):")


@router.message(Reschedule.time, F.text)
async def resched_time(message: Message, state: FSMContext, bot: Bot):
    from datetime import date as _date
    from timeutils import make_start_at, parse_time_range, payment_deadline, to_iso
    ts, te = parse_time_range(message.text)
    if not ts:
        await message.answer("Не понял время. Например <code>19:00-21:00</code>:")
        return
    data = await state.get_data()
    tid = data.get("rs_tid")
    d = _date.fromisoformat(data["rs_date_iso"])
    start_at = make_start_at(d, ts)
    deadline = payment_deadline(start_at)
    members = await db.reschedule_tournament(
        tid, data["rs_date_iso"], data["rs_date_display"],
        ts, te, to_iso(start_at), to_iso(deadline),
    )
    await state.clear()
    await refresh_announcement(bot, tid)
    title = esc(data["rs_title"])
    note = (f"🗓 Турнир «{title}» перенесён.\n"
            f"Новая дата: <b>{esc(data['rs_date_display'])} {esc(ts)}"
            + (f"–{esc(te)}" if te else "") + "</b>")
    for uid in members:
        try:
            await bot.send_message(uid, note)
        except Exception:
            pass
    await message.answer(
        f"✅ Перенесли. Уведомил участников: {len(members)}."
    )


# ---------- отмена турнира (вход — кнопка в ветке турнира) ----------

@router.callback_query(F.data.startswith("actour:"))
async def cancel_tour_confirm(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    tid = cb.data.split(":", 1)[1]
    t = await db.get_tournament(tid)
    if not t:
        await cb.answer("Не найдено.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, отменить турнир", callback_data=f"actour2:{tid}")],
        [InlineKeyboardButton(text="⬅️ Нет", callback_data=f"admtour:{tid}")],
    ])
    await _safe_edit(
        cb.message,
        f"Отменить турнир <b>{esc(t['title'])}</b>?\n"
        "Все записи будут сняты, участники получат уведомление.",
        kb,
    )
    await cb.answer()


@router.callback_query(F.data.startswith("actour2:"))
async def cancel_tour_do(cb: CallbackQuery, bot: Bot):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    tid = cb.data.split(":", 1)[1]
    res = await db.cancel_tournament(tid)
    if not res:
        await cb.answer("Турнир не найден.", show_alert=True)
        return
    title = esc(res["title"])
    # открепляем и помечаем анонс отменённым
    if res["announce_chat_id"] and res["announce_message_id"]:
        try:
            await bot.unpin_chat_message(res["announce_chat_id"], res["announce_message_id"])
        except Exception:
            pass
        try:
            await bot.edit_message_text(
                f"🚫 <b>{title}</b>\n\nТурнир отменён.",
                chat_id=res["announce_chat_id"],
                message_id=res["announce_message_id"],
            )
        except Exception:
            pass
    for uid in res["member_ids"]:
        try:
            await bot.send_message(uid, f"🚫 Турнир «{title}» отменён организатором.")
        except Exception:
            pass
    await _safe_edit(cb.message, f"🚫 Турнир «{title}» отменён. Участников уведомили: {len(res['member_ids'])}.", None)
    await cb.answer("Турнир отменён")


# ---------- /addpair ----------

@router.message(Command("addpair"))
async def cmd_addpair(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    ts = await db.list_active_tournaments()
    if not ts:
        await message.answer("Активных турниров нет.")
        return
    await state.clear()
    await message.answer(
        "➕ Добавить пару вручную — выбери турнир:",
        reply_markup=_tournaments_kb(ts, "aap"),
    )


@router.callback_query(F.data.startswith("aap:"))
async def addpair_tid(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    tid = cb.data.split(":", 1)[1]
    await state.set_state(AddPair.player)
    await state.update_data(tid=tid)
    await cb.message.answer("Игрок 1 — пришли <b>@ник</b> или имя:")
    await cb.answer()


async def _resolve(token: str):
    """(имя, user_id|None, username|None) из @ника или свободного имени."""
    token = token.strip()
    if token.startswith("@"):
        u = await db.find_user_by_username(token)
        if u:
            name = " ".join(filter(None, [u["first_name"], u["last_name"]])) \
                or u["username"] or token.lstrip("@")
            return name, u["user_id"], u["username"]
        return token.lstrip("@"), None, token.lstrip("@")
    return token, None, None


@router.message(AddPair.player, F.text)
async def addpair_player(message: Message, state: FSMContext):
    await state.update_data(player=message.text.strip())
    await state.set_state(AddPair.partner)
    await message.answer("Игрок 2 — пришли <b>@ник</b> или имя:")


@router.message(AddPair.partner, F.text)
async def addpair_partner(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    tid = data.get("tid")
    if not tid:
        await state.clear()
        await message.answer("Что-то пошло не так, начни заново: /addpair")
        return
    pl_name, pl_uid, pl_uname = await _resolve(data.get("player", ""))
    pa_name, pa_uid, pa_uname = await _resolve(message.text)
    rid = await db.create_manual_pair(
        tid, pl_name, pl_uname, pl_uid, pa_name, pa_uname, pa_uid
    )
    await state.clear()
    await message.answer(
        f"✅ Пара добавлена и отмечена оплаченной:\n<b>{esc(pl_name)} + {esc(pa_name)}</b>"
    )
    await refresh_announcement(bot, tid)
    log.info("manual pair %s added to %s by admin %s", rid, tid, message.from_user.id)
