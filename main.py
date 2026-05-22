import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import config
import db
import handlers
from content import load_content

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("padelking")


async def sync_tournaments() -> None:
    content = load_content()
    ids = []
    for t in content["tournaments"]:
        if "id" not in t or "title" not in t:
            log.warning("Пропускаю турнир без id/title: %r", t)
            continue
        await db.upsert_tournament(t)
        ids.append(t["id"])
    await db.deactivate_missing(ids)
    log.info("Турниров активно: %d", len(ids))


async def _maybe_start_health_server() -> None:
    """Если задан $PORT — поднимаем простой HTTP-сервер для healthcheck Render Web Service."""
    if not config.PORT:
        return
    from aiohttp import web

    async def health(_):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    log.info("Health-сервер слушает порт %s", config.PORT)


async def main() -> None:
    config.validate()
    await db.init_db()
    await sync_tournaments()

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(handlers.router)

    await _maybe_start_health_server()
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Бот запущен, polling…")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
