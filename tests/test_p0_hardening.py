"""上线加固 (4 个 P0) 的回归守卫。

覆盖:
  - 面板认证中间件: 令牌模式 / 仅回环模式 / Host 头剥离与白名单
  - backup_database: 落盘 / 同日跳过 / 保留策略
  - startup_reconcile: 瞬态任务 (submitting/leaving) 回退
  - startup_afternoon_catchup: 14:00 错过窗口的补跑边界
  - misfire_grace_time / coalesce 注册
  - 签到/签退失败连击告警与终态即告警
"""
from __future__ import annotations

import sqlite3
from datetime import time, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from seatbot.config import Config, LibraryConfig, RuntimeConfig
from seatbot.models import Task, TaskStatus
from seatbot.scheduler import Scheduler
from seatbot.store import backup_database
from seatbot.utils.timeutil import at_cst, today_cst
from seatbot.web.app import (
    _host_without_port,
    auth_decision,
    csrf_decision,
    make_app,
)

from tests.test_scheduler_core import (
    DummyClient,
    make_cfg,
    store,  # noqa: F401  (fixture 复用)
    nosleep,  # noqa: F401  (fixture 复用)
)


# ---------- P0-1 面板认证 ----------

ALLOWED = {"localhost", "127.0.0.1", "::1"}


def test_host_without_port():
    assert _host_without_port("127.0.0.1:8080") == "127.0.0.1"
    assert _host_without_port("[::1]:8080") == "::1"
    assert _host_without_port("localhost") == "localhost"
    assert _host_without_port("") == ""


def test_auth_decision_token_mode():
    ok, code, _, set_cookie = auth_decision(
        web_token="s3cret", client_host="10.0.0.9", host_header="10.0.0.9:8080",
        presented="s3cret", allowed_hosts=ALLOWED,
    )
    assert ok and set_cookie
    ok, code, _, _ = auth_decision(
        web_token="s3cret", client_host="10.0.0.9", host_header="10.0.0.9:8080",
        presented=None, allowed_hosts=ALLOWED,
    )
    assert not ok and code == 401
    ok, code, _, _ = auth_decision(
        web_token="s3cret", client_host="127.0.0.1", host_header="127.0.0.1:8080",
        presented="wrong", allowed_hosts=ALLOWED,
    )
    assert not ok and code == 401


def test_csrf_decision_writes_only():
    # 非 write 方法一律放行
    ok, _ = csrf_decision(method="GET", origin="https://evil.com",
                          referer=None, host_header="127.0.0.1:8080")
    assert ok


def test_csrf_cross_origin_rejected():
    # 跨站网页向本机面板提交写请求：Origin 与 Host 不同源 → 拒绝（BR-001）
    ok, reason = csrf_decision(method="POST", origin="https://evil.com",
                               referer=None, host_header="127.0.0.1:8080")
    assert not ok and "cross-origin" in reason
    # Referer 回退同样参与判定
    ok, _ = csrf_decision(method="POST", origin=None,
                          referer="https://evil.com/attack", host_header="127.0.0.1:8080")
    assert not ok


def test_csrf_same_origin_and_missing_origin_pass():
    # 同源 POST 放行
    ok, _ = csrf_decision(method="POST", origin="http://127.0.0.1:8080",
                          referer=None, host_header="127.0.0.1:8080")
    assert ok
    # 无 Origin/Referer（curl / 服务间调用）交由认证层把守，放行
    ok, _ = csrf_decision(method="POST", origin=None, referer=None,
                          host_header="127.0.0.1:8080")
    assert ok
    # 同 IP 不同端口视为不同源
    ok, _ = csrf_decision(method="POST", origin="http://127.0.0.1:9999",
                          referer=None, host_header="127.0.0.1:8080")
    assert not ok


def test_auth_decision_loopback_mode():
    ok, _, _, _ = auth_decision(
        web_token="", client_host="127.0.0.1", host_header="127.0.0.1:8080",
        presented=None, allowed_hosts=ALLOWED,
    )
    assert ok
    ok, code, _, _ = auth_decision(
        web_token="", client_host="10.0.0.9", host_header="127.0.0.1:8080",
        presented=None, allowed_hosts=ALLOWED,
    )
    assert not ok and code == 403
    ok, code, _, _ = auth_decision(
        web_token="", client_host=None, host_header="127.0.0.1:8080",
        presented=None, allowed_hosts=ALLOWED,
    )
    assert not ok and code == 403
    # Host 头不在白名单 (DNS rebinding 防护) 优先拒绝
    ok, code, _, _ = auth_decision(
        web_token="", client_host="127.0.0.1", host_header="evil.example.com",
        presented=None, allowed_hosts=ALLOWED,
    )
    assert not ok and code == 400


def _panel_app(token: str, host: str = "127.0.0.1") -> FastAPI:
    cfg = Config(
        library=LibraryConfig(room_id=1, room_name="t"),
        runtime=RuntimeConfig(web_token=token, web_host=host),
    )
    return make_app(cfg, None, None)


def test_middleware_token_mode_enforced():
    client = TestClient(_panel_app("s3cret"))
    assert client.get("/static/style.css").status_code == 401
    assert client.get("/static/style.css?token=wrong").status_code == 401
    resp = client.get("/static/style.css?token=s3cret")
    assert resp.status_code == 200
    assert "seatbot_token" in resp.headers.get("set-cookie", "")
    # cookie 已种下, 后续请求免带令牌
    assert client.get("/static/style.css").status_code == 200
    # 全新会话 (无 cookie 无令牌, 模拟跨站来源) 的 POST 被拒
    fresh = TestClient(_panel_app("s3cret"))
    assert fresh.post("/targets/replace", data={}).status_code == 401


def test_middleware_loopback_mode_rejects_nonlocal_client():
    # TestClient 的来源是 "testserver" (非回环): Host 白名单或回环校验必拦其一
    client = TestClient(_panel_app(""))
    assert client.get("/static/style.css").status_code in (400, 403)


# ---------- P0-2 数据库备份 ----------

def _make_db(path) -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE x(a)")
    con.execute("INSERT INTO x VALUES (1)")
    con.commit()
    con.close()


def test_backup_creates_and_same_day_skip(tmp_path):
    src = tmp_path / "src.db"
    _make_db(src)
    dest = backup_database(str(src), str(tmp_path / "bak"))
    assert dest is not None and dest.exists()
    con = sqlite3.connect(str(dest))
    assert con.execute("SELECT count(*) FROM x").fetchone()[0] == 1
    con.close()
    assert backup_database(str(src), str(tmp_path / "bak")) is None


def test_backup_retention_prunes_old(tmp_path):
    src = tmp_path / "src.db"
    _make_db(src)
    bdir = tmp_path / "bak"
    bdir.mkdir()
    for i in range(1, 6):
        (bdir / f"seatbot-2020010{i}.db").write_bytes(b"old")
    dest = backup_database(str(src), str(bdir), keep=2)
    files = sorted(bdir.glob("seatbot-*.db"))
    assert len(files) == 2
    assert files[-1] == dest


def test_backup_missing_db_returns_none(tmp_path):
    assert backup_database(str(tmp_path / "nope.db"), str(tmp_path / "bak")) is None


# ---------- P0-3 启动对账与补跑 ----------

async def _add_task(store, **kw) -> int:
    # 默认键逐次错开：活跃态 (账号,日,座位,开始) 有库级唯一索引，fixture 同键多行会 IntegrityError
    seq = _add_task.seq = getattr(_add_task, "seq", 0) + 1
    defaults = dict(
        id=None,
        account_id="zhangsan", seat_num="001", day=today_cst(),
        start_time=time(9, 0 + (seq - 1) * 2), end_time=time(11, 0 + (seq - 1) * 2),
        status=TaskStatus.ACTIVE,
    )
    defaults.update(kw)
    return await store.add_task(Task(**defaults))


async def test_startup_reconcile_resets_transients(store):
    t1 = await _add_task(store, status=TaskStatus.SUBMITTING)
    t2 = await _add_task(store, status=TaskStatus.LEAVING)
    t3 = await _add_task(store, status=TaskStatus.SIGNED)
    sched = Scheduler(make_cfg(), store)
    await sched.startup_reconcile()
    assert (await store.get_task(t1)).status == TaskStatus.PENDING
    assert (await store.get_task(t2)).status == TaskStatus.ACTIVE
    assert (await store.get_task(t3)).status == TaskStatus.SIGNED


async def test_catchup_guard_windows(store, monkeypatch):
    """13:00 与 14:00–14:05 保护窗口内不补跑 (避免与常驻 cron 双跑)。"""
    sched = Scheduler(make_cfg(), store)
    called: list[int] = []

    async def fake_submit(self, acc, t):
        called.append(t.id)

    monkeypatch.setattr(Scheduler, "_run_submit", fake_submit)
    tomorrow = today_cst() + timedelta(days=1)
    await _add_task(store, day=tomorrow, status=TaskStatus.PENDING)
    monkeypatch.setattr("seatbot.scheduler.now_cst", lambda: at_cst(today_cst(), time(13, 0)))
    await sched.startup_afternoon_catchup()
    assert called == []
    monkeypatch.setattr("seatbot.scheduler.now_cst", lambda: at_cst(today_cst(), time(14, 3)))
    await sched.startup_afternoon_catchup()
    assert called == []


async def test_catchup_submits_pending_after_window(store, monkeypatch):
    sched = Scheduler(make_cfg(), store)
    called: list[int] = []

    async def fake_submit(self, acc, t):
        called.append(t.id)

    monkeypatch.setattr(Scheduler, "_run_submit", fake_submit)
    tomorrow = today_cst() + timedelta(days=1)
    t1 = await _add_task(store, day=tomorrow, status=TaskStatus.PENDING)
    t2 = await _add_task(store, day=tomorrow, status=TaskStatus.FAILED)
    await _add_task(store, day=tomorrow, status=TaskStatus.ACTIVE, reserve_id=9)
    monkeypatch.setattr("seatbot.scheduler.now_cst", lambda: at_cst(today_cst(), time(14, 30)))
    await sched.startup_afternoon_catchup()
    assert called == [t1]           # 只补 PENDING
    assert (await store.get_task(t2)).status == TaskStatus.FAILED  # FAILED 不自动重试


async def test_catchup_no_pending_noop(store, monkeypatch):
    sched = Scheduler(make_cfg(), store)
    called: list[int] = []

    async def fake_submit(self, acc, t):
        called.append(t.id)

    monkeypatch.setattr(Scheduler, "_run_submit", fake_submit)
    tomorrow = today_cst() + timedelta(days=1)
    await _add_task(store, day=tomorrow, status=TaskStatus.ACTIVE, reserve_id=9)
    monkeypatch.setattr("seatbot.scheduler.now_cst", lambda: at_cst(today_cst(), time(14, 30)))
    await sched.startup_afternoon_catchup()
    assert called == []


async def test_catchup_generates_when_tomorrow_empty(store, monkeypatch, nosleep):
    """明天一条任务都没有 (14:00 批量从未跑) → 生成并全部提交。"""
    sched = Scheduler(make_cfg(), store)
    called: list[int] = []

    async def fake_submit(self, acc, t):
        called.append(t.id)

    monkeypatch.setattr(Scheduler, "_run_submit", fake_submit)
    monkeypatch.setattr("seatbot.scheduler.now_cst", lambda: at_cst(today_cst(), time(15, 0)))
    await sched.startup_afternoon_catchup()
    tomorrow = today_cst() + timedelta(days=1)
    tasks = await store.list_tasks(day=tomorrow)
    assert tasks, "应为明天生成任务"
    assert len(called) == len(tasks)


async def test_misfire_grace_configured(store):
    sched = Scheduler(make_cfg(), store)
    sched.start()
    try:
        job = sched.scheduler.get_job("afternoon_bootstrap")
        assert job is not None
        assert job.misfire_grace_time == 3600
        assert job.coalesce is True
        nd = sched.scheduler.get_job("new_day_bootstrap")
        assert nd.misfire_grace_time == 600
    finally:
        await sched.shutdown()


# ---------- P0-4 失败告警 ----------

async def test_fail_streak_notify_at_3_and_every_10(store):
    sched = Scheduler(make_cfg(), store)
    t = Task(
        id=77, account_id="zhangsan", seat_num="104", day=today_cst(),
        start_time=time(9, 0), end_time=time(11, 0), status=TaskStatus.ACTIVE,
    )
    for _ in range(2):
        await sched._track_signleave_failure(t, "签到", "r")
    assert len(await store.list_notifications(limit=10)) == 0
    await sched._track_signleave_failure(t, "签到", "r")  # 第 3 次 → 告警
    notes = await store.list_notifications(limit=10)
    assert len(notes) == 1
    for _ in range(6):
        await sched._track_signleave_failure(t, "签到", "r")
    assert len(await store.list_notifications(limit=10)) == 1  # 4..9 次不重复告警
    await sched._track_signleave_failure(t, "签到", "r")  # 第 10 次 → 再告警
    assert len(await store.list_notifications(limit=10)) == 2
    sched._clear_fail_streak(77)
    await sched._track_signleave_failure(t, "签到", "r")
    await sched._track_signleave_failure(t, "签到", "r")
    assert len(await store.list_notifications(limit=10)) == 2  # 清零后重新计连击


async def test_sign_missing_notifies_and_fails(store, nosleep):
    sched = Scheduler(make_cfg(), store)
    acc = await store.get_account("zhangsan")
    tid = await _add_task(store, status=TaskStatus.ACTIVE, reserve_id=123)
    t = await store.get_task(tid)
    sched._clients["zhangsan"] = DummyClient([{"success": False, "msg": "预约不存在"}])
    await sched._run_sign(acc, t)
    after = await store.get_task(tid)
    assert after.status == TaskStatus.FAILED
    notes = await store.list_notifications(limit=10)
    assert any("签到终止" in n["title"] for n in notes)
    assert sched._fail_streak.get(tid) is None  # 终态已清连击
