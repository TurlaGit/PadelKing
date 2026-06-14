"""Этап 1.4 — слот «🔍 ищет партнёра» и заявки «✋ Хочу в пару».

Поток (§5.4):
  Б жмёт «Хочу в пару» (deep-link ?start=pair_<rid> из анонса или
  кнопка pair:<rid> из списка одиночек) → заявка уходит одиночке А
  → А [✅ Принять]/[❌ Отклонить].
  Принять → пара сформирована, прочие заявки к А закрыты.
  Отклонить → Б получает варианты (записаться / выбрать другого одиночку).

Таймаут заявки (24ч/до дедлайна) — авто-протухание в планировщике (Этап 3);
поле expires_at заполняется уже сейчас.
"""
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

import db
from formatting import esc
from keyboards import (
    main_menu,
    pair_declined_options,
    pair_request_decision,
    singles_list,
)

log = logging.getLogger(__name__)
router = Router()

_REQUEST_TTL_HOURS = 24


def _name(user) -> str:
    return (
        " ".join(filter(None, [user.first_name, user.last_name]))
        or (user.username or str(user.id))
    )


def _at(username: str | None) -> str:
    return f" @{esc(username)}" if username else ""


def _expires_at() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=_REQUEST_TTL_HOURS)).isoformat(
        timespec="seconds"
    )


# ============================================================
#  Отправка заявки (общая для deep-link и callback)
# ============================================================

async def _send_pair_request(bot: Bot, rid: int, from_user) -> str:
    """Создаёт заявку Б→одиночке и шлёт DM одиночке. Возвращает текст для Б."""
    reg = await db.get_registration(rid)
    if not reg or reg["status"] != "looking":
        return "Увы, этот игрок уже нашёл партнёра."
    tid = reg["tournament_id"]

    res = await db.create_pair_request(
        tid=tid,
        to_registration_id=rid,
        from_user_id=from_user.id,
        from_name=_name(from_user),
        from_username=from_user.username,
        expires_at=_expires_at(),
    )
    status = res["status"]
    if status == "self":
        return "Это твой собственный слот 🙂"
    if status == "busy":
        return "Ты уже записан на этот турнир."
    if status == "taken":
        return "Увы, этот игрок уже нашёл партнёра."
    if status == "exists":
        return "Ты уже отправлял заявку этому игроку — ждём его ответа."

    # status == ok → уведомляем одиночку А
    t = await db.get_tournament(tid)
    title = esc(t["title"]) if t else "турнир"
    try:
        await bot.send_message(
            reg["player_user_id"],
            f"🤝 <b>{esc(_name(from_user))}</b>{_at(from_user.username)} хочет "
            f"играть с тобой в паре на турнир <b>{title}</b>.",
            reply_markup=pair_request_decision(res["request_id"]),
        )
    except Exception as e:
        log.info("Не доставили заявку одиночке %s: %s", reg["player_user_id"], e)
        return "Не получилось отправить заявку игроку. Попробуй позже."

    pname = esc(reg.get("player_name") or "игроку")
    return f"✅ Запрос отправлен <b>{pname}</b>, ждём его ответа."


# ---------- вход: deep-link ?start=pair_<rid> ----------

@router.message(CommandStart(deep_link=True, magic=F.args.startswith("pair_")))
async def want_pair_via_link(message: Message, command: CommandObject, bot: Bot):
    try:
        rid = int(command.args.split("_", 1)[1])
    except (ValueError, IndexError):
        await message.answer("Ссылка повреждена.", reply_markup=main_menu())
        return
    text = await _send_pair_request(bot, rid, message.from_user)
    await message.answer(text, reply_markup=main_menu())


# ---------- вход: callback pair:<rid> (из списка одиночек) ----------

@router.callback_query(F.data.startswith("pair:"))
async def want_pair_cb(cb: CallbackQuery, bot: Bot):
    rid = int(cb.data.split(":", 1)[1])
    text = await _send_pair_request(bot, rid, cb.from_user)
    await cb.message.answer(text, reply_markup=main_menu())
    await cb.answer()


# ---------- список одиночек: singles:<tid> ----------

@router.callback_query(F.data.startswith("singles:"))
async def show_singles(cb: CallbackQuery):
    tid = cb.data.split(":", 1)[1]
    singles = await db.list_singles(tid, exclude_user_id=cb.from_user.id)
    if not singles:
        await cb.message.answer("Сейчас никто не ищет партнёра 🤷", reply_markup=main_menu())
        await cb.answer()
        return
    await cb.message.answer(
        "Кто ищет партнёра — выбери, кому отправить заявку:",
        reply_markup=singles_list(tid, singles),
    )
    await cb.answer()


# ============================================================
#  Решение одиночки А: принять / отклонить
# ============================================================

@router.callback_query(F.data.startswith("preq:"))
async def pair_request_decision_cb(cb: CallbackQuery, bot: Bot):
    try:
        _, req_id_s, ans = cb.data.split(":")
        req_id = int(req_id_s)
    except ValueError:
        await cb.answer()
        return

    req = await db.get_pair_request(req_id)
    if not req or req["status"] != "pending":
        await cb.answer("Заявка уже неактуальна.", show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    reg = await db.get_registration(req["to_registration_id"])
    if not reg or cb.from_user.id != reg["player_user_id"]:
        await cb.answer("Это не твоя заявка.", show_alert=True)
        return

    t = await db.get_tournament(req["tournament_id"])
    title = esc(t["title"]) if t else "турнир"
    bname = esc(req.get("from_name") or "игрок")

    if ans == "yes":
        res = await db.accept_pair_request(req_id)
        if res["status"] != "ok":
            msg = {
                "stale": "Заявка уже неактуальна.",
                "taken": "Ты уже нашёл партнёра.",
                "partner_busy": "Этот игрок уже записался в другую пару.",
            }.get(res["status"], "Не получилось.")
            await cb.answer(msg, show_alert=True)
            return

        await cb.message.edit_text(
            f"✅ Готово! Вы в паре с <b>{bname}</b>{_at(req.get('from_username'))} "
            f"на <b>{title}</b>.\nСкоро пришлём реквизиты на оплату."
        )
        # уведомляем Б
        try:
            await bot.send_message(
                res["partner_user_id"],
                f"✅ <b>{esc(reg.get('player_name') or 'Игрок')}</b>"
                f"{_at(reg.get('player_username'))} принял твою заявку! "
                f"Вы в паре на <b>{title}</b>.\nСкоро пришлём реквизиты на оплату.",
            )
        except Exception as e:
            log.info("Не уведомили Б %s о принятии: %s", res["partner_user_id"], e)
        # уведомляем отклонённых
        for uid in res["notify_rejected"]:
            try:
                await bot.send_message(
                    uid,
                    f"К сожалению, <b>{esc(reg.get('player_name') or 'игрок')}</b> "
                    f"выбрал другого партнёра на турнир «{title}».",
                    reply_markup=pair_declined_options(req["tournament_id"]),
                )
            except Exception:
                pass
    else:
        await db.decline_pair_request(req_id)
        await cb.message.edit_text(f"Ты отклонил заявку <b>{bname}</b>.")
        try:
            await bot.send_message(
                req["from_user_id"],
                f"<b>{esc(reg.get('player_name') or 'Игрок')}</b> отклонил твою "
                f"заявку на турнир «{title}». Что дальше?",
                reply_markup=pair_declined_options(req["tournament_id"]),
            )
        except Exception as e:
            log.info("Не уведомили Б об отказе: %s", e)
    await cb.answer()
