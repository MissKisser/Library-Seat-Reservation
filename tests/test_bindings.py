"""bindings 单测：按星期几逐天校验、余量、候选与自动分配。"""
from datetime import time

import pytest

from seatbot.bindings import (
    account_margins,
    auto_assign,
    candidate_accounts,
    desired_slots_of,
    diff_matrices,
    matrix_windows,
    plan_matrix,
    rebind_candidates_for_days,
    rebind_matrices,
    set_day_slots,
    used_hours,
    validate_matrix,
)
from seatbot.models import Account, SeatTarget
from seatbot.utils.weekly import WEEKDAY_KEYS


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


def test_validate_matrix_same_seat_multi_slots_allowed():
    m = {"001": {"mon": ["08:00-10:00", "10:00-12:00", "14:00-16:00"]}}
    validate_matrix(m, max_seg_hours=2.0, daily_limit_hours=8.0)


def test_validate_matrix_same_seat_overlap_rejected():
    bad = {"001": {"mon": ["09:00-11:00", "10:00-12:00"]}}
    with pytest.raises(ValueError, match="周一.*重叠"):
        validate_matrix(bad, max_seg_hours=2.0, daily_limit_hours=8.0)

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


def test_candidate_accounts_allows_same_seat_nonoverlap():
    from datetime import time as _t
    accs = [_acc("a", {"001": {"mon": ["09:00-11:00"]}})]
    out = candidate_accounts(
        accs, seat="001", start=_t(14, 0), end=_t(16, 0),
        exclude_id="nobody", daily_limit_hours=8.0, weekday="mon")
    assert [c["id"] for c in out] == ["a"]

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


def test_auto_assign_one_account_takes_multiple_slots_same_seat():
    accs = [_acc("a", {})]
    desired = {"001": {"mon": ["08:00-10:00", "10:00-12:00"]}}
    matrices, unfillable = auto_assign(
        accs, desired, max_seg_hours=2.0, daily_limit_hours=8.0)
    assert unfillable == []
    assert matrices["a"]["001"]["mon"] == ["08:00-10:00", "10:00-12:00"]


def test_auto_assign_appends_to_existing_binding_same_seat():
    accs = [_acc("a", {"001": {"mon": ["08:00-10:00"]}})]
    desired = {"001": {"mon": ["08:00-10:00", "14:00-16:00"]}}
    matrices, unfillable = auto_assign(
        accs, desired, max_seg_hours=2.0, daily_limit_hours=8.0)
    assert unfillable == []
    assert matrices["a"]["001"]["mon"] == ["08:00-10:00", "14:00-16:00"]

# ---------- plan_matrix / diff_matrices ----------


def _full_weekday(slot: str) -> dict[str, list[str]]:
    """全周统一时段（list 形态）→ 内部会按 normalize 展开为 7 键 dict。"""
    return {wd: [slot] for wd in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}

def test_plan_matrix_minimal_single_account_covers_seat():
    """单账号容量足够时，minimal 用 1 个账号承包整座全天多段。"""
    accs = [_acc("a", {}), _acc("b", {})]
    desired = {"001": {"mon": ["08:00-10:00", "10:00-12:00",
                               "14:00-16:00", "16:00-18:00"]}}
    m, bad = plan_matrix(
        accs, desired, max_seg_hours=2.0, daily_limit_hours=8.0,
        mode="minimal", account_order=["a", "b"])
    assert bad == []
    assert m["a"]["001"]["mon"] == ["08:00-10:00", "10:00-12:00",
                                    "14:00-16:00", "16:00-18:00"]
    assert m["b"] == {}
    for aid in m:
        validate_matrix(m[aid], max_seg_hours=2.0, daily_limit_hours=8.0)


def test_plan_matrix_minimal_cross_seat_overlap_still_needs_two():
    """跨座同时间窗口仍互斥：两个座位同一时段必须两个账号。"""
    accs = [_acc("a", {}), _acc("b", {})]
    desired = {"001": {"mon": ["09:00-11:00"]}, "002": {"mon": ["09:00-11:00"]}}
    m, bad = plan_matrix(
        accs, desired, max_seg_hours=2.0, daily_limit_hours=8.0, mode="minimal")
    assert bad == []
    assert m["a"]["001"]["mon"] == ["09:00-11:00"]
    assert m["b"]["002"]["mon"] == ["09:00-11:00"]


def test_plan_matrix_safe_covers_exact():
    """safe 模式：与 auto_assign 同样按 (wd, seat, slot) 精确覆盖。"""
    accs = [_acc("a", {}), _acc("b", {}), _acc("c", {})]
    desired = {"001": {"mon": ["09:00-11:00"], "tue": ["15:00-17:00"]}}
    m, bad = plan_matrix(
        accs, desired, max_seg_hours=2.0, daily_limit_hours=5.0,
        mode="safe",
    )
    assert bad == []
    filled = [(aid, m[aid]["001"][wd])
              for aid in m for wd in ("mon", "tue")
              if m[aid].get("001", {}).get(wd)]
    assert len(filled) == 2
    # 每账号矩阵必须通过 validate_matrix
    for aid in m:
        validate_matrix(m[aid], max_seg_hours=2.0, daily_limit_hours=5.0)


def test_plan_matrix_exclusive_uses_one_account_per_seat():
    """独占优先：座位与账号一一对应，每座由专属账号整座承接。"""
    accs = [_acc("a", {}), _acc("b", {}), _acc("c", {}), _acc("d", {})]
    # 每天 4 段（4 座位互不重叠）
    desired = {
        "001": {"mon": ["09:00-11:00"]},
        "002": {"mon": ["11:00-13:00"]},
        "003": {"mon": ["13:00-15:00"]},
        "004": {"mon": ["15:00-17:00"]},
    }
    m, bad = plan_matrix(
        accs, desired, max_seg_hours=2.0, daily_limit_hours=5.0,
        mode="minimal", account_order=["a", "b", "c", "d"],
    )
    assert bad == []
    # 一一对应：任何账号都不持有多个座位
    for spec in m.values():
        assert len(spec) <= 1
    assert set(m["a"]) == {"001"}
    assert set(m["b"]) == {"002"}
    assert set(m["c"]) == {"003"}
    assert set(m["d"]) == {"004"}


def test_plan_matrix_account_order_decides_primary_account():
    """account_order 决定主账号优先级：首位账号承接首位座位。"""
    accs = [_acc("a", {}), _acc("b", {})]
    desired = {
        "001": {"mon": ["09:00-11:00"]},
        "002": {"mon": ["11:00-13:00"]},
    }
    m, bad = plan_matrix(
        accs, desired, max_seg_hours=2.0, daily_limit_hours=5.0,
        mode="minimal", account_order=["b", "a"],
    )
    assert bad == []
    assert set(m["b"]) == {"001"}
    assert set(m["a"]) == {"002"}

def test_diff_matrices_add_remove():
    """diff：added = 新矩阵多出的段；removed = 旧矩阵有但新矩阵没有的段。

    diff 条目统一为 {account, seat, weekday, slot} dict（与前端约定一致）。
    """
    old = {"x": {"001": {"mon": ["09:00-11:00"]}}}
    new = {"x": {"001": {"mon": ["15:00-17:00"]}}}
    diff = diff_matrices(old, new)
    assert {"account": "x", "seat": "001",
            "weekday": "mon", "slot": "09:00-11:00"} in diff["removed"]
    assert {"account": "x", "seat": "001",
            "weekday": "mon", "slot": "15:00-17:00"} in diff["added"]


def test_plan_matrix_minimal_two_seats_multi_window():
    """生产缩影：2 座位 × 多窗口（互不重叠）必须绑满全部段，每账号 validate 通过。"""
    accs = [_acc("a", {}), _acc("b", {}), _acc("c", {}), _acc("d", {}),
            _acc("e", {}), _acc("f", {})]
    desired = {
        "001": {
            "mon": ["09:00-11:00", "13:00-15:00", "15:00-17:00"],
            "tue": ["09:00-11:00", "13:00-15:00"],
        },
        "002": {
            "mon": ["11:00-13:00"],
            "tue": ["11:00-13:00", "15:00-17:00"],
        },
    }
    m, bad = plan_matrix(
        accs, desired, max_seg_hours=2.0, daily_limit_hours=5.0,
        mode="minimal",
    )
    total_filled = sum(
        len(slots)
        for aid in m for seat in desired
        for wd in ("mon", "tue")
        for slots in [m[aid].get(seat, {}).get(wd, [])]
    )
    expected = sum(len(slots) for s in desired.values() for slots in s.values())
    assert bad == [], f"应有解：bad={bad}"
    assert total_filled == expected, f"段数 {total_filled} ≠ 期望 {expected}"
    for aid in m:
        validate_matrix(m[aid], max_seg_hours=2.0, daily_limit_hours=5.0)


def test_plan_matrix_unfillable_reports_required_account_count():
    """账号不足：分不下的分段进入 unfillable，并给出所需账号数下界。"""
    accs = [_acc("a", {}), _acc("b", {})]
    m, bad = plan_matrix(
        accs, desired={"001": {"mon": ["09:00-11:00", "11:00-13:00",
                                      "13:00-15:00", "15:00-17:00"]}},
        max_seg_hours=2.0, daily_limit_hours=3.0,
        mode="minimal",
    )
    # 每账号每天最多装 1 段（2 段共 4h > 3h 限额），2 个账号最多覆盖 2 段
    assert any("需至少 3 个账号" in s for s in bad)
    covered = [slot for spec in m.values()
               for seat in spec for slot in spec[seat].get("mon", [])]
    assert len(covered) == 2
    assert set(covered) == {"09:00-11:00", "11:00-13:00"}


# ---------- plan_matrix：座位独占优先 ----------

def test_plan_matrix_exclusive_one_to_one_two_seats_two_accounts():
    """2 座 × 2 账号 × 7 天多段：账号与座位一一对应，互不交叉。"""
    accs = [_acc("a", {}), _acc("b", {})]
    desired = {
        "001": {wd: ["09:00-11:00", "14:00-16:00"] for wd in WEEKDAY_KEYS},
        "002": {wd: ["11:00-13:00", "16:00-18:00"] for wd in WEEKDAY_KEYS},
    }
    for mode in ("safe", "minimal"):
        m, bad = plan_matrix(
            accs, desired, max_seg_hours=2.0, daily_limit_hours=6.0,
            mode=mode, account_order=["a", "b"])
        assert bad == [], f"{mode}: {bad}"
        assert set(m["a"]) == {"001"}
        assert set(m["b"]) == {"002"}
        assert m["a"]["001"]["mon"] == ["09:00-11:00", "14:00-16:00"]
        assert m["b"]["002"]["mon"] == ["11:00-13:00", "16:00-18:00"]
        for aid in m:
            validate_matrix(m[aid], max_seg_hours=2.0, daily_limit_hours=6.0)


def test_plan_matrix_exclusive_single_account_covers_two_seats():
    """账号少于座位：单账号包两座，跨座时段不重叠。"""
    accs = [_acc("a", {})]
    desired = {
        "001": {wd: ["09:00-11:00"] for wd in WEEKDAY_KEYS},
        "002": {wd: ["11:00-13:00"] for wd in WEEKDAY_KEYS},
    }
    for mode in ("safe", "minimal"):
        m, bad = plan_matrix(
            accs, desired, max_seg_hours=2.0, daily_limit_hours=6.0, mode=mode)
        assert bad == [], f"{mode}: {bad}"
        assert set(m["a"]) == {"001", "002"}
        validate_matrix(m["a"], max_seg_hours=2.0, daily_limit_hours=6.0)


def test_plan_matrix_exclusive_extra_account_stays_empty():
    """账号多于座位：多余账号矩阵为空，不产生交叉。"""
    accs = [_acc("a", {}), _acc("b", {}), _acc("c", {})]
    desired = {
        "001": {wd: ["09:00-11:00"] for wd in WEEKDAY_KEYS},
        "002": {wd: ["11:00-13:00"] for wd in WEEKDAY_KEYS},
    }
    for mode in ("safe", "minimal"):
        m, bad = plan_matrix(
            accs, desired, max_seg_hours=2.0, daily_limit_hours=6.0, mode=mode)
        assert bad == [], f"{mode}: {bad}"
        assert set(m["a"]) == {"001"}
        assert set(m["b"]) == {"002"}
        assert m["c"] == {}


def test_plan_matrix_exclusive_overflow_stays_in_same_seat():
    """主账号限额装不下一座全天：溢出段落到次账号，且仍在同一座位。"""
    accs = [_acc("a", {}), _acc("b", {})]
    desired = {
        "001": {wd: ["08:00-10:00", "10:00-12:00", "14:00-16:00"]
                for wd in WEEKDAY_KEYS},
    }
    for mode in ("safe", "minimal"):
        m, bad = plan_matrix(
            accs, desired, max_seg_hours=2.0, daily_limit_hours=4.0, mode=mode)
        assert bad == [], f"{mode}: {bad}"
        assert m["a"]["001"]["mon"] == ["08:00-10:00", "10:00-12:00"]
        assert m["b"]["001"]["mon"] == ["14:00-16:00"]
        # 溢出仍在同一座位，次账号不持有其他座位
        assert set(m["b"]) == {"001"}


def test_plan_matrix_exclusive_rejects_cross_seat_overlap():
    """跨座排他：单账号无法同时持有同时段的两座 → 该段不可覆盖。"""
    accs = [_acc("a", {})]
    desired = {
        "001": {"mon": ["09:00-11:00"]},
        "002": {"mon": ["09:00-11:00"]},
    }
    m, bad = plan_matrix(
        accs, desired, max_seg_hours=2.0, daily_limit_hours=6.0, mode="safe")
    assert any("002" in s for s in bad)
    covered = [slot for spec in m.values()
               for seat in spec for slot in spec[seat].get("mon", [])]
    assert len(covered) == 1


def test_plan_matrix_exclusive_is_deterministic():
    """相同输入必须产生相同输出（无随机 / 时序依赖）。"""
    accs = [_acc("a", {}), _acc("b", {}), _acc("c", {})]
    desired = {
        "001": {wd: ["09:00-11:00", "14:00-16:00"] for wd in WEEKDAY_KEYS},
        "002": {wd: ["11:00-13:00"] for wd in WEEKDAY_KEYS},
    }
    outs = [
        plan_matrix(accs, desired, max_seg_hours=2.0, daily_limit_hours=4.0,
                    mode="safe")
        for _ in range(3)
    ]
    assert outs[0] == outs[1] == outs[2]


def test_auto_assign_safe_vs_minimal_distribution_differs():
    """auto_assign：safe 摊薄 vs minimal 打包，
    同 desired 下 minimal 动用账号数应 ≤ safe。
    """
    desired = {
        "001": {"mon": ["09:00-11:00"], "tue": ["13:00-15:00"],
                "wed": ["15:00-17:00"], "thu": ["09:00-11:00"]},
        "002": {"mon": ["15:00-17:00"], "tue": ["15:00-17:00"]},
    }
    accs = [_acc("a", {}), _acc("b", {}), _acc("c", {}), _acc("d", {})]
    m_safe, _ = auto_assign(
        accs, desired, max_seg_hours=2.0, daily_limit_hours=5.0, mode="safe",
    )
    m_min, _ = auto_assign(
        accs, desired, max_seg_hours=2.0, daily_limit_hours=5.0, mode="minimal",
    )
    safe_active = sum(1 for aid, spec in m_safe.items() if spec)
    min_active = sum(1 for aid, spec in m_min.items() if spec)
    assert min_active <= safe_active


# ---------- set_day_slots ----------

def test_set_day_slots_matches_legacy_semantics():
    m = set_day_slots({"001": ["09:00-11:00"]}, "001", "fri", ["19:00-21:00"])
    assert m["001"]["mon"] == ["09:00-11:00"]
    assert m["001"]["fri"] == ["19:00-21:00"]
    assert set_day_slots({"001": {"mon": ["09:00-11:00"]}}, "001", "mon", []) == {}


# ---------- rebind_candidates_for_days ----------

def test_rebind_candidates_requires_every_day():
    """全周换绑：接手账号必须每天都满足限制，任一天撞车即出局。"""
    accs = [
        _acc("src", {"001": {w: ["09:00-11:00"] for w in WEEKDAY_KEYS}}),
        _acc("free", {}),
        _acc("busy_tue", {"002": {"tue": ["10:00-12:00"]}}),
    ]
    out = rebind_candidates_for_days(
        accs, seat="001", rng="09:00-11:00", weekdays=list(WEEKDAY_KEYS),
        source_id="src", daily_limit_hours=5.0)
    assert [c["id"] for c in out] == ["free"]


def test_rebind_candidates_same_seat_nonoverlap_eligible():
    accs = [
        _acc("src", {"001": {"mon": ["09:00-11:00"]}}),
        _acc("same_seat", {"001": {"mon": ["13:00-15:00"]}}),
        _acc("no_room", {"002": {"mon": ["08:00-10:00", "10:00-13:00"]}}),
        _acc("ok", {"002": {"mon": ["13:00-15:00"]}}),
    ]
    out = rebind_candidates_for_days(
        accs, seat="001", rng="09:00-11:00", weekdays=["mon"],
        source_id="src", daily_limit_hours=5.0)
    # same_seat：同座 13:00-15:00 与 09:00-11:00 不重叠 → 现在可接手
    assert [c["id"] for c in out] == ["ok", "same_seat"]


def test_rebind_candidates_rejects_bad_range():
    with pytest.raises(ValueError):
        rebind_candidates_for_days(
            [], seat="001", rng="bad", weekdays=["mon"],
            source_id="src", daily_limit_hours=5.0)


# ---------- rebind_matrices ----------

def test_rebind_matrices_moves_slot_both_ways():
    src = _acc("src", {"001": {"mon": ["09:00-11:00"], "tue": ["09:00-11:00"]}})
    dst = _acc("dst", {"002": {"mon": ["13:00-15:00"]}})
    src_m, dst_m = rebind_matrices(
        src, dst, seat="001", rng="09:00-11:00", weekdays=["mon"],
        max_seg_hours=2.0, daily_limit_hours=5.0)
    assert src_m["001"]["mon"] == []
    assert src_m["001"]["tue"] == ["09:00-11:00"]
    assert dst_m["001"]["mon"] == ["09:00-11:00"]
    assert dst_m["002"]["mon"] == ["13:00-15:00"]


def test_rebind_matrices_rejects_daily_limit_breach():
    src = _acc("src", {"001": {"mon": ["09:00-11:00"]}})
    dst = _acc("dst", {"002": {"mon": ["13:00-15:00", "15:00-18:00"]}})
    with pytest.raises(ValueError):
        rebind_matrices(
            src, dst, seat="001", rng="09:00-11:00", weekdays=["mon"],
            max_seg_hours=2.0, daily_limit_hours=5.0)
    # 校验失败不得改动任一账号
    assert src.seat_slots == {"001": {"mon": ["09:00-11:00"]}}
    assert dst.seat_slots == {"002": {"mon": ["13:00-15:00", "15:00-18:00"]}}


def test_rebind_matrices_drops_empty_seat_entry():
    src = _acc("src", {"001": {"mon": ["09:00-11:00"]}})
    dst = _acc("dst", {})
    src_m, _ = rebind_matrices(
        src, dst, seat="001", rng="09:00-11:00", weekdays=["mon"],
        max_seg_hours=2.0, daily_limit_hours=5.0)
    assert "001" not in src_m


def test_rebind_matrices_appends_to_dst_existing_slots():
    src = _acc("src", {"001": {"mon": ["09:00-11:00"]}})
    dst = _acc("dst", {"001": {"mon": ["14:00-16:00"]},
                       "002": {"mon": ["19:00-21:00"]}})
    src_m, dst_m = rebind_matrices(
        src, dst, seat="001", rng="09:00-11:00", weekdays=["mon"],
        max_seg_hours=2.0, daily_limit_hours=8.0)
    assert src_m.get("001", {}).get("mon", []) == []
    assert dst_m["001"]["mon"] == ["14:00-16:00", "09:00-11:00"]
    assert dst_m["002"]["mon"] == ["19:00-21:00"]


def test_rebind_matrices_keeps_src_other_slots_same_seat_day():
    src = _acc("src", {"001": {"mon": ["09:00-11:00", "14:00-16:00"]}})
    dst = _acc("dst", {})
    src_m, dst_m = rebind_matrices(
        src, dst, seat="001", rng="09:00-11:00", weekdays=["mon"],
        max_seg_hours=2.0, daily_limit_hours=8.0)
    assert src_m["001"]["mon"] == ["14:00-16:00"]
    assert dst_m["001"]["mon"] == ["09:00-11:00"]

def test_rebind_candidates_empty_when_slot_over_max_seg():
    """时段本身超过单段上限 → 无候选（避免列出必然被拒的账号）。"""
    accs = [_acc("src", {"001": {"mon": ["09:00-13:00"]}}), _acc("free", {})]
    assert rebind_candidates_for_days(
        accs, seat="001", rng="09:00-13:00", weekdays=["mon"],
        source_id="src", daily_limit_hours=5.0, max_seg_hours=2.0) == []
    assert [c["id"] for c in rebind_candidates_for_days(
        accs, seat="001", rng="09:00-11:00", weekdays=["mon"],
        source_id="src", daily_limit_hours=5.0, max_seg_hours=2.0)] == ["free"]
