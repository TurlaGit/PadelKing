"""Инфраструктура надёжности: ретраи, анти-флуд, алерты о сбоях.

- send_with_retry: безопасная обёртка над bot.send_message и др.
  методами с экспоненциальным backoff (1с → 2с), игнорирует
  «постоянные» ошибки (Forbidden, ChatNotFound).
- AntiFloodMiddleware: подавляет повторные клики по той же кнопке
  чаще, чем раз в 2с (защита от двойного-тройного клика).
- announce_failure_alert: счётчик подряд-сбоев refresh_announcement;
  алерт админам при ≥3 подряд.
"""
import asyncio
import logging
import time
from collections import defaultdict
from typing import Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import CallbackQuery, TelegramObject

log = logging.getLogger(__name__)

_RETRY_DELAYS = (1.0, 2.0)  # 2 ретрая: 1с, 2с


async def send_with_retry(coro_factory: Callable[[], Awaitable], *, name: str = "send") -> object | None:
    """Вызывает coro_factory() с ретраями при временных сбоях.
    coro_factory — функция БЕЗ аргументов, возвращающая корутину
    (нужно, потому что корутину нельзя await-нуть дважды).
    Возвращает результат или None при окончательном провале.
    """
    last_exc = None
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            return await coro_factory()
        except TelegramRetryAfter as e:
            # Telegram сам сказал, сколько ждать
            await asyncio.sleep(min(float(e.retry_after) + 0.5, 30.0))
            last_exc = e
            continue
        except TelegramForbiddenError:
            log.info("%s: пользователь заблокировал бота, ретраи не нужны", name)
            return None
        except TelegramBadRequest as e:
            # Постоянные ошибки (chat not found, message not found) — не ретраим
            msg = str(e).lower()
            if any(k in msg for k in ("chat not found", "user not found", "message to edit not found")):
                log.info("%s: %s — постоянная ошибка, не ретраим", name, e)
                return None
            last_exc = e
        except TelegramNetworkError as e:
            last_exc = e
        except Exception as e:
            last_exc = e
        if attempt < len(_RETRY_DELAYS):
            await asyncio.sleep(_RETRY_DELAYS[attempt])
    log.warning("%s: окончательный сбой после ретраев: %s", name, last_exc)
    return None


class AntiFloodMiddleware(BaseMiddleware):
    """Гасит повторные клики по той же кнопке за <THROTTLE_SEC секунд.

    Для callback_query — отвечает «Подожди немного…» и не вызывает хэндлер.
    Для message не трогаем (текстовый флуд блокирует сам Telegram).
    """
    THROTTLE_SEC = 2.0
    _last_click: dict[tuple[int, str], float] = defaultdict(float)
    _MAX_CACHE = 5000

    async def __call__(self, handler, event: TelegramObject, data: dict):
        if isinstance(event, CallbackQuery) and event.from_user and event.data:
            key = (event.from_user.id, event.data)
            now = time.monotonic()
            last = self._last_click[key]
            if now - last < self.THROTTLE_SEC:
                try:
                    await event.answer("Подожди немного…", show_alert=False)
                except Exception:
                    pass
                return
            self._last_click[key] = now
            # Не даём словарю расти бесконечно
            if len(self._last_click) > self._MAX_CACHE:
                cutoff = now - self.THROTTLE_SEC * 3
                for k in [k for k, v in self._last_click.items() if v < cutoff]:
                    self._last_click.pop(k, None)
        return await handler(event, data)


# ---------- алерты о повторных сбоях refresh_announcement ----------

_announce_fail_streak: dict[int, int] = defaultdict(int)
_announce_alert_sent: dict[int, bool] = defaultdict(bool)


async def announce_success(chat_id: int) -> None:
    _announce_fail_streak[chat_id] = 0
    _announce_alert_sent[chat_id] = False


async def announce_failure(bot, chat_id: int, err: Exception, admin_ids: list[int]) -> None:
    _announce_fail_streak[chat_id] += 1
    streak = _announce_fail_streak[chat_id]
    log.warning("refresh_announcement сбой #%d для чата %s: %s", streak, chat_id, err)
    if streak >= 3 and not _announce_alert_sent[chat_id]:
        _announce_alert_sent[chat_id] = True
        text = (f"⚠️ Не могу опубликовать/обновить анонс в группе "
                f"<code>{chat_id}</code> ({streak} подряд).\n"
                "Проверь права бота в группе или GROUP_CHAT_ID.")
        for uid in admin_ids:
            try:
                await bot.send_message(uid, text)
            except Exception:
                pass
