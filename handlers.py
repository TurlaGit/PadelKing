"""Меню игрока, карточка турнира, «Мои записи», deep-link на турнир.

Запись/отмена/партнёрство живут в registration.py, partners.py,
matchmaking.py. Этот модуль — навигация и просмотр (новая модель пар).
"""
import logging

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message, TelegramObject, User

import db
from content import load_content
from formatting import esc, render_tournament
from keyboards import (
    PLAYER_REPLY,
    back_to_menu,
    dm_tournament_card,
    main_menu,
    my_registrations,
    tournaments_list,
)

log = logging.getLogger(__name__)
router = Router()


class KnownUsersMiddleware(BaseMiddleware):
    """На каждом апдейте кладёт пользователя в known_users —
    чтобы потом резолвить @ник → user_id (§5.3)."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user: User | None = data.get("event_from_user")
        if user is not None and not user.is_bot:
            try:
                await db.upsert_known_user(
                    user_id=user.id,
                    username=user.username,
                    first_name=user.first_name,
                    last_name=user.last_name,
                )
            except Exception as e:
                log.warning("upsert_known_user failed: %s", e)
        return await handler(event, data)


def _texts() -> dict:
    return load_content()["texts"]


async def _safe_edit(message: Message, text: str, reply_markup=None) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        await message.answer(text, reply_markup=reply_markup)


async def _render_card(tid: str, user_id: int):
    """Возвращает (text, markup) карточки турнира в личке или (None, None)."""
    t = await db.get_tournament(tid)
    if not t or not t.get("is_active"):
        return None, None
    location = await db.get_location(t["location_id"]) if t.get("location_id") else None
    main = await db.get_main_registrations(tid)
    waitlist = await db.get_waitlist_registrations(tid)
    text = render_tournament(t, location, main, waitlist)
    mine = await db.get_user_registration_in(tid, user_id)
    markup = dm_tournament_card(
        tid,
        mine["id"] if mine else None,
        closed=bool(t.get("is_closed")),
    )
    return text, markup


# ---------- /start ----------

@router.message(CommandStart(deep_link=True))
async def start_deep_link(message: Message, command: CommandObject):
    # confirm_/pair_ deep-links перехватывают partners/matchmaking роутеры выше.
    tid = (command.args or "").strip()
    text, markup = await _render_card(tid, message.from_user.id)
    if not text:
        await message.answer(_texts()["not_found"], reply_markup=main_menu())
        return
    await message.answer(text, reply_markup=markup)


@router.message(CommandStart())
async def start(message: Message):
    # Сначала ставим постоянное reply-меню снизу, потом inline-меню в чате.
    await message.answer(_texts()["welcome"], reply_markup=PLAYER_REPLY)
    await message.answer("Выбери раздел:", reply_markup=main_menu())


# Текстовые шорткаты с reply-кнопок снизу
@router.message(F.text == "🏆 Турниры")
async def shortcut_list(message: Message):
    tournaments = await db.list_active_tournaments()
    if not tournaments:
        await message.answer(_texts()["no_tournaments"], reply_markup=back_to_menu())
        return
    await message.answer(_texts()["list_header"], reply_markup=tournaments_list(tournaments))


@router.message(F.text == "📋 Мои записи")
async def shortcut_my_regs(message: Message):
    regs = await db.get_user_live_registrations(message.from_user.id)
    if not regs:
        await message.answer(_texts()["my_regs_empty"], reply_markup=back_to_menu())
        return
    await message.answer(_my_regs_text(regs), reply_markup=my_registrations(regs))


@router.message(F.text == "❓ Помощь")
async def shortcut_contact(message: Message):
    await message.answer(_texts()["admin_contact"], reply_markup=back_to_menu())


# ---------- навигация ----------

@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery):
    await _safe_edit(cb.message, _texts()["welcome"], main_menu())
    await cb.answer()


@router.callback_query(F.data == "contact")
async def cb_contact(cb: CallbackQuery):
    await _safe_edit(cb.message, _texts()["admin_contact"], back_to_menu())
    await cb.answer()


@router.callback_query(F.data == "list")
async def cb_list(cb: CallbackQuery):
    tournaments = await db.list_active_tournaments()
    if not tournaments:
        await _safe_edit(cb.message, _texts()["no_tournaments"], back_to_menu())
        await cb.answer()
        return
    await _safe_edit(cb.message, _texts()["list_header"], tournaments_list(tournaments))
    await cb.answer()


_PAY_ICON = {
    "paid": "💸 оплачено",
    "paid_manual": "💸 оплачено",
    "pending_review": "🧾 скрин на проверке",
    "rejected": "❌ оплата отклонена",
    "unpaid": "⏳ ждём оплату",
}


def _my_regs_text(regs: list[dict]) -> str:
    lines = [_texts()["my_regs_header"], ""]
    for r in regs:
        if r["reg_status"] == "looking":
            flag = "🔍 ищет партнёра"
        elif r["is_waitlist"]:
            flag = "📋 лист ожидания"
        elif r["reg_status"] == "pending_confirm":
            flag = "⏳ ждёт партнёра"
        else:
            flag = "✅ в составе"
        when = " ".join(filter(None, [r.get("date"), r.get("time")])).strip()
        title = esc(r["title"]) + (f" — {esc(when)}" if when else "")
        line = f"{flag} · <b>{title}</b>"
        pay = r.get("my_pay_status")
        if pay and r["reg_status"] == "active" and not r["is_waitlist"]:
            line += f"\n   {_PAY_ICON.get(pay, pay)}"
        lines.append(line)
    return "\n".join(lines)


@router.callback_query(F.data == "my_regs")
async def cb_my_regs(cb: CallbackQuery):
    regs = await db.get_user_live_registrations(cb.from_user.id)
    if not regs:
        await _safe_edit(cb.message, _texts()["my_regs_empty"], back_to_menu())
        await cb.answer()
        return
    await _safe_edit(cb.message, _my_regs_text(regs), my_registrations(regs))
    await cb.answer()


@router.callback_query(F.data.startswith("t:"))
async def cb_tournament(cb: CallbackQuery):
    tid = cb.data.split(":", 1)[1]
    text, markup = await _render_card(tid, cb.from_user.id)
    if not text:
        await cb.answer(_texts()["not_found"], show_alert=True)
        return
    await _safe_edit(cb.message, text, markup)
    await cb.answer()
