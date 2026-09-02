"""bindings 单测：按星期几逐天校验、余量、候选与自动分配。"""
from datetime import time

import pytest

from seatbot.bindings import (
    account_margins,
    auto_assign,
    candidate_accounts,
    desired_slots_of,
    matrix_windows,
    used_hours,
    validate_matrix,
)
from seatbot.models import Account, SeatTarget


def _acc(id_: str, seat_slots) -> Account:
    return Account(id=id_, phone="138", password="x", slots=[], seat_slots=seat_slots)


def _seat_target(desired=None) -> SeatTarget:
    return SeatTarget(seat_num="001", desired_slots=desired)


# ---------- matrix_windows ----------

def test_matrix_windows_weekday_dict_only_that_day():
    m = {"001": {"mon": ["09:00-11:00"], "tue": ["15:00-17:00"]}}
    assert [w[0] for w in matrix_windows(m, "mon")] == ["001"]
    assert matrix_windows(m, "mon")[0][1] == time(9, 0)
    assert [w[1] for w in matrix_windows(m, "tue")] == [time(15, 0)]
    assert matrix_windows(m, "wed") == []


def test_matrix_windows_none_weekday_unions_all_days():
    m = {"001": {"mon": ["09:00-11:00"], "tue": ["15:00-17:00"]}}
    assert len(matrix_windows(m, None)) == 2


def test_matrix_windows_list_form_all_days():
    m = {"001": ["09:00-11:00"]}
    assert len(matrix_windows(m, "fri")) == 1
    assert len(matrix_windows(m, None)) == 1


def test_matrix_windows_rejects_full_day_value():
    with pytest.raises(ValueError, match="字符串列表"):
        matrix_windows({"001": {"mon": "full"}}, "mon")


# ---------- validate_matrix ----------

def test_validate_matrix_per_day_one_slot_per_seat():
    ok = {"001": {"mon": ["09:00-11:00"], "tue": ["15:00-17:00"]}}
    validate_matrix(ok, max_seg_hours=2.0, daily_limit_hours=5.0)


def test_validate_matrix_two_slots_same_day_rejected():
    bad = {"001": {"mon": ["09:00-11:00"]}, "002": {"mon": ["15:00-17:00"], "sat": ["19:00-21:00"]}}
    validate_matrix(bad, max_seg_hours=2.0, daily_limit_hours=5.0)
    bad2 = {"001": {"mon": ["09:00-10:00", "11:00-12:00"]}}
    with pytest.raises(ValueError, match="周一.*最多绑定 1 个时段"):
        validate_matrix(bad2, max_seg_hours=2.0, daily_limit_hours=5.0)


def test_validate_matrix_two_slots_different_days_ok():
    ok = {"001": {"mon": ["09:00-11:00"], "tue": ["09:00-11:00", ]}}
    validate_matrix(ok, max_seg_hours=2.0, daily_limit_hours=5.0)


def test_validate_matrix_overlong_segment_prefixed():
    with pytest.raises(ValueError, match="周三"):
        validate_matrix(
            {"001": {"wed": ["09:00-12:00"]}},
            max_seg_hours=2.0, daily_limit_hours=5.0)


def test_validate_matrix_daily_limit_per_weekday():
    ok = {"001": {"mon": ["08:00-10:00"]}, "002": {"mon": ["10:00-12:00"]},
           "003": {"mon": ["13:00-15:00"]}}
    with pytest.raises(ValueError, match="周一.*每日总时长"):
        validate_matrix(ok, max_seg_hours=2.0, daily_limit_hours=5.0)


def test_validate_matrix_overlap_only_same_day():
    ok = {"001": {"mon": ["09:00-11:00"]}, "002": {"tue": ["10:30-12:30"]}}
    validate_matrix(ok, max_seg_hours=2.0, daily_limit_hours=5.0)
    bad = {"001": {"mon": ["09:00-11:00"]}, "002": {"mon": ["10:30-12:30"]}}
    with pytest.raises(ValueError, match="周一.*重叠"):
        validate_matrix(bad, max_seg_hours=2.0, daily_limit_hours=5.0)


# ---------- used_hours / account_margins ----------

def test_used_hours_per_weekday():
    m = {"001": {"mon": ["09:00-11:00"], "tue": ["15:00-17:00"]}}
    assert used_hours(m, "mon") == 2.0
    assert used_hours(m, "tue") == 2.0
    assert used_hours(m, "sun") == 0.0


def test_account_margins_shape():
    accs = [_acc("a", {"001": {"mon": ["09:00-11:00"]}})]
    out = account_margins(accs, daily_limit_hours=5.0)
    assert out[0]["id"] == "a"
    assert out[0]["days"]["mon"]["used_hours"] == 2.0
    assert out[0]["days"]["mon"]["remaining_hours"] == 3.0
    assert out[0]["days"]["tue"]["used_hours"] == 0.0


# ---------- desired_slots_of ----------

def test_desired_slots_of_none_defaults():
    assert desired_slots_of(_seat_target(None), "wed") == []


def test_desired_slots_of_list_form_uniform():
    assert desired_slots_of(_seat_target(["10:00-12:00"]), "sun") == ["10:00-12:00"]


def test_desired_slots_of_dict_form_per_day():
    d = _seat_target({"mon": ["09:00-11:00"], "sun": []})
    assert desired_slots_of(d, "mon") == ["09:00-11:00"]
    assert desired_slots_of(d, "sun") == []
    assert desired_slots_of(d, "tue") == []


# ---------- candidate_accounts ----------

def test_candidate_accounts_checks_only_given_weekday():
    accs = [
        _acc("busy_mon", {"001": {"mon": ["09:00-11:00"]}}),
        _acc("free_tue", {"002": {"tue": ["13:00-15:00"]}}),
    ]
    from datetime import time as _t
    out = candidate_accounts(
        accs, seat="001", start=_t(9, 0), end=_t(11, 0),
        exclude_id="nobody", daily_limit_hours=5.0, weekday="tue")
    assert [c["id"] for c in out] == ["busy_mon", "free_tue"]
    out_mon = candidate_accounts(
        accs, seat="001", start=_t(9, 0), end=_t(11, 0),
        exclude_id="nobody", daily_limit_hours=5.0, weekday="mon")
    assert [c["id"] for c in out_mon] == ["free_tue"]


def test_candidate_accounts_excludes_cross_seat_overlap_same_day():
    # 账号在"其他座位"当天已有重叠时段 → 不可接手（同一时刻只能持一个预约）
    from datetime import time as _t
    accs = [_acc("a", {"002": {"mon": ["10:00-12:00"]}})]
    out = candidate_accounts(
        accs, seat="001", start=_t(9, 0), end=_t(11, 0),
        exclude_id="nobody", daily_limit_hours=5.0, weekday="mon")
    assert out == []
    out_tue = candidate_accounts(
        accs, seat="001", start=_t(9, 0), end=_t(11, 0),
        exclude_id="nobody", daily_limit_hours=5.0, weekday="tue")
    assert [c["id"] for c in out_tue] == ["a"]


# ---------- auto_assign ----------

def test_auto_assign_fills_per_weekday():
    accs = [_acc("a", {}), _acc("b", {})]
    desired = {"001": {"mon": ["09:00-11:00"], "tue": ["15:00-17:00"]}}
    matrices, unfillable = auto_assign(
        accs, desired, max_seg_hours=2.0, daily_limit_hours=5.0)
    assert unfillable == []
    filled = [(aid, m["001"].get(wd))
              for aid, m in matrices.items()
              for wd in ("mon", "tue") if m.get("001", {}).get(wd)]
    assert len(filled) == 2
    for _aid, day_map in filled:
        assert day_map in (["09:00-11:00"], ["15:00-17:00"])


def test_auto_assign_respects_per_day_overlap():
    accs = [_acc("a", {"001": {"mon": ["09:00-11:00"]}})]
    desired = {"002": {"mon": ["10:00-12:00"], "tue": ["10:00-12:00"]}}
    matrices, unfillable = auto_assign(
        accs, desired, max_seg_hours=2.0, daily_limit_hours=5.0)
    assert matrices["a"]["002"].get("tue") == ["10:00-12:00"]
    assert matrices["a"]["002"].get("mon") in (None, [])
    assert any("周一" in u for u in unfillable)


def test_auto_assign_keeps_existing_bindings():
    accs = [_acc("a", {"001": {"mon": ["09:00-11:00"]}})]
    desired = {"001": {"mon": ["09:00-11:00"], "tue": ["15:00-17:00"]}}
    matrices, _ = auto_assign(
        accs, desired, max_seg_hours=2.0, daily_limit_hours=5.0)
    assert matrices["a"]["001"]["mon"] == ["09:00-11:00"]
    assert matrices["a"]["001"]["tue"] == ["15:00-17:00"]


def test_auto_assign_output_is_canonical_seven_keys():
    accs = [_acc("a", {})]
    desired = {"001": {"mon": ["09:00-11:00"]}}
    matrices, _ = auto_assign(accs, desired, max_seg_hours=2.0, daily_limit_hours=5.0)
    assert set(matrices["a"]["001"].keys()) == {
        "mon", "tue", "wed", "thu", "fri", "sat", "sun"}


def test_auto_assign_leaves_no_empty_seat_residue():
    # 回溯中被试过但最终未选中的账号，不应留下全空座位条目
    accs = [_acc("a", {}), _acc("b", {})]
    desired = {"001": {"mon": ["09:00-11:00"]}}
    matrices, _ = auto_assign(accs, desired, max_seg_hours=2.0, daily_limit_hours=5.0)
    assert matrices["a"]["001"]["mon"] == ["09:00-11:00"]
    assert "001" not in matrices["b"]
