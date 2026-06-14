"""Этап 1.2 — FSM записи игрока на турнир.

Точка входа — callback `reg:<tid>`. Диалог:
    имя (из Telegram / своё)  →  партнёр (знаю @ник / ищу).
Создаёт запись-пару в новой таблице `registrations`.

Привязка партнёра по нику с DM/пересылкой — шаг 1.3.
Лимит/лист ожидания и слоты — шаг 1.5. Оплаты — Этап 2.
"""
import logging
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import db
from content import load_content
from keyboards import main_menu, reg_cancel, reg_name, reg_partner_choice
from states import Register

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
async def partner_received(message: Message, state: FSMContext):
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

    rid = await db.create_registration(
        tid=tid,
        player_user_id=message.from_user.id,
        player_name=data.get("name") or _tg_name(message.from_user),
        player_username=message.from_user.username,
        partner_username=raw,
    )
    log.info("registration %s created (player=%s, partner=@%s)", rid, message.from_user.id, raw)
    await _finish(
        message,
        state,
        f"✅ Готово! Записал тебя в пару с <b>@{raw}</b>.\n"
        "Дальше подтвердим партнёра и пришлём реквизиты на оплату.",
    )


@router.callback_query(F.data == "pc:looking")
async def partner_looking(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tid = data.get("tid")
    if not tid:
        await cb.answer("Запись устарела, начни заново.", show_alert=True)
        return
    if await db.get_user_registration_in(tid, cb.from_user.id):
        await _finish(cb.message, state, "Ты уже записан на этот турнир ✅")
        await cb.answer()
        return

    rid = await db.create_registration(
        tid=tid,
        player_user_id=cb.from_user.id,
        player_name=data.get("name") or _tg_name(cb.from_user),
        player_username=cb.from_user.username,
        looking=True,
    )
    log.info("registration %s created (player=%s, looking)", rid, cb.from_user.id)
    await _finish(
        cb.message,
        state,
        "✅ Готово! Ты в списке со статусом <b>«ищет партнёра»</b> 🔍\n"
        "Как только кто-то захочет в пару — пришлём тебе заявку на подтверждение.",
    )
    await cb.answer()


# ---------- cancel ----------

@router.callback_query(F.data == "rn:cancel")
async def reg_cancel_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("Запись отменена.", reply_markup=main_menu())
    await cb.answer()
