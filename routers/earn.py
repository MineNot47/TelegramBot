from __future__ import annotations

from dataclasses import dataclass
import time
from urllib.parse import urlparse

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
from db import Database
from flyer_client import FlyerClient, FlyerTask
from settings_store import SettingsStore
from states import PromoActivateState
from utils import fmt_user, safe_int

router = Router()

# Кэш заданий Flyer (на пользователя), чтобы работали "Проверить/Пропустить".
_FLYER_CACHE: dict[int, tuple[float, list[str], dict[str, FlyerTask]]] = {}
_LOCAL_CACHE: dict[int, tuple[float, list[int]]] = {}
_TASK_CACHE: dict[int, tuple[float, list[str], dict[str, "_TaskCard"]]] = {}
_CACHE_TTL = 10 * 60


@dataclass(frozen=True)
class _TaskCard:
    key: str  # l:<chat_id> или f:<signature>
    title: str
    link: str | None
    reward: float


def _flyer_difficulty(raw: dict) -> str | None:
    """
    Пытаемся понять "сложность" задания Flyer по данным задания.
    Это эвристика: разные проекты/версии сервиса могут отдавать разные поля.
    """
    for k in ("difficulty", "complexity", "level"):
        v = raw.get(k)
        if v is None:
            continue
        try:
            n = int(float(v))
            if n <= 1:
                return "easy"
            if n == 2:
                return "medium"
            if n >= 3:
                return "hard"
        except Exception:
            pass
        if isinstance(v, str):
            s = v.strip().lower()
            if s in {"easy", "low", "simple"}:
                return "easy"
            if s in {"medium", "normal", "avg", "average"}:
                return "medium"
            if s in {"hard", "high", "difficult"}:
                return "hard"

    for k in ("type", "task_type", "action"):
        v = raw.get(k)
        if not isinstance(v, str):
            continue
        s = v.strip().lower()
        if "boost" in s:
            return "hard"
        if "subscribe" in s or s in {"sub", "subscription"}:
            return "easy"
    return None


def _flyer_action_ru(raw: dict) -> str | None:
    """
    Пытаемся определить тип действия (подписка/лайк/репост/буст) по данным Flyer.
    Возвращает русский текст для отображения на карточке.
    """
    candidates: list[str] = []
    # Явные поля типа действия
    for k in ("type", "task_type", "action", "kind", "category"):
        v = raw.get(k)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            candidates.append(str(int(v)))
        elif isinstance(v, str):
            candidates.append(v.strip())

    # иногда тип лежит глубже
    data = raw.get("data")
    if isinstance(data, dict):
        for k in ("type", "task_type", "action", "kind", "category"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                candidates.append(v.strip())

    # Подстраховка: иногда действие видно в названии/описании
    for k in ("title", "name", "description", "text", "hint", "task", "caption", "channel_name"):
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            candidates.append(v.strip())

    joined = " ".join(candidates).lower()
    if not joined:
        return None

    # Boost / голосование
    if any(x in joined for x in ("boost", "буст", "голос", "vote", "voting")):
        return "Буст / Голосование"
    # Подписка
    if any(
        x in joined
        for x in (
            "sub",
            "subscribe",
            "subscription",
            "join",
            "follow",
            "подпис",
            "пдп",
            "подп",
        )
    ):
        return "Подписка"
    # Лайк/реакция
    if any(
        x in joined
        for x in (
            "like",
            "reaction",
            "react",
            "emoji",
            "лайк",
            "лк",
            "реакц",
            "реакц.",
            "❤️",
            "❤",
        )
    ):
        return "Лайк / Реакция"
    # Репост/поделиться
    if any(
        x in joined
        for x in (
            "repost",
            "share",
            "forward",
            "репост",
            "реп",
            "пересл",
            "поделиться",
        )
    ):
        return "Репост / Поделиться"
    # Комментарий
    if any(x in joined for x in ("comment", "reply", "коммент", "комм", "ком", "ответ")):
        return "Комментарий"
    # Просмотр
    if any(x in joined for x in ("view", "watch", "просмотр", "viewing")):
        return "Просмотр"

    return None


def _flyer_reward(raw: dict, settings: SettingsStore) -> float:
    diff = _flyer_difficulty(raw)
    if diff == "easy":
        return settings.get_float("FLYER_REWARD_EASY")
    if diff == "medium":
        return settings.get_float("FLYER_REWARD_MEDIUM")
    if diff == "hard":
        return settings.get_float("FLYER_REWARD_HARD")
    # Если сервис не прислал сложность — отдельная настройка.
    return settings.get_float("FLYER_REWARD_UNKNOWN")


def _cache_flyer_set(user_id: int, tasks: list[FlyerTask]) -> None:
    order = [t.signature for t in tasks]
    mapping = {t.signature: t for t in tasks}
    _FLYER_CACHE[user_id] = (time.time(), order, mapping)


def _cache_flyer_get(user_id: int, signature: str) -> FlyerTask | None:
    item = _FLYER_CACHE.get(user_id)
    if not item:
        return None
    ts, _order, mapping = item
    if time.time() - ts > _CACHE_TTL:
        _FLYER_CACHE.pop(user_id, None)
        return None
    return mapping.get(signature)


def _cache_flyer_next(user_id: int, current_signature: str) -> str | None:
    item = _FLYER_CACHE.get(user_id)
    if not item:
        return None
    ts, order, _mapping = item
    if time.time() - ts > _CACHE_TTL:
        _FLYER_CACHE.pop(user_id, None)
        return None
    if not order:
        return None
    try:
        i = order.index(current_signature)
        return order[(i + 1) % len(order)]
    except ValueError:
        return order[0]


def _cache_local_set(user_id: int, chat_ids: list[int]) -> None:
    _LOCAL_CACHE[user_id] = (time.time(), chat_ids)


def _cache_local_next(user_id: int, current_chat_id: int) -> int | None:
    item = _LOCAL_CACHE.get(user_id)
    if not item:
        return None
    ts, order = item
    if time.time() - ts > _CACHE_TTL:
        _LOCAL_CACHE.pop(user_id, None)
        return None
    if not order:
        return None
    try:
        i = order.index(current_chat_id)
        return order[(i + 1) % len(order)]
    except ValueError:
        return order[0]


async def _check_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return m.status not in ("left", "kicked")
    except Exception:
        return False


def _normalize_link(link: str | None) -> str | None:
    if not link:
        return None
    s = link.strip().strip('"').strip("'")
    s = s.replace("\\/", "/").replace("\\\\", "\\")
    if any(ch.isspace() for ch in s):
        s = s.split()[0].strip()
    if not s:
        return None
    if s.startswith("https:\\") or s.startswith("http:\\"):
        s = s.replace("\\", "/")
    if s.startswith("@"):
        return f"https://t.me/{s.lstrip('@')}"
    if s.startswith("+"):
        return f"https://t.me/{s}"
    if s.startswith("joinchat/"):
        return "https://t.me/" + s
    if s.startswith("t.me/") or s.startswith("telegram.me/"):
        return "https://" + s
    if s.startswith("www.t.me/"):
        return "https://" + s[len("www.") :]
    if "://" not in s:
        # Иногда сервисы отдают просто username без @
        if s.replace("_", "").isalnum() and 4 < len(s) < 80:
            return f"https://t.me/{s}"
    return s


def _is_url_ok(url: str | None) -> bool:
    if not url:
        return False
    u = url.strip()
    if u.startswith("tg://"):
        return True
    try:
        p = urlparse(u)
    except Exception:
        return False
    return p.scheme in {"http", "https"} and bool(p.netloc)


def _telegram_deeplink(url: str | None) -> str | None:
    """
    Некоторые клиенты Telegram игнорируют кривые/редиректные URL-кнопки.
    Делаем запасной deep-link для Telegram.
    """
    url = _normalize_link(url)
    if not url:
        return None
    if url.startswith("tg://"):
        return url
    if url.startswith("https://t.me/+"):
        code = url.split("https://t.me/+", 1)[1].split("?", 1)[0].split("#", 1)[0]
        if code:
            return f"tg://join?invite={code}"
    if url.startswith("https://t.me/joinchat/"):
        code = url.split("https://t.me/joinchat/", 1)[1].split("?", 1)[0].split("#", 1)[0]
        if code:
            return f"tg://join?invite={code}"
    if url.startswith("https://t.me/"):
        part = url.split("https://t.me/", 1)[1].split("?", 1)[0].split("#", 1)[0].strip("/")
        if part and part.replace("_", "").isalnum():
            return f"tg://resolve?domain={part}"
    return None


def _tasks_keyboard(items: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for text, cb in items:
        kb.button(text=text, callback_data=cb)
    kb.adjust(1)
    return kb.as_markup()


def _task_card_kb(link: str | None, check_cb: str, skip_cb: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    link = _normalize_link(link)
    if _is_url_ok(link):
        kb.row(InlineKeyboardButton(text="📺 Подписаться", url=link))
    else:
        kb.row(InlineKeyboardButton(text="📺 Подписаться", callback_data="task:nolink"))
    kb.row(InlineKeyboardButton(text="✅ Проверить", callback_data=check_cb))
    kb.row(InlineKeyboardButton(text="⏭ Пропустить", callback_data=skip_cb))
    return kb.as_markup()


def _cache_task_set(user_id: int, cards: list[_TaskCard]) -> None:
    order = [c.key for c in cards]
    mapping = {c.key: c for c in cards}
    _TASK_CACHE[user_id] = (time.time(), order, mapping)


def _cache_task_get(user_id: int, key: str) -> _TaskCard | None:
    item = _TASK_CACHE.get(user_id)
    if not item:
        return None
    ts, _order, mapping = item
    if time.time() - ts > _CACHE_TTL:
        _TASK_CACHE.pop(user_id, None)
        return None
    return mapping.get(key)


def _cache_task_next(user_id: int, current_key: str) -> str | None:
    item = _TASK_CACHE.get(user_id)
    if not item:
        return None
    ts, order, _mapping = item
    if time.time() - ts > _CACHE_TTL:
        _TASK_CACHE.pop(user_id, None)
        return None
    if not order:
        return None
    try:
        i = order.index(current_key)
        return order[(i + 1) % len(order)]
    except ValueError:
        return order[0]


@router.callback_query(F.data == "tasks:menu")
async def tasks_menu(callback: CallbackQuery, bot: Bot, db: Database, flyer: FlyerClient | None, settings: SettingsStore) -> None:
    if not callback.from_user or not callback.message:
        return
    await callback.answer()

    cards: list[_TaskCard] = []

    # Локальные задания (добавленные админом).
    rows = await db.list_task_channels_full()
    for r in (rows or [])[:30]:
        chat_id = int(r["chat_id"])
        username = (r["username"] or "").strip()
        title = (r["title"] or "").strip() or (("@" + username) if username else str(chat_id))
        link = (r["url"] or "").strip() or (f"https://t.me/{username}" if username else None)
        reward = float(r["reward"]) if r["reward"] is not None else settings.get_float("TASK_REWARD")
        cards.append(_TaskCard(key=f"l:{chat_id}", title=title, link=link, reward=reward))

    # Задания Flyer (если ключ задан).
    if flyer is not None:
        flyer_tasks = await flyer.get_tasks(
            user_id=callback.from_user.id,
            language_code=callback.from_user.language_code,
            limit=config.FLYER_TASKS_LIMIT,
        )
        if flyer_tasks:
            _cache_flyer_set(callback.from_user.id, flyer_tasks)
            for t in flyer_tasks:
                title = t.title or "Задание"
                reward = _flyer_reward(t.raw, settings)
                cards.append(_TaskCard(key=f"f:{t.signature}", title=title, link=t.link, reward=reward))
                await db.upsert_flyer_task_meta(signature=t.signature, reward=reward, title=title, link=t.link)

    if not cards:
        await callback.message.answer("📋 Заданий пока нет. Загляните позже.")
        return

    _cache_task_set(callback.from_user.id, cards)
    await callback.message.answer("📋 <b>Задания</b>")
    await _send_task_card(callback.message, user_id=callback.from_user.id, key=cards[0].key, bot=bot, db=db, flyer=flyer)


async def _send_task_card(
    message: Message,
    *,
    user_id: int,
    key: str,
    bot: Bot,
    db: Database,
    flyer: FlyerClient | None,
) -> None:
    card = _cache_task_get(user_id, key)
    if card is None:
        await message.answer("⏳ Список заданий устарел. Нажмите «Заработать» ещё раз.")
        return

    action_line = ""
    if key.startswith("f:"):
        signature = key.split(":", 1)[1]
        ft = _cache_flyer_get(user_id, signature)
        if ft is not None:
            action = _flyer_action_ru(ft.raw)
            if action:
                action_line = f"🧩 Действие: <b>{action}</b>\n\n"

    # Для Flyer-заданий делаем "Подписаться" через callback, чтобы отдать пользователю
    # запасную ссылку/кнопки (некоторые задачи приходят со странными URL).
    if key.startswith("f:"):
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="📺 Подписаться", callback_data=f"task:go:{card.key}"))
        kb.row(InlineKeyboardButton(text="✅ Проверить", callback_data=f"task:check:{card.key}"))
        kb.row(InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"task:skip:{card.key}"))
        reply_markup = kb.as_markup()
    else:
        reply_markup = _task_card_kb(link=card.link, check_cb=f"task:check:{card.key}", skip_cb=f"task:skip:{card.key}")
    await message.answer(
        f"📌 <b>{card.title}</b>\n\n"
        f"{action_line}"
        f"Выполняйте это задание и получите <b>{card.reward:.2f}</b> звезд.\n"
        "Нажмите «Подписаться», затем «Проверить».",
        reply_markup=reply_markup,
    )


@router.callback_query(F.data.startswith("task:go:"))
async def task_go(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        return
    await callback.answer()
    key = callback.data.split(":", 2)[-1]
    card = _cache_task_get(callback.from_user.id, key)
    if card is None:
        await callback.message.answer("⏳ Список заданий устарел. Нажмите «Заработать» ещё раз.")
        return
    link = _normalize_link(card.link)
    if not link:
        await callback.message.answer("❌ В этом задании нет ссылки. Сообщите администратору.")
        return

    extra = ""
    if card.key.startswith("f:"):
        signature = card.key.split(":", 1)[1]
        ft = _cache_flyer_get(callback.from_user.id, signature)
        if ft is not None:
            action = _flyer_action_ru(ft.raw)
            if action:
                extra = f"\n🧩 Действие: <b>{action}</b>"

    kb = InlineKeyboardBuilder()
    deep = _telegram_deeplink(link)
    if deep and deep != link:
        kb.row(InlineKeyboardButton(text="📲 Открыть в Telegram", url=deep))
    kb.row(InlineKeyboardButton(text="🌐 Открыть ссылку", url=link))
    await callback.message.answer(f"🔗 Ссылка на канал/ресурс:\n{link}{extra}", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("task:skip:"))
async def task_skip(callback: CallbackQuery, bot: Bot, db: Database, flyer: FlyerClient | None) -> None:
    if not callback.from_user or not callback.message:
        return
    await callback.answer()
    key = callback.data.split(":", 2)[-1]
    nxt = _cache_task_next(callback.from_user.id, key)
    if nxt is None:
        await callback.message.answer("⏳ Список заданий устарел. Нажмите «Заработать» ещё раз.")
        return
    await _send_task_card(callback.message, user_id=callback.from_user.id, key=nxt, bot=bot, db=db, flyer=flyer)


@router.callback_query(F.data.startswith("task:check:"))
async def task_check(
    callback: CallbackQuery,
    bot: Bot,
    db: Database,
    flyer: FlyerClient | None,
    settings: SettingsStore,
) -> None:
    if not callback.from_user or not callback.message:
        return

    user = await db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Нажмите /start", show_alert=True)
        return
    if await db.is_balance_frozen(user.user_id):
        await callback.answer("🧊 Баланс заморожен.", show_alert=True)
        return

    key = callback.data.split(":", 2)[-1]
    if key.startswith("l:"):
        chat_id = safe_int(key.split(":", 1)[1])
        if chat_id is None:
            await callback.answer("Ошибка задания.", show_alert=True)
            return
        if await db.is_task_done(user.user_id, chat_id):
            await callback.answer("✅ Уже выполнено.", show_alert=True)
            return
        row = await db.get_task_channel(chat_id)
        check_required = int(row["check_required"]) == 1 if row else True
        if check_required:
            ok = await _check_member(bot, chat_id, user.user_id)
            if not ok:
                await callback.answer("❌ Не выполнено. Проверьте подписку.", show_alert=True)
                return
        reward = float(row["reward"]) if row and row["reward"] is not None else settings.get_float("TASK_REWARD")
        await db.mark_task_done(user.user_id, chat_id)
        await db.change_balance(user.user_id, reward)
        referrer_id = await db.try_reward_referral(
            user.user_id,
            ref_reward=settings.get_float("REF_REWARD"),
            ref_bonus=None,
        )
        await callback.answer("✅ Засчитано!")
        await callback.message.answer(f"🎉 Задание выполнено!\n💰 Начислено: <b>+{reward:.2f}</b>")
        if referrer_id is not None:
            try:
                await bot.send_message(
                    referrer_id,
                    f"✅ Ваш реферал {fmt_user(user.username, user.user_id)} выполнил 2 задания!\n"
                    f"💰 Начислено: <b>+{settings.get_float('REF_REWARD'):.2f}</b>",
                )
            except Exception:
                pass
        return

    if key.startswith("f:"):
        if flyer is None:
            await callback.answer("FlyerAPI не подключён.", show_alert=True)
            return
        signature = key.split(":", 1)[1]
        if await db.is_flyer_task_done(user.user_id, signature):
            await callback.answer("✅ Уже выполнено.", show_alert=True)
            return
        result = await flyer.check_task(user_id=user.user_id, signature=signature)
        ok, status = _flyer_status_ok(result)
        if not ok:
            suffix = f" (статус: {status})" if status else ""
            await callback.answer("❌ Не выполнено" + suffix, show_alert=True)
            return
        card = _cache_task_get(user.user_id, key)
        if card is not None:
            reward = float(card.reward)
        else:
            reward = await db.get_flyer_task_reward(signature)
            if reward is None:
                # если мета нет — выдаём UNKNOWN
                reward = settings.get_float("FLYER_REWARD_UNKNOWN")
        await db.mark_flyer_task_done(user.user_id, signature)
        await db.change_balance(user.user_id, reward)
        referrer_id = await db.try_reward_referral(
            user.user_id,
            ref_reward=settings.get_float("REF_REWARD"),
            ref_bonus=None,
        )
        await callback.answer("✅ Засчитано!")
        await callback.message.answer(f"🎉 Задание выполнено!\n💰 Начислено: <b>+{reward:.2f}</b>")
        if referrer_id is not None:
            try:
                await bot.send_message(
                    referrer_id,
                    f"✅ Ваш реферал {fmt_user(user.username, user.user_id)} выполнил 2 задания!\n"
                    f"💰 Начислено: <b>+{settings.get_float('REF_REWARD'):.2f}</b>",
                )
            except Exception:
                pass
        return

    await callback.answer("Ошибка задания.", show_alert=True)


@router.callback_query(F.data.startswith("ltask:open:"))
async def local_task_open(callback: CallbackQuery, bot: Bot, db: Database, settings: SettingsStore) -> None:
    if not callback.from_user or not callback.message:
        return
    await callback.answer()
    chat_id = safe_int(callback.data.split(":")[-1])
    if chat_id is None:
        await callback.message.answer("Ошибка задания.")
        return

    await _send_local_task_card(callback.message, bot=bot, db=db, settings=settings, chat_id=chat_id)


@router.callback_query(F.data.startswith("ltask:check:"))
async def local_task_check(callback: CallbackQuery, bot: Bot, db: Database, settings: SettingsStore) -> None:
    if not callback.from_user or not callback.message:
        return
    user = await db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Нажмите /start", show_alert=True)
        return
    if await db.is_balance_frozen(user.user_id):
        await callback.answer("🧊 Баланс заморожен.", show_alert=True)
        return

    chat_id = safe_int(callback.data.split(":")[-1])
    if chat_id is None:
        await callback.answer("Ошибка задания.", show_alert=True)
        return

    if await db.is_task_done(user.user_id, chat_id):
        await callback.answer("✅ Уже выполнено.", show_alert=True)
        return

    row = await db.get_task_channel(chat_id)
    check_required = int(row["check_required"]) == 1 if row else True
    if check_required:
        ok = await _check_member(bot, chat_id, user.user_id)
        if not ok:
            await callback.answer("❌ Не выполнено. Проверьте подписку.", show_alert=True)
            return

    reward = float(row["reward"]) if row and row["reward"] is not None else settings.get_float("TASK_REWARD")
    await db.mark_task_done(user.user_id, chat_id)
    await db.change_balance(user.user_id, reward)
    referrer_id = await db.try_reward_referral(
        user.user_id,
        ref_reward=settings.get_float("REF_REWARD"),
        ref_bonus=None,
    )
    await callback.answer("✅ Засчитано!")
    await callback.message.answer(f"🎉 Задание выполнено!\n💰 Начислено: <b>+{reward:.2f}</b>")
    if referrer_id is not None:
        try:
            await bot.send_message(
                referrer_id,
                f"✅ Ваш реферал {fmt_user(user.username, user.user_id)} выполнил 2 задания!\n"
                f"💰 Начислено: <b>+{settings.get_float('REF_REWARD'):.2f}</b>",
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("ftask:open:"))
async def flyer_task_open(callback: CallbackQuery, db: Database, flyer: FlyerClient | None, settings: SettingsStore) -> None:
    if not callback.from_user or not callback.message:
        return
    await callback.answer()
    if flyer is None:
        await callback.message.answer("FlyerAPI не подключён.")
        return

    signature = callback.data.split(":", 2)[-1]
    await _send_flyer_task_card(callback.message, user_id=callback.from_user.id, signature=signature, flyer=flyer, settings=settings)


def _flyer_status_ok(result: object) -> tuple[bool, str | None]:
    # Поддерживаем разные форматы ответа.
    if result is True:
        return True, None
    if isinstance(result, dict):
        s = result.get("status")
        if s is True:
            return True, None
        if isinstance(s, str):
            sl = s.lower()
            if sl in {"success", "done", "completed", "complete", "ok", "passed"}:
                return True, s
            return False, s
    if isinstance(result, str):
        sl = result.lower()
        if sl in {"success", "done", "completed", "complete", "ok", "passed"}:
            return True, result
        return False, result
    return False, None


@router.callback_query(F.data.startswith("ftask:check:"))
async def flyer_task_check(
    callback: CallbackQuery,
    bot: Bot,
    db: Database,
    flyer: FlyerClient | None,
    settings: SettingsStore,
) -> None:
    if not callback.from_user or not callback.message:
        return
    if flyer is None:
        await callback.answer("FlyerAPI не подключён.", show_alert=True)
        return

    user = await db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Нажмите /start", show_alert=True)
        return
    if await db.is_balance_frozen(user.user_id):
        await callback.answer("🧊 Баланс заморожен.", show_alert=True)
        return

    signature = callback.data.split(":", 2)[-1]
    if await db.is_flyer_task_done(user.user_id, signature):
        await callback.answer("✅ Уже выполнено.", show_alert=True)
        return

    result = await flyer.check_task(user_id=user.user_id, signature=signature)
    ok, status = _flyer_status_ok(result)
    if not ok:
        suffix = f" (статус: {status})" if status else ""
        await callback.answer("❌ Не выполнено" + suffix, show_alert=True)
        return

    task = _cache_flyer_get(user.user_id, signature)
    reward = await db.get_flyer_task_reward(signature)
    if reward is None:
        reward = _flyer_reward(task.raw if task else {}, settings)
        await db.upsert_flyer_task_meta(
            signature=signature,
            reward=reward,
            title=(task.title if task else None),
            link=(task.link if task else None),
        )
    await db.mark_flyer_task_done(user.user_id, signature)
    await db.change_balance(user.user_id, reward)
    referrer_id = await db.try_reward_referral(
        user.user_id,
        ref_reward=settings.get_float("REF_REWARD"),
        ref_bonus=None,
    )
    await callback.answer("✅ Засчитано!")
    await callback.message.answer(f"🎉 Задание выполнено!\n💰 Начислено: <b>+{reward:.2f}</b>")
    if referrer_id is not None:
        try:
            await bot.send_message(
                referrer_id,
                f"✅ Ваш реферал {fmt_user(user.username, user.user_id)} выполнил 2 задания!\n"
                f"💰 Начислено: <b>+{settings.get_float('REF_REWARD'):.2f}</b>",
            )
        except Exception:
            pass
        try:
            await bot.send_message(
                user.user_id,
                f"🎁 Реферальный бонус начислен: <b>+{settings.get_float('REF_BONUS'):.2f}</b>",
            )
        except Exception:
            pass


@router.callback_query(F.data == "task:nolink")
async def task_no_link(callback: CallbackQuery) -> None:
    await callback.answer("❌ Ссылка на задание не задана. Сообщите администратору.", show_alert=True)


async def _send_local_task_card(
    message: Message,
    *,
    bot: Bot | None,
    db: Database,
    settings: SettingsStore | None,
    chat_id: int,
) -> None:
    row = await db.get_task_channel(chat_id)
    if not row:
        await message.answer("Задание не найдено.")
        return

    username = (row["username"] or "").strip()
    title = (row["title"] or "").strip() or (("@" + username) if username else str(chat_id))
    link = (row["url"] or "").strip() or (f"https://t.me/{username}" if username else None)
    reward = float(row["reward"]) if row["reward"] is not None else (settings.get_float("TASK_REWARD") if settings else 0.0)
    await message.answer(
        f"📌 <b>{title}</b>\n\n"
        f"Выполняйте это задание и получите <b>{reward:.2f}</b> звезд.\n"
        "Нажмите «Подписаться», затем «Проверить».",
        reply_markup=_task_card_kb(link=link, check_cb=f"ltask:check:{chat_id}", skip_cb=f"ltask:skip:{chat_id}"),
    )


async def _send_flyer_task_card(
    message: Message,
    *,
    user_id: int,
    signature: str,
    flyer: FlyerClient,
    settings: SettingsStore | None = None,
) -> None:
    task = _cache_flyer_get(user_id, signature)
    if task is None:
        await message.answer("⏳ Список заданий устарел. Нажмите «Показать задания» ещё раз.")
        return
    reward = settings.get_float("TASK_REWARD") if settings else 0.0
    title = task.title or "Задание"
    await message.answer(
        f"📌 <b>{title}</b>\n\n"
        f"Выполняйте это задание и получите <b>{reward:.2f}</b> звезд.\n"
        "Нажмите «Подписаться», затем «Проверить».",
        reply_markup=_task_card_kb(link=task.link, check_cb=f"ftask:check:{task.signature}", skip_cb=f"ftask:skip:{task.signature}"),
    )


@router.callback_query(F.data.startswith("ltask:skip:"))
async def local_task_skip(callback: CallbackQuery, bot: Bot, db: Database, settings: SettingsStore) -> None:
    if not callback.from_user or not callback.message:
        return
    await callback.answer()
    chat_id = safe_int(callback.data.split(":")[-1])
    if chat_id is None:
        return
    nxt = _cache_local_next(callback.from_user.id, chat_id)
    if nxt is None:
        await callback.message.answer("⏳ Список заданий устарел. Нажмите «Показать задания» ещё раз.")
        return
    await _send_local_task_card(callback.message, bot=bot, db=db, settings=settings, chat_id=nxt)


@router.callback_query(F.data.startswith("ftask:skip:"))
async def flyer_task_skip(callback: CallbackQuery, db: Database, flyer: FlyerClient | None, settings: SettingsStore) -> None:
    if not callback.from_user or not callback.message:
        return
    if flyer is None:
        return
    await callback.answer()
    sig = callback.data.split(":", 2)[-1]
    nxt = _cache_flyer_next(callback.from_user.id, sig)
    if nxt is None:
        await callback.message.answer("⏳ Список заданий устарел. Нажмите «Показать задания» ещё раз.")
        return
    await _send_flyer_task_card(callback.message, user_id=callback.from_user.id, signature=nxt, flyer=flyer, settings=settings)


# ---- Промокод (ввод пользователем) ----
@router.message(PromoActivateState.waiting_code)
async def promo_activate(message: Message, state: FSMContext, db: Database) -> None:
    if not message.from_user:
        return
    if not await db.get_user(message.from_user.id):
        await message.answer("Нажмите /start для регистрации.")
        await state.clear()
        return
    if await db.is_balance_frozen(message.from_user.id):
        await message.answer("🧊 Баланс заморожен. Активация промокодов недоступна.")
        await state.clear()
        return
    code = (message.text or "").strip()
    if not code:
        await message.answer("Введите промокод текстом.")
        return

    ok, msg, reward = await db.activate_promo(message.from_user.id, code)
    if ok:
        await message.answer(f"✅ Промокод активирован!\n💰 Начислено: <b>+{reward:.2f}</b>")
    else:
        # msg уже: "Не существует" / "Закончился" / ...
        await message.answer(msg)
    await state.clear()
