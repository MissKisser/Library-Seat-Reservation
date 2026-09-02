"""守护账号页：按星期查看时段（下拉默认今天 / 14:00 后明天）的单元与渲染测试。"""
import asyncio
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from seatbot.config import load_config
from seatbot.models import Account
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore
from seatbot.web.app import make_app
from seatbot.web.routes import _default_view_weekday, _slots_diverged, _slots_view

WEEK_UNIFORM = {w: ["09:00-11:00"] for w in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
WEEK_DIVERGED = {**WEEK_UNIFORM, "tue": [], "sun": ["19:00-21:00"]}


def test_default_view_weekday_before_1400_is_today():
    """14:00 前默认查看今天。"""
    assert _default_view_weekday(datetime(2026, 9, 2, 13, 59)) == "wed"
    assert _default_view_weekday(datetime(2026, 9, 2, 0, 0)) == "wed"


def test_default_view_weekday_after_1400_is_tomorrow():
    """14:00 起（次日预约窗口开放）默认查看明天。"""
    assert _default_view_weekday(datetime(2026, 9, 2, 14, 0)) == "thu"
    assert _default_view_weekday(datetime(2026, 9, 2, 23, 59)) == "thu"


def test_slots_view_kinds():
    """四种展示形态：按座位矩阵 / 通用扁平 / 全天 / 无绑定。"""
    matrix = Account(id="张三", phone="13800000001", password="x",
                     slots=[], seat_slots={"s1": dict(WEEK_DIVERGED), "s2": dict(WEEK_UNIFORM)})
    view = _slots_view(matrix)
    assert view["kind"] == "seats"
    assert [row["seat"] for row in view["seats"]] == ["s1", "s2"]
    assert view["seats"][0]["days"]["tue"] == []
    assert view["seats"][1]["days"]["wed"] == ["09:00-11:00"]

    flat = Account(id="李四", phone="13800000002", password="x", slots=["09:00-11:00"])
    assert _slots_view(flat) == {"kind": "flat", "ranges": ["09:00-11:00"]}

    full = Account(id="王五", phone="13800000003", password="x", slots="full")
    assert _slots_view(full) == {"kind": "full"}

    none = Account(id="赵六", phone="13800000004", password="x", slots=[])
    assert _slots_view(none) == {"kind": "none"}


def test_slots_diverged():
    """全周统一不算分叉；任一天不一致即为分叉（触发查看日下拉）。"""
    uniform = Account(id="张三", phone="13800000001", password="x",
                      slots=[], seat_slots={"s1": dict(WEEK_UNIFORM)})
    diverged = Account(id="李四", phone="13800000002", password="x",
                       slots=[], seat_slots={"s1": dict(WEEK_DIVERGED)})
    assert not _slots_diverged(uniform)
    assert _slots_diverged(diverged)


_AUTH = {"Host": "127.0.0.1:8080", "X-Auth-Token": "test-token"}


def _client(tmp_path, accounts, settings=None):
    cfg = load_config("config.test.yaml")
    cfg.runtime.db_path = str(tmp_path / "accounts_page.db")
    cfg.runtime.web_token = "test-token"
    store = StateStore(cfg.runtime.db_path)

    async def seed():
        await store.init()
        for acc in accounts:
            await store.upsert_account(acc)
        if settings:
            await store.set_settings(settings)
    asyncio.run(seed())

    sched = Scheduler(cfg, store)
    app = make_app(cfg, store, sched)
    return TestClient(app, base_url="http://127.0.0.1:8080")


def test_accounts_page_shows_week_picker_when_diverged(tmp_path):
    """数据各天不一致时，即使全局统一模式也自动显示查看日下拉。"""
    c = _client(tmp_path, [
        Account(id="张三", phone="13800000001", password="x",
                slots=[], seat_slots={"s1": dict(WEEK_DIVERGED)}),
    ])
    r = c.get("/accounts", headers=_AUTH)
    assert r.status_code == 200
    html = r.text
    assert "查看日" in html
    assert "slotWeekPicker(" in html
    assert "accountSlotCell(" in html
    assert "09:00-11:00" in html
    assert "19:00-21:00" in html


def test_accounts_page_hides_week_picker_when_uniform(tmp_path):
    """全局统一 + 数据各天一致：不显示下拉，时段照常渲染。"""
    c = _client(tmp_path, [
        Account(id="张三", phone="13800000001", password="x",
                slots=[], seat_slots={"s1": dict(WEEK_UNIFORM)}),
    ])
    r = c.get("/accounts", headers=_AUTH)
    assert r.status_code == 200
    html = r.text
    assert "查看日" not in html
    assert "accountSlotCell(" in html
    assert "09:00-11:00" in html


def test_accounts_page_shows_week_picker_in_weekly_mode(tmp_path):
    """schedule_mode=weekly 时强制显示查看日下拉。"""
    c = _client(
        tmp_path,
        [Account(id="张三", phone="13800000001", password="x",
                 slots=[], seat_slots={"s1": dict(WEEK_UNIFORM)})],
        settings={"schedule_mode": "weekly"},
    )
    r = c.get("/accounts", headers=_AUTH)
    assert r.status_code == 200
    assert "查看日" in r.text


def test_accounts_page_empty(tmp_path):
    """空账号列表不 500，且不出下拉。"""
    c = _client(tmp_path, [])
    r = c.get("/accounts", headers=_AUTH)
    assert r.status_code == 200
    assert "查看日" not in r.text
