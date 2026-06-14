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
from timeutils import from_iso, now_wita, to_iso

log = logging.getLogger(__name__)

SCAN_INTERVAL = 60          # секунд между сканами
FINISH_GRACE_HOURS = 2      # турнир «прошёл» через 2ч после старта


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


async def _tick(bot: Bot) -> None:
    now = now_wita()
    await publish_scheduled(bot, now)
    await manage_pins(bot, now)
    await finish_past(bot, now)


async def run_scheduler(bot: Bot) -> None:
    log.info("Планировщик запущен (скан раз в %d с)", SCAN_INTERVAL)
    while True:
        try:
            await _tick(bot)
        except Exception as e:
            log.exception("Ошибка в цикле планировщика: %s", e)
        await asyncio.sleep(SCAN_INTERVAL)
