"""专属分配（dedicated）档与绑定页体检提示单测。

覆盖：
  - dedicated：座位↔账号严格一一对应，账号不足或额度不够时留缺口、绝不跨座
  - dedicated 与 safe/minimal 的跨座账号数对比（dedicated 必须为 0）
  - 绑定页体检上下文（座位数 / 账号数 / 跨座账号数 / 缺口座位数）
  - 设置页渲染含 dedicated 档文案

全程使用隔离临时库与打桩的占用查询，零真实网络请求。
"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from seatbot import settings as _settings
from seatbot.bindings import exclusive_health, plan_matrix, validate_matrix
from seatbot.config import load_config
from seatbot.models import Account
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore
from seatbot.utils.weekly import WEEKDAY_KEYS
from seatbot.web.app import make_app

TOKEN = "dedicated-token"
_AUTH = {"Host": "127.0.0.1:8080", "X-Auth-Token": TOKEN}


def _acc(id_: str, seat_slots=None) -> Account:
    """构造中性账号（a/b/c）。"""
    return Account(id=id_, phone="138", password="x", slots=[], seat_slots=seat_slots or {})


def _cross_seat_count(matrices: dict) -> int:
    """矩阵中同时持有 ≥2 个座位的账号数。"""
    return sum(1 for spec in matrices.values() if len(spec) >= 2)


# ---------- dedicated：一一对应 ----------

def test_dedicated_two_seats_two_accounts_one_to_one():
    """2 座 × 2 账号：严格 a→001、b→002，零跨座且无缺口。"""
    accs = [_acc("a"), _acc("b")]
    desired = {
        "001": {wd: ["09:00-11:00", "14:00-16:00"] for wd in WEEKDAY_KEYS},
        "002": {wd: ["11:00-13:00"] for wd in WEEKDAY_KEYS},
    }
    m, bad = plan_matrix(
        accs, desired, max_seg_hours=2.0, daily_limit_hours=6.0,
        mode="dedicated", account_order=["a", "b"])
    assert bad == [], f"不应有缺口：{bad}"
    assert set(m["a"]) == {"001"}
    assert set(m["b"]) == {"002"}
    assert _cross_seat_count(m) == 0
    assert m["a"]["001"]["mon"] == ["09:00-11:00", "14:00-16:00"]
    for aid in m:
        validate_matrix(m[aid], max_seg_hours=2.0, daily_limit_hours=6.0)


def test_dedicated_three_seats_two_accounts_leaves_gap():
    """3 座 × 2 账号：第 3 座整体留缺口，任何账号都不跨座。"""
    accs = [_acc("a"), _acc("b")]
    desired = {
        "001": {wd: ["09:00-11:00"] for wd in WEEKDAY_KEYS},
        "002": {wd: ["11:00-13:00"] for wd in WEEKDAY_KEYS},
        "003": {wd: ["16:00-18:00"] for wd in WEEKDAY_KEYS},
    }
    m, bad = plan_matrix(
        accs, desired, max_seg_hours=2.0, daily_limit_hours=6.0,
        mode="dedicated", account_order=["a", "b"])
    assert any("需至少 3 个账号（当前 2 个）" in s for s in bad), bad
    assert _cross_seat_count(m) == 0, "不得让任一账号跨座"
    assert set(m["a"]) == {"001"}
    assert set(m["b"]) == {"002"}
    assert "003" not in m["a"] and "003" not in m["b"]
    # 第 3 座的期望时段整座未覆盖
    covered = {slot for spec in m.values() for daymap in spec.values()
               for slots in daymap.values() for slot in slots}
    assert "16:00-18:00" not in covered


def test_dedicated_primary_over_daily_limit_leaves_gap():
    """主账号当日限额不足：该段留缺口，不转交他人。"""
    accs = [_acc("a"), _acc("b")]
    desired = {
        "001": {wd: ["08:00-10:00", "10:00-12:00", "14:00-16:00"]
                for wd in WEEKDAY_KEYS},
    }
    m, bad = plan_matrix(
        accs, desired, max_seg_hours=2.0, daily_limit_hours=4.0,
        mode="dedicated", account_order=["a", "b"])
    assert bad, "额度不足的段应进入 unfillable"
    assert m["a"]["001"]["mon"] == ["08:00-10:00", "10:00-12:00"]
    assert m["b"] == {}, "溢出段不得转交他人"
    assert _cross_seat_count(m) == 0


def test_dedicated_extra_account_stays_empty():
    """3 账号 2 座：第三个账号矩阵为空。"""
    accs = [_acc("a"), _acc("b"), _acc("c")]
    desired = {
        "001": {wd: ["09:00-11:00"] for wd in WEEKDAY_KEYS},
        "002": {wd: ["11:00-13:00"] for wd in WEEKDAY_KEYS},
    }
    m, bad = plan_matrix(
        accs, desired, max_seg_hours=2.0, daily_limit_hours=6.0,
        mode="dedicated", account_order=["a", "b", "c"])
    assert bad == []
    assert set(m["a"]) == {"001"}
    assert set(m["b"]) == {"002"}
    assert m["c"] == {}


def test_dedicated_never_cross_seat_while_safe_and_minimal_may():
    """同输入下 dedicated 跨座账号数恒为 0，safe/minimal 允许跨座。"""
    accs = [_acc("a"), _acc("b")]
    desired = {
        "001": {wd: ["09:00-11:00"] for wd in WEEKDAY_KEYS},
        "002": {wd: ["11:00-13:00"] for wd in WEEKDAY_KEYS},
        "003": {wd: ["16:00-18:00"] for wd in WEEKDAY_KEYS},
    }
    counts = {}
    for mode in ("safe", "minimal", "dedicated"):
        m, _bad = plan_matrix(
            accs, desired, max_seg_hours=2.0, daily_limit_hours=6.0,
            mode=mode, account_order=["a", "b"])
        counts[mode] = _cross_seat_count(m)
    assert counts["dedicated"] == 0
    assert counts["safe"] >= 1
    assert counts["minimal"] >= 1


def test_dedicated_is_deterministic():
    """相同输入必须产生完全一致的输出。"""
    accs = [_acc("a"), _acc("b"), _acc("c")]
    desired = {
        "001": {wd: ["09:00-11:00", "14:00-16:00"] for wd in WEEKDAY_KEYS},
        "002": {wd: ["11:00-13:00"] for wd in WEEKDAY_KEYS},
        "003": {wd: ["16:00-18:00"] for wd in WEEKDAY_KEYS},
    }
    outs = [
        plan_matrix(accs, desired, max_seg_hours=2.0, daily_limit_hours=6.0,
                    mode="dedicated", account_order=["a", "b", "c"])
        for _ in range(3)
    ]
    assert outs[0] == outs[1] == outs[2]


def test_dedicated_respects_seat_lexicographic_order():
    """座位按字典序、账号按 account_order 一一对应。"""
    accs = [_acc("a"), _acc("b")]
    desired = {
        "001": {wd: ["09:00-11:00"] for wd in WEEKDAY_KEYS},
        "002": {wd: ["11:00-13:00"] for wd in WEEKDAY_KEYS},
    }
    m, _bad = plan_matrix(
        accs, desired, max_seg_hours=2.0, daily_limit_hours=6.0,
        mode="dedicated", account_order=["b", "a"])
    assert set(m["b"]) == {"001"}
    assert set(m["a"]) == {"002"}


# ---------- 档位白名单 ----------

def test_dedicated_is_valid_allocation_strategy():
    """dedicated 进入档位白名单，且标签与帮助文案齐全。"""
    assert "dedicated" in _settings.ALLOCATION_STRATEGIES
    assert _settings.normalize_allocation_strategy("dedicated") == "dedicated"
    assert _settings.ALLOCATION_STRATEGY_LABELS["dedicated"]
    assert _settings.ALLOCATION_STRATEGY_HELP["dedicated"]["label"]
    assert _settings.ALLOCATION_STRATEGY_HELP["dedicated"]["desc"]
    for key in _settings.ALLOCATION_STRATEGIES:
        assert key in _settings.ALLOCATION_STRATEGY_LABELS
        assert key in _settings.ALLOCATION_STRATEGY_HELP


# ---------- 绑定页体检 ----------

def test_exclusive_health_counts_cross_seat_and_uncovered():
    """体检纯函数：座位数 / 账号数 / 跨座账号数 / 缺口座位数。"""
    accs = [
        _acc("a", {"001": {"mon": ["09:00-11:00"]}}),
        _acc("b", {"002": {"mon": ["11:00-13:00"]},
                   "003": {"mon": ["16:00-18:00"]}}),
    ]
    health = exclusive_health(accs, ["001", "002", "003"])
    assert health["seat_count"] == 3
    assert health["account_count"] == 2
    assert health["cross_seat_accounts"] == ["b"]
    assert health["cross_seat_count"] == 1
    assert health["uncovered_seats"] == []
    assert health["uncovered_count"] == 0
    assert health["one_to_one"] is False


def test_exclusive_health_reports_uncovered_seat():
    """无账号覆盖的座位计入缺口。"""
    accs = [_acc("a", {"001": {"mon": ["09:00-11:00"]}})]
    health = exclusive_health(accs, ["001", "002"])
    assert health["seat_count"] == 2
    assert health["account_count"] == 1
    assert health["uncovered_seats"] == ["002"]
    assert health["uncovered_count"] == 1
    assert health["cross_seat_count"] == 0


def test_exclusive_health_one_to_one_when_matched():
    """座位与账号一一对应且全部有覆盖时体检通过。"""
    accs = [_acc("a", {"001": {"mon": ["09:00-11:00"]}}),
            _acc("b", {"002": {"mon": ["11:00-13:00"]}})]
    health = exclusive_health(accs, ["001", "002"])
    assert health["one_to_one"] is True


# ---------- 页面渲染 ----------

def _client(tmp_path, monkeypatch, seats, accounts):
    """构造带目标座位与账号的隔离面板客户端。"""
    cfg = load_config("config.test.yaml")
    cfg.runtime.db_path = str(tmp_path / "dedicated.db")
    cfg.runtime.web_token = TOKEN
    store = StateStore(cfg.runtime.db_path)

    async def seed():
        await store.init()
        for seat in seats:
            await store.add_target_seat(seat, desired_slots=["09:00-11:00"])
        for acc in accounts:
            await store.upsert_account(acc)

    asyncio.run(seed())
    sched = Scheduler(cfg, store)
    app = make_app(cfg, store, sched)

    import seatbot.web.routes as R

    async def fake_fetch(request, store_, day, seat_nums, fresh=False):
        return [], None

    monkeypatch.setattr(R, "_fetch_others_occupied_cached", fake_fetch)
    return TestClient(app, base_url="http://127.0.0.1:8080"), store


def test_bindings_page_shows_exclusive_health(tmp_path, monkeypatch):
    """绑定页体检行：座位数 / 账号数 / 跨座账号数 / 缺口座位数。"""
    c, store = _client(
        tmp_path, monkeypatch, ["001", "002", "003"],
        [_acc("a", {"001": {"mon": ["09:00-11:00"]}}),
         _acc("b", {"002": {"mon": ["11:00-13:00"]},
                    "003": {"mon": ["16:00-18:00"]}})],
    )
    try:
        r = c.get("/bindings", headers=_AUTH)
        assert r.status_code == 200, r.text
        html = r.text
        assert "专属体检" in html
        assert "3 座 / 2 账号" in html
        assert "1 个账号跨座" in html
        assert "按独占策略重排" in html
    finally:
        asyncio.run(store.close())


def test_bindings_page_reports_uncovered_seat(tmp_path, monkeypatch):
    """缺口座位在体检行里标出。"""
    c, store = _client(
        tmp_path, monkeypatch, ["001", "002"],
        [_acc("a", {"001": {"mon": ["09:00-11:00"]}})],
    )
    try:
        r = c.get("/bindings", headers=_AUTH)
        assert r.status_code == 200, r.text
        html = r.text
        assert "2 座 / 1 账号" in html
        assert "1 座无覆盖" in html
    finally:
        asyncio.run(store.close())


def test_bindings_page_health_passes_when_one_to_one(tmp_path, monkeypatch):
    """一一对应时给出通过标记，且不显示重排入口。"""
    c, store = _client(
        tmp_path, monkeypatch, ["001", "002"],
        [_acc("a", {"001": {"mon": ["09:00-11:00"]}}),
         _acc("b", {"002": {"mon": ["11:00-13:00"]}})],
    )
    try:
        r = c.get("/bindings", headers=_AUTH)
        assert r.status_code == 200, r.text
        html = r.text
        assert "一一对应" in html
        assert "按独占策略重排" not in html
    finally:
        asyncio.run(store.close())


def test_settings_page_renders_dedicated_option(tmp_path, monkeypatch):
    """设置页列出 dedicated 档及其文案。"""
    c, store = _client(tmp_path, monkeypatch, ["001"], [_acc("a")])
    try:
        r = c.get("/settings", headers=_AUTH)
        assert r.status_code == 200, r.text
        html = r.text
        assert 'value="dedicated"' in html
        assert _settings.ALLOCATION_STRATEGY_HELP["dedicated"]["label"] in html
        assert _settings.ALLOCATION_STRATEGY_HELP["dedicated"]["desc"] in html
    finally:
        asyncio.run(store.close())


def test_replan_preview_accepts_dedicated_override(tmp_path, monkeypatch):
    """重排预览可按 dedicated 档计算：座位↔账号一一对应。"""
    c, store = _client(
        tmp_path, monkeypatch, ["001", "002"], [_acc("a"), _acc("b")])
    try:
        r = c.post("/bindings/replan/preview?strategy=dedicated",
                   headers=_AUTH, follow_redirects=False)
        assert r.status_code == 200, r.text
        plan = r.json()
        assert plan["strategy"] == "dedicated"
        assert plan["unfillable"] == []
        new = plan["new_matrix"]
        assert set(new["a"]) == {"001"}
        assert set(new["b"]) == {"002"}
    finally:
        asyncio.run(store.close())
