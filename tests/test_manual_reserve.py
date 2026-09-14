"""手动预约页（/manual）单测与集成互斥验证。

覆盖：
  - validate_manual_submission 九条规则逐条正反例
  - day_window_blocked 边界（明天 14:00 前后）
  - parse_manual_form 解析/校验
  - used_hours_for_account_day 累计（含 failed/complete 排除）
  - 路由：GET /manual 200、POST /manual/reserve 校验/成功/失败/IntegrityError
  - GET /api/manual/occupancy 400/502/200 三分支
  - /tasks/{id}/cancel 加 next 回跳（合法/防开放重定向）
  - 集成：manual 任务带 reserve_id → 托管/实况同步天然互斥（known 集合）
"""
from __future__ import annotations

import sqlite3
from datetime import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from seatbot.manual import (
    day_window_blocked,
    parse_manual_form,
    used_hours_for_account_day,
    validate_manual_submission,
)
from seatbot.models import (
    Account,
    SeatTarget,
    Task,
    TaskStatus,
    TASK_SOURCE_MANUAL,
)
from seatbot.reconcile import diff_user_reserved, parse_reservations, plan_adoption
from seatbot.utils.timeutil import at_cst, today_cst
from seatbot.utils.weekly import weekday_key
from seatbot.web.app import new_templates
from seatbot.web.routes import _safe_next, router


DAY = today_cst()
TOMORROW = DAY.fromordinal(DAY.toordinal() + 1)


# ---------- 纯函数：day_window_blocked ----------

def test_day_window_blocked_today_always_open():
    assert day_window_blocked(at_cst(DAY, time(0, 0)), DAY) is None
    assert day_window_blocked(at_cst(DAY, time(13, 59)), DAY) is None
    assert day_window_blocked(at_cst(DAY, time(23, 59)), DAY) is None


def test_day_window_blocked_tomorrow_before_14_blocked():
    msg = day_window_blocked(at_cst(DAY, time(13, 59)), TOMORROW)
    assert msg is not None
    assert "14:00" in msg or "14" in msg


def test_day_window_blocked_tomorrow_at_14_open():
    assert day_window_blocked(at_cst(DAY, time(14, 0)), TOMORROW) is None
    assert day_window_blocked(at_cst(DAY, time(18, 0)), TOMORROW) is None


def test_day_window_blocked_out_of_range_rejected():
    msg = day_window_blocked(at_cst(DAY, time(10, 0)),
                              DAY.fromordinal(DAY.toordinal() + 5))
    assert msg is not None and "今天" in msg or "明天" in msg


# ---------- 纯函数：parse_manual_form ----------

def test_parse_manual_form_success():
    d, s, e, sn = parse_manual_form(
        day_raw=DAY.isoformat(), start_raw="09:00", end_raw="11:00", seat_raw="42",
    )
    assert d == DAY
    assert s == time(9, 0) and e == time(11, 0)
    assert sn == "042"


def test_parse_manual_form_bad_date():
    d, _s, _e, reason = parse_manual_form(
        day_raw="bad", start_raw="09:00", end_raw="10:00", seat_raw="1",
    )
    assert d is None and "日期" in reason


def test_parse_manual_form_bad_time():
    d, _s, _e, reason = parse_manual_form(
        day_raw=DAY.isoformat(), start_raw="9:00", end_raw="10:00", seat_raw="1",
    )
    assert d is None and "时间" in reason


def test_parse_manual_form_bad_seat():
    d, _s, _e, reason = parse_manual_form(
        day_raw=DAY.isoformat(), start_raw="09:00", end_raw="10:00", seat_raw="abc",
    )
    assert d is None and "座位" in reason


def test_parse_manual_form_blank_seat_rejected():
    """空座位号不得经 zfill 静默变成 "000"。"""
    d, _s, _e, reason = parse_manual_form(
        day_raw=DAY.isoformat(), start_raw="09:00", end_raw="10:00", seat_raw="  ",
    )
    assert d is None and "座位号" in reason


# ---------- 纯函数：used_hours_for_account_day ----------

def test_used_hours_excludes_terminal_statuses():
    tasks = [
        _task(1, "a1", "021", "09:00", "11:00", TaskStatus.ACTIVE, DAY),
        _task(2, "a1", "022", "11:00", "12:00", TaskStatus.FAILED, DAY),
        _task(3, "a1", "023", "12:00", "13:00", TaskStatus.COMPLETE, DAY),
    ]
    assert used_hours_for_account_day(tasks, DAY, "a1") == 2.0


def test_used_hours_filters_other_account_and_day():
    other_day = DAY.fromordinal(DAY.toordinal() + 1)
    tasks = [
        _task(1, "a1", "021", "09:00", "11:00", TaskStatus.ACTIVE, DAY),
        _task(2, "a2", "021", "09:00", "11:00", TaskStatus.ACTIVE, DAY),
        _task(3, "a1", "021", "09:00", "11:00", TaskStatus.ACTIVE, other_day),
    ]
    assert used_hours_for_account_day(tasks, DAY, "a1") == 2.0


# ---------- 纯函数：validate_manual_submission（9 条规则） ----------

def _task(id_, acc, seat, start, end, status, day=DAY):
    return Task(
        id=id_, account_id=acc, day=day,
        start_time=time(*map(int, start.split(":"))),
        end_time=time(*map(int, end.split(":"))),
        seat_num=seat, status=status,
    )


def _base_kwargs(**over):
    kw = dict(
        day=DAY, start=time(9, 0), end=time(10, 0),
        seat_num="021", seat_slots=None, existing_tasks=[],
        max_seg_hours=2.0, daily_limit_hours=5.0,
        open_time="08:00", close_time="22:00",
        now=at_cst(DAY, time(8, 0)),
    )
    kw.update(over)
    return kw


def test_rule1_seat_must_be_numeric():
    issues = validate_manual_submission(**_base_kwargs(seat_num="abc"))
    assert any("座位号" in s for s in issues)


def test_rule3_invalid_range_end_le_start():
    issues = validate_manual_submission(**_base_kwargs(start=time(10, 0), end=time(10, 0)))
    assert any("结束" in s for s in issues)


def test_rule3_outside_open_close():
    issues = validate_manual_submission(**_base_kwargs(start=time(7, 0), end=time(9, 0)))
    assert any("营业时间" in s for s in issues)


def test_rule3_step_must_be_30min():
    issues = validate_manual_submission(**_base_kwargs(start=time(9, 15), end=time(10, 15)))
    assert any("30 分钟步进" in s for s in issues)


def test_rule4_max_segment_hours():
    issues = validate_manual_submission(
        **_base_kwargs(start=time(9, 0), end=time(11, 30), max_seg_hours=2.0),
    )
    assert any("单段上限" in s for s in issues)


def test_rule5_daily_limit():
    existing = [_task(1, "a1", "021", "08:00", "10:00", TaskStatus.ACTIVE, DAY),
                _task(2, "a1", "022", "10:00", "12:00", TaskStatus.ACTIVE, DAY),
                _task(3, "a1", "023", "12:00", "14:00", TaskStatus.ACTIVE, DAY)]
    issues = validate_manual_submission(
        **_base_kwargs(existing_tasks=existing, start=time(14, 0), end=time(16, 0)),
    )
    assert any("每日限额" in s for s in issues)


def test_rule5_caller_filters_terminal():
    """规格要求调用方过滤非终态任务；校验函数对 existing_tasks 全部累加。"""
    existing = [_task(1, "a1", "021", "08:00", "09:00", TaskStatus.READY, DAY),
                _task(2, "a1", "022", "10:00", "11:00", TaskStatus.ACTIVE, DAY)]
    issues = validate_manual_submission(
        **_base_kwargs(existing_tasks=existing, start=time(20, 0), end=time(21, 0)),
    )
    assert not any("每日限额" in s for s in issues)


def test_rule6_cross_seat_overlap():
    existing = [_task(1, "a1", "022", "10:00", "11:00", TaskStatus.ACTIVE, DAY)]
    issues = validate_manual_submission(
        **_base_kwargs(existing_tasks=existing,
                       start=time(10, 30), end=time(11, 30), seat_num="021"),
    )
    assert any("时间重叠" in s for s in issues)


def test_rule7_same_seat_second_segment_allowed():
    existing = [_task(1, "a1", "021", "08:00", "09:00", TaskStatus.ACTIVE, DAY)]
    issues = validate_manual_submission(
        **_base_kwargs(existing_tasks=existing,
                       start=time(10, 0), end=time(11, 0), seat_num="021"))
    assert not issues


def test_rule8_matrix_slot_blocked_only_for_future_day():
    tomorrow = DAY.fromordinal(DAY.toordinal() + 1)
    today_slots = {"021": {weekday_key(DAY): ["09:00-11:00"]}}
    # 今天：矩阵时段已实体化为任务（或已 failed），允许手动补约
    issues = validate_manual_submission(
        **_base_kwargs(seat_slots=today_slots,
                       start=time(9, 0), end=time(10, 0), seat_num="021"))
    assert not any("守护矩阵" in s for s in issues)
    # 明天（14:00 后窗口开放）：矩阵时段仍拦截，避免与 14:00 批量提交撞车
    tomorrow_slots = {"021": {weekday_key(tomorrow): ["09:00-11:00"]}}
    issues2 = validate_manual_submission(
        **_base_kwargs(day=tomorrow, seat_slots=tomorrow_slots,
                       start=time(9, 0), end=time(10, 0), seat_num="021",
                       now=at_cst(DAY, time(14, 30))))
    assert any("守护矩阵" in s for s in issues2)


def test_rule9_sign_deadline_today():
    issues = validate_manual_submission(
        **_base_kwargs(start=time(9, 0), end=time(10, 0),
                       now=at_cst(DAY, time(9, 25))),
    )
    assert any("签到窗口已过" in s for s in issues)


def test_rule9_sign_deadline_just_inside():
    issues = validate_manual_submission(
        **_base_kwargs(start=time(9, 0), end=time(10, 0),
                       now=at_cst(DAY, time(9, 5))),
    )
    assert not any("签到窗口已过" in s for s in issues)


def test_rule2_tomorrow_blocked_before_14():
    issues = validate_manual_submission(
        **_base_kwargs(day=TOMORROW, now=at_cst(DAY, time(10, 0))),
    )
    assert any("14" in s or "00" in s for s in issues)


def test_passes_clean_case():
    issues = validate_manual_submission(**_base_kwargs())
    assert issues == []


# ---------- 路由：/manual + /manual/reserve ----------

class FakeManualStore:
    def __init__(self):
        self.tasks: list[Task] = []
        self.accounts: list[Account] = [
            Account(id="张三", phone="13800000001", password="x", slots=[],
                    seat_slots={"021": {"mon": ["09:00-11:00"]}}),
            Account(id="李四", phone="13800000002", password="x", slots=[],
                    seat_slots={"021": {"tue": ["11:00-13:00"]}}),
        ]
        self.seats = [SeatTarget(seat_num="021"), SeatTarget(seat_num="022")]
        self.added: list[Task] = []
        self.added_initial_status: list[TaskStatus] = []
        self.added_initial_source: list[str] = []
        self.fail_integrity = False

    async def list_accounts(self, include_inactive=False):
        return self.accounts

    async def list_target_seats(self):
        return self.seats

    async def list_tasks(self, account_id=None, day=None, seat_num=None):
        return [t for t in self.tasks
                if (account_id is None or t.account_id == account_id)
                and (day is None or t.day == day)
                and (seat_num is None or t.seat_num == seat_num)]

    async def get_account(self, acc_id):
        return next((a for a in self.accounts if a.id == acc_id), None)

    async def get_task(self, task_id):
        return next((t for t in self.tasks if t.id == task_id), None)

    async def add_task(self, t: Task) -> int:
        if self.fail_integrity:
            raise sqlite3.IntegrityError("uq_tasks_active_identity")
        t.id = len(self.tasks) + 1
        self.added_initial_status.append(t.status)
        self.added_initial_source.append(t.source)
        self.tasks.append(t)
        self.added.append(t)
        return t.id

    async def update_task_status(self, task_id, status, reserve_id=None, last_error=None):
        t = await self.get_task(task_id)
        if t:
            t.status = status
            if reserve_id is not None:
                t.reserve_id = reserve_id
            if last_error is not None:
                t.last_error = last_error

    async def log_action(self, *args, **kwargs):
        return None


class FakeManualClient:
    def __init__(self, occupied=None, fail=False):
        self._occupied = occupied or []
        self._fail = fail

    def cookies(self):
        return {"sid": "x"}

    async def get_used_times(self, room_id, seat, day):
        if self._fail:
            raise RuntimeError("network down")
        return list(self._occupied)

    async def cancel(self, reserve_id):
        return {"success": True, "msg": "ok"}


class FakeManualSched:
    def __init__(self, occupied=None, fail_get=False, submit_outcome="ok"):
        self.client = FakeManualClient(occupied=occupied, fail=fail_get)
        self.run_submit_called = []
        self.submit_outcome = submit_outcome

    async def client_ready(self, acc):
        return self.client

    async def login_and_persist(self, acc, client, label=""):
        return True

    async def run_submit(self, acc, t):
        self.run_submit_called.append((acc.id, t.id))
        if self.submit_outcome == "ok":
            t.status = TaskStatus.ACTIVE
            t.reserve_id = 999
        elif self.submit_outcome == "signed":
            # tick 在提交期间并发签到的竞态形态
            t.status = TaskStatus.SIGNED
            t.reserve_id = 999
        else:
            t.status = TaskStatus.FAILED
            t.last_error = "座位已被预约"

    async def _warn(self, msg, acc_id=None):
        return None


class _Cfg:
    class library:
        room_id = 0
        open_time = "08:00"
        close_time = "22:00"
        max_reserve_hours = 2.0
        daily_reserve_hours_limit = 5.0


def _make_manual_client(store, sched):
    app = FastAPI()
    app.include_router(router)
    app.state.cfg = _Cfg
    app.state.store = store
    app.state.sched = sched
    app.state.templates = new_templates()
    return TestClient(app)


def test_manual_page_renders_200():
    store = FakeManualStore()
    sched = FakeManualSched()
    c = _make_manual_client(store, sched)
    r = c.get("/manual")
    assert r.status_code == 200
    assert "手动预约" in r.text
    assert "暂无可用账号" in r.text or "账号" in r.text


def test_manual_reserve_bad_date_redirects():
    store = FakeManualStore()
    sched = FakeManualSched()
    c = _make_manual_client(store, sched)
    r = c.post("/manual/reserve", data={
        "account_id": "张三", "seat_num": "021",
        "day": "not-a-date", "start": "09:00", "end": "10:00",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "/manual" in r.headers["location"]
    assert "error=" in r.headers["location"]


def test_manual_reserve_account_404():
    store = FakeManualStore()
    sched = FakeManualSched()
    c = _make_manual_client(store, sched)
    r = c.post("/manual/reserve", data={
        "account_id": "不存在", "seat_num": "021",
        "day": DAY.isoformat(), "start": "09:00", "end": "10:00",
    }, follow_redirects=False)
    assert r.status_code == 404


def test_manual_reserve_rule_fail_redirects():
    store = FakeManualStore()
    sched = FakeManualSched()
    c = _make_manual_client(store, sched)
    r = c.post("/manual/reserve", data={
        "account_id": "张三", "seat_num": "021",
        "day": DAY.isoformat(), "start": "09:00", "end": "11:30",
    }, follow_redirects=False)
    assert r.status_code == 303
    qs = parse_qs(urlparse(r.headers["location"]).query)
    assert "error" in qs
    assert "单段上限" in qs["error"][0]


def test_manual_reserve_precheck_occupied_redirects(monkeypatch):
    """固定时钟到 08:00：真实 now 会让 09:00 段在 08:40 后命中签到截止规则，
    校验先于预检重定向，断言将被带偏（时段依赖测试）。"""
    import seatbot.web.routes as R
    monkeypatch.setattr(R, "now_cst", lambda: at_cst(DAY, time(8, 0)))
    store = FakeManualStore()
    sched = FakeManualSched(occupied=[("09:30", "10:30")])
    c = _make_manual_client(store, sched)
    r = c.post("/manual/reserve", data={
        "account_id": "张三", "seat_num": "021",
        "day": DAY.isoformat(), "start": "09:00", "end": "10:00",
    }, follow_redirects=False)
    assert r.status_code == 303
    qs = parse_qs(urlparse(r.headers["location"]).query)
    assert "error" in qs
    assert "已被占用" in qs["error"][0]


def test_manual_reserve_integrity_error_redirects(monkeypatch):
    import seatbot.web.routes as R
    monkeypatch.setattr(R, "now_cst", lambda: at_cst(DAY, time(8, 0)))
    store = FakeManualStore()
    store.fail_integrity = True
    sched = FakeManualSched()
    c = _make_manual_client(store, sched)
    r = c.post("/manual/reserve", data={
        "account_id": "张三", "seat_num": "021",
        "day": DAY.isoformat(), "start": "20:00", "end": "21:00",
    }, follow_redirects=False)
    assert r.status_code == 303
    qs = parse_qs(urlparse(r.headers["location"]).query)
    assert "error" in qs
    assert "进行中" in qs["error"][0]


def test_manual_reserve_success_redirects_with_reserve_id(monkeypatch):
    """把 now_cst 固定到 8:00，确保 20:00-21:00 不会触发签到截止规则。"""
    import seatbot.web.routes as R
    monkeypatch.setattr(R, "now_cst", lambda: at_cst(DAY, time(8, 0)))
    store = FakeManualStore()
    sched = FakeManualSched(submit_outcome="ok")
    c = _make_manual_client(store, sched)
    r = c.post("/manual/reserve", data={
        "account_id": "张三", "seat_num": "021",
        "day": DAY.isoformat(), "start": "20:00", "end": "21:00",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert sched.run_submit_called
    qs = parse_qs(urlparse(r.headers["location"]).query)
    assert "msg" in qs
    assert "预约成功" in qs["msg"][0]
    assert "999" in qs["msg"][0]
    assert store.added_initial_source[0] == TASK_SOURCE_MANUAL
    assert store.added_initial_status[0] == TaskStatus.READY


def test_manual_reserve_failure_redirects_with_error(monkeypatch):
    import seatbot.web.routes as R
    monkeypatch.setattr(R, "now_cst", lambda: at_cst(DAY, time(8, 0)))
    store = FakeManualStore()
    sched = FakeManualSched(submit_outcome="fail")
    c = _make_manual_client(store, sched)
    r = c.post("/manual/reserve", data={
        "account_id": "张三", "seat_num": "021",
        "day": DAY.isoformat(), "start": "20:00", "end": "21:00",
    }, follow_redirects=False)
    assert r.status_code == 303
    qs = parse_qs(urlparse(r.headers["location"]).query)
    assert "error" in qs
    assert "提交未成" in qs["error"][0]


def test_manual_reserve_signed_race_still_success(monkeypatch):
    """提交期间 tick 已把时段签到（SIGNED）→ 仍按成功反馈，不误报"提交未成"。"""
    import seatbot.web.routes as R
    monkeypatch.setattr(R, "now_cst", lambda: at_cst(DAY, time(8, 0)))
    store = FakeManualStore()
    sched = FakeManualSched(submit_outcome="signed")
    c = _make_manual_client(store, sched)
    r = c.post("/manual/reserve", data={
        "account_id": "张三", "seat_num": "021",
        "day": DAY.isoformat(), "start": "20:00", "end": "21:00",
    }, follow_redirects=False)
    assert r.status_code == 303
    qs = parse_qs(urlparse(r.headers["location"]).query)
    assert "msg" in qs and "预约成功" in qs["msg"][0]


def test_manual_reserve_tomorrow_blocked_before_14(monkeypatch):
    """固定时钟到上午：14:00 后规则 2 放行，用例会走完整提交链路而误报失败。"""
    import seatbot.web.routes as R
    monkeypatch.setattr(R, "now_cst", lambda: at_cst(DAY, time(10, 0)))
    store = FakeManualStore()
    sched = FakeManualSched()
    c = _make_manual_client(store, sched)
    r = c.post("/manual/reserve", data={
        "account_id": "张三", "seat_num": "021",
        "day": TOMORROW.isoformat(), "start": "20:00", "end": "21:00",
    }, follow_redirects=False)
    assert r.status_code == 303
    qs = parse_qs(urlparse(r.headers["location"]).query)
    assert "error" in qs
    assert "14" in qs["error"][0] or "00" in qs["error"][0]


def test_manual_reserve_sign_deadline(monkeypatch):
    """把 now_cst 注入到当前时间之后 30 分钟的早晨，模拟已开始超过 20 分钟。"""
    import seatbot.web.routes as R
    fixed = at_cst(DAY, time(8, 30))
    monkeypatch.setattr(R, "now_cst", lambda: fixed)
    store = FakeManualStore()
    sched = FakeManualSched()
    c = _make_manual_client(store, sched)
    r = c.post("/manual/reserve", data={
        "account_id": "张三", "seat_num": "021",
        "day": DAY.isoformat(), "start": "08:00", "end": "09:00",
    }, follow_redirects=False)
    assert r.status_code == 303
    qs = parse_qs(urlparse(r.headers["location"]).query)
    assert "error" in qs
    assert "签到窗口" in qs["error"][0]


# ---------- /api/manual/occupancy ----------

def test_occupancy_400_bad_date():
    store = FakeManualStore()
    sched = FakeManualSched()
    c = _make_manual_client(store, sched)
    r = c.get("/api/manual/occupancy",
              params={"account_id": "张三", "seat_num": "021", "day": "bad"})
    assert r.status_code == 400


def test_occupancy_404_unknown_account():
    store = FakeManualStore()
    sched = FakeManualSched()
    c = _make_manual_client(store, sched)
    r = c.get("/api/manual/occupancy",
              params={"account_id": "不存在", "seat_num": "021", "day": DAY.isoformat()})
    assert r.status_code == 404


def test_occupancy_200_merges_blocked():
    store = FakeManualStore()
    store.tasks.append(_task(1, "张三", "021", "10:00", "11:00",
                              TaskStatus.ACTIVE, DAY))
    sched = FakeManualSched(occupied=[("14:00", "15:00")])
    c = _make_manual_client(store, sched)
    r = c.get("/api/manual/occupancy",
              params={"account_id": "张三", "seat_num": "021", "day": DAY.isoformat()})
    assert r.status_code == 200
    body = r.json()
    pairs = {tuple(p) for p in body["blocked"]}
    assert ("10:00", "11:00") in pairs
    assert ("14:00", "15:00") in pairs
    assert body["detail"]["occupied"] == [["14:00", "15:00"]]
    assert body["detail"]["tasks"] == [["10:00", "11:00"]]


def test_occupancy_502_on_client_error():
    store = FakeManualStore()
    sched = FakeManualSched(fail_get=True)
    c = _make_manual_client(store, sched)
    r = c.get("/api/manual/occupancy",
              params={"account_id": "张三", "seat_num": "021", "day": DAY.isoformat()})
    assert r.status_code == 502


# ---------- /tasks/{id}/cancel 加 next ----------

def test_safe_next_unit():
    assert _safe_next(None) is None
    assert _safe_next("") is None
    assert _safe_next("http://evil") is None
    assert _safe_next("//evil") is None
    assert _safe_next("/manual") == "/manual"
    assert _safe_next("/tasks?day=2026-09-12") == "/tasks?day=2026-09-12"


def test_task_cancel_with_next():
    store = FakeManualStore()
    store.tasks.append(Task(
        id=10, account_id="张三", day=DAY,
        start_time=time(20, 0), end_time=time(21, 0), seat_num="021",
        status=TaskStatus.ACTIVE, reserve_id=555,
    ))
    sched = FakeManualSched()
    c = _make_manual_client(store, sched)
    r = c.post("/tasks/10/cancel", data={"next": "/manual"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/manual"


def test_task_cancel_unsafe_next_falls_back():
    store = FakeManualStore()
    store.tasks.append(Task(
        id=11, account_id="张三", day=DAY,
        start_time=time(20, 0), end_time=time(21, 0), seat_num="021",
        status=TaskStatus.ACTIVE, reserve_id=556,
    ))
    sched = FakeManualSched()
    c = _make_manual_client(store, sched)
    r = c.post("/tasks/11/cancel",
               data={"next": "http://evil.com/x"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/tasks?cancelled=1"


# ---------- 集成：manual 任务天然屏蔽托管/实况同步（known 机制） ----------

def _entry(rid, seat, day, start, end, status=0):
    return {
        "id": rid, "seatNum": int(seat),
        "today": day.isoformat(),
        "startTime": int(at_cst(day, start).timestamp() * 1000),
        "endTime": int(at_cst(day, end).timestamp() * 1000),
        "status": status,
    }


def test_manual_reserve_id_excluded_from_diff_user_reserved():
    day = today_cst()
    parsed = parse_reservations(
        [_entry(501, "021", day, time(15, 0), time(16, 0), status=1)],
        {day, day.fromordinal(day.toordinal() + 1)},
    )
    to_add, to_del = diff_user_reserved(parsed, {501}, [])
    assert to_add == [] and to_del == []


def test_manual_reserve_id_excluded_from_plan_adoption():
    day = today_cst()
    parsed = [{
        "reserve_id": 502, "seat_num": "021", "day": day,
        "start": time(15, 0), "end": time(16, 0), "status": 0,
    }]
    actions = plan_adoption("a1", parsed,
                             known_reserve_ids={502},
                             stopped_reserve_ids=set(),
                             queued_reserve_ids=set(),
                             account_tasks=[],
                             now=at_cst(day, time(14, 0)))
    assert actions == []
