from datetime import time, timedelta

import pytest

from seatbot.utils.timeutil import (
    parse_hhmm,
    parse_range,
    expand_full_day,
    split_into_chunks,
    CST,
)


def test_parse_hhmm():
    assert parse_hhmm("08:30") == time(8, 30)
    with pytest.raises(ValueError):
        parse_hhmm("25:00")
    with pytest.raises(ValueError):
        parse_hhmm("8:30")


def test_parse_range():
    assert parse_range("08:00-10:00") == (time(8, 0), time(10, 0))
    with pytest.raises(ValueError):
        parse_range("08-10")
    with pytest.raises(ValueError):
        parse_range("10:00-08:00")


def test_cst_timezone():
    assert CST.utcoffset(None) == timedelta(hours=8)


def test_expand_full_day():
    chunks = expand_full_day("08:00", "22:00", max_hours=2.0)
    assert [(s.isoformat(timespec="minutes"), e.isoformat(timespec="minutes"))
            for s, e in chunks] == [
        ("08:00", "10:00"),
        ("10:00", "12:00"),
        ("12:00", "14:00"),
        ("14:00", "16:00"),
        ("16:00", "18:00"),
        ("18:00", "20:00"),
        ("20:00", "22:00"),
    ]


def test_split_into_chunks_basic():
    chunks = split_into_chunks(parse_range("08:00-12:00"), max_hours=2.0)
    assert [(s, e) for s, e in chunks] == [
        (time(8, 0), time(10, 0)),
        (time(10, 0), time(12, 0)),
    ]


def test_split_into_chunks_already_small():
    chunks = split_into_chunks(parse_range("14:00-15:00"), max_hours=2.0)
    assert chunks == [(time(14, 0), time(15, 0))]


def test_split_into_chunks_non_multiple():
    chunks = split_into_chunks(parse_range("08:00-13:00"), max_hours=2.0)
    # 5 hours → 2 + 2 + 1
    assert [(s, e) for s, e in chunks] == [
        (time(8, 0), time(10, 0)),
        (time(10, 0), time(12, 0)),
        (time(12, 0), time(13, 0)),
    ]