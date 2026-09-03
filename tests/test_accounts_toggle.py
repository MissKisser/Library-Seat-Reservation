"""账号启用/禁用（toggle）与删除守卫的路由回归测试。

覆盖:
  - toggle 禁用: 未提交任务作废、在途真预约保留、通知与审计落库、cookie 保留
  - toggle 启用: 回 active、冲突检测提示
  - 容量试算不足时需 confirm=1 二次确认
  - check 端点字段
  - delete 守卫: 有在途真预约须 confirm_force=1
"""
import asyncio
from datetime import time, timedelta

from fastapi.testclient import TestClient

from seatbot.config import load_config
from seatbot.models import Account, Task, TaskStatus
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore
from seatbot.utils.timeutil import today_cst
from seatbot.web.app import make_app

_AUTH = {"Host": "127.0.0.1:8080", "X-Auth-Token": "test-token"}

_MATRIX = {w: ["09:00-11:00"] for w in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


def _client(tmp_path, accounts, tasks=(), desired=None):
    cfg = load_config("config.test.yaml")
    cfg.runtime.db_path = str(tmp_path / "toggle.db")
    cfg.runtime.web_token = "test-token"
    store = StateStore(cfg.runtime.db_path)

    async def seed():
        await store.init()
        await store.add_target_seat("104")
        await store.set_target_seat_desired(
            "104", desired or ["09:00-11:00"])
        for acc in accounts:
            await store.upsert_account(acc)
        for t in tasks:
            await store.add_task(t)
    asyncio.run(seed())

    sched = Scheduler(cfg, store)
    app = make_app(cfg, store, sched)
    return TestClient(app, base_url="http://127.0.0.1:8080"), store


def _acc(acc_id, matrix=None):
    return Account(
        id=acc_id, phone="13800000000", password="x", slots=[],
        seat_slots={"104": dict(matrix)} if matrix else None,
    )


def _task(acc_id, status, day_offset_days=0, start_hour=17):
    day = today_cst() + timedelta(days=day_offset_days)
    return Task(id=None, account_id=acc_id, day=day,
                start_time=time(start_hour, 0), end_time=time(start_hour + 2, 0),
                seat_num="104", status=status, reserve_id=1001)


def test_toggle_disable_voids_pending_keeps_inflight(tmp_path):
    """禁用：PENDING 作废、在途保留、cookie 保留、通知落库、状态 inactive。"""
    c, store = _client(
        tmp_path,
        [_acc("a1", _MATRIX), _acc("a2")],
        tasks=[_task("a1", TaskStatus.PENDING), _task("a1", TaskStatus.ACTIVE, start_hour=15)],
    )

    async def seed_cookie():
        await store.save_account_cookies("a1", {"_uid": "x"})
    asyncio.run(seed_cookie())

    r = c.post("/accounts/a1/toggle", headers=_AUTH, follow_redirects=False)
    assert r.status_code == 303, r.text

    async def verify():
        t_pending = [t for t in await store.list_tasks(account_id="a1")
                     if t.status == TaskStatus.FAILED]
        t_inflight = [t for t in await store.list_tasks(account_id="a1")
                      if t.status == TaskStatus.ACTIVE]
        cookies = await store.load_account_cookies("a1")
        notifs = await store.list_notifications()
        return t_pending, t_inflight, cookies, notifs
    t_pending, t_inflight, cookies, notifs = asyncio.run(verify())
    assert len(t_pending) == 1 and "账号已禁用" in (t_pending[0].last_error or "")
    assert len(t_inflight) == 1
    assert cookies == {"_uid": "x"}
    assert any("已禁用" in n["title"] for n in notifs)

    async def status():
        return (await store.get_account("a1")).status
    assert asyncio.run(status()) == "inactive"


def test_toggle_enable_reports_conflicts(tmp_path):
    """启用：回 active；与其他账号同座位同段重叠时给出重排提醒。"""
    overlapping = {w: ["09:00-11:00"] for w in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    c, store = _client(tmp_path, [_acc("a1", _MATRIX), _acc("a2", overlapping)])

    async def set_inactive():
        await store.set_account_status("a1", "inactive")
    asyncio.run(set_inactive())

    r = c.post("/accounts/a1/toggle", headers=_AUTH, follow_redirects=False)
    assert r.status_code == 303
    from urllib.parse import unquote
    assert "重叠" in unquote(r.headers["location"])

    async def status():
        return (await store.get_account("a1")).status
    assert asyncio.run(status()) == "active"


def test_toggle_disable_capacity_short_requires_confirm(tmp_path):
    """剩余账号接不住守护时段（仅剩 1 账号但每日限额摆不下全周）→ 409 需 confirm=1。"""
    # 单座位每天 3 段 2h：违反"每账号每座位每天 1 段"，a1 禁用后剩余池必接不住
    heavy = ["08:00-10:00", "10:00-12:00", "14:00-16:00"]
    c, store = _client(tmp_path, [_acc("a1", _MATRIX), _acc("a2")], desired=heavy)
    r = c.post("/accounts/a1/toggle", headers=_AUTH, follow_redirects=False)
    assert r.status_code == 409
    r2 = c.post("/accounts/a1/toggle", headers=_AUTH, data={"confirm": "1"},
                follow_redirects=False)
    assert r2.status_code == 303

    async def status():
        return (await store.get_account("a1")).status
    assert asyncio.run(status()) == "inactive"


def test_account_check_endpoint(tmp_path):
    """check 端点返回在途/未提交/绑定段数与容量结论。"""
    c, _store = _client(
        tmp_path,
        [_acc("a1", _MATRIX), _acc("a2")],
        tasks=[_task("a1", TaskStatus.SIGNED), _task("a1", TaskStatus.PENDING, start_hour=19)],
        desired=["08:00-10:00", "10:00-12:00", "14:00-16:00"],
    )
    r = c.get("/accounts/a1/check", headers=_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["inflight"] == 1 and body["pending"] == 1
    assert body["bound_segments"] == 7
    assert body["capacity_ok"] is False and body["unfillable"]


def test_delete_guard_requires_force_when_inflight(tmp_path):
    """删除有在途真预约的账号：409 需 confirm_force=1。"""
    c, store = _client(tmp_path, [_acc("a1")], tasks=[_task("a1", TaskStatus.ACTIVE)])
    r = c.post("/accounts/a1/delete", headers=_AUTH, follow_redirects=False)
    assert r.status_code == 409

    async def exists():
        return await store.get_account("a1")
    assert asyncio.run(exists()) is not None

    r2 = c.post("/accounts/a1/delete", headers=_AUTH, data={"confirm_force": "1"},
                follow_redirects=False)
    assert r2.status_code == 303
    assert asyncio.run(exists()) is None


def test_accounts_page_shows_status_badges(tmp_path):
    """账号页状态徽章：守护中/禁用中，禁用行带 data-status 供 verifyAll 跳过。"""
    c, store = _client(tmp_path, [_acc("a1", _MATRIX), _acc("a2")])

    async def set_inactive():
        await store.set_account_status("a2", "inactive")
    asyncio.run(set_inactive())

    r = c.get("/accounts", headers=_AUTH)
    assert r.status_code == 200
    assert "守护中" in r.text and "禁用中" in r.text
    assert 'data-status="inactive"' in r.text
    assert ">启用</button>" in r.text and ">禁用</button>" in r.text
