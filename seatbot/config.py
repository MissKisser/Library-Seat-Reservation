"""YAML config loader with pydantic validation (v2: multi-seat)."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class LibraryConfig(BaseModel):
    room_id: int
    room_name: str
    # v2: removed `target_seat_num` — moved to DB table `target_seats` managed via Web
    time_unit_minutes: int = 30
    open_time: str = "08:00"
    close_time: str = "22:00"
    max_reserve_hours: float = 2.0
    # ★ 移动端 deptIdEnc，用于 getusedtimes 接口查询他人占用时段。
    # 与 PC web 端的 fidEnc 不同，必须用移动端版本，否则返回空数组。
    # 获取方式见 docs/superpowers/specs/2026-07-09-chaoxing-api-reference.md §11。
    # 快捷方法：手机打开学习通→座位页→Chrome DevTools inspect WebView，
    #   在任意请求的 URL 中找 deptIdEnc=XXXXXXXX 参数即是。
    fid_enc: str = ""


# slots is either the literal "full" or a list of "HH:MM-HH:MM" ranges
SlotSpec = str | list[str]


class SeatSeed(BaseModel):
    """Seed entry for target_seats table. Web is the source of truth at runtime."""
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
    # ★ v2: 该账号每天最多同时持有的预约段数 (1=悲观, 3=乐观)
    one_account_max_concurrent_segments_per_day: int = 1

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
                raise ValueError(f"bound seat_num must be 1-4 digit, got {s!r}")
            out.append(s2.zfill(3))
        return out

    @field_validator("one_account_max_concurrent_segments_per_day")
    @classmethod
    def _max_segments(cls, v: int) -> int:
        if v < 1:
            raise ValueError("must be ≥ 1")
        return v


class RuntimeConfig(BaseModel):
    stagger_seconds: list[int] = Field(default_factory=lambda: [0, 3])
    relogin_on_401: bool = True
    random_ua: bool = True
    # ★ v2: 全局默认 (账号级可覆盖)
    one_account_max_concurrent_segments_per_day_default: int = 1
    log_dir: str = "./logs"
    db_path: str = "./seatbot.db"
    web_host: str = "0.0.0.0"
    web_port: int = 8080


class Config(BaseModel):
    library: LibraryConfig
    # ★ v2: target_seats (seed for DB table)
    target_seats: list[SeatSeed] = Field(default_factory=list)
    accounts: list[AccountConfig] = []  # optional; web panel is the primary source
    # ★ v2+: 用户亲述已预约的段 (scheduler 跳过)
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
        # NOTE: empty lists are allowed. Web panel is the primary way
        # to manage accounts/seats (database is the source of truth).
        # Config is only a seed file.
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
