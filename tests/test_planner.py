from datetime import date, time

import pytest

from seatbot.models import Account, TaskStatus
from seatbot.planner import ReservationPlanner, PlannerError


def make_account(slots, **kw) -> Account:
    return Account(id="a", phone="1", password="p", slots=slots, **kw)


def make_planner(account, max_reserve_hours=2.0):
    # v2 构造签名: account, bound_seats, fallback_seats, max_reserve_hours
    return ReservationPlanner(
        account,
        bound_seats=account.bound_seats or ["104"],
        fallback_seats=["104", "105"],
        max_reserve_hours=max_reserve_hours,
    )


def test_expand_full():
    planner = make_planner(make_account("full"))
    tasks = planner.expand_for_day(date(2026, 7, 9))
    assert len(tasks) == 7
    assert tasks[0].start_time == time(8, 0)
    assert tasks[0].end_time == time(10, 0)
    assert tasks[-1].end_time == time(22, 0)
    assert all(t.status == TaskStatus.PENDING for t in tasks)
    assert all(t.account_id == "a" for t in tasks)


def test_expand_list_of_ranges():
    # v0.6: ≤2h 的 range 原样展开 (不再有自动拆段)
    planner = make_planner(make_account(["08:00-10:00", "14:00-16:00"]))
    tasks = planner.expand_for_day(date(2026, 7, 9))
    assert [(t.start_time, t.end_time) for t in tasks] == [
        (time(8, 0), time(10, 0)),
        (time(14, 0), time(16, 0)),
    ]


def test_expand_rejects_overlong_range():
    # ★ AGENTS.md 业务规则: >max_hours 的 range 必须拒绝, 不自动拆段
    planner = make_planner(make_account(["08:00-12:00"]))
    with pytest.raises(PlannerError, match="拆"):
        planner.expand_for_day(date(2026, 7, 9))


def test_expand_invalid_slots_raises():
    planner = make_planner(make_account(123))
    with pytest.raises(PlannerError):
        planner.expand_for_day(date(2026, 7, 9))


def test_no_overlap_between_chunks():
    planner = make_planner(make_account(["08:00-10:00", "10:00-12:00"]))
    tasks = planner.expand_for_day(date(2026, 7, 9))
    for prev, cur in zip(tasks, tasks[1:]):
        assert cur.start_time >= prev.end_time


def test_seat_slots_exact_no_cartesian():
    # ★ v2 守护矩阵核心语义: seat_slots 精确展开, 不做笛卡尔积
    acc = make_account(
        [], seat_slots={"104": ["09:00-11:00"], "105": ["15:00-17:00"]},
        bound_seats=["104", "105"],
    )
    tasks = ReservationPlanner(
        acc, bound_seats=acc.bound_seats, fallback_seats=["104", "105"],
        max_reserve_hours=2.0,
    ).expand_for_day(date(2026, 8, 25))
    assert len(tasks) == 2
    got = {(t.seat_num, t.start_time, t.end_time) for t in tasks}
    assert got == {("104", time(9, 0), time(11, 0)), ("105", time(15, 0), time(17, 0))}


def test_seat_slots_rejects_overlong_range():
    acc = make_account(
        [], seat_slots={"104": ["09:00-11:30"]}, bound_seats=["104"],
    )
    planner = ReservationPlanner(
        acc, bound_seats=acc.bound_seats, fallback_seats=["104"],
        max_reserve_hours=2.0,
    )
    with pytest.raises(PlannerError, match="拆"):
        planner.expand_for_day(date(2026, 8, 25))


def test_seat_slots_weekday_dict_per_day():
    # 星期维度: 按那天的星期键取时段; 缺天 = 不产任务
    acc = make_account(
        [], seat_slots={"104": {"mon": ["09:00-11:00"], "tue": ["15:00-17:00"]}},
        bound_seats=["104"],
    )
    planner = ReservationPlanner(
        acc, bound_seats=acc.bound_seats, fallback_seats=["104"],
        max_reserve_hours=2.0,
    )
    mon = planner.expand_for_day(date(2026, 8, 31))   # 周一
    tue = planner.expand_for_day(date(2026, 9, 1))    # 周二
    sun = planner.expand_for_day(date(2026, 9, 6))    # 周日（缺天）
    assert [(t.start_time, t.end_time) for t in mon] == [(time(9, 0), time(11, 0))]
    assert [(t.start_time, t.end_time) for t in tue] == [(time(15, 0), time(17, 0))]
    assert sun == []


def test_seat_slots_weekday_full_day_value():
    acc = make_account(
        [], seat_slots={"104": {"sun": "full"}}, bound_seats=["104"],
    )
    planner = ReservationPlanner(
        acc, bound_seats=acc.bound_seats, fallback_seats=["104"],
        max_reserve_hours=2.0,
    )
    tasks = planner.expand_for_day(date(2026, 9, 6))  # 周日
    assert len(tasks) == 7
    assert tasks[0].start_time == time(8, 0)


def test_seat_slots_list_form_still_uniform_all_days():
    acc = make_account([], seat_slots={"104": ["09:00-11:00"]}, bound_seats=["104"])
    planner = ReservationPlanner(
        acc, bound_seats=acc.bound_seats, fallback_seats=["104"],
        max_reserve_hours=2.0,
    )
    assert len(planner.expand_for_day(date(2026, 8, 31))) == 1
    assert len(planner.expand_for_day(date(2026, 9, 6))) == 1


def test_seat_slots_weekday_overlong_rejected_per_day():
    acc = make_account(
        [], seat_slots={"104": {"wed": ["09:00-12:00"]}}, bound_seats=["104"],
    )
    planner = ReservationPlanner(
        acc, bound_seats=acc.bound_seats, fallback_seats=["104"],
        max_reserve_hours=2.0,
    )
    with pytest.raises(PlannerError, match="拆"):
        planner.expand_for_day(date(2026, 9, 2))  # 周三
