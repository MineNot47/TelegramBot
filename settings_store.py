from __future__ import annotations

from dataclasses import dataclass

from db import Database


@dataclass
class SettingsStore:
    """
    Кэш настроек в памяти + хранение в SQLite.
    """

    db: Database
    defaults: dict[str, float | int]
    _values: dict[str, str]

    @classmethod
    async def create(cls, db: Database, defaults: dict[str, float | int]) -> "SettingsStore":
        store = cls(db=db, defaults=defaults, _values={})
        await store.load()
        return store

    async def load(self) -> None:
        values = await self.db.all_settings()
        self._values = values
        # Заполняем отсутствующие значения дефолтами.
        for k, v in self.defaults.items():
            if k not in self._values:
                await self.db.set_setting(k, str(v))
                self._values[k] = str(v)

    def get_str(self, key: str) -> str:
        if key in self._values:
            return self._values[key]
        if key in self.defaults:
            return str(self.defaults[key])
        raise KeyError(key)

    def get_float(self, key: str) -> float:
        return float(self.get_str(key))

    def get_int(self, key: str) -> int:
        return int(float(self.get_str(key)))

    async def set_value(self, key: str, value: float | int | str) -> None:
        await self.db.set_setting(key, str(value))
        self._values[key] = str(value)

