"""/bindings/rebind 换绑端点与绑定页换绑入口的集成测试（纯本地，零外呼）。"""
import asyncio
from datetime import date, time as _t

from urllib.parse import unquote

from fastapi.testclient import TestClient

from seatbot.config import load_config
from seatbot.models import Account, Task, TaskStatus
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore
from seatbot.web.app import make_app

_AUTH = {"Host": "127.0.0.1:8080", "X-Auth-Token": "test-token"}
WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ALL_DAYS = {w: ["09:00-11:00"] for w in WEEKDAY_KEYS}


def _client(tmp_path, accounts, tasks=(), settings=None):
    cfg = load_config("config.test.yaml")
    cfg.runtime.db_path = str(tmp_path / "rebind.db")
    cfg.runtime.web_token = "test-token"
    store = StateStore(cfg.runtime.db_path)

    async def seed():
        await store.init()
        await store.add_target_seat("001", label="甲")
        await store.add_target_seat("002", label="乙")
        for acc in accounts:
            await store.upsert_account(acc)
        for tk in tasks:
            await store.add_task(tk)
        if settings:
            await store.set_settings(settings)
    asyncio.run(seed())
    app = make_app(cfg, store, Scheduler(cfg, store))
    return TestClient(app, base_url="http://127.0.0.1:8080"), store


def _acc(id_, seat_slots):
    return Account(id=id_, phone=f"138{id_}", password="x",
                   slots=[], seat_slots=seat_slots)


def _task(account_id, day, seat="001", status=TaskStatus.PENDING, reserve_id=None):
    return Task(id=None, account_id=account_id, day=day, seat_num=seat,
                start_time=_t(9, 0), end_time=_t(11, 0),
                status=status, reserve_id=reserve_id)


def test_bindings_page_renders_rebind_button(tmp_path):
    """绑定页每行绑定在解绑旁给出换绑入口与合规候选账号。"""
    c, store = _client(tmp_path, [
        _acc("张三", {"001": dict(ALL_DAYS)}),
        _acc("李四", {}),
    ])
    try:
        r = c.get("/bindings", headers=_AUTH)
        assert r.status_code == 200
        assert "/bindings/rebind" in r.text
        assert "换绑" in r.text
        assert "李四（余 5h）" in r.text
    finally:
        asyncio.run(store.close())


def test_bindings_page_marks_no_free_account(tmp_path):
    """无合规候选时显示「无空闲账号」，不渲染换绑表单。"""
    c, store = _client(tmp_path, [_acc("张三", {"001": dict(ALL_DAYS)})])
    try:
        r = c.get("/bindings", headers=_AUTH)
        assert r.status_code == 200
        assert "无空闲账号" in r.text
    finally:
        asyncio.run(store.close())


def test_rebind_moves_matrix_and_pending_task(tmp_path):
    """全周统一换绑：7 天一起迁移，未提交的待约任务同步改归属。"""
    day = date.today()
    c, store = _client(tmp_path, [
        _acc("张三", {"001": dict(ALL_DAYS)}),
        _acc("李四", {"002": {w: ["13:00-15:00"] for w in WEEKDAY_KEYS}}),
    ], tasks=[_task("张三", day)])
    try:
        r = c.post("/bindings/rebind", headers=_AUTH, data={
            "account_id": "张三", "target_account_id": "李四",
            "seat_num": "001", "slot": "09:00-11:00",
        }, follow_redirects=False)
        assert r.status_code == 303
        assert "已换绑" in unquote(r.headers["location"])
        src = asyncio.run(store.get_account("张三"))
        dst = asyncio.run(store.get_account("李四"))
        assert "001" not in (src.seat_slots or {})
        assert all(dst.seat_slots["001"][w] == ["09:00-11:00"] for w in WEEKDAY_KEYS)
        assert dst.seat_slots["002"]["mon"] == ["13:00-15:00"]
        assert dst.bound_seats == ["001", "002"]
        moved = [t for t in asyncio.run(store.list_tasks(seat_num="001"))
                 if t.day == day]
        assert {t.account_id for t in moved} == {"李四"}
    finally:
        asyncio.run(store.close())


def test_rebind_single_weekday_only(tmp_path):
    """按天自定义模式：只换选中的星期几，其余天保持原账号。"""
    c, store = _client(tmp_path, [
        _acc("张三", {"001": dict(ALL_DAYS)}),
        _acc("李四", {}),
    ])
    try:
        r = c.post("/bindings/rebind", headers=_AUTH, data={
            "account_id": "张三", "target_account_id": "李四",
            "seat_num": "001", "slot": "09:00-11:00", "weekday": "wed",
        }, follow_redirects=False)
        assert r.status_code == 303
        src = asyncio.run(store.get_account("张三"))
        dst = asyncio.run(store.get_account("李四"))
        assert src.seat_slots["001"]["wed"] == []
        assert src.seat_slots["001"]["mon"] == ["09:00-11:00"]
        assert dst.seat_slots["001"]["wed"] == ["09:00-11:00"]
        assert dst.seat_slots["001"]["mon"] == []
    finally:
        asyncio.run(store.close())


def test_rebind_rejects_account_over_daily_limit(tmp_path):
    """接手账号当天累计超 5h → 拒绝，双方矩阵不变。"""
    c, store = _client(tmp_path, [
        _acc("张三", {"001": dict(ALL_DAYS)}),
        _acc("李四", {"002": {w: ["13:00-15:00", "15:00-18:00"]
                             for w in WEEKDAY_KEYS}}),
    ])
    try:
        r = c.post("/bindings/rebind", headers=_AUTH, data={
            "account_id": "张三", "target_account_id": "李四",
            "seat_num": "001", "slot": "09:00-11:00", "weekday": "mon",
        }, follow_redirects=False)
        assert r.status_code == 303
        assert "error=" in r.headers["location"]
        src = asyncio.run(store.get_account("张三"))
        assert src.seat_slots["001"]["mon"] == ["09:00-11:00"]
        dst = asyncio.run(store.get_account("李四"))
        assert "001" not in (dst.seat_slots or {})
    finally:
        asyncio.run(store.close())


def test_rebind_same_seat_nonoverlap_now_allowed(tmp_path):
    """接手账号同座已有非重叠时段 → 允许换绑并追加。"""
    c, store = _client(tmp_path, [
        _acc("张三", {"001": dict(ALL_DAYS)}),
        _acc("李四", {"001": {"mon": ["13:00-15:00"]}}),
    ])
    try:
        r = c.post("/bindings/rebind", headers=_AUTH, data={
            "account_id": "张三", "target_account_id": "李四",
            "seat_num": "001", "slot": "09:00-11:00", "weekday": "mon",
        }, follow_redirects=False)
        assert r.status_code == 303
        assert "msg=" in r.headers["location"]
        dst = asyncio.run(store.get_account("李四"))
        assert dst.seat_slots["001"]["mon"] == ["13:00-15:00", "09:00-11:00"]
        src = asyncio.run(store.get_account("张三"))
        assert src.seat_slots["001"]["mon"] == []
    finally:
        asyncio.run(store.close())


def test_rebind_keeps_tasks_with_real_reservation(tmp_path):
    """已持预约的任务不动（不产生任何真实超星操作）。"""
    day = date.today()
    c, store = _client(tmp_path, [
        _acc("张三", {"001": dict(ALL_DAYS)}),
        _acc("李四", {}),
    ], tasks=[_task("张三", day, status=TaskStatus.ACTIVE, reserve_id=999)])
    try:
        r = c.post("/bindings/rebind", headers=_AUTH, data={
            "account_id": "张三", "target_account_id": "李四",
            "seat_num": "001", "slot": "09:00-11:00",
        }, follow_redirects=False)
        assert r.status_code == 303
        assert "保留" in unquote(r.headers["location"])
        kept = asyncio.run(store.list_tasks(seat_num="001", day=day))
        assert [t.account_id for t in kept] == ["张三"]
    finally:
        asyncio.run(store.close())


def test_rebind_bad_input_not_500(tmp_path):
    """空提交 / 非法星期 / 未知账号 / 未注册座位均不 500。"""
    c, store = _client(tmp_path, [_acc("张三", {"001": dict(ALL_DAYS)}),
                                  _acc("李四", {})])
    try:
        cases = [
            {},
            {"account_id": "张三"},
            {"account_id": "张三", "target_account_id": "李四"},
            {"account_id": "张三", "target_account_id": "李四",
             "seat_num": "001", "slot": "bad-range"},
            {"account_id": "张三", "target_account_id": "李四",
             "seat_num": "001", "slot": "09:00-11:00", "weekday": "nope"},
            {"account_id": "张三", "target_account_id": "张三",
             "seat_num": "001", "slot": "09:00-11:00"},
            {"account_id": "张三", "target_account_id": "nobody",
             "seat_num": "001", "slot": "09:00-11:00"},
            {"account_id": "张三", "target_account_id": "李四",
             "seat_num": "999", "slot": "09:00-11:00"},
            {"account_id": "nobody", "target_account_id": "李四",
             "seat_num": "001", "slot": "09:00-11:00"},
        ]
        for data in cases:
            r = c.post("/bindings/rebind", headers=_AUTH, data=data,
                       follow_redirects=False)
            assert r.status_code in (303, 404, 422), \
                f"{data} -> {r.status_code}: {r.text[:400]}"
    finally:
        asyncio.run(store.close())


def test_bindings_manual_appends_second_slot(tmp_path):
    """绑定页手动绑定同座第二时段：追加而非覆盖第一段。"""
    c, store = _client(tmp_path, [
        _acc("张三", {"001": dict(ALL_DAYS)}),
    ])
    try:
        r = c.post("/bindings/manual", headers=_AUTH, data={
            "account_id": "张三", "seat_num": "001",
            "weekday": "mon", "start": "14:00", "end": "16:00",
        }, follow_redirects=False)
        assert r.status_code == 303
        acc = asyncio.run(store.get_account("张三"))
        assert acc.seat_slots["001"]["mon"] == ["09:00-11:00", "14:00-16:00"]
        assert acc.seat_slots["001"]["tue"] == ["09:00-11:00"]
    finally:
        asyncio.run(store.close())
