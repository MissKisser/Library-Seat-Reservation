"""Tests for compute_seat_coverage (v2 per-seat coverage)."""
from datetime import date, time

from seatbot.coverage import compute_seat_coverage
from seatbot.models import Account, SeatTarget


DAY = date(2026, 8, 26)


def _acc(id_: str, slots, bound_seats=None):
    return Account(
        id=id_, phone="13800000000", password="x",
        slots=slots, bound_seats=bound_seats or [],
    )


def _seat(num: str) -> SeatTarget:
    return SeatTarget(seat_num=num)


def _cells(row):
    return {(c.start.strftime("%H:%M"), c.end.strftime("%H:%M")): c for c in row.coverage.cells}


def test_blank_day_yields_28_half_hour_cells():
    rows = compute_seat_coverage([_acc("a", ["09:00-10:00"])], [_seat("104")], DAY)
    assert len(rows) == 1
    assert len(rows[0].coverage.cells) == 28   # 08:00-22:00 = 14h = 28 格


def test_bound_account_covers_only_its_seat():
    rows = compute_seat_coverage(
        [_acc("a", ["09:00-11:00"], bound_seats=["104"])],
        [_seat("104"), _seat("105")], DAY,
    )
    by_seat = {r.seat.seat_num: r for r in rows}
    c104 = _cells(by_seat["104"])
    c105 = _cells(by_seat["105"])
    assert c104[("09:00", "09:30")].accounts == ["a"]
    assert c104[("10:30", "11:00")].accounts == ["a"]
    assert c104[("11:00", "11:30")].accounts == []
    assert c105[("09:00", "09:30")].accounts == []


def test_wildcard_account_covers_all_target_seats():
    rows = compute_seat_coverage(
        [_acc("w", ["15:00-16:00"])],          # bound_seats=[] → wildcard
        [_seat("104"), _seat("105")], DAY,
    )
    for r in rows:
        c = _cells(r)
        assert c[("15:00", "15:30")].accounts == ["w"]


def test_gap_property_reports_uncovered_span():
    rows = compute_seat_coverage(
        [_acc("a", ["09:00-10:00"], bound_seats=["104"])],
        [_seat("104")], DAY,
    )
    gaps = rows[0].gaps
    # 缺口应包含 10:00 起到 22:00 的整段
    assert any(g[0] == time(10, 0) and g[1] == time(22, 0) for g in gaps)


def test_user_reserved_and_others_occupied_overlay():
    rows = compute_seat_coverage(
        [_acc("a", ["09:00-10:00"], bound_seats=["104"])],
        [_seat("104")], DAY,
        user_reserved=[("104", time(12, 0), time(13, 0))],
        others_occupied=[("104", time(18, 0), time(19, 0))],
    )
    c = _cells(rows[0])
    assert c[("12:30", "13:00")].user_reserved is True
    assert c[("12:30", "13:00")].accounts == []          # overlay 不算守护
    assert c[("18:30", "19:00")].others_occupied is True
    assert c[("09:30", "10:00")].user_reserved is False


def test_broken_slots_account_is_skipped():
    rows = compute_seat_coverage(
        [_acc("bad", ["not-a-range"], bound_seats=["104"]),
         _acc("good", ["09:00-10:00"], bound_seats=["104"])],
        [_seat("104")], DAY,
    )
    c = _cells(rows[0])
    assert c[("09:00", "09:30")].accounts == ["good"]


def test_seat_slots_account_covers_only_specified_pairs():
    """seat_slots 账号只覆盖矩阵里明确指定的 (seat, slot) 组合, bound_seats 被忽略。"""
    rows = compute_seat_coverage(
        [Account(id="a", phone="13800000000", password="x",
                 slots=[], bound_seats=["104", "105"],   # bound 应被忽略
                 seat_slots={"104": ["09:00-11:00"], "105": ["15:00-17:00"]})],
        [_seat("104"), _seat("105")], DAY,
    )
    by_seat = {r.seat.seat_num: r for r in rows}
    c104, c105 = _cells(by_seat["104"]), _cells(by_seat["105"])
    assert c104[("09:00", "09:30")].accounts == ["a"]
    assert c104[("10:30", "11:00")].accounts == ["a"]
    assert c104[("15:30", "16:00")].accounts == []       # 15-17 不属于 104 的矩阵
    assert c105[("15:00", "15:30")].accounts == ["a"]
    assert c105[("16:30", "17:00")].accounts == ["a"]
    assert c105[("09:30", "10:00")].accounts == []       # 09-11 不属于 105 的矩阵


def test_seat_slots_guard_matrix_yields_six_segments():
    """真实守护矩阵 (3 账号 × 2 座位 × 3 时段): 每座位每天恰好 3 段 = 6 格守护。"""
    matrix = {
        "xiongjt": {"104": ["09:00-11:00"], "105": ["15:00-17:00"]},
        "wangh":   {"104": ["15:00-17:00"], "105": ["19:00-21:00"]},
        "zhaozh":  {"104": ["19:00-21:00"], "105": ["09:00-11:00"]},
    }
    accs = [Account(id=k, phone="13800000000", password="x",
                    slots=[], bound_seats=list(v.keys()), seat_slots=v)
            for k, v in matrix.items()]
    rows = compute_seat_coverage(accs, [_seat("104"), _seat("105")], DAY)
    by_seat = {r.seat.seat_num: r for r in rows}
    for sn, expected in {
        "104": [("09:00", "xiongjt"), ("15:00", "wangh"), ("19:00", "zhaozh")],
        "105": [("09:00", "zhaozh"), ("15:00", "xiongjt"), ("19:00", "wangh")],
    }.items():
        c = _cells(by_seat[sn])
        for start, acc_id in expected:
            hh, mm = map(int, start.split(":"))
            for half in range(4):                        # 每段 2h = 4 个半小时格
                key = (f"{hh + (mm + half * 30) // 60:02d}:{(mm + half * 30) % 60:02d}",
                       f"{hh + (mm + (half + 1) * 30) // 60:02d}:{(mm + (half + 1) * 30) % 60:02d}")
                assert c[key].accounts == [acc_id], f"{sn} {key}: {c[key].accounts}"
        # 段外无守护
        assert c[("08:00", "08:30")].accounts == []
        assert c[("21:30", "22:00")].accounts == []


def test_seat_slots_and_flat_accounts_coexist():
    """seat_slots 账号与扁平 slots wildcard 账号可以在同一张图上叠加。"""
    rows = compute_seat_coverage(
        [Account(id="ss", phone="13800000000", password="x", slots=[],
                 bound_seats=[], seat_slots={"104": ["09:00-10:00"]}),
         _acc("wild", ["10:00-11:00"])],                 # bound_seats=[] → wildcard
        [_seat("104")], DAY,
    )
    c = _cells(rows[0])
    assert c[("09:00", "09:30")].accounts == ["ss"]
    assert c[("10:00", "10:30")].accounts == ["wild"]


def test_invalid_window_raises():
    try:
        compute_seat_coverage([_acc("a", [])], [_seat("104")], DAY,
                              open_time="10:00", close_time="10:20")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for non-30min window")
