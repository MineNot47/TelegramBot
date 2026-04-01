from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder


def main_menu(is_admin: bool) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.row(KeyboardButton(text="⭐ Личный кабинет"))
    kb.row(KeyboardButton(text="👥 Рефералы"), KeyboardButton(text="💰 Заработать"))
    kb.row(KeyboardButton(text="🎁 Промокод"), KeyboardButton(text="💸 Вывод"))
    kb.row(KeyboardButton(text="ℹ️ О боте"))
    if is_admin:
        kb.row(KeyboardButton(text="👑 Админ-панель"))
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=False)


def sponsors_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Проверить подписку", callback_data="sponsors:check")
    return kb.as_markup()


def earn_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Задания", callback_data="earn:tasks")
    kb.button(text="🎁 Промокод", callback_data="earn:promo")
    kb.adjust(1)
    return kb.as_markup()


def tasks_entry_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Показать задания", callback_data="tasks:menu")
    kb.button(text="🎲 Игральная кость", callback_data="dice:menu")
    kb.adjust(1)
    return kb.as_markup()


def tasks_list_kb(task_chat_ids: list[int]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for chat_id in task_chat_ids:
        kb.button(text=f"✅ Проверить {chat_id}", callback_data=f"task:check:{chat_id}")
    kb.adjust(1)
    return kb.as_markup()


def withdraw_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💝 Heart with Bow — 15 ⭐", callback_data="wd:new:heart_bow_15")
    kb.button(text="🧸 Teddy Bear — 15 ⭐", callback_data="wd:new:teddy_15")
    kb.button(text="🎁 Gift Box — 25 ⭐", callback_data="wd:new:gift_box_25")
    kb.button(text="🌹 Rose — 25 ⭐", callback_data="wd:new:rose_25")
    kb.button(text="🎂 Birthday Cake — 50 ⭐", callback_data="wd:new:cake_50")
    kb.button(text="💐 Bouquet — 50 ⭐", callback_data="wd:new:bouquet_50")
    kb.button(text="🚀 Rocket — 50 ⭐", callback_data="wd:new:rocket_50")
    kb.button(text="🍾 Champagne — 50 ⭐", callback_data="wd:new:champagne_50")
    kb.button(text="🏆 Trophy — 100 ⭐", callback_data="wd:new:trophy_100")
    kb.button(text="💍 Ring — 100 ⭐", callback_data="wd:new:ring_100")
    kb.button(text="💎 Diamond — 100 ⭐", callback_data="wd:new:diamond_100")
    kb.adjust(1)
    return kb.as_markup()


def wd_confirm_kb(wd_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить", callback_data=f"admin:wd:approve:{wd_id}")
    kb.button(text="❌ Отклонить", callback_data=f"admin:wd:decline:{wd_id}")
    kb.adjust(2)
    return kb.as_markup()


def admin_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.row(KeyboardButton(text="📊 Статистика"))
    kb.row(KeyboardButton(text="📢 Рассылка"), KeyboardButton(text="🎁 Промокоды"))
    kb.row(KeyboardButton(text="👥 Пользователи"), KeyboardButton(text="🚫 Баны"))
    kb.row(KeyboardButton(text="🧊 Заморозка"), KeyboardButton(text="📝 Правила"))
    kb.row(KeyboardButton(text="🛠 Техработы"))
    kb.row(KeyboardButton(text="🧪 Flyer Debug"))
    kb.row(KeyboardButton(text="📺 Спонсоры"), KeyboardButton(text="📋 Задания"))
    kb.row(KeyboardButton(text="⚙️ Настройки"))
    kb.row(KeyboardButton(text="⬅️ Назад"))
    return kb.as_markup(resize_keyboard=True)


def admin_simple_actions_kb(actions: list[tuple[str, str]], columns: int = 1) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for text, cb in actions:
        kb.button(text=text, callback_data=cb)
    kb.adjust(columns)
    return kb.as_markup()


def back_inline(cb: str = "back") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=cb)]])
