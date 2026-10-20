"""测试共享夹具：守护账号矩阵、种子写入与常用 pytest 夹具。

被 tests/conftest.py 重导出，供全部测试模块直接使用 `store` / `nosleep` 夹具。
"""
from __future__ import annotations

import pytest

from seatbot.models import Account
from seatbot.store import StateStore

GUARD_MATRIX = {
    "zhangsan": {"104": ["09:00-11:00"], "105": ["15:00-17:00"]},
    "lisi":   {"104": ["15:00-17:00"], "105": ["19:00-21:00"]},
    "wangwu":  {"104": ["19:00-21:00"], "105": ["09:00-11:00"]},
}


async def seed_guard_accounts(store: StateStore) -> None:
    """把守护账号矩阵与两个目标座位写入库。

    入参 store 为已初始化的状态库实例。
    """
    for i, (acc_id, seat_slots) in enumerate(GUARD_MATRIX.items()):
        await store.upsert_account(Account(
            id=acc_id, phone=f"1380000000{i}", password="p",
            slots=[], bound_seats=list(seat_slots.keys()),
            seat_slots=seat_slots,
        ))
    await store.add_target_seat("104")
    await store.add_target_seat("105")


@pytest.fixture
async def store(tmp_path):
    """提供隔离的临时状态库，已预置守护账号与目标座位。"""
    s = StateStore(str(tmp_path / "t.db"))
    await s.init()
    await seed_guard_accounts(s)
    yield s
    await s.close()


@pytest.fixture
def nosleep(monkeypatch):
    """把调度器里的 asyncio.sleep 替换为空操作，加速签到/签退相关用例。"""
    async def _sleep(_s):
        return
    monkeypatch.setattr("seatbot.scheduler.asyncio.sleep", _sleep)
