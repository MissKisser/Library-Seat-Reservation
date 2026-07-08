"""Merge guard accounts' slots into a coverage report for the target seat.

Public entry point: `compute_coverage(accounts, day, open_time, close_time)`
returns a `Coverage` describing which 30-minute cells are guarded by at least
one account, where gaps and overlaps are, and per-account ranges.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterable

from seatbot.models import Account
from seatbot.utils.timeutil import expand_account_slots, parse_hhmm


CELL_MINUTES = 30


@dataclass
class Cell:
    start: time
    end: time
    accounts: list[str] = field(default_factory=list)  # guard account ids

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
        """Uncovered time ranges."""
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
        """Cells covered by 2+ accounts. List of (start, end, [account_ids])."""
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


def _cell_index(t: time, open_time: time) -> int:
    base = datetime.combine(date.today(), open_time)
    cur = datetime.combine(date.today(), t)
    return max(0, int((cur - base).total_seconds() // (CELL_MINUTES * 60)))


def compute_coverage(
    accounts: Iterable[Account],
    day: date,
    open_time: time | str = time(8, 0),
    close_time: time | str = time(22, 0),
) -> Coverage:
    """Build a per-30-min coverage map for `day`.

    `accounts` may include duplicates; accounts with `slots == "full"` expand
    to the full open-close window via `expand_account_slots`.
    """
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

    for acc in accounts:
        try:
            ranges = expand_account_slots(acc.slots, max_hours=24.0)
        except Exception:
            continue  # skip accounts with broken slot config
        for s, e in ranges:
            s_idx = _cell_index(s, open_time)
            e_idx = _cell_index(e, open_time)
            for i in range(s_idx, e_idx):
                if 0 <= i < n_cells and acc.id not in cells[i].accounts:
                    cells[i].accounts.append(acc.id)

    return Coverage(day=day, open_time=open_time, close_time=close_time, cells=cells)