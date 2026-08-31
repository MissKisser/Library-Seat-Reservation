"""YAML config loader with pydantic validation (v2: multi-seat)."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class LibraryConfig(BaseModel):
    room_id: int
    room_name: str
    # v2: removed `target_seat_num` — moved to DB table `target_seats` managed via Web
    open_time: str = "08:00"
    close_time: str = "22:00"
    max_reserve_hours: float = 2.0
    #: 每个账号每天累计预约时长上限（超星每日限额）
    daily_reserve_hours_limit: float = 5.0


# slots is either the literal "full" or a list of "HH:MM-HH:MM" ranges
SlotSpec = str | list[str]


class SeatSeed(BaseModel):
    """目标座位的初始配置，运行时以页面设置为准。"""
    seat_num: str
    label: str = ""

    @field_validator("seat_num")
    @classmethod
    def _seat_num_format(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit() or not (1 <= len(v) <= 4):
            raise ValueError(f"seat_num must be 1-4 digit number, got {v!r}")
        return v.zfill(3)


class UserReservedSeed(BaseModel):
    """用户亲述已预约的段, scheduler 必须跳过 (避免重复预约, 避免覆盖用户预约)。"""
    account_id: str
    seat_num: str
    day: str       # 'YYYY-MM-DD'
    start_time: str  # 'HH:MM'
    end_time: str    # 'HH:MM'
    note: str = ""

    @field_validator("day")
    @classmethod
    def _day_format(cls, v: str) -> str:
        from datetime import date
        try:
            date.fromisoformat(v)
        except Exception as e:
            raise ValueError(f"day must be YYYY-MM-DD, got {v!r}") from e
        return v


class AccountConfig(BaseModel):
    id: str
    phone: str
    password: str
    slots: SlotSpec | None = None
    bound_seats: list[str] = Field(default_factory=list)
    # ★ v2 (2026-08-24): per-seat slots — 同一账号对不同座位可以预约不同时段
    # 例如 {"104": ["09:00-11:00"], "105": ["15:00-17:00"]} 表示
    # 该账号对 104 守 09-11,对 105 守 15-17。
    # 与 slots 互斥:同时设置时 planner 用 seat_slots 忽略 slots。
    seat_slots: dict[str, SlotSpec] | None = None

    @field_validator("slots")
    @classmethod
    def _slots_format(cls, v: SlotSpec) -> SlotSpec:
        if v == "full":
            return v
        if not isinstance(v, list):
            raise ValueError("slots must be 'full' or list of 'HH:MM-HH:MM'")
        for r in v:
            if not isinstance(r, str) or len(r.split("-")) != 2:
                raise ValueError(f"invalid slot range: {r!r}")
        return v

    @field_validator("bound_seats")
    @classmethod
    def _bound_seats_format(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for s in v:
            s2 = s.strip()
            if not s2.isdigit() or not (1 <= len(s2) <= 4):
                raise ValueError(f"bound seat_num must be 1-4 digit number, got {s!r}")
            out.append(s2.zfill(3))
        return out


class RuntimeConfig(BaseModel):
    stagger_seconds: list[int] = Field(default_factory=lambda: [0, 3])
    # 兼容保留；新代码以 submit_strategy 为准（见 seatbot/settings.py）。
    direct_submit_enabled: bool = True
    # 提交策略（四档，可在系统设置页调整，未调整时沿用此处配置）：
    #   direct_first / direct_only / page_rewrite_first / page_rewrite_only
    submit_strategy: str = "direct_first"
    relay_lead_seconds: int = 300
    tick_interval_seconds: int = 30
    anchor_retry_enabled: bool = True
    anchor_scan_limit: int = 12
    #: 通知外推 webhook；未设置时仅在看板展示。
    #: 触发时 POST JSON {"title": ..., "content": ...}
    notify_webhook: str = ""
    #: 后台实况核对间隔（秒，可在系统设置页调整，未调整时沿用此处配置）
    reconcile_interval_seconds: int = 90
    log_dir: str = "./logs"
    db_path: str = "./seatbot.db"
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    #: 面板访问令牌。非空时所有请求必须携带 (Authorization: Bearer / X-Auth-Token / ?token= / cookie);
    #: 为空时仅允许本机回环客户端访问, 非回环客户端一律 403。
    web_token: str = ""

    @field_validator("submit_strategy")
    @classmethod
    def _strategy_format(cls, v: str) -> str:
        from seatbot.settings import SUBMIT_STRATEGIES
        if v in SUBMIT_STRATEGIES:
            return v
        raise ValueError(f"submit_strategy must be one of {', '.join(SUBMIT_STRATEGIES)}")

    @field_validator("relay_lead_seconds")
    @classmethod
    def _relay_lead_format(cls, v: int) -> int:
        if not (30 <= int(v) <= 900):
            raise ValueError("relay_lead_seconds must be 30–900")
        return int(v)

    @field_validator("tick_interval_seconds")
    @classmethod
    def _tick_interval_format(cls, v: int) -> int:
        if not (5 <= int(v) <= 300):
            raise ValueError("tick_interval_seconds must be 5–300")
        return int(v)

    @field_validator("anchor_scan_limit")
    @classmethod
    def _anchor_limit_format(cls, v: int) -> int:
        if not (4 <= int(v) <= 20):
            raise ValueError("anchor_scan_limit must be 4–20")
        return int(v)

    @field_validator("reconcile_interval_seconds")
    @classmethod
    def _reconcile_interval_format(cls, v: int) -> int:
        if not (60 <= int(v) <= 86400):
            raise ValueError("reconcile_interval_seconds must be 60–86400")
        return int(v)


class Config(BaseModel):
    library: LibraryConfig
    # 目标座位的初始配置
    target_seats: list[SeatSeed] = Field(default_factory=list)
    accounts: list[AccountConfig] = []  # 可选；主要通过页面管理
    # 用户已预约的时段（调度器会跳过，避免重复预约）
    user_reserved: list[UserReservedSeed] = Field(default_factory=list)
    runtime: RuntimeConfig

    @model_validator(mode="after")
    def _unique_ids(self) -> "Config":
        ids = [a.id for a in self.accounts]
        if len(ids) != len(set(ids)):
            raise ValueError("account ids must be unique")
        seats = [s.seat_num for s in self.target_seats]
        if len(seats) != len(set(seats)):
            raise ValueError("target_seats seat_num must be unique")
        # 空列表是允许的，页面是主要的账号/座位管理方式。
        # 配置文件仅提供初始值。
        return self


class ConfigError(Exception):
    pass


def load_config(path: Path | str) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML: {e}") from e
    try:
        return Config(**raw)
    except Exception as e:
        raise ConfigError(f"config validation failed: {e}") from e
