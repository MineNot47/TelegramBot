from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.types import CallbackQuery, Message, TelegramObject

from db import Database
from keyboards import sponsors_kb
from settings_store import SettingsStore


class RateLimitMiddleware(BaseMiddleware):
    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval_seconds = float(min_interval_seconds)
        self._last: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id: int | None = None
        text: str | None = None
        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
            text = event.text
        if isinstance(event, CallbackQuery) and event.from_user:
            user_id = event.from_user.id

        # Не ограничиваем команды (иначе /start может "молчать" при повторном нажатии).
        if isinstance(event, Message) and text and text.strip().startswith("/"):
            return await handler(event, data)

        if user_id is not None and self.min_interval_seconds > 0:
            now = time.time()
            last = self._last.get(user_id, 0.0)
            if now - last < self.min_interval_seconds:
                # Тихо игнорируем спам (без лишних уведомлений).
                # Для callback можно показать короткое уведомление.
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer("⏳", show_alert=False)
                    except Exception:
                        pass
                return
            self._last[user_id] = now

        return await handler(event, data)


class DebugLogMiddleware(BaseMiddleware):
    """
    Мини-логгер входящих сообщений/колбэков для диагностики (можно убрать позже).
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        import logging

        et = type(event).__name__
        if isinstance(event, Message) and event.from_user:
            logging.info("IN %s chat=%s user=%s text=%r", et, event.chat.type, event.from_user.id, (event.text or "")[:200])
        elif isinstance(event, CallbackQuery) and event.from_user:
            chat_type = event.message.chat.type if event.message else None
            logging.info("IN %s chat=%s user=%s data=%r", et, chat_type, event.from_user.id, (event.data or "")[:200])
        else:
            logging.info("IN %s", et)
        return await handler(event, data)


class BanMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        db: Database | None = data.get("db")
        if db is None:
            return await handler(event, data)

        user_id: int | None = None
        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
        if isinstance(event, CallbackQuery) and event.from_user:
            user_id = event.from_user.id

        if user_id is not None and await db.is_banned(user_id):
            if isinstance(event, Message):
                await event.answer("🚫 Вы заблокированы и не можете пользоваться ботом.")
            if isinstance(event, CallbackQuery):
                await event.answer("🚫 Вы заблокированы.", show_alert=True)
            return

        return await handler(event, data)


class SponsorMiddleware(BaseMiddleware):
    """
    Закрывает доступ к боту, пока пользователь не подписан на все каналы-спонсоры.

    Исключения:
    - /start
    - callback sponsors:check
    - админы
    """

    def __init__(self, admins: list[int]) -> None:
        self.admins = set(int(x) for x in admins)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        db: Database | None = data.get("db")
        bot: Bot | None = data.get("bot")
        if db is None or bot is None:
            return await handler(event, data)

        user_id: int | None = None
        language_code: str | None = None
        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
            language_code = event.from_user.language_code
            if event.text and event.text.strip().startswith("/start"):
                # /start всегда отдаём хендлеру: там регистрация/рефералы и показ "обязательной подписки".
                return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.from_user:
            user_id = event.from_user.id
            language_code = event.from_user.language_code
            if event.data == "sponsors:check":
                return await handler(event, data)

        if user_id is None:
            return await handler(event, data)

        if user_id in self.admins:
            return await handler(event, data)

        sponsor_ids = await db.list_sponsor_channels()
        if not sponsor_ids:
            return await handler(event, data)

        async def send_gate() -> None:
            rows = await db.list_sponsor_channels_full()
            links = await db.list_sponsor_links()
            lines = [
                "🔒 <b>Доступ к боту открывается после подписки на спонсоров.</b>",
                "",
                "📺 Каналы-спонсоры:",
            ]
            for r in rows:
                title = (r["title"] or "").strip()
                username = (r["username"] or "").strip()
                url = (r["url"] or "").strip()
                cid = int(r["chat_id"])
                if url:
                    lines.append(f"• <a href=\"{url}\">{title or url}</a>")
                elif username:
                    lines.append(f"• <a href=\"https://t.me/{username}\">{title or '@' + username}</a>")
                else:
                    lines.append(f"• {title + ' — ' if title else ''}<code>{cid}</code>")
            if links:
                lines += ["", "🔗 Дополнительно:"]
                for l in links[:20]:
                    t = (l["title"] or "").strip()
                    u = str(l["url"]).strip()
                    lines.append(f"• <a href=\"{u}\">{t or u}</a>")
            lines += ["", "После подписки нажмите кнопку ниже:"]

            text = "\n".join(lines)
            if isinstance(event, Message):
                await event.answer(text, reply_markup=sponsors_kb())
            if isinstance(event, CallbackQuery) and event.message:
                await event.message.answer(text, reply_markup=sponsors_kb())

        # Проверяем только те каналы, у которых включена проверка.
        required = [int(r["chat_id"]) for r in await db.list_sponsor_channels_full() if int(r["check_required"]) == 1]
        for chat_id in required:
            try:
                member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
                if member.status in ("left", "kicked"):
                    if isinstance(event, CallbackQuery):
                        await event.answer("🔒 Подпишитесь на всех спонсоров.", show_alert=True)
                    await send_gate()
                    return
            except Exception:
                if isinstance(event, CallbackQuery):
                    await event.answer("🔒 Не удалось проверить подписку. Добавьте бота в канал как администратора.", show_alert=True)
                await send_gate()
                return

        return await handler(event, data)


class MaintenanceMiddleware(BaseMiddleware):
    """
    Режим технических работ:
    - включается через настройки (MAINTENANCE_ENABLED=1)
    - пропускает админов и исключения (MAINTENANCE_EXCEPT_IDS)
    """

    DEFAULT_TEXT = (
        "🛠 <b>Технические работы</b>\n\n"
        "Сейчас бот временно недоступен.\n"
        "Пожалуйста, попробуйте позже."
    )

    def __init__(self, admins: list[int]) -> None:
        self.admins = set(int(x) for x in admins)

    @staticmethod
    def _parse_ids(raw: str | None) -> set[int]:
        if not raw:
            return set()
        out: set[int] = set()
        for part in str(raw).replace(";", ",").split(","):
            p = part.strip()
            if not p:
                continue
            try:
                out.add(int(p))
            except Exception:
                continue
        return out

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        settings: SettingsStore | None = data.get("settings")
        db: Database | None = data.get("db")
        if settings is None or db is None:
            return await handler(event, data)

        # не блокируем успешные оплаты
        if isinstance(event, Message) and event.successful_payment:
            return await handler(event, data)

        user_id: int | None = None
        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
        if isinstance(event, CallbackQuery) and event.from_user:
            user_id = event.from_user.id

        if user_id is None:
            return await handler(event, data)
        if user_id in self.admins:
            return await handler(event, data)

        enabled = False
        try:
            enabled = settings.get_int("MAINTENANCE_ENABLED") == 1
        except Exception:
            enabled = False
        if not enabled:
            return await handler(event, data)

        try:
            exc = self._parse_ids(settings.get_str("MAINTENANCE_EXCEPT_IDS"))
        except Exception:
            exc = set()
        if user_id in exc:
            return await handler(event, data)

        try:
            text = settings.get_str("MAINTENANCE_TEXT")
        except Exception:
            text = self.DEFAULT_TEXT

        if isinstance(event, CallbackQuery):
            try:
                await event.answer("🛠 Техработы", show_alert=True)
            except Exception:
                pass
            if event.message:
                try:
                    await event.message.answer(text)
                except Exception:
                    pass
            return

        if isinstance(event, Message):
            try:
                await event.answer(text)
            except Exception:
                pass
            return

        return


class LastSeenMiddleware(BaseMiddleware):
    """
    Обновляет last_seen_at и username у зарегистрированных пользователей.
    (Если пользователя нет в БД — просто ничего не делает.)
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        db: Database | None = data.get("db")
        if db is None:
            return await handler(event, data)

        user_id: int | None = None
        username: str | None = None
        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
            username = event.from_user.username
        if isinstance(event, CallbackQuery) and event.from_user:
            user_id = event.from_user.id
            username = event.from_user.username

        if user_id is not None:
            try:
                await db.execute(
                    "UPDATE users SET last_seen_at=?, username=? WHERE user_id=?",
                    (int(time.time()), username, int(user_id)),
                )
            except Exception:
                pass

        return await handler(event, data)
