"""Tests for compute_seat_coverage (v2 per-seat coverage)."""
from datetime import date, time

from seatbot.coverage import compute_seat_coverage
from seatbot.models import Account, SeatTarget


DAY = date(2026, 8, 26)


def _acc(id_: str, slots, bound_seats=None, **kw):
    return Account(
        id=id_, phone="13800000000", password="x",
        slots=slots, bound_seats=bound_seats or [], **kw,
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
    assert c105[("09:00", "09:30")].accounts == []


def test_unbound_account_covers_nothing():
    # 空绑定账号不参与守护, 不再通配全部目标座位
    rows = compute_seat_coverage(
        [_acc("w", ["15:00-16:00"])],          # bound_seats=[] → 不参与
        [_seat("104"), _seat("105")], DAY,
    )
    for row in rows:
        c = _cells(row)
        assert c[("15:00", "15:30")].accounts == []


def test_gap_property_reports_uncovered_span():
    rows = compute_seat_coverage(
        [_acc("a", ["09:00-10:00"], bound_seats=["104"])],
        [_seat("104")], DAY,
    )
    gaps = rows[0].coverage.gaps
    assert any(g[0] == time(10, 0) and g[1] == time(22, 0) for g in gaps)


def test_user_reserved_and_others_occupied_overlay():
    rows = compute_seat_coverage(
        [_acc("a", ["09:00-10:00"], bound_seats=["104"])],
        [_seat("104")], DAY,
        user_reserved=[("104", time(12, 0), time(13, 0))],
        others_occupied=[("104", time(18, 0), time(19, 0))],
    )
    c = _cells(rows[0])
    assert c[("09:00", "09:30")].accounts == ["a"]
    assert c[("09:00", "09:30")].user_reserved is False
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
    """seat_slots 账号与显式绑定 bound_seats 的扁平账号可以叠加。"""
    rows = compute_seat_coverage(
        [_acc("ss", [], seat_slots={"104": ["09:00-10:00"]}),
         _acc("flat", ["10:00-11:00"], bound_seats=["104"])],
        [_seat("104")], DAY,
    )
    c = _cells(rows[0])
    assert c[("09:00", "09:30")].accounts == ["ss"]
    assert c[("10:00", "10:30")].accounts == ["flat"]


def test_invalid_window_raises():
    try:
        compute_seat_coverage([_acc("a", [])], [_seat("104")], DAY,
                              open_time="10:00", close_time="10:20")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for non-30min window")


def test_coverage_weekday_dict_paints_only_that_weekday():
    acc = _acc("a", [], seat_slots={"104": {"mon": ["09:00-10:00"], "tue": ["15:00-16:00"]}})
    rows_mon = compute_seat_coverage([acc], [_seat("104")], date(2026, 8, 31))
    rows_tue = compute_seat_coverage([acc], [_seat("104")], date(2026, 9, 1))
    cells_mon = _cells(rows_mon[0])
    cells_tue = _cells(rows_tue[0])
    assert cells_mon[("09:00", "09:30")].accounts == ["a"]
    assert ("15:00", "15:30") not in cells_mon or \
        cells_mon[("15:00", "15:30")].accounts == []
    assert cells_tue[("15:00", "15:30")].accounts == ["a"]
    assert cells_tue[("09:00", "09:30")].accounts == []


def test_coverage_weekday_missing_day_blank():
    acc = _acc("a", [], seat_slots={"104": {"mon": ["09:00-10:00"]}})
    rows = compute_seat_coverage([acc], [_seat("104")], date(2026, 9, 6))  # 周日
    cells = _cells(rows[0])
    assert all(c.accounts == [] for c in cells.values())
