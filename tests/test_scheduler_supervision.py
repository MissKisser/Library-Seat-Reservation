"""监督检测与自动落座: reservelist status=5 → sign 解除, 通知与审计落库。"""
import asyncio
from datetime import date, datetime, time, timedelta, timezone

from seatbot.models import Account, Task, TaskStatus
from seatbot.scheduler import Scheduler


class _FakeStore:
    def __init__(self, tasks=None):
        self.tasks = list(tasks or [])
        self.notifications = []
        self.actions = []
        self.statuses = []

    async def log_message(self, level, account_id, message):
        pass

    async def log_action(self, account_id, action, request, response,
                         success, message=None):
        self.actions.append((account_id, action, request, success))

    async def update_task_status(self, task_id, status, last_error=None, **k):
        self.statuses.append((task_id, status, last_error))
        for t in self.tasks:
            if t.id == task_id:
                t.status = status

    async def add_notification(self, title, body="", level="warn"):
        self.notifications.append({"title": title, "body": body, "level": level})

    async def list_notifications(self, limit=5):
        return self.notifications[:limit]

    async def list_tasks(self, account_id=None, day=None, seat_num=None):
        return [t for t in self.tasks
                if (account_id is None or t.account_id == account_id)
                and (day is None or t.day == day)]


class _FakeClient:
    def __init__(self, supervised, sign_results=None, has_cookies=True):
        self._supervised = list(supervised)
        self._sign_results = list(sign_results or [])
        self._has_cookies = has_cookies
        self.sign_calls = []
        self.supervise_polls = 0

    def cookies(self):
        return {"_uid": "x"} if self._has_cookies else {}

    async def supervised_reservations(self):
        self.supervise_polls += 1
        return list(self._supervised)

    async def sign(self, rid):
        self.sign_calls.append(rid)
        return (self._sign_results.pop(0) if self._sign_results
                else {"success": True, "msg": "ok"})


class _StubSched(Scheduler):
    """client_ready 返回注入的 _FakeClient; relogin 通道直通。"""

    def __init__(self, client, store):
        self.store = store
        self.cfg = type("C", (), {"runtime": type("R", (), {"notify_webhook": ""})()})()
        self._clients = {}
        self._fail_streak = {}
        self._supervise_seen = {}
        self.relay_lead_seconds = 300
        self.logins = 0
        self._client = client

    async def client_ready(self, acc):
        return self._client

    async def login_and_persist(self, acc, client, label="登录"):
        self.logins += 1
        return True

    async def _act_with_relogin(self, client, acc, fn, reserve_id, label):
        return await fn(reserve_id)


def _acc():
    return Account(id="刘恒", phone="1", password="x", slots=[])


def _task(task_id, rid, status=TaskStatus.SIGNED):
    return Task(
        id=task_id, account_id="刘恒", day=date(2026, 8, 31),
        start_time=time(15, 0), end_time=time(17, 0),
        seat_num="030", status=status, reserve_id=rid,
    )


def _rec(rid, seat="030"):
    return {"id": rid, "seatNum": seat, "status": 5,
            "roomId": 11692, "startTime": 0, "endTime": 0}


def _run_check(supervised, sign_results=None, tasks=None):
    store = _FakeStore(tasks)
    client = _FakeClient(supervised, sign_results)
    sched = _StubSched(client, store)
    asyncio.run(sched._check_supervision(_acc()))
    return store, client, sched


def test_supervised_reservation_triggers_sign_and_notifications():
    store, client, _ = _run_check(
        [_rec(189743666)], sign_results=[{"success": True, "msg": "ok"}],
        tasks=[_task(70, 189743666)],
    )
    assert client.sign_calls == [189743666]
    titles = [n["title"] for n in store.notifications]
    assert "检测到监督：正在自动落座" in titles
    assert "监督已解除" in titles
    warn = [n for n in store.notifications if n["level"] == "warn"]
    assert len(warn) == 1 and "刘恒" in warn[0]["body"] and "030" in warn[0]["body"]
    assert ("刘恒", "supervise_sign", "189743666", True) in store.actions


def test_sign_success_marks_matching_active_task_signed():
    store, _, _ = _run_check(
        [_rec(1001)], sign_results=[{"success": True, "msg": "ok"}],
        tasks=[_task(70, 1001, status=TaskStatus.ACTIVE)],
    )
    assert store.statuses == [(70, TaskStatus.SIGNED, "")]


def test_detection_notification_fires_once_per_episode():
    store, client, sched = _run_check(
        [_rec(1001)], sign_results=[{"success": False, "msg": "失败"}],
    )
    asyncio.run(sched._check_supervision(_acc()))
    assert client.sign_calls == [1001, 1001]
    detected = [n for n in store.notifications
                if "检测到监督" in n["title"]]
    assert len(detected) == 1


def test_check_skipped_when_no_cookies():
    store = _FakeStore()
    client = _FakeClient([_rec(1001)], has_cookies=False)
    sched = _StubSched(client, store)
    asyncio.run(sched._check_supervision(_acc()))
    assert client.supervise_polls == 0
    assert client.sign_calls == []


def test_reservelist_failure_is_swallowed():
    store = _FakeStore()
    client = _FakeClient([])

    async def supervised_reservations():
        raise RuntimeError("network down")

    client.supervised_reservations = supervised_reservations
    sched = _StubSched(client, store)
    asyncio.run(sched._check_supervision(_acc()))
    assert client.sign_calls == []


def test_client_supervised_reservations_filters_status_5(monkeypatch):
    from seatbot.client import ChaoxingClient

    client = ChaoxingClient()

    async def fake_reserve_list(**kw):
        return [{"id": 1, "status": 1}, {"id": 2, "status": 5},
                {"id": 3, "status": 0}, {"id": 4, "status": 8}]

    monkeypatch.setattr(client, "reserve_list", fake_reserve_list)
    out = asyncio.run(client.supervised_reservations())
    assert [r["id"] for r in out] == [2]


def test_session_expiry_relogins_and_retries():
    from seatbot.client import ChaoxingError

    store = _FakeStore()
    client = _FakeClient([])
    client.resets = 0
    polls = {"n": 0}

    async def supervised_reservations():
        polls["n"] += 1
        if polls["n"] == 1:
            raise ChaoxingError("reservelist rejected: 您当前未登录，请重新登录")
        return [_rec(1001)]

    client.supervised_reservations = supervised_reservations

    def reset_session():
        client.resets += 1

    client.reset_session = reset_session
    sched = _StubSched(client, store)
    asyncio.run(sched._check_supervision(_acc()))
    assert client.resets == 1
    assert sched.logins == 1
    assert client.sign_calls == [1001]


def test_tick_account_polls_supervision_only_while_holding_seat(monkeypatch):
    from seatbot.utils import timeutil

    fixed = datetime(2026, 8, 31, 16, 0, 0,
                     tzinfo=timezone(timedelta(hours=8)))
    monkeypatch.setattr("seatbot.scheduler.now_cst", lambda: fixed)
    monkeypatch.setattr("seatbot.scheduler.today_cst",
                        lambda: fixed.date())

    store = _FakeStore([_task(70, 1001, status=TaskStatus.SIGNED)])
    client = _FakeClient([_rec(1001)])
    sched = _StubSched(client, store)

    async def get_account(acc_id):
        return _acc()

    store.get_account = get_account
    asyncio.run(sched.tick_account("刘恒"))
    assert client.supervise_polls == 1
    assert client.sign_calls == [1001]

    # 无在途任务 → 不轮询监督
    client2 = _FakeClient([_rec(1001)])
    store2 = _FakeStore([])
    store2.get_account = get_account
    sched2 = _StubSched(client2, store2)
    asyncio.run(sched2.tick_account("刘恒"))
    assert client2.supervise_polls == 0
