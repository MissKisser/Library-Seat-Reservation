"""Scheduler 核心行为测试 (v0.6 修复的回归守卫)。

覆盖:
  - _afternoon_bootstrap 先为明天生成任务再提交 (P0-1: 旧代码只提交不生成)
  - _run_sign 幂等: 成功 → SIGNED; "不在签到时间内" → SIGNED 终止重试
  - 会话自愈: "您当前未登录" → reset + relogin + 重试一次
  - _run_leave: 无 reserve_id → FAILED (不再虚假 COMPLETE);
    服务端幂等终结消息 → COMPLETE; 其他失败 → 保持 ACTIVE 由 tick 重试
  - tick_account 只处理今天的任务 (14:00 后明日任务 ACTIVE 不再饿死当日 sign)
"""
from __future__ import annotations

from datetime import time, timedelta

import pytest

from seatbot.config import Config, LibraryConfig, RuntimeConfig
from seatbot.models import Account, Task, TaskStatus
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore
from seatbot.utils.timeutil import at_cst, now_cst, today_cst


class DummyClient:
    """离线替身: 记录调用, 按脚本返回响应。"""

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.calls: list[tuple[str, int]] = []
        self.logins = 0
        self.resets = 0
        self._has_cookies = True

    def cookies(self):
        return {"_uid": "x"} if self._has_cookies else {}

    def reset_session(self):
        self.resets += 1

    async def login(self, phone, password):
        self.logins += 1
        self._has_cookies = True

    async def _act(self, name, rid):
        self.calls.append((name, rid))
        return self._responses.pop(0) if self._responses else {"success": True}

    async def sign(self, rid):
        return await self._act("sign", rid)

    async def signback(self, rid):
        return await self._act("signback", rid)

    async def leave(self, rid):
        return await self._act("leave", rid)


def make_cfg() -> Config:
    return Config(
        library=LibraryConfig(room_id=11692, room_name="t"),
        runtime=RuntimeConfig(stagger_seconds=[0, 0]),
    )


GUARD_MATRIX = {
    "zhangsan": {"104": ["09:00-11:00"], "105": ["15:00-17:00"]},
    "lisi":   {"104": ["15:00-17:00"], "105": ["19:00-21:00"]},
    "wangwu":  {"104": ["19:00-21:00"], "105": ["09:00-11:00"]},
}


async def seed_guard_accounts(store: StateStore) -> None:
    for i, (acc_id, seat_slots) in enumerate(GUARD_MATRIX.items()):
        await store.upsert_account(Account(
            id=acc_id, phone=f"1380000000{i}", password="p",
            slots=[], bound_seats=list(seat_slots.keys()),
            seat_slots=seat_slots,
        ))
    await store.add_target_seat("104")
    await store.add_target_seat("105")


@pytest.fixture
async def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    await s.init()
    await seed_guard_accounts(s)
    yield s
    await s.close()


@pytest.fixture
def nosleep(monkeypatch):
    async def _sleep(_s):
        return
    monkeypatch.setattr("seatbot.scheduler.asyncio.sleep", _sleep)


async def fake_submit_ok(self, acc, t):
    await self.store.update_task_status(t.id, TaskStatus.ACTIVE, reserve_id=9000 + (t.id or 0))


# ---------- P0-1: afternoon bootstrap 生成 + 提交 ----------

async def test_afternoon_bootstrap_generates_and_submits(store, monkeypatch, nosleep):
    monkeypatch.setattr(Scheduler, "_run_submit", fake_submit_ok)
    sched = Scheduler(make_cfg(), store)
    await sched._afternoon_bootstrap()

    tomorrow = today_cst() + timedelta(days=1)
    tasks = await store.list_tasks(day=tomorrow)
    # 守护矩阵: 3 账号 × 2 段 = 恰好 6 条 (AGENTS.md 验证方法)
    assert len(tasks) == 6, [(t.account_id, t.seat_num) for t in tasks]
    assert all(t.status == TaskStatus.ACTIVE for t in tasks)
    assert all(t.reserve_id for t in tasks)

    # 幂等: 再跑一次不产生重复任务
    await sched._afternoon_bootstrap()
    tasks2 = await store.list_tasks(day=tomorrow)
    assert len(tasks2) == 6


# ---------- P0-2: sign 幂等 ----------

async def test_run_sign_success_marks_signed(store, monkeypatch):
    dummy = DummyClient([{"success": True}])
    monkeypatch.setattr(Scheduler, "_client_for", lambda self, acc: dummy)
    sched = Scheduler(make_cfg(), store)
    t = Task(id=None, account_id="zhangsan", day=today_cst(),
             start_time=time(9, 0), end_time=time(11, 0),
             seat_num="104", status=TaskStatus.ACTIVE, reserve_id=1)
    t.id = await store.add_task(t)
    await sched._run_sign(await store.get_account("zhangsan"), t)
    assert (await store.get_task(t.id)).status == TaskStatus.SIGNED
    assert dummy.calls == [("sign", 1)]


async def test_run_sign_window_closed_stops_retry(store, monkeypatch):
    # "不在签到时间内" (已签过/窗口过) → SIGNED, 不再每分钟重打 API
    # now 固定在时段结束之后，服务端仍拒签 → 判定"窗口已过"终止重试
    fixed = at_cst(today_cst(), time(12, 0))
    monkeypatch.setattr("seatbot.scheduler.now_cst", lambda: fixed)
    dummy = DummyClient([{"success": False, "msg": "不在签到时间内无法签到"}])
    monkeypatch.setattr(Scheduler, "_client_for", lambda self, acc: dummy)
    sched = Scheduler(make_cfg(), store)
    t = Task(id=None, account_id="zhangsan", day=today_cst(),
             start_time=time(9, 0), end_time=time(11, 0),
             seat_num="104", status=TaskStatus.ACTIVE, reserve_id=1)
    t.id = await store.add_task(t)
    await sched._run_sign(await store.get_account("zhangsan"), t)
    assert (await store.get_task(t.id)).status == TaskStatus.SIGNED


async def test_run_sign_other_failure_keeps_active(store, monkeypatch):
    dummy = DummyClient([{"success": False, "msg": "服务器开小差了"}])
    monkeypatch.setattr(Scheduler, "_client_for", lambda self, acc: dummy)
    sched = Scheduler(make_cfg(), store)
    t = Task(id=None, account_id="zhangsan", day=today_cst(),
             start_time=time(9, 0), end_time=time(11, 0),
             seat_num="104", status=TaskStatus.ACTIVE, reserve_id=1)
    t.id = await store.add_task(t)
    await sched._run_sign(await store.get_account("zhangsan"), t)
    assert (await store.get_task(t.id)).status == TaskStatus.ACTIVE


# ---------- P1-3: 未登录自愈 ----------

async def test_run_sign_relogin_on_not_logged_in(store, monkeypatch):
    dummy = DummyClient([
        {"success": False, "msg": "您当前未登录"},   # 第一次: 陈旧 cookie
        {"success": True},                            # relogin 后成功
    ])
    monkeypatch.setattr(Scheduler, "_client_for", lambda self, acc: dummy)
    sched = Scheduler(make_cfg(), store)
    t = Task(id=None, account_id="zhangsan", day=today_cst(),
             start_time=time(9, 0), end_time=time(11, 0),
             seat_num="104", status=TaskStatus.ACTIVE, reserve_id=7)
    t.id = await store.add_task(t)
    await sched._run_sign(await store.get_account("zhangsan"), t)
    assert dummy.resets == 1          # 清了陈旧 cookie
    assert dummy.logins == 1          # 重新登录
    assert len(dummy.calls) == 2      # sign 重试了一次
    assert (await store.get_task(t.id)).status == TaskStatus.SIGNED


# ---------- leave 终态语义 ----------

async def test_run_leave_without_reserve_marks_failed(store, monkeypatch):
    dummy = DummyClient([])
    monkeypatch.setattr(Scheduler, "_client_for", lambda self, acc: dummy)
    sched = Scheduler(make_cfg(), store)
    t = Task(id=None, account_id="zhangsan", day=today_cst(),
             start_time=time(9, 0), end_time=time(11, 0),
             seat_num="104", status=TaskStatus.SIGNED, reserve_id=None)
    t.id = await store.add_task(t)
    await sched._run_leave(await store.get_account("zhangsan"), t)
    after = await store.get_task(t.id)
    assert after.status == TaskStatus.FAILED
    assert "无预约号" in (after.last_error or "")


async def test_run_leave_idempotent_end_completes(store, monkeypatch):
    # 剩余充足 (≥20min) 时 signback 失败 → 回退暂离; 暂离报"剩余不足"类
    # 幂等消息 → 预约将在服务端自然终结, 收尾 COMPLETE 停止重试
    fixed = now_cst().replace(hour=10, minute=30, second=0, microsecond=0)
    monkeypatch.setattr("seatbot.scheduler.now_cst", lambda: fixed)
    dummy = DummyClient([
        {"success": False, "msg": "系统繁忙"},                      # signback 失败
        {"success": False, "msg": "剩余时长小于暂离时长，无法暂离"},  # 回退暂离 → 幂等收尾
    ])
    monkeypatch.setattr(Scheduler, "_client_for", lambda self, acc: dummy)
    sched = Scheduler(make_cfg(), store)
    t = Task(id=None, account_id="zhangsan", day=today_cst(),
             start_time=time(9, 0), end_time=time(11, 0),
             seat_num="104", status=TaskStatus.SIGNED, reserve_id=5)
    t.id = await store.add_task(t)
    await sched._run_leave(await store.get_account("zhangsan"), t)
    assert (await store.get_task(t.id)).status == TaskStatus.COMPLETE
    assert dummy.calls == [("signback", 5), ("leave", 5)]


async def test_run_leave_failure_keeps_active_for_retry(store, monkeypatch):
    # signback 与暂离通道双双失败 (非幂等消息) → 保持 ACTIVE, 下个 tick 重试
    fixed = now_cst().replace(hour=10, minute=30, second=0, microsecond=0)
    monkeypatch.setattr("seatbot.scheduler.now_cst", lambda: fixed)
    dummy = DummyClient([
        {"success": False, "msg": "网络异常"},
        {"success": False, "msg": "网络异常"},
    ])
    monkeypatch.setattr(Scheduler, "_client_for", lambda self, acc: dummy)
    sched = Scheduler(make_cfg(), store)
    t = Task(id=None, account_id="zhangsan", day=today_cst(),
             start_time=time(9, 0), end_time=time(11, 0),
             seat_num="104", status=TaskStatus.SIGNED, reserve_id=5)
    t.id = await store.add_task(t)
    await sched._run_leave(await store.get_account("zhangsan"), t)
    assert (await store.get_task(t.id)).status == TaskStatus.ACTIVE


async def test_run_leave_near_end_skips_leave_and_retries(store, monkeypatch):
    # 到点前 5 分钟签退 (RELAY_LEAD_SECONDS=300): signback 失败时剩余 <20min,
    # 不允许落回暂离通道 (必然失败且会误触发幂等收尾掐断重试),
    # 任务保持 ACTIVE 由下个 tick 重试 signback
    fixed = now_cst().replace(hour=10, minute=55, second=0, microsecond=0)
    monkeypatch.setattr("seatbot.scheduler.now_cst", lambda: fixed)
    dummy = DummyClient([{"success": False, "msg": "系统繁忙"}])
    monkeypatch.setattr(Scheduler, "_client_for", lambda self, acc: dummy)
    sched = Scheduler(make_cfg(), store)
    t = Task(id=None, account_id="zhangsan", day=today_cst(),
             start_time=time(9, 0), end_time=time(11, 0),
             seat_num="104", status=TaskStatus.SIGNED, reserve_id=5)
    t.id = await store.add_task(t)
    await sched._run_leave(await store.get_account("zhangsan"), t)
    assert (await store.get_task(t.id)).status == TaskStatus.ACTIVE
    assert dummy.calls == [("signback", 5)]   # 未落暂离通道, 留给下个 tick 重试


# ---------- tick 只看今天 (P0: 明日 ACTIVE 不再饿死当日操作) ----------

async def test_tick_handles_today_despite_tomorrow_active(store, monkeypatch):
    dummy = DummyClient([{"success": True}])
    monkeypatch.setattr(Scheduler, "_client_for", lambda self, acc: dummy)
    sched = Scheduler(make_cfg(), store)

    today = today_cst()
    now = now_cst()
    # 今天: 09:00-11:00 进行中未签 (构造一个包含当前时刻的时段)
    s = (now - timedelta(minutes=10)).time()
    e = (now + timedelta(minutes=30)).time()
    cur = Task(id=None, account_id="zhangsan", day=today,
               start_time=s, end_time=e, seat_num="104",
               status=TaskStatus.ACTIVE, reserve_id=11)
    cur.id = await store.add_task(cur)
    # 明天: 一条"更晚"的 ACTIVE 任务 (旧 find_active_task 会被它吸走)
    tmr = Task(id=None, account_id="zhangsan", day=today + timedelta(days=1),
               start_time=time(19, 0), end_time=time(21, 0), seat_num="104",
               status=TaskStatus.ACTIVE, reserve_id=12)
    tmr.id = await store.add_task(tmr)

    await sched.tick_account("zhangsan")
    assert (await store.get_task(cur.id)).status == TaskStatus.SIGNED
    assert (await store.get_task(tmr.id)).status == TaskStatus.ACTIVE
    # SIGNED 后再 tick 同一任务不会重复 sign
    n_calls = len(dummy.calls)
    await sched.tick_account("zhangsan")
    assert len(dummy.calls) == n_calls


# ---------- 当日补约（14:00 后当日 PENDING 低频自动补提交） ----------

async def test_today_backfill_submits_only_pending_not_started(store, monkeypatch, nosleep):
    sched = Scheduler(make_cfg(), store)
    now = now_cst().replace(hour=15, minute=0, second=0, microsecond=0)
    monkeypatch.setattr("seatbot.scheduler.now_cst", lambda: now)
    monkeypatch.setattr("seatbot.scheduler.at_cst", at_cst)
    today = today_cst()
    calls: list[int] = []

    async def fake_submit(self, acc, t):
        calls.append(t.id)
        await self.store.update_task_status(t.id, TaskStatus.ACTIVE, reserve_id=100 + t.id)

    monkeypatch.setattr(Scheduler, "_run_submit", fake_submit)
    # 可补：今天 PENDING 且 19:00 才结束
    ok = Task(id=None, account_id="zhangsan", day=today,
              start_time=time(17, 0), end_time=time(19, 0),
              seat_num="104", status=TaskStatus.PENDING)
    ok.id = await store.add_task(ok)
    # 不补：今天 PENDING 但时段已开始（进行中/已结束的段格子不可点，补了必被拒）
    past = Task(id=None, account_id="zhangsan", day=today,
                start_time=time(9, 0), end_time=time(10, 0),
                seat_num="105", status=TaskStatus.PENDING)
    past.id = await store.add_task(past)
    # 不补：FAILED（自动重试会追加违约记录，须人工确认）
    failed = Task(id=None, account_id="zhangsan", day=today,
                  start_time=time(17, 0), end_time=time(19, 0),
                  seat_num="104", status=TaskStatus.FAILED)
    failed.id = await store.add_task(failed)
    # 不补：明天的 PENDING（归 14:00 批量管）
    tmr = Task(id=None, account_id="zhangsan", day=today + timedelta(days=1),
               start_time=time(17, 0), end_time=time(19, 0),
               seat_num="104", status=TaskStatus.PENDING)
    tmr.id = await store.add_task(tmr)

    await sched._today_backfill_locked(now)
    assert calls == [ok.id]


async def test_today_backfill_silent_outside_window(store, monkeypatch, nosleep):
    sched = Scheduler(make_cfg(), store)
    calls: list[int] = []

    async def fake_submit(self, acc, t):
        calls.append(t.id)

    monkeypatch.setattr(Scheduler, "_run_submit", fake_submit)
    Task(id=None, account_id="zhangsan", day=today_cst(),
         start_time=time(17, 0), end_time=time(19, 0),
         seat_num="104", status=TaskStatus.PENDING)
    # 窗口未开（13:00）→ 不提交也不建账
    monkeypatch.setattr("seatbot.scheduler.now_cst",
                        lambda: now_cst().replace(hour=13, minute=0, second=0, microsecond=0))
    await sched.today_backfill_tick()
    assert calls == []


async def test_tick_drains_inactive_account(store, monkeypatch):
    """禁用账号排干：无在途任务→tick 零动作；有进行中 ACTIVE→照常签到。"""
    sched = Scheduler(make_cfg(), store)
    await store.set_account_status("zhangsan", "inactive")
    dummy_idle = DummyClient([])
    monkeypatch.setattr(Scheduler, "_client_for", lambda self, acc: dummy_idle)
    await sched.tick_account("zhangsan")
    assert dummy_idle.calls == []

    now = now_cst()
    t = Task(id=None, account_id="zhangsan", day=today_cst(),
             start_time=(now - timedelta(minutes=10)).time(),
             end_time=(now + timedelta(minutes=30)).time(),
             seat_num="104", status=TaskStatus.ACTIVE, reserve_id=21)
    t.id = await store.add_task(t)
    dummy = DummyClient([{"success": True}])
    monkeypatch.setattr(Scheduler, "_client_for", lambda self, acc: dummy)
    await sched.tick_account("zhangsan")
    assert dummy.calls == [("sign", 21)]
    assert (await store.get_task(t.id)).status == TaskStatus.SIGNED
