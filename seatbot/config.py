"""YAML config loader with pydantic validation."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class LibraryConfig(BaseModel):
    room_id: int
    room_name: str
    target_seat_num: str  # the seat all guard accounts fight to occupy
    time_unit_minutes: int = 30
    open_time: str = "08:00"
    close_time: str = "22:00"
    max_reserve_hours: float = 2.0

    @field_validator("target_seat_num")
    @classmethod
    def _seat_num_format(cls, v: str) -> str:
        if not v.isdigit() or not (1 <= len(v) <= 4):
            raise ValueError(f"target_seat_num must be 1-4 digit number, got {v!r}")
        return v.zfill(3)  # normalize to 3-digit zero-padded


# slots is either the literal "full" or a list of "HH:MM-HH:MM" ranges
SlotSpec = str | list[str]


class AccountConfig(BaseModel):
    id: str
    phone: str
    password: str
    slots: SlotSpec

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


class RuntimeConfig(BaseModel):
    stagger_seconds: list[int] = Field(default_factory=lambda: [0, 3])
    relogin_on_401: bool = True
    random_ua: bool = True
    log_dir: str = "./logs"
    db_path: str = "./seatbot.db"
    web_host: str = "0.0.0.0"
    web_port: int = 8080


class Config(BaseModel):
    library: LibraryConfig
    accounts: list[AccountConfig] = []  # optional; web panel is the primary source
    runtime: RuntimeConfig

    @model_validator(mode="after")
    def _unique_ids(self) -> "Config":
        ids = [a.id for a in self.accounts]
        if len(ids) != len(set(ids)):
            raise ValueError("account ids must be unique")
        # NOTE: an empty `accounts` list is allowed. The Web panel is the
        # primary way to manage accounts (database is the source of truth
        # at runtime). Config is only a seed file.
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
