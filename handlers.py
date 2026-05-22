import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import CallbackQuery, Message

import db
from announcement import update_announcement
from config import ADMIN_CHAT_ID
from content import load_content
from formatting import esc, format_tournament, render_payment
from keyboards import back_to_menu, main_menu, tournament_card, tournaments_list
from sheets import append_registration

log = logging.getLogger(__name__)
router = Router()


def _texts() -> dict:
    return load_content()["texts"]


def _full_name(user) -> str:
    return " ".join(filter(None, [user.first_name, user.last_name])) or user.username or str(user.id)


async def _safe_edit(message: Message, text: str, reply_markup=None) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        # Если редактировать нельзя (например, прошло > 48ч) — шлём новым.
        await message.answer(text, reply_markup=reply_markup)


# ---------- /start ----------

@router.message(CommandStart(deep_link=True))
async def start_deep_link(message: Message, command: CommandObject):
    tid = (command.args or "").strip()
    t = await db.get_tournament(tid) if tid else None
    if not t or not t.get("is_active"):
        await message.answer(_texts()["not_found"], reply_markup=main_menu())
        return
    participants = await db.get_participants(tid)
    registered = any(p["user_id"] == message.from_user.id for p in participants)
    await message.answer(
        format_tournament(t, participants),
        reply_markup=tournament_card(tid, registered),
    )


@router.message(CommandStart())
async def start(message: Message):
    await message.answer(_texts()["welcome"], reply_markup=main_menu())


# ---------- Menu navigation ----------

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


@router.callback_query(F.data == "my_regs")
async def cb_my_regs(cb: CallbackQuery):
    regs = await db.get_user_registrations(cb.from_user.id)
    if not regs:
        await _safe_edit(cb.message, _texts()["my_regs_empty"], back_to_menu())
        await cb.answer()
        return
    lines = [_texts()["my_regs_header"], ""]
    for r in regs:
        flag = "✅" if r["my_status"] == "active" else "⏳"
        when = " ".join(filter(None, [r.get("date"), r.get("time")])).strip()
        lines.append(f"{flag} <b>{esc(r['title'])}</b>" + (f" — {esc(when)}" if when else ""))
    await _safe_edit(cb.message, "\n".join(lines), tournaments_list(regs))
    await cb.answer()


@router.callback_query(F.data.startswith("t:"))
async def cb_tournament(cb: CallbackQuery):
    tid = cb.data.split(":", 1)[1]
    t = await db.get_tournament(tid)
    if not t or not t.get("is_active"):
        await cb.answer(_texts()["not_found"], show_alert=True)
        return
    participants = await db.get_participants(tid)
    registered = any(p["user_id"] == cb.from_user.id for p in participants)
    await _safe_edit(
        cb.message,
        format_tournament(t, participants),
        tournament_card(tid, registered),
    )
    await cb.answer()


# ---------- Register / cancel ----------

@router.callback_query(F.data.startswith("join:"))
async def cb_join(cb: CallbackQuery, bot: Bot):
    tid = cb.data.split(":", 1)[1]
    t = await db.get_tournament(tid)
    if not t or not t.get("is_active"):
        await cb.answer(_texts()["not_found"], show_alert=True)
        return

    user = cb.from_user
    full_name = _full_name(user)
    result = await db.register_user(tid, user.id, user.username, full_name)
    texts = _texts()

    if result["status"] == "already":
        await cb.answer(texts["already_registered"], show_alert=True)
        return
    if result["status"] == "no_tournament":
        await cb.answer(texts["not_found"], show_alert=True)
        return

    status = result["status"]
    base = texts["joined"] if status == "active" else texts["waitlist"]

    # DM пользователю с реквизитами
    payment = render_payment(texts.get("payment", "").strip(), t)
    dm_text = base + (f"\n\n{payment}" if payment else "")
    try:
        await bot.send_message(user.id, dm_text)
    except Exception as e:
        log.info("Не смогли отправить DM пользователю %s: %s", user.id, e)

    # Обновляем карточку турнира в текущем чате
    participants = await db.get_participants(tid)
    await _safe_edit(
        cb.message,
        format_tournament(t, participants),
        tournament_card(tid, True),
    )

    # Обновляем анонс в группе
    fresh = await db.get_tournament(tid)
    await update_announcement(bot, fresh)

    # Уведомление админу
    try:
        admin_text = (
            "🆕 <b>Новая запись на турнир</b>\n"
            f"🏆 {esc(t['title'])}\n"
            f"👤 {esc(full_name)}"
            + (f" @{esc(user.username)}" if user.username else "")
            + f" (id <code>{user.id}</code>)\n"
            f"Статус: <b>{status}</b>"
        )
        await bot.send_message(ADMIN_CHAT_ID, admin_text)
    except Exception as e:
        log.info("Не смогли уведомить админа: %s", e)

    # Google Sheets
    await append_registration(tid, t["title"], user.id, user.username or "", full_name, status)

    await cb.answer(texts["joined"] if status == "active" else texts["waitlist"])


@router.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(cb: CallbackQuery, bot: Bot):
    tid = cb.data.split(":", 1)[1]
    t = await db.get_tournament(tid)
    if not t:
        await cb.answer(_texts()["not_found"], show_alert=True)
        return

    user = cb.from_user
    promoted = await db.cancel_registration(tid, user.id)
    texts = _texts()

    participants = await db.get_participants(tid)
    await _safe_edit(
        cb.message,
        format_tournament(t, participants),
        tournament_card(tid, False),
    )

    fresh = await db.get_tournament(tid)
    await update_announcement(bot, fresh)

    if promoted:
        try:
            note = texts["promoted"].format(title=t["title"])
            payment = render_payment(texts.get("payment", "").strip(), t)
            text = note + (f"\n\n{payment}" if payment else "")
            await bot.send_message(promoted["user_id"], text)
        except Exception as e:
            log.info("Не смогли уведомить promoted-пользователя: %s", e)
        await append_registration(
            tid, t["title"], promoted["user_id"],
            promoted.get("username") or "",
            promoted.get("full_name") or "",
            "promoted_to_active",
        )

    try:
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"❌ Отмена записи: <b>{esc(t['title'])}</b>\n"
            f"👤 {esc(_full_name(user))}"
            + (f" @{esc(user.username)}" if user.username else "")
            + f" (id <code>{user.id}</code>)",
        )
    except Exception as e:
        log.info("Не смогли уведомить админа об отмене: %s", e)

    await append_registration(
        tid, t["title"], user.id, user.username or "", _full_name(user), "cancelled"
    )
    await cb.answer(texts["cancelled"])
