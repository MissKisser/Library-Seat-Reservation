from datetime import date, time

import pytest

from seatbot.models import Account, Task, TaskStatus
from seatbot.planner import ReservationPlanner, PlannerError


def make_account(slots) -> Account:
    return Account(id="a", phone="1", password="p", seat_num="001", slots=slots)


def test_expand_full():
    planner = ReservationPlanner(make_account("full"), max_reserve_hours=2.0)
    tasks = planner.expand_for_day(date(2026, 7, 9))
    assert len(tasks) == 7
    assert tasks[0].start_time == time(8, 0)
    assert tasks[0].end_time == time(10, 0)
    assert tasks[-1].end_time == time(22, 0)
    assert all(t.status == TaskStatus.PENDING for t in tasks)
    assert all(t.account_id == "a" for t in tasks)


def test_expand_list_of_ranges():
    planner = ReservationPlanner(
        make_account(["08:00-12:00", "14:00-18:00"]),
        max_reserve_hours=2.0,
    )
    tasks = planner.expand_for_day(date(2026, 7, 9))
    # 08-10, 10-12, 14-16, 16-18 = 4 tasks
    assert [(t.start_time, t.end_time) for t in tasks] == [
        (time(8, 0), time(10, 0)),
        (time(10, 0), time(12, 0)),
        (time(14, 0), time(16, 0)),
        (time(16, 0), time(18, 0)),
    ]


def test_expand_invalid_slots_raises():
    planner = ReservationPlanner(make_account(123), max_reserve_hours=2.0)
    with pytest.raises(PlannerError):
        planner.expand_for_day(date(2026, 7, 9))


def test_no_overlap_between_chunks():
    planner = ReservationPlanner(
        make_account(["08:00-22:00"]), max_reserve_hours=2.0
    )
    tasks = planner.expand_for_day(date(2026, 7, 9))
    for prev, cur in zip(tasks, tasks[1:]):
        assert cur.start_time >= prev.end_time
