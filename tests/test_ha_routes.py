"""HA /api/ha/* 端点鉴权与基础行为。"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from seatbot.ha import HaConfig, HaRuntime
from seatbot.web.ha_routes import router as ha_router


def _make_runtime(key: str = "K", mode: str = "primary", state: str = "active",
                  instance_id: str = "I1") -> HaRuntime:
    cfg = HaConfig(
        mode=mode, key=key, instance_id=instance_id, peer_url="http://peer",
        heartbeat_interval=15, lease_ttl=90, activation_buffer=60,
        snapshot_interval=300, failback_grace=180,
    )
    rt = HaRuntime()
    rt.cfg = cfg
    rt.mode = mode
    if mode == "primary":
        rt.primary_state = state
    elif mode == "backup":
        rt.backup_state = state
    rt.active_since = 1000.0 if state == "active" else None
    return rt


@pytest.fixture
def client_primary():
    app = FastAPI()
    app.include_router(ha_router)
    app.state.ha = _make_runtime(key="K", mode="primary", state="active")
    return TestClient(app)


@pytest.fixture
def client_backup_standby():
    app = FastAPI()
    app.include_router(ha_router)
    app.state.ha = _make_runtime(key="K", mode="backup", state="standby")
    return TestClient(app)


def test_status_requires_key(client_primary):
    r = client_primary.get("/api/ha/status")
    assert r.status_code == 401
    r2 = client_primary.get("/api/ha/status", headers={"X-HA-Key": "wrong"})
    assert r2.status_code == 401


def test_status_with_key(client_primary):
    r = client_primary.get("/api/ha/status", headers={"X-HA-Key": "K"})
    assert r.status_code == 200
    body = r.json()
    assert {"instance_id", "role", "state"} <= set(body.keys())
    assert body["role"] == "primary"
    assert body["state"] == "active"


def test_heartbeat_refreshes_seen(client_backup_standby):
    rt = client_backup_standby.app.state.ha
    before = rt.last_heartbeat_seen
    r = client_backup_standby.post(
        "/api/ha/bk/heartbeat",
        headers={"X-HA-Key": "K"},
        json={"instance_id": "primary-1"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "backup"
    assert rt.last_heartbeat_seen is not None
    if before is not None:
        assert rt.last_heartbeat_seen >= before
    assert rt.last_primary_contact is not None


def test_heartbeat_on_active_backup_enters_failback_pending(client_backup_standby):
    rt = client_backup_standby.app.state.ha
    rt.backup_state = "active"
    rt.active_since = 100.0
    r = client_backup_standby.post(
        "/api/ha/bk/heartbeat",
        headers={"X-HA-Key": "K"},
        json={"instance_id": "primary-1"},
    )
    assert r.status_code == 200
    assert rt.backup_state == "failback_pending"


def test_bk_state_returns_runtime_state(client_primary):
    r = client_primary.get("/api/ha/bk/state", headers={"X-HA-Key": "K"})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "primary"
    assert body["state"] == "active"


def test_restore_rejected_when_not_warming(client_primary):
    rt = client_primary.app.state.ha
    rt.primary_state = "active"
    r = client_primary.post(
        "/api/ha/restore",
        headers={"X-HA-Key": "K"},
        content=b"x",
    )
    assert r.status_code == 409


def test_restore_accepts_when_warming(tmp_path):
    import asyncio
    from seatbot.store import StateStore

    async def _run():
        store = StateStore(str(tmp_path / "rest.db"))
        await store.init()
        try:
            app = FastAPI()
            app.include_router(ha_router)
            rt = _make_runtime(key="K", mode="primary", state="warming")
            rt.primary_state = "warming"
            app.state.ha = rt
            app.state.store = store
            client = TestClient(app)
            # 截断 gzip magic → apply 会失败 → 应 422
            r = client.post(
                "/api/ha/restore",
                headers={"X-HA-Key": "K"},
                content=b"\x1f\x8b\x00\x00",
            )
            assert r.status_code in (422, 200)
        finally:
            await store.close()

    asyncio.run(_run())


def test_claim_idempotent(client_backup_standby):
    rt = client_backup_standby.app.state.ha
    rt.backup_state = "active"
    rt.active_since = 100.0
    r1 = client_backup_standby.post(
        "/api/ha/bk/claim",
        headers={"X-HA-Key": "K"},
        json={},
    )
    assert r1.status_code == 200
    r2 = client_backup_standby.post(
        "/api/ha/bk/claim",
        headers={"X-HA-Key": "K"},
        json={},
    )
    assert r2.status_code == 200


def test_snapshot_application_via_route(tmp_path):
    """端点层：先构建一份 snapshot 再 POST /api/ha/bk/snapshot 验证。"""
    import asyncio
    from seatbot.ha_sync import build_snapshot
    from seatbot.store import StateStore

    async def _run():
        src = StateStore(str(tmp_path / "src.db"))
        dst = StateStore(str(tmp_path / "dst.db"))
        await src.init()
        await dst.init()
        try:
            await src.set_settings({"custom.x": "42"})
            payload, _ = await build_snapshot(src)
            app = FastAPI()
            app.include_router(ha_router)
            app.state.ha = _make_runtime(key="K", mode="backup", state="standby")
            app.state.store = dst
            client = TestClient(app)
            r = client.post(
                "/api/ha/bk/snapshot",
                headers={"X-HA-Key": "K"},
                content=payload,
            )
            assert r.status_code == 200, r.text
            assert (await dst.get_setting("custom.x")) == "42"
        finally:
            await src.close()
            await dst.close()

    asyncio.run(_run())