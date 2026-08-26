"""Expand user-facing slot config into ≤ max_hours task chunks (v2: per-seat).

v2:
  - Each task carries its own `seat_num` (不再共享 library.target_seat_num)。
  - 一个 account 拥有 `bound_seats` 列表, 调度时该账号会在每个绑定的座位上
    各 expand 一遍 slots。
  - 如果 `bound_seats` 为空 (wildcard 账号),调度时使用 `fallback_seats`
    (library 的 target_seats 表)逐一展开。
  - 单账号每天段数上限由 `one_account_max_concurrent_segments_per_day` 控制,
    超出时 round-robin 分配到其他可用的账号 (如有);无人接管的段落会
    PlannerError 抛出 (UI 必须提示用户增加账号)。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time

from seatbot.models import Account, Task, TaskStatus
from seatbot.utils.timeutil import expand_account_slots, parse_range


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
    ):
        self.account = account
        self.bound_seats = list(bound_seats)
        self.fallback_seats = list(fallback_seats)
        self.max_reserve_hours = max_reserve_hours

    def expand_for_day(self, day: date) -> list[Task]:
        """展开成 (account, seat, slot) → Task list.

        例子 (单 slots + 多座 — 笛卡尔积, v2 默认):
          account=guard_a, bound_seats=['084'], slots=['08:00-10:00','15:00-17:00']
          → 2 Task: (guard_a, 084, 08:00-10:00) + (guard_a, 084, 15:00-17:00)

          account=guard_c, bound_seats=['084','085'], slots=['08:00-10:00']
          → 2 Task: (guard_c, 084, 08:00-10:00) + (guard_c, 085, 08:00-10:00)

          account=guard_wild, bound_seats=[], fallback_seats=['084','085'], slots=['08:00-10:00']
          → 2 Task: (guard_wild, 084, 08:00-10:00) + (guard_wild, 085, 08:00-10:00)

        例子 (per-seat slots, 2026-08-24 新增):
          account=guard_a, seat_slots={'104': ['09:00-11:00'], '105': ['15:00-17:00']}
          → 2 Task: (guard_a, 104, 09:00-11:00) + (guard_a, 105, 15:00-17:00)
          (跳过笛卡尔积,只用 seat_slots 字典里指定的 (seat, slot) 组合)
        """
        out: list[Task] = []

        # 模式 1: per-seat slots (2026-08-24 新增)
        # 如果 account.seat_slots 非空,只生成 seat_slots 里指定的 (seat, slot) 组合
        if self.account.seat_slots:
            for seat_num, slots in self.account.seat_slots.items():
                if slots == "full":
                    # per-seat "full" 展开成当日所有 2h 段
                    from seatbot.utils.timeutil import expand_full_day
                    chunks = expand_full_day(
 "08:00", "22:00", max_hours=self.max_reserve_hours
                    )
                elif isinstance(slots, list):
                    _reject_overlong_ranges(slots, self.max_reserve_hours, self.account.id)
                    try:
                        chunks = expand_account_slots(slots, self.max_reserve_hours)
                    except Exception as e:
                        raise PlannerError(
                            f"failed to expand seat_slots for {self.account.id} seat={seat_num}: {e}"
                        ) from e
                else:
                    raise PlannerError(
                        f"seat_slots[{seat_num}] must be 'full' or list[str], got {type(slots).__name__}"
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

        # 模式 2: 传统笛卡尔积 (slots × bound_seats)
        seats = self.bound_seats or self.fallback_seats
        if not seats:
            raise PlannerError(
                f"account {self.account.id} has no bound seats and no fallback seats"
            )
        if isinstance(self.account.slots, list):
            _reject_overlong_ranges(self.account.slots, self.max_reserve_hours, self.account.id)
        try:
            chunks = expand_account_slots(self.account.slots, self.max_reserve_hours)
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


# ====================================================================
# 跨账号编排:把一天的所有段 (seat × slot) 分配给账号并强制 N=1 上限
# ====================================================================

def plan_day_for_all_accounts(
    accounts: list[Account],
    target_seats: list[str],
    day: date,
    max_reserve_hours: float,
) -> list[Task]:
    """对每个 account 在每个目标座位上预 expand 一遍 slots, 然后按
    `one_account_max_concurrent_segments_per_day` 严格约束做 round-robin 排序:

      - 默认 1 段/账号/天:每账号每天只允许 1 个 task;溢出的 task 由
        后续账号接管 (轮班)
      - ≥2 段/账号/天:允许多段,但每个 task 仍要校验 distinct seat(可选)
    """
    raw: list[tuple[str, Account, list[tuple[time, time]], list[str]]] = []
    # raw item: (account_id, account, chunks, bound_seats_for_this_account)
    for acc in accounts:
        bound = acc.bound_seats or list(target_seats)
        if not bound:
            continue
        try:
            chunks = expand_account_slots(acc.slots, max_reserve_hours)
        except Exception:
            continue
        raw.append((acc.id, acc, chunks, bound))

    if not raw:
        return []

    # 按 (seat, start_time) 排序所有 (account, seat, slot) 候选,然后做 round-robin
    candidates: list[tuple[time, time, str, str]] = []
    for acc_id, acc, chunks, bound in raw:
        for seat in bound:
            for s, e in chunks:
                candidates.append((s, e, seat, acc_id))
    candidates.sort(key=lambda x: (x[2], x[0], x[3]))   # seat, start, account

    # 每天每个账号最多 N 段
    per_acc: dict[str, int] = defaultdict(int)
    chosen: list[tuple[time, time, str, str]] = []
    for cand in candidates:
        s, e, seat, acc_id = cand
        limit = next(
            a.one_account_max_concurrent_segments_per_day for a in accounts if a.id == acc_id
        )
        # 该段是否已被占 (同 seat, 同时段已有别的账号)? pick first only
        already = any(c[2] == seat and not (c[1] <= s or c[0] >= e) for c in chosen)
        if already:
            continue
        if per_acc[acc_id] >= limit:
            continue
        chosen.append(cand)
        per_acc[acc_id] += 1

    # 写入 Task 列表
    out: list[Task] = []
    for s, e, seat, acc_id in chosen:
        out.append(Task(
            id=None,
            account_id=acc_id,
            day=day,
            start_time=s,
            end_time=e,
            seat_num=seat,
            status=TaskStatus.PENDING,
        ))
    return out
