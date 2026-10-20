"""按策略重排端点：2 座 × 2 账号重排后一一对应，不交叉。

全程使用隔离临时库与打桩的占用查询，零真实网络请求。
"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from seatbot.config import load_config
from seatbot.models import Account
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore
from seatbot.web.app import make_app

TOKEN = "replan-token"

#: 两个座位使用同一组时段：只有"账号↔座位一一对应"才可能不违反跨座排他。
SEAT_SLOTS = ["09:00-11:00", "14:00-16:00"]


def _client(tmp_path, monkeypatch) -> tuple[TestClient, StateStore]:
    """构造带 2 座位 + 2 空账号的隔离面板客户端。"""
    cfg = load_config("config.test.yaml")
    cfg.runtime.db_path = str(tmp_path / "replan.db")
    cfg.runtime.web_token = TOKEN
    store = StateStore(cfg.runtime.db_path)
    asyncio.run(store.init())

    async def seed():
        for seat in ("001", "002"):
            await store.add_target_seat(seat, desired_slots=list(SEAT_SLOTS))
        for acc_id in ("a", "b"):
            await store.upsert_account(Account(
                id=acc_id, phone="13800000000", password="x", slots=[]))

    asyncio.run(seed())
    sched = Scheduler(cfg, store)
    app = make_app(cfg, store, sched)

    import seatbot.web.routes as R

    async def fake_fetch(request, store_, day, seat_nums, fresh=False):
        return [], None

    monkeypatch.setattr(R, "_fetch_others_occupied_cached", fake_fetch)
    return TestClient(app, base_url="http://127.0.0.1:8080"), store


def _post(c: TestClient, path: str):
    return c.post(f"{path}?token={TOKEN}",
                  headers={"Host": "127.0.0.1:8080"},
                  follow_redirects=False)


def test_replan_preview_assigns_one_account_per_seat(tmp_path, monkeypatch):
    """预览：账号与座位一一对应，任一账号都不持有多个座位。"""
    c, store = _client(tmp_path, monkeypatch)
    try:
        r = _post(c, "/bindings/replan/preview")
        assert r.status_code == 200, r.text
        plan = r.json()
        assert plan["unfillable"] == []
        new = plan["new_matrix"]
        assert set(new["a"]) == {"001"}
        assert set(new["b"]) == {"002"}
        assert new["a"]["001"]["mon"] == list(SEAT_SLOTS)
        assert new["b"]["002"]["mon"] == list(SEAT_SLOTS)
    finally:
        asyncio.run(store.close())


def test_replan_apply_writes_one_account_per_seat(tmp_path, monkeypatch):
    """落库：重排写库后账号矩阵仍是一一对应。"""
    c, store = _client(tmp_path, monkeypatch)
    try:
        r = _post(c, "/bindings/replan/apply")
        assert r.status_code == 303, r.text
        accounts = asyncio.run(store.list_accounts())
        by_id = {a.id: a for a in accounts}
        assert set(by_id["a"].seat_slots) == {"001"}
        assert set(by_id["b"].seat_slots) == {"002"}
    finally:
        asyncio.run(store.close())
