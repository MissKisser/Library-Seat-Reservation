"""配置真源与墓碑语义回归：软删除不复活、配置文件不覆盖既有矩阵。

对应验收报告 A-06：启动种子只增不覆盖，删除的账号/座位不因配置文件再次出现。
"""
import pytest

from seatbot.bindings import desired_slots_of
from seatbot.config import AccountConfig
from seatbot.models import Account
from seatbot.store import StateStore


@pytest.fixture
async def store(tmp_path):
    """提供隔离的临时状态库。"""
    s = StateStore(str(tmp_path / "tombstone.db"))
    await s.init()
    yield s
    await s.close()


def _cfg_account(acc_id: str, **kw) -> AccountConfig:
    """构造一个「配置文件形态」的账号种子项。"""
    payload = {
        "id": acc_id,
        "phone": "13800000000",
        "password": "p",
        "slots": [],
        "bound_seats": [],
        "seat_slots": None,
    }
    payload.update(kw)
    return AccountConfig(**payload)


async def test_deleted_account_is_not_resurrected_by_config(store: StateStore):
    """已软删除的账号不因配置文件再次出现而复活。"""
    await store.upsert_account(Account(
        id="a1", phone="13800000000", password="p", slots=[]))
    await store.delete_account("a1")

    await store.sync_accounts([_cfg_account("a1")])

    # 墓碑仍在：任何视图都不可见
    assert await store.get_account("a1") is None
    assert [a.id for a in await store.list_accounts()] == []
    assert [a.id for a in await store.list_accounts(include_inactive=True)] == []


async def test_existing_account_matrix_not_overwritten_by_config(store: StateStore):
    """配置文件只刷新凭据，不覆盖页面上已维护的守护矩阵。"""
    matrix = {"001": {"mon": ["09:00-11:00"], "tue": [], "wed": [],
                      "thu": [], "fri": [], "sat": [], "sun": []}}
    await store.upsert_account(Account(
        id="a1", phone="13800000000", password="old", slots=[],
        seat_slots=matrix))

    await store.sync_accounts([_cfg_account(
        "a1", phone="13900000000", password="new",
        seat_slots={"002": ["15:00-17:00"]})])

    acc = await store.get_account("a1")
    assert acc is not None
    # 凭据被刷新
    assert acc.phone == "13900000000"
    assert acc.password == "new"
    # 守护矩阵原样保留
    assert acc.seat_slots == matrix
    assert "002" not in (acc.seat_slots or {})


async def test_config_sync_inserts_new_account_only(store: StateStore):
    """配置里的新账号被插入；已存在的账号不产生第二行。"""
    await store.upsert_account(Account(
        id="a1", phone="13800000000", password="p", slots=[]))

    n = await store.sync_accounts([_cfg_account("a1"), _cfg_account("a2")])

    assert n == 2
    assert [a.id for a in await store.list_accounts()] == ["a1", "a2"]
    assert len(await store.list_accounts()) == 2


async def test_deleted_seat_is_not_resurrected_by_seed(store: StateStore):
    """已软删除的目标座位不因配置种子再次出现而复活。"""
    await store.add_target_seat("001", label="窗口位")
    await store.delete_target_seat("001")

    await store.seed_target_seat("001", label="窗口位")

    assert [s.seat_num for s in await store.list_target_seats()] == []


async def test_seed_target_seat_is_idempotent_and_keeps_existing(store: StateStore):
    """种子只插入不存在的座位；已存在座位的标签与期望时段不被覆盖。"""
    await store.add_target_seat("001", label="原标签",
                                desired_slots=["09:00-11:00"])

    await store.seed_target_seat("001", label="新标签",
                                 desired_slots=["14:00-16:00"])
    await store.seed_target_seat("001", label="新标签")

    seats = await store.list_target_seats()
    assert len(seats) == 1
    assert seats[0].seat_num == "001"
    assert seats[0].label == "原标签"
    assert desired_slots_of(seats[0], "mon") == ["09:00-11:00"]


async def test_explicit_add_resurrects_deleted_seat(store: StateStore):
    """显式的「添加目标座位」动作可以复活已软删除的座位（与种子语义不同）。"""
    await store.add_target_seat("001", label="窗口位")
    await store.delete_target_seat("001")
    assert [s.seat_num for s in await store.list_target_seats()] == []

    await store.add_target_seat("001", label="窗口位")

    assert [s.seat_num for s in await store.list_target_seats()] == ["001"]
