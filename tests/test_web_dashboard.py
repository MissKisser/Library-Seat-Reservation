"""Unit tests for the dashboard coverage annotation and day switching."""
import asyncio
from datetime import date, time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.templating import Jinja2Templates

from seatbot.coverage import compute_seat_coverage
from seatbot.models import Account, SeatTarget, Task, TaskStatus
from seatbot.web.app import TEMPLATES_DIR
from seatbot.web.routes import _annotate_rows, router


DAY = date(2026, 8, 28)
NEXT_DAY = date(2026, 8, 29)


def _task(id_, acc, seat, start, end, status, day=DAY):
    return Task(
        id=id_, account_id=acc, day=day,
        start_time=time(*map(int, start.split(":"))),
        end_time=time(*map(int, end.split(":"))),
        seat_num=seat, status=status,
    )


class FakeStore:
    """In-memory store stub; accounts carry no phone/password so the
    dashboard route skips every Chaoxing call."""

    def __init__(self, tasks=(), seat_slots=None):
        self.tasks = list(tasks)
        self.seat_slots = seat_slots or {"104": ["09:00-11:00"]}

    async def list_accounts(self):
        return [Account(id="a1", phone="", password="", slots=[], seat_slots=self.seat_slots)]

    async def list_target_seats(self):
        return [SeatTarget(seat_num="104")]

    async def list_user_reserved(self, day=None):
        return []

    async def list_tasks(self, account_id=None, day=None, seat_num=None):
        return [
            t for t in self.tasks
            if (account_id is None or t.account_id == account_id)
            and (day is None or t.day == day)
            and (seat_num is None or t.seat_num == seat_num)
        ]

    async def list_logs(self, account_id=None, level=None, limit=200):
        return []


class _StubCfg:
    class library:
        room_id = 0
        open_time = time(8, 0)
        close_time = time(22, 0)


def _make_client(store):
    app = FastAPI()
    app.include_router(router)
    app.state.cfg = _StubCfg
    app.state.store = store
    app.state.sched = None
    app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    return TestClient(app)


@pytest.fixture
def client():
    return _make_client(FakeStore())


def test_annotate_matches_two_hour_task_across_cells():
    asyncio.run(_test_annotate_matches_two_hour_task_across_cells())


async def _test_annotate_matches_two_hour_task_across_cells():
    """一个 2h 任务应标注其覆盖的全部 30min 格子, 而非要求与格子完全等长。"""
    store = FakeStore(tasks=[
        _task(1, "a1", "104", "09:00", "11:00", TaskStatus.ACTIVE),
    ])
    accounts = await store.list_accounts()
    seats = await store.list_target_seats()
    rows = compute_seat_coverage(accounts, seats, DAY)
    annotated = await _annotate_rows(rows, store, DAY)

    cell_104 = annotated[0]
    cells = cell_104["cells"]
    by_start = {c["start"]: c for c in cells}
    covered = by_start[time(9, 0)]
    assert covered["accounts_info"][0]["status"] == "active"
    assert covered["accounts_info"][0]["task_id"] == 1
    assert covered["accounts_info"][0]["start_time"] == "09:00"
    assert covered["accounts_info"][0]["end_time"] == "11:00"
    for start in (time(9, 30), time(10, 0), time(10, 30)):
        assert by_start[start]["accounts_info"][0]["status"] == "active"
    assert by_start[time(8, 0)]["accounts_info"] == []
    assert by_start[time(11, 0)]["accounts_info"] == []


def test_annotate_reports_failed_and_complete():
    asyncio.run(_test_annotate_reports_failed_and_complete())


async def _test_annotate_reports_failed_and_complete():
    store = FakeStore(
        tasks=[
            _task(2, "a1", "104", "09:00", "11:00", TaskStatus.FAILED),
            _task(3, "a1", "104", "15:00", "17:00", TaskStatus.COMPLETE),
        ],
        seat_slots={"104": ["09:00-11:00", "15:00-17:00"]},
    )
    accounts = await store.list_accounts()
    seats = await store.list_target_seats()
    rows = compute_seat_coverage(accounts, seats, DAY)
    annotated = await _annotate_rows(rows, store, DAY)

    statuses = {c["start"]: c["accounts_info"][0]["status"]
                for c in annotated[0]["cells"] if c["accounts_info"]}
    assert statuses[time(9, 0)] == "failed"
    assert statuses[time(10, 30)] == "failed"
    assert statuses[time(15, 0)] == "complete"
    assert statuses[time(16, 30)] == "complete"


def test_annotate_unpainted_cells_stay_empty():
    asyncio.run(_test_annotate_unpainted_cells_stay_empty())


async def _test_annotate_unpainted_cells_stay_empty():
    """seat_slots 只声明 09:00-11:00, 画布其余格子不应有任何账号。"""
    store = FakeStore(tasks=[])
    accounts = await store.list_accounts()
    seats = await store.list_target_seats()
    rows = compute_seat_coverage(accounts, seats, DAY)
    annotated = await _annotate_rows(rows, store, DAY)

    painted = [c for c in annotated[0]["cells"] if c["accounts_info"]]
    assert len(painted) == 4
    assert all(c["accounts_info"][0]["status"] == "pending" for c in painted)


def test_dashboard_data_returns_both_days(client):
    # 任务日动态取真实"明天"，避免跨天后 today/tomorrow 视图与硬编码日期错位
    from seatbot.utils.timeutil import today_cst
    from datetime import timedelta
    tomorrow = today_cst() + timedelta(days=1)
    store = FakeStore(tasks=[
        _task(4, "a1", "104", "09:00", "11:00", TaskStatus.ACTIVE, day=tomorrow),
    ])
    client = _make_client(store)
    resp = client.get("/api/dashboard-data")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["today"]) >= {"view_day", "rows", "gap_count", "occ_err"}
    assert set(body["tomorrow"]) >= {"view_day", "rows", "gap_count", "occ_err"}
    assert body["tomorrow"]["view_day"] > body["today"]["view_day"]
    # 今天: 无任务 → 涂色格保持 pending (已规划未预约)
    today_status = [c["accounts_info"][0]["status"]
                    for r in body["today"]["rows"] for c in r["cells"] if c["accounts_info"]]
    assert today_status == ["pending"] * 4
    # 明天: 任务落在明天 → 明天视图的涂色格标注为 active
    tmr_status = [c["accounts_info"][0]["status"]
                  for r in body["tomorrow"]["rows"] for c in r["cells"] if c["accounts_info"]]
    assert tmr_status == ["active"] * 4


def test_dashboard_page_renders_both_day_tables(client):
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text
    # 新模板为数据驱动: 首屏 JSON 与 /api/dashboard-data 同源
    assert 'coverageGrid(' in html
    assert '"view_day"' in html
    assert '护城河' in html
    assert 'cell-card' in html

def test_api_version_returns_local_counter(client):
    resp = client.get("/api/version")
    assert resp.status_code == 200
    body = resp.json()
    assert "v" in body and isinstance(body["v"], int)


def test_others_occupied_cache_avoids_refetch(monkeypatch, client):
    """TTL 内的重复调用不得再次触达底层超星查询。"""
    import asyncio
    from seatbot.web import routes
    calls = {"n": 0}

    async def fake_fetch(request, store, day, seat_nums):
        calls["n"] += 1
        return ([("104", __import__("datetime").time(9, 0), __import__("datetime").time(9, 30))], None)

    monkeypatch.setattr(routes, "_fetch_others_occupied", fake_fetch)
    routes._OCC_CACHE.clear()
    from datetime import date as _date
    d = _date(2026, 8, 29)

    async def run():
        return await asyncio.gather(
            routes._fetch_others_occupied_cached(None, None, d, ["104"]),
            routes._fetch_others_occupied_cached(None, None, d, ["104"]),
        )

    r1, r2 = asyncio.run(run())
    assert calls["n"] == 1
    assert r1 == r2
