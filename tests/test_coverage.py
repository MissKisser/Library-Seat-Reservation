"""Tests for seatbot.coverage — merge guard accounts' slots into a coverage map."""
from datetime import date, time

import pytest

from seatbot.coverage import compute_coverage
from seatbot.models import Account


def _acc(aid: str, slots) -> Account:
    return Account(id=aid, phone="13800000000", password="p", slots=slots)


def test_full_day_single_account_covers_all_cells():
    cov = compute_coverage(
        [_acc("a", "full")],
        day=date(2026, 7, 9),
        open_time="08:00",
        close_time="22:00",
    )
    assert cov.total_cells == 14 * 2  # 14h * 2 cells/h
    assert cov.covered_cells == cov.total_cells
    assert cov.gaps == []


def test_partial_slot_leaves_gap():
    cov = compute_coverage(
        [_acc("a", ["09:00-11:00"])],
        day=date(2026, 7, 9),
    )
    # 09:00-11:00 = 4 cells; 24 cells total
    assert cov.covered_cells == 4
    # Gap is 08:00-09:00 and 11:00-22:00
    gaps = cov.gaps
    assert (time(8, 0), time(9, 0)) in gaps
    assert (time(11, 0), time(22, 0)) in gaps


def test_two_accounts_overlap_one_cell():
    cov = compute_coverage(
        [
            _acc("a", ["09:00-11:00"]),
            _acc("b", ["10:30-12:30"]),
        ],
        day=date(2026, 7, 9),
    )
    # overlapping cells are 10:30-11:00 (1 cell, covered by both)
    overlap = cov.overlaps
    assert overlap, "expected at least one overlap range"
    # Find the overlap containing 10:30-11:00
    ten_thirty = [r for r in overlap if r[0] == time(10, 30) and r[1] == time(11, 0)]
    assert ten_thirty, f"missing 10:30-11:00 overlap, got {overlap}"
    assert set(ten_thirty[0][2]) == {"a", "b"}


def test_three_accounts_form_full_coverage_with_overlaps():
    # 8-12 / 11-13 / 12-14 -> no gaps (continuous with overlaps)
    cov = compute_coverage(
        [
            _acc("a", ["08:00-12:00"]),
            _acc("b", ["11:00-13:00"]),
            _acc("c", ["12:00-14:00"]),
        ],
        day=date(2026, 7, 9),
        open_time="08:00",
        close_time="14:00",
    )
    assert cov.gaps == []
    assert cov.total_cells == 12  # 6h
    assert cov.covered_cells == 12
    # 3 overlap cells: 11:00-12:00 (a+b), 12:00-13:00 (b+c), and so on
    assert cov.overlaps


def test_account_with_broken_slots_is_skipped():
    cov = compute_coverage(
        [
            _acc("good", "full"),
            _acc("bad", "garbage"),
        ],
        day=date(2026, 7, 9),
    )
    # bad is silently skipped; coverage from good still total
    assert cov.covered_cells == cov.total_cells


def test_cell_30min_granularity():
    # 30-min slot exactly fills 1 cell
    cov = compute_coverage(
        [_acc("a", ["09:00-09:30"])],
        day=date(2026, 7, 9),
    )
    assert cov.covered_cells == 1


def test_empty_accounts_full_gap():
    cov = compute_coverage(
        [], day=date(2026, 7, 9),
    )
    assert cov.covered_cells == 0
    gaps = cov.gaps
    assert gaps == [(time(8, 0), time(22, 0))]


def test_custom_open_close_short_window():
    cov = compute_coverage(
        [_acc("a", "full")],
        day=date(2026, 7, 9),
        open_time="09:00",
        close_time="11:00",
    )
    # 2h = 4 cells
    assert cov.total_cells == 4


def test_invalid_window_raises():
    with pytest.raises(ValueError):
        compute_coverage(
            [_acc("a", "full")],
            day=date(2026, 7, 9),
            open_time="22:00",
            close_time="08:00",
        )


def test_non_aligned_window_raises():
    with pytest.raises(ValueError):
        compute_coverage(
            [_acc("a", "full")],
            day=date(2026, 7, 9),
            open_time="08:15",
            close_time="10:00",
        )