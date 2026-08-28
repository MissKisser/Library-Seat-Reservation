"""Coverage reports.

Public entry point:
  - `compute_seat_coverage(accounts, target_seats, day, open, close)` —
    rows = target_seats; each row has 30-min cells with the guard account ids
    covering that seat at that half-hour. seat_slots 账号只按其精确矩阵涂格,
    扁平 slots 账号按 bound_seats / wildcard 涂格 (与 planner 同一优先级规则)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterable

from seatbot.models import Account, SeatTarget
from seatbot.utils.timeutil import expand_account_slots, parse_hhmm


CELL_MINUTES = 30


@dataclass
class Cell:
    start: time
    end: time
    accounts: list[str] = field(default_factory=list)  # guard account ids
    user_reserved: bool = False          # 用户手动登记的本人预约
    others_occupied: bool = False        # 来自 Chaoxing /getusedtimes 的他人预约

    @property
    def covered(self) -> bool:
        return bool(self.accounts)


@dataclass
class Coverage:
    day: date
    open_time: time
    close_time: time
    cells: list[Cell]

    @property
    def gaps(self) -> list[tuple[time, time]]:
        out: list[tuple[time, time]] = []
        cur: time | None = None
        for c in self.cells:
            if c.covered:
                if cur is not None:
                    out.append((cur, c.start))
                    cur = None
            else:
                if cur is None:
                    cur = c.start
        if cur is not None:
            out.append((cur, self.cells[-1].end))
        return out

    @property
    def overlaps(self) -> list[tuple[time, time, list[str]]]:
        out: list[tuple[time, time, list[str]]] = []
        run_start: time | None = None
        run_accs: set[str] = set()
        for c in self.cells:
            if len(c.accounts) >= 2:
                if run_start is None:
                    run_start = c.start
                run_accs.update(c.accounts)
            else:
                if run_start is not None:
                    out.append((run_start, c.start, sorted(run_accs)))
                    run_start = None
                    run_accs = set()
        if run_start is not None:
            out.append((run_start, self.cells[-1].end, sorted(run_accs)))
        return out

    @property
    def total_cells(self) -> int:
        return len(self.cells)

    @property
    def covered_cells(self) -> int:
        return sum(1 for c in self.cells if c.covered)


# ---------- v2 新增: per-seat coverage ----------

@dataclass
class SeatCoverage:
    seat: SeatTarget
    coverage: Coverage   # 复用上面的数据结构, Cell.accounts 里存该座位上 [primary, ...] 账号列表

    @property
    def gaps(self) -> list[tuple[time, time]]:
        return self.coverage.gaps


def _cell_index(t: time, open_time: time) -> int:
    base_min = open_time.hour * 60 + open_time.minute
    cur_min = t.hour * 60 + t.minute
    return max(0, (cur_min - base_min) // CELL_MINUTES)


def _make_blank_coverage(day: date, open_time: time | str, close_time: time | str) -> Coverage:
    if isinstance(open_time, str):
        open_time = parse_hhmm(open_time)
    if isinstance(close_time, str):
        close_time = parse_hhmm(close_time)
    total_min = int(
        (datetime.combine(day, close_time) - datetime.combine(day, open_time)).total_seconds() // 60
    )
    if total_min <= 0:
        raise ValueError("close_time must be after open_time")
    if total_min % CELL_MINUTES != 0:
        raise ValueError(
            f"open-close window must be a multiple of {CELL_MINUTES} minutes"
        )
    n_cells = total_min // CELL_MINUTES
    cells = [
        Cell(
            start=(datetime.combine(day, open_time) + timedelta(minutes=i * CELL_MINUTES)).time(),
            end=(datetime.combine(day, open_time) + timedelta(minutes=(i + 1) * CELL_MINUTES)).time(),
        )
        for i in range(n_cells)
    ]
    return Coverage(day=day, open_time=open_time, close_time=close_time, cells=cells)


def _paint_ranges(cov: Coverage, ranges: list[tuple[time, time]], acc_id: str) -> None:
    """把 (start, end) 区间涂到 cov.cells 上 (同账号同格不重复)。"""
    for s, e in ranges:
        s_idx = _cell_index(s, cov.open_time)
        e_idx = _cell_index(e, cov.open_time)
        for i in range(s_idx, e_idx):
            if 0 <= i < len(cov.cells) and acc_id not in cov.cells[i].accounts:
                cov.cells[i].accounts.append(acc_id)


def compute_seat_coverage(
    accounts: Iterable[Account],
    target_seats: Iterable[SeatTarget],
    day: date,
    open_time: time | str = time(8, 0),
    close_time: time | str = time(22, 0),
    user_reserved: list[tuple[str, time, time]] | None = None,
    others_occupied: list[tuple[str, time, time]] | None = None,
) -> list[SeatCoverage]:
    """Per-seat coverage for v2 multi-seat mode.

    账号分两种模式 (与 planner 同一优先级规则):

    - seat_slots 模式 (推荐, AGENTS.md 2026-08-24): 账号只覆盖
      seat_slots 里明确指定的 (seat, slot) 组合; bound_seats 被忽略。
    - 扁平 slots 模式 (兜底): 绑定该座位的账号 (bound_seats) 或
      无绑定 wildcard 账号, 用其 slots 覆盖。

    `user_reserved`: 可选的 [(seat_num, start_time, end_time), ...] 列表,
    cell.accounts 之外另存于 cell.user_reserved (供 gantt 渲染蓝色)。

    `others_occupied`: 可选的 [(seat_num, start_time, end_time), ...] 列表,
    标记到 cell.others_occupied (供 gantt 渲染灰色 — 来自 Chaoxing
    /getusedtimes 的他人预约)。和 user_reserved 互不覆盖,可以叠加。
    """
    accounts = list(accounts)
    seats = list(target_seats)
    out: list[SeatCoverage] = []

    # 把 user_reserved 索引成 {seat_num: [start, end list]}
    ur_by_seat: dict[str, list[tuple[time, time]]] = {}
    for sn, s, e in (user_reserved or []):
        ur_by_seat.setdefault(sn, []).append((s, e))

    # 把 others_occupied 也索引成 {seat_num: [start, end list]}
    oo_by_seat: dict[str, list[tuple[time, time]]] = {}
    for sn, s, e in (others_occupied or []):
        oo_by_seat.setdefault(sn, []).append((s, e))

    # 按模式分组: seat_slots 账号走精确矩阵, 其余走 bound/wildcard 扁平逻辑
    ss_accs = [a for a in accounts if a.seat_slots]
    flat_accs = [a for a in accounts if not a.seat_slots]

    for seat in seats:
        cov = _make_blank_coverage(day, open_time, close_time)
        seat_num = seat.seat_num

        # 模式 1: seat_slots 精确矩阵 — 只涂 seat_slots[seat_num] 指定的时段
        for acc in ss_accs:
            spec = acc.seat_slots.get(seat_num)
            if not spec:
                continue
            try:
                ranges = expand_account_slots(spec, max_hours=24.0)
            except Exception:
                continue
            _paint_ranges(cov, ranges, acc.id)

        # 模式 2: 扁平 slots — 绑定该座位的账号优先, 空绑定的 wildcard 后置
        bound_accs = [a for a in flat_accs if seat_num in a.bound_seats]
        wild_accs = [a for a in flat_accs if not a.bound_seats]
        for acc in bound_accs + wild_accs:
            try:
                ranges = expand_account_slots(acc.slots, max_hours=24.0)
            except Exception:
                continue
            _paint_ranges(cov, ranges, acc.id)
        # 叠加 user_reserved → 标记哪些 cell 是用户亲述已预约
        for s, e in ur_by_seat.get(seat_num, []):
            s_idx = _cell_index(s, cov.open_time)
            e_idx = _cell_index(e, cov.open_time)
            for i in range(s_idx, e_idx):
                if 0 <= i < len(cov.cells):
                    cov.cells[i].user_reserved = True
        # 叠加 others_occupied → 来自 Chaoxing /getusedtimes 的他人预约
        for s, e in oo_by_seat.get(seat_num, []):
            s_idx = _cell_index(s, cov.open_time)
            e_idx = _cell_index(e, cov.open_time)
            for i in range(s_idx, e_idx):
                if 0 <= i < len(cov.cells):
                    cov.cells[i].others_occupied = True
        out.append(SeatCoverage(seat=seat, coverage=cov))
    return out
