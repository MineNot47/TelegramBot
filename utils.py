from __future__ import annotations

import datetime as dt


def ts_to_date(ts: int) -> str:
    d = dt.datetime.fromtimestamp(int(ts))
    return d.strftime("%d.%m.%Y %H:%M")


def fmt_user(username: str | None, user_id: int) -> str:
    if username:
        return f"@{username}"
    return f"<code>{user_id}</code>"


def safe_float(text: str) -> float | None:
    try:
        return float(text.replace(",", ".").strip())
    except Exception:
        return None


def safe_int(text: str) -> int | None:
    try:
        return int(text.strip())
    except Exception:
        return None

