"""Хранилище: пользователи, воронка, подписки, платежи (SQLite)."""
import time

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY,          -- Telegram user id
    username        TEXT,
    first_name      TEXT,
    source          TEXT,                         -- метка из ссылки t.me/bot?start=<source>
    created_at      INTEGER NOT NULL,
    funnel          TEXT,                         -- 'funnel' | 'winback' | NULL (не в воронке)
    funnel_step     INTEGER NOT NULL DEFAULT 0,   -- индекс следующего сообщения
    funnel_next_at  INTEGER,                      -- когда отправить следующее
    sub_until       INTEGER,                      -- доступ оплачен до (unix)
    sub_active      INTEGER NOT NULL DEFAULT 0,
    last_charge_id  TEXT,                         -- для отмены автопродления Stars
    autorenew       INTEGER NOT NULL DEFAULT 0,
    reminders_sent  TEXT NOT NULL DEFAULT '',     -- '3d,1d' за текущий период
    daily_time      TEXT,                         -- 'HH:MM' личное ежедневное напоминание
    daily_last      TEXT,                         -- дата последней отправки 'YYYY-MM-DD'
    blocked         INTEGER NOT NULL DEFAULT 0    -- пользователь заблокировал бота
);
CREATE TABLE IF NOT EXISTS payments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL,
    amount              INTEGER NOT NULL,         -- звёзды или копейки
    currency            TEXT NOT NULL,
    charge_id           TEXT UNIQUE,
    provider_charge_id  TEXT,
    is_recurring        INTEGER NOT NULL DEFAULT 0,
    created_at          INTEGER NOT NULL
);
"""


def now() -> int:
    return int(time.time())


class DB:
    def __init__(self, path):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self):
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def _exec(self, sql, params=()):
        await self.conn.execute(sql, params)
        await self.conn.commit()

    async def _all(self, sql, params=()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchall()

    async def _one(self, sql, params=()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    # ── пользователи ──────────────────────────────────────
    async def get_user(self, user_id: int):
        return await self._one("SELECT * FROM users WHERE id = ?", (user_id,))

    async def add_user(self, user_id, username, first_name, source, first_delay_s):
        """Создаёт пользователя и ставит его в воронку прогрева. True — если новый."""
        has_funnel = first_delay_s is not None
        async with self.conn.execute(
            "INSERT OR IGNORE INTO users (id, username, first_name, source, created_at,"
            " funnel, funnel_step, funnel_next_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            (user_id, username, first_name, source, now(),
             "funnel" if has_funnel else None, now() + first_delay_s if has_funnel else None),
        ) as cur:
            created = cur.rowcount > 0
        if not created:
            await self.conn.execute(
                "UPDATE users SET username = ?, first_name = ?, blocked = 0 WHERE id = ?",
                (username, first_name, user_id),
            )
        await self.conn.commit()
        return created

    async def set_blocked(self, user_id: int):
        await self._exec("UPDATE users SET blocked = 1 WHERE id = ?", (user_id,))

    # ── воронка ───────────────────────────────────────────
    async def due_funnel(self):
        return await self._all(
            "SELECT * FROM users WHERE funnel IS NOT NULL AND funnel_next_at IS NOT NULL"
            " AND funnel_next_at <= ? AND blocked = 0",
            (now(),),
        )

    async def set_funnel(self, user_id, funnel, step, next_at):
        await self._exec(
            "UPDATE users SET funnel = ?, funnel_step = ?, funnel_next_at = ? WHERE id = ?",
            (funnel, step, next_at, user_id),
        )

    async def stop_funnel(self, user_id):
        await self.set_funnel(user_id, None, 0, None)

    # ── подписка ──────────────────────────────────────────
    async def activate(self, user_id, until, charge_id, autorenew):
        await self._exec(
            "UPDATE users SET sub_until = ?, sub_active = 1, last_charge_id = ?,"
            " autorenew = ?, reminders_sent = '', funnel = NULL, funnel_next_at = NULL"
            " WHERE id = ?",
            (until, charge_id, int(autorenew), user_id),
        )

    async def set_autorenew(self, user_id, value: bool):
        await self._exec("UPDATE users SET autorenew = ? WHERE id = ?", (int(value), user_id))

    async def deactivate(self, user_id):
        await self._exec(
            "UPDATE users SET sub_active = 0, autorenew = 0 WHERE id = ?", (user_id,)
        )

    async def active_subs(self):
        return await self._all("SELECT * FROM users WHERE sub_active = 1")

    async def mark_reminder(self, user_id, current: str, key: str):
        value = ",".join(filter(None, [current, key]))
        await self._exec("UPDATE users SET reminders_sent = ? WHERE id = ?", (value, user_id))

    # ── платежи ───────────────────────────────────────────
    async def add_payment(self, user_id, amount, currency, charge_id, provider_charge_id,
                          is_recurring) -> bool:
        """False — если такой платёж уже записан (защита от дублей)."""
        async with self.conn.execute(
            "INSERT OR IGNORE INTO payments (user_id, amount, currency, charge_id,"
            " provider_charge_id, is_recurring, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, amount, currency, charge_id, provider_charge_id, int(is_recurring), now()),
        ) as cur:
            inserted = cur.rowcount > 0
        await self.conn.commit()
        return inserted

    # ── ежедневные напоминания ────────────────────────────
    async def set_daily(self, user_id, hhmm: str | None, last: str | None = None):
        """last — дата, за которую напоминание считается уже отправленным."""
        await self._exec(
            "UPDATE users SET daily_time = ?, daily_last = ? WHERE id = ?", (hhmm, last, user_id)
        )

    async def daily_candidates(self, hhmm: str, today: str):
        return await self._all(
            "SELECT * FROM users WHERE daily_time IS NOT NULL AND daily_time <= ?"
            " AND (daily_last IS NULL OR daily_last < ?) AND blocked = 0",
            (hhmm, today),
        )

    async def mark_daily(self, user_id, today: str):
        await self._exec("UPDATE users SET daily_last = ? WHERE id = ?", (today, user_id))

    # ── админка ───────────────────────────────────────────
    async def audience(self, segment: str):
        where = {
            "all": "1 = 1",
            "subs": "sub_active = 1",
            "free": "sub_active = 0",
        }[segment]
        return await self._all(f"SELECT id FROM users WHERE blocked = 0 AND {where}")

    async def stats(self):
        q = lambda sql, p=(): self._one(sql, p)  # noqa: E731
        month_ago = now() - 30 * 24 * 3600
        return {
            "users": (await q("SELECT COUNT(*) c FROM users"))["c"],
            "blocked": (await q("SELECT COUNT(*) c FROM users WHERE blocked = 1"))["c"],
            "active": (await q("SELECT COUNT(*) c FROM users WHERE sub_active = 1"))["c"],
            "ever_paid": (await q("SELECT COUNT(DISTINCT user_id) c FROM payments"))["c"],
            "new_30d": (await q("SELECT COUNT(*) c FROM users WHERE created_at >= ?",
                                (month_ago,)))["c"],
            "revenue_30d": await self._all(
                "SELECT currency, SUM(amount) s, COUNT(*) n FROM payments"
                " WHERE created_at >= ? GROUP BY currency", (month_ago,)),
            "funnel": await self._all(
                "SELECT funnel, funnel_step, COUNT(*) n FROM users WHERE funnel IS NOT NULL"
                " GROUP BY funnel, funnel_step ORDER BY funnel, funnel_step"),
            "sources": await self._all(
                "SELECT COALESCE(source, '—') src, COUNT(*) n,"
                " SUM(id IN (SELECT user_id FROM payments)) paid"
                " FROM users GROUP BY src ORDER BY n DESC LIMIT 10"),
        }
