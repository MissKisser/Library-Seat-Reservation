"""Coverage reports.

v2 additions: per-seat coverage alongside the legacy per-account coverage.

Public entry points:
  - `compute_coverage(accounts, day, open, close)` — legacy: rows = 30-min cells,
    `accounts` list per cell. Used by Dashboard-by-account view.
  - `compute_seat_coverage(accounts, target_seats, day, open, close)` — new:
    rows = target_seats; each row has 30-min cells with the primary account_id
    (lowest-priority binding) on that seat at that half-hour.
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
    base = datetime.combine(date.today(), open_time)
    cur = datetime.combine(date.today(), t)
    return max(0, int((cur - base).total_seconds() // (CELL_MINUTES * 60)))


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


def compute_coverage(
    accounts: Iterable[Account],
    day: date,
    open_time: time | str = time(8, 0),
    close_time: time | str = time(22, 0),
) -> Coverage:
    """Legacy per-account coverage (rows = 30-min cells across the open-close window)."""
    cov = _make_blank_coverage(day, open_time, close_time)
    open_time = cov.open_time
    for acc in accounts:
        try:
            ranges = expand_account_slots(acc.slots, max_hours=24.0)
        except Exception:
            continue
        for s, e in ranges:
            s_idx = _cell_index(s, open_time)
            e_idx = _cell_index(e, open_time)
            for i in range(s_idx, e_idx):
                if 0 <= i < len(cov.cells) and acc.id not in cov.cells[i].accounts:
                    cov.cells[i].accounts.append(acc.id)
    return cov


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

    For each target seat, compute which 30-min cells are covered by a guard
    account bound to that seat (or by a guard account with empty bindings —
    such accounts are treated as wildcards and cover all seats).

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

    for seat in seats:
        cov = _make_blank_coverage(day, open_time, close_time)
        seat_num = seat.seat_num
        # 把该座位上 bindings 排序后的账号列表(空 bindings 的视为 wildcard,后置)
        bound_accs = [a for a in accounts if seat_num in a.bound_seats]
        wild_accs = [a for a in accounts if not a.bound_seats]
        ordered = bound_accs + wild_accs
        for acc in ordered:
            try:
                ranges = expand_account_slots(acc.slots, max_hours=24.0)
            except Exception:
                continue
            for s, e in ranges:
                s_idx = _cell_index(s, cov.open_time)
                e_idx = _cell_index(e, cov.open_time)
                for i in range(s_idx, e_idx):
                    if 0 <= i < len(cov.cells) and acc.id not in cov.cells[i].accounts:
                        cov.cells[i].accounts.append(acc.id)
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
