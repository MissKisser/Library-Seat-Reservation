"""双实例 HA 冒烟（零真实账号）：主力 + 备用，端到端时序断言。

不需要真实超星账号；config 留空账号；通过 /api/ha/status 抓取 state 变化。
HA 配置在进程启动前直接写入 SQLite，启动后 supervisor 自动加载。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / ".tmp_pytest" / "smoke"
TMP.mkdir(parents=True, exist_ok=True)
PRIMARY_DIR = TMP / "primary"
BACKUP_DIR = TMP / "backup"
for d in (PRIMARY_DIR, BACKUP_DIR):
    d.mkdir(parents=True, exist_ok=True)

PRIMARY_PORT = 19767
BACKUP_PORT = 19768
KEY = "smoke-shared-key"


def _port_free(port: int) -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _write_config(path: Path, port: int) -> None:
    db_abs = path.parent.resolve() / "seatbot.db"
    path.write_text(
        f"""library:
  room_id: 0
  room_name: smoke
  open_time: "08:00"
  close_time: "22:00"
  max_reserve_hours: 2.0
  daily_reserve_hours_limit: 14
target_seats: []
accounts: []
user_reserved: []
notify_webhook: ""
ha:
  mode: standalone
  key: ""
  instance_id: ""
  peer_url: ""
  heartbeat_interval_seconds: 5
  lease_ttl_seconds: 30
  activation_buffer_seconds: 15
  snapshot_interval_seconds: 20
  failback_grace_seconds: 30
runtime:
  stagger_seconds: [0, 0]
  db_path: {db_abs}
  log_dir: {path.parent.resolve() / "logs"}
  web_host: 127.0.0.1
  web_port: {port}
  web_token: smoke-token
  allowed_hosts:
    - 127.0.0.1
""",
        encoding="utf-8",
    )


def _ha_get(port: int, path: str, with_key: bool = True) -> tuple[int, dict | None]:
    url = f"http://127.0.0.1:{port}{path}"
    headers = {"X-HA-Key": KEY} if with_key else {}
    req = urllib.request.Request(url, method="GET", headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "null")
        except Exception:
            return e.code, None
    except Exception as e:
        return -1, {"error": str(e)}


def _wait_for_port(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _port_free(port):
            return True
        time.sleep(0.5)
    return False


def _seed_db_with_ha(db_path: Path, mode: str, peer_port: int) -> None:
    """直接写入 SQLite 的 HA 配置，避免走 web 鉴权/CSRF。"""
    con = sqlite3.connect(str(db_path))
    try:
        now = int(time.time() * 1000)
        rows = [
            ("ha.mode", mode),
            ("ha.key", KEY),
            ("ha.instance_id", ""),  # 启动后 ensure_ha_bootstrap 会填充
            ("ha.peer_url", f"http://127.0.0.1:{peer_port}"),
            ("ha.heartbeat_interval_seconds", "5"),
            ("ha.lease_ttl_seconds", "30"),
            ("ha.activation_buffer_seconds", "15"),
            ("ha.snapshot_interval_seconds", "20"),
            ("ha.failback_grace_seconds", "30"),
        ]
        for k, v in rows:
            con.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (k, v, now),
            )
        con.commit()
    finally:
        con.close()


def _start_seatbot(cwd: Path, port: int, label: str):
    log = cwd / "err.log"
    log.write_text("", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "seatbot", "run", "-c", str(cwd / "config.yaml")],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        stdout=open(str(log), "ab", 0), stderr=open(str(log), "ab", 0),
    )


@pytest.fixture(scope="module")
def smoke_env():
    # 清理
    for d in (PRIMARY_DIR, BACKUP_DIR):
        for p in d.glob("*.db*"):
            p.unlink(missing_ok=True)
        (d / "logs").mkdir(parents=True, exist_ok=True)

    _write_config(PRIMARY_DIR / "config.yaml", PRIMARY_PORT)
    _write_config(BACKUP_DIR / "config.yaml", BACKUP_PORT)

    # 先把 DB 初始化一次（创建 schema），再写 HA 配置；否则 ensure_ha_bootstrap 会在启动时覆盖 mode
    import subprocess as _sp
    init_p = _sp.run(
        [sys.executable, "-m", "seatbot", "init-db", "-c", str(PRIMARY_DIR / "config.yaml")],
        cwd=str(PRIMARY_DIR), env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True, text=True,
    )
    init_b = _sp.run(
        [sys.executable, "-m", "seatbot", "init-db", "-c", str(BACKUP_DIR / "config.yaml")],
        cwd=str(BACKUP_DIR), env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True, text=True,
    )

    # 写 HA 配置（覆盖 init-db 时的 standalone 默认）
    _seed_db_with_ha(PRIMARY_DIR / "seatbot.db", "primary", BACKUP_PORT)
    _seed_db_with_ha(BACKUP_DIR / "seatbot.db", "backup", PRIMARY_PORT)

    primary = _start_seatbot(PRIMARY_DIR, PRIMARY_PORT, "primary")
    backup = _start_seatbot(BACKUP_DIR, BACKUP_PORT, "backup")

    try:
        assert _wait_for_port(PRIMARY_PORT, timeout=60), "primary port not ready"
        assert _wait_for_port(BACKUP_PORT, timeout=60), "backup port not ready"
        # 等 supervisor reload + 第一波心跳/快照
        time.sleep(8)
        yield {"primary": primary, "backup": backup}
    finally:
        for p in (primary, backup):
            try:
                p.terminate()
                p.wait(timeout=10)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass


def test_smoke_failover_and_failback(smoke_env):
    primary = smoke_env["primary"]
    backup = smoke_env["backup"]

    # 0) 校验 DB 真的写进去了
    for label, db in (("primary", PRIMARY_DIR / "seatbot.db"),
                     ("backup", BACKUP_DIR / "seatbot.db")):
        con = sqlite3.connect(str(db))
        try:
            rows = con.execute(
                "SELECT key, value FROM app_settings WHERE key IN ('ha.mode','ha.key')"
            ).fetchall()
        finally:
            con.close()
        print(f"[smoke] db {label} ha rows: {dict(rows)}")

    # 1) 双方 /api/ha/status：主力 active + 备用 standby
    code, pbody = _ha_get(PRIMARY_PORT, "/api/ha/status")
    assert code == 200, f"primary status: {code} {pbody}"
    assert pbody["role"] == "primary" and pbody["state"] == "active", pbody

    code, bbody = _ha_get(BACKUP_PORT, "/api/ha/status")
    assert code == 200, f"backup status: {code} {bbody}"
    assert bbody["role"] == "backup" and bbody["state"] == "standby", bbody

    # 2) 备用收到过快照：ha.mode/peer_url/heartbeat 已与主力同步；
    #    ha.key 必须保留为本机 KEY（不能被主力快照覆盖）
    con = sqlite3.connect(str(BACKUP_DIR / "seatbot.db"))
    try:
        rows = con.execute(
            "SELECT key, value FROM app_settings WHERE key LIKE 'ha.%'"
        ).fetchall()
    finally:
        con.close()
    kvs = dict(rows)
    assert kvs.get("ha.key") == KEY, f"ha.key overwritten by snapshot: {kvs}"
    assert kvs.get("ha.mode") == "backup", f"ha.mode overwritten: {kvs}"

    # 3) 杀主力 → 备用在 TTL+buffer（约 45s）内接管
    print(f"[smoke] killing primary pid={primary.pid}")
    primary.terminate()
    try:
        primary.wait(timeout=10)
    except Exception:
        primary.kill()

    deadline = time.time() + 70
    activated = False
    while time.time() < deadline:
        code, bbody = _ha_get(BACKUP_PORT, "/api/ha/status")
        if code == 200 and bbody and bbody.get("state") == "active":
            activated = True
            break
        time.sleep(2)
    assert activated, f"backup failed to activate within 70s: code={code} body={bbody}"

    # 4) 重启主力 → 回暖/claim/restore → 主力 active + 备用 standby
    print("[smoke] restarting primary")
    new_primary = _start_seatbot(PRIMARY_DIR, PRIMARY_PORT, "primary-restart")
    try:
        assert _wait_for_port(PRIMARY_PORT, timeout=60), "primary restart failed"
        deadline = time.time() + 90
        p_state = None
        b_state = None
        while time.time() < deadline:
            _, pb = _ha_get(PRIMARY_PORT, "/api/ha/status")
            _, bb = _ha_get(BACKUP_PORT, "/api/ha/status")
            p_state = pb.get("state") if pb else None
            b_state = bb.get("state") if bb else None
            if p_state == "active" and b_state == "standby":
                break
            time.sleep(2)
        assert p_state == "active", f"primary did not reach active: {p_state}"
        assert b_state == "standby", f"backup did not retreat: {b_state}"
    finally:
        try:
            new_primary.terminate()
            new_primary.wait(timeout=10)
        except Exception:
            try:
                new_primary.kill()
            except Exception:
                pass