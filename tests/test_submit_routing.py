"""_run_submit 日期分流测试 (2026-08-26 跨天修复 B1 页面内改写通道)。

覆盖:
  - 未来日期任务 → submit_via_page_rewrite, 绝不 submit_in_browser (会错约当天)
  - 今天任务 → submit_in_browser (浏览器通道)
  - direct_submit_enabled=False + 未来任务 → 直接 FAILED, 两条通道都不走
  - 改写通道成功但占用核验为空 → 保留 ACTIVE + ERROR 日志 (人工复核)
"""
from __future__ import annotations

from datetime import date, time, timedelta

import pytest

from seatbot.config import Config, LibraryConfig, RuntimeConfig
from seatbot.models import Task, TaskStatus
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore
from seatbot.utils.timeutil import today_cst


class RoutingClient:
    """替身: 记录走哪条通道, 可脚本化返回。"""

    def __init__(self, submit_result=None, used_times=None):
        self.calls: list[str] = []
        self.submit_result = submit_result or {
            "success": True, "reserve_id": 999001, "msg": None, "raw": {},
        }
        self.used_times = used_times if used_times is not None else []

    def cookies(self):
        return {"_uid": "x"}

    async def login(self, phone, password):
        pass

    async def submit_via_page_rewrite(self, **kw):
        self.calls.append("page_rewrite")
        return dict(self.submit_result)

    async def submit_in_browser(self, **kw):
        self.calls.append("browser")
        return dict(self.submit_result)

    async def get_used_times(self, room_id, seat_num, day):
        self.calls.append("getused")
        return list(self.used_times)


def make_cfg(**runtime_kw) -> Config:
    return Config(
        library=LibraryConfig(room_id=11692, room_name="t"),
        runtime=RuntimeConfig(stagger_seconds=[0, 0], **runtime_kw),
    )


async def _seed(tmp_path, day) -> tuple[StateStore, Task]:
    store = StateStore(str(tmp_path / "t.db"))
    await store.init()
    from tests.test_scheduler_core import seed_guard_accounts
    await seed_guard_accounts(store)
    t = Task(
        id=None, account_id="xiongjt", day=day,
        start_time=time(9, 0), end_time=time(11, 0), seat_num="104",
        status=TaskStatus.PENDING,
    )
    tid = await store.add_task(t)
    loaded = await store.get_task(tid)
    return store, loaded


@pytest.fixture
def tmp_store(tmp_path):
    return tmp_path


async def test_future_day_routes_to_direct(tmp_path):
    store, t = await _seed(tmp_path, today_cst() + timedelta(days=1))
    sched = Scheduler(make_cfg(), store)
    client = RoutingClient(used_times=[("09:00", "11:00")])
    sched._clients["xiongjt"] = client
    acc = await store.get_account("xiongjt")
    await sched._run_submit(acc, t)
    assert client.calls == ["page_rewrite", "getused"]
    after = await store.get_task(t.id)
    assert after.status == TaskStatus.ACTIVE and after.reserve_id == 999001


async def test_today_routes_to_browser(tmp_path):
    store, t = await _seed(tmp_path, today_cst())
    sched = Scheduler(make_cfg(), store)
    client = RoutingClient()
    sched._clients["xiongjt"] = client
    acc = await store.get_account("xiongjt")
    await sched._run_submit(acc, t)
    assert client.calls == ["browser"]
    after = await store.get_task(t.id)
    assert after.status == TaskStatus.ACTIVE


async def test_disabled_switch_fails_future_task_without_any_submit(tmp_path):
    store, t = await _seed(tmp_path, today_cst() + timedelta(days=1))
    sched = Scheduler(make_cfg(direct_submit_enabled=False), store)
    client = RoutingClient()
    sched._clients["xiongjt"] = client
    acc = await store.get_account("xiongjt")
    await sched._run_submit(acc, t)
    assert client.calls == []          # 两条通道都不许走 (回退浏览器=错约当天)
    after = await store.get_task(t.id)
    assert after.status == TaskStatus.FAILED


async def test_occupancy_check_empty_keeps_active_and_logs_error(tmp_path):
    store, t = await _seed(tmp_path, today_cst() + timedelta(days=1))
    sched = Scheduler(make_cfg(), store)
    client = RoutingClient(used_times=[])   # 核验为空
    sched._clients["xiongjt"] = client
    acc = await store.get_account("xiongjt")
    await sched._run_submit(acc, t)
    after = await store.get_task(t.id)
    # 预约号是服务端发的: 不标 FAILED (避免留下无人管理的真预约), 留 ACTIVE 人工复核
    assert after.status == TaskStatus.ACTIVE
    logs = await store.list_logs(limit=20)
    assert any("occupancy check EMPTY" in l.message for l in logs)
