"""实况核对判定函数单测（纯同步，不发任何网络请求）。"""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

from seatbot.reconcile import classify_task, pick_read_account
from seatbot.store import StateStore


def test_fully_covered_is_consistent():
    ratio, bad = classify_task("14:00", "16:00", [("14:00", "16:00")])
    assert ratio == 1.0 and not bad


def test_partially_covered_is_mismatch():
    ratio, bad = classify_task("14:00", "16:00", [("15:00", "17:00")])
    assert ratio == 0.5 and bad


def test_zero_coverage_is_mismatch():
    _, bad = classify_task("14:00", "16:00", [])
    assert bad


from datetime import time, timedelta

from seatbot.config import Config, LibraryConfig, RuntimeConfig
from seatbot.models import Account, Task, TaskStatus
from seatbot.scheduler import Scheduler
from seatbot.utils.timeutil import today_cst


def _make_cfg() -> Config:
    return Config(
        library=LibraryConfig(room_id=11692, room_name="t"),
        runtime=RuntimeConfig(stagger_seconds=[0, 0]),
    )


class FakeClient:
    """离线替身：cookies 恒有，get_used_times 按脚本返回。"""

    script: list = []

    def cookies(self):
        return {"_uid": "x"}

    async def get_used_times(self, room_id, seat_num, day):
        return FakeClient.script.pop(0) if FakeClient.script else []


def test_sweep_flags_then_restores_missing_reservation(tmp_path, monkeypatch):
    async def main():
        store = StateStore(str(tmp_path / "t.db"))
        await store.init()
        await store.upsert_account(Account(
            id="张三", phone="1", password="p", slots=[]))
        await store.add_target_seat("030")
        tid = await store.add_task(Task(
            id=None, account_id="张三",
            day=today_cst() + timedelta(days=1),
            start_time=time(14, 0), end_time=time(16, 0)))
        await store.update_task_status(tid, TaskStatus.ACTIVE, reserve_id=189832612)

        sched = Scheduler(_make_cfg(), store)

        async def fake_client_ready(self, acc):
            return FakeClient()

        monkeypatch.setattr(Scheduler, "client_ready", fake_client_ready)

        # 失守沿：服务端只有上午段，14:00-16:00 无占用
        FakeClient.script = [[("08:00", "10:00")]]
        out = await sched.reconcile_sweep(write=True)
        assert out == {"checked": 1, "mismatch": 1, "fetch_ok": True}
        fresh = await store.get_task(tid)
        assert (fresh.last_error or "").startswith("实况核对: ")

        # 快照已落库
        snaps = await store.list_reconcile_results()
        assert len(snaps) == 1 and snaps[0]["ok"] is False

        # 恢复沿：服务端占用完整覆盖
        FakeClient.script = [[("14:00", "16:00")]]
        await sched.reconcile_sweep(write=True)
        fresh = await store.get_task(tid)
        assert not (fresh.last_error or "")
        assert (await sched.reconcile_sweep(write=False))["checked"] == 1
        await store.close()


def test_sweep_heals_stale_session_and_retries(tmp_path, monkeypatch):
    async def main():
        store = StateStore(str(tmp_path / "t.db"))
        await store.init()
        await store.upsert_account(Account(
            id="张三", phone="1", password="p", slots=[]))
        await store.add_target_seat("030")
        tid = await store.add_task(Task(
            id=None, account_id="张三",
            day=today_cst() + timedelta(days=1),
            start_time=time(14, 0), end_time=time(16, 0)))
        await store.update_task_status(tid, TaskStatus.ACTIVE, reserve_id=1)

        class StaleClient:
            def __init__(self):
                self.resets = self.logins = 0
                self.dead = True

            def cookies(self):
                return {"_uid": "x"}

            def reset_session(self):
                self.resets += 1

            async def login(self, phone, password):
                self.logins += 1
                self.dead = False

            async def get_used_times(self, room_id, seat_num, day):
                if self.dead:
                    raise RuntimeError("未登录")
                return [("14:00", "16:00")]

        c = StaleClient()
        sched = Scheduler(_make_cfg(), store)

        async def fake_client_ready(self, acc):
            return c

        async def fake_login(self, acc, client, label="登录"):
            await client.login(acc.phone, acc.password)
            return True

        monkeypatch.setattr(Scheduler, "client_ready", fake_client_ready)
        monkeypatch.setattr(Scheduler, "login_and_persist", fake_login)
        out = await sched.reconcile_sweep(write=True)
        assert c.resets == 1 and c.logins == 1
        assert out == {"checked": 1, "mismatch": 0, "fetch_ok": True}
        assert not ((await store.get_task(tid)).last_error or "")
        await store.close()
    asyncio.run(main())


def test_tick_only_runs_during_open_hours(tmp_path, monkeypatch):
    import seatbot.scheduler as S
    from seatbot.utils.timeutil import at_cst

    async def main():
        store = StateStore(str(tmp_path / "t.db"))
        await store.init()
        sched = Scheduler(_make_cfg(), store)
        calls = {"n": 0}

        async def fake_sweep():
            calls["n"] += 1

        monkeypatch.setattr(sched, "reconcile_sweep", fake_sweep)
        # 闭馆时段（23:30）静默
        monkeypatch.setattr(S, "now_cst", lambda: at_cst(today_cst(), time(23, 30)))
        await sched.reconcile_tick()
        assert calls["n"] == 0
        assert sched._reconcile_last_at is None
        # 开放时段（09:00）首次到期即核对
        monkeypatch.setattr(S, "now_cst", lambda: at_cst(today_cst(), time(9, 0)))
        await sched.reconcile_tick()
        assert calls["n"] == 1
        await store.close()
    asyncio.run(main())


def test_adjacent_server_windows_merge():
    ratio, bad = classify_task("14:00", "16:00", [("13:00", "15:00"), ("15:00", "17:00")])
    assert ratio == 1.0 and not bad


def test_pick_read_account_prefers_freshest_cookie():
    a = SimpleNamespace(id="张三", phone="1", password="p")
    b = SimpleNamespace(id="李四", phone="2", password="p")
    c = SimpleNamespace(id="王五", phone="3", password="p")
    assert pick_read_account([a, b, c], {"张三": 100, "李四": 200}).id == "李四"


def test_pick_read_account_falls_back_without_cookie_records():
    b = SimpleNamespace(id="李四", phone="2", password="p")
    c = SimpleNamespace(id="王五", phone="3", password="p")
    assert pick_read_account([b, c]).id == "李四"


def test_pick_read_account_ignores_deleted_account_recency():
    a = SimpleNamespace(id="张三", phone="1", password="p")
    c = SimpleNamespace(id="王五", phone="3", password="p")
    assert pick_read_account([a, c], {"李四": 999}).id == "张三"


def test_pick_read_account_none_when_no_credentials():
    assert pick_read_account([SimpleNamespace(id="x", phone="", password="")]) is None


def test_reconcile_results_roundtrip_and_cap(tmp_path):
    async def main():
        s = StateStore(str(tmp_path / "t.db"))
        await s.init()
        for i in range(5):
            await s.save_reconcile_result(
                date(2026, 9, 1), "030", i % 2 == 0, f'{{"n":{i}}}')
        rows = await s.list_reconcile_results(limit=3)
        assert len(rows) == 3
        assert rows[0]["detail"] == '{"n":4}'      # 新→旧
        assert rows[0]["ok"] is True and rows[1]["ok"] is False
        await s.close()
    asyncio.run(main())



def test_dashboard_endpoint_forwards_fresh_flag(monkeypatch):
    """端点必须把 ?fresh=1 透传给 _build_dashboard_data（按钮链路的关键一环）。"""
    import asyncio

    import seatbot.web.routes as R

    captured: dict = {}

    async def fake_build(request, *, fresh=False):
        captured["fresh"] = fresh
        return {}

    monkeypatch.setattr(R, "_build_dashboard_data", fake_build)

    class FakeReq:
        def __init__(self, params):
            self.query_params = params

    asyncio.run(R.api_dashboard_data(FakeReq({"fresh": "1"})))
    assert captured["fresh"] is True
    asyncio.run(R.api_dashboard_data(FakeReq({})))
    assert captured["fresh"] is False


def test_dashboard_fresh_bypass_and_throttle(monkeypatch):
    import asyncio
    import time as _t
    from datetime import date as _date
    from datetime import time as _dtime

    import seatbot.web.routes as R

    key = ("2026-09-01", ("030",))
    R._OCC_CACHE.clear()
    R._OCC_CACHE[key] = (
        _t.monotonic(), True,
        ([("030", _dtime(14, 0), _dtime(16, 0))], None),
    )
    R._LAST_FRESH_AT = 0.0
    calls: list = []

    async def fake_fetch(request, store, day, seat_nums):
        calls.append(day)
        return [], None

    monkeypatch.setattr(R, "_fetch_others_occupied", fake_fetch)

    async def main():
        # fresh 缺省: 90s TTL 命中, 不发请求
        await R._fetch_others_occupied_cached(
            None, None, _date(2026, 9, 1), ["030"])
        # fresh=True: 绕过缓存强制拉取并记录节流时间戳
        await R._fetch_others_occupied_cached(
            None, None, _date(2026, 9, 1), ["030"], fresh=True)
        # 30s 节流: 刚强制过, 再点也回落缓存
        await R._fetch_others_occupied_cached(
            None, None, _date(2026, 9, 1), ["030"], fresh=True)
        return calls

    assert asyncio.run(main()) == [_date(2026, 9, 1)]
    R._OCC_CACHE.clear()