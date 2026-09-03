"""Expand user-facing slot config into ≤ max_hours task chunks (v2: per-seat).

模式:
  - `seat_slots` 矩阵模式（推荐, Web 绑定页/账号表单维护）: 每个 (座位, 时段)
    精确展开成一条 Task, 不做笛卡尔积。
  - `slots` + `bound_seats` 扁平模式（遗留）: 每个绑定座位各展开一遍 slots。
  - 账号既无 seat_slots 也无 bound_seats = 未参与守护, 展开结果为空
    （不回退到全部目标座位, 避免笛卡尔积意外放大）。
"""
from __future__ import annotations

from datetime import date, datetime

from seatbot.models import Account, Task, TaskStatus
from seatbot.utils.timeutil import expand_account_slots, parse_range
from seatbot.utils.weekly import slots_for_weekday, weekday_key

class PlannerError(Exception):
    pass


def _reject_overlong_ranges(slots: list[str], max_hours: float, account_id: str) -> None:
    """★ v0.6: 单个 range 超过 max_hours 时直接拒绝, 不再自动拆段。

    业务规则 (AGENTS.md 2026-08-24): 自动拆段 (如 19:00-21:30 → 2 段) 会破坏
    "每账号每座位精确 1 段" 的守护矩阵; 超长 range 属配置错误, 应显式暴露。
    """
    for r in slots:
        s, e = parse_range(r)
        dur_h = (
            datetime.combine(date.today(), e) - datetime.combine(date.today(), s)
        ).total_seconds() / 3600
        if dur_h > max_hours:
            raise PlannerError(
                f"account {account_id}: slot {r} 时长 {dur_h}h 超过单段上限 "
                f"{max_hours}h — 自动拆段已禁用, 请在配置层拆成多段"
            )


class ReservationPlanner:
    def __init__(
        self,
        account: Account,
        bound_seats: list[str],           # 该账号绑定的目标座位
        fallback_seats: list[str],        # 当 bound_seats 空时用的 fallback (library 的 target_seats)
        max_reserve_hours: float,
        open_time: str = "08:00",         # 馆舍营业窗口，"full" 展开的边界
        close_time: str = "22:00",
    ):
        self.account = account
        self.bound_seats = list(bound_seats)
        self.fallback_seats = list(fallback_seats)
        self.max_reserve_hours = max_reserve_hours
        self.open_time = open_time
        self.close_time = close_time

    def expand_for_day(self, day: date) -> list[Task]:
        """展开成 (account, seat, slot) → Task list.

        矩阵模式:
          account=guard_a, seat_slots={'104': ['09:00-11:00'], '105': ['15:00-17:00']}
          → 2 Task: (guard_a, 104, 09:00-11:00) + (guard_a, 105, 15:00-17:00)

        扁平模式（遗留）:
          account=guard_c, bound_seats=['084','085'], slots=['08:00-10:00']
          → 2 Task: (guard_c, 084, 08:00-10:00) + (guard_c, 085, 08:00-10:00)

        未绑定（seat_slots 与 bound_seats 均为空）→ 空列表。
        """
        out: list[Task] = []

        # 模式 1: per-seat slots 矩阵（按那天的星期键取时段）
        if self.account.seat_slots:
            wd = weekday_key(day)
            for seat_num, spec in self.account.seat_slots.items():
                day_spec = slots_for_weekday(spec, wd)
                if day_spec == "full":
                    # per-seat "full" 展开成馆舍营业时段内的 ≤max_hours 段
                    from seatbot.utils.timeutil import expand_full_day
                    chunks = expand_full_day(
                        self.open_time, self.close_time,
                        max_hours=self.max_reserve_hours,
                    )
                elif isinstance(day_spec, list):
                    if not day_spec:
                        # 该天未配置时段 → 该座位当天不产任务
                        continue
                    _reject_overlong_ranges(
                        day_spec, self.max_reserve_hours, self.account.id)
                    try:
                        chunks = expand_account_slots(
                            day_spec, self.max_reserve_hours)
                    except Exception as e:
                        raise PlannerError(
                            f"failed to expand seat_slots for "
                            f"{self.account.id} seat={seat_num} day={wd}: {e}"
                        ) from e
                else:
                    raise PlannerError(
                        f"seat_slots[{seat_num}][{wd}] must be 'full' or "
                        f"list[str], got {type(day_spec).__name__}"
                    )
                if not chunks:
                    raise PlannerError(
                        f"no chunks produced for {self.account.id} seat={seat_num}"
                    )
                for s, e in chunks:
                    out.append(Task(
                        id=None,
                        account_id=self.account.id,
                        day=day,
                        start_time=s,
                        end_time=e,
                        seat_num=seat_num,
                        status=TaskStatus.PENDING,
                    ))
            return out
        # 模式 2: 传统笛卡尔积 (slots × bound_seats); 未绑定账号不参与守护
        if not self.bound_seats:
            return []
        seats = self.bound_seats
        if isinstance(self.account.slots, list):
            _reject_overlong_ranges(self.account.slots, self.max_reserve_hours, self.account.id)
        try:
            chunks = expand_account_slots(
                self.account.slots, self.max_reserve_hours,
                open_time=self.open_time, close_time=self.close_time,
            )
        except Exception as e:
            raise PlannerError(
                f"failed to expand slots for {self.account.id}: {e}"
            ) from e
        if not chunks:
            raise PlannerError(
                f"no slots produced for {self.account.id} on {day}"
            )
        for seat in seats:
            for s, e in chunks:
                out.append(Task(
                    id=None,
                    account_id=self.account.id,
                    day=day,
                    start_time=s,
                    end_time=e,
                    seat_num=seat,
                    status=TaskStatus.PENDING,
                ))
        return out
