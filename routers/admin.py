from __future__ import annotations

import time
import io
import json

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import BufferedInputFile

from config import ADMINS
from db import Database
from keyboards import admin_menu, admin_simple_actions_kb, main_menu
from settings_store import SettingsStore
from flyer_client import FlyerClient
from states import (
    BanState,
    BroadcastState,
    ChannelManageState,
    FreezeState,
    MaintenanceState,
    PromoCreateState,
    RulesEditState,
    SettingsState,
    UserSearchState,
)
from utils import fmt_user, safe_float, safe_int, ts_to_date

router = Router()

# Важно: ограничиваем весь роутер админов на уровне фильтров.
# Иначе catch-all хендлеры могут "съедать" сообщения обычных пользователей.
router.message.filter(F.from_user.id.in_(ADMINS))
router.callback_query.filter(F.from_user.id.in_(ADMINS))

MAIN_MENU_TEXTS = {
    "⭐ Личный кабинет",
    "👥 Рефералы",
    "💰 Заработать",
    "🎁 Промокод",
    "💸 Вывод",
    "ℹ️ О боте",
    "👑 Админ-панель",
}

def _mt_get_enabled(settings: SettingsStore) -> bool:
    try:
        return settings.get_int("MAINTENANCE_ENABLED") == 1
    except Exception:
        return False


def _mt_get_exc(settings: SettingsStore) -> set[int]:
    try:
        raw = settings.get_str("MAINTENANCE_EXCEPT_IDS")
    except Exception:
        raw = ""
    out: set[int] = set()
    for p in str(raw).replace(";", ",").split(","):
        s = p.strip()
        if not s:
            continue
        try:
            out.add(int(s))
        except Exception:
            continue
    return out


async def _mt_set_exc(settings: SettingsStore, ids: set[int]) -> None:
    await settings.set_value("MAINTENANCE_EXCEPT_IDS", ",".join(str(x) for x in sorted(ids)))


def _admin_only(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in ADMINS)


def _admin_only_cb(callback: CallbackQuery) -> bool:
    return bool(callback.from_user and callback.from_user.id in ADMINS)


@router.message(F.text == "👑 Админ-панель")
async def open_admin_panel(message: Message) -> None:
    if not _admin_only(message):
        return
    await message.answer("👑 <b>Админ-панель</b>", reply_markup=admin_menu())


@router.message(F.text == "⬅️ Назад")
async def admin_back(message: Message) -> None:
    if not _admin_only(message):
        return
    await message.answer("Главное меню 👇", reply_markup=main_menu(is_admin=True))


@router.message(F.text == "📊 Статистика")
async def admin_stats(message: Message, db: Database) -> None:
    if not _admin_only(message):
        return
    total = await db.count_users()
    active_24h = await db.count_active_users(24 * 3600)
    pending = await db.count_withdrawals("pending")
    all_wd = await db.count_withdrawals()
    await message.answer(
        "📊 <b>Статистика</b>\n\n"
        f"👥 Всего пользователей: <b>{total}</b>\n"
        f"🟢 Активные (24ч): <b>{active_24h}</b>\n"
        f"📩 Заявок (всего): <b>{all_wd}</b>\n"
        f"⏳ В ожидании: <b>{pending}</b>"
    )


@router.message(F.text == "🧪 Flyer Debug")
async def flyer_debug(message: Message, flyer: FlyerClient | None) -> None:
    if not _admin_only(message):
        return
    if flyer is None:
        await message.answer("FlyerAPI не подключён (нет ключа/ошибка инициализации).")
        return
    uid = message.from_user.id if message.from_user else 0
    tasks = await flyer.get_tasks(user_id=uid, language_code=message.from_user.language_code if message.from_user else "ru", limit=3)
    dump = json.dumps(tasks, ensure_ascii=False, indent=2)
    if len(dump) <= 3500:
        await message.answer("🧪 <b>Flyer get_tasks (пример)</b>\n\n<pre>" + dump + "</pre>")
        return
    bio = io.BytesIO(dump.encode("utf-8"))
    f = BufferedInputFile(bio.getvalue(), filename="flyer_tasks_sample.json")
    await message.answer_document(f, caption="🧪 Flyer get_tasks (пример)")


# ---- Рассылка ----
@router.message(F.text == "📢 Рассылка")
async def broadcast_start(message: Message, state: FSMContext) -> None:
    if not _admin_only(message):
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Без кнопки", callback_data="admin:bc:plain")],
            [InlineKeyboardButton(text="🔗 С кнопкой", callback_data="admin:bc:btn")],
        ]
    )
    await state.clear()
    await message.answer("📢 <b>Рассылка</b>\nВыберите режим:", reply_markup=kb)


@router.callback_query(F.data.in_({"admin:bc:plain", "admin:bc:btn"}))
async def broadcast_mode(callback: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only_cb(callback) or not callback.message:
        return
    await callback.answer()
    mode = "plain" if callback.data.endswith("plain") else "btn"
    await state.update_data(bc_mode=mode)
    await state.set_state(BroadcastState.waiting_message)
    await callback.message.answer(
        "Отправьте сообщение для рассылки (текст или медиа).\nОтмена: /cancel"
        + ("\n\nПосле этого бот попросит текст и ссылку кнопки." if mode == "btn" else "")
    )


@router.message(F.text == "/cancel")
async def cancel_any(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    await state.clear()
    await message.answer("✅ Отменено.", reply_markup=admin_menu() if message.from_user.id in ADMINS else None)


@router.message(BroadcastState.waiting_message)
async def broadcast_run(message: Message, bot: Bot, db: Database, state: FSMContext) -> None:
    if not _admin_only(message):
        await state.clear()
        return
    data = await state.get_data()
    mode = data.get("bc_mode", "plain")

    if mode == "btn":
        await state.update_data(bc_from_chat_id=message.chat.id, bc_message_id=message.message_id)
        await state.set_state(BroadcastState.waiting_button_text)
        await message.answer("Введите <b>текст кнопки</b> (например: Перейти):")
        return

    # plain
    user_ids = await db.list_user_ids()
    sent = 0
    failed = 0
    for uid in user_ids:
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=message.chat.id, message_id=message.message_id)
            sent += 1
        except Exception:
            failed += 1

    await state.clear()
    await message.answer(f"✅ Рассылка завершена.\nОтправлено: <b>{sent}</b>\nОшибок: <b>{failed}</b>")


@router.message(BroadcastState.waiting_button_text)
async def broadcast_btn_text(message: Message, state: FSMContext) -> None:
    if not _admin_only(message):
        await state.clear()
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Введите текст кнопки.")
        return
    await state.update_data(bc_btn_text=text)
    await state.set_state(BroadcastState.waiting_button_url)
    await message.answer("Введите <b>ссылку</b> для кнопки (начиная с http/https):")


@router.message(BroadcastState.waiting_button_url)
async def broadcast_btn_url(message: Message, bot: Bot, db: Database, state: FSMContext) -> None:
    if not _admin_only(message):
        await state.clear()
        return
    url = (message.text or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        await message.answer("Ссылка должна начинаться с http:// или https://")
        return

    data = await state.get_data()
    from_chat_id = int(data["bc_from_chat_id"])
    message_id = int(data["bc_message_id"])
    btn_text = str(data["bc_btn_text"])
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=btn_text, url=url)]]
    )

    user_ids = await db.list_user_ids()
    sent = 0
    failed = 0
    for uid in user_ids:
        try:
            await bot.copy_message(
                chat_id=uid,
                from_chat_id=from_chat_id,
                message_id=message_id,
                reply_markup=markup,
            )
            sent += 1
        except Exception:
            failed += 1

    await state.clear()
    await message.answer(f"✅ Рассылка с кнопкой завершена.\nОтправлено: <b>{sent}</b>\nОшибок: <b>{failed}</b>")


# ---- Промокоды ----
@router.message(F.text == "🎁 Промокоды")
async def promos_menu(message: Message) -> None:
    if not _admin_only(message):
        return
    kb = admin_simple_actions_kb(
        [
            ("➕ Создать промокод", "admin:promo:create"),
            ("📋 Список промокодов", "admin:promo:list"),
        ],
        columns=1,
    )
    await message.answer("🎁 <b>Промокоды</b>", reply_markup=kb)


@router.callback_query(F.data == "admin:promo:list")
async def promos_list(callback: CallbackQuery, db: Database) -> None:
    if not _admin_only_cb(callback) or not callback.message:
        return
    rows = await db.list_promos()
    if not rows:
        await callback.answer()
        await callback.message.answer("Промокодов нет.")
        return

    lines = ["📋 <b>Промокоды</b>\n"]
    for r in rows[:30]:
        exp = ""
        if r["expires_at"] is not None:
            exp = f" | до {ts_to_date(int(r['expires_at']))}"
        lines.append(
            f"• <code>{r['code']}</code> — <b>{float(r['reward']):.2f}</b> | "
            f"{int(r['used_count'])}/{int(r['uses_limit'])}{exp}"
        )
    await callback.answer()
    await callback.message.answer("\n".join(lines))


@router.callback_query(F.data == "admin:promo:create")
async def promo_create_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only_cb(callback) or not callback.message:
        return
    await callback.answer()
    await state.set_state(PromoCreateState.waiting_code)
    await callback.message.answer("➕ Введите <b>название промокода</b> (например: SPRING2026):")


@router.message(PromoCreateState.waiting_code)
async def promo_create_code(message: Message, state: FSMContext) -> None:
    if not _admin_only(message):
        await state.clear()
        return
    code = (message.text or "").strip().upper()
    if not code or len(code) < 3:
        await message.answer("Короткое название. Попробуйте ещё раз.")
        return
    await state.update_data(code=code)
    await state.set_state(PromoCreateState.waiting_reward)
    await message.answer("Введите <b>награду</b> (например: 1.5):")


@router.message(PromoCreateState.waiting_reward)
async def promo_create_reward(message: Message, state: FSMContext) -> None:
    if not _admin_only(message):
        await state.clear()
        return
    val = safe_float(message.text or "")
    if val is None or val <= 0:
        await message.answer("Введите число > 0.")
        return
    await state.update_data(reward=float(val))
    await state.set_state(PromoCreateState.waiting_limit)
    await message.answer("Введите <b>лимит активаций</b> (например: 100):")


@router.message(PromoCreateState.waiting_limit)
async def promo_create_limit(message: Message, state: FSMContext) -> None:
    if not _admin_only(message):
        await state.clear()
        return
    limit = safe_int(message.text or "")
    if limit is None or limit <= 0:
        await message.answer("Введите целое число > 0.")
        return
    await state.update_data(limit=int(limit))
    await state.set_state(PromoCreateState.waiting_expires_days)
    await message.answer("Введите срок действия в <b>днях</b> (0 — без срока):")


@router.message(PromoCreateState.waiting_expires_days)
async def promo_create_expires(message: Message, state: FSMContext, db: Database) -> None:
    if not _admin_only(message):
        await state.clear()
        return
    days = safe_int(message.text or "")
    if days is None or days < 0:
        await message.answer("Введите целое число (0 или больше).")
        return

    data = await state.get_data()
    code = str(data["code"])
    reward = float(data["reward"])
    limit = int(data["limit"])

    expires_at = None
    if days > 0:
        expires_at = int(time.time()) + days * 86400

    try:
        await db.create_promo(code, reward, limit, expires_at=expires_at)
    except Exception:
        await message.answer("❌ Не удалось создать (возможно, такой промокод уже есть).")
    else:
        suffix = f", срок {days} дн." if days > 0 else ", без срока"
        await message.answer(f"✅ Промокод создан: <code>{code}</code> (+{reward:.2f}, лимит {limit}{suffix})")
    await state.clear()


# ---- Пользователи ----
@router.message(F.text == "👥 Пользователи")
async def users_start(message: Message, state: FSMContext) -> None:
    if not _admin_only(message):
        return
    await state.set_state(UserSearchState.waiting_user_id)
    await message.answer("👥 Введите <b>ID пользователя</b>:")


@router.message(UserSearchState.waiting_user_id)
async def users_show(message: Message, state: FSMContext, db: Database) -> None:
    if not _admin_only(message):
        await state.clear()
        return
    raw = (message.text or "").strip()
    uid = safe_int(raw)
    user = await (db.get_user(uid) if uid is not None else db.find_user_by_username(raw))
    if not user:
        await message.answer("Пользователь не найден.")
        await state.clear()
        return

    banned = await db.is_banned(user.user_id)
    frozen = await db.is_balance_frozen(user.user_id)
    await state.update_data(target_user_id=user.user_id)
    await state.set_state(UserSearchState.waiting_balance_delta)

    await message.answer(
        "👤 <b>Пользователь</b>\n\n"
        f"🆔 <code>{user.user_id}</code>\n"
        f"👤 {fmt_user(user.username, user.user_id)}\n"
        f"💰 Баланс: <b>{user.balance:.2f}</b>\n"
        f"🧊 Заморозка: <b>{'ДА' if frozen else 'НЕТ'}</b>\n"
        f"📅 Регистрация: <b>{ts_to_date(user.registered_at)}</b>\n"
        f"👥 Рефералы: <b>{user.invited_count}</b>\n"
        f"🚫 Бан: <b>{'ДА' if banned else 'НЕТ'}</b>\n\n"
        "Введите сумму для изменения баланса (например: <code>10</code> или <code>-5</code>).\n"
        "Отмена: /cancel"
    )


@router.message(UserSearchState.waiting_balance_delta)
async def users_balance_change(message: Message, state: FSMContext, db: Database) -> None:
    if not _admin_only(message):
        await state.clear()
        return
    delta = safe_float(message.text or "")
    if delta is None or delta == 0:
        await message.answer("Введите число (может быть отрицательным).")
        return
    data = await state.get_data()
    uid = int(data["target_user_id"])
    await db.change_balance(uid, float(delta))
    user = await db.get_user(uid)
    await state.clear()
    await message.answer(f"✅ Баланс изменён. Текущий: <b>{(user.balance if user else 0):.2f}</b>")


# ---- Баны ----
@router.message(F.text == "🚫 Баны")
async def bans_menu(message: Message) -> None:
    if not _admin_only(message):
        return
    kb = admin_simple_actions_kb(
        [
            ("🚫 Забанить", "admin:ban"),
            ("✅ Разбанить", "admin:unban"),
            ("📋 Список банов", "admin:ban:list"),
        ],
        columns=1,
    )
    await message.answer("🚫 <b>Баны</b>", reply_markup=kb)


# ---- Заморозка баланса ----
@router.message(F.text == "🧊 Заморозка")
async def freezes_menu(message: Message) -> None:
    if not _admin_only(message):
        return
    kb = admin_simple_actions_kb(
        [
            ("🧊 Заморозить", "admin:freeze"),
            ("🔥 Разморозить", "admin:unfreeze"),
            ("📋 Список", "admin:freeze:list"),
        ],
        columns=1,
    )
    await message.answer("🧊 <b>Заморозка баланса</b>", reply_markup=kb)


@router.callback_query(F.data == "admin:freeze:list")
async def freezes_list(callback: CallbackQuery, db: Database) -> None:
    if not _admin_only_cb(callback) or not callback.message:
        return
    rows = await db.list_balance_freezes()
    if not rows:
        await callback.answer()
        await callback.message.answer("Заморозок нет.")
        return
    lines = ["🧊 <b>Замороженные</b>\n"]
    for r in rows[:30]:
        lines.append(f"• <code>{r['user_id']}</code> — {r['reason'] or 'без причины'}")
    await callback.answer()
    await callback.message.answer("\n".join(lines))


@router.callback_query(F.data.in_({"admin:freeze", "admin:unfreeze"}))
async def freeze_unfreeze_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only_cb(callback) or not callback.message:
        return
    await callback.answer()
    action = callback.data.split(":")[1]  # freeze/unfreeze
    await state.update_data(freeze_action=action)
    await state.set_state(FreezeState.waiting_user_id)
    await callback.message.answer("Введите <b>ID пользователя</b> или <b>@username</b>:")


@router.message(FreezeState.waiting_user_id)
async def freeze_unfreeze_user_id(message: Message, state: FSMContext, db: Database) -> None:
    if not _admin_only(message):
        await state.clear()
        return
    raw = (message.text or "").strip()
    uid = safe_int(raw)
    if uid is None:
        u = await db.find_user_by_username(raw)
        uid = u.user_id if u else None
    if uid is None:
        await message.answer("Пользователь не найден. Введите ID или @username.")
        return
    data = await state.get_data()
    action = data.get("freeze_action")
    if action == "unfreeze":
        await db.unfreeze_balance(uid)
        await state.clear()
        await message.answer("✅ Разморожено.")
        return

    await state.update_data(target_user_id=uid)
    await state.set_state(FreezeState.waiting_reason)
    await message.answer("Введите причину заморозки (или '-' без причины):")


@router.message(FreezeState.waiting_reason)
async def freeze_reason(message: Message, state: FSMContext, db: Database) -> None:
    if not _admin_only(message):
        await state.clear()
        return
    data = await state.get_data()
    uid = int(data["target_user_id"])
    reason = (message.text or "").strip()
    if reason == "-":
        reason = None
    await db.freeze_balance(uid, reason=reason)
    await state.clear()
    await message.answer("✅ Баланс заморожен.")


@router.callback_query(F.data == "admin:ban:list")
async def bans_list(callback: CallbackQuery, db: Database) -> None:
    if not _admin_only_cb(callback) or not callback.message:
        return
    rows = await db.list_bans()
    if not rows:
        await callback.answer()
        await callback.message.answer("Банов нет.")
        return
    lines = ["🚫 <b>Забаненные</b>\n"]
    for r in rows[:30]:
        lines.append(f"• <code>{r['user_id']}</code> — {r['reason'] or 'без причины'}")
    await callback.answer()
    await callback.message.answer("\n".join(lines))


@router.callback_query(F.data.in_({"admin:ban", "admin:unban"}))
async def ban_unban_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only_cb(callback) or not callback.message:
        return
    await callback.answer()
    action = callback.data.split(":")[1]  # ban/unban
    await state.update_data(ban_action=action)
    await state.set_state(BanState.waiting_user_id)
    await callback.message.answer("Введите <b>ID пользователя</b>:")


@router.message(BanState.waiting_user_id)
async def ban_unban_user_id(message: Message, state: FSMContext, db: Database) -> None:
    if not _admin_only(message):
        await state.clear()
        return
    raw = (message.text or "").strip()
    uid = safe_int(raw)
    if uid is None:
        u = await db.find_user_by_username(raw)
        uid = u.user_id if u else None
    if uid is None:
        await message.answer("Пользователь не найден. Введите ID или @username.")
        return
    data = await state.get_data()
    action = data.get("ban_action")
    if action == "unban":
        await db.unban_user(uid)
        await state.clear()
        await message.answer("✅ Разбанено.")
        return

    # ban
    await state.update_data(target_user_id=uid)
    await state.set_state(BanState.waiting_reason)
    await message.answer("Введите причину бана (или '-' без причины):")


@router.message(BanState.waiting_reason)
async def ban_reason(message: Message, state: FSMContext, db: Database) -> None:
    if not _admin_only(message):
        await state.clear()
        return
    data = await state.get_data()
    uid = int(data["target_user_id"])
    reason = (message.text or "").strip()
    if reason == "-":
        reason = None
    await db.ban_user(uid, reason=reason)
    await state.clear()
    await message.answer("✅ Пользователь забанен.")


# ---- Спонсоры / Задания (каналы) ----
@router.message(F.text.in_({"📺 Спонсоры", "📋 Задания"}))
async def channels_menu(message: Message, state: FSMContext) -> None:
    if not _admin_only(message):
        return
    kind = "sponsors" if message.text == "📺 Спонсоры" else "tasks"
    await state.update_data(ch_kind=kind)
    kb = admin_simple_actions_kb(
        [
            ("➕ Добавить", f"admin:ch:add:{kind}"),
            ("➖ Удалить", f"admin:ch:del:{kind}"),
            ("📋 Список", f"admin:ch:list:{kind}"),
        ],
        columns=1,
    )
    title = "📺 Спонсоры" if kind == "sponsors" else "📋 Задания"
    hint = ""
    if kind == "sponsors":
        hint = (
            "\n\n💡 Можно добавить:\n"
            "- канал по <code>-100...</code> или <code>@username</code> (будет проверка подписки)\n"
            "- ссылку <code>https://...</code> (без проверки)\n"
            "- без проверки: начните с <code>!</code>, например <code>!@mychannel</code>\n"
            "- удалить ссылку: <code>link:ID</code>"
        )
    if kind == "tasks":
        hint = (
            "\n\n💡 Форматы для добавления задания:\n"
            "- <code>@channel 1.5</code>\n"
            "- <code>-100123... 2</code>\n"
            "- <code>https://ваша_ссылка @channel 3</code>\n"
            "- без проверки: <code>!@channel 1.5</code>\n"
        )
    await message.answer(f"{title}\nВыберите действие:{hint}", reply_markup=kb)


@router.callback_query(F.data.startswith("admin:ch:list:"))
async def channels_list(callback: CallbackQuery, bot: Bot, db: Database, settings: SettingsStore) -> None:
    if not _admin_only_cb(callback) or not callback.message:
        return
    kind = callback.data.split(":")[-1]
    lines = ["📋 <b>Список</b>\n"]
    if kind == "sponsors":
        rows = await db.list_sponsor_channels_full()
        links = await db.list_sponsor_links()
        if not rows and not links:
            await callback.answer()
            await callback.message.answer("Список пуст.")
            return
        for r in rows:
            cid = int(r["chat_id"])
            title = r["title"] or ""
            username = r["username"] or ""
            url = r["url"] or ""
            chk = "✅" if int(r["check_required"]) == 1 else "➖"
            name = title or (("@" + username) if username else str(cid))
            extra = f" | {url}" if url else ""
            lines.append(f"• {chk} <b>{name}</b> — <code>{cid}</code>{extra}")
        if links:
            lines.append("")
            lines.append("🔗 <b>Ссылки (без проверки)</b>:")
            for l in links[:30]:
                lines.append(f"• <code>link:{int(l['id'])}</code> — {l['title'] or ''} {l['url']}")
    else:
        rows = await db.list_task_channels_full()
        links = await db.list_task_links()
        if not rows:
            if not links:
                await callback.answer()
                await callback.message.answer("Список пуст.")
                return
        for r in rows[:50]:
            cid = int(r["chat_id"])
            title = (r["title"] or "").strip()
            username = (r["username"] or "").strip()
            url = (r["url"] or "").strip()
            chk = "✅" if int(r["check_required"]) == 1 else "➖"
            reward = float(r["reward"]) if r["reward"] is not None else settings.get_float("TASK_REWARD")
            name = title or (("@" + username) if username else str(cid))
            extra = f" | {url}" if url else ""
            lines.append(f"• {chk} <b>{name}</b> — <code>{cid}</code> | 💰 {reward:.2f}{extra}")
        if links:
            lines.append("")
            lines.append("🔗 <b>Задания-ссылки (без проверки)</b>:")
            for l in links[:30]:
                rid = int(l["id"])
                u = str(l["url"] or "")
                r = float(l["reward"]) if l["reward"] is not None else settings.get_float("TASK_REWARD")
                lines.append(f"• <code>link:{rid}</code> — 💰 {r:.2f} | {u}")
    await callback.answer()
    await callback.message.answer("\n".join(lines))


@router.callback_query(F.data.startswith("admin:ch:"))
async def channels_add_del_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only_cb(callback) or not callback.message:
        return
    _, _, action, kind = callback.data.split(":")  # admin ch add sponsors
    if action not in ("add", "del"):
        await callback.answer("Ошибка.", show_alert=True)
        return
    await callback.answer()
    await state.update_data(ch_action=action, ch_kind=kind)
    await state.set_state(ChannelManageState.waiting_chat)
    await callback.message.answer(
        "Отправьте <b>ID</b> (например: <code>-100123...</code>) или <b>@username</b>.\n"
        "Для спонсоров можно отправить ссылку <code>https://...</code> (без проверки).\n"
        "Для заданий можно отправить ссылку <code>https://...</code> (без проверки, с наградой).\n"
        "Без проверки канала: начните с <code>!</code> (пример: <code>!@mychannel</code>).\n"
        "🤖 Ботов можно добавлять только <b>без проверки</b> (как ссылку или как задание без проверки).\n"
        "Удалить ссылку: <code>link:ID</code>.\n"
        "Отмена: /cancel"
    )


@router.message(ChannelManageState.waiting_chat)
async def channels_add_del_apply(
    message: Message,
    bot: Bot,
    state: FSMContext,
    db: Database,
    settings: SettingsStore,
) -> None:
    if not _admin_only(message):
        await state.clear()
        return
    data = await state.get_data()
    action = data.get("ch_action")
    kind = data.get("ch_kind")
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Введите ID или @username.")
        return

    chat_id: int | None = None
    username: str | None = None
    title: str | None = None
    forced_url: str | None = None

    no_check = False
    if raw.startswith("!"):
        no_check = True
        raw = raw[1:].strip()

    # Задания: награда в конце строки (например: "@channel 1.5")
    reward: float | None = None
    if kind == "tasks" and action == "add" and " " in raw:
        maybe_reward = raw.split()[-1]
        val = safe_float(maybe_reward)
        if val is not None:
            reward = float(val)
            raw = raw[: -len(maybe_reward)].strip()

    # URL + target (и для спонсоров, и для заданий)
    if action == "add" and (raw.startswith("http://") or raw.startswith("https://")) and " " in raw:
        url_part, rest = raw.split(None, 1)
        rest = rest.strip()
        if rest.startswith("@") or safe_int(rest) is not None:
            forced_url = url_part.strip()
            raw = rest

    # Спонсоры: можно добавить ссылку без проверки (URL без target)
    if kind == "sponsors" and action == "add" and forced_url is None and (raw.startswith("http://") or raw.startswith("https://")):
        link_id = await db.add_sponsor_link(raw, title=None, check_required=False)
        await message.answer(f"✅ Ссылка добавлена: <code>link:{link_id}</code>")
        await state.clear()
        return

    # Задания: можно добавить ссылку без проверки (URL без target)
    if kind == "tasks" and action == "add" and forced_url is None and (raw.startswith("http://") or raw.startswith("https://")):
        if reward is None:
            reward = settings.get_float("TASK_REWARD")
        link_id = await db.add_task_link(raw, title=None, reward=float(reward), check_required=False)
        await message.answer(f"✅ Задание-ссылка добавлено: <code>link:{link_id}</code> (💰 {float(reward):.2f})")
        await state.clear()
        return

    # Спонсоры: удалить ссылку
    if kind == "sponsors" and action == "del" and raw.lower().startswith("link:"):
        link_id = safe_int(raw.split(":", 1)[1])
        if link_id is None:
            await message.answer("Неверный формат. Пример: link:12")
            return
        await db.remove_sponsor_link(link_id)
        await message.answer("✅ Ссылка удалена.")
        await state.clear()
        return

    # Задания: удалить задание-ссылку
    if kind == "tasks" and action == "del" and raw.lower().startswith("link:"):
        link_id = safe_int(raw.split(":", 1)[1])
        if link_id is None:
            await message.answer("Неверный формат. Пример: link:12")
            return
        await db.remove_task_link(link_id)
        await message.answer("✅ Задание-ссылка удалено.")
        await state.clear()
        return

    if raw.startswith("@"):
        username = raw.lstrip("@")
        try:
            chat = await bot.get_chat(raw)
            chat_id = int(chat.id)
            title = getattr(chat, "title", None) or getattr(chat, "first_name", None)
            username = chat.username or username
            chat_type = str(getattr(chat, "type", "") or "")
        except Exception:
            await message.answer("❌ Не удалось найти чат. Проверьте @username.")
            return
    else:
        chat_id = safe_int(raw)
        if chat_id is None:
            await message.answer("Введите корректный ID (число) или @username.")
            return
        try:
            chat = await bot.get_chat(chat_id)
            title = getattr(chat, "title", None) or getattr(chat, "first_name", None)
            username = chat.username
            chat_type = str(getattr(chat, "type", "") or "")
        except Exception:
            title = None
            chat_type = ""

    if action == "add":
        # Если добавляют "приватный" чат (это пользователь/бот), проверка подписки невозможна.
        is_private = chat_type == "private"
        if kind == "sponsors":
            url = f"https://t.me/{username}" if username else None
            if (forced_url is None) and (url is None):
                await message.answer(
                    "❌ Не могу добавить без ссылки.\n\n"
                    "Для приватного канала укажите так:\n"
                    "<code>https://t.me/+xxxx -1001234567890</code>\n\n"
                    "Или добавьте @username, чтобы ссылка собралась автоматически."
                )
                return
            if is_private:
                link_id = await db.add_sponsor_link(forced_url or url, title=(f"🤖 @{username}" if username else title), check_required=False)
                await message.answer(
                    "✅ Добавлено как ссылка (без проверки), т.к. это бот/пользователь.\n"
                    f"Удаление: <code>link:{link_id}</code>"
                )
                await state.clear()
                return
            await db.add_sponsor_channel(
                chat_id,
                title=title,
                username=username,
                url=forced_url or url,
                check_required=not no_check,
            )
        else:
            url = forced_url or (f"https://t.me/{username}" if username else None)
            if url is None:
                await message.answer(
                    "❌ Не могу добавить задание без ссылки.\n\n"
                    "Пример:\n"
                    "<code>https://t.me/+xxxx -1001234567890 1.5</code>\n"
                    "или\n"
                    "<code>https://ваша_ссылка @channel 1.5</code>"
                )
                return
            if reward is None:
                reward = settings.get_float("TASK_REWARD")
            await db.add_task_channel(
                chat_id,
                title=title,
                username=username,
                url=url,
                reward=float(reward),
                check_required=(False if is_private else (not no_check)),
            )
        await message.answer("✅ Добавлено.")
    else:
        if kind == "sponsors":
            await db.remove_sponsor_channel(chat_id)
        else:
            await db.remove_task_channel(chat_id)
        await message.answer("✅ Удалено.")

    await state.clear()


# ---- Настройки ----
@router.message(F.text == "⚙️ Настройки")
async def settings_menu(message: Message, settings: SettingsStore) -> None:
    if not _admin_only(message):
        return
    kb = admin_simple_actions_kb(
        [
            ("REF_REWARD", "admin:set:REF_REWARD"),
            ("REF_BONUS", "admin:set:REF_BONUS"),
            ("TASK_REWARD", "admin:set:TASK_REWARD"),
            ("GIFT_REWARD", "admin:set:GIFT_REWARD"),
            ("FLYER_EASY", "admin:set:FLYER_REWARD_EASY"),
            ("FLYER_MED", "admin:set:FLYER_REWARD_MEDIUM"),
            ("FLYER_HARD", "admin:set:FLYER_REWARD_HARD"),
            ("FLYER_UNK", "admin:set:FLYER_REWARD_UNKNOWN"),
            ("DICE_COST", "admin:set:DICE_ROLL_COST"),
            ("DICE_WIN6", "admin:set:DICE_WIN_ON_6"),
            ("DICE_XTR$", "admin:set:DICE_ROLL_COST_XTR"),
            ("DICE_XTR6", "admin:set:DICE_WIN_ON_6_XTR"),
        ],
        columns=2,
    )
    await message.answer(
        "⚙️ <b>Настройки</b>\n\n"
        f"REF_REWARD: <b>{settings.get_float('REF_REWARD'):.2f}</b>\n"
        f"REF_BONUS: <b>{settings.get_float('REF_BONUS'):.2f}</b>\n"
        f"TASK_REWARD: <b>{settings.get_float('TASK_REWARD'):.2f}</b>\n"
        f"GIFT_REWARD: <b>{settings.get_float('GIFT_REWARD'):.2f}</b>\n\n"
        "Flyer награды (⭐):\n"
        f"• EASY: <b>{settings.get_float('FLYER_REWARD_EASY'):.2f}</b>\n"
        f"• MEDIUM: <b>{settings.get_float('FLYER_REWARD_MEDIUM'):.2f}</b>\n"
        f"• HARD: <b>{settings.get_float('FLYER_REWARD_HARD'):.2f}</b>\n\n"
        f"• UNKNOWN: <b>{settings.get_float('FLYER_REWARD_UNKNOWN'):.2f}</b>\n\n"
        "🎲 Кость:\n"
        f"• COST: <b>{settings.get_float('DICE_ROLL_COST'):.2f}</b>\n"
        f"• WIN6: <b>{settings.get_float('DICE_WIN_ON_6'):.2f}</b>\n\n"
        "✨ Кость за Stars:\n"
        f"• COST_XTR: <b>{settings.get_float('DICE_ROLL_COST_XTR'):.2f}</b>\n"
        f"• WIN6_XTR: <b>{settings.get_float('DICE_WIN_ON_6_XTR'):.2f}</b>\n\n"
        "Выберите, что изменить:",
        reply_markup=kb,
    )


# ---- Правила ----
@router.message(F.text == "📝 Правила")
async def rules_edit_start(message: Message, state: FSMContext, settings: SettingsStore) -> None:
    if not _admin_only(message):
        return
    await state.clear()
    await state.set_state(RulesEditState.waiting_text)
    current = ""
    try:
        current = settings.get_str("RULES_TEXT")
    except Exception:
        current = ""
    preview = (current[:800] + "…") if current and len(current) > 800 else current
    await message.answer(
        "📝 <b>Правила</b>\n\n"
        "Отправьте новый текст правил одним сообщением.\n"
        "Можно использовать HTML-теги (<code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, <code>&lt;code&gt;</code>, ссылки).\n"
        "Отмена: /cancel\n\n"
        + ("Текущие (превью):\n" + preview if preview else "Текущие правила ещё не заданы в настройках.")
    )


@router.message(RulesEditState.waiting_text)
async def rules_edit_apply(message: Message, state: FSMContext, settings: SettingsStore) -> None:
    if not _admin_only(message):
        await state.clear()
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Отправьте текст правил.")
        return
    await settings.set_value("RULES_TEXT", text)
    await state.clear()
    await message.answer("✅ Правила обновлены.", reply_markup=admin_menu())


# ---- Технические работы ----
@router.message(F.text == "🛠 Техработы")
async def maintenance_menu(message: Message, settings: SettingsStore) -> None:
    enabled = _mt_get_enabled(settings)
    exc = _mt_get_exc(settings)
    status = "✅ ВКЛ" if enabled else "❌ ВЫКЛ"
    kb = admin_simple_actions_kb(
        [
            ("🔁 Переключить", "admin:mt:toggle"),
            ("➕ Исключение", "admin:mt:add"),
            ("➖ Удалить", "admin:mt:del"),
            ("📋 Список", "admin:mt:list"),
            ("📝 Текст", "admin:mt:text"),
        ],
        columns=2,
    )
    await message.answer(
        "🛠 <b>Технические работы</b>\n\n"
        f"Статус: <b>{status}</b>\n"
        f"Исключений: <b>{len(exc)}</b>\n\n"
        "Во время техработ бот отвечает только админам и пользователям из исключений.",
        reply_markup=kb,
    )


@router.callback_query(F.data == "admin:mt:toggle")
async def maintenance_toggle(callback: CallbackQuery, settings: SettingsStore) -> None:
    if not callback.message:
        return
    enabled = _mt_get_enabled(settings)
    await settings.set_value("MAINTENANCE_ENABLED", 0 if enabled else 1)
    await callback.answer("Готово.")
    await callback.message.answer(f"🛠 Техработы: <b>{'включены' if not enabled else 'выключены'}</b>")


@router.callback_query(F.data == "admin:mt:list")
async def maintenance_list(callback: CallbackQuery, settings: SettingsStore) -> None:
    if not callback.message:
        return
    enabled = _mt_get_enabled(settings)
    exc = sorted(_mt_get_exc(settings))
    try:
        text = settings.get_str("MAINTENANCE_TEXT")
    except Exception:
        text = ""
    lines: list[str] = [
        "🛠 <b>Техработы</b>",
        f"Статус: <b>{'ВКЛ' if enabled else 'ВЫКЛ'}</b>",
        "",
        "Исключения (ID):",
    ]
    if exc:
        lines += [f"• <code>{x}</code>" for x in exc[:50]]
    else:
        lines.append("• (пусто)")
    if text:
        lines += ["", "Текст (превью):", text[:400] + ("…" if len(text) > 400 else "")]
    await callback.answer()
    await callback.message.answer("\n".join(lines))


@router.callback_query(F.data.in_({"admin:mt:add", "admin:mt:del"}))
async def maintenance_exc_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message:
        return
    action = callback.data.split(":")[-1]  # add/del
    await callback.answer()
    await state.update_data(mt_action=action)
    await state.set_state(MaintenanceState.waiting_user_add if action == "add" else MaintenanceState.waiting_user_remove)
    await callback.message.answer("Введите <b>ID пользователя</b> или <b>@username</b>:")


@router.callback_query(F.data == "admin:mt:text")
async def maintenance_text_start(callback: CallbackQuery, state: FSMContext, settings: SettingsStore) -> None:
    if not callback.message:
        return
    await callback.answer()
    await state.set_state(MaintenanceState.waiting_text)
    current = ""
    try:
        current = settings.get_str("MAINTENANCE_TEXT")
    except Exception:
        current = ""
    preview = (current[:800] + "…") if current and len(current) > 800 else current
    await callback.message.answer(
        "🛠 <b>Текст техработ</b>\n\n"
        "Отправьте новый текст одним сообщением.\n"
        "Можно использовать HTML (<code>&lt;b&gt;</code>, ссылки).\n"
        "Отмена: /cancel\n\n"
        + ("Текущий (превью):\n" + preview if preview else "Текст не задан.")
    )


@router.message(MaintenanceState.waiting_text)
async def maintenance_text_apply(message: Message, state: FSMContext, settings: SettingsStore) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Отправьте текст.")
        return
    await settings.set_value("MAINTENANCE_TEXT", text)
    await state.clear()
    await message.answer("✅ Текст техработ обновлён.", reply_markup=admin_menu())


@router.message(MaintenanceState.waiting_user_add)
async def maintenance_exc_add(message: Message, state: FSMContext, db: Database, settings: SettingsStore) -> None:
    raw = (message.text or "").strip()
    uid = safe_int(raw)
    if uid is None:
        u = await db.find_user_by_username(raw)
        uid = u.user_id if u else None
    if uid is None:
        await message.answer("Пользователь не найден. Введите ID или @username.")
        return
    exc = _mt_get_exc(settings)
    exc.add(int(uid))
    await _mt_set_exc(settings, exc)
    await state.clear()
    await message.answer(f"✅ Добавлено в исключения: <code>{int(uid)}</code>")


@router.message(MaintenanceState.waiting_user_remove)
async def maintenance_exc_del(message: Message, state: FSMContext, db: Database, settings: SettingsStore) -> None:
    raw = (message.text or "").strip()
    uid = safe_int(raw)
    if uid is None:
        u = await db.find_user_by_username(raw)
        uid = u.user_id if u else None
    if uid is None:
        await message.answer("Пользователь не найден. Введите ID или @username.")
        return
    exc = _mt_get_exc(settings)
    exc.discard(int(uid))
    await _mt_set_exc(settings, exc)
    await state.clear()
    await message.answer(f"✅ Удалено из исключений: <code>{int(uid)}</code>")


@router.callback_query(F.data.startswith("admin:set:"))
async def setting_change_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only_cb(callback) or not callback.message:
        return
    key = callback.data.split(":")[-1]
    await callback.answer()
    await state.update_data(setting_key=key)
    await state.set_state(SettingsState.waiting_value)
    await callback.message.answer(f"Введите новое значение для <b>{key}</b> (число). Отмена: /cancel")


@router.message(SettingsState.waiting_value)
async def setting_change_apply(message: Message, state: FSMContext, settings: SettingsStore) -> None:
    if not _admin_only(message):
        await state.clear()
        return
    val = safe_float(message.text or "")
    if val is None or val < 0:
        await message.answer("Введите число (>= 0).")
        return
    data = await state.get_data()
    key = str(data["setting_key"])
    await settings.set_value(key, float(val))
    await state.clear()
    await message.answer(f"✅ Сохранено: {key} = <b>{float(val):.2f}</b>")


@router.message(~F.text.in_(MAIN_MENU_TEXTS))
async def admin_fallback(message: Message) -> None:
    # Чтобы обычные сообщения админов в панели не пропадали.
    if not _admin_only(message):
        return
    if message.text and message.text.startswith("/"):
        return
    await message.answer("Используйте кнопки админ-панели 👇", reply_markup=admin_menu())
