"""实况同步单测：reservelist 生效预约 → user_reserved 自动补登/清退。

纯函数与 Scheduler.sync_user_reserved 均离线可测，不发任何网络请求。
"""
from __future__ import annotations

import asyncio
from datetime import date, time, timedelta

from seatbot.reconcile import (
    AUTO_SYNC_NOTE,
    diff_user_reserved,
    parse_reservations,
)
from seatbot.utils.timeutil import at_cst, today_cst

SEAT_A = 42
SEAT_B = 43


def _entry(rid: int, seat: int, day: date, start: time, end: time,
           status: int = 0) -> dict:
    """构造一条 reservelist 原始条目（字段名与 docs/api/reservelist.md 一致）。"""
    return {
        "id": rid,
        "seatNum": seat,
        "today": day.isoformat(),
        "startTime": int(at_cst(day, start).timestamp() * 1000),
        "endTime": int(at_cst(day, end).timestamp() * 1000),
        "status": status,
    }


def _days() -> set[date]:
    return {today_cst(), today_cst() + timedelta(days=1)}


# ---------- parse_reservations ----------

def test_parse_keeps_active_entry_and_normalizes_fields():
    day = today_cst()
    parsed = parse_reservations(
        [_entry(101, SEAT_A, day, time(19, 30), time(21, 30), status=0)],
        _days(),
    )
    assert parsed == [{
        "reserve_id": 101, "seat_num": "042", "day": day,
        "start": time(19, 30), "end": time(21, 30),
        "status": 0,
    }]


def test_parse_drops_terminal_status_and_out_of_window():
    day = today_cst()
    far = day + timedelta(days=2)
    parsed = parse_reservations([
        _entry(201, SEAT_A, day, time(9, 0), time(11, 0), status=7),
        _entry(202, SEAT_A, day, time(9, 0), time(11, 0), status=2),
        _entry(203, SEAT_A, day, time(9, 0), time(11, 0), status=8),
        _entry(204, SEAT_A, far, time(9, 0), time(11, 0), status=0),
    ], _days())
    assert parsed == []


def test_parse_keeps_in_progress_and_supervised():
    day = today_cst()
    parsed = parse_reservations([
        _entry(301, SEAT_A, day, time(9, 0), time(11, 0), status=1),
        _entry(302, SEAT_A, day, time(15, 0), time(17, 0), status=5),
        _entry(303, SEAT_A, day, time(19, 0), time(21, 0), status=3),
    ], _days())
    assert {p["reserve_id"] for p in parsed} == {301, 302, 303}


# ---------- diff_user_reserved ----------

def _row(seat: str, day: date, start: str, end: str,
         note: str = "", rid: int = 1) -> dict:
    return {"id": rid, "account_id": "张三", "seat_num": seat,
            "day": day.isoformat(), "start_time": start,
            "end_time": end, "note": note, "created_at": 0}


def test_diff_adds_untracked_reservation():
    day = today_cst()
    parsed = parse_reservations(
        [_entry(101, SEAT_A, day, time(19, 30), time(21, 30))], _days())
    to_add, to_del = diff_user_reserved(parsed, set(), [])
    assert len(to_add) == 1
    assert to_add[0]["reserve_id"] == 101
    assert to_del == []


def test_diff_skips_task_tracked_reserve_id():
    day = today_cst()
    parsed = parse_reservations(
        [_entry(101, SEAT_A, day, time(19, 30), time(21, 30))], _days())
    to_add, _ = diff_user_reserved(parsed, {101}, [])
    assert to_add == []


def test_diff_skips_overlapping_existing_row():
    day = today_cst()
    parsed = parse_reservations(
        [_entry(101, SEAT_A, day, time(19, 30), time(21, 30))], _days())
    manual = [_row("042", day, "19:00", "21:00", note="手动登记", rid=9)]
    to_add, to_del = diff_user_reserved(parsed, set(), manual)
    assert to_add == [] and to_del == []


def test_diff_prunes_only_stale_auto_rows():
    day = today_cst()
    auto = [_row("042", day, "19:30", "21:30", note=AUTO_SYNC_NOTE, rid=5)]
    manual = [_row("042", day, "09:00", "11:00", note="", rid=6)]
    # 本轮 reservelist 已查不到该自动行（取消/履约）：只清自动行
    to_add, to_del = diff_user_reserved([], set(), auto + manual)
    assert to_add == [] and to_del == [5]


def test_diff_keeps_auto_row_still_present():
    day = today_cst()
    parsed = parse_reservations(
        [_entry(101, SEAT_A, day, time(19, 30), time(21, 30))], _days())
    auto = [_row("042", day, "19:30", "21:30", note=AUTO_SYNC_NOTE, rid=5)]
    to_add, to_del = diff_user_reserved(parsed, set(), auto)
    assert to_add == [] and to_del == []


# ---------- Scheduler.sync_user_reserved 端到端（离线替身） ----------

def _make_cfg():
    from seatbot.config import Config, LibraryConfig, RuntimeConfig
    return Config(
        library=LibraryConfig(room_id=11692, room_name="t"),
        runtime=RuntimeConfig(stagger_seconds=[0, 0]),
    )


class FakeReserveClient:
    """离线替身：cookies 恒有，reserve_list 按脚本返回。"""

    script: list = []

    def cookies(self):
        return {"_uid": "x"}

    async def reserve_list(self):
        return FakeReserveClient.script.pop(0) if FakeReserveClient.script else []


def test_sync_registers_then_prunes(tmp_path, monkeypatch):
    async def main():
        from seatbot.models import Account, Task, TaskStatus
        from seatbot.scheduler import Scheduler
        from seatbot.store import StateStore

        store = StateStore(str(tmp_path / "t.db"))
        await store.init()
        try:
            await store.upsert_account(Account(
                id="张三", phone="1", password="p", slots=[]))
            day = today_cst()
            tid = await store.add_task(Task(
                id=None, account_id="张三", day=day,
                start_time=time(14, 0), end_time=time(16, 0)))
            await store.update_task_status(
                tid, TaskStatus.ACTIVE, reserve_id=101)
            sched = Scheduler(_make_cfg(), store)

            async def fake_client_ready(self, acc):
                return FakeReserveClient()

            monkeypatch.setattr(Scheduler, "client_ready", fake_client_ready)

            # 101 已被本地任务托管（跳过）、102 无人托管（补登）、103 已取消（忽略）
            FakeReserveClient.script = [[
                _entry(101, SEAT_A, day, time(14, 0), time(16, 0)),
                _entry(102, SEAT_B, day, time(19, 30), time(21, 30)),
                _entry(103, SEAT_A, day, time(9, 0), time(11, 0), status=7),
            ]]
            out = await sched.sync_user_reserved()
            assert out["added"] == 1
            rows = await store.list_user_reserved()
            assert len(rows) == 1
            assert (rows[0]["seat_num"], rows[0]["start_time"],
                    rows[0]["end_time"], rows[0]["note"]) == (
                "043", "19:30", "21:30", AUTO_SYNC_NOTE)

            # 下一轮预约消失：自动行被清退，计数正确
            FakeReserveClient.script = [[]]
            out = await sched.sync_user_reserved()
            # 102 已被托管采纳；消失后转 pending_decision
            assert out["added"] == 0 and out["pruned"] == 1
            assert out["adopted"] == 0 and out["queued"] == 0
            assert out["pending"] == 1 and out["ended"] == 0
        finally:
            await store.close()
    asyncio.run(main())


def test_sync_skips_account_on_query_failure(tmp_path, monkeypatch):
    async def main():
        from seatbot.models import Account
        from seatbot.scheduler import Scheduler
        from seatbot.store import StateStore

        store = StateStore(str(tmp_path / "t.db"))
        await store.init()
        try:
            await store.upsert_account(Account(
                id="张三", phone="1", password="p", slots=[]))
            sched = Scheduler(_make_cfg(), store)

            class DeadClient:
                def cookies(self):
                    return {"_uid": "x"}

                async def reserve_list(self):
                    raise RuntimeError("会话失效")

            async def fake_client_ready(self, acc):
                return DeadClient()

            monkeypatch.setattr(Scheduler, "client_ready", fake_client_ready)
            out = await sched.sync_user_reserved()
            assert out == {"added": 0, "pruned": 0,
                           "adopted": 0, "queued": 0,
                           "pending": 0, "ended": 0}
            assert await store.list_user_reserved() == []
        finally:
            await store.close()
    asyncio.run(main())


def test_sync_ignores_manual_rows_and_skips_no_credential_accounts(
        tmp_path, monkeypatch):
    async def main():
        from seatbot.models import Account
        from seatbot.scheduler import Scheduler
        from seatbot.store import StateStore

        store = StateStore(str(tmp_path / "t.db"))
        await store.init()
        try:
            await store.upsert_account(Account(
                id="张三", phone="1", password="p", slots=[]))
            await store.upsert_account(Account(
                id="李四", phone="", password="", slots=[]))
            day = today_cst()
            await store.add_user_reserved(
                "张三", "042", day, time(19, 0), time(21, 0), note="手动登记")

            calls: list = []

            class ProbeClient:
                def cookies(self):
                    return {"_uid": "x"}

                async def reserve_list(self):
                    calls.append(1)
                    return []

            sched = Scheduler(_make_cfg(), store)

            async def fake_client_ready(self, acc):
                return ProbeClient()

            monkeypatch.setattr(Scheduler, "client_ready", fake_client_ready)
            out = await sched.sync_user_reserved()
            assert out == {"added": 0, "pruned": 0,
                           "adopted": 0, "queued": 0,
                           "pending": 0, "ended": 0}
            # 无凭据账号未发起查询；手动行原样保留
            assert len(calls) == 1
            rows = await store.list_user_reserved()
            assert len(rows) == 1 and rows[0]["note"] == "手动登记"
        finally:
            await store.close()
    asyncio.run(main())


def test_tick_runs_sync_after_sweep(tmp_path, monkeypatch):
    """节拍器到期时 sweep 与 sync 同轮执行。"""
    import seatbot.scheduler as S
    from seatbot.scheduler import Scheduler
    from seatbot.store import StateStore

    async def main():
        store = StateStore(str(tmp_path / "t.db"))
        await store.init()
        try:
            sched = Scheduler(_make_cfg(), store)
            calls = {"sweep": 0, "sync": 0}

            async def fake_sweep():
                calls["sweep"] += 1

            async def fake_sync():
                calls["sync"] += 1
                return {"added": 0, "pruned": 0}

            monkeypatch.setattr(sched, "reconcile_sweep", fake_sweep)
            monkeypatch.setattr(sched, "sync_user_reserved", fake_sync)
            monkeypatch.setattr(S, "now_cst",
                                lambda: at_cst(today_cst(), time(9, 0)))
            await sched.reconcile_tick()
            assert calls == {"sweep": 1, "sync": 1}
        finally:
            await store.close()
    asyncio.run(main())


# ---------- plan_adoption 纯函数 ----------

def test_plan_adoption_creates_new_for_untracked_reservation():
    """未跟踪的 reservelist 条目 → create。"""
    from seatbot.reconcile import plan_adoption
    day = today_cst()
    parsed = [{
        "reserve_id": 1, "seat_num": "001", "day": day,
        "start": time(9, 0), "end": time(10, 0), "status": 0,
    }]
    actions = plan_adoption("zs", parsed, set(), set(), set(), [],
                             now=at_cst(day, time(8, 0)))
    assert len(actions) == 1
    assert actions[0]["kind"] == "create"
    assert actions[0]["status"] == "active"  # status=0 → active


def test_plan_adoption_status_mapping_for_in_progress():
    """status=1/3 → signed（已签到，仅管签退）；status=0/5 → active。"""
    from seatbot.reconcile import plan_adoption
    day = today_cst()
    parsed = [
        {"reserve_id": 1, "seat_num": "001", "day": day,
         "start": time(9, 0), "end": time(10, 0), "status": 1},
        {"reserve_id": 2, "seat_num": "001", "day": day,
         "start": time(11, 0), "end": time(12, 0), "status": 3},
        {"reserve_id": 3, "seat_num": "001", "day": day,
         "start": time(13, 0), "end": time(14, 0), "status": 5},
    ]
    actions = plan_adoption("zs", parsed, set(), set(), set(), [],
                             now=at_cst(day, time(8, 0)))
    statuses = {a["reserve_id"]: a["status"] for a in actions}
    assert statuses[1] == "signed"
    assert statuses[2] == "signed"
    assert statuses[3] == "active"
def test_plan_adoption_skips_known_stopped_and_ended():
    """known_reserve_ids / stopped 拦截；queued 允许重新采纳。"""
    from seatbot.reconcile import plan_adoption
    day = today_cst()
    parsed = [
        {"reserve_id": 1, "seat_num": "001", "day": day,
         "start": time(9, 0), "end": time(10, 0), "status": 0},
        {"reserve_id": 2, "seat_num": "001", "day": day,
         "start": time(11, 0), "end": time(12, 0), "status": 0},
    ]
    now = at_cst(day, time(8, 0))
    # 1 已被任务跟踪 → 跳过
    a1 = plan_adoption("zs", parsed[:1], {1}, set(), set(), [], now=now)
    assert a1 == []
    # 2 被 stopped → 跳过
    a2 = plan_adoption("zs", parsed[1:], set(), {2}, set(), [], now=now)
    assert a2 == []
    # queued 包含 2 → 允许采纳
    a3 = plan_adoption("zs", parsed[1:], set(), {2}, {2}, [], now=now)

def test_plan_adoption_skips_ended():
    """时段已结束（now ≥ end）→ 跳过。"""
    from seatbot.reconcile import plan_adoption
    day = today_cst()
    parsed = [{"reserve_id": 1, "seat_num": "001", "day": day,
                "start": time(9, 0), "end": time(10, 0), "status": 0}]
    now = at_cst(day, time(11, 0))
    actions = plan_adoption("zs", parsed, set(), set(), set(), [], now=now)
    assert actions == []


def test_plan_adoption_converts_pending_matrix_task():
    """同键 PENDING 任务 → convert（不新建任务）。"""
    from seatbot.reconcile import plan_adoption
    day = today_cst()
    parsed = [{"reserve_id": 1, "seat_num": "001", "day": day,
                "start": time(9, 0), "end": time(10, 0), "status": 0}]
    now = at_cst(day, time(8, 0))
    account_tasks = [{
        "id": 99, "account_id": "zs", "day": day,
        "seat_num": "001", "start_time": time(9, 0),
        "status": "pending", "reserve_id": None,
    }]
    actions = plan_adoption("zs", parsed, set(), set(), set(),
                             account_tasks, now=now)
    assert len(actions) == 1
    assert actions[0]["kind"] == "convert"
    assert actions[0]["task_id"] == 99


# ---------- sync_user_reserved 托管路径端到端 ----------

def test_sync_adopts_new_reservation_creates_task_and_hosted(tmp_path, monkeypatch):
    """实况同步：未跟踪预约 → 自动托管（建托管任务 + hosted 行 + 占位行）。"""
    async def main():
        from datetime import datetime
        from seatbot.models import Account
        from seatbot.scheduler import Scheduler
        from seatbot.store import StateStore

        store = StateStore(str(tmp_path / "t.db"))
        await store.init()
        try:
            await store.upsert_account(Account(
                id="张三", phone="1", password="p", slots=[]))
            sched = Scheduler(_make_cfg(), store)

            async def fake_client_ready(self, acc):
                return FakeReserveClient()

            monkeypatch.setattr(Scheduler, "client_ready", fake_client_ready)
            day = today_cst()
            FakeReserveClient.script = [[
                _entry(501, SEAT_A, day, time(14, 0), time(16, 0), status=0),
            ]]
            out = await sched.sync_user_reserved()
            assert out["adopted"] == 1
            hosted = await store.list_hosted(states=["hosting"])
            assert len(hosted) == 1
            assert hosted[0]["reserve_id"] == 501
            assert hosted[0]["account_id"] == "张三"
            # 任务应存在
            tasks = await store.list_tasks(account_id="张三", day=day)
            adopted = [t for t in tasks if t.reserve_id == 501]
            assert len(adopted) == 1
            assert adopted[0].source == "adopt"
            assert adopted[0].status.value == "active"
            # 占位行
            ur = await store.list_user_reserved(day=day, seat_num="042")
            assert any(r["note"] == "实况自动同步" for r in ur)
        finally:
            await store.close()
    asyncio.run(main())


def test_sync_adopts_in_progress_reservation_marks_signed(tmp_path, monkeypatch):
    """status=1/3 采纳时任务初始状态 = SIGNED。"""
    async def main():
        from seatbot.models import Account
        from seatbot.scheduler import Scheduler
        from seatbot.store import StateStore

        store = StateStore(str(tmp_path / "t.db"))
        await store.init()
        try:
            await store.upsert_account(Account(
                id="张三", phone="1", password="p", slots=[]))
            sched = Scheduler(_make_cfg(), store)

            async def fake_client_ready(self, acc):
                return FakeReserveClient()

            monkeypatch.setattr(Scheduler, "client_ready", fake_client_ready)
            day = today_cst()
            FakeReserveClient.script = [[
                _entry(601, SEAT_A, day, time(15, 0), time(17, 0), status=1),
            ]]
            await sched.sync_user_reserved()
            tasks = await store.list_tasks(account_id="张三", day=day)
            adopted = [t for t in tasks if t.reserve_id == 601]
            assert len(adopted) == 1
            assert adopted[0].status.value == "signed"
        finally:
            await store.close()
    asyncio.run(main())


def test_sync_pending_decision_when_reservation_disappears(tmp_path, monkeypatch):
    """托管行 hosting 但 reservelist 消失（未开始）→ 转 pending_decision + 任务 FAILED。"""
    async def main():
        from seatbot.models import Account, Task, TaskStatus
        from seatbot.scheduler import Scheduler
        from seatbot.store import StateStore

        store = StateStore(str(tmp_path / "t.db"))
        await store.init()
        try:
            await store.upsert_account(Account(
                id="张三", phone="1", password="p", slots=[]))
            sched = Scheduler(_make_cfg(), store)

            async def fake_client_ready(self, acc):
                return FakeReserveClient()

            monkeypatch.setattr(Scheduler, "client_ready", fake_client_ready)
            day = today_cst()
            # 第一轮：采纳
            FakeReserveClient.script = [[
                _entry(701, SEAT_A, day, time(15, 0), time(17, 0), status=0),
            ]]
            await sched.sync_user_reserved()
            tasks = await store.list_tasks(account_id="张三", day=day)
            assert tasks and tasks[0].status.value == "active"
            # 第二轮：预约从 reservelist 消失，且 now < start → pending_decision
            FakeReserveClient.script = [[]]
            out = await sched.sync_user_reserved()
            assert out["pending"] == 1
            hosted = await store.list_hosted()
            assert hosted[0]["state"] == "pending_decision"
            assert hosted[0]["outcome"] == "App 端预约已取消"
            t = await store.get_task(tasks[0].id)
            assert t.status.value == "failed"
            assert t.last_error == "App 端预约已取消"
        finally:
            await store.close()
    asyncio.run(main())


def test_sync_pending_decision_timeout_auto_abandon(tmp_path, monkeypatch):
    """pending_decision 行 now ≥ start → 自动 stopped + 通知。"""
    async def main():
        import seatbot.scheduler as S
        from seatbot.models import Account, Task, TaskStatus
        from seatbot.scheduler import Scheduler
        from seatbot.store import StateStore

        store = StateStore(str(tmp_path / "t.db"))
        await store.init()
        try:
            await store.upsert_account(Account(
                id="张三", phone="1", password="p", slots=[]))
            day = today_cst()
            # 直接预置 pending_decision 行（时段已过）
            await store.upsert_hosted(
                "张三", 801, seat_num="042", day=day,
                start=time(8, 0), end=time(9, 0),
                state="pending_decision", outcome="App 端取消")
            sched = Scheduler(_make_cfg(), store)

            async def fake_client_ready(self, acc):
                return FakeReserveClient()

            monkeypatch.setattr(Scheduler, "client_ready", fake_client_ready)
            FakeReserveClient.script = [[]]
            # now 固定在 11:00（晚于 9:00 end）
            monkeypatch.setattr(S, "now_cst",
                                lambda: at_cst(day, time(11, 0)))
            out = await sched.sync_user_reserved()
            hosted = await store.list_hosted()
            assert hosted[0]["state"] == "stopped"
            assert hosted[0]["outcome"] == "未确认自动放弃"
        finally:
            await store.close()
    asyncio.run(main())


def test_sync_archived_ended_after_task_completed(tmp_path, monkeypatch):
    """托管任务 COMPLETE → hosted 转 ended。"""
    async def main():
        from seatbot.models import Account, Task, TaskStatus
        from seatbot.scheduler import Scheduler
        from seatbot.store import StateStore

        store = StateStore(str(tmp_path / "t.db"))
        await store.init()
        try:
            await store.upsert_account(Account(
                id="张三", phone="1", password="p", slots=[]))
            day = today_cst()
            await store.upsert_hosted(
                "张三", 901, seat_num="042", day=day,
                start=time(15, 0), end=time(16, 0),
                state="hosting", outcome="")
            tid = await store.add_task(Task(
                id=None, account_id="张三", day=day,
                start_time=time(15, 0), end_time=time(16, 0),
                seat_num="042", status=TaskStatus.COMPLETE,
                reserve_id=901, source="adopt"))
            await store.update_hosted(
                (await store.list_hosted())[0]["id"], task_id=tid)
            sched = Scheduler(_make_cfg(), store)

            async def fake_client_ready(self, acc):
                return FakeReserveClient()

            monkeypatch.setattr(Scheduler, "client_ready", fake_client_ready)
            FakeReserveClient.script = [[]]
            out = await sched.sync_user_reserved()
            hosted = await store.list_hosted()
            assert hosted[0]["state"] == "ended"
            assert hosted[0]["outcome"] == "已履约"
        finally:
            await store.close()
    asyncio.run(main())


def test_sync_does_not_clear_rows_on_account_failure(tmp_path, monkeypatch):
    """单账号查询失败：不清退既有 hosted 行（既有语义保留）。"""
    async def main():
        from seatbot.models import Account
        from seatbot.scheduler import Scheduler
        from seatbot.store import StateStore

        store = StateStore(str(tmp_path / "t.db"))
        await store.init()
        try:
            await store.upsert_account(Account(
                id="张三", phone="1", password="p", slots=[]))
            day = today_cst()
            await store.upsert_hosted(
                "张三", 1001, seat_num="042", day=day,
                start=time(15, 0), end=time(16, 0),
                state="hosting", outcome="")
            sched = Scheduler(_make_cfg(), store)

            class DeadClient:
                def cookies(self):
                    return {"_uid": "x"}

                async def reserve_list(self):
                    raise RuntimeError("会话失效")

            async def fake_client_ready(self, acc):
                return DeadClient()

            monkeypatch.setattr(Scheduler, "client_ready", fake_client_ready)
            await sched.sync_user_reserved()
            hosted = await store.list_hosted()
            assert hosted[0]["state"] == "hosting"
        finally:
            await store.close()
    asyncio.run(main())


def test_sync_inactive_account_no_name_error(tmp_path, monkeypatch):
    """禁用账号同步：actions 初始化正常，不抛 NameError，日志无 NameError 告警。"""
    async def main():
        from seatbot.models import Account
        from seatbot.scheduler import Scheduler
        from seatbot.store import StateStore

        store = StateStore(str(tmp_path / "t.db"))
        await store.init()
        try:
            await store.upsert_account(Account(
                id="张三", phone="1", password="p", slots=[], status="inactive"))
            sched = Scheduler(_make_cfg(), store)

            async def fake_client_ready(self, acc):
                return FakeReserveClient()

            monkeypatch.setattr(Scheduler, "client_ready", fake_client_ready)
            FakeReserveClient.script = [[]]
            out = await sched.sync_user_reserved()
            assert out == {"added": 0, "pruned": 0, "adopted": 0, "queued": 0, "pending": 0, "ended": 0}
            logs = await store.list_logs()
            assert not any("NameError" in l.message for l in logs)
        finally:
            await store.close()
    asyncio.run(main())
