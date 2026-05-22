import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import LinkPreviewOptions

import db
from config import GROUP_CHAT_ID
from content import load_content
from formatting import format_tournament
from keyboards import announce_button

log = logging.getLogger(__name__)


async def update_announcement(bot: Bot, tournament: dict) -> None:
    """Постит или редактирует анонс турнира в групповом чате клуба."""
    chat_id = tournament.get("group_chat_id") or GROUP_CHAT_ID
    if not chat_id:
        return

    participants = await db.get_participants(tournament["id"])
    text = format_tournament(tournament, participants)
    label = (load_content()["texts"].get("announce_button") or "Записаться").strip()
    markup = announce_button(tournament["id"], label)
    preview = LinkPreviewOptions(is_disabled=True)

    existing_chat = tournament.get("announce_chat_id")
    existing_msg = tournament.get("announce_message_id")

    if existing_chat == chat_id and existing_msg:
        try:
            await bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=existing_msg,
                reply_markup=markup,
                link_preview_options=preview,
            )
            return
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            log.info("Не удалось отредактировать анонс, пересоздаю: %s", e)
        except Exception as e:
            log.warning("edit_message_text упал: %s", e)

    try:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=markup,
            link_preview_options=preview,
        )
        await db.set_announce_message(tournament["id"], chat_id, msg.message_id)
    except Exception as e:
        log.warning("Не удалось отправить анонс в группу %s: %s", chat_id, e)
