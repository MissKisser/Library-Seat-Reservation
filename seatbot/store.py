"""Async SQLite state store."""
from __future__ import annotations

import json
import time as _time
from dataclasses import dataclass
from datetime import date
from typing import Any

import aiosqlite

from seatbot.models import Account, Task, TaskStatus


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id          TEXT PRIMARY KEY,
  phone       TEXT NOT NULL,
  password    TEXT NOT NULL,
  seat_num    TEXT NOT NULL,
  slots_json  TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'active',
  bootstrap_day TEXT,
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id  TEXT NOT NULL,
  day         TEXT NOT NULL,
  start_time  TEXT NOT NULL,
  end_time    TEXT NOT NULL,
  status      TEXT NOT NULL,
  reserve_id  INTEGER,
  last_error  TEXT,
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_account_day ON tasks(account_id, day);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS actions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          INTEGER NOT NULL,
  account_id  TEXT NOT NULL,
  action      TEXT NOT NULL,
  request     TEXT,
  response    TEXT,
  success     INTEGER NOT NULL,
  message     TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_account_ts ON actions(account_id, ts DESC);

CREATE TABLE IF NOT EXISTS logs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          INTEGER NOT NULL,
  level       TEXT NOT NULL,
  account_id  TEXT,
  message     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts DESC);
"""


@dataclass
class ActionRow:
    id: int
    ts: int
    account_id: str
    action: str
    request: str | None
    response: str | None
    success: bool
    message: str | None


@dataclass
class LogRow:
    id: int
    ts: int
    level: str
    account_id: str | None
    message: str


class StateStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if not self._db:
            raise RuntimeError("StateStore not initialized; call init() first")
        return self._db

    # ---------- introspection ----------
    async def list_tables(self) -> list[str]:
        cur = await self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        rows = await cur.fetchall()
        return [r[0] for r in rows]

    # ---------- accounts ----------
    async def upsert_account(self, acc: Account) -> None:
        now = int(_time.time() * 1000)
        slots_json = acc.slots if isinstance(acc.slots, str) else json.dumps(acc.slots)
        cur = await self.db.execute(
            "SELECT created_at FROM accounts WHERE id=?", (acc.id,)
        )
        row = await cur.fetchone()
        if row is None:
            await self.db.execute(
                """INSERT INTO accounts
                   (id, phone, password, seat_num, slots_json, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
                (acc.id, acc.phone, acc.password, acc.seat_num, slots_json, now, now),
            )
        else:
            await self.db.execute(
                """UPDATE accounts SET phone=?, password=?, seat_num=?, slots_json=?, updated_at=?
                   WHERE id=?""",
                (acc.phone, acc.password, acc.seat_num, slots_json, now, acc.id),
            )
        await self.db.commit()

    async def get_account(self, acc_id: str) -> Account | None:
        cur = await self.db.execute(
            "SELECT id, phone, password, seat_num, slots_json FROM accounts WHERE id=?",
            (acc_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        slots = row[4]
        if slots not in ("full",):
            try:
                slots = json.loads(slots)
            except Exception:
                pass
        return Account(id=row[0], phone=row[1], password=row[2], seat_num=row[3], slots=slots)

    async def list_accounts(self) -> list[Account]:
        cur = await self.db.execute("SELECT id FROM accounts ORDER BY id")
        rows = await cur.fetchall()
        out: list[Account] = []
        for r in rows:
            a = await self.get_account(r[0])
            if a:
                out.append(a)
        return out

    async def sync_accounts(self, accounts: list[Account]) -> int:
        """Upsert a batch of accounts from config. Returns count upserted."""
        for acc in accounts:
            await self.upsert_account(acc)
        return len(accounts)

    async def delete_account(self, acc_id: str) -> None:
        await self.db.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
        await self.db.commit()

    async def set_bootstrap_day(self, acc_id: str, day: date) -> None:
        await self.db.execute(
            "UPDATE accounts SET bootstrap_day=? WHERE id=?", (day.isoformat(), acc_id)
        )
        await self.db.commit()

    async def get_bootstrap_day(self, acc_id: str) -> date | None:
        cur = await self.db.execute(
            "SELECT bootstrap_day FROM accounts WHERE id=?", (acc_id,)
        )
        row = await cur.fetchone()
        if not row or not row[0]:
            return None
        return date.fromisoformat(row[0])

    # ---------- tasks ----------
    async def add_task(self, t: Task) -> int:
        now = int(_time.time() * 1000)
        cur = await self.db.execute(
            """INSERT INTO tasks
               (account_id, day, start_time, end_time, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                t.account_id, t.day.isoformat(),
                t.start_time.isoformat(timespec="minutes"),
                t.end_time.isoformat(timespec="minutes"),
                t.status.value, now, now,
            ),
        )
        await self.db.commit()
        return cur.lastrowid or 0

    async def update_task_status(
        self,
        task_id: int,
        status: TaskStatus,
        *,
        reserve_id: int | None = None,
        last_error: str | None = None,
    ) -> None:
        now = int(_time.time() * 1000)
        await self.db.execute(
            """UPDATE tasks
               SET status=?, reserve_id=COALESCE(?, reserve_id),
                   last_error=COALESCE(?, last_error), updated_at=?
               WHERE id=?""",
            (status.value, reserve_id, last_error, now, task_id),
        )
        await self.db.commit()

    async def get_task(self, task_id: int) -> Task | None:
        cur = await self.db.execute(
            "SELECT id, account_id, day, start_time, end_time, status, reserve_id, last_error FROM tasks WHERE id=?",
            (task_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return _row_to_task(row)

    async def list_tasks(
        self, account_id: str | None = None, day: date | None = None
    ) -> list[Task]:
        q = "SELECT id, account_id, day, start_time, end_time, status, reserve_id, last_error FROM tasks WHERE 1=1"
        args: list[Any] = []
        if account_id:
            q += " AND account_id=?"
            args.append(account_id)
        if day:
            q += " AND day=?"
            args.append(day.isoformat())
        q += " ORDER BY day, start_time"
        cur = await self.db.execute(q, args)
        rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]

    async def find_active_task(self, account_id: str) -> Task | None:
        cur = await self.db.execute(
            "SELECT id, account_id, day, start_time, end_time, status, reserve_id, last_error "
            "FROM tasks WHERE account_id=? AND status IN ('active','submitting','leaving') "
            "ORDER BY day DESC, start_time DESC LIMIT 1",
            (account_id,),
        )
        row = await cur.fetchone()
        return _row_to_task(row) if row else None

    # ---------- actions / logs ----------
    async def log_action(
        self,
        account_id: str,
        action: str,
        request: str | None,
        response: str | None,
        success: bool,
        message: str | None = None,
    ) -> None:
        now = int(_time.time() * 1000)
        await self.db.execute(
            "INSERT INTO actions (ts, account_id, action, request, response, success, message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now, account_id, action, request, response, 1 if success else 0, message),
        )
        await self.db.commit()

    async def list_actions(
        self, account_id: str | None = None, limit: int = 50
    ) -> list[ActionRow]:
        q = "SELECT id, ts, account_id, action, request, response, success, message FROM actions"
        args: list[Any] = []
        if account_id:
            q += " WHERE account_id=?"
            args.append(account_id)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        cur = await self.db.execute(q, args)
        rows = await cur.fetchall()
        return [
            ActionRow(
                id=r[0], ts=r[1], account_id=r[2], action=r[3],
                request=r[4], response=r[5], success=bool(r[6]), message=r[7],
            )
            for r in rows
        ]

    async def log_message(
        self, level: str, account_id: str | None, message: str
    ) -> None:
        now = int(_time.time() * 1000)
        await self.db.execute(
            "INSERT INTO logs (ts, level, account_id, message) VALUES (?, ?, ?, ?)",
            (now, level, account_id, message),
        )
        await self.db.commit()

    async def list_logs(
        self,
        account_id: str | None = None,
        level: str | None = None,
        limit: int = 200,
    ) -> list[LogRow]:
        q = "SELECT id, ts, level, account_id, message FROM logs WHERE 1=1"
        args: list[Any] = []
        if account_id:
            q += " AND account_id=?"
            args.append(account_id)
        if level:
            q += " AND level=?"
            args.append(level)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        cur = await self.db.execute(q, args)
        rows = await cur.fetchall()
        return [LogRow(id=r[0], ts=r[1], level=r[2], account_id=r[3], message=r[4]) for r in rows]


def _row_to_task(row) -> Task:
    from datetime import time
    h, m = map(int, row[3].split(":"))
    eh, em = map(int, row[4].split(":"))
    return Task(
        id=row[0],
        account_id=row[1],
        day=date.fromisoformat(row[2]),
        start_time=time(h, m),
        end_time=time(eh, em),
        status=TaskStatus(row[5]),
        reserve_id=row[6],
        last_error=row[7],
    )