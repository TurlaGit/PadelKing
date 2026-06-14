"""Этап 3 — планировщик: цикл «скан раз в минуту», restart-safe.

Все времена — в поясе WITA (UTC+8), сравнения по ISO-строкам.
3.1: отложенная публикация, пины, завершение турниров.
Напоминания/авто-снятие — 3.2; протухание заявок/чистка — 3.3.
"""
import asyncio
import logging
from datetime import timedelta

from aiogram import Bot

import db
from announcement import refresh_announcement
from config import ADMIN_IDS
from formatting import esc
from keyboards import pair_declined_options
from timeutils import from_iso, now_wita, to_iso

log = logging.getLogger(__name__)

SCAN_INTERVAL = 60            # секунд между сканами
FINISH_GRACE_HOURS = 2        # турнир «прошёл» через 2ч после старта
PROMO_WINDOW_HOURS = 5        # окно оплаты для поднятых из листа ожидания
SCREENSHOT_TTL_DAYS = 14      # сколько храним скрины после турнира


async def _dm(bot: Bot, uid: int, text: str, **kw) -> None:
    try:
        await bot.send_message(uid, text, **kw)
    except Exception as e:
        log.info("DM %s не доставлен: %s", uid, e)


def _fmt(dt) -> str:
    return dt.strftime("%d.%m %H:%M") if dt else ""


def _fully_paid(reg: dict) -> bool:
    return reg.get("pay_total", 0) > 0 and reg["pay_paid"] == reg["pay_total"]


def _pair_name(reg: dict) -> str:
    a = reg.get("player_name") or "—"
    b = reg.get("partner_name") or "—"
    return f"{a} + {b}"


async def publish_scheduled(bot: Bot, now) -> None:
    """Публикует анонсы запланированных турниров, когда настало publish_at."""
    for t in await db.list_due_scheduled(to_iso(now)):
        await db.set_tournament_status_v2(t["id"], "published")
        await refresh_announcement(bot, t["id"])
        log.info("Авто-публикация турнира %s", t["id"])


async def manage_pins(bot: Bot, now) -> None:
    """Закрепляет анонсы будущих турниров (один раз)."""
    for t in await db.list_published_with_announce():
        chat, msg = t.get("announce_chat_id"), t.get("announce_message_id")
        if not chat or not msg or t.get("pinned"):
            continue
        start = from_iso(t.get("start_at"))
        if start and start > now:
            try:
                await bot.pin_chat_message(chat, msg, disable_notification=True)
                await db.set_pinned(t["id"], True)
                log.info("Закрепил анонс турнира %s", t["id"])
            except Exception as e:
                log.info("Не удалось закрепить %s: %s", t["id"], e)


async def finish_past(bot: Bot, now) -> None:
    """Помечает прошедшие турниры finished и открепляет их анонсы."""
    grace_iso = to_iso(now - timedelta(hours=FINISH_GRACE_HOURS))
    for t in await db.list_finished_due(grace_iso):
        chat, msg = t.get("announce_chat_id"), t.get("announce_message_id")
        if t.get("pinned") and chat and msg:
            try:
                await bot.unpin_chat_message(chat, msg)
            except Exception as e:
                log.info("Не удалось открепить %s: %s", t["id"], e)
            await db.set_pinned(t["id"], False)
        await db.set_tournament_status_v2(t["id"], "finished")
        log.info("Турнир %s завершён", t["id"])


async def payment_reminders(bot: Bot, now) -> None:
    """Цепочка напоминаний об оплате (мягкое → жёсткое за час)."""
    for p in await db.get_unpaid_payment_candidates():
        uid = p.get("user_id")
        if not uid:
            continue
        promo = p.get("promo_deadline")
        D = from_iso(promo) if promo else from_iso(p.get("payment_deadline"))
        if not D or now >= D:  # снятие — отдельной джобой
            continue
        stage = p.get("reminder_stage") or 0
        title = esc(p.get("title") or "турнир")
        soft_off = timedelta(hours=2.5) if promo else timedelta(hours=24)

        if now >= D - timedelta(hours=1):
            if stage < 2:
                await _dm(bot, uid,
                    f"⏰ Остался 1 час на оплату турнира «{title}». Если оплата не "
                    f"поступит до <b>{_fmt(D)}</b>, бронь снимется и место освободится. "
                    "Если уже оплатил — пришли скрин сюда.")
                await db.set_payment_reminder_stage(p["id"], 2, to_iso(now))
        elif now >= D - soft_off:
            if stage < 1:
                await _dm(bot, uid,
                    f"🔔 Напоминание: оплати участие в турнире «{title}» до "
                    f"<b>{_fmt(D)}</b>, иначе бронь снимется. Скрин пришли сюда.")
                await db.set_payment_reminder_stage(p["id"], 1, to_iso(now))


async def notify_yana_unpaid(bot: Bot, now) -> None:
    """За час до дедлайна — Яне список всех неоплативших (один раз на турнир)."""
    for t in await db.list_unnotified_published():
        D = from_iso(t.get("payment_deadline"))
        if not D or now < D - timedelta(hours=1):
            continue
        unpaid = [r for r in await db.get_active_payment_state(t["id"]) if not _fully_paid(r)]
        if unpaid:
            lines = [f"⚠️ За час до дедлайна «{esc(t['title'])}». Не оплатили:"]
            lines += [f"• {esc(_pair_name(r))}" for r in unpaid]
            text = "\n".join(lines)
            for admin_id in ADMIN_IDS:
                await _dm(bot, admin_id, text)
        await db.set_unpaid_notified(t["id"])


async def auto_remove_unpaid(bot: Bot, now) -> None:
    """Снимает неоплаченные пары после дедлайна, поднимает из листа ожидания."""
    for reg in await db.get_active_payment_state():
        if _fully_paid(reg):
            continue
        promo = reg.get("promo_deadline")
        D = from_iso(promo) if promo else from_iso(reg.get("payment_deadline"))
        if not D or now < D:
            continue
        t_deadline = from_iso(reg.get("payment_deadline"))
        promo_iso = (to_iso(now + timedelta(hours=PROMO_WINDOW_HOURS))
                     if t_deadline and now >= t_deadline else None)
        res = await db.remove_registration_unpaid(reg["id"], promo_iso)
        if not res:
            continue
        title = esc(reg.get("title") or "турнир")
        for uid in res["member_ids"]:
            await _dm(bot, uid,
                f"❌ Бронь на турнир «{title}» снята — оплата не поступила вовремя.")
        for admin_id in ADMIN_IDS:
            await _dm(bot, admin_id,
                f"❌ Авто-снятие (неоплата) на «{title}»: {esc(_pair_name(reg))}.")
        if res["promoted"]:
            await _notify_promoted(bot, res["promoted"], title)
        await refresh_announcement(bot, res["tid"])
        log.info("Авто-снятие пары %s (неоплата)", reg["id"])


async def _notify_promoted(bot: Bot, promoted: dict, title: str) -> None:
    text = (f"🎉 Освободилось место — вас подняли из листа ожидания на «{title}»!\n"
            f"Оплатите в течение {PROMO_WINDOW_HOURS} часов — пришлите скрин сюда.")
    for uid in (promoted.get("player_user_id"), promoted.get("partner_user_id")):
        if uid:
            await _dm(bot, uid, text)


async def expire_pair_requests(bot: Bot, now) -> None:
    """Протухание заявок в пару (24ч/до дедлайна) с уведомлением автора."""
    for r in await db.expire_due_pair_requests(to_iso(now)):
        await _dm(bot, r["from_user_id"],
            f"⌛ Твоя заявка в пару на турнир «{esc(r.get('title') or 'турнир')}» "
            "истекла без ответа. Что дальше?",
            reply_markup=pair_declined_options(r["tournament_id"]))


async def cleanup_screenshots(bot: Bot, now) -> None:
    cutoff = to_iso(now - timedelta(days=SCREENSHOT_TTL_DAYS))
    n = await db.cleanup_old_screenshots(cutoff)
    if n:
        log.info("Удалено старых скринов: %d", n)


async def _tick(bot: Bot) -> None:
    now = now_wita()
    await publish_scheduled(bot, now)
    await manage_pins(bot, now)
    await finish_past(bot, now)
    await payment_reminders(bot, now)
    await notify_yana_unpaid(bot, now)
    await auto_remove_unpaid(bot, now)
    await expire_pair_requests(bot, now)
    await cleanup_screenshots(bot, now)


async def run_scheduler(bot: Bot) -> None:
    log.info("Планировщик запущен (скан раз в %d с)", SCAN_INTERVAL)
    while True:
        try:
            await _tick(bot)
        except Exception as e:
            log.exception("Ошибка в цикле планировщика: %s", e)
        await asyncio.sleep(SCAN_INTERVAL)
