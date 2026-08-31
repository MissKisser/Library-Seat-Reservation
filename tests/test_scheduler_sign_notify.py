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
