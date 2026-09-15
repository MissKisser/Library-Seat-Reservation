"""HA 闸门接入：调度器真实动作 + Web 直调端点。"""
from __future__ import annotations

import pytest

from seatbot.ha import HaRuntime


@pytest.fixture
async def store(tmp_path):
    from seatbot.store import StateStore
    s = StateStore(str(tmp_path / "ha-fence.db"))
    await s.init()
    yield s
    await s.close()


def _make_cfg():
    from seatbot.config import Config, LibraryConfig, RuntimeConfig
    return Config(
        library=LibraryConfig(room_id=11692, room_name="t"),
        runtime=RuntimeConfig(stagger_seconds=[0, 0]),
    )


async def _seed_account_task(store):
    from datetime import time
    from seatbot.models import Account, Task, TaskStatus
    from seatbot.utils.timeutil import today_cst
    await store.upsert_account(Account(id="acc-a", phone="1", password="p", slots=[]))
    d = today_cst()
    t = Task(id=None, account_id="acc-a", day=d,
             start_time=time(9, 0), end_time=time(11, 0),
             seat_num="001", status=TaskStatus.PENDING)
    t.id = await store.add_task(t)
    return await store.get_account("acc-a"), await store.get_task(t.id)


async def test_submit_blocked_when_standby(store, monkeypatch):
    from seatbot.models import TaskStatus
    from seatbot.scheduler import Scheduler

    calls: list = []

    class _FakeClient:
        def cookies(self):
            return {"_uid": "x"}

        async def submit_direct(self, *a, **kw):
            calls.append(("submit_direct", a, kw))
            return {"success": True, "reserve_id": 999}

        async def submit_via_page_rewrite(self, *a, **kw):
            calls.append(("page", a, kw))
            return {"success": True, "reserve_id": 998}

    monkeypatch.setattr(Scheduler, "_client_for", lambda self, acc: _FakeClient())
    sched = Scheduler(_make_cfg(), store)
    rt = HaRuntime()
    rt.mode = "backup"
    rt.backup_state = "standby"
    sched.ha = rt

    acc, t = await _seed_account_task(store)
    await sched._run_submit(acc, t)
    assert calls == []
    assert (await store.get_task(t.id)).status == TaskStatus.PENDING


async def test_sign_blocked_when_suspended(store, monkeypatch):
    from seatbot.scheduler import Scheduler

    calls: list = []

    class _FakeClient:
        def cookies(self):
            return {"_uid": "x"}

        async def sign(self, rid):
            calls.append(("sign", rid))
            return {"success": True}

    monkeypatch.setattr(Scheduler, "_client_for", lambda self, acc: _FakeClient())
    sched = Scheduler(_make_cfg(), store)
    rt = HaRuntime()
    rt.mode = "primary"
    rt.primary_state = "suspended"
    sched.ha = rt

    acc, t = await _seed_account_task(store)
    await store.update_task_status(t.id, __import__("seatbot.models", fromlist=["TaskStatus"]).TaskStatus.ACTIVE, reserve_id=7)
    loaded = await store.get_task(t.id)
    await sched._run_sign(acc, loaded)
    assert calls == []


async def test_leave_blocked_when_backup_failback_pending(store, monkeypatch):
    from seatbot.models import TaskStatus
    from seatbot.scheduler import Scheduler

    calls: list = []

    class _FakeClient:
        def cookies(self):
            return {"_uid": "x"}

        async def signback(self, rid):
            calls.append(("signback", rid))
            return {"success": True}

    monkeypatch.setattr(Scheduler, "_client_for", lambda self, acc: _FakeClient())
    sched = Scheduler(_make_cfg(), store)
    rt = HaRuntime()
    rt.mode = "backup"
    rt.backup_state = "failback_pending"
    sched.ha = rt

    acc, t = await _seed_account_task(store)
    await store.update_task_status(t.id, TaskStatus.SIGNED, reserve_id=8)
    loaded = await store.get_task(t.id)
    await sched._run_leave(acc, loaded)
    assert calls == []


async def test_supervision_blocked_when_suspended(store, monkeypatch):
    from seatbot.scheduler import Scheduler

    class _FakeClient:
        def cookies(self):
            return {"_uid": "x"}

        async def supervised_reservations(self):
            raise AssertionError("should not be called")

    monkeypatch.setattr(Scheduler, "_client_for", lambda self, acc: _FakeClient())
    sched = Scheduler(_make_cfg(), store)
    rt = HaRuntime()
    rt.mode = "primary"
    rt.primary_state = "suspended"
    sched.ha = rt

    acc, _ = await _seed_account_task(store)
    await sched._check_supervision(acc)


async def test_afternoon_bootstrap_skipped_when_suspended(store, monkeypatch):
    from seatbot.scheduler import Scheduler

    class _FakeClient:
        def cookies(self):
            return {"_uid": "x"}

        async def submit_direct(self, *a, **kw):
            raise AssertionError("must not submit")

        async def submit_via_page_rewrite(self, *a, **kw):
            raise AssertionError("must not submit")

    monkeypatch.setattr(Scheduler, "_client_for", lambda self, acc: _FakeClient())
    sched = Scheduler(_make_cfg(), store)
    rt = HaRuntime()
    rt.mode = "primary"
    rt.primary_state = "suspended"
    sched.ha = rt

    from datetime import time, timedelta
    from seatbot.models import Account, TaskStatus
    from seatbot.utils.timeutil import today_cst
    await store.upsert_account(Account(id="acc-a", phone="1", password="p", slots=[]))
    d = today_cst() + timedelta(days=1)
    # 没有 task 存在；即使 bootstrap 仍要生成任务，但 submit 必须跳过

    await sched._afternoon_bootstrap_locked()
    tasks = await store.list_tasks(day=d)
    # 即使生成，因闸门拦下 submit → tasks 应都仍为 PENDING
    assert all(t.status == TaskStatus.PENDING for t in tasks) if tasks else True
    assert sched.ha.primary_state == "suspended"


async def test_submit_passes_when_active_primary(store, monkeypatch):
    """sanity：active 主力下闸门不应误拦。"""
    from datetime import time
    from seatbot.models import Task, TaskStatus
    from seatbot.scheduler import Scheduler
    from seatbot.utils.timeutil import today_cst

    calls: list = []

    class _FakeClient:
        def cookies(self):
            return {"_uid": "x"}

        async def submit_direct(self, *a, **kw):
            calls.append("submit")
            return {"success": True, "reserve_id": 100}

        async def submit_via_page_rewrite(self, *a, **kw):
            calls.append("page")
            return {"success": True, "reserve_id": 101}

    monkeypatch.setattr(Scheduler, "_client_for", lambda self, acc: _FakeClient())
    sched = Scheduler(_make_cfg(), store)
    rt = HaRuntime()
    rt.mode = "primary"
    rt.primary_state = "active"
    sched.ha = rt

    from seatbot.models import Account
    await store.upsert_account(Account(id="acc-a", phone="1", password="p", slots=[]))
    t = Task(id=None, account_id="acc-a", day=today_cst(),
             start_time=time(9, 0), end_time=time(11, 0),
             seat_num="001", status=TaskStatus.PENDING)
    t.id = await store.add_task(t)
    acc = await store.get_account("acc-a")
    loaded = await store.get_task(t.id)
    await sched._run_submit(acc, loaded)
    assert "submit" in calls or "page" in calls


async def test_web_task_sign_returns_409_when_backup_standby(tmp_path):
    """Web 端 task_sign 在 backup/standby 时必须 409。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from seatbot.ha import HaRuntime
    from seatbot.web.routes import router

    app = FastAPI()
    app.include_router(router)
    rt = HaRuntime()
    rt.mode = "backup"
    rt.backup_state = "standby"
    app.state.ha = rt

    class _S:
        async def get_task(self, tid):
            return None

        async def get_account(self, acc_id):
            return None

        async def add_notification(self, *a, **kw):
            return None

    app.state.store = _S()
    app.state.sched = type("X", (), {"ha": rt})()

    client = TestClient(app)
    # 任何已有 token 都不存在；CSRF 也会拦截。先确保 409 来自 ha 闸门：
    # PanelAuth 在 test_app 里关闭 web_token 且不做 CSRF 校验；这里直接调端点
    r = client.post("/tasks/1/sign", headers={"Host": "testserver"})
    assert r.status_code == 409


def _make_fencing_app(store, rt):
    from seatbot.config import Config, LibraryConfig, RuntimeConfig
    from seatbot.scheduler import Scheduler
    from seatbot.web.app import make_app

    cfg = Config(
        library=LibraryConfig(room_id=0, room_name="t"),
        runtime=RuntimeConfig(stagger_seconds=[0, 0], web_token=""),
    )
    sched = Scheduler(cfg, store)
    sched.ha = rt
    sched._clients = {}
    return make_app(cfg, store, sched)


async def test_standby_blocks_mutations_except_settings(store):
    """备用待命期：写请求被真实挂载的 HaWriteGuardMiddleware 拦下；/settings 豁免；/settings/reset 必须 409。"""
    from fastapi.testclient import TestClient

    from seatbot.ha import HaConfig, HaRuntime

    rt = HaRuntime()
    rt.mode = "backup"
    rt.backup_state = "standby"
    rt.cfg = HaConfig(
        mode="backup", key="K", instance_id="I", peer_url="",
        heartbeat_interval=15, lease_ttl=90, activation_buffer=60,
        snapshot_interval=300, failback_grace=180,
    )

    app = _make_fencing_app(store, rt)
    client = TestClient(app)
    # 1) API 路径 → 409 JSON
    r = client.post("/api/notifications/dismiss-all",
                    headers={"X-HA-Key": "K", "Host": "testserver"},
                    follow_redirects=False)
    assert r.status_code == 409
    assert "备用" in r.json().get("detail", "") or "写入" in r.json().get("detail", "")
    # 2) 表单路径 → 303 重定向回 referer 带 ha_readonly=1
    r_form = client.post("/accounts",
                         headers={"X-HA-Key": "K", "Host": "testserver",
                                  "Referer": "/bindings"},
                         follow_redirects=False)
    assert r_form.status_code == 303
    assert "ha_readonly=1" in r_form.headers.get("location", "")
    # 3) /api/ha/* 端点正常通行（前置已豁免 PanelAuth）
    r2 = client.get("/api/ha/status", headers={"X-HA-Key": "K"})
    assert r2.status_code == 200
    # 4) GET 不拦
    r3 = client.get("/")
    assert r3.status_code in (200, 404, 307)  # 不 409
    # 5) /settings/reset 待命期拦截 409
    r5 = client.post("/settings/reset", headers={"X-HA-Key": "K", "Host": "testserver"},
                     follow_redirects=False)
    assert r5.status_code == 409
    # 6) /settings 写豁免
    r4 = client.post("/settings", data={"ha.mode": "backup", "ha.peer_url": "http://peer", "ha.key": "K"},
                     headers={"X-HA-Key": "K", "Host": "testserver"},
                     follow_redirects=False)
    assert r4.status_code != 409


async def test_active_backup_allows_writes(store):
    """备用已激活（active）→ 真实挂载的写保护不拦。"""
    from fastapi.testclient import TestClient

    from seatbot.ha import HaConfig, HaRuntime

    rt = HaRuntime()
    rt.mode = "backup"
    rt.backup_state = "active"
    rt.cfg = HaConfig(
        mode="backup", key="K", instance_id="I", peer_url="",
        heartbeat_interval=15, lease_ttl=90, activation_buffer=60,
        snapshot_interval=300, failback_grace=180,
    )

    app = _make_fencing_app(store, rt)
    client = TestClient(app)
    # active 时 POST 不被 HaWriteGuardMiddleware 拦；可能因别的原因非 200，但不应是 409
    r = client.post("/accounts", headers={"Host": "testserver"}, follow_redirects=False)
    assert r.status_code != 409