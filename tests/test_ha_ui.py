"""HA 设置页卡片 / dashboard 横幅 / key 重生成。"""
from __future__ import annotations

import pytest


@pytest.fixture
async def store(tmp_path):
    from seatbot.store import StateStore
    s = StateStore(str(tmp_path / "ha-ui.db"))
    await s.init()
    yield s
    await s.close()


def _build_app(store, ha):
    from fastapi import FastAPI
    from seatbot.web.app import make_app
    from seatbot.config import Config, LibraryConfig, RuntimeConfig
    cfg = Config(
        library=LibraryConfig(room_id=0, room_name="t"),
        runtime=RuntimeConfig(stagger_seconds=[0, 0], web_token=""),
    )
    from seatbot.scheduler import Scheduler

    class _S:
        async def peek_next_relay(self):
            return None

    sched = Scheduler(cfg, store)
    sched.ha = ha
    sched._clients = {}
    app = make_app(cfg, store, sched)
    return app


def test_settings_page_renders_ha_card(store):
    from fastapi.testclient import TestClient
    from seatbot.ha import HaConfig, HaRuntime
    rt = HaRuntime()
    rt.mode = "standalone"
    rt.cfg = HaConfig(
        mode="standalone", key="", instance_id="I", peer_url="",
        heartbeat_interval=15, lease_ttl=90, activation_buffer=60,
        snapshot_interval=300, failback_grace=180,
    )
    app = _build_app(store, rt)
    client = TestClient(app)
    r = client.get("/settings")
    assert r.status_code == 200
    assert "高可用" in r.text
    assert "ha.mode" in r.text


def test_save_primary_generates_key(store):
    from fastapi.testclient import TestClient
    from seatbot.ha import HaConfig, HaRuntime, load_ha_config
    import asyncio
    rt = HaRuntime()
    rt.mode = "standalone"
    rt.cfg = HaConfig(
        mode="standalone", key="", instance_id="I", peer_url="",
        heartbeat_interval=15, lease_ttl=90, activation_buffer=60,
        snapshot_interval=300, failback_grace=180,
    )
    app = _build_app(store, rt)
    client = TestClient(app)
    r = client.post(
        "/settings",
        data={
            "ha.mode": "primary",
            "ha.peer_url": "https://peer.example",
            "ha.heartbeat_interval_seconds": "15",
            "ha.lease_ttl_seconds": "90",
            "ha.activation_buffer_seconds": "60",
            "ha.snapshot_interval_seconds": "300",
            "ha.failback_grace_seconds": "180",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    cfg = asyncio.run(load_ha_config(store))
    assert (cfg.key or "") != ""
    assert cfg.mode == "primary"


def test_save_backup_requires_url_and_key(store):
    from fastapi.testclient import TestClient
    from seatbot.ha import HaConfig, HaRuntime
    rt = HaRuntime()
    rt.mode = "standalone"
    rt.cfg = HaConfig(
        mode="standalone", key="", instance_id="I", peer_url="",
        heartbeat_interval=15, lease_ttl=90, activation_buffer=60,
        snapshot_interval=300, failback_grace=180,
    )
    app = _build_app(store, rt)
    client = TestClient(app)
    r = client.post(
        "/settings",
        data={"ha.mode": "backup"},
        follow_redirects=False,
    )
    # 应回显错误（重定向到 /settings?error=...）— 不应 500
    assert r.status_code in (303, 200)
    if r.status_code == 303:
        assert "error=" in r.headers.get("location", "")


def test_dashboard_banner_when_standby(store):
    from fastapi.testclient import TestClient
    from seatbot.ha import HaConfig, HaRuntime
    rt = HaRuntime()
    rt.mode = "backup"
    rt.backup_state = "standby"
    rt.cfg = HaConfig(
        mode="backup", key="K", instance_id="I", peer_url="",
        heartbeat_interval=15, lease_ttl=90, activation_buffer=60,
        snapshot_interval=300, failback_grace=180,
    )
    app = _build_app(store, rt)
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert "备用" in r.text


def test_dashboard_banner_when_suspended(store):
    from fastapi.testclient import TestClient
    from seatbot.ha import HaConfig, HaRuntime
    rt = HaRuntime()
    rt.mode = "primary"
    rt.primary_state = "suspended"
    rt.cfg = HaConfig(
        mode="primary", key="K", instance_id="I", peer_url="",
        heartbeat_interval=15, lease_ttl=90, activation_buffer=60,
        snapshot_interval=300, failback_grace=180,
    )
    app = _build_app(store, rt)
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert "暂停" in r.text or "suspended" in r.text.lower()


def test_key_regenerate_endpoint(store):
    from fastapi.testclient import TestClient
    from seatbot.ha import HaConfig, HaRuntime, load_ha_config
    import asyncio
    rt = HaRuntime()
    rt.mode = "primary"
    rt.cfg = HaConfig(
        mode="primary", key="OLD", instance_id="I", peer_url="",
        heartbeat_interval=15, lease_ttl=90, activation_buffer=60,
        snapshot_interval=300, failback_grace=180,
    )
    app = _build_app(store, rt)
    client = TestClient(app)
    r = client.post("/ha/key/regenerate", follow_redirects=False)
    assert r.status_code == 303
    cfg = asyncio.run(load_ha_config(store))
    assert cfg.key != "OLD"
    assert len(cfg.key) >= 32


def test_settings_page_has_editable_ha_key(store):
    from fastapi.testclient import TestClient
    from seatbot.ha import HaConfig, HaRuntime
    rt = HaRuntime()
    rt.mode = "standalone"
    rt.cfg = HaConfig(
        mode="standalone", key="MY_KEY", instance_id="I", peer_url="",
        heartbeat_interval=15, lease_ttl=90, activation_buffer=60,
        snapshot_interval=300, failback_grace=180,
    )
    app = _build_app(store, rt)
    client = TestClient(app)
    r = client.get("/settings")
    assert r.status_code == 200
    assert 'name="ha.key"' in r.text


def test_save_backup_with_explicit_key(store):
    from fastapi.testclient import TestClient
    from seatbot.ha import HaConfig, HaRuntime, load_ha_config
    import asyncio
    rt = HaRuntime()
    rt.mode = "standalone"
    rt.cfg = HaConfig(
        mode="standalone", key="", instance_id="I", peer_url="",
        heartbeat_interval=15, lease_ttl=90, activation_buffer=60,
        snapshot_interval=300, failback_grace=180,
    )
    app = _build_app(store, rt)
    client = TestClient(app)
    r = client.post(
        "/settings",
        data={
            "ha.mode": "backup",
            "ha.peer_url": "https://peer.example",
            "ha.key": "COPIED_KEY_FROM_PRIMARY",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "saved=1" in r.headers.get("location", "")
    cfg = asyncio.run(load_ha_config(store))
    assert cfg.mode == "backup"
    assert cfg.key == "COPIED_KEY_FROM_PRIMARY"
    assert cfg.peer_url == "https://peer.example"


def test_save_backup_empty_key_keeps_existing(store):
    from fastapi.testclient import TestClient
    from seatbot.ha import HaConfig, HaRuntime, load_ha_config
    import asyncio
    asyncio.run(store.set_settings({"ha.key": "EXISTING_KEY"}))
    rt = HaRuntime()
    rt.mode = "standalone"
    rt.cfg = HaConfig(
        mode="standalone", key="EXISTING_KEY", instance_id="I", peer_url="",
        heartbeat_interval=15, lease_ttl=90, activation_buffer=60,
        snapshot_interval=300, failback_grace=180,
    )
    app = _build_app(store, rt)
    client = TestClient(app)
    r = client.post(
        "/settings",
        data={
            "ha.mode": "backup",
            "ha.peer_url": "https://peer.example",
            "ha.key": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "saved=1" in r.headers.get("location", "")
    cfg = asyncio.run(load_ha_config(store))
    assert cfg.mode == "backup"
    assert cfg.key == "EXISTING_KEY"


async def test_standalone_switch_to_primary_spawns_supervisor(store):
    """从 standalone 切 primary：自动拉起 supervisor 协程，且状态能从 warming 推进到 active。"""
    from httpx import ASGITransport, AsyncClient
    from seatbot.ha import HaConfig, HaRuntime

    rt = HaRuntime()
    rt.mode = "standalone"
    rt.cfg = HaConfig(
        mode="standalone", key="", instance_id="I", peer_url="",
        heartbeat_interval=1, lease_ttl=90, activation_buffer=60,
        snapshot_interval=300, failback_grace=180,
    )
    app = _build_app(store, rt)
    assert app.state.ha_supervisor_task is None

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        r = await client.post(
            "/settings",
            data={
                "ha.mode": "primary",
                "ha.heartbeat_interval_seconds": "1",
            },
        )
        assert r.status_code == 303

    task = app.state.ha_supervisor_task
    assert task is not None
    assert not task.done()

    # 等待一个 tick（heartbeat_interval=1s），状态从 warming 推进为 active
    await asyncio.sleep(1.2)
    assert rt.mode == "primary"
    assert rt.primary_state == "active"
    assert rt.can_act() is True

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------- helpers ----------

import asyncio


def await_handler(coro_fn, *args, **kwargs):
    """Helper: run async function synchronously in tests."""
    return asyncio.get_event_loop().run_until_complete(coro_fn(*args, **kwargs))


# Pytest-asyncio default loop_scope is function; we need async fixtures in
# this module to keep their loop alive across sync test functions. Use
# asyncio.run() in helper instead of get_event_loop for safety.
import asyncio as _aio


def _run(coro):
    return _aio.run(coro)


def await_handler(coro_fn, *args, **kwargs):
    return _run(coro_fn(*args, **kwargs))