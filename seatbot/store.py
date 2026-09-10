"""Async SQLite state store (v2: target_seats + bindings + per-task seat_num)."""
from __future__ import annotations

import json
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

import aiosqlite

from seatbot.models import Account, SeatTarget, Task, TaskStatus
from seatbot.utils.timeutil import CST, epoch_ms_to_cst_str
from seatbot.utils.weekly import WEEKDAY_KEYS, normalize_weekly
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
  updated_at  INTEGER NOT NULL,
  desired_slots_json TEXT,
  desired_slots_weekly_json TEXT
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
  source      TEXT NOT NULL DEFAULT 'matrix',
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
-- 注:idx_tasks_seat_day 在迁移里强制创建 (依赖新增的 seat_num 列)
CREATE TABLE IF NOT EXISTS hosted_reservations (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id   TEXT NOT NULL,
  seat_num     TEXT NOT NULL,
  day          TEXT NOT NULL,
  start_time   TEXT NOT NULL,
  end_time     TEXT NOT NULL,
  reserve_id   INTEGER NOT NULL,
  state        TEXT NOT NULL,
  outcome      TEXT NOT NULL DEFAULT '',
  task_id      INTEGER,
  created_at   INTEGER NOT NULL,
  updated_at   INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_hosted_account_reserve
  ON hosted_reservations(account_id, reserve_id);
CREATE INDEX IF NOT EXISTS idx_hosted_state ON hosted_reservations(state);

CREATE TABLE IF NOT EXISTS logs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          INTEGER NOT NULL,
  level       TEXT NOT NULL,
  account_id  TEXT,
  message     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts DESC);

-- 通知: 批量缺口等需要用户看到的事件 (看板 banner + 可选 webhook)
CREATE TABLE IF NOT EXISTS notifications (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          INTEGER NOT NULL,
  level       TEXT NOT NULL,
  title       TEXT NOT NULL,
  body        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_notifications_ts ON notifications(ts DESC);

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

CREATE TABLE IF NOT EXISTS account_cookies (
  account_id   TEXT PRIMARY KEY,
  cookies_json TEXT NOT NULL,
  updated_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_reserved_seat_day ON user_reserved(seat_num, day);

-- 系统设置表（已保存的设置会持久保留）
CREATE TABLE IF NOT EXISTS app_settings (
  key         TEXT PRIMARY KEY,
  value       TEXT NOT NULL,
  updated_at  INTEGER NOT NULL
);

-- 实况核对快照（按 (day, seat) 一行的滚动历史）
CREATE TABLE IF NOT EXISTS reconcile_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    day TEXT NOT NULL,
    seat_num TEXT NOT NULL,
    ok INTEGER NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
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
        # 覆盖图数据版本号: 任何影响覆盖视图的写操作 +1, 前端据此按需拉取
        self._data_version = 0

    @property
    def data_version(self) -> int:
        return self._data_version

    def _bump(self) -> None:
        self._data_version += 1

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        await self._migrate_v1_to_v2()
        await self._migrate_task_identity_guard()

    async def _migrate_task_identity_guard(self) -> None:
        """活跃态任务的 (账号,日,座位,开始) 唯一索引：应用层 E4 检查之外的库级防线。

        仅约束未终态行（complete/failed 不受限，历史可重开同键任务）；
        存量活跃态重复行会阻止创建，此时记 WARN 交人工处理（不自动删数据）。
        """
        cur = await self.db.execute(
            "SELECT 1 FROM tasks "
            "WHERE status IN ('pending','ready','submitting','active','signed','leaving') "
            "GROUP BY account_id, day, seat_num, start_time HAVING COUNT(*) > 1 LIMIT 1"
        )
        if await cur.fetchone():
            await self.log_message(
                "WARN", None,
                "tasks 存在同(账号,日,座位,开始)的活跃重复行，唯一索引未创建，需人工核对",
            )
            await self.db.commit()
            return
        try:
            await self.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_active_identity "
                "ON tasks(account_id, day, seat_num, start_time) "
                "WHERE status IN ('pending','ready','submitting','active','signed','leaving')"
            )
            await self.db.commit()
        except Exception as e:
            await self.db.rollback()
            await self.log_message("WARN", None, f"任务唯一索引创建失败: {e}")
            await self.db.commit()

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
        # 2b. tasks: 增加 source（v2 手动预约自动托管字段）。
        # 任务来源：matrix / import / adopt / adopt_matrix；缺列则补默认 'matrix'。
        cur = await self.db.execute("PRAGMA table_info(tasks)")
        task_cols = {row[1] for row in await cur.fetchall()}
        if "source" not in task_cols:
            await self.db.execute(
                "ALTER TABLE tasks ADD COLUMN source TEXT NOT NULL DEFAULT 'matrix'"
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

        # 4. 索引补建 (post-migration,依赖新增的 seat_num / source 列)
        try:
            await self.db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_seat_day ON tasks(seat_num, day)"
            )
            await self.db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_source ON tasks(source)"
            )
        except Exception:
            pass
        # 5. target_seats: 增加期望时段列
        cur = await self.db.execute("PRAGMA table_info(target_seats)")
        seat_cols = {row[1] for row in await cur.fetchall()}
        if "desired_slots_json" not in seat_cols:
            await self.db.execute(
                "ALTER TABLE target_seats ADD COLUMN desired_slots_json TEXT"
            )
        if "desired_slots_weekly_json" not in seat_cols:
            await self.db.execute(
                "ALTER TABLE target_seats ADD COLUMN desired_slots_weekly_json TEXT"
            )
            # 独立双配置迁移：存量 desired_slots_json 若为按天 dict 且 7 天不全相同（周级），则搬至 weekly 列
            cur2 = await self.db.execute(
                "SELECT seat_num, desired_slots_json FROM target_seats WHERE desired_slots_json IS NOT NULL")
            for sid, raw in await cur2.fetchall():
                try:
                    val = json.loads(raw)
                except Exception:
                    continue
                if isinstance(val, dict) and any(k in WEEKDAY_KEYS for k in val.keys()):
                    vals = [json.dumps(val.get(wd, []), sort_keys=True) for wd in WEEKDAY_KEYS]
                    is_uniform = len(set(vals)) == 1
                    if not is_uniform:
                        await self.db.execute(
                            "UPDATE target_seats SET desired_slots_weekly_json=?, desired_slots_json=NULL WHERE seat_num=?",
                            (json.dumps(val), sid),
                        )
        # 6. 星期维度形态归一: seat_slots / desired_slots 的 list 值展开为 7 键 dict（幂等）
        cur = await self.db.execute("SELECT id, seat_slots_json FROM accounts")
        for rid, raw in await cur.fetchall():
            try:
                data = json.loads(raw or "{}") or {}
            except Exception:
                continue
            if not isinstance(data, dict) or not data:
                continue
            new = {}
            dirty = False
            for seat, val in data.items():
                norm = normalize_weekly(val, allow_full=True)
                new[seat] = norm
                if norm != val:
                    dirty = True
            if dirty:
                await self.db.execute(
                    "UPDATE accounts SET seat_slots_json=? WHERE id=?",
                    (json.dumps(new), rid),
                )
        cur = await self.db.execute(
            "SELECT seat_num, desired_slots_json FROM target_seats "
            "WHERE desired_slots_json IS NOT NULL")
        for sid, raw in await cur.fetchall():
            try:
                val = json.loads(raw)
            except Exception:
                continue
            norm = normalize_weekly(val, allow_full=False)
            if norm is not None and norm != val:
                await self.db.execute(
                    "UPDATE target_seats SET desired_slots_json=? WHERE seat_num=?",
                    (json.dumps(norm), sid),
                )
        # 6b. weekly 列的归一（同样幂等）
        cur = await self.db.execute(
            "SELECT seat_num, desired_slots_weekly_json FROM target_seats "
            "WHERE desired_slots_weekly_json IS NOT NULL")
        for sid, raw in await cur.fetchall():
            try:
                val = json.loads(raw)
            except Exception:
                continue
            norm = normalize_weekly(val, allow_full=False)
            if norm is not None and norm != val:
                await self.db.execute(
                    "UPDATE target_seats SET desired_slots_weekly_json=? WHERE seat_num=?",
                    (json.dumps(norm), sid),
                )
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

    # ---------- app_settings（系统设置，页面已保存的为准） ----------
    async def get_settings_map(self) -> dict[str, str]:
        cur = await self.db.execute("SELECT key, value FROM app_settings")
        rows = await cur.fetchall()
        return {r[0]: r[1] for r in rows}

    async def get_setting(self, key: str) -> str | None:
        cur = await self.db.execute("SELECT value FROM app_settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None

    async def set_settings(self, patch: dict[str, str]) -> None:
        """批量 upsert 键值；空 patch 不做事。"""
        if not patch:
            return
        self._bump()
        now = int(_time.time() * 1000)
        for k, v in patch.items():
            await self.db.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (k, str(v), now),
            )
        await self.db.commit()

    async def delete_setting(self, key: str) -> bool:
        cur = await self.db.execute("DELETE FROM app_settings WHERE key=?", (key,))
        await self.db.commit()
        return cur.rowcount > 0

    async def clear_settings(self) -> int:
        cur = await self.db.execute("DELETE FROM app_settings")
        await self.db.commit()
        return cur.rowcount

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
        self._bump()
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
        self._bump()
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


    # ---------- hosted_reservations (手动预约自动托管表) ----------
    async def upsert_hosted(
        self,
        account_id: str,
        reserve_id: int,
        *,
        seat_num: str,
        day: date,
        start: time,
        end: time,
        state: str,
        outcome: str = "",
        task_id: int | None = None,
    ) -> int:
        """按 (account_id, reserve_id) 命中则 UPDATE (新 state/outcome/task_id + updated_at)，否则 INSERT。

        返回行 id；调用方负责 state 合法性（queued/hosting/pending_decision/stopped/ended）。
        """
        self._bump()
        now = int(_time.time() * 1000)
        sn = seat_num.zfill(3) if seat_num.isdigit() else seat_num
        ds = day.isoformat()
        ss = start.strftime("%H:%M")
        es = end.strftime("%H:%M")
        cur = await self.db.execute(
            "SELECT id FROM hosted_reservations WHERE account_id=? AND reserve_id=?",
            (account_id, reserve_id),
        )
        row = await cur.fetchone()
        if row is None:
            cur = await self.db.execute(
                """INSERT INTO hosted_reservations
                   (account_id, seat_num, day, start_time, end_time,
                    reserve_id, state, outcome, task_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (account_id, sn, ds, ss, es,
                 reserve_id, state, outcome, task_id, now, now),
            )
            await self.db.commit()
            return cur.lastrowid or 0
        await self.db.execute(
            """UPDATE hosted_reservations SET state=?, outcome=?, task_id=?,
                   updated_at=? WHERE id=?""",
            (state, outcome, task_id, now, row[0]),
        )
        await self.db.commit()
        return row[0]

    async def get_hosted(self, hosted_id: int) -> dict | None:
        """按 hosted_reservations.id 取一行（含全部列）；不存在返回 None。"""
        cur = await self.db.execute(
            "SELECT id, account_id, seat_num, day, start_time, end_time, "
            "reserve_id, state, outcome, task_id, created_at, updated_at "
            "FROM hosted_reservations WHERE id=?",
            (hosted_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "account_id": row[1], "seat_num": row[2],
            "day": row[3], "start_time": row[4], "end_time": row[5],
            "reserve_id": row[6], "state": row[7], "outcome": row[8],
            "task_id": row[9], "created_at": row[10], "updated_at": row[11],
        }

    async def list_hosted(
        self,
        states: list[str] | None = None,
        *,
        limit: int | None = 50,
        offset: int = 0,
        include_final: bool = True,
    ) -> list[dict]:
        """列出托管行；states 给定时只返回命中状态集合的行，否则默认含 stopped/ended。

        排序：updated_at 倒序；分页用 limit/offset；limit=None 时不设行数上限。
        """
        q = ("SELECT id, account_id, seat_num, day, start_time, end_time, "
             "reserve_id, state, outcome, task_id, created_at, updated_at "
             "FROM hosted_reservations WHERE 1=1")
        args: list[Any] = []
        if states is not None:
            if not states:
                return []
            placeholders = ",".join("?" for _ in states)
            q += f" AND state IN ({placeholders})"
            args.extend(states)
        elif not include_final:
            q += " AND state IN ('queued', 'hosting', 'pending_decision')"
        if limit is None:
            q += " ORDER BY updated_at DESC, id DESC LIMIT -1 OFFSET ?"
            args.append(offset)
        else:
            q += " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
            args.extend([limit, offset])
        cur = await self.db.execute(q, args)
        rows = await cur.fetchall()
        return [
            {
                "id": r[0], "account_id": r[1], "seat_num": r[2],
                "day": r[3], "start_time": r[4], "end_time": r[5],
                "reserve_id": r[6], "state": r[7], "outcome": r[8],
                "task_id": r[9], "created_at": r[10], "updated_at": r[11],
            }
            for r in rows
        ]

    async def count_hosted(self, states: list[str] | None = None) -> int:
        """统计托管行总数，states 给定时只统计指定状态集合。"""
        q = "SELECT COUNT(*) FROM hosted_reservations WHERE 1=1"
        args: list[Any] = []
        if states is not None:
            if not states:
                return 0
            placeholders = ",".join("?" for _ in states)
            q += f" AND state IN ({placeholders})"
            args.extend(states)
        cur = await self.db.execute(q, args)
        row = await cur.fetchone()
        return int(row[0] or 0) if row else 0

    async def update_hosted(
        self,
        hosted_id: int,
        *,
        state: str | None = None,
        outcome: str | None = None,
        task_id: int | None = None,
    ) -> None:
        """更新托管行：state/outcome/task_id 任一非 None 即改，否则保持；同步 updated_at。"""
        self._bump()
        sets: list[str] = []
        args: list[Any] = []
        if state is not None:
            sets.append("state=?")
            args.append(state)
        if outcome is not None:
            sets.append("outcome=?")
            args.append(outcome)
        if task_id is not None:
            sets.append("task_id=?")
            args.append(task_id)
        if not sets:
            return
        sets.append("updated_at=?")
        args.append(int(_time.time() * 1000))
        args.append(hosted_id)
        await self.db.execute(
            f"UPDATE hosted_reservations SET {', '.join(sets)} WHERE id=?",
            args,
        )
        await self.db.commit()

    async def find_hosted_by_reserve(
        self, account_id: str, reserve_id: int,
    ) -> dict | None:
        """按 (account_id, reserve_id) 查托管行；不存在返回 None。"""
        cur = await self.db.execute(
            "SELECT id, account_id, seat_num, day, start_time, end_time, "
            "reserve_id, state, outcome, task_id, created_at, updated_at "
            "FROM hosted_reservations WHERE account_id=? AND reserve_id=?",
            (account_id, reserve_id),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "account_id": row[1], "seat_num": row[2],
            "day": row[3], "start_time": row[4], "end_time": row[5],
            "reserve_id": row[6], "state": row[7], "outcome": row[8],
            "task_id": row[9], "created_at": row[10], "updated_at": row[11],
        }

    # ---------- accounts ----------

    async def upsert_account(self, acc: Account) -> None:
        self._bump()
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
                    json.dumps({
                        seat: (normalize_weekly(val, allow_full=True) or {})
                        for seat, val in (acc.seat_slots or {}).items()
                    } if acc.seat_slots else {}),
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
                    json.dumps({
                        seat: (normalize_weekly(val, allow_full=True) or {})
                        for seat, val in (acc.seat_slots or {}).items()
                    } if acc.seat_slots else {}),
                    bound_json, now, acc.id,
                ),
            )
        await self.db.commit()

    async def get_account(self, acc_id: str) -> Account | None:
        cur = await self.db.execute(
            "SELECT id, phone, password, slots_json, seat_slots_json, bound_seats_json, status "
            "FROM accounts WHERE id=? AND status!='disabled'",
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
            status=row[6] or "active",
        )

    async def list_accounts(self, *, include_inactive: bool = False) -> list[Account]:
        """列出账号，双口径：默认仅守护中(active)；include_inactive=True 追加禁用中(inactive)。

        disabled 为已删除墓碑，任何视图不可见。调度/分配调用点用默认口径即可
        排除禁用账号；账号管理页用 include_inactive=True。
        """
        cur = await self.db.execute(
            "SELECT id, status FROM accounts WHERE status!='disabled' ORDER BY id"
        )
        rows = await cur.fetchall()
        out: list[Account] = []
        for r in rows:
            if not include_inactive and r[1] != "active":
                continue
            a = await self.get_account(r[0])
            if a:
                out.append(a)
        return out

    async def set_account_status(self, acc_id: str, status: str) -> None:
        """设置账号生命周期状态（'active' | 'inactive' | 'disabled'），联动版本探针。"""
        if status not in ("active", "inactive", "disabled"):
            raise ValueError(f"invalid account status: {status!r}")
        self._bump()
        await self.db.execute(
            "UPDATE accounts SET status=?, updated_at=? WHERE id=?",
            (status, int(_time.time() * 1000), acc_id),
        )
        await self.db.commit()

    async def sync_accounts(self, accounts) -> int:
        """启动时同步账号，已在页面管理的账号以页面为准。

        - 新账号：整行插入（含 slots/seat_slots/bound_seats）；
        - 已有账号：仅刷新凭据 phone/password，不覆盖座位绑定；
        - 已删除（status='disabled'）的账号：跳过，不再恢复。

        Returns: 实际处理的账号数。
        """
        from seatbot.models import Account as _Account
        n = 0
        for cfg_acc in accounts:
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
            cur = await self.db.execute(
                "SELECT status FROM accounts WHERE id=?", (acc.id,)
            )
            row = await cur.fetchone()
            if row is None:
                await self.upsert_account(acc)
            elif row[0] == "disabled":
                continue
            else:
                await self.db.execute(
                    "UPDATE accounts SET phone=?, password=?, updated_at=? WHERE id=?",
                    (acc.phone, acc.password,
                     int(_time.time() * 1000), acc.id),
                )
                await self.db.commit()
            n += 1
        return n

    async def delete_account(self, acc_id: str) -> None:
        """软删除账号，已删除的账号不会因配置文件再次出现。"""
        self._bump()
        await self.db.execute(
            "UPDATE accounts SET status='disabled', updated_at=? WHERE id=?",
            (int(_time.time() * 1000), acc_id),
        )
        await self.db.execute("DELETE FROM account_cookies WHERE account_id=?", (acc_id,))
        await self.db.commit()

    # ---------- 登录会话 cookie 持久化 ----------
    async def save_account_cookies(self, account_id: str, cookies: dict[str, str]) -> None:
        """保存账号的登录会话 cookie（JSON），供重启后免浏览器登录恢复会话。"""
        await self.db.execute(
            "INSERT INTO account_cookies (account_id, cookies_json, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET cookies_json=excluded.cookies_json, "
            "updated_at=excluded.updated_at",
            (account_id, json.dumps(cookies), int(_time.time() * 1000)),
        )
        await self.db.commit()

    async def load_account_cookies(self, account_id: str) -> dict[str, str] | None:
        """读取账号持久化的登录 cookie；不存在或损坏时返回 None。"""
        cur = await self.db.execute(
            "SELECT cookies_json FROM account_cookies WHERE account_id=?", (account_id,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        try:
            data = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None
        return data if isinstance(data, dict) and data else None

    async def clear_account_cookies(self, account_id: str) -> None:
        """清除账号持久化的登录 cookie（会话已确认失效时调用）。"""
        await self.db.execute("DELETE FROM account_cookies WHERE account_id=?", (account_id,))
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
    async def add_target_seat(
        self, seat_num: str, *, label: str = "",
        desired_slots: list[str] | dict[str, list[str]] | None = None,
        desired_slots_weekly: dict[str, list[str]] | None = None,
    ) -> None:
        """注册目标座位；对已软删除的座位重新启用（Web/CLI 入口）。"""
        self._bump()
        now = int(_time.time() * 1000)
        # weekly 列需显式传入时才写，否则沿用 add 时的 desired_slots 语义（兼容旧调）
        if desired_slots_weekly is None and isinstance(desired_slots, dict) and any(k in desired_slots for k in ("mon","tue","wed","thu","fri","sat","sun")):
            # 调用方若把 weekly dict 误传到 desired_slots，自动纠偏（防御）
            desired_slots_weekly = desired_slots  # type: ignore
            desired_slots = None
        uni_json = (lambda n: json.dumps(n) if n is not None else None)(normalize_weekly(desired_slots, allow_full=False) if desired_slots is not None else None)
        wk_json = (lambda n: json.dumps(n) if n is not None else None)(normalize_weekly(desired_slots_weekly, allow_full=False) if desired_slots_weekly is not None else None)
        await self.db.execute(
            """INSERT INTO target_seats
                   (seat_num, label, enabled, created_at, updated_at, desired_slots_json, desired_slots_weekly_json)
               VALUES (?, ?, 1, ?, ?, ?, ?)
               ON CONFLICT(seat_num) DO UPDATE SET
                   label=excluded.label, enabled=1, updated_at=excluded.updated_at,
                   desired_slots_json=COALESCE(excluded.desired_slots_json, target_seats.desired_slots_json),
                   desired_slots_weekly_json=COALESCE(excluded.desired_slots_weekly_json, target_seats.desired_slots_weekly_json)""",
            (seat_num.zfill(3), label, now, now, uni_json, wk_json),
        )
        await self.db.commit()
    async def seed_target_seat(
        self, seat_num: str, *, label: str = "",
        desired_slots: list[str] | None = None,
    ) -> None:
        """按配置文件初始化目标座位：仅插入不存在的，已删除的不再恢复。"""
        now = int(_time.time() * 1000)
        await self.db.execute(
            """INSERT INTO target_seats
                   (seat_num, label, enabled, created_at, updated_at, desired_slots_json)
               VALUES (?, ?, 1, ?, ?, ?)
               ON CONFLICT(seat_num) DO NOTHING""",
            (seat_num.zfill(3), label, now, now,
             (lambda n: json.dumps(n) if n is not None else None)(
                 normalize_weekly(desired_slots, allow_full=False))),
        )
        await self.db.commit()

    async def set_target_seat_desired(
        self, seat_num: str, slots: list[str] | dict[str, list[str]] | None,
        *, is_weekly: bool | None = None,
    ) -> bool:
        """更新座位期望时段；is_weekly 指明写哪列（True=按天 weekly，False=全周统一 uniform）；
        为 None 时按 slots 形态自动判定（dict 含周键且 7 天不全同→weekly，其余→uniform）；
        slots=None 时按 is_weekly 清对应列，为 None 时清两列（清空后该座位不再检查缺口）。"""
        self._bump()
        if slots is None:
            if is_weekly is None:
                cur = await self.db.execute(
                    "UPDATE target_seats SET desired_slots_json=NULL, desired_slots_weekly_json=NULL, updated_at=? "
                    "WHERE seat_num=? AND enabled=1",
                    (int(_time.time() * 1000), seat_num.zfill(3)),
                )
            else:
                col = "desired_slots_weekly_json" if is_weekly else "desired_slots_json"
                cur = await self.db.execute(
                    f"UPDATE target_seats SET {col}=NULL, updated_at=? WHERE seat_num=? AND enabled=1",
                    (int(_time.time() * 1000), seat_num.zfill(3)),
                )
            await self.db.commit()
            return cur.rowcount > 0
        # 非空：按 is_weekly 或自动判定列
        if is_weekly is None:
            # 自动：dict 且含周键且 7 天不全同 → weekly，否则 uniform
            if isinstance(slots, dict) and any(k in WEEKDAY_KEYS for k in slots.keys()):
                # 归一后判断是否全同
                try:
                    norm_tmp = normalize_weekly(slots, allow_full=False)
                    vals = [json.dumps(norm_tmp.get(wd, []), sort_keys=True) for wd in WEEKDAY_KEYS] if isinstance(norm_tmp, dict) else []
                    is_weekly = len(set(vals)) != 1
                except Exception:
                    is_weekly = True
            else:
                is_weekly = False
        norm = normalize_weekly(slots, allow_full=False)
        col = "desired_slots_weekly_json" if is_weekly else "desired_slots_json"
        cur = await self.db.execute(
            f"UPDATE target_seats SET {col}=?, updated_at=? "
            "WHERE seat_num=? AND enabled=1",
            (json.dumps(norm) if norm is not None else None,
             int(_time.time() * 1000), seat_num.zfill(3)),
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def delete_target_seat(self, seat_num: str) -> None:
        """软删除目标座位，已删除的座位不会因配置文件再次出现。"""
        self._bump()
        await self.db.execute(
            "UPDATE target_seats SET enabled=0, updated_at=? WHERE seat_num=?",
            (int(_time.time() * 1000), seat_num.zfill(3)),
        )
        await self.db.commit()
    async def list_target_seats(self) -> list[SeatTarget]:
        try:
            cur = await self.db.execute(
                "SELECT seat_num, label, enabled, created_at, updated_at, "
                "desired_slots_json, desired_slots_weekly_json FROM target_seats WHERE enabled=1 ORDER BY seat_num"
            )
            rows = await cur.fetchall()
        except Exception as e:
            if "no such column" in str(e).lower():
                cur = await self.db.execute(
                    "SELECT seat_num, label, enabled, created_at, updated_at, "
                    "desired_slots_json FROM target_seats WHERE enabled=1 ORDER BY seat_num"
                )
                rows = await cur.fetchall()
                out: list[SeatTarget] = []
                for r in rows:
                    try:
                        desired = json.loads(r[5]) if r[5] else None
                    except Exception:
                        desired = None
                    out.append(SeatTarget(
                        seat_num=r[0], label=r[1], enabled=bool(r[2]),
                        created_at=r[3], updated_at=r[4],
                        desired_slots=desired,
                        desired_slots_weekly=None,
                    ))
                return out
            raise
        out: list[SeatTarget] = []
        for r in rows:
            try:
                desired = json.loads(r[5]) if r[5] else None
            except Exception:
                desired = None
            try:
                desired_weekly = json.loads(r[6]) if len(r) > 6 and r[6] else None
            except Exception:
                desired_weekly = None
            out.append(SeatTarget(
                seat_num=r[0], label=r[1], enabled=bool(r[2]),
                created_at=r[3], updated_at=r[4],
                desired_slots=desired,
                desired_slots_weekly=desired_weekly,
            ))
        return out
    # ---------- tasks ----------
    async def max_task_updated_at(self) -> int:
        """tasks.updated_at 的最大值 (毫秒, 空表返回 0)。

        版本探针的库侧分量: 绕过本进程的带外写入 (如人工补约脚本
        直改数据库) 也会推高该值, 使前端版本探测能感知并刷新。
        """
        async with self.db.execute(
            "SELECT COALESCE(MAX(updated_at), 0) FROM tasks"
        ) as cur:
            row = await cur.fetchone()
        return int(row[0] or 0) if row else 0

    async def add_task(self, t: Task) -> int:
        self._bump()
        now = int(_time.time() * 1000)
        cur = await self.db.execute(
            """INSERT INTO tasks
               (account_id, seat_num, day, start_time, end_time, status,
                reserve_id, source, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                t.account_id, t.seat_num or "", t.day.isoformat(),
                t.start_time.isoformat(timespec="minutes"),
                t.end_time.isoformat(timespec="minutes"),
                t.status.value, t.reserve_id,
                t.source or "matrix",
                now, now,
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
        source: str | None = None,        # None = 保持不变（COALESCE 语义）
    ) -> None:
        self._bump()
        now = int(_time.time() * 1000)
        if last_error == "":
            # ★ 成功路径清残留: 任务转 ACTIVE/SIGNED/COMPLETE 时不应再挂历史错误文案
            await self.db.execute(
                """UPDATE tasks
                   SET status=?, reserve_id=COALESCE(?, reserve_id),
                       last_error=NULL, source=COALESCE(?, source),
                       updated_at=?
                   WHERE id=?""",
                (status.value, reserve_id, source, now, task_id),
            )
        else:
            await self.db.execute(
                """UPDATE tasks
                   SET status=?, reserve_id=COALESCE(?, reserve_id),
                       last_error=COALESCE(?, last_error),
                       source=COALESCE(?, source),
                       updated_at=?
                   WHERE id=?""",
                (status.value, reserve_id, last_error, source, now, task_id),
            )

    async def shorten_task_end(self, task_id: int, new_end: "time") -> None:
        """缩短任务结束时间（换座释放：时段开始签到后短持即签退）。"""
        self._bump()
        await self.db.execute(
            "UPDATE tasks SET end_time=?, updated_at=? WHERE id=?",
            (new_end.isoformat(timespec="minutes"),
             int(_time.time() * 1000), task_id),
        )
        await self.db.commit()

    async def update_task_account(self, task_id: int, account_id: str) -> None:
        """改绑任务归属账号（换账号重试时与矩阵迁移保持一致）。"""
        self._bump()
        await self.db.execute(
            "UPDATE tasks SET account_id=?, updated_at=? WHERE id=?",
            (account_id, int(_time.time() * 1000), task_id),
        )
        await self.db.commit()

    async def get_task(self, task_id: int) -> Task | None:
        cur = await self.db.execute(

            "SELECT id, account_id, seat_num, day, start_time, end_time, "
            "status, reserve_id, last_error, source "
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
            "status, reserve_id, last_error, source, created_at, updated_at "
            "FROM tasks WHERE 1=1"
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
        day: str | date | None = None,
    ) -> list[LogRow]:
        q = "SELECT id, ts, level, account_id, message FROM logs WHERE 1=1"
        args: list[Any] = []
        if day:
            try:
                d = date.fromisoformat(day) if isinstance(day, str) else day
                start_dt = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=CST)
                end_dt = start_dt + timedelta(days=1)
                start_ms = int(start_dt.timestamp() * 1000)
                end_ms = int(end_dt.timestamp() * 1000)
                q += " AND ts >= ? AND ts < ?"
                args.extend([start_ms, end_ms])
            except Exception:
                pass
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

    async def list_log_days(self, limit: int = 30) -> list[str]:
        """获取最近产生过日志的日期列表 (CST 时区 YYYY-MM-DD)。"""
        cur = await self.db.execute(
            "SELECT DISTINCT date(datetime(ts/1000, 'unixepoch', '+8 hours')) as d "
            "FROM logs ORDER BY d DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [r[0] for r in rows if r[0]]

    async def add_notification(self, title: str, body: str = "", level: str = "warn") -> None:
        """落一条用户需要看到的通知（看板 banner 展示，可选 webhook 外推）。

        表容量封顶 30 条: 连败类告警每 10 分钟就落一条, 不封顶会积压,
        忽略最新几条后更旧的同类告警又会顶进轮播, 表现为"忽略不生效"。
        """
        await self.db.execute(
            "INSERT INTO notifications (ts, level, title, body) VALUES (?, ?, ?, ?)",
            (int(_time.time() * 1000), level, title, body),
        )
        await self.db.execute(
            "DELETE FROM notifications WHERE id NOT IN ("
            "SELECT id FROM notifications ORDER BY ts DESC, id DESC LIMIT 30)"
        )
        await self.db.commit()

    async def purge_old_logs(self, *, keep_days: int = 30) -> int:
        """删除 keep_days 天前的 actions/logs 行并返回删除总数（启动时调用控制库膨胀）。"""
        cutoff = int((_time.time() - keep_days * 86400) * 1000)
        cur = await self.db.execute("DELETE FROM actions WHERE ts < ?", (cutoff,))
        n = cur.rowcount or 0
        cur = await self.db.execute("DELETE FROM logs WHERE ts < ?", (cutoff,))
        n += cur.rowcount or 0
        await self.db.commit()
        return n

    async def list_notifications(self, limit: int = 5) -> list[dict]:
        cur = await self.db.execute(
            "SELECT id, ts, level, title, body FROM notifications ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "ts": r[1],
                "time_str": epoch_ms_to_cst_str(r[1]),
                "level": r[2],
                "title": r[3],
                "body": r[4],
            }
            for r in rows
        ]

    async def dismiss_notification(self, nid: int) -> bool:
        """删除一条通知; 返回该 id 是否确实存在并已删除。"""
        cur = await self.db.execute("DELETE FROM notifications WHERE id = ?", (nid,))
        await self.db.commit()
        return cur.rowcount > 0

    async def dismiss_all_notifications(self) -> int:
        """清空全部通知; 返回删除条数。"""
        cur = await self.db.execute("DELETE FROM notifications")
        await self.db.commit()
        return cur.rowcount

    async def save_reconcile_result(
        self, day: date, seat_num: str, ok: bool, detail: str,
    ) -> None:
        """落一条实况核对快照；表封顶 96 行（默认间隔下约 48h 历史）。"""
        self._bump()
        await self.db.execute(
            "INSERT INTO reconcile_results (ts, day, seat_num, ok, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (int(_time.time() * 1000), day.isoformat(), seat_num,
             1 if ok else 0, detail),
        )
        await self.db.execute(
            "DELETE FROM reconcile_results WHERE id NOT IN ("
            "SELECT id FROM reconcile_results ORDER BY id DESC LIMIT 96)"
        )
        await self.db.commit()

    async def list_reconcile_results(self, limit: int = 48) -> list[dict]:
        """按时间倒序取核对快照。"""
        cur = await self.db.execute(
            "SELECT ts, day, seat_num, ok, detail FROM reconcile_results "
            "ORDER BY id DESC LIMIT ?", (limit,),
        )
        rows = await cur.fetchall()
        return [
            {"ts": r[0], "day": r[1], "seat_num": r[2],
             "ok": bool(r[3]), "detail": r[4]}
            for r in rows
        ]

    async def cookie_recency(self) -> dict[str, int]:
        """各账号最近一次会话落库时间（account_id → updated_at），供动态选号。"""
        cur = await self.db.execute(
            "SELECT account_id, updated_at FROM account_cookies")
        return {r[0]: r[1] for r in await cur.fetchall()}


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
        source=row[9] if len(row) > 9 and row[9] else "matrix",
        created_at=row[10] if len(row) > 10 else 0,
        updated_at=row[11] if len(row) > 11 else 0,
    )


def backup_database(db_path: str, backup_dir: str, keep: int = 7):
    """SQLite 在线备份 (sqlite3 backup API, 服务运行中调用安全)。

    Args:
        db_path: 源数据库路径。
        backup_dir: 备份目录 (自动创建)。
        keep: 保留最近多少份备份, 超出删除最旧。

    Returns:
        本次写入的备份文件 Path; 当日已有备份 (跳过) 或源库不存在时返回 None。
    """
    import sqlite3
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    src = Path(db_path)
    if not src.exists():
        return None
    dest_dir = Path(backup_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone(timedelta(hours=8)))
    dest = dest_dir / f"seatbot-{now:%Y%m%d}.db"
    if dest.exists():
        return None
    tmp = dest.with_suffix(".db.tmp")
    src_con = sqlite3.connect(str(src), timeout=30)
    try:
        dst_con = sqlite3.connect(str(tmp))
        try:
            src_con.backup(dst_con)
        finally:
            dst_con.close()
    finally:
        src_con.close()
    tmp.replace(dest)
    backups = sorted(dest_dir.glob("seatbot-*.db"))
    for old in backups[:-keep] if len(backups) > keep else []:
        try:
            old.unlink()
        except OSError:
            pass
    return dest
