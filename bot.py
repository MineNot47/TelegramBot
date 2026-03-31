from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramNetworkError

import config
from db import Database
from middlewares import BanMiddleware, DebugLogMiddleware, LastSeenMiddleware, RateLimitMiddleware, SponsorMiddleware
from routers import admin_router, common_router, earn_router, withdraw_router
from settings_store import SettingsStore
from flyer_client import FlyerClient
from flyer_webhook_server import create_app

from aiohttp import web


async def _seed_channels(db: Database, bot: Bot) -> None:
    # Добавляем каналы из config.py в БД (только добавление, без удаления).
    sponsor_existing = set(await db.list_sponsor_channels())
    for cid in config.SPONSOR_CHANNELS:
        cid_int = int(cid)
        if cid_int in sponsor_existing:
            continue
        try:
            chat = await bot.get_chat(cid_int)
            url = f"https://t.me/{chat.username}" if chat.username else None
            await db.add_sponsor_channel(cid_int, title=chat.title, username=chat.username, url=url, check_required=True)
        except Exception:
            await db.add_sponsor_channel(cid_int)

    task_existing = set(await db.list_task_channels())
    for cid in config.TASK_CHANNELS:
        cid_int = int(cid)
        if cid_int in task_existing:
            continue
        try:
            chat = await bot.get_chat(cid_int)
            await db.add_task_channel(cid_int, title=chat.title, username=chat.username)
        except Exception:
            await db.add_task_channel(cid_int)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    db_path = (os.getenv("DB_PATH") or "bot.sqlite3").strip() or "bot.sqlite3"
    logging.info("DB_PATH=%s", db_path)
    db = Database(db_path)
    await db.connect()
    await db.init()

    # timeout в aiogram — это float (секунды). proxy поддерживает http(s) и socks (через aiohttp-socks).
    session = AiohttpSession(proxy=config.PROXY_URL, timeout=120.0)
    bot = Bot(token=config.TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML), session=session)

    # На случай, если раньше был включён webhook, удаляем его.
    # Это не решит ситуацию, когда бот запущен в двух местах одновременно,
    # но уберёт конфликты webhook/getUpdates.
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    settings = await SettingsStore.create(db, config.DEFAULT_SETTINGS)
    await _seed_channels(db, bot)

    try:
        me = await bot.get_me()
    except TelegramNetworkError as e:
        logging.error("Не удалось подключиться к api.telegram.org: %s", e)
        logging.error(
            "Если Telegram API недоступен в вашей сети — включите VPN/прокси и задайте BOT_PROXY, затем запустите снова."
        )
        raise
    bot_username = me.username or "YourBot"

    flyer = None
    if config.FLYER_API_KEY:
        try:
            flyer = FlyerClient(config.FLYER_API_KEY)
            logging.info("FlyerAPI: включено (ключ найден в окружении).")
        except Exception as e:
            flyer = None
            logging.error("FlyerAPI: не удалось инициализировать (%s). Продолжаю без Flyer.", e)

    dp = Dispatcher(storage=MemoryStorage())

    # Middleware в aiogram 3 лучше вешать на конкретные события (message/callback_query),
    # иначе они будут получать объект Update, а не Message/CallbackQuery.
    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(RateLimitMiddleware(config.GLOBAL_RATE_LIMIT_SECONDS))
        observer.outer_middleware(BanMiddleware())
        observer.outer_middleware(SponsorMiddleware(config.ADMINS))
        observer.outer_middleware(LastSeenMiddleware())
        if config.DEBUG_LOG:
            observer.outer_middleware(DebugLogMiddleware())

    # Важно: common_router содержит fallback, поэтому подключаем его последним.
    dp.include_router(admin_router)
    dp.include_router(withdraw_router)
    dp.include_router(earn_router)
    dp.include_router(common_router)

    try:
        # ---- Опциональный вебхук-сервер для Flyer (требует публичный HTTPS URL) ----
        # Включение:
        #   setx FLYER_WEBHOOK_SECRET "любая_строка"
        #   setx FLYER_WEBHOOK_PORT "8080"  (локально)
        # URL в панели Flyer:
        #   https://<ваш_домен>/flyer/webhook/<FLYER_WEBHOOK_SECRET>
        webhook_secret = (os.getenv("FLYER_WEBHOOK_SECRET") or "").strip()
        # Railway/Heroku-like платформы обычно дают порт в переменной PORT.
        # Если PORT не задан — используем FLYER_WEBHOOK_PORT (удобно для локального запуска).
        port_raw = (os.getenv("PORT") or "").strip() or (os.getenv("FLYER_WEBHOOK_PORT") or "").strip()
        webhook_port = int(port_raw) if port_raw.isdigit() else None

        runner: web.AppRunner | None = None
        if webhook_secret and webhook_port:
            app = create_app(db=db, settings=settings, bot=bot, secret=webhook_secret)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host="0.0.0.0", port=int(webhook_port))
            await site.start()
            logging.info(
                "Flyer webhook server: listening on 0.0.0.0:%s (path: /flyer/webhook/<secret>)",
                webhook_port,
            )

        try:
            await dp.start_polling(
                bot,
                db=db,
                settings=settings,
                bot_username=bot_username,
                flyer=flyer,
            )
        finally:
            if runner is not None:
                try:
                    await runner.cleanup()
                except Exception:
                    pass
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
