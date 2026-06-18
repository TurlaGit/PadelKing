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
from config import ADMIN_IDS
from content import load_content
from formatting import _price_text, esc, render_payment
from keyboards import pay_choice, payment_review, pending_payments_choose

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


async def resend_requisites_to_user(bot: Bot, rid: int, user_id: int, t: dict | None = None) -> None:
    """Шлёт игроку реквизиты конкретно по этой паре (для кнопки
    «💸 Оплатить» в «Мои записи»). Если пара ещё неполная — создаём
    платёжные строки только если оба user_id известны."""
    if not t:
        reg = await db.get_registration(rid)
        if not reg:
            return
        t = await db.get_tournament(reg["tournament_id"])
    pays = await db.ensure_pair_payments(rid)
    payment = next((p for p in pays if p["user_id"] == user_id), None)
    body = render_payment(_texts().get("payment", "").strip(), t or {})
    text = (body or "💳 Реквизиты для оплаты турнира.") + "\n\nКак ты оплачиваешь?"
    pays_for = payment["pays_for"] if payment else "self"
    try:
        await bot.send_message(user_id, text, reply_markup=pay_choice(rid, pays_for))
    except Exception as e:
        log.info("Реквизиты не доставлены игроку %s: %s", user_id, e)


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

def _expected_amounts(t: dict) -> str:
    single = _price_text(t)
    if t.get("price_amount") is not None and t.get("currency"):
        d = t["price_amount"] * 2
        if float(d).is_integer():
            d = f"{int(d):,}".replace(",", ".")
        return f"за себя {single} · за двоих {d} {t['currency']}"
    return f"{single}" if single else ""


async def _send_review_card(bot: Bot, payment_id: int) -> None:
    """Шлёт всем админам фото-скрин с кнопками ✅за себя/👥за двоих/❌."""
    pay = await db.get_payment(payment_id)
    if not pay or not pay.get("screenshot_file_id"):
        return
    reg = await db.get_registration(pay["registration_id"])
    t = await db.get_tournament(reg["tournament_id"])

    a = esc(reg.get("player_name") or "—") + (f" @{esc(reg['player_username'])}" if reg.get("player_username") else "")
    b = esc(reg.get("partner_name") or "—") + (f" @{esc(reg['partner_username'])}" if reg.get("partner_username") else "")
    who_ru = "за себя" if pay["pays_for"] == "self" else "за обоих"
    expected = _expected_amounts(t)
    caption = (
        f"🧾 <b>Проверка оплаты</b> — {esc(t['title'])}\n"
        f"Пара: {a} + {b}\n"
        f"Плательщик: <b>{who_ru}</b>"
        + (f"\nОжидается: {esc(expected)}" if expected else "")
    )
    kb = payment_review(payment_id)
    fid = pay["screenshot_file_id"]
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_photo(admin_id, fid, caption=caption, reply_markup=kb)
        except Exception:
            try:
                await bot.send_document(admin_id, fid, caption=caption, reply_markup=kb)
            except Exception as e:
                log.info("Карточка проверки не доставлена админу %s: %s", admin_id, e)
                try:
                    await bot.send_message(admin_id, caption + "\n(медиа недоступно)", reply_markup=kb)
                except Exception:
                    pass


async def _attach_screenshot(bot: Bot, reply_to: Message, user, payment: dict, file_id: str):
    rid = payment["registration_id"]
    await db.set_payment_screenshot(rid, user.id, file_id)
    await reply_to.answer("Спасибо, скрин получен — передали на проверку ✅")
    fresh = await db.get_payment_for_user(rid, user.id)
    if fresh:
        await _send_review_card(bot, fresh["id"])


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


# ============================================================
#  2.3 — решение Яны по скрину
# ============================================================

async def _edit_admin_caption(cb: CallbackQuery, suffix: str) -> None:
    base = cb.message.caption or cb.message.text or ""
    new = f"{base}\n\n{suffix}"
    try:
        if cb.message.caption is not None:
            await cb.message.edit_caption(caption=new, reply_markup=None)
        else:
            await cb.message.edit_text(new, reply_markup=None)
    except Exception:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass


@router.callback_query(F.data.startswith("pchk:"))
async def payment_decision_cb(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Только для администратора.", show_alert=True)
        return
    try:
        _, pid_s, action = cb.data.split(":")
        payment_id = int(pid_s)
    except ValueError:
        await cb.answer()
        return

    pay = await db.get_payment(payment_id)
    if not pay:
        await cb.answer("Платёж не найден.", show_alert=True)
        return
    if pay["status"] in ("paid", "paid_manual"):
        await cb.answer("Уже подтверждено.", show_alert=True)
        await _edit_admin_caption(cb, "✅ <i>Уже подтверждено ранее.</i>")
        return

    reg = await db.get_registration(pay["registration_id"])
    t = await db.get_tournament(reg["tournament_id"])
    title = esc(t["title"]) if t else "турнир"

    if action == "reject":
        res = await db.reject_payment(payment_id, cb.from_user.id)
        await _edit_admin_caption(cb, "❌ <i>Отклонено.</i>")
        if res:
            admin_link = _texts().get("admin_username", "@zhurakovskaya")
            try:
                await bot.send_message(
                    res["user_id"],
                    f"❌ Оплата за турнир «{title}» не принята. "
                    f"Пришли корректный скрин сюда или свяжись с организатором "
                    f"{admin_link}.",
                )
            except Exception as e:
                log.info("Не уведомили игрока об отклонении: %s", e)
        await cb.answer("Отклонено")
        return

    mode = "both" if action == "both" else "self"
    res = await db.approve_payment(payment_id, mode, cb.from_user.id)
    if not res:
        await cb.answer("Не получилось.", show_alert=True)
        return
    mark = "👥 за двоих" if mode == "both" else "✅ за себя"
    await _edit_admin_caption(cb, f"{mark} — <i>принято.</i>")
    for uid in res["notified_user_ids"]:
        try:
            await bot.send_message(
                uid,
                f"✅ Ваша оплата за турнир «{title}» принята. "
                "Поздравляем — ждём вас на корте! 🎾",
            )
        except Exception as e:
            log.info("Не уведомили игрока %s о принятии оплаты: %s", uid, e)
    await cb.answer("Принято")
