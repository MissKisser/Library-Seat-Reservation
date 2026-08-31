 """Routes helper 单测: _parse_seat_slots / _matrix_set_day / _fmt_weekly。"""
 import pytest

 from seatbot.models import Account
 from seatbot.web.routes import (
     _fmt_weekly,
     _matrix_set_day,
     _parse_seat_slots,
 )
 from seatbot.reconcile import pick_read_account


def _acc(id_: str, phone="138", pw="x") -> Account:
    return Account(id=id_, phone=phone, password=pw, slots=[])


# ---------- _parse_seat_slots（双形态 → 规范 7 键 dict） ----------

def test_parse_seat_slots_empty_returns_none():
    assert _parse_seat_slots("", 2.0, 5.0) is None
    assert _parse_seat_slots("{}", 2.0, 5.0) is None
    assert _parse_seat_slots("   ", 2.0, 5.0) is None


def test_parse_seat_slots_list_form_normalizes_to_weekday_dict():
    # "85" 演示键名 zfill(3) 补零；001/085 均为中性占位座位号
    out = _parse_seat_slots('{"001": ["09:00-11:00"], "85": ["15:00-17:00"]}', 2.0, 5.0)
    assert out["001"]["mon"] == ["09:00-11:00"]
    assert out["001"]["sun"] == ["09:00-11:00"]
    assert out["085"]["tue"] == ["15:00-17:00"]
    assert set(out["001"].keys()) == {
        "mon", "tue", "wed", "thu", "fri", "sat", "sun"}


def test_parse_seat_slots_weekday_form_kept_per_day():
    raw = '{"001": {"mon": ["09:00-11:00"], "sun": []}}'
    out = _parse_seat_slots(raw, 2.0, 5.0)
    assert out["001"]["mon"] == ["09:00-11:00"]
    assert out["001"]["sun"] == []
    assert out["001"]["tue"] == []


def test_parse_seat_slots_rejects_bad_weekday_key():
    with pytest.raises(ValueError, match="星期键"):
        _parse_seat_slots('{"001": {"monday": ["09:00-11:00"]}}', 2.0, 5.0)


def test_parse_seat_slots_rejects_bad_json():
    with pytest.raises(ValueError, match="JSON"):
        _parse_seat_slots("{not-json", 2.0, 5.0)


def test_parse_seat_slots_rejects_bad_seat_key():
    with pytest.raises(ValueError, match="座位号"):
        _parse_seat_slots('{"abc": ["09:00-11:00"]}', 2.0, 5.0)


def test_parse_seat_slots_rejects_bare_string_value():
    with pytest.raises(ValueError, match="字符串数组"):
        _parse_seat_slots('{"001": "09:00-11:00"}', 2.0, 5.0)


def test_parse_seat_slots_rejects_overlong_range():
    with pytest.raises(ValueError, match="上限"):
        _parse_seat_slots('{"001": {"mon": ["19:00-21:30"]}}', 2.0, 5.0)


def test_parse_seat_slots_rejects_bad_range_format():
    with pytest.raises(ValueError, match="格式"):
        _parse_seat_slots('{"001": ["not-a-range"]}', 2.0, 5.0)


def test_parse_seat_slots_rejects_second_slot_per_seat_same_day():
    with pytest.raises(ValueError, match="最多绑定 1 个时段"):
        _parse_seat_slots(
            '{"001": {"mon": ["09:00-10:00", "11:00-12:00"]}}', 2.0, 5.0)


def test_parse_seat_slots_rejects_daily_limit_overrun_same_day():
    with pytest.raises(ValueError, match="每日总时长"):
        _parse_seat_slots(
            '{"001": {"mon": ["08:00-10:00"]}, "002": {"mon": ["10:00-12:00"]},'
            ' "106": {"mon": ["13:00-15:00"]}}',
            2.0, 5.0)


def test_parse_seat_slots_rejects_overlap_across_seats_same_day():
    with pytest.raises(ValueError, match="重叠"):
        _parse_seat_slots(
            '{"001": {"tue": ["09:00-11:00"]}, "002": {"tue": ["10:30-12:30"]}}',
            2.0, 5.0)


def test_parse_seat_slots_same_range_different_days_ok():
    out = _parse_seat_slots(
        '{"001": {"mon": ["09:00-11:00"]}, "002": {"tue": ["09:00-11:00"]}}',
        2.0, 5.0)
    assert out["001"]["mon"] == ["09:00-11:00"]
    assert out["002"]["tue"] == ["09:00-11:00"]


# ---------- _matrix_set_day ----------

def test_matrix_set_day_sets_only_that_weekday():
    m = _matrix_set_day({}, "001", "wed", ["19:00-21:00"])
    assert m["001"]["wed"] == ["19:00-21:00"]
    assert m["001"]["thu"] == []


def test_matrix_set_day_preserves_other_days_and_seats():
    src = {"001": {"mon": ["09:00-11:00"]}, "002": {"tue": ["15:00-17:00"]}}
    m = _matrix_set_day(dict(src), "001", "tue", ["19:00-21:00"])
    assert m["001"]["mon"] == ["09:00-11:00"]
    assert m["001"]["tue"] == ["19:00-21:00"]
    assert m["002"]["tue"] == ["15:00-17:00"]


def test_matrix_set_day_legacy_list_value_migrated():
    m = _matrix_set_day({"001": ["09:00-11:00"]}, "001", "fri", ["19:00-21:00"])
    assert m["001"]["fri"] == ["19:00-21:00"]
    assert m["001"]["mon"] == ["09:00-11:00"]


def test_matrix_set_day_drops_seat_when_all_days_empty():
    m = _matrix_set_day({"001": {"mon": ["09:00-11:00"]}}, "001", "mon", [])
    assert "001" not in m


def test_matrix_set_day_does_not_mutate_input():
    src = {"001": {"mon": ["09:00-11:00"]}}
    _matrix_set_day(src, "001", "tue", ["15:00-17:00"])
    assert "tue" not in src["001"]


# ---------- _fmt_weekly ----------

def test_fmt_weekly_dict_value_per_day_summary():
    assert _fmt_weekly({"mon": ["09:00-11:00"], "wed": []}) == ["周一 09:00-11:00"]


def test_fmt_weekly_legacy_list_value():
    assert _fmt_weekly(["09:00-11:00"]) == ["每天 09:00-11:00"]


def test_fmt_weekly_empty_values():
    assert _fmt_weekly(None) == []
    assert _fmt_weekly({}) == []
    assert _fmt_weekly([]) == []


# ---------- _pick_read_account ----------

def test_pick_read_account_prefers_default():
    accs = [_acc("wangh"), _acc("xiongjt"), _acc("zhaozh")]
    assert _pick_read_account(accs).id == "xiongjt"


def test_pick_read_account_falls_back_when_default_missing():
    accs = [_acc("wangh"), _acc("zhaozh")]
    assert _pick_read_account(accs).id == "wangh"


def test_pick_read_account_skips_credentialess_default():
    accs = [_acc("xiongjt", phone="", pw=""), _acc("zhaozh")]
    assert _pick_read_account(accs).id == "zhaozh"


def test_pick_read_account_none_when_no_credentials():
    assert _pick_read_account([_acc("a", phone="", pw="")]) is None
