from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class BroadcastState(StatesGroup):
    waiting_message = State()
    waiting_button_text = State()
    waiting_button_url = State()


class PromoCreateState(StatesGroup):
    waiting_code = State()
    waiting_reward = State()
    waiting_limit = State()
    waiting_expires_days = State()


class UserSearchState(StatesGroup):
    waiting_user_id = State()
    waiting_balance_delta = State()


class ChannelManageState(StatesGroup):
    waiting_chat = State()


class SettingsState(StatesGroup):
    waiting_value = State()


class PromoActivateState(StatesGroup):
    waiting_code = State()


class BanState(StatesGroup):
    waiting_user_id = State()
    waiting_reason = State()


class FreezeState(StatesGroup):
    waiting_user_id = State()
    waiting_reason = State()


class RulesEditState(StatesGroup):
    waiting_text = State()


class DonateState(StatesGroup):
    waiting_amount = State()
