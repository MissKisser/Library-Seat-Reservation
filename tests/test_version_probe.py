"""版本探针库侧分量测试。

覆盖:
  - StateStore.max_task_updated_at: 空库返回 0, 建任务后等于该任务 updated_at
  - /api/version 的 v = 内存写计数 + 库侧最大 updated_at (带外写入可感知)
  - store 不支持库侧查询时退回纯内存计数, 探针不炸
"""
from __future__ import annotations

import asyncio
from datetime import date, time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from seatbot.models import Task, TaskStatus
from seatbot.store import StateStore
from seatbot.web.routes import router


def test_max_task_updated_at_tracks_writes(tmp_path):
    asyncio.run(_max_task_updated_at_tracks_writes(tmp_path))


async def _max_task_updated_at_tracks_writes(tmp_path):
    store = StateStore(str(tmp_path / "vp.db"))
    await store.init()
    try:
        assert await store.max_task_updated_at() == 0

        t = Task(
            id=None, account_id="a1", day=date(2026, 9, 1),
            start_time=time(14, 0), end_time=time(16, 0),
            seat_num="030", status=TaskStatus.PENDING,
        )
        await store.add_task(t)
        assert await store.max_task_updated_at() > 0

        # 模拟带外写入: 绕过 store 方法直改数据库 (等价人工补约脚本)
        out_of_band_ts = 1788157404032
        await store.db.execute(
            "UPDATE tasks SET updated_at = ?", (out_of_band_ts,))
        await store.db.commit()

        assert await store.max_task_updated_at() == out_of_band_ts
    finally:
        await store.close()


class _CounterStore:
    data_version = 3

    async def max_task_updated_at(self):
        return 1788157404032


class _CounterOnlyStore:
    data_version = 3


class _StubCfg:
    class library:
        room_id = 0
        open_time = time(8, 0)
        close_time = time(22, 0)


def _make_client(store):
    app = FastAPI()
    app.include_router(router)
    app.state.cfg = _StubCfg
    app.state.store = store
    return TestClient(app)


def test_api_version_combines_counter_and_db_component():
    v = _make_client(_CounterStore()).get("/api/version").json()["v"]
    assert v == 3 + 1788157404032


def test_api_version_falls_back_to_counter_without_db_component():
    v = _make_client(_CounterOnlyStore()).get("/api/version").json()["v"]
    assert v == 3
