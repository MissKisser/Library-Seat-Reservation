"""HA 主备容灾：设置读写、状态机、闸门、看护协程。

全部时序使用单调时钟；防脑裂不变式：任一时刻至多一个 can_act() == True。

模块分层：
  - HA_MODES / HA_DEFAULTS / generate_ha_key: 配置键与默认值。
  - HaConfig / load_ha_config / ensure_ha_bootstrap: 读写 app_settings。
  - HaRuntime / NullHaRuntime: 内存态状态机与闸门。
  - run_ha_supervisor / _primary_tick / _backup_tick / _push_restore_loop / _after_activation:
    看护协程（Task 7/8 实装）。
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import httpx


HA_MODES: tuple[str, ...] = ("standalone", "primary", "backup")

# 9 个配置键的默认值。spec §5。
HA_DEFAULTS: dict[str, str] = {
    "ha.mode": "standalone",
    "ha.key": "",
    "ha.instance_id": "",  # 由 ensure_ha_bootstrap 首次引导
    "ha.peer_url": "",
    "ha.heartbeat_interval_seconds": "15",
    "ha.lease_ttl_seconds": "90",
    "ha.activation_buffer_seconds": "60",
    "ha.snapshot_interval_seconds": "300",
    "ha.failback_grace_seconds": "180",
}

# 本机 ha.* 键集合：快照应用时强制保留，避免两端互覆
HA_LOCAL_KEYS: frozenset[str] = frozenset(HA_DEFAULTS.keys())

# schema 版本：apply_snapshot 与 build_snapshot 必须一致
SCHEMA_VERSION: int = 1


def generate_ha_key() -> str:
    """生成共享密钥（≥32 字节 url-safe）。"""
    return secrets.token_urlsafe(32)


def _coerce_int(value: str | None, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class HaConfig:
    mode: str
    key: str
    instance_id: str
    peer_url: str
    heartbeat_interval: int
    lease_ttl: int
    activation_buffer: int
    snapshot_interval: int
    failback_grace: int


async def load_ha_config(store) -> HaConfig:
    """从 app_settings 读取全部 HA 键，缺省回填 HA_DEFAULTS。"""
    rows = await store.get_settings_map()
    mode = rows.get("ha.mode", HA_DEFAULTS["ha.mode"])
    if mode not in HA_MODES:
        mode = "standalone"
    return HaConfig(
        mode=mode,
        key=rows.get("ha.key", ""),
        instance_id=rows.get("ha.instance_id", ""),
        peer_url=rows.get("ha.peer_url", ""),
        heartbeat_interval=_coerce_int(
            rows.get("ha.heartbeat_interval_seconds"),
            int(HA_DEFAULTS["ha.heartbeat_interval_seconds"]),
        ),
        lease_ttl=_coerce_int(
            rows.get("ha.lease_ttl_seconds"),
            int(HA_DEFAULTS["ha.lease_ttl_seconds"]),
        ),
        activation_buffer=_coerce_int(
            rows.get("ha.activation_buffer_seconds"),
            int(HA_DEFAULTS["ha.activation_buffer_seconds"]),
        ),
        snapshot_interval=_coerce_int(
            rows.get("ha.snapshot_interval_seconds"),
            int(HA_DEFAULTS["ha.snapshot_interval_seconds"]),
        ),
        failback_grace=_coerce_int(
            rows.get("ha.failback_grace_seconds"),
            int(HA_DEFAULTS["ha.failback_grace_seconds"]),
        ),
    )


async def ensure_ha_bootstrap(store) -> HaConfig:
    """幂等写入 HA 默认值与 instance_id。已有 instance_id 不覆盖。"""
    rows = await store.get_settings_map()
    patch: dict[str, str] = {}
    for k, default in HA_DEFAULTS.items():
        if k == "ha.instance_id":
            if not rows.get(k):
                patch[k] = uuid.uuid4().hex
        else:
            if k not in rows:
                patch[k] = default
    if patch:
        await store.set_settings(patch)
    return await load_ha_config(store)


def _constant_time_eq(a: str, b: str) -> bool:
    """常数时间字符串比对，避免时序泄露。"""
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


@dataclass
class HaRuntime:
    """HA 内存态：双角色字段 + 哨兵时刻。"""
    cfg: HaConfig | None = None
    mode: str = "standalone"
    primary_state: str = "active"   # warming / active / suspended
    backup_state: str = "standby"    # standby / active / failback_pending
    active_since: Optional[float] = None
    last_heartbeat_sent_ok: Optional[float] = None
    last_heartbeat_seen: Optional[float] = None
    last_snapshot_at: Optional[float] = None
    last_primary_contact: Optional[float] = None
    boot_monotonic: float = field(default_factory=lambda: time.monotonic())
    _snapshot_requested: bool = False
    _clock: Callable[[], float] = time.monotonic

    def now(self) -> float:
        return self._clock()

    def tick(self, seconds: float) -> None:
        """推进测试时钟。"""
        boot = self.boot_monotonic
        self._clock = (lambda b=boot, s=seconds: b + s)  # noqa: E731
    def request_snapshot(self) -> None:
        """设置一次性快照推送标志。"""
        self._snapshot_requested = True

    def consume_snapshot_request(self) -> bool:
        """读取并清空一次性快照请求标志。"""
        if self._snapshot_requested:
            self._snapshot_requested = False
            return True
        return False

    def reload(self, store) -> HaConfig:
        """从 store 重新读取 HA 配置并同步 mode。"""
        import asyncio
        cfg = asyncio.get_event_loop().run_until_complete(load_ha_config(store))
        return self.apply_config(cfg)

    def apply_config(self, cfg: HaConfig) -> HaConfig:
        """应用新配置：变更 mode 时同步状态机初始态。"""
        old_mode = self.mode
        self.cfg = cfg
        self.mode = cfg.mode
        if old_mode != cfg.mode:
            # 模式切换：把另一侧状态重置
            if cfg.mode == "primary":
                self.primary_state = "warming"
                self.active_since = None
            elif cfg.mode == "backup":
                self.primary_state = "active"  # 占位
                self.backup_state = "standby"
                self.active_since = None
                self.last_heartbeat_seen = self.now()
            else:
                self.primary_state = "active"
                self.backup_state = "standby"
                self.active_since = None
        return cfg

    def can_act(self) -> bool:
        """闸门：任一模式只有"该模式激活态"才返回 True。"""
        if self.mode == "standalone":
            return True
        if self.mode == "primary":
            return self.primary_state == "active"
        if self.mode == "backup":
            return self.backup_state == "active"
        return False

    def status_payload(self) -> dict[str, Any]:
        """组装 /api/ha/status 应答体。"""
        now = self.now()
        last_seen = self.last_heartbeat_seen
        age: Optional[float] = None
        if last_seen is not None:
            age = max(0.0, now - last_seen)
        return {
            "instance_id": self.cfg.instance_id if self.cfg else "",
            "role": self.mode if self.mode in ("primary", "backup") else "standalone",
            "state": (self.primary_state if self.mode == "primary"
                      else self.backup_state if self.mode == "backup"
                      else "standalone"),
            "active_since": self.active_since,
            "last_heartbeat_age_seconds": age,
            "last_snapshot_at": self.last_snapshot_at,
            "peer_url": self.cfg.peer_url if self.cfg else "",
        }


class NullHaRuntime:
    """scheduler 默认 ha 字段，保证未接线时行为不变。"""

    cfg: Any = None
    mode: str = "standalone"
    primary_state: str = "active"
    backup_state: str = "standby"
    active_since: Optional[float] = None

    def can_act(self) -> bool:
        return True

    def reload(self, store) -> None:  # pragma: no cover
        return None

    def request_snapshot(self) -> None:  # pragma: no cover
        return None

    def status_payload(self) -> dict[str, Any]:  # pragma: no cover
        return {
            "instance_id": "",
            "role": "standalone",
            "state": "standalone",
            "active_since": None,
            "last_heartbeat_age_seconds": None,
            "last_snapshot_at": None,
            "peer_url": "",
        }


# Task 7/8 实装：看护协程 + 主力 tick + 备用 tick

logger = logging.getLogger(__name__)


async def run_ha_supervisor(store, sched, cfg: HaConfig, runtime: HaRuntime) -> None:
    """HA 看护协程主循环：每 min(heartbeat_interval, 5) 秒 tick 一次。

    内部捕获所有异常记日志，不退出（确保任何瞬时网络抖动不杀进程）。
    """
    logger.info("ha: supervisor starting mode=%s key_present=%s", runtime.mode,
                bool(runtime.cfg and runtime.cfg.key))
    while True:
        try:
            interval = min(runtime.cfg.heartbeat_interval if runtime.cfg else 15, 5)
            await asyncio.sleep(max(1, interval))
            try:
                await ensure_ha_bootstrap(store)
                cfg = await load_ha_config(store)
                runtime.apply_config(cfg)
            except Exception as exc:
                logger.warning("ha: reload config failed: %s", exc)
            if runtime.mode == "primary":
                try:
                    await _primary_tick(store, sched, runtime)
                except Exception as exc:
                    logger.warning("ha: primary tick failed: %s", exc)
            elif runtime.mode == "backup":
                try:
                    await _backup_tick(store, sched, runtime)
                except Exception as exc:
                    logger.warning("ha: backup tick failed: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("ha: supervisor loop caught: %s", exc)
            await asyncio.sleep(1)


async def _primary_tick(store, sched, runtime: HaRuntime) -> None:
    """主力侧一个 tick：状态机推进 + 心跳/快照推送 + 公网自检。

    状态机表（spec §6 主力表）：
      warming: 探测备用可达 → 常规重启 / 接管中 → 转 active 让位协商
      active: 心跳失败超 TTL + 公网自检失败 → suspended
      active: 心跳应答 active_since 非空 → 让位 warming
      active: snapshot_requested 或间隔到 → build_snapshot + POST
    """
    if runtime.cfg is None:
        return
    now = runtime.now()
    interval = runtime.cfg.heartbeat_interval
    ttl = runtime.cfg.lease_ttl
    peer = (runtime.cfg.peer_url or "").rstrip("/")

    # 1) 决定是否进入下一态
    if runtime.primary_state == "warming":
        reachable = False
        payload = {}
        if peer:
            try:
                async with httpx.AsyncClient(timeout=5) as c:
                    r = await c.post(
                        f"{peer}/api/ha/bk/heartbeat",
                        headers={"X-HA-Key": runtime.cfg.key},
                        json={"instance_id": runtime.cfg.instance_id},
                    )
                    if r.status_code == 200:
                        payload = r.json() if isinstance(r.json(), dict) else {}
                        reachable = True
            except Exception as exc:
                logger.info("ha: warming peer probe failed: %s", exc)
                reachable = False
                payload = {}

        if not peer:
            # 双端皆死兜底：直进 active
            runtime.primary_state = "active"
            runtime.active_since = now
            logger.warning("ha: warming -> active (no peer_url configured)")
        elif reachable and payload.get("active_since") is None:
            # 备用待命，常规重启秒级恢复
            runtime.primary_state = "active"
            runtime.active_since = now
            logger.warning("ha: warming -> active (backup standby)")
        elif not reachable:
            grace = runtime.cfg.failback_grace
            if now - runtime.boot_monotonic > grace:
                runtime.primary_state = "active"
                runtime.active_since = now
                logger.warning("ha: warming -> active (peer unreachable after grace, fallback)")
                try:
                    await store.add_notification(
                        "双端皆死兜底",
                        "对端不可达已超容忍窗口，主力兜底接管调度。",
                        level="warning",
                    )
                except Exception:
                    pass
            else:
                logger.info(
                    "ha: warming awaiting peer (unreachable, elapsed=%.1fs, grace=%ds)",
                    now - runtime.boot_monotonic,
                    grace,
                )
        else:
            active_since = payload.get("active_since")
            if payload.get("role") != "backup" or active_since is None:
                runtime.primary_state = "active"
                runtime.active_since = now
            else:
                logger.info("ha: warming awaiting failback (backup active_since=%s)", active_since)
        return

    if runtime.primary_state == "suspended":
        if peer:
            try:
                async with httpx.AsyncClient(timeout=5, trust_env=False) as c:
                    r = await c.post(
                        f"{peer}/api/ha/bk/heartbeat",
                        headers={"X-HA-Key": runtime.cfg.key},
                        json={"instance_id": runtime.cfg.instance_id},
                    )
                    if r.status_code == 200:
                        runtime.primary_state = "active"
                        runtime.active_since = now
                        runtime.last_heartbeat_sent_ok = now
                        runtime.last_heartbeat_seen = now
                        runtime.last_primary_contact = now
                        logger.info("ha: suspended -> active (heartbeat recovered)")
                        try:
                            await store.add_notification(
                                "主力恢复",
                                "心跳恢复，主力已重新接管调度。",
                                level="info",
                            )
                        except Exception:
                            pass
            except Exception as exc:
                logger.debug("ha: suspended heartbeat probe failed: %s", exc)
        return

    if runtime.primary_state != "active":
        return

    # 2) 心跳推送
    if not peer:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(
                f"{peer}/api/ha/bk/heartbeat",
                headers={"X-HA-Key": runtime.cfg.key},
                json={"instance_id": runtime.cfg.instance_id},
            )
            if r.status_code == 200:
                body = r.json()
                runtime.last_heartbeat_sent_ok = now
                runtime.last_heartbeat_seen = now
                runtime.last_primary_contact = now
                if body.get("active_since") is not None:
                    # 应答者展示备用仍活跃 → 分区愈合让位
                    runtime.primary_state = "warming"
                    runtime.active_since = None
                    logger.warning("ha: demoting to warming (backup still active)")
                    return
            else:
                raise RuntimeError(f"heartbeat http {r.status_code}")
    except Exception as exc:
        # 心跳失败 → 检查 ttl 是否超期
        last = runtime.last_heartbeat_sent_ok or 0.0
        if now - last >= ttl:
            # 公网自检：GET /api/ha/status
            try:
                async with httpx.AsyncClient(timeout=5) as c:
                    r = await c.get(
                        f"{peer}/api/ha/status",
                        headers={"X-HA-Key": runtime.cfg.key},
                    )
                    status_body = r.json() if r.status_code == 200 else {}
            except Exception as exc2:
                logger.warning("ha: tunnel probe failed: %s", exc2)
                status_body = {}
            responder = status_body.get("instance_id") if isinstance(status_body, dict) else None
            if responder and responder != runtime.cfg.instance_id:
                # 应答者是备用或第三方 → 备用进程在跑，公网活着，但 /bk/* 挂
                logger.warning("ha: tunnel alive but backup /bk/* dead; stay active")
            elif responder == runtime.cfg.instance_id:
                # 公网自检应答者是自己 → frps 隧道在、备用进程挂了
                logger.warning("ha: backup process unreachable, stay active (warn-only)")
            else:
                runtime.primary_state = "suspended"
                logger.warning("ha: -> suspended (peer + tunnel dead)")
                try:
                    await store.add_notification(
                        "主力暂停",
                        f"心跳失败且公网自检失败；已停止真实调度。可在设置页恢复。",
                        level="error",
                    )
                except Exception:
                    pass
        return

    # 3) 快照推送
    need_snap = runtime.consume_snapshot_request()
    if not need_snap and runtime.last_snapshot_at is None:
        need_snap = True
    if not need_snap and runtime.last_snapshot_at is not None:
        if now - runtime.last_snapshot_at >= runtime.cfg.snapshot_interval:
            need_snap = True
    if need_snap:
        try:
            from seatbot.ha_sync import build_snapshot
            payload, _ = await build_snapshot(store)
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(
                    f"{peer}/api/ha/bk/snapshot",
                    headers={"X-HA-Key": runtime.cfg.key},
                    content=payload,
                )
                if r.status_code == 200:
                    runtime.last_snapshot_at = now
                else:
                    logger.warning("ha: snapshot push http %s", r.status_code)
        except Exception as exc:
            logger.warning("ha: snapshot push failed: %s", exc)


async def _backup_tick(store, sched, runtime: HaRuntime) -> None:
    """Task 8 实装：备用看门狗 + 激活自愈 + failback_pending 处理。"""
    if runtime.cfg is None:
        return
    now = runtime.now()
    ttl = runtime.cfg.lease_ttl
    buf = runtime.cfg.activation_buffer
    grace = runtime.cfg.failback_grace

    if runtime.backup_state == "standby":
        last = runtime.last_heartbeat_seen or runtime.boot_monotonic
        if now - last >= ttl + buf:
            # 进入 active
            runtime.backup_state = "active"
            runtime.active_since = now
            logger.warning("ha: backup -> active (heartbeat silence)")
            try:
                await store.add_notification(
                    "备用接管",
                    f"心跳静默 {int(now - last)}s 超阈值（{ttl}+{buf}），开始接管调度。",
                    level="warn",
                )
            except Exception:
                pass
            try:
                asyncio.create_task(_after_activation(store, sched))
            except Exception:
                pass
        return

    if runtime.backup_state == "active":
        # 收到心跳的逻辑在 bk_heartbeat 路由已转 failback_pending；这里仅超时回活
        return

    if runtime.backup_state == "failback_pending":
        last = runtime.last_primary_contact or 0.0
        if now - last >= grace:
            runtime.backup_state = "active"
            runtime.active_since = now
            logger.warning("ha: failback_pending -> active (grace exceeded)")
            try:
                await store.add_notification(
                    "回切超时回活",
                    "备用处于 failback_pending 超 grace 未完成 → 重新接管",
                    level="warn",
                )
            except Exception:
                pass
        return


async def _after_activation(store, sched) -> None:
    """激活后自愈：对账 + 用户硬预约同步。"""
    try:
        if sched is not None and hasattr(sched, "reconcile_sweep"):
            await sched.reconcile_sweep(write=True)
    except Exception as exc:
        logger.warning("ha: reconcile_sweep failed: %s", exc)
    try:
        if sched is not None and hasattr(sched, "sync_user_reserved"):
            await sched.sync_user_reserved()
    except Exception as exc:
        logger.warning("ha: sync_user_reserved failed: %s", exc)


async def _push_restore_loop(store, runtime: HaRuntime) -> None:
    """Task 8 实装：循环推送自身快照回主力，等主力确认后 standby。"""
    if runtime.cfg is None or runtime.mode != "backup":
        return
    peer = (runtime.cfg.peer_url or "").rstrip("/")
    if not peer:
        return
    from seatbot.ha_sync import build_snapshot
    deadline = runtime.now() + runtime.cfg.failback_grace
    while runtime.now() < deadline and runtime.backup_state == "failback_pending":
        try:
            payload, _ = await build_snapshot(store)
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(
                    f"{peer}/api/ha/restore",
                    headers={"X-HA-Key": runtime.cfg.key},
                    content=payload,
                )
                if r.status_code == 200:
                    runtime.backup_state = "standby"
                    runtime.active_since = None
                    logger.warning("ha: backup -> standby (restore accepted)")
                    try:
                        await store.add_notification(
                            "备用退位",
                            "主力回暖成功接管，备用退回 standby。",
                            level="info",
                        )
                    except Exception:
                        pass
                    return
        except Exception as exc:
            logger.warning("ha: push restore failed: %s", exc)
        await asyncio.sleep(5)


__all__ = [
    "HA_MODES",
    "HA_DEFAULTS",
    "HA_LOCAL_KEYS",
    "SCHEMA_VERSION",
    "HaConfig",
    "HaRuntime",
    "NullHaRuntime",
    "generate_ha_key",
    "load_ha_config",
    "ensure_ha_bootstrap",
    "run_ha_supervisor",
]