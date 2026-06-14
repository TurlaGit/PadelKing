"""Этап 2 — оплаты.

2.1: когда пара сформирована (оба user_id известны), обоим игрокам
уходят реквизиты + выбор «за себя / за обоих». Приём скрина и проверка
Яной — шаги 2.2–2.3.
"""
import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

import db
from content import load_content
from formatting import esc, render_payment
from keyboards import pay_choice

log = logging.getLogger(__name__)
router = Router()


def _texts() -> dict:
    return load_content()["texts"]


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
