"""HA 设置键、默认值、instance_id 引导与状态机/闸门基础。"""
from __future__ import annotations

import pytest

from seatbot.ha import (
    HA_MODES,
    NullHaRuntime,
    ensure_ha_bootstrap,
    generate_ha_key,
    load_ha_config,
)
from seatbot.ha import HaRuntime


@pytest.fixture
async def store(tmp_path):
    from seatbot.store import StateStore
    s = StateStore(str(tmp_path / "ha.db"))
    await s.init()
    yield s
    await s.close()


async def test_defaults_and_instance_id(store):
    await ensure_ha_bootstrap(store)
    cfg = await load_ha_config(store)
    assert cfg.mode == "standalone"
    assert cfg.heartbeat_interval == 15
    assert cfg.lease_ttl == 90
    assert cfg.activation_buffer == 60
    assert cfg.snapshot_interval == 300
    assert cfg.failback_grace == 180
    assert cfg.instance_id
    again = await load_ha_config(store)
    assert again.instance_id == cfg.instance_id


async def test_generate_key_unique_and_long():
    k1 = generate_ha_key()
    k2 = generate_ha_key()
    assert len(k1) >= 32
    assert k1 != k2


async def test_modes_constant():
    assert set(HA_MODES) == {"standalone", "primary", "backup"}


async def test_invalid_mode_falls_back(store):
    await ensure_ha_bootstrap(store)
    await store.set_settings({"ha.mode": "weird"})
    cfg = await load_ha_config(store)
    assert cfg.mode == "standalone"


def test_can_act_matrix():
    rt = HaRuntime()
    rt.mode = "standalone"
    assert rt.can_act()
    rt.mode = "primary"
    rt.primary_state = "warming"
    assert not rt.can_act()
    rt.primary_state = "active"
    assert rt.can_act()
    rt.primary_state = "suspended"
    assert not rt.can_act()
    rt.mode = "backup"
    rt.backup_state = "standby"
    assert not rt.can_act()
    rt.backup_state = "active"
    assert rt.can_act()
    rt.backup_state = "failback_pending"
    assert not rt.can_act()


def test_null_runtime_always_true():
    assert NullHaRuntime().can_act()