from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FlyerTask:
    signature: str
    link: str | None
    title: str | None
    reward: float | None
    raw: dict[str, Any]


def _deep_find_link(obj: Any, *, depth: int = 4, seen: int = 0, limit: int = 500) -> str | None:
    """
    Flyer может отдавать ссылку в неожиданных вложенных структурах.
    Ищем любую строку, похожую на telegram-ссылку.
    """
    if seen > limit or depth <= 0:
        return None

    if isinstance(obj, str):
        s = obj.strip().strip('"').strip("'").replace("\\/", "/")
        if not s:
            return None
        if s.startswith(("tg://", "http://", "https://")):
            return s
        if s.startswith(("@", "+", "joinchat/", "t.me/", "telegram.me/", "www.t.me/")):
            return s
        if "t.me/" in s or "telegram.me/" in s:
            # например редиректы с параметрами, где внутри есть t.me
            i = s.find("t.me/")
            if i != -1:
                return "https://" + s[i:]
        return None

    if isinstance(obj, dict):
        # сначала "вероятные" ключи
        preferred = ("link", "url", "href", "invite_link", "channel_url", "button_url", "resource", "data")
        for k in preferred:
            if k in obj:
                found = _deep_find_link(obj.get(k), depth=depth - 1, seen=seen + 1, limit=limit)
                if found:
                    return found
        # затем всё остальное
        for v in obj.values():
            found = _deep_find_link(v, depth=depth - 1, seen=seen + 1, limit=limit)
            if found:
                return found
        return None

    if isinstance(obj, (list, tuple, set)):
        for v in obj:
            found = _deep_find_link(v, depth=depth - 1, seen=seen + 1, limit=limit)
            if found:
                return found
        return None

    return None


class FlyerClient:
    """
    Тонкая обёртка над flyerapi.
    Ключ храните в переменной окружения FLYER_API_KEY (не в коде).
    """

    def __init__(self, key: str) -> None:
        from flyerapi import Flyer  # type: ignore

        self._flyer = Flyer(key)

    async def check_sub(self, user_id: int, language_code: str | None, message: dict | None = None) -> bool:
        """
        Проверка обязательной подписки через Flyer.
        В типичном сценарии Flyer сам показывает пользователю сообщение/кнопки, а мы просто останавливаем хендлер.
        """
        if message is None:
            return bool(await self._flyer.check(user_id, language_code=language_code))
        return bool(await self._flyer.check(user_id, language_code=language_code, message=message))

    async def get_tasks_raw(self, user_id: int, language_code: str | None, limit: int = 5) -> Any:
        """
        Возвращает "сырой" ответ flyerapi.get_tasks без фильтрации/парсинга.
        Удобно для диагностики, потому что структура может меняться.
        """
        return await self._flyer.get_tasks(user_id=user_id, language_code=language_code, limit=limit)

    async def get_tasks(self, user_id: int, language_code: str | None, limit: int = 5) -> list[FlyerTask]:
        tasks = await self._flyer.get_tasks(user_id=user_id, language_code=language_code, limit=limit)
        out: list[FlyerTask] = []
        for t in tasks or []:
            if not isinstance(t, dict):
                continue
            signature = str(t.get("signature") or "").strip()
            if not signature:
                continue
            link: Any = t.get("link") or t.get("url") or t.get("channel_url") or t.get("invite_link")
            # flyerapi иногда возвращает ссылку как dict/list
            if isinstance(link, dict):
                link = link.get("url") or link.get("link") or link.get("href")
            if isinstance(link, (list, tuple)) and link:
                link = link[0]
            if link is not None:
                link = str(link).strip().strip('"').strip("'")
                link = link.replace("\\/", "/")
            if not link:
                # Иногда ссылка не приходит, но приходит username канала
                for k in ("channel_username", "username", "channel", "domain", "login"):
                    v = t.get(k)
                    if v is None:
                        continue
                    v = str(v).strip()
                    if not v:
                        continue
                    link = v
                    break
            if not link:
                link = _deep_find_link(t)

            if link:
                if link.startswith("@"):
                    link = f"https://t.me/{link.lstrip('@')}"
                elif link.startswith("+"):
                    link = f"https://t.me/{link}"
                elif link.startswith("joinchat/"):
                    link = "https://t.me/" + link
                elif link.startswith("t.me/") or link.startswith("telegram.me/"):
                    link = "https://" + link
                elif link.startswith("www.t.me/"):
                    link = "https://" + link[len("www.") :]
                elif "://" not in link:
                    # просто username без @
                    if link.replace("_", "").isalnum() and 4 < len(link) < 80:
                        link = f"https://t.me/{link}"
                    else:
                        link = None

            title = t.get("title") or t.get("name") or t.get("channel_name")
            if title is not None:
                title = str(title)

            reward = None
            for k in ("reward", "price", "amount", "value"):
                if k in t and t.get(k) is not None:
                    try:
                        reward = float(t.get(k))
                        break
                    except Exception:
                        pass

            out.append(
                FlyerTask(
                    signature=signature,
                    link=link,
                    title=title,
                    reward=reward,
                    raw=t,
                )
            )
        return out

    async def check_task(self, user_id: int, signature: str) -> Any:
        return await self._flyer.check_task(user_id=user_id, signature=signature)
