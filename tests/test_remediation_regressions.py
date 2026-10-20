"""整改批次的回归守卫。

覆盖:
  - B2-01: 单段上限非法时立即报错, 不得死循环 (旧实现会卡死事件循环)
  - B3-01: HA 闸门自身异常时按 fail-closed 拦截本次真实动作
  - B3-02: 期望时段数据异常须显式反映在覆盖检查结果里; 日志日期过滤不得被丢弃;
            tick 作业注册失败必须告警 (日志 + 通知)
  - A-03: 通知 / 动作 / 日志的读序与写侧裁剪规则一致 (同毫秒读序确定)
  - D3-1: status 子命令输出手机号脱敏
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from seatbot.config import Config, LibraryConfig, RuntimeConfig
from seatbot.models import Account
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore
from seatbot.utils.timeutil import split_into_chunks
from seatbot.web.app import make_app

# ---------------------------------------------------------------- B2-01

def test_split_into_chunks_rejects_non_positive_max_hours_without_hanging():
    """单段上限 ≤ 0 必须立即报错；旧实现游标不前进，会死循环卡死事件循环。"""
    code = textwrap.dedent("""
        import sys
        from datetime import time
        from seatbot.utils.timeutil import split_into_chunks
        try:
            split_into_chunks((time(9, 0), time(11, 0)), 0.0)
        except ValueError as e:
            print("ValueError:", e)
            sys.exit(0)
        sys.exit(1)
    """)
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        timeout=60, check=False,
    )
    assert proc.returncode == 0, f"未按预期快速报错: {proc.stdout}{proc.stderr}"
    assert "ValueError" in proc.stdout


def test_split_into_chunks_rejects_negative_max_hours():
    with pytest.raises(ValueError):
        split_into_chunks((parse_t(9, 0), parse_t(11, 0)), -1.0)


def parse_t(h: int, m: int):
    """构造 time 对象（避免在模块级 import 冲突）。"""
    from datetime import time
    return time(h, m)


def test_routes_has_no_private_split_copy():
    """路由层不得再保留与 timeutil 漂移的私有切分副本。"""
    from seatbot.web import routes
    assert not hasattr(routes, "_split_range_ceiling")


# ---------------------------------------------------------------- B3-01

class _BoomHa:
    """模拟 HA 运行时自身故障（can_act 抛错）。"""

    def can_act(self) -> bool:
        raise RuntimeError("ha state unavailable")


class _AlwaysActHa:
    def can_act(self) -> bool:
        return True


class _LogStore:
    """只记录日志与通知的最小存储桩。"""

    def __init__(self):
        self.messages: list[tuple[str, str | None, str]] = []
        self.notifications: list[dict] = []

    async def log_message(self, level, account_id, message):
        self.messages.append((level, account_id, message))

    async def add_notification(self, title, body="", level="warn"):
        self.notifications.append({"title": title, "body": body, "level": level})

    async def get_settings_map(self):
        return {}

    async def list_accounts(self, include_inactive=True):
        return []


def _scheduler(store) -> Scheduler:
    cfg = Config(
        library=LibraryConfig(room_id=1, room_name="t"),
        runtime=RuntimeConfig(stagger_seconds=[0, 0]),
    )
    return Scheduler(cfg, store)


def test_ha_gate_blocks_when_gate_itself_raises():
    """闸门自身异常时必须拦截本次真实动作（fail-closed），不得静默放行。"""
    store = _LogStore()
    sched = _scheduler(store)
    sched.ha = _BoomHa()
    assert sched._ha_blocked("submit") is True
    assert sched._ha_blocked("sign") is True
    assert sched._ha_blocked("leave") is True


def test_ha_gate_allows_when_healthy():
    store = _LogStore()
    sched = _scheduler(store)
    sched.ha = _AlwaysActHa()
    assert sched._ha_blocked("submit") is False


# ---------------------------------------------------------------- B3-02

async def test_list_logs_invalid_day_does_not_drop_filter(tmp_path):
    """日期过滤解析失败时不得退化成全量返回，且必须留下 WARN 记录。"""
    store = StateStore(str(tmp_path / "logs.db"))
    await store.init()
    try:
        await store.log_message("INFO", None, "一条正常日志")
        rows = await store.list_logs(day="not-a-date")
        assert rows == []
        warned = await store.list_logs(level="WARN")
        assert any("日期过滤" in r.message for r in warned)
    finally:
        await store.close()


async def test_list_logs_valid_day_filters_rows(tmp_path):
    store = StateStore(str(tmp_path / "logs2.db"))
    await store.init()
    try:
        await store.log_message("INFO", None, "今天的日志")
        assert len(await store.list_logs(day="2026-01-01")) == 0
        assert len(await store.list_logs()) == 1
    finally:
        await store.close()


async def test_tick_job_registration_failure_warns_and_notifies(tmp_path):
    """tick 作业注册失败必须告警：落 WARN 日志 + 推通知，不得静默跳过。"""
    store = _LogStore()
    sched = _scheduler(store)

    def _boom_add_job(*a, **kw):
        raise RuntimeError("scheduler registry unavailable")

    sched.scheduler.add_job = _boom_add_job
    sched.store = store
    # 造一个启用中的账号
    store.list_accounts = _accounts_with_one()

    await sched.sync_jobs()

    assert any("注册 tick 作业" in m[2] for m in store.messages), store.messages
    assert any("调度作业注册失败" in n["title"] for n in store.notifications), store.notifications


async def test_tick_job_registration_failure_notifies_only_once(tmp_path):
    """同一作业持续失败时只通知一次，避免每轮刷屏。"""
    store = _LogStore()
    sched = _scheduler(store)
    sched.scheduler.add_job = lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError("scheduler registry unavailable"))
    store.list_accounts = _accounts_with_one()

    await sched.sync_jobs()
    await sched.sync_jobs()

    assert len([n for n in store.notifications if "调度作业注册失败" in n["title"]]) == 1


async def test_sync_jobs_refreshes_existing_tick_trigger():
    """已注册 tick 作业的触发器必须能真正刷新，不得因参数用错而恒定失败。"""
    store = _LogStore()
    sched = _scheduler(store)
    store.list_accounts = _accounts_with_one()
    sched.scheduler.start(paused=True)
    try:
        await sched.sync_jobs()
        job = sched.scheduler.get_job("tick_zhangsan")
        assert job is not None
        await sched.sync_jobs()
        job = sched.scheduler.get_job("tick_zhangsan")
        assert job is not None
        assert job.misfire_grace_time == sched.tick_misfire_grace_seconds
        assert not any("刷新 tick 作业" in m[2] for m in store.messages), store.messages
    finally:
        sched.scheduler.shutdown(wait=False)


def _accounts_with_one():
    async def _list(include_inactive=True):
        return [Account(id="zhangsan", phone="1", password="p", slots=[])]
    return _list


def test_coverage_reports_desired_slot_data_anomaly(tmp_path, monkeypatch):
    """期望时段解析失败时，覆盖检查必须显式反映数据异常，不得当作"无缺口"。"""
    import seatbot.web.routes as R

    cfg = Config(
        library=LibraryConfig(room_id=1, room_name="t"),
        runtime=RuntimeConfig(stagger_seconds=[0, 0], web_token="tok"),
    )
    store = StateStore(str(tmp_path / "cov.db"))
    asyncio.run(store.init())

    async def _seed():
        await store.add_target_seat("101", label="座位")
        await store.set_target_seat_desired("101", ["这不是一个合法时段"])
        await store.upsert_account(Account(
            id="zhangsan", phone="1", password="p", slots=[],
            seat_slots={"101": ["09:00-11:00"]},
        ))

    asyncio.run(_seed())

    async def _fake_fetch(request, store_, day, seat_nums, fresh=False):
        return [], None

    monkeypatch.setattr(R, "_fetch_others_occupied_cached", _fake_fetch)
    app = make_app(cfg, store, Scheduler(cfg, store))
    client = TestClient(app, base_url="http://127.0.0.1:8080")
    try:
        resp = client.get("/api/dashboard-data?token=tok")
        assert resp.status_code == 200
        body = resp.json()
        assert body["today"]["desired_errors"], "期望时段数据异常必须显式上报"
        assert body["today"]["gap_count"] > 0, "数据异常不得被当作无缺口"
    finally:
        client.close()
        asyncio.run(store.close())


# ---------------------------------------------------------------- A-03

async def test_list_notifications_ordering_is_deterministic(tmp_path, monkeypatch):
    """同一毫秒内的多条通知读序必须确定（与写侧裁剪规则一致）。"""
    monkeypatch.setattr("seatbot.store._time.time", lambda: 1000.0)
    store = StateStore(str(tmp_path / "notif.db"))
    await store.init()
    try:
        for i in range(5):
            await store.add_notification(f"n{i}")
        rows = await store.list_notifications(limit=10)
        assert [r["title"] for r in rows] == ["n4", "n3", "n2", "n1", "n0"]
    finally:
        await store.close()


async def test_list_actions_and_logs_ordering_is_deterministic(tmp_path, monkeypatch):
    monkeypatch.setattr("seatbot.store._time.time", lambda: 2000.0)
    store = StateStore(str(tmp_path / "act.db"))
    await store.init()
    try:
        for i in range(3):
            await store.log_action("a", "act", None, "r", True, f"m{i}")
            await store.log_message("INFO", "a", f"m{i}")
        actions = await store.list_actions(limit=10)
        assert [a.message for a in actions] == ["m2", "m1", "m0"]
        logs = await store.list_logs(limit=10)
        assert [l.message for l in logs] == ["m2", "m1", "m0"]
    finally:
        await store.close()


# ---------------------------------------------------------------- D3-1

def test_mask_phone_hides_middle_digits():
    from seatbot.__main__ import _mask_phone
    assert _mask_phone("13800000000") == "138****0000"
    assert "0000" not in _mask_phone("13800000000")[:7]
    assert _mask_phone("") == ""
    assert _mask_phone("123") == "***"


def test_status_command_output_masks_phone(tmp_path, capsys):
    """status 子命令不得打印完整明文手机号。"""
    cfg_text = textwrap.dedent(f"""
        library:
          room_id: 1
          room_name: "t"
          open_time: "08:00"
          close_time: "22:00"
          max_reserve_hours: 2.0
        accounts:
          - id: zhangsan
            phone: "13800000000"
            password: "secret"
            slots: full
        runtime:
          stagger_seconds: [0, 0]
          log_dir: {tmp_path / "logs"!s}
          db_path: {tmp_path / "status.db"!s}
          web_host: 127.0.0.1
          web_port: 8080
    """).strip()
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(cfg_text, encoding="utf-8")

    # 预置账号（status 只读库，不做 config 播种）
    async def _seed():
        s = StateStore(str(tmp_path / "status.db"))
        await s.init()
        try:
            await s.upsert_account(Account(
                id="zhangsan", phone="13800000000", password="secret", slots="full"))
        finally:
            await s.close()

    asyncio.run(_seed())

    from seatbot.__main__ import _cmd_status

    args = SimpleNamespace(config=str(cfg_file))
    assert asyncio.run(_cmd_status(args)) == 0
    out = capsys.readouterr().out
    assert "138****0000" in out
    assert "13800000000" not in out
