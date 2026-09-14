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


# ---------- Task 7: 主力看护协程 ----------

class _FakePeer:
    """内存假对端：记录 heartbeat/snapshot 推送，可编排响应/异常。"""

    def __init__(self) -> None:
        self.heartbeats: list[dict] = []
        self.snapshots: list[bytes] = []
        self.fail_bk: bool = False
        self.fail_all: bool = False
        self.status_response: dict = {"instance_id": "peer-1", "active_since": None}
        self.heartbeat_response: dict | None = None
        self.raise_exc: Exception | None = None


@pytest.fixture
def fake_peer(monkeypatch):
    from seatbot import ha as _ha
    peer = _FakePeer()

    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, content=None, json=None, timeout=None):
            if peer.fail_all or (peer.fail_bk and "/api/ha/bk/" in url):
                if peer.raise_exc:
                    raise peer.raise_exc
                raise RuntimeError("peer dead")
            if "/api/ha/bk/heartbeat" in url:
                peer.heartbeats.append(json or {})
                if peer.heartbeat_response is not None:
                    return _Resp(200, peer.heartbeat_response)
                return _Resp(200, {"role": "backup", "active_since": None})
            if "/api/ha/bk/snapshot" in url:
                peer.snapshots.append(content or b"")
                return _Resp(200, {"applied": True, "tables": ["accounts"]})
            if "/api/ha/restore" in url:
                return _Resp(200, {"applied": True})
            return _Resp(404, {})

        async def get(self, url, headers=None, timeout=None):
            if peer.fail_all or (peer.fail_bk and "/api/ha/bk/" in url):
                if peer.raise_exc:
                    raise peer.raise_exc
                raise RuntimeError("peer dead")
            if "/api/ha/status" in url:
                return _Resp(200, peer.status_response)
            return _Resp(404, {})

    class _Resp:
        def __init__(self, code: int, body: dict):
            self.status_code = code
            self._body = body

        def json(self):
            return self._body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"http {self.status_code}")

    monkeypatch.setattr(_ha.httpx, "AsyncClient", _FakeAsyncClient)
    return peer


async def test_primary_normal_warming_to_active(fake_peer, tmp_path):
    from seatbot import ha as _ha
    from seatbot.ha import HaConfig, HaRuntime, _primary_tick
    from seatbot.store import StateStore

    store = StateStore(str(tmp_path / "p.db"))
    await store.init()
    try:
        rt = HaRuntime()
        rt.mode = "primary"
        rt.primary_state = "warming"
        rt.cfg = HaConfig(
            mode="primary", key="K", instance_id="primary-1",
            peer_url="http://peer", heartbeat_interval=15,
            lease_ttl=90, activation_buffer=60,
            snapshot_interval=300, failback_grace=180,
        )
        fake_peer.status_response = {"instance_id": "peer-1", "active_since": None}
        sched = type("S", (), {})()
        await _primary_tick(store, sched, rt)
        assert rt.primary_state == "active"
        assert rt.active_since is not None
    finally:
        await store.close()

async def test_primary_warming_unreachable_peer_stays_warming_until_grace(fake_peer, tmp_path):
    """对端不可达时：warming 保持直到超过 failback_grace 才兜底转 active。"""
    from seatbot.ha import HaConfig, HaRuntime, _primary_tick
    from seatbot.store import StateStore

    store = StateStore(str(tmp_path / "p.db"))
    await store.init()
    try:
        rt = HaRuntime()
        rt.mode = "primary"
        rt.primary_state = "warming"
        rt.cfg = HaConfig(
            mode="primary", key="K", instance_id="primary-1",
            peer_url="http://peer", heartbeat_interval=15,
            lease_ttl=90, activation_buffer=60,
            snapshot_interval=300, failback_grace=180,
        )
        fake_peer.fail_all = True
        sched = type("S", (), {})()

        # 1) 未超 grace：保持 warming，不可调度
        await _primary_tick(store, sched, rt)
        assert rt.primary_state == "warming"
        assert rt.active_since is None
        assert rt.can_act() is False

        # 2) 超过 grace：兜底进入 active
        rt.tick(200.0)
        await _primary_tick(store, sched, rt)
        assert rt.primary_state == "active"
        assert rt.active_since is not None
        assert rt.can_act() is True
    finally:
        await store.close()



async def test_primary_suspends_when_peer_and_tunnel_dead(fake_peer, tmp_path):
    from seatbot import ha as _ha
    from seatbot.ha import HaConfig, HaRuntime, _primary_tick
    from seatbot.store import StateStore

    store = StateStore(str(tmp_path / "p.db"))
    await store.init()
    try:
        rt = HaRuntime()
        rt.mode = "primary"
        rt.primary_state = "active"
        rt.cfg = HaConfig(
            mode="primary", key="K", instance_id="primary-1",
            peer_url="http://peer", heartbeat_interval=15,
            lease_ttl=90, activation_buffer=60,
            snapshot_interval=300, failback_grace=180,
        )
        # 模拟时钟已经走远
        rt.last_heartbeat_sent_ok = 0.0
        fake_peer.fail_all = True
        sched = type("S", (), {})()
        # 推 8 次心跳；中途公网自检也失败 → suspended
        for _ in range(8):
            await _primary_tick(store, sched, rt)
        assert rt.primary_state == "suspended"
    finally:
        await store.close()
async def test_primary_suspended_recovers_to_active_on_heartbeat_success(fake_peer, tmp_path):
    """suspended 态每 tick 继续发心跳，心跳恢复后转入 active。"""
    from seatbot.ha import HaConfig, HaRuntime, _primary_tick
    from seatbot.store import StateStore

    store = StateStore(str(tmp_path / "p.db"))
    await store.init()
    try:
        rt = HaRuntime()
        rt.mode = "primary"
        rt.primary_state = "suspended"
        rt.cfg = HaConfig(
            mode="primary", key="K", instance_id="primary-1",
            peer_url="http://peer", heartbeat_interval=15,
            lease_ttl=90, activation_buffer=60,
            snapshot_interval=300, failback_grace=180,
        )
        fake_peer.fail_all = True
        sched = type("S", (), {})()

        # 1) 对端持续失败：保持 suspended
        await _primary_tick(store, sched, rt)
        assert rt.primary_state == "suspended"
        assert rt.can_act() is False

        # 2) 对端心跳恢复：恢复 active，重置 last_heartbeat_sent_ok 并发通知
        fake_peer.fail_all = False
        fake_peer.heartbeat_response = {"role": "backup", "active_since": None}
        await _primary_tick(store, sched, rt)
        assert rt.primary_state == "active"
        assert rt.active_since is not None
        assert rt.last_heartbeat_sent_ok is not None
        assert rt.can_act() is True

        notifs = await store.list_notifications()
        assert any(n["title"] == "主力恢复" for n in notifs)
    finally:
        await store.close()

async def test_primary_transient_heartbeat_failure_does_not_suspend(fake_peer, tmp_path):
    """心跳基线已初始化时，单次瞬时心跳失败未超 TTL 不得触发 suspended。"""
    from seatbot.ha import HaConfig, HaRuntime, _primary_tick
    from seatbot.store import StateStore

    store = StateStore(str(tmp_path / "p.db"))
    await store.init()
    try:
        rt = HaRuntime()
        rt.mode = "primary"
        rt.primary_state = "active"
        rt.active_since = rt.now()
        rt.last_heartbeat_sent_ok = rt.now()
        rt.cfg = HaConfig(
            mode="primary", key="K", instance_id="primary-1",
            peer_url="http://peer", heartbeat_interval=15,
            lease_ttl=90, activation_buffer=60,
            snapshot_interval=300, failback_grace=180,
        )
        fake_peer.fail_all = True
        sched = type("S", (), {})()

        # 瞬时失败 1 次：距上次 ok 仅 0 秒，远小于 lease_ttl 90 秒，保持 active
        await _primary_tick(store, sched, rt)
        assert rt.primary_state == "active"
        assert rt.can_act() is True
    finally:
        await store.close()



async def test_primary_stays_active_when_only_peer_process_dead(fake_peer, tmp_path):
    """备用进程挂但隧道仍在（公网自检应答者是自己）→ 主力保持 active，只记日志。"""
    from seatbot import ha as _ha
    from seatbot.ha import HaConfig, HaRuntime, _primary_tick
    from seatbot.store import StateStore

    store = StateStore(str(tmp_path / "p.db"))
    await store.init()
    try:
        rt = HaRuntime()
        rt.mode = "primary"
        rt.primary_state = "active"
        rt.cfg = HaConfig(
            mode="primary", key="K", instance_id="primary-1",
            peer_url="http://peer", heartbeat_interval=15,
            lease_ttl=90, activation_buffer=60,
            snapshot_interval=300, failback_grace=180,
        )
        rt.last_heartbeat_sent_ok = 0.0
        fake_peer.fail_bk = True   # /bk/* 挂
        # status 仍能通；应答 instance_id 是主力自己（隧道活着）
        fake_peer.status_response = {"instance_id": rt.cfg.instance_id, "active_since": None}
        sched = type("S", (), {})()
        for _ in range(8):
            await _primary_tick(store, sched, rt)
        assert rt.primary_state == "active"
    finally:
        await store.close()
async def test_primary_demotes_to_warming_when_tunnel_answered_by_backup(fake_peer, tmp_path):
    """心跳失败但公网自检应答者是备用 → 备用存活，主力让位转 warming 防脑裂。"""
    from seatbot.ha import HaConfig, HaRuntime, _primary_tick
    from seatbot.store import StateStore

    store = StateStore(str(tmp_path / "p.db"))
    await store.init()
    try:
        rt = HaRuntime()
        rt.mode = "primary"
        rt.primary_state = "active"
        rt.cfg = HaConfig(
            mode="primary", key="K", instance_id="primary-1",
            peer_url="http://peer", heartbeat_interval=15,
            lease_ttl=90, activation_buffer=60,
            snapshot_interval=300, failback_grace=180,
        )
        rt.last_heartbeat_sent_ok = 0.0
        fake_peer.fail_bk = True   # /bk/* 挂
        # status 能通；应答者是备用 (peer-1)
        fake_peer.status_response = {"instance_id": "peer-1", "active_since": 50.0}
        sched = type("S", (), {})()
        await _primary_tick(store, sched, rt)
        assert rt.primary_state == "warming"
        assert rt.active_since is None
        assert rt.can_act() is False
    finally:
        await store.close()



async def test_primary_demotes_to_warming_on_active_backup_heartbeat(fake_peer, tmp_path):
    """心跳应答 active_since 非空 → 主力让位进 warming。"""
    from seatbot import ha as _ha
    from seatbot.ha import HaConfig, HaRuntime, _primary_tick
    from seatbot.store import StateStore

    store = StateStore(str(tmp_path / "p.db"))
    await store.init()
    try:
        rt = HaRuntime()
        rt.mode = "primary"
        rt.primary_state = "active"
        rt.active_since = 100.0
        rt.cfg = HaConfig(
            mode="primary", key="K", instance_id="primary-1",
            peer_url="http://peer", heartbeat_interval=15,
            lease_ttl=90, activation_buffer=60,
            snapshot_interval=300, failback_grace=180,
        )
        fake_peer.heartbeat_response = {"role": "backup", "active_since": 123.0}
        sched = type("S", (), {})()
        await _primary_tick(store, sched, rt)
        assert rt.primary_state == "warming"
        assert rt.active_since is None
    finally:
        await store.close()


# ---------- Task 8: 备用看门狗 ----------

async def test_backup_activates_on_silence(fake_peer, tmp_path):
    """心跳静默超 TTL+buffer → standby→active。"""
    from seatbot import ha as _ha
    from seatbot.ha import HaConfig, HaRuntime, _backup_tick
    from seatbot.store import StateStore

    store = StateStore(str(tmp_path / "b.db"))
    await store.init()
    try:
        rt = HaRuntime()
        rt.mode = "backup"
        rt.backup_state = "standby"
        rt.last_heartbeat_seen = 0.0
        rt.boot_monotonic = 0.0
        rt.cfg = HaConfig(
            mode="backup", key="K", instance_id="backup-1",
            peer_url="http://peer", heartbeat_interval=15,
            lease_ttl=90, activation_buffer=60,
            snapshot_interval=300, failback_grace=180,
        )
        # 假时钟：当前 1000（> ttl+buffer=150）
        rt._clock = lambda: 1000.0
        sched = type("S", (), {})()
        await _backup_tick(store, sched, rt)
        assert rt.backup_state == "active"
        assert rt.active_since == 1000.0
    finally:
        await store.close()


async def test_failback_pending_reactivates_after_grace(fake_peer, tmp_path):
    """failback_pending 超 grace → 重新接管 active。"""
    from seatbot import ha as _ha
    from seatbot.ha import HaConfig, HaRuntime, _backup_tick
    from seatbot.store import StateStore

    store = StateStore(str(tmp_path / "b.db"))
    await store.init()
    try:
        rt = HaRuntime()
        rt.mode = "backup"
        rt.backup_state = "failback_pending"
        rt.last_primary_contact = 0.0
        rt._clock = lambda: 10000.0
        rt.cfg = HaConfig(
            mode="backup", key="K", instance_id="backup-1",
            peer_url="http://peer", heartbeat_interval=15,
            lease_ttl=90, activation_buffer=60,
            snapshot_interval=300, failback_grace=180,
        )
        sched = type("S", (), {})()
        await _backup_tick(store, sched, rt)
        assert rt.backup_state == "active"
    finally:
        await store.close()


async def test_standby_remains_when_recent_heartbeat(fake_peer, tmp_path):
    """心跳新 → 维持 standby。"""
    from seatbot import ha as _ha
    from seatbot.ha import HaConfig, HaRuntime, _backup_tick
    from seatbot.store import StateStore

    store = StateStore(str(tmp_path / "b.db"))
    await store.init()
    try:
        rt = HaRuntime()
        rt.mode = "backup"
        rt.backup_state = "standby"
        rt.last_heartbeat_seen = 1000.0
        rt._clock = lambda: 1010.0  # 仅过去 10s
        rt.cfg = HaConfig(
            mode="backup", key="K", instance_id="backup-1",
            peer_url="http://peer", heartbeat_interval=15,
            lease_ttl=90, activation_buffer=60,
            snapshot_interval=300, failback_grace=180,
        )
        sched = type("S", (), {})()
        await _backup_tick(store, sched, rt)
        assert rt.backup_state == "standby"
    finally:
        await store.close()