from __future__ import annotations

import time

import logging
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.filters.command import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    PreCheckoutQuery,
    Message,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
import re
from config import ADMINS
from db import Database
from keyboards import main_menu, sponsors_kb, tasks_entry_kb, withdraw_menu
from settings_store import SettingsStore
from states import DonateState, PromoActivateState
from utils import fmt_user, safe_int, ts_to_date
from urllib.parse import quote_plus

router = Router()


async def _is_admin(user_id: int) -> bool:
    return user_id in ADMINS


async def _require_sponsors(bot: Bot, db: Database, user_id: int) -> tuple[bool, list[int]]:
    sponsor_ids = await db.list_sponsor_channels()
    if not sponsor_ids:
        return True, []

    # Админы могут проходить без подписок.
    if await _is_admin(user_id):
        return True, sponsor_ids

    for chat_id in sponsor_ids:
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if member.status in ("left", "kicked"):
                return False, sponsor_ids
        except Exception:
            # Если бот не видит канал/не админ — считаем, что доступа нет.
            return False, sponsor_ids
    return True, sponsor_ids


async def _send_sponsor_gate(message: Message, sponsor_ids: list[int]) -> None:
    lines = [
        "🔒 <b>Доступ к боту открывается после подписки на спонсоров.</b>",
        "",
        "📺 Каналы-спонсоры:",
    ]
    for cid in sponsor_ids:
        lines.append(f"• <code>{cid}</code>")
    lines += ["", "После подписки нажмите кнопку ниже:"]
    await message.answer("\n".join(lines), reply_markup=sponsors_kb())


async def _require_sponsors2(bot: Bot, db: Database, user_id: int) -> tuple[bool, list[dict], list[dict]]:
    sponsor_rows = await db.list_sponsor_channels_full()
    link_rows = await db.list_sponsor_links()
    sponsors = [
        {
            "chat_id": int(r["chat_id"]),
            "title": r["title"],
            "username": r["username"],
            "url": r["url"],
            "check_required": int(r["check_required"]),
        }
        for r in sponsor_rows
    ]
    links = [{"id": int(l["id"]), "title": l["title"], "url": l["url"]} for l in link_rows]

    sponsor_ids = [int(s["chat_id"]) for s in sponsors]
    if not sponsor_ids and not links:
        return True, [], []

    if await _is_admin(user_id):
        return True, sponsors, links

    required = [int(s["chat_id"]) for s in sponsors if int(s["check_required"]) == 1]
    for chat_id in required:
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if member.status in ("left", "kicked"):
                return False, sponsors, links
        except Exception:
            return False, sponsors, links
    return True, sponsors, links


async def _send_sponsor_gate2(message: Message, sponsors: list[dict], links: list[dict]) -> None:
    lines = [
        "🔒 <b>Доступ к боту открывается после подписки на спонсоров.</b>",
        "",
        "📺 Каналы-спонсоры:",
    ]
    for s in sponsors:
        cid = int(s["chat_id"])
        title = (s.get("title") or "").strip()
        username = (s.get("username") or "").strip()
        url = (s.get("url") or "").strip()
        if url:
            lines.append(f"• <a href=\"{url}\">{title or url}</a>")
        elif username:
            lines.append(f"• <a href=\"https://t.me/{username}\">{title or '@' + username}</a>")
        else:
            lines.append(f"• {title + ' — ' if title else ''}<code>{cid}</code>")
    if links:
        lines += ["", "🔗 Дополнительно:"]
        for l in links[:20]:
            u = str(l.get("url") or "").strip()
            t = (l.get("title") or "").strip()
            if u:
                lines.append(f"• <a href=\"{u}\">{t or u}</a>")
    lines += ["", "После подписки нажмите кнопку ниже:"]
    await message.answer("\n".join(lines), reply_markup=sponsors_kb())


@router.message(Command("start"))
async def start(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: SettingsStore,
    bot_username: str,
) -> None:
    if not message.from_user:
        return

    # Бот рассчитан на личные сообщения. В группах/каналах клавиатура может не отображаться.
    if message.chat.type != "private":
        await message.answer(
            "👋 Напишите мне в личные сообщения, чтобы пользоваться ботом:\n"
            f"https://t.me/{bot_username}"
        )
        return

    user_id = message.from_user.id
    username = message.from_user.username
    logging.info("HANDLE /start user=%s chat=%s args=%r", user_id, message.chat.type, command.args)

    # Реферал из deep-link /start <id>
    referrer_id: int | None = None
    if command.args:
        try:
            candidate = int(command.args.strip())
            if candidate != user_id:
                referrer_id = candidate
        except Exception:
            referrer_id = None

    created, _ = await db.upsert_user(user_id, username, referrer_id=None, now=int(time.time()))

    # Если пользователь новый — пытаемся привязать реферала (наградим позже, после заданий).
    if created and referrer_id is not None:
        ref_user = await db.get_user(referrer_id)
        if ref_user:
            # Привязываем реферала (чтобы нельзя было поменять позже).
            await db.execute("UPDATE users SET referrer_id=? WHERE user_id=?", (referrer_id, user_id))
            await db.add_referral(user_id, referrer_id, required_tasks=2)
            await db.execute("UPDATE users SET invited_count = invited_count + 1 WHERE user_id=?", (referrer_id,))

            try:
                await bot.send_message(
                    referrer_id,
                    "👥 По вашей ссылке перешёл новый пользователь!\n"
                    f"🆕 Реферал: {fmt_user(username, user_id)}\n\n"
                    "⏳ Награда будет начислена после того, как он выполнит <b>2 задания</b>.",
                )
            except Exception:
                pass

    ok, sponsors, links = await _require_sponsors2(bot, db, user_id)
    if not ok:
        await _send_sponsor_gate2(message, sponsors, links)
        return

    is_admin = await _is_admin(user_id)
    await message.answer(
        "👋 <b>Добро пожаловать!</b>\nВыберите действие в меню ниже.",
        reply_markup=main_menu(is_admin=is_admin),
    )


def _normalize_slash_commands(text: str) -> str:
    # Иногда пользователи вставляют "похожий" слэш (например, fullwidth '／').
    return (
        text.replace("／", "/")  # U+FF0F
        .replace("⁄", "/")  # U+2044
        .strip()
    )


# Если пользователь пишет "Старт"/"Start" текстом.
@router.message(F.text.casefold().in_({"старт", "start"}))
async def start_word(message: Message, bot: Bot, db: Database, settings: SettingsStore, bot_username: str) -> None:
    class _Cmd:
        args = None

    await start(message=message, command=_Cmd(), bot=bot, db=db, settings=settings, bot_username=bot_username)


# /start с возможным нестандартным слэшем (например, "／start").
@router.message(F.text.regexp(r"^[\/／⁄]start(\b|@)", flags=re.IGNORECASE))
async def start_weird_slash(message: Message, bot: Bot, db: Database, settings: SettingsStore, bot_username: str) -> None:
    norm = _normalize_slash_commands(message.text or "")
    if not norm.lower().startswith("/start"):
        return
    cmd = norm.split(maxsplit=1)[0]  # /start или /start@Bot
    args_part = norm[len(cmd) :].strip()

    class _Cmd:
        args = args_part or None

    await start(message=message, command=_Cmd(), bot=bot, db=db, settings=settings, bot_username=bot_username)


@router.callback_query(F.data == "sponsors:check")
async def sponsors_check(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    if not callback.from_user:
        return
    if callback.message and callback.message.chat.type != "private":
        await callback.answer("Откройте бота в личных сообщениях.", show_alert=True)
        return
    ok, sponsors, links = await _require_sponsors2(bot, db, callback.from_user.id)
    if ok:
        await callback.answer("✅ Подписка подтверждена!")
        await callback.message.answer(
            "✅ Доступ открыт. Используйте меню ниже.",
            reply_markup=main_menu(is_admin=callback.from_user.id in ADMINS),
        )
    else:
        await callback.answer("❌ Вы не подписаны на всех спонсоров.", show_alert=True)
        if callback.message:
            await _send_sponsor_gate2(callback.message, sponsors, links)


@router.message(F.text == "⭐ Личный кабинет")
async def cabinet(message: Message, bot: Bot, db: Database, bot_username: str) -> None:
    if not message.from_user:
        return
    if message.chat.type != "private":
        await message.answer("Пожалуйста, используйте бота в личных сообщениях.")
        return
    ok, sponsors, links = await _require_sponsors2(bot, db, message.from_user.id)
    if not ok:
        await _send_sponsor_gate2(message, sponsors, links)
        return

    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("Похоже, вы ещё не зарегистрированы. Нажмите /start")
        return
    frozen = await db.is_balance_frozen(user.user_id)

    ref_link = f"https://t.me/{bot_username}?start={user.user_id}"
    active_24h = "✅" if int(time.time()) - int(user.last_seen_at) <= 24 * 3600 else "❌"
    text = (
        "⭐ <b>Личный кабинет</b>\n\n"
        f"🆔 ID: <code>{user.user_id}</code>\n"
        f"👤 Username: {fmt_user(user.username, user.user_id)}\n"
        f"💰 Баланс: <b>{user.balance:.2f}</b>\n"
        f"🧊 Заморозка: <b>{'ДА' if frozen else 'НЕТ'}</b>\n"
        f"📅 Регистрация: <b>{ts_to_date(user.registered_at)}</b>\n"
        f"🟢 Активен (24ч): <b>{active_24h}</b>\n"
        f"👥 Рефералы: <b>{user.invited_count}</b>\n\n"
        f"🔗 Ваша ссылка:\n<code>{ref_link}</code>"
    )
    await message.answer(text)


@router.message(F.text == "👥 Рефералы")
async def referrals(message: Message, bot: Bot, db: Database, settings: SettingsStore, bot_username: str) -> None:
    if not message.from_user:
        return
    if message.chat.type != "private":
        await message.answer("Пожалуйста, используйте бота в личных сообщениях.")
        return
    ok, sponsors, links = await _require_sponsors2(bot, db, message.from_user.id)
    if not ok:
        await _send_sponsor_gate2(message, sponsors, links)
        return

    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("Похоже, вы ещё не зарегистрированы. Нажмите /start")
        return

    ref_link = f"https://t.me/{bot_username}?start={user.user_id}"
    text = (
        "👥 <b>Реферальная программа</b>\n\n"
        f"✅ Приглашено: <b>{user.invited_count}</b>\n"
        "📌 Условие: реферал должен выполнить <b>2 задания</b>\n\n"
        f"💰 Награда вам: <b>{settings.get_float('REF_REWARD'):.2f}</b>\n\n"
        f"🔗 Ваша ссылка:\n<code>{ref_link}</code>"
    )
    share_url = (
        "https://t.me/share/url?url="
        + quote_plus(ref_link)
        + "&text="
        + quote_plus("Присоединяйся по моей ссылке 👇")
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📤 Поделиться ссылкой", url=share_url)]]
    )
    await message.answer(text, reply_markup=kb)


@router.message(F.text == "💰 Заработать")
async def earn(message: Message, bot: Bot, db: Database) -> None:
    if not message.from_user:
        return
    if message.chat.type != "private":
        await message.answer("Пожалуйста, используйте бота в личных сообщениях.")
        return
    ok, sponsors, links = await _require_sponsors2(bot, db, message.from_user.id)
    if not ok:
        await _send_sponsor_gate2(message, sponsors, links)
        return

    await message.answer(
        "💰 <b>Заработать</b>\nНиже список доступных заданий:",
        reply_markup=tasks_entry_kb(),
    )


@router.message(F.text == "🎁 Промокод")
async def promo_start(message: Message, bot: Bot, db: Database, state: FSMContext) -> None:
    if not message.from_user:
        return
    if message.chat.type != "private":
        await message.answer("Пожалуйста, используйте бота в личных сообщениях.")
        return
    ok, sponsors, links = await _require_sponsors2(bot, db, message.from_user.id)
    if not ok:
        await _send_sponsor_gate2(message, sponsors, links)
        return

    if not await db.get_user(message.from_user.id):
        await message.answer("Похоже, вы ещё не зарегистрированы. Нажмите /start")
        return

    await state.set_state(PromoActivateState.waiting_code)
    await message.answer("🎁 Введите промокод одним сообщением:")


@router.message(F.text == "💸 Вывод")
async def withdraw(message: Message, bot: Bot, db: Database) -> None:
    if not message.from_user:
        return
    if message.chat.type != "private":
        await message.answer("Пожалуйста, используйте бота в личных сообщениях.")
        return
    ok, sponsors, links = await _require_sponsors2(bot, db, message.from_user.id)
    if not ok:
        await _send_sponsor_gate2(message, sponsors, links)
        return

    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("Похоже, вы ещё не зарегистрированы. Нажмите /start")
        return
    if await db.is_balance_frozen(user.user_id):
        await message.answer(
            "🧊 <b>Баланс заморожен</b>\n\n"
            "Заработок и вывод временно недоступны.\n"
            "Обратитесь в поддержку."
        )
        return

    if (message.from_user.id not in config.ADMINS) and (user.invited_count < int(config.WITHDRAW_MIN_INVITES)):
        await message.answer(
            "⛔ <b>Вывод временно недоступен</b>\n\n"
            f"Условие: пригласить минимум <b>{int(config.WITHDRAW_MIN_INVITES)}</b> человек.\n"
            f"Сейчас приглашено: <b>{user.invited_count}</b>\n\n"
            "Перейдите в «👥 Рефералы» и пригласите друзей по своей ссылке."
        )
        return

    await message.answer(
        f"💸 <b>Вывод</b>\n\n💰 Ваш баланс: <b>{user.balance:.2f}</b>\nВыберите вариант:",
        reply_markup=withdraw_menu(),
    )


@router.message(F.text == "ℹ️ О боте")
async def about(message: Message) -> None:
    if message.chat.type != "private":
        await message.answer("Пожалуйста, используйте бота в личных сообщениях.")
        return
    text = (
        "ℹ️ <b>О боте</b>\n\n"
        "Этот бот позволяет зарабатывать баланс за задания/рефералов и оформлять заявки на вывод.\n"
        "🔒 Доступ может быть ограничен подпиской на спонсоров.\n\n"
        "Если что-то не работает — напишите в поддержку или прочитайте правила."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💬 Поддержка", url=f"https://t.me/{config.SUPPORT_USERNAME}"),
                InlineKeyboardButton(text="📜 Правила", callback_data="about:rules"),
            ]
            ,
            [InlineKeyboardButton(text="💖 Поддержать донатом", callback_data="donate:start")],
        ]
    )
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "about:rules")
async def about_rules(callback: CallbackQuery, settings: SettingsStore) -> None:
    if not callback.message:
        return
    await callback.answer()
    try:
        rules = settings.get_str("RULES_TEXT")
    except Exception:
        rules = config.RULES_TEXT
    await callback.message.answer(f"📜 <b>Правила</b>\n\n{rules}")


# ---- Донат (Telegram Stars / XTR) ----
@router.callback_query(F.data == "donate:start")
async def donate_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not callback.message:
        return
    await callback.answer()
    await state.set_state(DonateState.waiting_amount)
    await callback.message.answer(
        "💖 <b>Поддержать донатом</b>\n\n"
        "Введите сумму доната в ⭐ (целое число), например: <code>10</code>\n"
        "Отмена: /cancel"
    )


@router.message(Command("cancel"))
async def cancel_user_state(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    cur = await state.get_state()
    if not cur:
        return
    await state.clear()
    await message.answer("✅ Отменено.")


@router.message(DonateState.waiting_amount)
async def donate_amount(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user or message.chat.type != "private":
        await state.clear()
        return
    raw = (message.text or "").strip()
    amount = safe_int(raw)
    if amount is None or amount <= 0:
        await message.answer("Введите целое число ⭐ (например: <code>10</code>).")
        return
    if amount > 10_000:
        await message.answer("Слишком большая сумма. Введите число до <code>10000</code> ⭐.")
        return

    await state.clear()

    # Telegram Stars invoices: currency="XTR", provider_token="".
    # Важно: prices должен содержать ровно 1 элемент.
    try:
        from aiogram.types import LabeledPrice
    except Exception:
        await message.answer("Ошибка: не удалось создать счёт (LabeledPrice). Обновите aiogram.")
        return

    payload = f"donate:{message.from_user.id}:{amount}:{int(time.time())}"
    await bot.send_invoice(
        chat_id=message.chat.id,
        title="Донат ⭐",
        description="Спасибо за поддержку проекта!",
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=f"Донат {amount} ⭐", amount=int(amount))],
    )


@router.pre_checkout_query()
async def donate_pre_checkout(pre_checkout_query: PreCheckoutQuery, bot: Bot) -> None:
    # Всегда подтверждаем донат (проверку суммы Telegram делает сам).
    try:
        await pre_checkout_query.answer(ok=True)
    except Exception:
        try:
            await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
        except Exception:
            pass


@router.message(F.successful_payment)
async def donate_success(message: Message, bot: Bot) -> None:
    sp = message.successful_payment
    if not sp:
        return
    if sp.currency != "XTR":
        return
    # Чтобы не мешать другим оплатам (например, мини-играм), обрабатываем только payload доната.
    payload = sp.invoice_payload or ""
    if not payload.startswith("donate:"):
        return
    stars = int(sp.total_amount)
    await message.answer(f"💖 Спасибо за донат!\n⭐ Получено: <b>{stars}</b>")
    # Уведомим админов (по желанию)
    for admin_id in config.ADMINS:
        try:
            await bot.send_message(
                admin_id,
                "💖 <b>Новый донат</b>\n"
                f"👤 Пользователь: {fmt_user(message.from_user.username if message.from_user else None, message.from_user.id if message.from_user else 0)}\n"
                f"⭐ Сумма: <b>{stars}</b>",
            )
        except Exception:
            pass


@router.message()
async def fallback(message: Message) -> None:
    if not message.from_user:
        return
    await message.answer("Используйте кнопки меню 👇")


# ---- Inline mode: @BotUsername в любом чате ----
# Включите Inline Mode в BotFather (/setinline), иначе Telegram не будет присылать inline_query.
@router.inline_query()
async def inline_referral(inline_query: InlineQuery, db: Database, bot_username: str) -> None:
    if not inline_query.from_user:
        return

    user = await db.get_user(inline_query.from_user.id)
    if not user:
        text = (
            "Чтобы получить реферальную ссылку, сначала запустите бота в личных сообщениях:\n"
            f"https://t.me/{bot_username}"
        )
        res = InlineQueryResultArticle(
            id="need_start",
            title="⚠️ Сначала запустите бота",
            description="Откройте бота в личке, затем вернитесь в чат.",
            input_message_content=InputTextMessageContent(message_text=text, disable_web_page_preview=True),
        )
        await inline_query.answer([res], cache_time=0, is_personal=True)
        return

    ref_link = f"https://t.me/{bot_username}?start={user.user_id}"
    msg = (
        "👥 <b>Приглашаю в бота!</b>\n\n"
        "Перейди по ссылке и выполняй задания 👇\n"
        f"<a href=\"{ref_link}\">🚀 Открыть бота</a>\n\n"
        f"🔗 <code>{ref_link}</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚀 Открыть бота", url=ref_link)]])
    res = InlineQueryResultArticle(
        id=f"ref_{user.user_id}",
        title="🔗 Моя реферальная ссылка",
        description="Красивое сообщение + кнопка",
        input_message_content=InputTextMessageContent(
            message_text=msg,
            parse_mode="HTML",
            disable_web_page_preview=True,
        ),
        reply_markup=kb,
    )
    await inline_query.answer([res], cache_time=0, is_personal=True)
