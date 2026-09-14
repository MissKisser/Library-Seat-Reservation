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
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional


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
        """测试用：推进假时钟。"""
        boot = self.boot_monotonic
        self.boot_monotonic = boot
        self._clock = (lambda b=boot, s=seconds: lambda: b + s)  # noqa: E731

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


# Task 7/8 占用位：真正的看护协程在后续任务实装。
async def run_ha_supervisor(store, sched, cfg: HaConfig, runtime: HaRuntime) -> None:
    """占位：完整实装在 Task 7/8。"""
    raise NotImplementedError("实装见 Task 7/8")


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