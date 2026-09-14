"""_run_submit 提交通道与策略分流测试。

覆盖:
  - 任务提交统一走直连四策略通道 (direct_first 默认直连优先, 成功即不再走改写通道)
  - 直连失败 → 回退 submit_via_page_rewrite; 仍"无格子"且无锚点 → FAILED
  - page_rewrite_only → 仅页面通道, 绝不直连; direct_only → 仅直连, 失败即 FAILED
  - 今天任务 → 统一走直连四策略通道 (默认优先直连提交)
  - 提交成功但占用核验为空 → 保留 ACTIVE + ERROR 日志 (人工复核)
"""
from __future__ import annotations

import time as _time
from datetime import time, timedelta

import pytest

from seatbot.config import Config, LibraryConfig, RuntimeConfig
from seatbot.models import Task, TaskStatus
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore
from seatbot.utils.timeutil import today_cst


class RoutingClient:
    """替身: 记录走哪条通道, 可分通道脚本化返回。"""

    def __init__(self, direct_result=None, rewrite_result=None, used_times=None):
        self.calls: list[str] = []
        default = {
            "success": True, "reserve_id": 999001, "msg": None, "raw": {},
        }
        self.direct_result = direct_result or {**default, "channel": "direct"}
        self.rewrite_result = rewrite_result or {
            **default, "channel": "page-rewrite",
        }
        self.used_times = used_times if used_times is not None else []
        self.logins = 0
        self.resets = 0

    def cookies(self):
        return {"_uid": "x"}

    def reset_session(self):
        self.resets += 1

    async def login(self, phone, password):
        self.logins += 1

    async def submit_direct(self, **kw):
        self.calls.append("direct")
        return dict(self.direct_result)

    async def submit_via_page_rewrite(self, **kw):
        self.calls.append("page_rewrite")
        return dict(self.rewrite_result)

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
        id=None, account_id="zhangsan", day=day,
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
    sched._clients["zhangsan"] = client
    acc = await store.get_account("zhangsan")
    await sched._run_submit(acc, t)
    assert client.calls == ["direct", "getused"]
    after = await store.get_task(t.id)
    assert after.status == TaskStatus.ACTIVE and after.reserve_id == 999001


async def test_direct_failure_falls_back_to_page_rewrite(tmp_path):
    store, t = await _seed(tmp_path, today_cst() + timedelta(days=1))
    sched = Scheduler(make_cfg(), store)
    client = RoutingClient(
        direct_result={
            "success": False, "reserve_id": None,
            "msg": "submit_enc seed not found on seat page",
            "raw": None, "channel": "direct",
        },
        used_times=[("09:00", "11:00")],
    )
    sched._clients["zhangsan"] = client
    acc = await store.get_account("zhangsan")
    await sched._run_submit(acc, t)
    assert client.calls == ["direct", "getused", "direct", "page_rewrite", "getused"]
    after = await store.get_task(t.id)
    assert after.status == TaskStatus.ACTIVE and after.reserve_id == 999001


async def test_direct_seed_not_found_retries_with_anchor_direct_success(tmp_path):
    store, t = await _seed(tmp_path, today_cst() + timedelta(days=1))
    sched = Scheduler(make_cfg(), store)
    client = RoutingClient(used_times=[("09:00", "11:00")])
    direct_calls = 0

    async def submit_direct(**kw):
        nonlocal direct_calls
        client.calls.append("direct")
        direct_calls += 1
        if direct_calls == 1:
            return {"success": False, "reserve_id": None, "msg": "submit_enc seed not found on seat page", "channel": "direct"}
        return {"success": True, "reserve_id": 888001, "msg": None, "channel": "direct"}

    client.submit_direct = submit_direct
    sched._clients["zhangsan"] = client
    acc = await store.get_account("zhangsan")
    await sched._run_submit(acc, t)
    assert client.calls == ["direct", "getused", "direct", "getused"]
    after = await store.get_task(t.id)
    assert after.status == TaskStatus.ACTIVE and after.reserve_id == 888001


async def test_page_rewrite_timeout_triggers_anchor_retry(tmp_path):
    store, t = await _seed(tmp_path, today_cst() + timedelta(days=1))
    sched = Scheduler(make_cfg(), store)
    rewrite_calls = 0

    async def submit_via_page_rewrite(**kw):
        nonlocal rewrite_calls
        client.calls.append("page_rewrite")
        rewrite_calls += 1
        if rewrite_calls == 1:
            return {"success": False, "reserve_id": None, "msg": "page load failed: Timeout 12000ms exceeded", "channel": "page-rewrite"}
        return {"success": True, "reserve_id": 777001, "msg": None, "channel": "page-rewrite"}

    client = RoutingClient(
        direct_result={"success": False, "reserve_id": None, "msg": "submit rejected", "channel": "direct"},
        used_times=[("09:00", "11:00")],
    )
    client.submit_via_page_rewrite = submit_via_page_rewrite
    sched._clients["zhangsan"] = client
    acc = await store.get_account("zhangsan")
    await sched._run_submit(acc, t)
    assert client.calls == ["direct", "page_rewrite", "getused", "page_rewrite", "getused"]
    after = await store.get_task(t.id)
    assert after.status == TaskStatus.ACTIVE and after.reserve_id == 777001


async def test_all_channels_fail_without_anchor_marks_failed(tmp_path):
    store, t = await _seed(tmp_path, today_cst() + timedelta(days=1))
    sched = Scheduler(make_cfg(), store)
    client = RoutingClient(
        direct_result={
            "success": False, "reserve_id": None, "msg": "submit rejected",
            "raw": None, "channel": "direct",
        },
        rewrite_result={
            "success": False, "reserve_id": None,
            "msg": "today-page has no selectable cell; cannot trigger form",
            "raw": None, "channel": "page-rewrite",
        },
        used_times=[],   # 锚点筛选: 无占用记录的候选一律不放行 → 无锚点
    )
    sched._clients["zhangsan"] = client
    acc = await store.get_account("zhangsan")
    await sched._run_submit(acc, t)
    assert client.calls[:2] == ["direct", "page_rewrite"]
    assert client.calls.count("page_rewrite") == 1   # 无锚点 → 不再三试
    after = await store.get_task(t.id)
    assert after.status == TaskStatus.FAILED
    assert "no selectable cell" in (after.last_error or "")


async def test_today_routes_to_direct(tmp_path):
    store, t = await _seed(tmp_path, today_cst())
    sched = Scheduler(make_cfg(), store)
    client = RoutingClient()
    sched._clients["zhangsan"] = client
    acc = await store.get_account("zhangsan")
    await sched._run_submit(acc, t)
    assert client.calls == ["direct"]
    after = await store.get_task(t.id)
    assert after.status == TaskStatus.ACTIVE and after.reserve_id == 999001

async def test_page_rewrite_only_never_touches_direct(tmp_path):
    store, t = await _seed(tmp_path, today_cst() + timedelta(days=1))
    sched = Scheduler(make_cfg(submit_strategy="page_rewrite_only"), store)
    client = RoutingClient(used_times=[("09:00", "11:00")])
    sched._clients["zhangsan"] = client
    acc = await store.get_account("zhangsan")
    await sched._run_submit(acc, t)
    assert client.calls == ["page_rewrite", "getused"]
    after = await store.get_task(t.id)
    assert after.status == TaskStatus.ACTIVE and after.reserve_id == 999001


async def test_direct_only_never_falls_back(tmp_path):
    store, t = await _seed(tmp_path, today_cst() + timedelta(days=1))
    sched = Scheduler(make_cfg(submit_strategy="direct_only"), store)
    client = RoutingClient(
        direct_result={
            "success": False, "reserve_id": None,
            "msg": "submit_enc seed not found on seat page",
            "raw": None, "channel": "direct",
        },
        used_times=[],   # 不触达核验
    )
    sched._clients["zhangsan"] = client
    acc = await store.get_account("zhangsan")
    await sched._run_submit(acc, t)
    assert client.calls == ["direct"]   # 失败即终, 不得回退任何通道
    after = await store.get_task(t.id)
    assert after.status == TaskStatus.FAILED


async def test_occupancy_check_empty_keeps_active_and_logs_error(tmp_path):
    store, t = await _seed(tmp_path, today_cst() + timedelta(days=1))
    sched = Scheduler(make_cfg(), store)
    client = RoutingClient(used_times=[])   # 核验为空
    sched._clients["zhangsan"] = client
    acc = await store.get_account("zhangsan")
    await sched._run_submit(acc, t)
    after = await store.get_task(t.id)
    # 预约号是服务端发的: 不标 FAILED (避免留下无人管理的真预约), 留 ACTIVE 人工复核
    assert after.status == TaskStatus.ACTIVE
    # 核验未反映时等待重查一次 (服务端落库延迟), 两次均未覆盖才告警
    assert client.calls.count("getused") == 2
    logs = await store.list_logs(limit=20)
    assert any("占用核验未反映" in l.message for l in logs)


async def test_submit_direct_relogin_in_cooldown_skips_login_and_falls_back(tmp_path):
    store, t = await _seed(tmp_path, today_cst() + timedelta(days=1))
    sched = Scheduler(make_cfg(), store)
    client = RoutingClient(
        direct_result={
            "success": False, "reserve_id": None,
            "msg": "您当前未登录", "raw": None, "channel": "direct",
        },
        rewrite_result={
            "success": True, "reserve_id": 999002,
            "msg": None, "raw": {}, "channel": "page-rewrite",
        },
        used_times=[("09:00", "11:00")],
    )
    sched._clients["zhangsan"] = client
    # 预置账号处于冷却期
    sched._relogin_cooldown["zhangsan"] = _time.monotonic() + 300.0

    acc = await store.get_account("zhangsan")
    await sched._run_submit(acc, t)

    # 处于冷却期内：未执行浏览器重登，直接走后续改写通道并成功
    assert client.logins == 0
    assert "page_rewrite" in client.calls
    after = await store.get_task(t.id)
    assert after.status == TaskStatus.ACTIVE and after.reserve_id == 999002


async def test_submit_direct_consecutive_relogin_failures_trigger_cooldown(tmp_path):
    store, t1 = await _seed(tmp_path, today_cst() + timedelta(days=1))
    sched = Scheduler(make_cfg(), store)
    client = RoutingClient(
        direct_result={
            "success": False, "reserve_id": None,
            "msg": "您当前未登录", "raw": None, "channel": "direct",
        },
        rewrite_result={
            "success": False, "reserve_id": None,
            "msg": "改写也失败", "raw": {}, "channel": "page-rewrite",
        },
    )
    sched._clients["zhangsan"] = client
    acc = await store.get_account("zhangsan")

    # 第 1 次提交：触发未登录重登 1 次，重试仍未登录
    await sched._run_submit(acc, t1)
    assert client.logins == 1
    assert sched._relogin_fail_count.get("zhangsan") == 1
    assert "zhangsan" not in sched._relogin_cooldown

    # 第 2 次提交：触发未登录重登第 2 次，仍未登录，进入 5 分钟冷却
    t2 = Task(
        id=None, account_id="zhangsan", day=today_cst() + timedelta(days=1),
        start_time=time(11, 0), end_time=time(13, 0), seat_num="104",
        status=TaskStatus.PENDING,
    )
    t2.id = await store.add_task(t2)
    await sched._run_submit(acc, t2)
    assert client.logins == 2
    assert sched._relogin_fail_count.get("zhangsan") == 2
    assert sched._relogin_cooldown.get("zhangsan", 0.0) > _time.monotonic()

    # 第 3 次提交：处于冷却期内，不再重登
    t3 = Task(
        id=None, account_id="zhangsan", day=today_cst() + timedelta(days=1),
        start_time=time(13, 0), end_time=time(15, 0), seat_num="104",
        status=TaskStatus.PENDING,
    )
    t3.id = await store.add_task(t3)
    await sched._run_submit(acc, t3)
    assert client.logins == 2


async def test_page_rewrite_only_timeout_triggers_anchor_retry(tmp_path):
    store, t = await _seed(tmp_path, today_cst() + timedelta(days=1))
    sched = Scheduler(make_cfg(submit_strategy="page_rewrite_only"), store)
    rewrite_calls = 0

    async def submit_via_page_rewrite(**kw):
        nonlocal rewrite_calls
        client.calls.append("page_rewrite")
        rewrite_calls += 1
        if rewrite_calls == 1:
            return {
                "success": False, "reserve_id": None,
                "msg": "page load failed: Timeout 12000ms exceeded",
                "channel": "page-rewrite",
            }
        return {"success": True, "reserve_id": 777002, "msg": None, "channel": "page-rewrite"}

    client = RoutingClient(used_times=[("09:00", "11:00")])
    client.submit_via_page_rewrite = submit_via_page_rewrite
    sched._clients["zhangsan"] = client
    acc = await store.get_account("zhangsan")
    await sched._run_submit(acc, t)

    assert client.calls == ["page_rewrite", "getused", "page_rewrite", "getused"]
    after = await store.get_task(t.id)
    assert after.status == TaskStatus.ACTIVE and after.reserve_id == 777002


