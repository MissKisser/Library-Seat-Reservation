"""Unit tests for GET /api/status."""
from datetime import datetime, time, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from seatbot.scheduler import NextRelay
from seatbot.web.routes import router


class FakeStore:
    async def list_logs(self, account_id=None, level=None, limit=200):
        return []

    async def list_target_seats(self):
        return []


class FakeSched:
    async def peek_next_relay(self):
        return None


class _StubCfg:
    class library:
        target_seat_num = "084"
        room_id = 0
        room_name = ""
        open_time = time(8, 0)
        close_time = time(22, 0)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    app.state.cfg = _StubCfg
    app.state.store = FakeStore()
    app.state.sched = FakeSched()
    return TestClient(app)


def test_status_no_relay(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    j = r.json()
    assert "now" in j
    assert j["next_relay_at"] is None
    assert j["next_relay_in_min"] is None
    assert j["next_relay_account_id"] is None
    assert j["next_relay_task_id"] is None
    assert j["next_relay_status"] is None
    assert j["last_log"] is None


def test_status_with_relay(client):
    nxt = NextRelay(
        at=datetime.now(timezone(timedelta(hours=8))) + timedelta(minutes=15),
        delta_minutes=15,
        account_id="guard_a",
        task_id=42,
        seat_num="084",
        start_time=time(10, 0),
        end_time=time(12, 0),
        status="pending",
    )

    async def fake_peek():
        return nxt

    client.app.state.sched.peek_next_relay = fake_peek
    r = client.get("/api/status")
    assert r.status_code == 200
    j = r.json()
    assert j["next_relay_in_min"] == 15
    assert j["next_relay_account_id"] == "guard_a"
    assert j["next_relay_task_id"] == 42
    assert j["next_relay_status"] == "pending"
    assert j["next_relay_at"] is not None
    assert j["last_log"] is None


def test_status_with_last_log(client):
    """When store has a log, the status payload should surface it."""
    from seatbot.store import LogRow

    log = LogRow(
        id=1,
        ts="2026-07-08 10:00:00",
        level="INFO",
        account_id="guard_a",
        message="hello",
    )

    async def fake_logs(account_id=None, level=None, limit=200):
        return [log]

    client.app.state.store.list_logs = fake_logs
    r = client.get("/api/status")
    assert r.status_code == 200
    j = r.json()
    assert j["last_log"] is not None
    assert j["last_log"]["level"] == "INFO"
    assert j["last_log"]["account_id"] == "guard_a"
    assert j["last_log"]["message"] == "hello"


def test_status_now_ms_epoch(client):
    import time as _t
    j = client.get("/api/status").json()
    assert abs(j["now_ms"] - _t.time() * 1000) < 5000


def test_version_now_ms_epoch(client):
    import time as _t
    j = client.get("/api/version").json()
    assert abs(j["now_ms"] - _t.time() * 1000) < 5000
