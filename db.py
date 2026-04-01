from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import aiosqlite


@dataclass(frozen=True)
class User:
    user_id: int
    username: str | None
    balance: float
    registered_at: int
    referrer_id: int | None
    invited_count: int
    last_seen_at: int


class Database:
    def __init__(self, path: str = "bot.sqlite3") -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        # Убедимся, что папка для БД существует (важно для деплоя, например DB_PATH=/data/bot.sqlite3).
        dir_path = os.path.dirname(os.path.abspath(self.path))
        if dir_path and not os.path.exists(dir_path):
            os.makedirs(dir_path, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def init(self) -> None:
        # users: user_id, username, balance, дата регистрации, реферал, приглашено, last_seen
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id        INTEGER PRIMARY KEY,
                username       TEXT,
                balance        REAL NOT NULL DEFAULT 0,
                registered_at  INTEGER NOT NULL,
                referrer_id    INTEGER,
                invited_count  INTEGER NOT NULL DEFAULT 0,
                last_seen_at   INTEGER NOT NULL,
                FOREIGN KEY(referrer_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS bans (
                user_id      INTEGER PRIMARY KEY,
                reason       TEXT,
                banned_at    INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sponsor_channels (
                chat_id INTEGER PRIMARY KEY,
                title   TEXT,
                username TEXT,
                url      TEXT,
                check_required INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS sponsor_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                url TEXT NOT NULL,
                check_required INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS task_channels (
                chat_id INTEGER PRIMARY KEY,
                title   TEXT,
                username TEXT,
                url      TEXT,
                reward   REAL,
                check_required INTEGER NOT NULL DEFAULT 1
            );

            -- Задания-ссылки (без проверки) — как sponsor_links, но с наградой
            CREATE TABLE IF NOT EXISTS task_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                url TEXT NOT NULL,
                reward REAL,
                check_required INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS task_link_completions (
                user_id INTEGER NOT NULL,
                link_id INTEGER NOT NULL,
                done_at INTEGER NOT NULL,
                PRIMARY KEY(user_id, link_id),
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY(link_id) REFERENCES task_links(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS task_completions (
                user_id  INTEGER NOT NULL,
                chat_id  INTEGER NOT NULL,
                done_at  INTEGER NOT NULL,
                PRIMARY KEY(user_id, chat_id),
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS flyer_task_completions (
                user_id   INTEGER NOT NULL,
                signature TEXT NOT NULL,
                done_at   INTEGER NOT NULL,
                PRIMARY KEY(user_id, signature),
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            -- Метаданные заданий Flyer (чтобы вебхуки могли начислять ту же награду)
            CREATE TABLE IF NOT EXISTS flyer_task_meta (
                signature  TEXT PRIMARY KEY,
                reward     REAL,
                title      TEXT,
                link       TEXT,
                updated_at INTEGER NOT NULL
            );

            -- Отложенные реферальные награды (после выполнения заданий)
            CREATE TABLE IF NOT EXISTS referrals (
                user_id       INTEGER PRIMARY KEY,  -- приглашённый
                referrer_id   INTEGER NOT NULL,      -- пригласивший
                created_at    INTEGER NOT NULL,
                required_tasks INTEGER NOT NULL DEFAULT 2,
                notified_at   INTEGER,
                rewarded_at   INTEGER,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY(referrer_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            -- Заморозка баланса (ограничение заработка/вывода)
            CREATE TABLE IF NOT EXISTS balance_freezes (
                user_id   INTEGER PRIMARY KEY,
                reason    TEXT,
                frozen_at INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS gifts (
                user_id        INTEGER PRIMARY KEY,
                last_gift_at   INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS promo_codes (
                code        TEXT PRIMARY KEY,
                reward      REAL NOT NULL,
                uses_limit  INTEGER NOT NULL,
                used_count  INTEGER NOT NULL DEFAULT 0,
                created_at  INTEGER NOT NULL,
                expires_at  INTEGER
            );

            CREATE TABLE IF NOT EXISTS promo_activations (
                code       TEXT NOT NULL,
                user_id    INTEGER NOT NULL,
                used_at    INTEGER NOT NULL,
                PRIMARY KEY(code, user_id),
                FOREIGN KEY(code) REFERENCES promo_codes(code) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS withdrawals (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id        INTEGER NOT NULL,
                username       TEXT,
                item           TEXT NOT NULL,
                amount         REAL NOT NULL,
                created_at     INTEGER NOT NULL,
                status         TEXT NOT NULL DEFAULT 'pending', -- pending/approved/declined
                admin_id       INTEGER,
                processed_at   INTEGER,
                decline_reason TEXT,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
            """
        )
        # Миграция для старых баз (если таблицы уже существовали без username).
        for table in ("sponsor_channels", "task_channels"):
            try:
                await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN username TEXT;")
            except Exception:
                pass
        # Миграция sponsor_channels (url/check_required).
        try:
            await self.conn.execute("ALTER TABLE sponsor_channels ADD COLUMN url TEXT;")
        except Exception:
            pass
        try:
            await self.conn.execute("ALTER TABLE sponsor_channels ADD COLUMN check_required INTEGER NOT NULL DEFAULT 1;")
        except Exception:
            pass
        # Миграция task_channels (url/reward/check_required).
        try:
            await self.conn.execute("ALTER TABLE task_channels ADD COLUMN url TEXT;")
        except Exception:
            pass
        try:
            await self.conn.execute("ALTER TABLE task_channels ADD COLUMN reward REAL;")
        except Exception:
            pass
        try:
            await self.conn.execute("ALTER TABLE task_channels ADD COLUMN check_required INTEGER NOT NULL DEFAULT 1;")
        except Exception:
            pass
        # Миграция промокодов (expires_at).
        try:
            await self.conn.execute("ALTER TABLE promo_codes ADD COLUMN expires_at INTEGER;")
        except Exception:
            pass
        await self.conn.commit()

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        await self.conn.execute(sql, tuple(params))
        await self.conn.commit()

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> Optional[aiosqlite.Row]:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return await cur.fetchall()

    # ---- users ----
    async def get_user(self, user_id: int) -> User | None:
        row = await self.fetchone("SELECT * FROM users WHERE user_id=?", (user_id,))
        if not row:
            return None
        return User(
            user_id=row["user_id"],
            username=row["username"],
            balance=float(row["balance"]),
            registered_at=int(row["registered_at"]),
            referrer_id=row["referrer_id"],
            invited_count=int(row["invited_count"]),
            last_seen_at=int(row["last_seen_at"]),
        )

    async def find_user_by_username(self, username: str) -> User | None:
        uname = username.strip().lstrip("@")
        if not uname:
            return None
        row = await self.fetchone(
            "SELECT * FROM users WHERE LOWER(username)=LOWER(?) LIMIT 1",
            (uname,),
        )
        if not row:
            return None
        return User(
            user_id=row["user_id"],
            username=row["username"],
            balance=float(row["balance"]),
            registered_at=int(row["registered_at"]),
            referrer_id=row["referrer_id"],
            invited_count=int(row["invited_count"]),
            last_seen_at=int(row["last_seen_at"]),
        )

    async def upsert_user(
        self,
        user_id: int,
        username: str | None,
        *,
        referrer_id: int | None = None,
        now: int | None = None,
    ) -> tuple[bool, User]:
        """
        Возвращает (created, user).
        created=True если пользователь был создан впервые (и это момент регистрации).
        """
        now_ts = int(time.time()) if now is None else int(now)
        existing = await self.get_user(user_id)
        if existing:
            await self.execute(
                "UPDATE users SET username=?, last_seen_at=? WHERE user_id=?",
                (username, now_ts, user_id),
            )
            updated = await self.get_user(user_id)
            assert updated is not None
            return False, updated

        await self.execute(
            """
            INSERT INTO users(user_id, username, balance, registered_at, referrer_id, invited_count, last_seen_at)
            VALUES(?, ?, 0, ?, ?, 0, ?)
            """,
            (user_id, username, now_ts, referrer_id, now_ts),
        )
        created = await self.get_user(user_id)
        assert created is not None
        return True, created

    async def change_balance(self, user_id: int, delta: float) -> None:
        await self.execute(
            "UPDATE users SET balance = ROUND(balance + ?, 8) WHERE user_id=?",
            (float(delta), user_id),
        )

    async def set_balance(self, user_id: int, value: float) -> None:
        await self.execute("UPDATE users SET balance=? WHERE user_id=?", (float(value), user_id))

    async def count_users(self) -> int:
        row = await self.fetchone("SELECT COUNT(*) AS c FROM users")
        return int(row["c"] if row else 0)

    async def count_active_users(self, seconds: int) -> int:
        cutoff = int(time.time()) - int(seconds)
        row = await self.fetchone("SELECT COUNT(*) AS c FROM users WHERE last_seen_at>=?", (cutoff,))
        return int(row["c"] if row else 0)

    async def list_user_ids(self) -> list[int]:
        rows = await self.fetchall("SELECT user_id FROM users")
        return [int(r["user_id"]) for r in rows]

    # ---- referrals ----
    async def add_referral(self, user_id: int, referrer_id: int, *, required_tasks: int = 2) -> None:
        """
        Создаёт отложенную реф-связь (если уже есть — не трогаем).
        Награда будет выдана после required_tasks выполненных заданий.
        """
        await self.execute(
            """
            INSERT OR IGNORE INTO referrals(user_id, referrer_id, created_at, required_tasks)
            VALUES(?, ?, ?, ?)
            """,
            (int(user_id), int(referrer_id), int(time.time()), int(required_tasks)),
        )

    async def count_done_tasks_total(self, user_id: int) -> int:
        row = await self.fetchone(
            """
            SELECT
              (SELECT COUNT(*) FROM task_completions WHERE user_id=?) +
              (SELECT COUNT(*) FROM flyer_task_completions WHERE user_id=?) AS c
            """,
            (int(user_id), int(user_id)),
        )
        return int(row["c"] if row else 0)

    # ---- balance freeze ----
    async def is_balance_frozen(self, user_id: int) -> bool:
        row = await self.fetchone("SELECT 1 FROM balance_freezes WHERE user_id=?", (int(user_id),))
        return bool(row)

    async def get_balance_freeze(self, user_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM balance_freezes WHERE user_id=?", (int(user_id),))

    async def freeze_balance(self, user_id: int, reason: str | None = None) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO balance_freezes(user_id, reason, frozen_at) VALUES(?, ?, ?)",
            (int(user_id), reason, int(time.time())),
        )

    async def unfreeze_balance(self, user_id: int) -> None:
        await self.execute("DELETE FROM balance_freezes WHERE user_id=?", (int(user_id),))

    async def list_balance_freezes(self) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM balance_freezes ORDER BY frozen_at DESC")

    async def try_reward_referral(
        self,
        user_id: int,
        *,
        ref_reward: float,
        ref_bonus: float | None = None,
    ) -> int | None:
        """
        Если пользователь был приглашён по реф-ссылке и выполнил достаточно заданий,
        выдаёт награду пригласившему (один раз).
        Возвращает referrer_id, если награда была выдана.
        """
        ref = await self.fetchone(
            "SELECT referrer_id, required_tasks, rewarded_at FROM referrals WHERE user_id=?",
            (int(user_id),),
        )
        if not ref:
            return None
        if ref["rewarded_at"] is not None:
            return None
        required = int(ref["required_tasks"] or 2)
        done = await self.count_done_tasks_total(int(user_id))
        if done < required:
            return None

        now = int(time.time())
        await self.conn.execute("BEGIN IMMEDIATE")
        try:
            ref2 = await self.fetchone(
                "SELECT referrer_id, required_tasks, rewarded_at FROM referrals WHERE user_id=?",
                (int(user_id),),
            )
            if not ref2 or ref2["rewarded_at"] is not None:
                await self.conn.execute("ROLLBACK")
                return None
            referrer_id = int(ref2["referrer_id"])
            # Если у пригласившего заморозка — не начисляем сейчас (чтобы можно было начислить после разморозки).
            frozen = await self.fetchone("SELECT 1 FROM balance_freezes WHERE user_id=?", (referrer_id,))
            if frozen:
                await self.conn.execute("ROLLBACK")
                return None
            cur = await self.conn.execute(
                "UPDATE referrals SET rewarded_at=? WHERE user_id=? AND rewarded_at IS NULL",
                (now, int(user_id)),
            )
            if cur.rowcount == 0:
                await self.conn.execute("ROLLBACK")
                return None
            await self.conn.execute(
                "UPDATE users SET balance = ROUND(balance + ?, 8) WHERE user_id=?",
                (float(ref_reward), referrer_id),
            )
            await self.conn.commit()
            return referrer_id
        except Exception:
            try:
                await self.conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    # ---- bans ----
    async def is_banned(self, user_id: int) -> bool:
        row = await self.fetchone("SELECT 1 FROM bans WHERE user_id=?", (user_id,))
        return bool(row)

    async def ban_user(self, user_id: int, reason: str | None = None) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO bans(user_id, reason, banned_at) VALUES(?, ?, ?)",
            (user_id, reason, int(time.time())),
        )

    async def unban_user(self, user_id: int) -> None:
        await self.execute("DELETE FROM bans WHERE user_id=?", (user_id,))

    async def list_bans(self) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM bans ORDER BY banned_at DESC")

    # ---- settings ----
    async def get_setting(self, key: str) -> str | None:
        row = await self.fetchone("SELECT value FROM settings WHERE key=?", (key,))
        return str(row["value"]) if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, value))

    async def all_settings(self) -> dict[str, str]:
        rows = await self.fetchall("SELECT key, value FROM settings")
        return {str(r["key"]): str(r["value"]) for r in rows}

    # ---- channels ----
    async def list_sponsor_channels(self) -> list[int]:
        rows = await self.fetchall("SELECT chat_id FROM sponsor_channels ORDER BY chat_id")
        return [int(r["chat_id"]) for r in rows]

    async def list_sponsor_channels_full(self) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT chat_id, title, username, url, check_required FROM sponsor_channels ORDER BY chat_id"
        )

    async def add_sponsor_channel(
        self,
        chat_id: int,
        title: str | None = None,
        username: str | None = None,
        url: str | None = None,
        check_required: bool = True,
    ) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO sponsor_channels(chat_id, title, username, url, check_required) VALUES(?, ?, ?, ?, ?)",
            (int(chat_id), title, username, url, 1 if check_required else 0),
        )

    async def remove_sponsor_channel(self, chat_id: int) -> None:
        await self.execute("DELETE FROM sponsor_channels WHERE chat_id=?", (int(chat_id),))

    async def add_sponsor_link(self, url: str, title: str | None = None, check_required: bool = False) -> int:
        cur = await self.conn.execute(
            "INSERT INTO sponsor_links(title, url, check_required) VALUES(?, ?, ?)",
            (title, url, 1 if check_required else 0),
        )
        await self.conn.commit()
        return int(cur.lastrowid)

    async def remove_sponsor_link(self, link_id: int) -> None:
        await self.execute("DELETE FROM sponsor_links WHERE id=?", (int(link_id),))

    async def list_sponsor_links(self) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT id, title, url, check_required FROM sponsor_links ORDER BY id DESC")

    async def list_task_channels(self) -> list[int]:
        rows = await self.fetchall("SELECT chat_id FROM task_channels ORDER BY chat_id")
        return [int(r["chat_id"]) for r in rows]

    async def list_task_channels_full(self) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT chat_id, title, username, url, reward, check_required FROM task_channels ORDER BY chat_id"
        )

    async def add_task_link(
        self,
        url: str,
        *,
        title: str | None = None,
        reward: float | None = None,
        check_required: bool = False,
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO task_links(title, url, reward, check_required) VALUES(?, ?, ?, ?)",
            (title, str(url).strip(), float(reward) if reward is not None else None, 1 if check_required else 0),
        )
        await self.conn.commit()
        return int(cur.lastrowid)

    async def remove_task_link(self, link_id: int) -> None:
        await self.execute("DELETE FROM task_links WHERE id=?", (int(link_id),))

    async def list_task_links(self) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT id, title, url, reward, check_required FROM task_links ORDER BY id DESC")

    async def get_task_link(self, link_id: int) -> aiosqlite.Row | None:
        return await self.fetchone(
            "SELECT id, title, url, reward, check_required FROM task_links WHERE id=?",
            (int(link_id),),
        )

    async def get_task_channel(self, chat_id: int) -> aiosqlite.Row | None:
        return await self.fetchone(
            "SELECT chat_id, title, username, url, reward, check_required FROM task_channels WHERE chat_id=?",
            (int(chat_id),),
        )

    async def add_task_channel(
        self,
        chat_id: int,
        title: str | None = None,
        username: str | None = None,
        url: str | None = None,
        reward: float | None = None,
        check_required: bool = True,
    ) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO task_channels(chat_id, title, username, url, reward, check_required) VALUES(?, ?, ?, ?, ?, ?)",
            (int(chat_id), title, username, url, reward, 1 if check_required else 0),
        )

    async def remove_task_channel(self, chat_id: int) -> None:
        await self.execute("DELETE FROM task_channels WHERE chat_id=?", (int(chat_id),))

    # ---- tasks ----
    async def is_task_done(self, user_id: int, chat_id: int) -> bool:
        row = await self.fetchone(
            "SELECT 1 FROM task_completions WHERE user_id=? AND chat_id=?",
            (int(user_id), int(chat_id)),
        )
        return bool(row)

    async def mark_task_done(self, user_id: int, chat_id: int) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO task_completions(user_id, chat_id, done_at) VALUES(?, ?, ?)",
            (int(user_id), int(chat_id), int(time.time())),
        )

    async def is_task_link_done(self, user_id: int, link_id: int) -> bool:
        row = await self.fetchone(
            "SELECT 1 FROM task_link_completions WHERE user_id=? AND link_id=?",
            (int(user_id), int(link_id)),
        )
        return bool(row)

    async def mark_task_link_done(self, user_id: int, link_id: int) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO task_link_completions(user_id, link_id, done_at) VALUES(?, ?, ?)",
            (int(user_id), int(link_id), int(time.time())),
        )

    async def is_flyer_task_done(self, user_id: int, signature: str) -> bool:
        row = await self.fetchone(
            "SELECT 1 FROM flyer_task_completions WHERE user_id=? AND signature=?",
            (int(user_id), signature),
        )
        return bool(row)

    async def mark_flyer_task_done(self, user_id: int, signature: str) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO flyer_task_completions(user_id, signature, done_at) VALUES(?, ?, ?)",
            (int(user_id), signature, int(time.time())),
        )

    async def upsert_flyer_task_meta(
        self,
        *,
        signature: str,
        reward: float | None,
        title: str | None = None,
        link: str | None = None,
    ) -> None:
        sig = str(signature).strip()
        if not sig:
            return
        await self.execute(
            """
            INSERT OR REPLACE INTO flyer_task_meta(signature, reward, title, link, updated_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (sig, float(reward) if reward is not None else None, title, link, int(time.time())),
        )

    async def get_flyer_task_reward(self, signature: str) -> float | None:
        row = await self.fetchone("SELECT reward FROM flyer_task_meta WHERE signature=?", (str(signature).strip(),))
        if not row:
            return None
        if row["reward"] is None:
            return None
        return float(row["reward"])

    # ---- gifts ----
    async def get_last_gift_at(self, user_id: int) -> int | None:
        row = await self.fetchone("SELECT last_gift_at FROM gifts WHERE user_id=?", (int(user_id),))
        return int(row["last_gift_at"]) if row else None

    async def set_last_gift_at(self, user_id: int, ts: int) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO gifts(user_id, last_gift_at) VALUES(?, ?)",
            (int(user_id), int(ts)),
        )

    # ---- promo ----
    async def create_promo(self, code: str, reward: float, uses_limit: int, expires_at: int | None = None) -> None:
        await self.execute(
            """
            INSERT INTO promo_codes(code, reward, uses_limit, used_count, created_at, expires_at)
            VALUES(?, ?, ?, 0, ?, ?)
            """,
            (code.upper().strip(), float(reward), int(uses_limit), int(time.time()), expires_at),
        )

    async def list_promos(self) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM promo_codes ORDER BY created_at DESC")

    async def activate_promo(self, user_id: int, code: str) -> tuple[bool, str, float]:
        """
        Возвращает (ok, message, reward).
        Операция атомарная.
        """
        code_norm = code.upper().strip()
        now_ts = int(time.time())

        await self.conn.execute("BEGIN IMMEDIATE;")
        try:
            promo = await self.fetchone("SELECT * FROM promo_codes WHERE code=?", (code_norm,))
            if not promo:
                await self.conn.execute("ROLLBACK;")
                return False, "Не существует", 0.0

            expires_at = promo["expires_at"]
            if expires_at is not None and now_ts > int(expires_at):
                await self.conn.execute("ROLLBACK;")
                return False, "Закончился", 0.0
            if int(promo["used_count"]) >= int(promo["uses_limit"]):
                await self.conn.execute("ROLLBACK;")
                return False, "Лимит активаций исчерпан.", 0.0

            already = await self.fetchone(
                "SELECT 1 FROM promo_activations WHERE code=? AND user_id=?",
                (code_norm, int(user_id)),
            )
            if already:
                await self.conn.execute("ROLLBACK;")
                return False, "Вы уже активировали этот промокод.", 0.0

            reward = float(promo["reward"])
            await self.conn.execute(
                "INSERT INTO promo_activations(code, user_id, used_at) VALUES(?, ?, ?)",
                (code_norm, int(user_id), now_ts),
            )
            await self.conn.execute(
                "UPDATE promo_codes SET used_count = used_count + 1 WHERE code=?",
                (code_norm,),
            )
            await self.conn.execute(
                "UPDATE users SET balance = ROUND(balance + ?, 8) WHERE user_id=?",
                (reward, int(user_id)),
            )
            await self.conn.commit()
            return True, "✅ Промокод активирован!", reward
        except Exception:
            await self.conn.execute("ROLLBACK;")
            raise

    # ---- withdrawals ----
    async def create_withdrawal(self, user_id: int, username: str | None, item: str, amount: float) -> int:
        """
        Создание заявки с удержанием средств сразу:
        - проверяем баланс
        - списываем amount
        - создаём withdrawals(status=pending)
        """
        now_ts = int(time.time())
        await self.conn.execute("BEGIN IMMEDIATE;")
        try:
            bal_row = await self.fetchone("SELECT balance FROM users WHERE user_id=?", (int(user_id),))
            current_balance = float(bal_row["balance"] if bal_row else 0.0)
            if current_balance + 1e-9 < float(amount):
                await self.conn.execute("ROLLBACK;")
                raise ValueError("Недостаточно баланса")

            await self.conn.execute(
                "UPDATE users SET balance = ROUND(balance - ?, 8) WHERE user_id=?",
                (float(amount), int(user_id)),
            )
            cur = await self.conn.execute(
                """
                INSERT INTO withdrawals(user_id, username, item, amount, created_at, status)
                VALUES(?, ?, ?, ?, ?, 'pending')
                """,
                (int(user_id), username, item, float(amount), now_ts),
            )
            await self.conn.commit()
            return int(cur.lastrowid)
        except Exception:
            await self.conn.execute("ROLLBACK;")
            raise

    async def get_withdrawal(self, wid: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM withdrawals WHERE id=?", (int(wid),))

    async def count_withdrawals(self, status: str | None = None) -> int:
        if status:
            row = await self.fetchone("SELECT COUNT(*) AS c FROM withdrawals WHERE status=?", (status,))
        else:
            row = await self.fetchone("SELECT COUNT(*) AS c FROM withdrawals")
        return int(row["c"] if row else 0)

    async def process_withdrawal(self, wid: int, admin_id: int, approve: bool, reason: str | None = None) -> aiosqlite.Row:
        """
        Атомарно:
        - проверяем pending
        - если approve: списываем баланс
        - меняем статус
        Возвращает запись заявки после обработки.
        """
        now_ts = int(time.time())

        await self.conn.execute("BEGIN IMMEDIATE;")
        try:
            row = await self.fetchone("SELECT * FROM withdrawals WHERE id=?", (int(wid),))
            if not row:
                await self.conn.execute("ROLLBACK;")
                raise ValueError("Заявка не найдена")
            if str(row["status"]) != "pending":
                await self.conn.execute("ROLLBACK;")
                return row

            if approve:
                await self.conn.execute(
                    """
                    UPDATE withdrawals
                    SET status='approved', admin_id=?, processed_at=?, decline_reason=NULL
                    WHERE id=?
                    """,
                    (int(admin_id), now_ts, int(wid)),
                )
            else:
                # Возврат средств при отказе.
                await self.conn.execute(
                    "UPDATE users SET balance = ROUND(balance + ?, 8) WHERE user_id=?",
                    (float(row["amount"]), int(row["user_id"])),
                )
                await self.conn.execute(
                    """
                    UPDATE withdrawals
                    SET status='declined', admin_id=?, processed_at=?, decline_reason=?
                    WHERE id=?
                    """,
                    (int(admin_id), now_ts, reason, int(wid)),
                )

            await self.conn.commit()
            updated = await self.fetchone("SELECT * FROM withdrawals WHERE id=?", (int(wid),))
            assert updated is not None
            return updated
        except Exception:
            await self.conn.execute("ROLLBACK;")
            raise
