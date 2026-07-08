"""Domain dataclasses used across the codebase."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum
from typing import Any


# ---------- account / slot ----------

@dataclass
class Account:
    id: str
    phone: str
    password: str
    seat_num: str
    slots: str | list[str]  # SlotSpec

    def display_name(self) -> str:
        return f"{self.id} (seat {self.seat_num})"


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
    status: TaskStatus = TaskStatus.PENDING
    reserve_id: int | None = None
    last_error: str | None = None

    def chunk_key(self) -> str:
        return f"{self.account_id}|{self.day.isoformat()}|{self.start_time.isoformat(timespec='minutes')}"


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
