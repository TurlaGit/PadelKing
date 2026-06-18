"""Публикация и live-обновление анонса турнира в группе клуба.

refresh_announcement вызывается из всех точек, меняющих состав
(запись, подтверждение партнёра, приём заявки, отмена). Если анонса
ещё нет — постит, иначе редактирует через editMessageText.
"""
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import LinkPreviewOptions

import db
from config import GROUP_CHAT_ID
from content import load_content
from formatting import render_tournament
from keyboards import announce_keyboard

log = logging.getLogger(__name__)


async def refresh_announcement(bot: Bot, tid: str) -> None:
    t = await db.get_tournament(tid)
    if not t:
        return
    chat_id = t.get("group_chat_id") or GROUP_CHAT_ID
    if not chat_id:
        return

    location = None
    if t.get("location_id"):
        location = await db.get_location(t["location_id"])
    main = await db.get_main_registrations(tid)
    waitlist = await db.get_waitlist_registrations(tid)

    text = render_tournament(t, location, main, waitlist)
    label = (load_content()["texts"].get("announce_button") or "✅ Записаться").strip()
    singles = [r for r in main if r["status"] == "looking"]
    maps_url = (location or {}).get("maps_url")
    markup = announce_keyboard(tid, label, maps_url, singles,
                               closed=bool(t.get("is_closed")))
    preview = LinkPreviewOptions(is_disabled=True)

    existing_chat = t.get("announce_chat_id")
    existing_msg = t.get("announce_message_id")

    if existing_chat == chat_id and existing_msg:
        try:
            await bot.edit_message_text(
                text=text, chat_id=chat_id, message_id=existing_msg,
                reply_markup=markup, link_preview_options=preview,
            )
            return
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            log.info("Анонс не отредактирован, пересоздаю: %s", e)
        except Exception as e:
            log.warning("edit_message_text упал: %s", e)

    try:
        msg = await bot.send_message(
            chat_id=chat_id, text=text, reply_markup=markup,
            link_preview_options=preview,
        )
        await db.set_announce_message(tid, chat_id, msg.message_id)
    except Exception as e:
        log.warning("Не удалось отправить анонс в группу %s: %s", chat_id, e)
