"""Этап 1.2 — FSM записи игрока на турнир.

Точка входа — callback `reg:<tid>`. Диалог:
    имя (из Telegram / своё)  →  партнёр (знаю @ник / ищу).
Создаёт запись-пару в новой таблице `registrations`.

Привязка партнёра по нику с DM/пересылкой — шаг 1.3.
Лимит/лист ожидания и слоты — шаг 1.5. Оплаты — Этап 2.
"""
import logging
import re

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import db
from announcement import refresh_announcement
from content import load_content
from formatting import esc
from keyboards import (
    main_menu,
    pair_declined_options,
    reg_cancel,
    reg_name,
    reg_partner_choice,
)
from partners import notify_named_partner
from states import Register

_WAITLIST_NOTE = (
    "\n\n📋 Основной состав заполнен — вы в <b>листе ожидания</b>. "
    "Поднимем автоматически, как только освободится место."
)

log = logging.getLogger(__name__)
router = Router()

# Telegram username: 5–32 символа [A-Za-z0-9_]. Принимаем с/без ведущего @.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
_MAX_NAME_LEN = 64


def _texts() -> dict:
    return load_content()["texts"]


def _tg_name(user) -> str:
    return (
        " ".join(filter(None, [user.first_name, user.last_name]))
        or (user.username or str(user.id))
    )


async def _go_partner_step(message: Message, state: FSMContext, name: str) -> None:
    await state.set_state(None)
    await state.update_data(name=name)
    await message.answer(
        f"Имя: <b>{name}</b>\n\nС кем играешь?",
        reply_markup=reg_partner_choice(),
    )


async def _finish(message: Message, state: FSMContext, text: str) -> None:
    await state.clear()
    await message.answer(text, reply_markup=main_menu())


# ---------- entry ----------

@router.callback_query(F.data.startswith("reg:"))
async def reg_start(cb: CallbackQuery, state: FSMContext):
    tid = cb.data.split(":", 1)[1]
    t = await db.get_tournament(tid)
    if not t or not t.get("is_active"):
        await cb.answer(_texts()["not_found"], show_alert=True)
        return
    existing = await db.get_user_registration_in(tid, cb.from_user.id)
    if existing:
        await cb.answer("Ты уже записан на этот турнир ✅", show_alert=True)
        return

    await state.clear()
    await state.update_data(tid=tid)
    await cb.message.answer(
        f"Записываемся на <b>{t['title']}</b>!\nКак записать тебя в списке участников?",
        reply_markup=reg_name(_tg_name(cb.from_user)),
    )
    await cb.answer()


# ---------- name step ----------

@router.callback_query(F.data == "rn:tg")
async def name_from_tg(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("tid"):
        await cb.answer("Запись устарела, начни заново.", show_alert=True)
        return
    await _go_partner_step(cb.message, state, _tg_name(cb.from_user))
    await cb.answer()


@router.callback_query(F.data == "rn:edit")
async def name_edit(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("tid"):
        await cb.answer("Запись устарела, начни заново.", show_alert=True)
        return
    await state.set_state(Register.name)
    await cb.message.answer("Напиши, как тебя записать:", reply_markup=reg_cancel())
    await cb.answer()


@router.message(Register.name, F.text)
async def name_received(message: Message, state: FSMContext):
    name = (message.text or "").strip()[:_MAX_NAME_LEN]
    if not name:
        await message.answer("Имя не может быть пустым. Напиши ещё раз:", reply_markup=reg_cancel())
        return
    await _go_partner_step(message, state, name)


# ---------- partner step ----------

@router.callback_query(F.data == "pc:known")
async def partner_known(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("tid"):
        await cb.answer("Запись устарела, начни заново.", show_alert=True)
        return
    await state.set_state(Register.partner_username)
    await cb.message.answer(
        "Напиши <b>@ник</b> партнёра в Telegram (например, <code>@ivanov</code>):",
        reply_markup=reg_cancel(),
    )
    await cb.answer()


@router.message(Register.partner_username, F.text)
async def partner_received(message: Message, state: FSMContext, bot: Bot):
    raw = (message.text or "").strip().lstrip("@")
    if not _USERNAME_RE.match(raw):
        await message.answer(
            "Это не похоже на @ник. Ник состоит из латинских букв, цифр и _ "
            "(5–32 символа). Попробуй ещё раз или нажми «❌ Отмена».",
            reply_markup=reg_cancel(),
        )
        return

    own = (message.from_user.username or "").lower()
    if raw.lower() == own:
        await message.answer(
            "Это твой собственный ник 🙂 Укажи ник партнёра:",
            reply_markup=reg_cancel(),
        )
        return

    data = await state.get_data()
    tid = data.get("tid")
    if not tid:
        await _finish(message, state, "Запись устарела, начни заново.")
        return

    # Повторная защита от двойной записи (вдруг успел записаться в другом окне).
    if await db.get_user_registration_in(tid, message.from_user.id):
        await _finish(message, state, "Ты уже записан на этот турнир ✅")
        return

    reg = await db.create_registration(
        tid=tid,
        player_user_id=message.from_user.id,
        player_name=data.get("name") or _tg_name(message.from_user),
        player_username=message.from_user.username,
        partner_username=raw,
    )
    rid = reg["id"]
    wl_note = _WAITLIST_NOTE if reg["waitlisted"] else ""
    log.info("registration %s created (player=%s, partner=@%s, wl=%s)",
             rid, message.from_user.id, raw, reg["waitlisted"])

    await refresh_announcement(bot, tid)
    result = await notify_named_partner(bot, rid)
    if result["mode"] == "direct":
        await _finish(
            message,
            state,
            f"✅ Готово! Отправил <b>@{raw}</b> запрос на подтверждение.\n"
            "Как только он подтвердит — пришлём реквизиты на оплату." + wl_note,
        )
    else:
        await state.clear()
        await message.answer(
            f"⚠️ <b>@{raw}</b> ещё не запускал бота, поэтому я не могу написать ему первым.\n"
            "Перешли ему сообщение ниже 👇"
        )
        await message.answer(result["forward_text"])
        await message.answer(
            "Как только партнёр подтвердит участие по ссылке — пришлём реквизиты." + wl_note,
            reply_markup=main_menu(),
        )


@router.callback_query(F.data == "pc:looking")
async def partner_looking(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    tid = data.get("tid")
    if not tid:
        await cb.answer("Запись устарела, начни заново.", show_alert=True)
        return
    if await db.get_user_registration_in(tid, cb.from_user.id):
        await _finish(cb.message, state, "Ты уже записан на этот турнир ✅")
        await cb.answer()
        return

    reg = await db.create_registration(
        tid=tid,
        player_user_id=cb.from_user.id,
        player_name=data.get("name") or _tg_name(cb.from_user),
        player_username=cb.from_user.username,
        looking=True,
    )
    wl_note = _WAITLIST_NOTE if reg["waitlisted"] else ""
    log.info("registration %s created (player=%s, looking, wl=%s)",
             reg["id"], cb.from_user.id, reg["waitlisted"])
    await refresh_announcement(bot, tid)
    await _finish(
        cb.message,
        state,
        "✅ Готово! Ты в списке со статусом <b>«ищет партнёра»</b> 🔍\n"
        "Как только кто-то захочет в пару — пришлём тебе заявку на подтверждение." + wl_note,
    )
    await cb.answer()


# ---------- cancel wizard (отмена процесса записи) ----------

@router.callback_query(F.data == "rn:cancel")
async def reg_cancel_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("Запись отменена.", reply_markup=main_menu())
    await cb.answer()


# ---------- отмена существующей записи (§5.5–5.6) ----------

async def _notify_promoted(bot: Bot, promoted: dict, title: str) -> None:
    """Уведомляет поднятую из листа ожидания пару (игрока и партнёра)."""
    text = (
        f"🎉 Освободилось место — вас подняли из листа ожидания на "
        f"<b>{title}</b>!\nСкоро пришлём реквизиты на оплату."
    )
    for uid in (promoted.get("player_user_id"), promoted.get("partner_user_id")):
        if uid:
            try:
                await bot.send_message(uid, text)
            except Exception as e:
                log.info("Не уведомили поднятого %s: %s", uid, e)


@router.callback_query(F.data.startswith("rcancel:"))
async def cancel_existing_cb(cb: CallbackQuery, bot: Bot):
    try:
        rid = int(cb.data.split(":", 1)[1])
    except ValueError:
        await cb.answer()
        return
    reg = await db.get_registration(rid)
    if not reg:
        await cb.answer("Запись не найдена.", show_alert=True)
        return

    tid = reg["tournament_id"]
    res = await db.cancel_my_registration(tid, cb.from_user.id)
    if not res:
        await cb.answer("Нечего отменять.", show_alert=True)
        return

    t = await db.get_tournament(tid)
    title = esc(t["title"]) if t else "турнир"
    await cb.message.edit_text(f"Запись на <b>{title}</b> отменена.")
    await refresh_announcement(bot, tid)

    other = res["other_user_id"]
    if other:
        try:
            await bot.send_message(
                other,
                f"⚠️ Твой партнёр отменил участие в турнире <b>{title}</b>. "
                "Вы больше не в паре. Что дальше?",
                reply_markup=pair_declined_options(tid),
            )
        except Exception as e:
            log.info("Не уведомили партнёра %s об отмене: %s", other, e)

    if res["promoted"]:
        await _notify_promoted(bot, res["promoted"], title)

    await cb.answer("Отменено")
