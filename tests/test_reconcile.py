"""实况核对判定函数单测（纯同步，不发任何网络请求）。"""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

from seatbot.reconcile import classify_task, pick_read_account
from seatbot.store import StateStore


def test_fully_covered_is_consistent():
    ratio, bad = classify_task("14:00", "16:00", [("14:00", "16:00")])
    assert ratio == 1.0 and not bad


def test_partially_covered_is_mismatch():
    ratio, bad = classify_task("14:00", "16:00", [("15:00", "17:00")])
    assert ratio == 0.5 and bad


def test_zero_coverage_is_mismatch():
    _, bad = classify_task("14:00", "16:00", [])
    assert bad


def test_adjacent_server_windows_merge():
    ratio, bad = classify_task("14:00", "16:00", [("13:00", "15:00"), ("15:00", "17:00")])
    assert ratio == 1.0 and not bad


def test_pick_read_account_prefers_freshest_cookie():
    a = SimpleNamespace(id="张三", phone="1", password="p")
    b = SimpleNamespace(id="李四", phone="2", password="p")
    c = SimpleNamespace(id="王五", phone="3", password="p")
    assert pick_read_account([a, b, c], {"张三": 100, "李四": 200}).id == "李四"


def test_pick_read_account_falls_back_without_cookie_records():
    b = SimpleNamespace(id="李四", phone="2", password="p")
    c = SimpleNamespace(id="王五", phone="3", password="p")
    assert pick_read_account([b, c]).id == "李四"


def test_pick_read_account_ignores_deleted_account_recency():
    a = SimpleNamespace(id="张三", phone="1", password="p")
    c = SimpleNamespace(id="王五", phone="3", password="p")
    assert pick_read_account([a, c], {"李四": 999}).id == "张三"


def test_pick_read_account_none_when_no_credentials():
    assert pick_read_account([SimpleNamespace(id="x", phone="", password="")]) is None


def test_reconcile_results_roundtrip_and_cap(tmp_path):
    async def main():
        s = StateStore(str(tmp_path / "t.db"))
        await s.init()
        for i in range(5):
            await s.save_reconcile_result(
                date(2026, 9, 1), "030", i % 2 == 0, f'{{"n":{i}}}')
        rows = await s.list_reconcile_results(limit=3)
        assert len(rows) == 3
        assert rows[0]["detail"] == '{"n":4}'      # 新→旧
        assert rows[0]["ok"] is True and rows[1]["ok"] is False
        await s.close()
    asyncio.run(main())
