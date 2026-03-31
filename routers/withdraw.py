from __future__ import annotations

import datetime as dt

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from config import ADMINS, PAYMENT_CHANNEL_ID, WITHDRAW_MIN_INVITES
from db import Database
from keyboards import wd_confirm_kb
from utils import fmt_user

router = Router()

# Стоимость вариантов вывода в "балансе" бота.
# При желании можно поменять на свои значения.
WITHDRAW_ITEMS: dict[str, tuple[str, float]] = {
    "heart_bow_15": ("💝 Heart with Bow", 15.0),
    "teddy_15": ("🧸 Teddy Bear", 15.0),
    "gift_box_25": ("🎁 Gift Box", 25.0),
    "rose_25": ("🌹 Rose", 25.0),
    "cake_50": ("🎂 Birthday Cake", 50.0),
    "bouquet_50": ("💐 Bouquet", 50.0),
    "rocket_50": ("🚀 Rocket", 50.0),
    "champagne_50": ("🍾 Champagne", 50.0),
    "trophy_100": ("🏆 Trophy", 100.0),
    "ring_100": ("💍 Ring", 100.0),
    "diamond_100": ("💎 Diamond", 100.0),
}


@router.callback_query(F.data.startswith("wd:new:"))
async def wd_new(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    if not callback.from_user or not callback.message:
        return

    code = callback.data.split(":")[-1]
    if code not in WITHDRAW_ITEMS:
        await callback.answer("Неизвестный вариант.", show_alert=True)
        return
    title, amount = WITHDRAW_ITEMS[code]

    user = await db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Нажмите /start", show_alert=True)
        return

    if await db.is_balance_frozen(user.user_id):
        await callback.answer("Баланс заморожен.", show_alert=True)
        await callback.message.answer(
            "🧊 <b>Баланс заморожен</b>\n\n"
            "Вывод временно недоступен. Обратитесь в поддержку."
        )
        return

    if (callback.from_user.id not in ADMINS) and (user.invited_count < int(WITHDRAW_MIN_INVITES)):
        await callback.answer("Вывод доступен после приглашения рефералов.", show_alert=True)
        await callback.message.answer(
            "⛔ <b>Вывод недоступен</b>\n\n"
            f"Условие: пригласить минимум <b>{int(WITHDRAW_MIN_INVITES)}</b> человек.\n"
            f"Сейчас приглашено: <b>{user.invited_count}</b>"
        )
        return
    try:
        wd_id = await db.create_withdrawal(user.user_id, user.username, title, amount)
    except ValueError:
        await callback.answer("Недостаточно баланса.", show_alert=True)
        await callback.message.answer(
            "❌ <b>Недостаточно баланса</b>\n"
            f"Ваш баланс: <b>{user.balance:.2f}</b>\n"
            f"Нужно: <b>{amount:.2f}</b>"
        )
        return

    await callback.answer("✅ Заявка создана!")
    await callback.message.answer(
        "✅ <b>Заявка на вывод создана</b>\n"
        f"📦 Вывод: <b>{title}</b>\n"
        f"💰 Сумма: <b>{amount:.2f}</b>\n\n"
        "💸 Средства списаны и удерживаются до решения администратора."
    )

    admin_text = (
        "📩 <b>Новая заявка на вывод</b>\n\n"
        f"🆔 Заявка: <code>{wd_id}</code>\n"
        f"👤 Пользователь: {fmt_user(user.username, user.user_id)}\n"
        f"💰 Сумма: <b>{amount:.2f}</b>\n"
        f"📦 Вывод: <b>{title}</b>"
    )
    for admin_id in ADMINS:
        try:
            await bot.send_message(admin_id, admin_text, reply_markup=wd_confirm_kb(wd_id))
        except Exception:
            pass


@router.callback_query(F.data.startswith("admin:wd:"))
async def wd_process(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    # Обработчик живёт здесь, чтобы у админов работало даже без открытой панели.
    if not callback.from_user or not callback.message:
        return
    if callback.from_user.id not in ADMINS:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Ошибка данных.", show_alert=True)
        return
    action = parts[2]  # approve/decline
    try:
        wd_id = int(parts[3])
    except Exception:
        await callback.answer("Ошибка ID.", show_alert=True)
        return

    approve = action == "approve"
    try:
        row = await db.process_withdrawal(wd_id, callback.from_user.id, approve=approve)
    except ValueError as e:
        await callback.answer(str(e), show_alert=True)
        return

    status = str(row["status"])
    await callback.answer("Готово.")
    await callback.message.edit_reply_markup(reply_markup=None)

    username = row["username"]
    user_id = int(row["user_id"])
    item = str(row["item"])
    amount = float(row["amount"])
    today = dt.datetime.now().strftime("%d.%m.%Y")

    if status == "approved":
        # Уведомление пользователю
        try:
            await bot.send_message(
                user_id,
                "✅ <b>Ваша заявка на вывод одобрена!</b>\n"
                f"🎁 Вы получили: <b>{item}</b>",
            )
        except Exception:
            pass

        # Канал выплат
        try:
            await bot.send_message(
                PAYMENT_CHANNEL_ID,
                "💸 <b>Выплата одобрена</b>\n"
                f"👤 Пользователь: {fmt_user(username, user_id)}\n"
                f"💰 Вывод: <b>{item}</b> (<b>{amount:.2f}</b>)\n"
                f"📅 Дата: <b>{today}</b>",
            )
        except Exception:
            pass

        await callback.message.answer("✅ Одобрено и отправлено в канал выплат.")

    elif status == "declined":
        try:
            await bot.send_message(user_id, "❌ <b>Ваша заявка на вывод отклонена</b>")
        except Exception:
            pass
        try:
            await bot.send_message(
                PAYMENT_CHANNEL_ID,
                "❌ <b>Выплата отклонена</b>\n"
                f"👤 Пользователь: {fmt_user(username, user_id)}\n"
                f"📅 Дата: <b>{today}</b>",
            )
        except Exception:
            pass
        await callback.message.answer("❌ Отклонено.")
    else:
        await callback.message.answer("ℹ️ Заявка уже была обработана ранее.")
