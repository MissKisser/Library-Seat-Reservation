"""签到成功应同步落入看板通知（level=info, 不走 webhook 外推）。"""
import asyncio
from datetime import date, time

from seatbot.models import Account, Task, TaskStatus
from seatbot.scheduler import Scheduler


class _FakeStore:
    def __init__(self):
        self.notifications = []
        self.statuses = []

    async def log_message(self, level, account_id, message):
        pass

    async def log_action(self, *a, **k):
        pass

    async def update_task_status(self, task_id, status, last_error=None, **k):
        self.statuses.append((task_id, status, last_error))

    async def add_notification(self, title, body="", level="warn"):
        self.notifications.append({"title": title, "body": body, "level": level})

    async def list_notifications(self, limit=5):
        return self.notifications[:limit]


class _StubRuntime:
    notify_webhook = ""


class _StubCfg:
    runtime = _StubRuntime()


class _FakeClient:
    def cookies(self):
        return {"k": "v"}

    async def sign(self, reserve_id):
        return {"success": True, "msg": "ok"}

    async def signback(self, reserve_id):
        return {"success": True, "msg": "ok"}

    async def leave(self, reserve_id):
        return {"success": False, "msg": "剩余时长小于暂离时长"}


class _StubSched(Scheduler):
    async def client_ready(self, acc):
        return _FakeClient()

    async def _act_with_relogin(self, client, acc, fn, reserve_id, label):
        return {"success": True, "msg": "ok"}


def _run_sign_once():
    store = _FakeStore()
    sched = _StubSched.__new__(_StubSched)
    sched.store = store
    sched.cfg = _StubCfg()
    sched._fail_streak = {}
    acc = Account(id="刘恒", phone="1", password="x", slots=[])
    t = Task(
        id=70, account_id="刘恒", day=date(2026, 8, 31),
        start_time=time(8, 0), end_time=time(10, 0),
        seat_num="030", status=TaskStatus.ACTIVE, reserve_id=189743666,
    )
    asyncio.run(sched._run_sign(acc, t))
    return store


def test_sign_success_lands_in_notifications_as_info():
    store = _run_sign_once()
    infos = [n for n in store.notifications if n["level"] == "info"]
    assert len(infos) == 1
    n = infos[0]
    assert "签到成功" in n["title"]
    assert "刘恒" in n["body"] and "030" in n["body"] and "189743666" in n["body"]


def test_sign_success_marks_task_signed():
    store = _run_sign_once()
    assert store.statuses == [(70, TaskStatus.SIGNED, "")]


def _run_leave_once(signback_result):
    store = _FakeStore()
    sched = _StubSched.__new__(_StubSched)
    sched.store = store
    sched.cfg = _StubCfg()
    sched._fail_streak = {}

    class _SchedLeave(_StubSched):
        async def _act_with_relogin(self, client, acc, fn, reserve_id, label):
            return signback_result

    sched = _SchedLeave.__new__(_SchedLeave)
    sched.store = store
    sched.cfg = _StubCfg()
    sched._fail_streak = {}
    acc = Account(id="刘恒", phone="1", password="x", slots=[])
    t = Task(
        id=70, account_id="刘恒", day=date(2026, 8, 31),
        start_time=time(16, 0), end_time=time(18, 0),
        seat_num="030", status=TaskStatus.ACTIVE, reserve_id=189743666,
    )
    asyncio.run(sched._run_leave(acc, t))
    return store


def test_signback_success_lands_in_notifications_as_info():
    store = _run_leave_once({"success": True, "msg": "ok"})
    infos = [n for n in store.notifications if n["level"] == "info"]
    assert len(infos) == 1
    assert "签退" in infos[0]["title"]
    assert "刘恒" in infos[0]["body"] and "189743666" in infos[0]["body"]


def test_signback_idempotent_close_lands_in_notifications(monkeypatch):
    import asyncio as _asyncio_mod

    async def _instant(s):
        return None

    monkeypatch.setattr(_asyncio_mod, "sleep", _instant)
    store = _run_leave_once({"success": False, "msg": "已签退"})
    infos = [n for n in store.notifications if n["level"] == "info"]
    assert any("签退" in n["title"] for n in infos)


def _run_afternoon_bootstrap(tasks_for_tomorrow):
    class _Store:
        def __init__(self):
            self.notifications = []
            self.tomorrow_tasks = list(tasks_for_tomorrow)

        async def log_message(self, level, account_id, message):
            pass

        async def list_target_seats(self):
            return []

        async def list_accounts(self):
            return [Account(id="a1", phone="1", password="x", slots=[])]

        async def list_tasks(self, account_id=None, day=None, seat_num=None):
            return [t for t in self.tomorrow_tasks
                    if (day is None or t.day == day)]

        async def update_task_status(self, task_id, status, last_error=None, **k):
            for t in self.tomorrow_tasks:
                if t.id == task_id:
                    t.status = status
                    t.last_error = last_error

        async def get_task(self, task_id):
            for t in self.tomorrow_tasks:
                if t.id == task_id:
                    return t
            return None

        async def add_notification(self, title, body="", level="warn"):
            self.notifications.append({"title": title, "body": body, "level": level})

        async def list_notifications(self, limit=5):
            return self.notifications[:limit]

    store = _Store()

    class _SchedBootstrap(_StubSched):
        async def _bootstrap_for_account(self, acc, day, seats):
            pass

        async def _run_submit(self, acc, t):
            for tt in self.store.tomorrow_tasks:
                if tt.id == t.id:
                    tt.status = TaskStatus.FAILED
                    tt.last_error = "本周违约次数已达上限"

    sched = _SchedBootstrap.__new__(_SchedBootstrap)
    sched.store = store
    sched.cfg = _StubCfg()
    sched._fail_streak = {}
    asyncio.run(sched._afternoon_bootstrap())
    return store


def _tomorrow_task(id_, status, seat="030", start="08:00", end="10:00", rid=None, err=""):
    return Task(
        id=id_, account_id="a1", day=date(2026, 9, 1),
        start_time=time(*map(int, start.split(":"))),
        end_time=time(*map(int, end.split(":"))),
        seat_num=seat, status=status, reserve_id=rid, last_error=err,
    )


def test_afternoon_bootstrap_all_success_notifies_info(monkeypatch):
    import asyncio as _asyncio_mod

    async def _instant(s):
        return None

    monkeypatch.setattr(_asyncio_mod, "sleep", _instant)
    store = _run_afternoon_bootstrap([
        _tomorrow_task(1, TaskStatus.ACTIVE, rid=1001),
        _tomorrow_task(2, TaskStatus.ACTIVE, seat="031", start="10:00", rid=1002),
    ])
    infos = [n for n in store.notifications
             if n["level"] == "info" and "全部成功" in n["title"]]
    assert len(infos) == 1
    assert "2" in infos[0]["body"]


def test_afternoon_bootstrap_partial_failure_notifies_error(monkeypatch):
    import asyncio as _asyncio_mod

    async def _instant(s):
        return None

    monkeypatch.setattr(_asyncio_mod, "sleep", _instant)
    store = _run_afternoon_bootstrap([
        _tomorrow_task(1, TaskStatus.ACTIVE, rid=1001),
        _tomorrow_task(2, TaskStatus.FAILED, seat="031", err="本周违约次数已达上限"),
    ])
    errors = [n for n in store.notifications
              if n["level"] == "error" and "失败" in n["title"]]
    assert len(errors) == 1
    assert "031" in errors[0]["body"] and "违约" in errors[0]["body"]
