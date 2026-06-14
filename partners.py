"""Этап 1.3 — привязка партнёра, указанного по @нику.

Сценарии (§5.3):
  • партнёр Б известен боту  → прямой DM с [✅ Подтвердить] [❌ Отказаться];
  • Б неизвестен/заблокировал → игроку А отдаём текст для пересылки
    с deep-link `?start=confirm_<rid>` — открытие = согласие;
  • подтверждение привязывает Б (status записи → 'active').

Лимит/лист ожидания (active vs waitlist) накладывается на шаге 1.5.
"""
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

import db
from config import BOT_USERNAME
from formatting import esc
from keyboards import main_menu, partner_confirm

log = logging.getLogger(__name__)
router = Router()


def _name(user) -> str:
    return (
        " ".join(filter(None, [user.first_name, user.last_name]))
        or (user.username or str(user.id))
    )


def _at(username: str | None) -> str:
    return f" @{esc(username)}" if username else ""


def _confirm_link(rid: int) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=confirm_{rid}"


# ============================================================
#  Уведомление партнёра после создания записи (вызывается из registration.py)
# ============================================================

async def notify_named_partner(bot: Bot, rid: int) -> dict:
    """Пытается достучаться до партнёра напрямую. Если нельзя — готовит
    текст для пересылки. Возвращает {'mode': 'direct'|'forward', 'forward_text'?}.
    """
    reg = await db.get_registration(rid)
    if not reg:
        return {"mode": "forward", "forward_text": ""}
    t = await db.get_tournament(reg["tournament_id"])
    title = t["title"] if t else "турнир"
    nick = reg.get("partner_username")
    inviter = esc(reg.get("player_name") or "Игрок")

    user = await db.find_user_by_username(nick) if nick else None
    if user and not user["is_blocked"]:
        try:
            await bot.send_message(
                user["user_id"],
                f"🎾 <b>{inviter}</b>{_at(reg.get('player_username'))} записал тебя "
                f"в пару на турнир <b>{esc(title)}</b>.\n\nПодтверждаешь участие?",
                reply_markup=partner_confirm(rid),
            )
            return {"mode": "direct"}
        except TelegramForbiddenError:
            await db.mark_user_blocked(user["user_id"], True)
        except Exception as e:
            log.warning("DM партнёру %s не доставлен: %s", user["user_id"], e)

    forward_text = (
        f"Привет! 🎾\n"
        f"Тебя записали в пару на турнир <b>{esc(title)}</b>.\n"
        f"Чтобы подтвердить участие, открой бота по ссылке:\n"
        f"{_confirm_link(rid)}"
    )
    return {"mode": "forward", "forward_text": forward_text}


# ============================================================
#  Общая логика подтверждения (используется и кнопкой, и deep-link)
# ============================================================

async def _do_confirm(bot: Bot, rid: int, confirmer) -> str:
    status = await db.bind_partner(rid, confirmer.id, _name(confirmer), confirmer.username)
    if status != "ok":
        return status
    reg = await db.get_registration(rid)
    t = await db.get_tournament(reg["tournament_id"])
    title = esc(t["title"]) if t else "турнир"
    # Уведомляем игрока А
    try:
        await bot.send_message(
            reg["player_user_id"],
            f"✅ <b>{esc(_name(confirmer))}</b>{_at(confirmer.username)} подтвердил "
            f"участие! Вы в паре на <b>{title}</b>.",
        )
    except Exception as e:
        log.info("Не уведомили игрока %s о подтверждении: %s", reg["player_user_id"], e)
    return "ok"


_CONFIRM_FAIL = {
    "stale": "Эта заявка уже неактуальна.",
    "self": "Нельзя подтвердить собственную заявку 🙂",
    "partner_busy": "Ты уже записан на этот турнир в другой паре.",
}


# ---------- подтверждение кнопкой в DM ----------

@router.callback_query(F.data.startswith("pconf:"))
async def partner_confirm_cb(cb: CallbackQuery, bot: Bot):
    try:
        _, rid_s, ans = cb.data.split(":")
        rid = int(rid_s)
    except ValueError:
        await cb.answer()
        return

    reg = await db.get_registration(rid)
    if not reg or reg["status"] != "pending_confirm":
        await cb.answer("Эта заявка уже неактуальна.", show_alert=True)
        return

    if ans == "yes":
        result = await _do_confirm(bot, rid, cb.from_user)
        if result == "ok":
            title = esc((await db.get_tournament(reg["tournament_id"]))["title"])
            await cb.message.edit_text(
                f"✅ Готово! Ты в паре с <b>{esc(reg.get('player_name') or 'игроком')}</b>"
                f"{_at(reg.get('player_username'))} на <b>{title}</b>.\n"
                "Скоро пришлём реквизиты на оплату.",
            )
        else:
            await cb.answer(_CONFIRM_FAIL.get(result, "Не получилось."), show_alert=True)
            return
    else:
        await db.set_registration_status(rid, "cancelled")
        try:
            await bot.send_message(
                reg["player_user_id"],
                f"❌ <b>{esc(_name(cb.from_user))}</b>{_at(cb.from_user.username)} "
                "отказался от участия в паре. Запись отменена — можешь записаться "
                "заново и указать другого партнёра или выбрать «🔍 Ищу партнёра».",
                reply_markup=main_menu(),
            )
        except Exception as e:
            log.info("Не уведомили игрока об отказе: %s", e)
        await cb.message.edit_text("Ты отказался от участия. Спасибо за ответ!")
    await cb.answer()


# ---------- подтверждение по deep-link (пересылка) ----------

@router.message(CommandStart(deep_link=True, magic=F.args.startswith("confirm_")))
async def confirm_via_link(message: Message, command: CommandObject, bot: Bot):
    try:
        rid = int(command.args.split("_", 1)[1])
    except (ValueError, IndexError):
        await message.answer("Ссылка повреждена.", reply_markup=main_menu())
        return

    reg = await db.get_registration(rid)
    if not reg or reg["status"] != "pending_confirm":
        await message.answer("Эта заявка уже неактуальна.", reply_markup=main_menu())
        return
    if message.from_user.id == reg["player_user_id"]:
        await message.answer(
            "Это твоя собственная заявка 🙂 Партнёр должен открыть ссылку сам.",
            reply_markup=main_menu(),
        )
        return

    result = await _do_confirm(bot, rid, message.from_user)
    if result == "ok":
        title = esc((await db.get_tournament(reg["tournament_id"]))["title"])
        await message.answer(
            f"✅ Готово! Ты в паре с <b>{esc(reg.get('player_name') or 'игроком')}</b>"
            f"{_at(reg.get('player_username'))} на <b>{title}</b>.\n"
            "Скоро пришлём реквизиты на оплату.",
            reply_markup=main_menu(),
        )
    else:
        await message.answer(_CONFIRM_FAIL.get(result, "Не получилось."), reply_markup=main_menu())
