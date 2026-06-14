"""Этап 2 — оплаты.

2.1: когда пара сформирована (оба user_id известны), обоим игрокам
уходят реквизиты + выбор «за себя / за обоих». Приём скрина и проверка
Яной — шаги 2.2–2.3.
"""
import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import db
from config import ADMIN_CHAT_ID
from content import load_content
from formatting import esc, render_payment
from keyboards import pay_choice, pending_payments_choose

log = logging.getLogger(__name__)
router = Router()


def _texts() -> dict:
    return load_content()["texts"]


def _name(user) -> str:
    return (
        " ".join(filter(None, [user.first_name, user.last_name]))
        or (user.username or str(user.id))
    )


def _file_id(message: Message) -> str | None:
    if message.photo:
        return message.photo[-1].file_id
    if message.document:
        return message.document.file_id
    return None


async def send_requisites_to_pair(bot: Bot, rid: int) -> None:
    """Создаёт платёжные строки и шлёт реквизиты обоим игрокам пары."""
    payments = await db.ensure_pair_payments(rid)
    if not payments:
        return
    reg = await db.get_registration(rid)
    t = await db.get_tournament(reg["tournament_id"])
    if not t:
        return
    body = render_payment(_texts().get("payment", "").strip(), t)

    for p in payments:
        text = body or "💳 Реквизиты для оплаты турнира."
        text += "\n\nКак ты оплачиваешь?"
        try:
            await bot.send_message(
                p["user_id"], text, reply_markup=pay_choice(rid, p["pays_for"])
            )
        except Exception as e:
            log.info("Реквизиты не доставлены игроку %s: %s", p["user_id"], e)


@router.callback_query(F.data.startswith("pay:"))
async def pay_choice_cb(cb: CallbackQuery):
    try:
        _, mode, rid_s = cb.data.split(":")
        rid = int(rid_s)
    except ValueError:
        await cb.answer()
        return
    if mode not in ("self", "both"):
        await cb.answer()
        return

    payment = await db.get_payment_for_user(rid, cb.from_user.id)
    if not payment:
        await cb.answer("Запись не найдена.", show_alert=True)
        return

    await db.set_payment_pays_for(rid, cb.from_user.id, mode)
    try:
        await cb.message.edit_reply_markup(reply_markup=pay_choice(rid, mode))
    except Exception:
        pass

    if mode == "both":
        await cb.answer("Отметил: платишь за обоих 👥")
    else:
        await cb.answer("Отметил: платишь за себя 🙋")


# ============================================================
#  2.2 — приём скриншота оплаты
# ============================================================

async def _attach_screenshot(bot: Bot, reply_to: Message, user, payment: dict, file_id: str):
    rid = payment["registration_id"]
    await db.set_payment_screenshot(rid, user.id, file_id)
    await reply_to.answer("Спасибо, скрин получен — передали на проверку ✅")
    # Базовый пинг админу; полная карточка проверки (с кнопками) — шаг 2.3.
    try:
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"🧾 Новый скрин оплаты: <b>{esc(payment.get('title') or 'турнир')}</b>\n"
            f"От: {esc(_name(user))}"
            + (f" @{esc(user.username)}" if user.username else ""),
        )
    except Exception as e:
        log.info("Не уведомили админа о скрине: %s", e)


@router.message(F.chat.type == "private", F.photo | F.document)
async def receive_screenshot(message: Message, state: FSMContext, bot: Bot):
    file_id = _file_id(message)
    if not file_id:
        return
    pendings = await db.get_user_pending_payments(message.from_user.id)
    if not pendings:
        await message.answer(
            "Сейчас у тебя нет турниров, ожидающих оплаты. "
            "Если хочешь записаться — нажми /start."
        )
        return
    if len(pendings) == 1:
        await _attach_screenshot(bot, message, message.from_user, pendings[0], file_id)
        return
    # несколько ожидающих оплат — уточняем турнир
    await state.update_data(pending_file_id=file_id)
    await message.answer(
        "К какому турниру относится оплата?",
        reply_markup=pending_payments_choose(pendings),
    )


@router.callback_query(F.data.startswith("payscr:"))
async def choose_payment_tournament(cb: CallbackQuery, state: FSMContext, bot: Bot):
    rid = int(cb.data.split(":", 1)[1])
    data = await state.get_data()
    file_id = data.get("pending_file_id")
    if not file_id:
        await cb.answer("Пришли скрин ещё раз, пожалуйста.", show_alert=True)
        return
    payment = await db.get_payment_for_user(rid, cb.from_user.id)
    if not payment:
        await cb.answer("Запись не найдена.", show_alert=True)
        return
    payment["title"] = (await db.get_tournament(
        (await db.get_registration(rid))["tournament_id"]
    ))["title"]
    await _attach_screenshot(bot, cb.message, cb.from_user, payment, file_id)
    await state.update_data(pending_file_id=None)
    await cb.answer()
