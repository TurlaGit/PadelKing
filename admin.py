"""Этап 2.4 — мини-админка оплат (для ADMIN_IDS).

/payments — статусы оплат по турниру + ручное подтверждение по игроку.
/addpair  — добавить готовую пару вручную (нал/форс-мажор), без напоминаний.

Полный визард создания турнира и аналитика — Этап 4.
"""
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
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
from formatting import esc
from states import AddPair

log = logging.getLogger(__name__)
router = Router()

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
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


async def _safe_edit(message: Message, text: str, kb) -> None:
    try:
        await message.edit_text(text, reply_markup=kb)
    except Exception:
        await message.answer(text, reply_markup=kb)


# ---------- /admin меню ----------

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not _is_admin(message.from_user.id):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать турнир", callback_data="tw:start")],
        [InlineKeyboardButton(text="💳 Оплаты", callback_data="adm:payments")],
        [InlineKeyboardButton(text="➕ Добавить пару вручную", callback_data="adm:addpair")],
        [InlineKeyboardButton(text="📊 Аналитика", callback_data="adm:analytics")],
        [InlineKeyboardButton(text="🗑 Отменить турнир", callback_data="adm:canceltour")],
    ])
    await message.answer("⚙️ <b>Админка PadelKing</b>", reply_markup=kb)


def _admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать турнир", callback_data="tw:start")],
        [InlineKeyboardButton(text="💳 Оплаты", callback_data="adm:payments")],
        [InlineKeyboardButton(text="➕ Добавить пару вручную", callback_data="adm:addpair")],
        [InlineKeyboardButton(text="📊 Аналитика", callback_data="adm:analytics")],
        [InlineKeyboardButton(text="🗑 Отменить турнир", callback_data="adm:canceltour")],
    ])


@router.callback_query(F.data == "adm:menu")
async def adm_menu(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    await _safe_edit(cb.message, "⚙️ <b>Админка PadelKing</b>", _admin_menu_kb())
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
        [InlineKeyboardButton(text="💰 Итоги турнира", callback_data="adm:results")],
    ])
    await cb.message.answer("📊 Аналитика:", reply_markup=kb)
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


@router.callback_query(F.data == "adm:payments")
async def adm_payments(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    ts = await db.list_active_tournaments()
    if not ts:
        await cb.message.answer("Активных турниров нет.")
        await cb.answer()
        return
    await cb.message.answer("💳 Оплаты — выбери турнир:", reply_markup=_tournaments_kb(ts, "apay"))
    await cb.answer()


@router.callback_query(F.data == "adm:addpair")
async def adm_addpair(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    ts = await db.list_active_tournaments()
    if not ts:
        await cb.message.answer("Активных турниров нет.")
        await cb.answer()
        return
    await state.clear()
    await cb.message.answer("➕ Добавить пару — выбери турнир:", reply_markup=_tournaments_kb(ts, "aap"))
    await cb.answer()


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


# ---------- отмена турнира админом ----------

@router.callback_query(F.data == "adm:canceltour")
async def cancel_tour_list(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    ts = await db.list_active_tournaments()
    if not ts:
        await cb.message.answer("Активных турниров нет.")
        await cb.answer()
        return
    await cb.message.answer("🗑 Отменить турнир — выбери:", reply_markup=_tournaments_kb(ts, "actour"))
    await cb.answer()


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
        [InlineKeyboardButton(text="⬅️ Нет", callback_data="adm:menu")],
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
