"""Routes helper 单测: _parse_seat_slots / _pick_read_account。"""
import pytest

from seatbot.models import Account
from seatbot.web.routes import _parse_seat_slots, _pick_read_account


def _acc(id_: str, phone="138", pw="x") -> Account:
    return Account(id=id_, phone=phone, password=pw, slots=[])


# ---------- _parse_seat_slots ----------

def test_parse_seat_slots_empty_returns_none():
    assert _parse_seat_slots("", 2.0) is None
    assert _parse_seat_slots("{}", 2.0) is None
    assert _parse_seat_slots("   ", 2.0) is None


def test_parse_seat_slots_valid_zfills_keys():
    out = _parse_seat_slots('{"104": ["09:00-11:00"], "85": ["15:00-17:00"]}', 2.0)
    assert out == {"104": ["09:00-11:00"], "085": ["15:00-17:00"]}


def test_parse_seat_slots_rejects_bad_json():
    with pytest.raises(ValueError, match="JSON"):
        _parse_seat_slots("{not-json", 2.0)


def test_parse_seat_slots_rejects_bad_seat_key():
    with pytest.raises(ValueError, match="座位号"):
        _parse_seat_slots('{"abc": ["09:00-11:00"]}', 2.0)


def test_parse_seat_slots_rejects_non_list_value():
    with pytest.raises(ValueError, match="字符串数组"):
        _parse_seat_slots('{"104": "09:00-11:00"}', 2.0)


def test_parse_seat_slots_rejects_overlong_range():
    with pytest.raises(ValueError, match="上限"):
        _parse_seat_slots('{"104": ["19:00-21:30"]}', 2.0)


def test_parse_seat_slots_rejects_bad_range_format():
    with pytest.raises(ValueError, match="时段格式"):
        _parse_seat_slots('{"104": ["not-a-range"]}', 2.0)


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
