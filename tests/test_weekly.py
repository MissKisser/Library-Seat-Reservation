"""weekly 工具单测：双形态归一与按天取值。"""
from datetime import date

import pytest

from seatbot.utils.weekly import (
    WEEKDAY_KEYS,
    normalize_weekly,
    slots_for_weekday,
    weekday_key,
)


def test_weekday_key_maps_monday_to_mon():
    assert weekday_key(date(2026, 8, 31)) == "mon"   # 周一
    assert weekday_key(date(2026, 9, 6)) == "sun"    # 周日


def test_normalize_none_passes_through():
    assert normalize_weekly(None) is None


def test_normalize_list_expands_to_seven_identical_keys():
    out = normalize_weekly(["09:00-11:00"])
    assert list(out.keys()) == list(WEEKDAY_KEYS)
    assert all(out[w] == ["09:00-11:00"] for w in WEEKDAY_KEYS)


def test_normalize_dict_fills_missing_days_with_empty():
    out = normalize_weekly({"mon": ["09:00-11:00"], "sat": []})
    assert out["mon"] == ["09:00-11:00"]
    assert out["sat"] == []
    assert out["sun"] == []
    assert set(out.keys()) == set(WEEKDAY_KEYS)


def test_normalize_rejects_bad_weekday_key():
    with pytest.raises(ValueError, match="星期键"):
        normalize_weekly({"monday": ["09:00-11:00"]})


def test_normalize_rejects_bare_string_without_full():
    with pytest.raises(ValueError, match="字符串数组"):
        normalize_weekly("full")


def test_normalize_accepts_full_when_allowed():
    out = normalize_weekly("full", allow_full=True)
    assert all(out[w] == "full" for w in WEEKDAY_KEYS)


def test_normalize_day_value_full_inside_dict():
    out = normalize_weekly({"sun": "full"}, allow_full=True)
    assert out["sun"] == "full"


def test_normalize_rejects_non_list_day_value():
    with pytest.raises(ValueError):
        normalize_weekly({"mon": "09:00-11:00"})


def test_slots_for_weekday_list_form_same_every_day():
    assert slots_for_weekday(["09:00-11:00"], "sat") == ["09:00-11:00"]


def test_slots_for_weekday_dict_form_missing_day_empty():
    val = {"mon": ["09:00-11:00"]}
    assert slots_for_weekday(val, "mon") == ["09:00-11:00"]
    assert slots_for_weekday(val, "tue") == []


def test_slots_for_weekday_full_passthrough():
    assert slots_for_weekday("full", "wed") == "full"
    assert slots_for_weekday({"sun": "full"}, "sun") == "full"


def test_slots_for_weekday_none_is_empty():
    assert slots_for_weekday(None, "mon") == []
