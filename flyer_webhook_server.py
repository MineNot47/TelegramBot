from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web
from aiogram import Bot

from db import Database
from settings_store import SettingsStore

logger = logging.getLogger(__name__)


def _status_ok(value: Any) -> bool:
    if value is True:
        return True
    if value is None:
        return False
    if isinstance(value, str):
        s = value.strip().lower()
        return s in {"success", "done", "completed", "complete", "ok", "passed", "finish", "finished"}
    return False


async def _handle_event(
    payload: dict[str, Any],
    *,
    db: Database,
    settings: SettingsStore,
    bot: Bot | None,
) -> None:
    """
    Формат вебхука (по документации flyer): {"type": "...", "key_number": 123, "data": {...}}
    Мы обрабатываем только то, что можем:
    - если в data есть user_id + signature + status=успех => зачисляем награду.
    Остальные события просто логируем.
    """
    event_type = str(payload.get("type") or "").strip()
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if not isinstance(data, dict):
        data = {}

    user_id = data.get("user_id")
    signature = data.get("signature")
    status = data.get("status")

    logger.info("Flyer webhook: type=%s data_keys=%s", event_type, sorted(list(data.keys()))[:30])

    try:
        user_id_int = int(user_id) if user_id is not None else None
    except Exception:
        user_id_int = None
    signature_str = str(signature).strip() if signature is not None else ""

    if user_id_int and signature_str and _status_ok(status):
        if await db.is_balance_frozen(user_id_int):
            logger.info("Flyer webhook: user %s frozen, skip credit for signature=%s", user_id_int, signature_str)
            return
        if not await db.is_flyer_task_done(user_id_int, signature_str):
            reward = await db.get_flyer_task_reward(signature_str)
            if reward is None:
                reward = settings.get_float("FLYER_REWARD_UNKNOWN")
            await db.mark_flyer_task_done(user_id_int, signature_str)
            await db.change_balance(user_id_int, reward)
            logger.info("Flyer webhook: credited user_id=%s signature=%s reward=%.2f", user_id_int, signature_str, reward)
            referrer_id = await db.try_reward_referral(
                user_id_int,
                ref_reward=settings.get_float("REF_REWARD"),
                ref_bonus=None,
            )
            if bot is not None:
                try:
                    await bot.send_message(
                        user_id_int,
                        f"✅ Задание засчитано!\n💰 Начислено: <b>+{reward:.2f}</b>",
                    )
                except Exception:
                    pass
                if referrer_id is not None:
                    try:
                        await bot.send_message(
                            referrer_id,
                            f"✅ Ваш реферал <code>{user_id_int}</code> выполнил 2 задания!\n"
                            f"💰 Начислено: <b>+{settings.get_float('REF_REWARD'):.2f}</b>",
                        )
                    except Exception:
                        pass


def create_app(
    *,
    db: Database,
    settings: SettingsStore,
    bot: Bot | None,
    secret: str,
) -> web.Application:
    app = web.Application()

    async def handle(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            body = await request.read()
            logger.warning("Flyer webhook: invalid json (%s bytes)", len(body))
            return web.json_response({"status": False}, status=400)

        if not isinstance(payload, dict):
            return web.json_response({"status": False}, status=400)

        await _handle_event(payload, db=db, settings=settings, bot=bot)
        return web.json_response({"status": True})

    # Секрет прячем в URL, чтобы случайные запросы не долбили endpoint.
    app.router.add_post(f"/flyer/webhook/{secret}", handle)
    return app
