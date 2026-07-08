"""Expand user-facing slot config into ≤ max_hours task chunks."""
from __future__ import annotations

from datetime import date

from seatbot.models import Account, Task, TaskStatus
from seatbot.utils.timeutil import expand_account_slots


class PlannerError(Exception):
    pass


class ReservationPlanner:
    def __init__(self, account: Account, max_reserve_hours: float):
        self.account = account
        self.max_reserve_hours = max_reserve_hours

    def expand_for_day(self, day: date) -> list[Task]:
        try:
            chunks = expand_account_slots(
                self.account.slots, self.max_reserve_hours
            )
        except Exception as e:
            raise PlannerError(
                f"failed to expand slots for {self.account.id}: {e}"
            ) from e
        if not chunks:
            raise PlannerError(
                f"no slots produced for {self.account.id} on {day}"
            )
        return [
            Task(
                id=None,
                account_id=self.account.id,
                day=day,
                start_time=s,
                end_time=e,
                status=TaskStatus.PENDING,
            )
            for s, e in chunks
        ]
