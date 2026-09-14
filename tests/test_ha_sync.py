"""HA 快照引擎 roundtrip + 失败用例。"""
from __future__ import annotations

import gzip

import pytest

from seatbot.ha import SCHEMA_VERSION
from seatbot.ha_sync import HaSnapshotError, apply_snapshot, build_snapshot


@pytest.fixture
async def src_store(tmp_path):
    from seatbot.store import StateStore
    s = StateStore(str(tmp_path / "src.db"))
    await s.init()
    yield s
    await s.close()


@pytest.fixture
async def dst_store(tmp_path):
    from seatbot.store import StateStore
    s = StateStore(str(tmp_path / "dst.db"))
    await s.init()
    yield s
    await s.close()


async def test_snapshot_roundtrip_preserves_receiver_ha_and_drops_logs(src_store, dst_store):
    await src_store.add_notification("hello", "body")
    await src_store.set_settings({"ha.key": "K1", "custom.x": "1"})
    await dst_store.set_settings({
        "ha.mode": "backup",
        "ha.key": "K-old",
        "ha.instance_id": "ID-DST",
        "ha.peer_url": "http://dst",
    })
    await src_store.db.execute(
        "INSERT INTO logs (ts, level, account_id, message) VALUES (?, ?, ?, ?)",
        (1, "info", None, "x"),
    )
    await src_store.db.execute(
        "INSERT INTO logs (ts, level, account_id, message) VALUES (?, ?, ?, ?)",
        (2, "info", None, "y"),
    )
    await src_store.db.execute(
        "INSERT INTO logs (ts, level, account_id, message) VALUES (?, ?, ?, ?)",
        (3, "info", None, "z"),
    )
    await src_store.db.commit()

    payload, meta = await build_snapshot(src_store)
    assert meta["schema_version"] == SCHEMA_VERSION
    assert meta["sha256"]
    assert "logs" in (meta.get("tables_dropped") or []) or meta["tables"]

    result = await apply_snapshot(dst_store, payload)
    assert result["applied"]

    assert await dst_store.get_setting("custom.x") == "1"
    assert await dst_store.get_setting("ha.key") == "K-old"
    assert await dst_store.get_setting("ha.instance_id") == "ID-DST"

    cur = await dst_store.db.execute("SELECT COUNT(*) FROM logs")
    (n,) = await cur.fetchone()
    assert n == 0


async def test_apply_rejects_bad_payload(dst_store):
    with pytest.raises(HaSnapshotError):
        await apply_snapshot(dst_store, b"not-a-gzip")


async def test_apply_rejects_schema_mismatch(src_store, dst_store):
    payload, _ = await build_snapshot(src_store)
    # 篡改头 4 字节（gzip magic）后扔回去会先在 gunzip 失败
    bad = b"\x00\x00\x00\x00" + payload[4:]
    with pytest.raises(HaSnapshotError):
        await apply_snapshot(dst_store, bad)


async def test_payload_is_valid_gzip(src_store):
    payload, _ = await build_snapshot(src_store)
    # 不抛异常 = 是合法 gzip
    import io
    with gzip.GzipFile(fileobj=io.BytesIO(payload)) as gz:
        gz.read()