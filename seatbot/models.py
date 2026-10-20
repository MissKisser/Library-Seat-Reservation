"""领域数据类：目标座位、守护账号、任务与任务状态。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from enum import Enum

# ---------- 目标座位 ----------

@dataclass
class SeatTarget:
    """目标座位：用户希望在其期望时段内始终保持「已预约」的座位。

    入参:
        seat_num: 三位补零座位号，例如 '001'。
        label: 可选昵称，例如 '靠窗主座'。
        enabled: 是否启用；停用后不参与覆盖检查与任务展开。
        created_at: 创建时刻，epoch 毫秒。
        updated_at: 最近一次变更时刻，epoch 毫秒。
        desired_slots: 期望时段；None 表示未设置（不检查缺口），
            list 表示全周统一，dict[星期键, list] 表示按天。
        desired_slots_weekly: 按天的期望时段，与 desired_slots 互不覆盖，
            读取时按 schedule_mode 选列。
    """
    seat_num: str            # 三位补零座位号, e.g. '001'
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


# ---------- 守护账号 ----------

@dataclass
class Account:
    """守护账号：程序用其手机号与密码登录超星，替用户抢占目标座位。

    入参:
        id: 账号标识（上游实名，同名不同人追加序号）。
        phone: 登录手机号。
        password: 登录密码。
        slots: 扁平时段配置；'full' 或 ['HH:MM-HH:MM', ...]。
            与 seat_slots 同时存在时以 seat_slots 为准。
        bound_seats: 扁平模式下绑定的目标座位号列表。
        seat_slots: 每座位时段矩阵，形如
            {座位号: {星期键: 时段列表}}；None 表示回退扁平模式。
        status: 生命周期状态，active=守护中、inactive=禁用中（可逆）、
            disabled=已删除（不可逆，全视图不可见）。
    """
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


# ---------- 任务生命周期 ----------

class TaskStatus(str, Enum):  # noqa: UP042 — 迁移到 StrEnum 会改变 str()/format() 的输出
    """任务状态机：pending → submitting → active → signed → leaving → complete。"""
    PENDING = "pending"          # 等待执行
    READY = "ready"              # 已到时间, 准备执行 (web quick-reserve 创建)
    SUBMITTING = "submitting"    # 正在 submit
    ACTIVE = "active"            # submit 成功 (已预约, 尚未签到)
    SIGNED = "signed"            # 已签到, 等待到点 leave (幂等防重签)
    LEAVING = "leaving"          # 正在 leave
    COMPLETE = "complete"        # 已签退/取消, 时段结束
    FAILED = "failed"            # 出错


# ---------- 任务来源 ----------

# 排程生成的任务（默认；14:00 批量、当日补约、启动补跑均属此列）。
TASK_SOURCE_MATRIX = "matrix"
# /tasks 导入页面写入的本地任务（用户在 App 手动抢到的预约号手动登记）。
TASK_SOURCE_IMPORT = "import"
# 自动托管采纳后新建的任务（无矩阵/导入历史同键记录时新增）。
TASK_SOURCE_ADOPT = "adopt"
TASK_SOURCE_ADOPT_MATRIX = "adopt_matrix"
# /manual 手动预约页面写入的任务（用户从面板发起，系统账号走既有 submit 通道）。
TASK_SOURCE_MANUAL = "manual"


@dataclass
class Task:
    """单条预约任务：一个账号在一个座位上的一段时段。

    入参:
        id: 库内自增主键；未落库时为 None。
        account_id: 承担该任务的账号 id。
        day: 预约日期。
        start_time: 时段开始时刻。
        end_time: 时段结束时刻。
        seat_num: 该任务预订的目标座位号。
        status: 当前状态，取值见 TaskStatus。
        reserve_id: 超星返回的预约号。
        last_error: 最近一次失败或说明信息。
        created_at: 创建时刻，epoch 毫秒。
        updated_at: 最近一次状态变更时刻，epoch 毫秒。
        source: 任务来源，取值见 TASK_SOURCE_* 常量。
    """
    id: int | None
    account_id: str
    day: date
    start_time: time
    end_time: time
    seat_num: str = ""                    # 该任务预订的目标座位
    status: TaskStatus = TaskStatus.PENDING
    reserve_id: int | None = None
    last_error: str | None = None
    created_at: int = 0                   # epoch 毫秒，仅作展示
    updated_at: int = 0                   # epoch 毫秒，最近一次状态变更
    # 任务来源（matrix / import / adopt / adopt_matrix / manual）。
    # matrix = 排程生成；import = /tasks 导入；adopt = 托管新建；
    # adopt_matrix = 托管接附矩阵任务；manual = /manual 手动预约。
    source: str = TASK_SOURCE_MATRIX

    def chunk_key(self) -> str:
        """返回该任务的唯一业务键：账号|座位|日期|开始时刻。"""
        return (
            f"{self.account_id}|{self.seat_num}|{self.day.isoformat()}"
            f"|{self.start_time.isoformat(timespec='minutes')}"
        )
