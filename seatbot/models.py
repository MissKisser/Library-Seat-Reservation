"""Domain dataclasses used across the codebase (v2: multi-seat + per-task seat_num)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from enum import Enum
from typing import Any


# ---------- target seats (was library.target_seat_num in v1) ----------

@dataclass
class SeatTarget:
    """A seat the user wants to keep occupied during targeted time slots."""
    seat_num: str            # '084'
    label: str = ""          # 可选昵称, e.g. '靠窗主座'
    enabled: bool = True
    created_at: int = 0
    updated_at: int = 0


# ---------- account / slot ----------

@dataclass
class Account:
    id: str
    phone: str
    password: str
    slots: str | list[str]                            # SlotSpec
    bound_seats: list[str] = field(default_factory=list)   # ★ 新增: 绑定的目标座位
    # ★ 新增: 单账号每天最多同时持有的预约段数 (悲观=1, 乐观=3+)
    one_account_max_concurrent_segments_per_day: int = 1
    # ★ v0.5+: per-seat slots (None 表示回退笛卡尔积模式)
    seat_slots: dict[str, list[str]] | None = None

    def display_name(self) -> str:
        return f"{self.id}"


# ---------- seat binding (account ↔ seat) ----------

@dataclass
class AccountSeatBinding:
    account_id: str
    seat_num: str
    priority: int = 0
    created_at: int = 0


# ---------- task lifecycle ----------

class TaskStatus(str, Enum):
    PENDING = "pending"          # 等待执行
    READY = "ready"              # 已到时间, 准备执行
    SUBMITTING = "submitting"    # 正在 submit
    ACTIVE = "active"            # 已预约 + 已签到
    LEAVING = "leaving"          # 正在 leave
    COMPLETE = "complete"        # 已签退, 时段结束
    FAILED = "failed"            # 出错


@dataclass
class Task:
    id: int | None
    account_id: str
    day: date
    start_time: time
    end_time: time
    seat_num: str = ""                    # ★ 新增: 该任务预订的目标座位
    status: TaskStatus = TaskStatus.PENDING
    reserve_id: int | None = None
    last_error: str | None = None

    def chunk_key(self) -> str:
        return (
            f"{self.account_id}|{self.seat_num}|{self.day.isoformat()}"
            f"|{self.start_time.isoformat(timespec='minutes')}"
        )


# ---------- API result wrappers ----------

@dataclass
class ReserveResult:
    success: bool
    reserve_id: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class SignResult:
    success: bool
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class LeaveResult:
    success: bool
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class CancelResult:
    success: bool
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class RoomConfig:
    room_id: int
    room_name: str
    open_time: time
    close_time: time
    max_reserve_hours: float
    pre_sign_duration_min: int
    sign_duration_min: int
    raw: dict[str, Any] = field(default_factory=dict)


# ---------- user_reserved (用户硬预约) ----------

@dataclass
class UserReserved:
    id: int | None
    account_id: str
    seat_num: str
    day: date
    start_time: time
    end_time: time
    note: str = ""
    created_at: int = 0
