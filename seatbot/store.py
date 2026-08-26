"""Async SQLite state store (v2: target_seats + bindings + per-task seat_num)."""
from __future__ import annotations

import json
import time as _time
from dataclasses import dataclass
from datetime import date, time
from typing import Any

import aiosqlite

from seatbot.models import Account, SeatTarget, Task, TaskStatus


# v2 schema:
#  - accounts:        + bound_seats_json
#  - target_seats:    NEW (multi-seat registry)
#  - tasks:           + seat_num
SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id          TEXT PRIMARY KEY,
  phone       TEXT NOT NULL,
  password    TEXT NOT NULL,
  slots_json  TEXT NOT NULL,
  seat_slots_json TEXT NOT NULL DEFAULT '{}',
  bound_seats_json  TEXT NOT NULL DEFAULT '[]',
  status      TEXT NOT NULL DEFAULT 'active',
  bootstrap_day TEXT,
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS target_seats (
  seat_num    TEXT PRIMARY KEY,
  label       TEXT NOT NULL DEFAULT '',
  enabled     INTEGER NOT NULL DEFAULT 1,
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id  TEXT NOT NULL,
  seat_num    TEXT NOT NULL DEFAULT '',  -- v2: per-task seat
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

-- 注:idx_tasks_seat_day 在迁移里强制创建 (依赖新增的 seat_num 列)

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

-- ★ 用户硬预约段 (亲述, scheduler 不应再覆盖)
CREATE TABLE IF NOT EXISTS user_reserved (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id  TEXT NOT NULL,
  seat_num    TEXT NOT NULL,
  day         TEXT NOT NULL,
  start_time  TEXT NOT NULL,
  end_time    TEXT NOT NULL,
  note        TEXT,
  created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_reserved_seat_day ON user_reserved(seat_num, day);
"""


# 旧账号表结构 (用于 v1→v2 schema 迁移判断)
_LEGACY_ACCOUNTS_COLS = {
    "id", "phone", "password", "slots_json", "status",
    "bootstrap_day", "created_at", "updated_at",
}


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
    def __init__(self, db_path: str, *, legacy_target_seat_num: str | None = None):
        """
        legacy_target_seat_num: 来自 v1 配置的目标座位号,用于 v1→v2 数据迁移
        (创建一条 target_seats 行,把 v1 的 tasks.seat_num 留空的回填)。
        """
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._legacy_target_seat_num = legacy_target_seat_num

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        await self._migrate_v1_to_v2()

    async def _migrate_v1_to_v2(self) -> None:
        """v1 → v2 schema migrations.

        1. accounts table may lack `bound_seats_json`: add the column.
        2. tasks table may lack `seat_num`:
           add column; backfill from legacy_target_seat_num where missing.
        3. seed target_seats with the legacy seat if provided.
        """
        cur = await self.db.execute("PRAGMA table_info(accounts)")
        acc_cols = {row[1] for row in await cur.fetchall()}

        # 1. accounts: 增加缺失列
        if "bound_seats_json" not in acc_cols:
            await self.db.execute(
                "ALTER TABLE accounts ADD COLUMN bound_seats_json TEXT NOT NULL DEFAULT '[]'"
            )
        # 如果 accounts 表是 v1 的 "裸" 表 (没有 enabled 等),让它继续用
        # v1 的精简字段 — 已通过 SCHEMA 重置 + DEFAULT 兼容。

        # 2. tasks: 增加 seat_num
        cur = await self.db.execute("PRAGMA table_info(tasks)")
        task_cols = {row[1] for row in await cur.fetchall()}
        if "seat_num" not in task_cols:
            await self.db.execute(
                "ALTER TABLE tasks ADD COLUMN seat_num TEXT NOT NULL DEFAULT ''"
            )
            await self.db.commit()

        # 3. seed target_seats + 回填 tasks.seat_num
        if self._legacy_target_seat_num:
            seat = self._legacy_target_seat_num
            cur = await self.db.execute(
                "SELECT 1 FROM target_seats WHERE seat_num=?", (seat,)
            )
            if not await cur.fetchone():
                now = int(_time.time() * 1000)
                await self.db.execute(
                    "INSERT INTO target_seats (seat_num, label, enabled, created_at, updated_at) "
                    "VALUES (?, ?, 1, ?, ?)",
                    (seat, "migrated from v1", now, now),
                )
            await self.db.execute(
                "UPDATE tasks SET seat_num=? WHERE seat_num=''", (seat,)
            )
        # 4. 索引补建 (post-migration,依赖新增的 seat_num 列)
        try:
            await self.db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_seat_day ON tasks(seat_num, day)"
            )
        except Exception:
            pass
        await self.db.commit()

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

    # ---------- user_reserved (用户硬预约段, 跳过 scheduler 重复预约) ----------
    async def add_user_reserved(self, account_id: str, seat_num: str,
                                 day: date, start_time: time, end_time: time,
                                 note: str = "") -> int:
        now = int(_time.time() * 1000)
        cur = await self.db.execute(
            """INSERT INTO user_reserved
               (account_id, seat_num, day, start_time, end_time, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (account_id, seat_num.zfill(3) if seat_num.isdigit() else seat_num,
             day.isoformat(),
             start_time.strftime("%H:%M"), end_time.strftime("%H:%M"),
             note, now),
        )
        await self.db.commit()
        return cur.lastrowid or 0

    async def delete_user_reserved(self, rid: int) -> None:
        await self.db.execute("DELETE FROM user_reserved WHERE id=?", (rid,))
        await self.db.commit()

    async def list_user_reserved(
        self, seat_num: str | None = None, day: date | None = None,
    ) -> list[dict]:
        q = "SELECT id, account_id, seat_num, day, start_time, end_time, note, created_at FROM user_reserved WHERE 1=1"
        args: list[Any] = []
        if seat_num:
            q += " AND seat_num=?"
            args.append(seat_num.zfill(3) if seat_num.isdigit() else seat_num)
        if day:
            q += " AND day=?"
            args.append(day.isoformat())
        q += " ORDER BY day, seat_num, start_time"
        cur = await self.db.execute(q, args)
        rows = await cur.fetchall()
        return [
            {
                "id": r[0], "account_id": r[1], "seat_num": r[2],
                "day": r[3], "start_time": r[4], "end_time": r[5],
                "note": r[6], "created_at": r[7],
            }
            for r in rows
        ]

    async def get_user_reserved_slot_set(self, day: date, seat_num: str) -> list[tuple[time, time]]:
        """返回某 (day, seat_num) 的所有 (start, end) tuples,scheduler 用以 skip。"""
        cur = await self.db.execute(
            "SELECT start_time, end_time FROM user_reserved "
            "WHERE day=? AND seat_num=? ORDER BY start_time",
            (day.isoformat(), seat_num.zfill(3) if seat_num.isdigit() else seat_num),
        )
        rows = await cur.fetchall()
        out: list[tuple[time, time]] = []
        for r in rows:
            sh, sm = map(int, r[0].split(":"))
            eh, em = map(int, r[1].split(":"))
            out.append((time(sh, sm), time(eh, em)))
        return out

    # ---------- accounts ----------
    async def upsert_account(self, acc: Account) -> None:
        now = int(_time.time() * 1000)
        slots_json = acc.slots if isinstance(acc.slots, str) else json.dumps(acc.slots)
        bound_json = json.dumps(acc.bound_seats)
        cur = await self.db.execute(
            "SELECT created_at FROM accounts WHERE id=?", (acc.id,)
        )
        row = await cur.fetchone()
        if row is None:
            await self.db.execute(
                """INSERT INTO accounts
                   (id, phone, password, slots_json, seat_slots_json, bound_seats_json,
                    status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
                (
                    acc.id, acc.phone, acc.password, slots_json,
                    json.dumps(acc.seat_slots or {}),
                    bound_json,
                    now, now,
                ),
            )
        else:
            await self.db.execute(
                """UPDATE accounts SET phone=?, password=?, slots_json=?,
                       seat_slots_json=?, bound_seats_json=?, updated_at=?
                   WHERE id=?""",
                (
                    acc.phone, acc.password, slots_json,
                    json.dumps(acc.seat_slots or {}),
                    bound_json, now, acc.id,
                ),
            )
        await self.db.commit()

    async def get_account(self, acc_id: str) -> Account | None:
        cur = await self.db.execute(
            "SELECT id, phone, password, slots_json, seat_slots_json, bound_seats_json "
            "FROM accounts WHERE id=?",
            (acc_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        slots = row[3]
        if slots not in ("full",):
            try:
                slots = json.loads(slots)
            except Exception:
                pass
        bound = row[5] or "[]"
        try:
            bound_list = json.loads(bound)
        except Exception:
            bound_list = []
        seat_slots_raw = row[4] or "{}"
        try:
            seat_slots = json.loads(seat_slots_raw) or None
        except Exception:
            seat_slots = None
        return Account(
            id=row[0], phone=row[1], password=row[2],
            slots=slots, bound_seats=bound_list,
            seat_slots=seat_slots,
        )

    async def list_accounts(self) -> list[Account]:
        cur = await self.db.execute("SELECT id FROM accounts ORDER BY id")
        rows = await cur.fetchall()
        out: list[Account] = []
        for r in rows:
            a = await self.get_account(r[0])
            if a:
                out.append(a)
        return out

    async def sync_accounts(self, accounts) -> int:
        """Sync accounts to DB. 可接受 AccountConfig (pydantic) 或 Account (dataclass)。
        实际使用 cfg.accounts (AccountConfig 列表),这里转成 Account dataclass。
        """
        from seatbot.models import Account as _Account
        for cfg_acc in accounts:
            # cfg_acc 可能是 pydantic BaseModel 或 dataclass
            raw_slots = cfg_acc.slots
            if isinstance(raw_slots, str):
                slots = raw_slots  # 'full' 原样
            else:
                slots = list(raw_slots) if raw_slots else []
            bound = list(cfg_acc.bound_seats) if cfg_acc.bound_seats else []
            seat_slots = getattr(cfg_acc, 'seat_slots', None)
            acc = _Account(
                id=cfg_acc.id,
                phone=cfg_acc.phone,
                password=cfg_acc.password,
                slots=slots,
                bound_seats=bound,
                seat_slots=seat_slots,
            )
            await self.upsert_account(acc)
        return len(accounts)

    async def delete_account(self, acc_id: str) -> None:
        await self.db.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
        await self.db.commit()

    async def set_bootstrap_day(self, acc_id: str, day: date) -> None:
        await self.db.execute(
            "UPDATE accounts SET bootstrap_day=? WHERE id=?",
            (day.isoformat(), acc_id),
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

    # ---------- target seats ----------
    async def add_target_seat(self, seat_num: str, *, label: str = "") -> None:
        now = int(_time.time() * 1000)
        await self.db.execute(
            """INSERT INTO target_seats (seat_num, label, enabled, created_at, updated_at)
               VALUES (?, ?, 1, ?, ?)
               ON CONFLICT(seat_num) DO UPDATE SET label=excluded.label, updated_at=excluded.updated_at""",
            (seat_num.zfill(3), label, now, now),
        )
        await self.db.commit()

    async def delete_target_seat(self, seat_num: str) -> None:
        await self.db.execute(
            "DELETE FROM target_seats WHERE seat_num=?", (seat_num.zfill(3),)
        )
        await self.db.commit()

    async def list_target_seats(self) -> list[SeatTarget]:
        cur = await self.db.execute(
            "SELECT seat_num, label, enabled, created_at, updated_at "
            "FROM target_seats WHERE enabled=1 ORDER BY seat_num"
        )
        rows = await cur.fetchall()
        return [
            SeatTarget(
                seat_num=r[0], label=r[1], enabled=bool(r[2]),
                created_at=r[3], updated_at=r[4],
            )
            for r in rows
        ]

    # ---------- tasks ----------
    async def add_task(self, t: Task) -> int:
        now = int(_time.time() * 1000)
        cur = await self.db.execute(
            """INSERT INTO tasks
               (account_id, seat_num, day, start_time, end_time, status,
                reserve_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                t.account_id, t.seat_num or "", t.day.isoformat(),
                t.start_time.isoformat(timespec="minutes"),
                t.end_time.isoformat(timespec="minutes"),
                t.status.value, t.reserve_id, now, now,
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
        last_error: str | None = None,   # "" = 显式清空(成功路径); None = 保持不变
    ) -> None:
        now = int(_time.time() * 1000)
        if last_error == "":
            # ★ 成功路径清残留: 任务转 ACTIVE/SIGNED/COMPLETE 时不应再挂历史错误文案
            await self.db.execute(
                """UPDATE tasks
                   SET status=?, reserve_id=COALESCE(?, reserve_id),
                       last_error=NULL, updated_at=?
                   WHERE id=?""",
                (status.value, reserve_id, now, task_id),
            )
        else:
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
            "SELECT id, account_id, seat_num, day, start_time, end_time, status, reserve_id, last_error "
            "FROM tasks WHERE id=?",
            (task_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return _row_to_task(row)

    async def list_tasks(
        self, account_id: str | None = None,
        day: date | None = None,
        seat_num: str | None = None,
    ) -> list[Task]:
        q = (
            "SELECT id, account_id, seat_num, day, start_time, end_time, "
            "status, reserve_id, last_error FROM tasks WHERE 1=1"
        )
        args: list[Any] = []
        if account_id:
            q += " AND account_id=?"
            args.append(account_id)
        if day:
            q += " AND day=?"
            args.append(day.isoformat())
        if seat_num:
            q += " AND seat_num=?"
            args.append(seat_num)
        q += " ORDER BY day, seat_num, start_time"
        cur = await self.db.execute(q, args)
        rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]

    async def find_next_task_after(
        self, day: date, after_time: "time", *, statuses: tuple[str, ...] = ("pending", "ready", "failed")
    ) -> Task | None:
        """Find next task on `day` whose start_time > after_time (跨 account 跨 seat)。

        用于 `_maybe_relay` 在 leave 完当前段之后,选下一段由谁执行。
        """
        placeholders = ",".join("?" for _ in statuses)
        cur = await self.db.execute(
            f"SELECT id, account_id, seat_num, day, start_time, end_time, "
            f"status, reserve_id, last_error FROM tasks "
            f"WHERE day=? AND start_time > ? AND status IN ({placeholders}) "
            f"ORDER BY start_time ASC, account_id ASC LIMIT 1",
            (day.isoformat(),
             after_time.strftime("%H:%M") if hasattr(after_time, "strftime") else str(after_time),
             *statuses),
        )
        row = await cur.fetchone()
        return _row_to_task(row) if row else None

    async def has_active_task_for_account_day_start(
        self, account_id: str, day: date, start_time: time,
        seat_num: str,
        statuses: tuple[str, ...] = ("pending", "ready", "active", "signed", "submitting", "leaving"),
    ) -> bool:
        """E4 防护: 检查同 (account_id, day, seat_num, start_time) 是否已存在 task。

        实测发现后端不拦截'同账号同段重复预约',因此 Scheduler 必须在写库前
        去重,避免一个账号同一天同一座位同一时段被预约两次。
        返回: True (已存在,应跳过) / False (可写)
        """
        placeholders = ",".join("?" for _ in statuses)
        cur = await self.db.execute(
            f"SELECT 1 FROM tasks "
        f"WHERE account_id=? AND day=? AND start_time=? AND seat_num=? AND status IN ({placeholders})",
            (
                account_id, day.isoformat(),
                start_time.strftime("%H:%M"),
                seat_num,
                *statuses,
            ),
        )
        row = await cur.fetchone()
        return row is not None

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
    sh, sm = map(int, row[4].split(":"))
    eh, em = map(int, row[5].split(":"))
    return Task(
        id=row[0],
        account_id=row[1],
        seat_num=row[2] or "",
        day=date.fromisoformat(row[3]),
        start_time=time(sh, sm),
        end_time=time(eh, em),
        status=TaskStatus(row[6]),
        reserve_id=row[7],
        last_error=row[8],
    )
