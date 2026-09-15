"""HA 快照引擎：构建（主力侧）/ 应用（备用侧与回暖期主力）。

约束（spec §9）：
  - 全表同步，logs 剔除（副本不需要主力的运行日志）。
  - 接收端 ha.* 键保留（避免两端 instance_id 互覆）。
  - sqlite3 backup API 在线热备；阻塞 IO 全部 asyncio.to_thread 包装。
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import sqlite3
from typing import Any

from seatbot.ha import HA_LOCAL_KEYS, SCHEMA_VERSION


class HaSnapshotError(Exception):
    """快照接收/校验失败。"""


async def build_snapshot(store) -> tuple[bytes, dict[str, Any]]:
    """构建一份 gzip 化的 DB 快照副本，剔除 logs 表。

    Returns:
        (gzip 字节, 元数据)。元数据字段：schema_version, sha256, tables, rows。
    """
    db_path = store.db_path
    return await asyncio.to_thread(_build_snapshot_sync, db_path)


def _build_snapshot_sync(db_path: str) -> tuple[bytes, dict[str, Any]]:
    import tempfile
    from pathlib import Path

    tmp_dir = Path(tempfile.mkdtemp(prefix="ha-snap-"))
    backup_path = tmp_dir / "snap.db"
    src_con = sqlite3.connect(str(db_path), timeout=30)
    try:
        dst_con = sqlite3.connect(str(backup_path))
        try:
            src_con.backup(dst_con)
        finally:
            dst_con.close()
    finally:
        src_con.close()

    # 剔除 logs 表
    dst_con = sqlite3.connect(str(backup_path))
    try:
        dst_con.execute("DELETE FROM logs")
        dst_con.commit()
        tables = _list_tables(dst_con)
        rows = {t: _count_rows(dst_con, t) for t in tables}
        schema_fp = _compute_schema_fingerprint(dst_con)
    finally:
        dst_con.close()

    raw = backup_path.read_bytes()
    gz = gzip.compress(raw, compresslevel=6)
    sha = hashlib.sha256(raw).hexdigest()

    meta = {
        "schema_version": SCHEMA_VERSION,
        "schema_fingerprint": schema_fp,
        "sha256": sha,
        "tables": tables,
        "rows": rows,
    }

    # 清理临时文件
    try:
        backup_path.unlink()
        tmp_dir.rmdir()
    except OSError:
        pass

    return gz, meta


async def apply_snapshot(store, payload: bytes) -> dict[str, Any]:
    """接收并应用一份 gzip 快照到 store。

    Raises:
        HaSnapshotError: 解压失败 / integrity_check 非 ok / schema 不一致。
    """
    db_path = store.db_path
    return await asyncio.to_thread(_apply_snapshot_sync, store, db_path, payload)


def _apply_snapshot_sync(store, db_path: str, payload: bytes) -> dict[str, Any]:
    import tempfile
    from pathlib import Path

    if not payload:
        raise HaSnapshotError("empty payload")
    # gzip magic
    if payload[:2] != b"\x1f\x8b":
        raise HaSnapshotError("payload is not gzip-compressed")

    try:
        raw = gzip.decompress(payload)
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        raise HaSnapshotError(f"gzip decompress failed: {exc}") from exc

    tmp_dir = Path(tempfile.mkdtemp(prefix="ha-apply-"))
    incoming = tmp_dir / "incoming.db"
    incoming.write_bytes(raw)

    # 校验：integrity_check 必须 ok
    inc_con = sqlite3.connect(str(incoming))
    try:
        cur = inc_con.execute("PRAGMA integrity_check")
        row = cur.fetchone()
        if not row or row[0] != "ok":
            raise HaSnapshotError(f"integrity_check failed: {row}")
        cur = inc_con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        inc_fp = _compute_schema_fingerprint(inc_con)
    finally:
        inc_con.close()

    # 校验接收端表结构指纹：版本不一致时拒绝应用，防止破坏新库
    live_con = sqlite3.connect(str(db_path), timeout=30)
    try:
        live_fp = _compute_schema_fingerprint(live_con)
    finally:
        live_con.close()
    if inc_fp != live_fp:
        raise HaSnapshotError(
            f"schema fingerprint mismatch: incoming={inc_fp[:16]} != live={live_fp[:16]}"
        )

    # 抓取接收端现行 ha.* 键与自有 logs/notifications（保留用）
    live_con = sqlite3.connect(str(db_path), timeout=30)
    try:
        preserved: dict[str, str] = {}
        try:
            cur = live_con.execute("SELECT key, value FROM app_settings WHERE key LIKE 'ha.%'")
            preserved = {r[0]: r[1] for r in cur.fetchall()}
        except sqlite3.OperationalError:
            preserved = {}
        try:
            cur = live_con.execute("SELECT ts, level, account_id, message FROM logs ORDER BY id ASC")
            preserved_logs = cur.fetchall()
        except sqlite3.OperationalError:
            preserved_logs = []
        try:
            cur = live_con.execute("SELECT ts, level, title, body FROM notifications ORDER BY id ASC")
            preserved_notifs = cur.fetchall()
        except sqlite3.OperationalError:
            preserved_notifs = []
    finally:
        live_con.close()

    # backup API 灌入活库
    src = sqlite3.connect(str(incoming))
    try:
        dst = sqlite3.connect(str(db_path), timeout=30)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    # 回写保留键
    dst = sqlite3.connect(str(db_path), timeout=30)
    try:
        now_ms = int(_now_ms())
        for k, v in preserved.items():
            if k not in HA_LOCAL_KEYS:
                # 防御性：未来若扩展 ha.* 命名空间，请显式登记到 HA_LOCAL_KEYS
                continue
            dst.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (k, v, now_ms),
            )
        if preserved_logs:
            dst.executemany(
                "INSERT INTO logs (ts, level, account_id, message) VALUES (?, ?, ?, ?)",
                preserved_logs,
            )
        if preserved_notifs:
            for r in preserved_notifs:
                cur = dst.execute(
                    "SELECT 1 FROM notifications WHERE ts=? AND level=? AND title=? AND body=?",
                    (r[0], r[1], r[2], r[3]),
                )
                if not cur.fetchone():
                    dst.execute(
                        "INSERT INTO notifications (ts, level, title, body) VALUES (?, ?, ?, ?)",
                        (r[0], r[1], r[2], r[3]),
                    )
        dst.commit()
        cur = dst.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables_after = [r[0] for r in cur.fetchall()]
    finally:
        dst.close()

    # 失效 store 的内存态
    if hasattr(store, "_data_version"):
        try:
            store._bump()  # type: ignore[attr-defined]
        except Exception:
            pass

    try:
        incoming.unlink()
        tmp_dir.rmdir()
    except OSError:
        pass

    return {"applied": True, "tables": tables_after}


def _list_tables(con: sqlite3.Connection) -> list[str]:
    cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    return [r[0] for r in cur.fetchall()]

def _compute_schema_fingerprint(con: sqlite3.Connection) -> str:
    """计算数据库的表结构指纹 (表名及列定义排序摘要)。"""
    cur = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    tables = [r[0] for r in cur.fetchall()]
    parts: list[str] = []
    for table in tables:
        col_cur = con.execute(f"PRAGMA table_info({table})")
        cols = [(r[1], (r[2] or "").upper(), r[3], r[5]) for r in col_cur.fetchall()]
        cols.sort(key=lambda x: x[0])
        parts.append(f"{table}:{cols}")
    raw = ";".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _count_rows(con: sqlite3.Connection, table: str) -> int:
    if not table.replace("_", "").isalnum():
        return 0
    try:
        cur = con.execute(f"SELECT COUNT(*) FROM {table}")
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return 0


def _now_ms() -> int:
    import time as _t
    return int(_t.time() * 1000)


__all__ = ["HaSnapshotError", "build_snapshot", "apply_snapshot"]