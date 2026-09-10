import asyncio
from datetime import date, time

import pytest

from seatbot.models import Account, Task, TaskStatus
from seatbot.store import StateStore


@pytest.fixture
async def store(tmp_path):
    db = tmp_path / "test.db"
    s = StateStore(str(db))
    await s.init()
    yield s
    await s.close()


async def test_init_creates_tables(store: StateStore):
    tables = await store.list_tables()
    assert {"accounts", "tasks", "actions", "logs"}.issubset(set(tables))


async def test_account_upsert_and_get(store: StateStore):
    acc = Account(id="zs", phone="138", password="p", slots="full")
    await store.upsert_account(acc)
    loaded = await store.get_account("zs")
    assert loaded is not None
    assert loaded.id == "zs"
    assert loaded.slots == "full"


async def test_list_accounts(store: StateStore):
    for i in range(3):
        await store.upsert_account(
            Account(id=f"a{i}", phone=str(i), password="p", slots="full")
        )
    accs = await store.list_accounts()
    assert {a.id for a in accs} == {"a0", "a1", "a2"}


async def test_task_lifecycle(store: StateStore):
    acc = Account(id="zs", phone="1", password="p", slots="full")
    await store.upsert_account(acc)
    t = Task(id=None, account_id="zs", day=date(2026, 7, 9),
             start_time=time(8, 0), end_time=time(10, 0))
    tid = await store.add_task(t)
    assert tid is not None
    await store.update_task_status(tid, TaskStatus.ACTIVE, reserve_id=12345)
    got = await store.get_task(tid)
    assert got.status == TaskStatus.ACTIVE
    assert got.reserve_id == 12345


async def test_list_tasks_by_account_day(store: StateStore):
    acc = Account(id="zs", phone="1", password="p", slots="full")
    await store.upsert_account(acc)
    for h in (8, 10, 14):
        await store.add_task(Task(
            id=None, account_id="zs", day=date(2026, 7, 9),
            start_time=time(h, 0), end_time=time(h + 2, 0),
        ))
    tasks = await store.list_tasks(account_id="zs", day=date(2026, 7, 9))
    assert len(tasks) == 3


async def test_sync_accounts(store: StateStore):
    """sync_accounts should upsert a batch from config."""
    accs = [
        Account(id="a", phone="1", password="p", slots="full"),
        Account(id="b", phone="2", password="p",
                slots=["08:00-12:00", "14:00-22:00"]),
    ]
    n = await store.sync_accounts(accs)
    assert n == 2
    loaded = await store.list_accounts()
    assert {a.id for a in loaded} == {"a", "b"}
    b = await store.get_account("b")
    assert b is not None
    assert b.slots == ["08:00-12:00", "14:00-22:00"]

    # Re-sync: should still have 2, not 4 (idempotent)
    n2 = await store.sync_accounts(accs)
    assert n2 == 2
    loaded2 = await store.list_accounts()
    assert len(loaded2) == 2


async def test_log_action(store: StateStore):
    await store.log_action("zs", "submit", '{"x":1}', '{"y":2}', True, "ok")
    rows = await store.list_actions(account_id="zs", limit=10)
    assert len(rows) == 1
    assert rows[0].action == "submit"
    assert rows[0].success is True


async def test_log_message(store: StateStore):
    await store.log_message("INFO", "zs", "hello world")
    rows = await store.list_logs(account_id="zs", limit=10)
    assert len(rows) == 1
    assert rows[0].level == "INFO"


async def test_list_logs_by_day(store: StateStore):
    from seatbot.utils.timeutil import CST, today_cst
    from datetime import datetime, timedelta
    today = today_cst()
    yesterday = today - timedelta(days=1)

    ts_today = int(datetime(today.year, today.month, today.day, 10, 0, tzinfo=CST).timestamp() * 1000)
    ts_yesterday = int(datetime(yesterday.year, yesterday.month, yesterday.day, 10, 0, tzinfo=CST).timestamp() * 1000)

    await store.db.execute("INSERT INTO logs (ts, level, account_id, message) VALUES (?, ?, ?, ?)", (ts_today, "INFO", "zs", "today log"))
    await store.db.execute("INSERT INTO logs (ts, level, account_id, message) VALUES (?, ?, ?, ?)", (ts_yesterday, "WARN", "zs", "yesterday log"))
    await store.db.commit()

    today_logs = await store.list_logs(day=today)
    assert len(today_logs) == 1
    assert today_logs[0].message == "today log"

    yesterday_logs = await store.list_logs(day=yesterday.isoformat())
    assert len(yesterday_logs) == 1
    assert yesterday_logs[0].message == "yesterday log"

    days = await store.list_log_days()
    assert today.isoformat() in days
    assert yesterday.isoformat() in days

async def test_update_task_status_clears_last_error_on_success(store: StateStore):
    """成功路径传 last_error="" 应清空历史错误; None 保持不变。"""
    acc = Account(id="zs", phone="1", password="p", slots="full")
    await store.upsert_account(acc)
    t = Task(id=None, account_id="zs", day=date(2026, 8, 27),
             start_time=time(9, 0), end_time=time(11, 0), seat_num="104",
             status=TaskStatus.PENDING)
    tid = await store.add_task(t)

    await store.update_task_status(tid, TaskStatus.FAILED, last_error="boom")
    assert (await store.get_task(tid)).last_error == "boom"

    # None = keep (失败重试场景不抹审计线索)
    await store.update_task_status(tid, TaskStatus.PENDING)
    assert (await store.get_task(tid)).last_error == "boom"

    # "" = clear (成功转 ACTIVE/SIGNED/COMPLETE 不残留旧文案)
    await store.update_task_status(tid, TaskStatus.ACTIVE, reserve_id=99, last_error="")
    got = await store.get_task(tid)
    assert got.status == TaskStatus.ACTIVE and got.reserve_id == 99
    assert got.last_error is None

def test_dismiss_notification_removes_row(tmp_path):
    async def run():
        s = StateStore(str(tmp_path / "dismiss.db"))
        await s.init()
        try:
            await s.add_notification("签到终止", level="error")
            await s.add_notification("提示", level="warn")
            rows = await s.list_notifications()
            target = rows[0]["id"]
            deleted = await s.dismiss_notification(target)
            remaining = await s.list_notifications()
            return deleted, target, [r["id"] for r in rows], [r["id"] for r in remaining]
        finally:
            await s.close()

    deleted, target, before, after = asyncio.run(run())
    assert deleted is True
    assert len(after) == len(before) - 1
    assert set(before) - set(after) == {target}


def test_dismiss_notification_unknown_id_is_noop(tmp_path):
    async def run():
        s = StateStore(str(tmp_path / "dismiss2.db"))
        await s.init()
        try:
            await s.add_notification("仅一条")
            deleted = await s.dismiss_notification(987654)
            return deleted, len(await s.list_notifications())
        finally:
            await s.close()

    deleted, count = asyncio.run(run())
    assert deleted is False
    assert count == 1


def test_add_notification_caps_table_at_30(tmp_path):
    async def run():
        s = StateStore(str(tmp_path / "cap.db"))
        await s.init()
        try:
            for i in range(35):
                await s.add_notification(f"n{i}")
            return await s.list_notifications(limit=100)
        finally:
            await s.close()

    rows = asyncio.run(run())
    assert len(rows) == 30
    assert rows[0]["title"] == "n34"


def test_dismiss_all_notifications(tmp_path):
    async def run():
        s = StateStore(str(tmp_path / "all.db"))
        await s.init()
        try:
            await s.add_notification("a")
            await s.add_notification("b")
            deleted = await s.dismiss_all_notifications()
            return deleted, await s.list_notifications(limit=10)
        finally:
            await s.close()

    deleted, rows = asyncio.run(run())
    assert deleted == 2
    assert rows == []


def test_weekly_form_migration_normalizes_legacy_lists(tmp_path):
    """存量 list 形态启动后归一为 7 键 dict；幂等。"""
    import json

    async def run():
        db = tmp_path / "weekly_mig.db"
        s = StateStore(str(db))
        await s.init()
        try:
            # 直接写入旧形态模拟存量数据
            await s.db.execute(
                "INSERT INTO accounts (id, phone, password, slots_json, seat_slots_json,"
                " bound_seats_json, status, created_at, updated_at)"
                " VALUES ('old', '138', 'p', 'full', ?, '[]', 'active', 0, 0)",
                (json.dumps({"021": ["09:00-11:00"]}),),
            )
            await s.db.execute(
                "INSERT INTO target_seats (seat_num, label, enabled, created_at,"
                " updated_at, desired_slots_json) VALUES ('021', '', 1, 0, 0, ?)",
                (json.dumps(["10:00-12:00"]),),
            )
            await s.db.commit()
            await s.close()

            s2 = StateStore(str(db))
            await s2.init()   # 迁移发生点
            try:
                acc = await s2.get_account("old")
                assert acc.seat_slots["021"]["mon"] == ["09:00-11:00"]
                assert acc.seat_slots["021"]["sun"] == ["09:00-11:00"]
                seats = await s2.list_target_seats()
                assert seats[0].desired_slots["tue"] == ["10:00-12:00"]
            finally:
                await s2.close()
        finally:
            if s._db:
                await s.close()

    asyncio.run(run())


def test_set_target_seat_desired_accepts_dict_and_none(tmp_path):

    async def run():
        s = StateStore(str(tmp_path / "weekly_desired.db"))
        await s.init()
        try:
            await s.add_target_seat("021")
            await s.set_target_seat_desired(
                "021", {"mon": ["09:00-11:00"], "sun": []})
            seats = await s.list_target_seats()
            # dict 形态自动识别为按天（weekly），存于 weekly 列
            assert seats[0].desired_slots_weekly["mon"] == ["09:00-11:00"]
            assert seats[0].desired_slots_weekly["tue"] == []
            assert seats[0].desired_slots is None
            await s.set_target_seat_desired("021", None)
            seats = await s.list_target_seats()
            assert seats[0].desired_slots is None
            assert seats[0].desired_slots_weekly is None
        finally:
            await s.close()

    asyncio.run(run())


async def test_active_task_identity_unique_index(tmp_path):
    """库级 E4 防线：活跃态同 (账号,日,座位,开始) 第二行被拒；终态行不受限。"""
    s = StateStore(str(tmp_path / "uq.db"))
    await s.init()
    try:
        base = dict(account_id="u1", day=date.today(), seat_num="001",
                    start_time=time(9, 0), end_time=time(11, 0))
        t1 = Task(id=None, status=TaskStatus.ACTIVE, **base)
        t1.id = await s.add_task(t1)
        assert t1.id
        dup = Task(id=None, status=TaskStatus.PENDING, **base)
        with pytest.raises(Exception):
            await s.add_task(dup)
        # 终态行不受唯一索引约束（complete/failed 历史可重开同键任务）
        done = Task(id=None, status=TaskStatus.COMPLETE, **base)
        done.id = await s.add_task(done)
        assert done.id
    finally:
        await s.close()


async def test_account_status_lifecycle(tmp_path):
    """三态生命周期：双口径过滤、墓碑全视图不可见、set_account_status 联动版本。"""
    s = StateStore(str(tmp_path / "st.db"))
    await s.init()
    try:
        from seatbot.models import Account
        await s.upsert_account(Account(id="zs", phone="138", password="p", slots=[]))
        await s.upsert_account(Account(id="ls", phone="139", password="p", slots=[]))
        assert {a.id for a in await s.list_accounts()} == {"zs", "ls"}

        await s.set_account_status("ls", "inactive")
        assert {a.id for a in await s.list_accounts()} == {"zs"}
        both = await s.list_accounts(include_inactive=True)
        assert {a.id for a in both} == {"zs", "ls"}
        inactive = next(a for a in both if a.id == "ls")
        assert inactive.status == "inactive"
        # 禁用中账号 Web 视图仍可读（get_account 放行 inactive）
        assert (await s.get_account("ls")).status == "inactive"

        await s.set_account_status("ls", "disabled")
        assert {a.id for a in await s.list_accounts(include_inactive=True)} == {"zs"}
        assert await s.get_account("ls") is None
    finally:
        await s.close()


# ---------- hosted_reservations + tasks.source ----------

async def test_task_source_default_and_explicit(tmp_path):
    """tasks.source 默认 'matrix'；显式写入 'import' / 'adopt' 等。"""
    s = StateStore(str(tmp_path / "st.db"))
    await s.init()
    try:
        d = date(2026, 9, 10)
        # 默认 source
        t1 = Task(id=None, account_id="zs", day=d,
                  start_time=time(9, 0), end_time=time(11, 0))
        t1.id = await s.add_task(t1)
        loaded = await s.get_task(t1.id)
        assert loaded.source == "matrix"

        # 显式 source=adopt
        t2 = Task(id=None, account_id="zs", day=d,
                  start_time=time(14, 0), end_time=time(16, 0),
                  source="adopt")
        t2.id = await s.add_task(t2)
        loaded2 = await s.get_task(t2.id)
        assert loaded2.source == "adopt"
    finally:
        await s.close()


async def test_update_task_status_source_keep_and_set(tmp_path):
    """update_task_status(source=None) 保持原值；source=显式值覆盖。"""
    s = StateStore(str(tmp_path / "st.db"))
    await s.init()
    try:
        d = date(2026, 9, 10)
        t = Task(id=None, account_id="zs", day=d,
                 start_time=time(9, 0), end_time=time(11, 0),
                 source="import")
        t.id = await s.add_task(t)
        # 保持：None
        await s.update_task_status(t.id, TaskStatus.ACTIVE, reserve_id=1)
        loaded = await s.get_task(t.id)
        assert loaded.source == "import"
        # 覆盖
        await s.update_task_status(t.id, TaskStatus.ACTIVE, source="adopt")
        loaded2 = await s.get_task(t.id)
        assert loaded2.source == "adopt"
    finally:
        await s.close()


async def test_hosted_upsert_idempotent(tmp_path):
    """upsert_hosted 按 (account_id, reserve_id) 命中则 UPDATE，不重插。"""
    s = StateStore(str(tmp_path / "st.db"))
    await s.init()
    try:
        d = date(2026, 9, 10)
        id1 = await s.upsert_hosted(
            "zs", 1001, seat_num="001", day=d,
            start=time(9, 0), end=time(11, 0),
            state="hosting", outcome="")
        # 再次写入：应命中同一行
        id2 = await s.upsert_hosted(
            "zs", 1001, seat_num="001", day=d,
            start=time(9, 0), end=time(11, 0),
            state="pending_decision", outcome="App 端取消")
        assert id1 == id2
        row = await s.get_hosted(id1)
        assert row["state"] == "pending_decision"
        assert row["outcome"] == "App 端取消"
    finally:
        await s.close()


async def test_hosted_list_with_state_filter_and_pagination(tmp_path):
    """list_hosted 按状态过滤、分页；倒序。"""
    s = StateStore(str(tmp_path / "st.db"))
    await s.init()
    try:
        d = date(2026, 9, 10)
        for i in range(5):
            await s.upsert_hosted(
                "zs", 2000 + i, seat_num="001", day=d,
                start=time(9, 0), end=time(10, 0),
                state="hosting", outcome="")
        for i in range(3):
            await s.upsert_hosted(
                "ls", 3000 + i, seat_num="002", day=d,
                start=time(11, 0), end=time(12, 0),
                state="ended", outcome="已履约")
        # 状态过滤
        hosting = await s.list_hosted(states=["hosting"])
        assert len(hosting) == 5
        ended = await s.list_hosted(states=["ended"])
        assert len(ended) == 3
        # 分页
        page1 = await s.list_hosted(states=["hosting"], limit=2, offset=0)
        page2 = await s.list_hosted(states=["hosting"], limit=2, offset=2)
        assert len(page1) == 2 and len(page2) == 2
        ids_p1 = {h["id"] for h in page1}
        ids_p2 = {h["id"] for h in page2}
        assert ids_p1.isdisjoint(ids_p2)
    finally:
        await s.close()


async def test_find_hosted_by_reserve(tmp_path):
    """按 (account_id, reserve_id) 查 hosted 行。"""
    s = StateStore(str(tmp_path / "st.db"))
    await s.init()
    try:
        d = date(2026, 9, 10)
        await s.upsert_hosted(
            "zs", 7777, seat_num="001", day=d,
            start=time(9, 0), end=time(11, 0),
            state="hosting", outcome="")
        row = await s.find_hosted_by_reserve("zs", 7777)
        assert row and row["reserve_id"] == 7777
        miss = await s.find_hosted_by_reserve("zs", 9999)
        assert miss is None
    finally:
        await s.close()


async def test_count_hosted_and_unlimited_list(tmp_path):
    """count_hosted 统计总数及状态过滤；list_hosted(limit=None) 全量返回。"""
    s = StateStore(str(tmp_path / "st.db"))
    await s.init()
    try:
        d = date(2026, 9, 10)
        for i in range(65):
            st = "stopped" if i < 15 else "ended"
            await s.upsert_hosted(
                "zs", 5000 + i, seat_num="001", day=d,
                start=time(9, 0), end=time(10, 0),
                state=st, outcome="")
        assert await s.count_hosted() == 65
        assert await s.count_hosted(states=["stopped"]) == 15
        assert await s.count_hosted(states=["ended"]) == 50
        # 默认 limit=50 只返回 50 行
        assert len(await s.list_hosted()) == 50
        # limit=None 全量返回 65 行
        all_rows = await s.list_hosted(limit=None)
        assert len(all_rows) == 65
    finally:
        await s.close()
