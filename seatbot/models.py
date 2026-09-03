"""Domain dataclasses used across the codebase (v2: multi-seat + per-task seat_num)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from enum import Enum


# ---------- target seats (was library.target_seat_num in v1) ----------

@dataclass
class SeatTarget:
    """A seat the user wants to keep occupied during targeted time slots."""
    seat_num: str            # '084'
    label: str = ""          # 可选昵称, e.g. '靠窗主座'
    enabled: bool = True
    created_at: int = 0
    updated_at: int = 0
    # 该座位期望被守护的时段；None = 未设置（不检查）；
    # list = 全周统一；dict[星期键, list] = 按天
    # 独立双配置：desired_slots 为全周统一（uniform），desired_slots_weekly 为按天（weekly），
    # 互不覆盖；读取时按 schedule_mode 选列
    desired_slots: list[str] | dict[str, list[str]] | None = None
    desired_slots_weekly: dict[str, list[str]] | None = None
# ---------- account / slot ----------

@dataclass
class Account:
    id: str
    phone: str
    password: str
    slots: str | list[str]                            # SlotSpec
    bound_seats: list[str] = field(default_factory=list)  # 绑定的目标座位
    # per-seat slots；None = 回退笛卡尔积模式；
    # 值为 dict[星期键, list[str] | "full"]（启动迁移后统一此形态）
    seat_slots: dict[str, dict[str, list[str] | str]] | None = None
    # 生命周期：active=守护中 / inactive=禁用中（可逆，在途任务继续履约） /
    # disabled=已删除墓碑（不可逆，全视图不可见）
    status: str = "active"


# ---------- task lifecycle ----------

class TaskStatus(str, Enum):
    PENDING = "pending"          # 等待执行
    READY = "ready"              # 已到时间, 准备执行 (web quick-reserve 创建)
    SUBMITTING = "submitting"    # 正在 submit
    ACTIVE = "active"            # submit 成功 (已预约, 尚未签到)
    SIGNED = "signed"            # ★ 已签到, 等待到点 leave (幂等防重签)
    LEAVING = "leaving"          # 正在 leave
    COMPLETE = "complete"        # 已签退/取消, 时段结束
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
    created_at: int = 0                   # epoch 秒，仅作展示
    updated_at: int = 0                   # epoch 秒，最近一次状态变更

    def chunk_key(self) -> str:
        return (
            f"{self.account_id}|{self.seat_num}|{self.day.isoformat()}"
            f"|{self.start_time.isoformat(timespec='minutes')}"
        )


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
