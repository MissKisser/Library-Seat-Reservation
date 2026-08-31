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
