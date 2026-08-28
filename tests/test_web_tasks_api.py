"""Unit tests for the tasks kanban JSON API and the manual signback route."""
from datetime import date, time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from seatbot.models import Account, Task, TaskStatus
from seatbot.web.routes import router

DAY = date(2026, 8, 28)


def _task(id_, acc, seat, start, end, status, day=DAY):
    return Task(
        id=id_, account_id=acc, day=day,
        start_time=time(*map(int, start.split(":"))),
        end_time=time(*map(int, end.split(":"))),
        seat_num=seat, status=status,
    )


class FakeStore:
    def __init__(self, tasks=()):
        self.tasks = list(tasks)
        self.status_updates = []
        self.actions = None

    async def list_tasks(self, account_id=None, day=None, seat_num=None):
        return [t for t in self.tasks
                if (account_id is None or t.account_id == account_id)
                and (day is None or t.day == day)
                and (seat_num is None or t.seat_num == seat_num)]

    async def get_task(self, task_id):
        return next((t for t in self.tasks if t.id == task_id), None)

    async def get_account(self, acc_id):
        if acc_id == "a1":
            return Account(id="a1", phone="", password="", slots=[])
        return None

    async def update_task_status(self, task_id, status, last_error=None):
        self.status_updates.append((task_id, status))

    async def log_action(self, *args, **kwargs):
        self.actions = args


class FakeClient:
    def cookies(self):
        return {"sid": "x"}

    async def signback(self, reserve_id):
        return {"success": True, "msg": "ok"}


class FakeSched:
    def _client_for(self, acc):
        return FakeClient()


def _make_client(store, sched=None):
    app = FastAPI()
    app.include_router(router)
    app.state.cfg = None
    app.state.store = store
    app.state.sched = sched
    return TestClient(app)


def test_api_tasks_returns_serialized_day_tasks():
    store = FakeStore([
        _task(1, "a1", "104", "09:00", "11:00", TaskStatus.PENDING),
        _task(2, "a1", "105", "15:00", "17:00", TaskStatus.ACTIVE),
    ])
    client = _make_client(store)
    r = client.get("/api/tasks", params={"day": DAY.isoformat()})
    assert r.status_code == 200
    body = r.json()
    assert body["day"] == DAY.isoformat()
    assert [t["id"] for t in body["tasks"]] == [1, 2]
    assert body["tasks"][1]["status"] == "active"
    assert body["tasks"][1]["start"] == "15:00"


def test_api_tasks_defaults_to_today():
    store = FakeStore([])
    client = _make_client(store)
    r = client.get("/api/tasks")
    assert r.status_code == 200
    assert len(r.json()["day"]) == 10


def test_leave_route_signs_back_and_completes():
    t = _task(2, "a1", "105", "15:00", "17:00", TaskStatus.ACTIVE)
    t.reserve_id = 901
    store = FakeStore([t])
    client = _make_client(store, sched=FakeSched())
    r = client.post("/tasks/2/leave", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/?left=1"
    assert store.status_updates == [(2, TaskStatus.COMPLETE)]
    assert store.actions[1] == "signback"


def test_leave_route_rejects_task_without_reserve():
    store = FakeStore([_task(3, "a1", "104", "09:00", "11:00", TaskStatus.PENDING)])
    client = _make_client(store, sched=FakeSched())
    r = client.post("/tasks/3/leave", follow_redirects=False)
    assert r.status_code == 400
