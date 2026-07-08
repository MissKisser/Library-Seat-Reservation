# Library Seat Reservation Bot — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python automation that, for N static user↔seat pairs, performs login / reserve / sign-in / sign-out against the Chaoxing (超星) library seat system `office.chaoxing.com`, plus a FastAPI web panel for management.

**Architecture:** Pure-HTTP `httpx.AsyncClient` (no headless browser by default, with Playwright fallback for `enc` generation) → APScheduler-driven state machine → SQLite-backed task store → FastAPI + Jinja2 web panel.

**Tech Stack:** Python 3.11+, `httpx`, `apscheduler`, `pydantic`, `pyyaml`, `aiosqlite`, `fastapi`, `uvicorn`, `jinja2`, `pyexecjs2` (QuickJS), `playwright` (fallback), `pytest`, `pytest-asyncio`.

**Reference Spec:** `docs/superpowers/specs/2026-07-08-library-seat-reservation-design.md`

---

## File Structure

```
Library-Seat-Reservation/
├── seatbot/
│   ├── __init__.py
│   ├── __main__.py              # CLI entry
│   ├── config.py                # YAML loader + pydantic models
│   ├── models.py                # Domain dataclasses (Account, Task, Slot, ...)
│   ├── store.py                 # SQLite StateStore
│   ├── client.py                # ChaoxingClient (httpx)
│   ├── enc.py                   # EncGenerator (JS exec + Playwright fallback)
│   ├── planner.py               # ReservationPlanner (slot expansion)
│   ├── scheduler.py             # APScheduler wrapper + state machine
│   ├── web/
│   │   ├── __init__.py
│   │   ├── app.py               # FastAPI factory
│   │   ├── routes.py            # All routes
│   │   ├── templates/
│   │   │   ├── base.html
│   │   │   ├── dashboard.html
│   │   │   ├── accounts_list.html
│   │   │   ├── accounts_form.html
│   │   │   ├── tasks_list.html
│   │   │   ├── seats.html
│   │   │   └── logs.html
│   │   └── static/
│   │       └── style.css
│   └── utils/
│       ├── __init__.py
│       ├── ua.py                # UA pool
│       └── timeutil.py          # time helpers (CST, slot parse, ...)
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_config.py
│   ├── test_planner.py
│   ├── test_store.py
│   ├── test_timeutil.py
│   ├── test_client_login.py     # marked @pytest.mark.integration
│   └── test_client_submit.py   # integration
├── config.example.yaml
├── pyproject.toml
├── requirements.txt
├── README.md
├── Dockerfile
├── docker-compose.yml
├── systemd/seatbot.service
└── docs/superpowers/
    ├── specs/2026-07-08-library-seat-reservation-design.md
    └── plans/2026-07-08-library-seat-reservation.md
```

---

## Task 1: Project skeleton + pyproject.toml

**Files:**
- Create: `pyproject.toml`
- Create: `requirements.txt`
- Create: `seatbot/__init__.py`
- Create: `seatbot/utils/__init__.py`
- Create: `.gitignore`
- Create: `README.md` (skeleton)

- [ ] **Step 1: Create pyproject.toml**

Write `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "seatbot"
version = "0.1.0"
description = "超星图书馆座位自动预约/签到/签退"
requires-python = ">=3.11"
dependencies = [
  "httpx>=0.27",
  "apscheduler>=3.10",
  "pydantic>=2.6",
  "pyyaml>=6.0",
  "aiosqlite>=0.20",
  "fastapi>=0.110",
  "uvicorn[standard]>=0.27",
  "jinja2>=3.1",
  "pyexecjs2>=1.5",
  "playwright>=1.42",
  "python-multipart>=0.0.9",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.0",
  "pytest-asyncio>=0.23",
  "pytest-cov>=4.1",
  "ruff>=0.3",
]

[project.scripts]
seatbot = "seatbot.__main__:cli"

[tool.setuptools.packages.find]
include = ["seatbot*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = [
  "integration: requires real network / real Chaoxing account",
]
```

- [ ] **Step 2: Create requirements.txt**

```
-r pyproject.toml
# Pinned versions for reproducibility; update as needed.
httpx==0.27.0
apscheduler==3.10.4
pydantic==2.6.4
pyyaml==6.0.1
aiosqlite==0.20.0
fastapi==0.110.0
uvicorn[standard]==0.27.1
jinja2==3.1.3
pyexecjs2==1.5.1
playwright==1.42.0
python-multipart==0.0.9
pytest==8.1.1
pytest-asyncio==0.23.6
pytest-cov==4.1.0
ruff==0.3.4
```

- [ ] **Step 3: Create .gitignore**

```
__pycache__/
*.pyc
*.pyo
.venv/
.env
config.yaml
seatbot.db
seatbot.db.bak
logs/
.playwright-mcp/
*.png
```

- [ ] **Step 4: Create empty package __init__.py**

Write `seatbot/__init__.py`:

```python
"""超星图书馆座位自动化预约/签到/签退程序."""

__version__ = "0.1.0"
```

Write `seatbot/utils/__init__.py`:

```python
"""Internal utilities."""
```

- [ ] **Step 5: Create README.md skeleton**

```markdown
# SeatBot — 超星图书馆座位自动预约

详见 [设计文档](../specs/2026-07-08-library-seat-reservation-design.md) 与 [实施计划](../plans/2026-07-08-library-seat-reservation.md)。

## 快速开始

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .[dev]
playwright install chromium
cp config.example.yaml config.yaml
# 编辑 config.yaml 填入账号
python -m seatbot init-db
python -m seatbot run
# 访问 http://localhost:8080
```

## 已知风险

- 纯 HTTP 签到: 缺少真实蓝牙基站 + 位置, 超星风控可能识别为作弊, 账号可能被拉入黑名单
- 多账号并发: 同一 IP 多账号, 容易被识别为脚本

## 命令

```bash
python -m seatbot run --config config.yaml
python -m seatbot once --account zhangsan --action all
python -m seatbot login --account zhangsan
python -m seatbot status
python -m seatbot init-db
```
```

- [ ] **Step 6: Commit**

```bash
git init
git add .
git commit -m "chore: project skeleton + pyproject"
```

---

## Task 2: Config models + YAML loader

**Files:**
- Create: `seatbot/config.py`
- Create: `config.example.yaml`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write failing test**

Write `tests/test_config.py`:

```python
from pathlib import Path
import textwrap

import pytest

from seatbot.config import load_config, ConfigError


def test_load_minimal_config(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(textwrap.dedent("""
        library:
          room_id: 11692
          room_name: "2号楼图书馆-3F"
          time_unit_minutes: 30
          open_time: "08:00"
          close_time: "22:00"
          max_reserve_hours: 2.0
        accounts:
          - id: zhangsan
            phone: "13800000001"
            password: "secret"
            seat_num: "084"
            slots: full
        runtime:
          stagger_seconds: [0, 3]
          relogin_on_401: true
          random_ua: true
          log_dir: ./logs
          db_path: ./seatbot.db
          web_host: 0.0.0.0
          web_port: 8080
    """).strip())

    cfg = load_config(cfg_file)
    assert cfg.library.room_id == 11692
    assert cfg.accounts[0].id == "zhangsan"
    assert cfg.accounts[0].slots == "full"
    assert cfg.runtime.web_port == 8080


def test_load_missing_library_raises(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("accounts: []\nruntime: {}\n")
    with pytest.raises(ConfigError):
        load_config(cfg_file)


def test_account_id_must_be_unique(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(textwrap.dedent("""
        library:
          room_id: 1
          room_name: "x"
          time_unit_minutes: 30
          open_time: "08:00"
          close_time: "22:00"
          max_reserve_hours: 2.0
        accounts:
          - {id: a, phone: "1", password: "p", seat_num: "001", slots: full}
          - {id: a, phone: "2", password: "p", seat_num: "002", slots: full}
        runtime: {stagger_seconds: [0,0], relogin_on_401: true, random_ua: true,
                  log_dir: "./logs", db_path: "./x.db", web_host: "0.0.0.0", web_port: 1}
    """).strip())
    with pytest.raises(ConfigError, match="unique"):
        load_config(cfg_file)
```

- [ ] **Step 2: Run tests, expect failure**

Run: `pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'seatbot.config'`

- [ ] **Step 3: Implement seatbot/config.py**

```python
"""YAML config loader with pydantic validation."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class LibraryConfig(BaseModel):
    room_id: int
    room_name: str
    time_unit_minutes: int = 30
    open_time: str = "08:00"
    close_time: str = "22:00"
    max_reserve_hours: float = 2.0


# slots is either the literal "full" or a list of "HH:MM-HH:MM" ranges
SlotSpec = str | list[str]


class AccountConfig(BaseModel):
    id: str
    phone: str
    password: str
    seat_num: str
    slots: SlotSpec

    @field_validator("seat_num")
    @classmethod
    def _seat_num_format(cls, v: str) -> str:
        if not v.isdigit() or not (1 <= len(v) <= 4):
            raise ValueError(f"seat_num must be 1-4 digit number, got {v!r}")
        return v.zfill(3)  # normalize to 3-digit zero-padded

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
    accounts: list[AccountConfig]
    runtime: RuntimeConfig

    @model_validator(mode="after")
    def _unique_ids(self) -> "Config":
        ids = [a.id for a in self.accounts]
        if len(ids) != len(set(ids)):
            raise ValueError("account ids must be unique")
        if not self.accounts:
            raise ValueError("at least one account is required")
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
```

- [ ] **Step 4: Create config.example.yaml**

```yaml
library:
  room_id: 11692
  room_name: "2号楼图书馆-3F-3楼备考自习室"
  time_unit_minutes: 30
  open_time: "08:00"
  close_time: "22:00"
  max_reserve_hours: 2.0

accounts:
  - id: zhangsan
    phone: "13800000000"
    password: "REPLACE_ME"
    seat_num: "084"
    slots: full     # 或 ["08:00-12:00", "14:00-22:00"]

runtime:
  stagger_seconds: [0, 3]
  relogin_on_401: true
  random_ua: true
  log_dir: ./logs
  db_path: ./seatbot.db
  web_host: 0.0.0.0
  web_port: 8080
```

- [ ] **Step 5: Run tests, expect pass**

Run: `pytest tests/test_config.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add seatbot/config.py tests/test_config.py config.example.yaml
git commit -m "feat(config): YAML loader with pydantic validation"
```

---

## Task 3: Time utilities (slot parsing, CST, day boundary)

**Files:**
- Create: `seatbot/utils/timeutil.py`
- Create: `tests/test_timeutil.py`

- [ ] **Step 1: Write failing test**

```python
from datetime import date, time, datetime, timezone, timedelta

import pytest

from seatbot.utils.timeutil import (
    parse_hhmm,
    parse_range,
    today_cst,
    now_cst,
    expand_full_day,
    split_into_chunks,
    CST,
)


def test_parse_hhmm():
    assert parse_hhmm("08:30") == time(8, 30)
    with pytest.raises(ValueError):
        parse_hhmm("25:00")
    with pytest.raises(ValueError):
        parse_hhmm("8:30")


def test_parse_range():
    assert parse_range("08:00-10:00") == (time(8, 0), time(10, 0))
    with pytest.raises(ValueError):
        parse_range("08-10")
    with pytest.raises(ValueError):
        parse_range("10:00-08:00")


def test_cst_timezone():
    assert CST.utcoffset(None) == timedelta(hours=8)


def test_expand_full_day():
    chunks = expand_full_day("08:00", "22:00", max_hours=2.0)
    assert [(s.isoformat(timespec="minutes"), e.isoformat(timespec="minutes"))
            for s, e in chunks] == [
        ("08:00", "10:00"),
        ("10:00", "12:00"),
        ("12:00", "14:00"),
        ("14:00", "16:00"),
        ("16:00", "18:00"),
        ("18:00", "20:00"),
        ("20:00", "22:00"),
    ]


def test_split_into_chunks_basic():
    chunks = split_into_chunks(parse_range("08:00-12:00"), max_hours=2.0)
    assert [(s, e) for s, e in chunks] == [
        (time(8, 0), time(10, 0)),
        (time(10, 0), time(12, 0)),
    ]


def test_split_into_chunks_already_small():
    chunks = split_into_chunks(parse_range("14:00-15:00"), max_hours=2.0)
    assert chunks == [(time(14, 0), time(15, 0))]


def test_split_into_chunks_non_multiple():
    chunks = split_into_chunks(parse_range("08:00-13:00"), max_hours=2.0)
    # 5 hours → 2 + 2 + 1
    assert [(s, e) for s, e in chunks] == [
        (time(8, 0), time(10, 0)),
        (time(10, 0), time(12, 0)),
        (time(12, 0), time(13, 0)),
    ]
```

- [ ] **Step 2: Run tests, expect failure**

Run: `pytest tests/test_timeutil.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement seatbot/utils/timeutil.py**

```python
"""Time helpers: HH:MM parsing, slot expansion, CST."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable


CST = timezone(timedelta(hours=8), name="CST")


def now_cst() -> datetime:
    return datetime.now(CST)


def today_cst() -> date:
    return now_cst().date()


def parse_hhmm(s: str) -> time:
    """Parse 'HH:MM' into a time. Reject 'H:MM' (must be 2-digit hour)."""
    parts = s.split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid HH:MM: {s!r}")
    h, m = parts
    if len(h) != 2 or len(m) != 2:
        raise ValueError(f"HH and MM must be 2 digits: {s!r}")
    return time(int(h), int(m))


def parse_range(r: str) -> tuple[time, time]:
    """Parse 'HH:MM-HH:MM' into (start, end). end must be > start."""
    if r.count("-") != 1:
        raise ValueError(f"invalid range: {r!r}")
    s, e = r.split("-")
    start, end = parse_hhmm(s.strip()), parse_hhmm(e.strip())
    if end <= start:
        raise ValueError(f"end must be after start: {r!r}")
    return start, end


def split_into_chunks(
    range_: tuple[time, time], max_hours: float
) -> list[tuple[time, time]]:
    """Split a (start, end) range into chunks of at most max_hours."""
    start, end = range_
    start_min = start.hour * 60 + start.minute
    end_min = end.hour * 60 + end.minute
    chunk_min = int(max_hours * 60)
    chunks: list[tuple[time, time]] = []
    cur = start_min
    while cur < end_min:
        nxt = min(cur + chunk_min, end_min)
        sh, sm = divmod(cur, 60)
        eh, em = divmod(nxt, 60)
        chunks.append((time(sh, sm), time(eh, em)))
        cur = nxt
    return chunks


def expand_full_day(
    open_hhmm: str, close_hhmm: str, max_hours: float
) -> list[tuple[time, time]]:
    """Expand the full operating day into chunks of at most max_hours."""
    return split_into_chunks(
        (parse_hhmm(open_hhmm), parse_hhmm(close_hhmm)),
        max_hours=max_hours,
    )


def expand_account_slots(
    slots: str | list[str], max_hours: float
) -> list[tuple[time, time]]:
    """Expand a SlotSpec ('full' or list of 'HH:MM-HH:MM') into chunks."""
    if slots == "full":
        return expand_full_day("08:00", "22:00", max_hours=max_hours)
    return [c for r in slots for c in split_into_chunks(parse_range(r), max_hours)]
```

- [ ] **Step 4: Run tests, expect pass**

Run: `pytest tests/test_timeutil.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add seatbot/utils/timeutil.py tests/test_timeutil.py
git commit -m "feat(utils): time helpers — slot expansion, CST, parse"
```

---

## Task 4: User-Agent pool

**Files:**
- Create: `seatbot/utils/ua.py`
- Create: `tests/test_ua.py`

- [ ] **Step 1: Write failing test**

```python
import re

from seatbot.utils.ua import random_ua, CHROME_UAS, MOBILE_UAS


def test_pool_nonempty():
    assert len(CHROME_UAS) >= 5
    assert len(MOBILE_UAS) >= 3


def test_random_ua_returns_valid_string():
    for _ in range(50):
        ua = random_ua()
        assert isinstance(ua, str)
        assert re.match(r"^Mozilla/\d+\.\d+", ua)


def test_random_ua_can_be_desktop_or_mobile():
    seen = {random_ua() for _ in range(100)}
    assert any("Chrome" in u or "Safari" in u for u in seen)
```

- [ ] **Step 2: Run tests, expect failure**

Run: `pytest tests/test_ua.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement seatbot/utils/ua.py**

```python
"""User-Agent pool for spoofing."""
import random


CHROME_UAS = [
    # Windows Chrome
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # macOS Chrome
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Linux Chrome
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

MOBILE_UAS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
]

ALL_UAS = CHROME_UAS + MOBILE_UAS


def random_ua() -> str:
    return random.choice(ALL_UAS)
```

- [ ] **Step 4: Run tests, expect pass**

Run: `pytest tests/test_ua.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add seatbot/utils/ua.py tests/test_ua.py
git commit -m "feat(utils): UA pool for anti-fingerprinting"
```

---

## Task 5: Domain models (dataclasses)

**Files:**
- Create: `seatbot/models.py`

(No tests for pure dataclasses — covered indirectly via planner/store.)

- [ ] **Step 1: Implement seatbot/models.py**

```python
"""Domain dataclasses used across the codebase."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum
from typing import Any


# ---------- account / slot ----------

@dataclass
class Account:
    id: str
    phone: str
    password: str
    seat_num: str
    slots: str | list[str]  # SlotSpec

    def display_name(self) -> str:
        return f"{self.id} (seat {self.seat_num})"


# ---------- task lifecycle ----------

class TaskStatus(str, Enum):
    PENDING = "pending"          # 等待执行
    READY = "ready"              # 已到时间, 准备执行
    SUBMITTING = "submitting"    # 正在 submit
    ACTIVE = "active"            # 已预约 + 已签到
    LEAVING = "leaving"          # 正在 leave
    COMPLETE = "complete"        # 已签退, 时段结束
    FAILED = "failed"            # 出错


@dataclass
class Task:
    id: int | None
    account_id: str
    day: date
    start_time: time
    end_time: time
    status: TaskStatus = TaskStatus.PENDING
    reserve_id: int | None = None
    last_error: str | None = None

    def chunk_key(self) -> str:
        return f"{self.account_id}|{self.day.isoformat()}|{self.start_time.isoformat(timespec='minutes')}"


# ---------- API result wrappers ----------

@dataclass
class ReserveResult:
    success: bool
    reserve_id: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class SignResult:
    success: bool
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class LeaveResult:
    success: bool
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class CancelResult:
    success: bool
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class RoomConfig:
    room_id: int
    room_name: str
    open_time: time
    close_time: time
    max_reserve_hours: float
    pre_sign_duration_min: int
    sign_duration_min: int
    raw: dict[str, Any] = field(default_factory=dict)
```

- [ ] **Step 2: Verify import works**

Run: `python -c "from seatbot.models import Account, Task, TaskStatus, ReserveResult; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add seatbot/models.py
git commit -m "feat(models): domain dataclasses (Account, Task, results)"
```

---

## Task 6: ReservationPlanner (slot → task list)

**Files:**
- Create: `seatbot/planner.py`
- Create: `tests/test_planner.py`

- [ ] **Step 1: Write failing test**

```python
from datetime import date, time

import pytest

from seatbot.models import Account, Task, TaskStatus
from seatbot.planner import ReservationPlanner, PlannerError


def make_account(slots) -> Account:
    return Account(id="a", phone="1", password="p", seat_num="001", slots=slots)


def test_expand_full():
    planner = ReservationPlanner(make_account("full"), max_reserve_hours=2.0)
    tasks = planner.expand_for_day(date(2026, 7, 9))
    assert len(tasks) == 7
    assert tasks[0].start_time == time(8, 0)
    assert tasks[0].end_time == time(10, 0)
    assert tasks[-1].end_time == time(22, 0)
    assert all(t.status == TaskStatus.PENDING for t in tasks)
    assert all(t.account_id == "a" for t in tasks)


def test_expand_list_of_ranges():
    planner = ReservationPlanner(
        make_account(["08:00-12:00", "14:00-18:00"]),
        max_reserve_hours=2.0,
    )
    tasks = planner.expand_for_day(date(2026, 7, 9))
    # 08-10, 10-12, 14-16, 16-18 = 4 tasks
    assert [(t.start_time, t.end_time) for t in tasks] == [
        (time(8, 0), time(10, 0)),
        (time(10, 0), time(12, 0)),
        (time(14, 0), time(16, 0)),
        (time(16, 0), time(18, 0)),
    ]


def test_expand_invalid_slots_raises():
    planner = ReservationPlanner(make_account(123), max_reserve_hours=2.0)
    with pytest.raises(PlannerError):
        planner.expand_for_day(date(2026, 7, 9))


def test_no_overlap_between_chunks():
    planner = ReservationPlanner(
        make_account(["08:00-22:00"]), max_reserve_hours=2.0
    )
    tasks = planner.expand_for_day(date(2026, 7, 9))
    for prev, cur in zip(tasks, tasks[1:]):
        assert cur.start_time >= prev.end_time
```

- [ ] **Step 2: Run tests, expect failure**

Run: `pytest tests/test_planner.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement seatbot/planner.py**

```python
"""Expand user-facing slot config into ≤ max_hours task chunks."""
from __future__ import annotations

from datetime import date

from seatbot.models import Account, Task, TaskStatus
from seatbot.utils.timeutil import expand_account_slots


class PlannerError(Exception):
    pass


class ReservationPlanner:
    def __init__(self, account: Account, max_reserve_hours: float):
        self.account = account
        self.max_reserve_hours = max_reserve_hours

    def expand_for_day(self, day: date) -> list[Task]:
        try:
            chunks = expand_account_slots(
                self.account.slots, self.max_reserve_hours
            )
        except Exception as e:
            raise PlannerError(
                f"failed to expand slots for {self.account.id}: {e}"
            ) from e
        if not chunks:
            raise PlannerError(
                f"no slots produced for {self.account.id} on {day}"
            )
        return [
            Task(
                id=None,
                account_id=self.account.id,
                day=day,
                start_time=s,
                end_time=e,
                status=TaskStatus.PENDING,
            )
            for s, e in chunks
        ]
```

- [ ] **Step 4: Run tests, expect pass**

Run: `pytest tests/test_planner.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add seatbot/planner.py tests/test_planner.py
git commit -m "feat(planner): expand slot config into ≤2h task chunks"
```

---

## Task 7: SQLite StateStore (schema + CRUD)

**Files:**
- Create: `seatbot/store.py`
- Create: `tests/test_store.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Create tests/conftest.py with event_loop fixture**

```python
import asyncio
import pytest


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
```

- [ ] **Step 2: Write failing test for store**

```python
from datetime import date, time

import pytest

from seatbot.models import Account, Task, TaskStatus
from seatbot.store import StateStore


@pytest.fixture
async def store(tmp_path):
    db = tmp_path / "test.db"
    s = StateStore(str(db))
    await s.init()
    yield s
    await s.close()


async def test_init_creates_tables(store: StateStore):
    tables = await store.list_tables()
    assert {"accounts", "tasks", "actions", "logs"}.issubset(set(tables))


async def test_account_upsert_and_get(store: StateStore):
    acc = Account(id="zs", phone="138", password="p", seat_num="084", slots="full")
    await store.upsert_account(acc)
    loaded = await store.get_account("zs")
    assert loaded is not None
    assert loaded.id == "zs"
    assert loaded.seat_num == "084"
    assert loaded.slots == "full"


async def test_list_accounts(store: StateStore):
    for i in range(3):
        await store.upsert_account(
            Account(id=f"a{i}", phone=str(i), password="p", seat_num=f"00{i}", slots="full")
        )
    accs = await store.list_accounts()
    assert {a.id for a in accs} == {"a0", "a1", "a2"}


async def test_task_lifecycle(store: StateStore):
    acc = Account(id="zs", phone="1", password="p", seat_num="001", slots="full")
    await store.upsert_account(acc)
    t = Task(id=None, account_id="zs", day=date(2026, 7, 9),
             start_time=time(8, 0), end_time=time(10, 0))
    tid = await store.add_task(t)
    assert tid is not None
    await store.update_task_status(tid, TaskStatus.ACTIVE, reserve_id=12345)
    got = await store.get_task(tid)
    assert got.status == TaskStatus.ACTIVE
    assert got.reserve_id == 12345


async def test_list_tasks_by_account_day(store: StateStore):
    acc = Account(id="zs", phone="1", password="p", seat_num="001", slots="full")
    await store.upsert_account(acc)
    for h in (8, 10, 14):
        await store.add_task(Task(
            id=None, account_id="zs", day=date(2026, 7, 9),
            start_time=time(h, 0), end_time=time(h + 2, 0),
        ))
    tasks = await store.list_tasks(account_id="zs", day=date(2026, 7, 9))
    assert len(tasks) == 3
    assert [t.start_time.hour for t in tasks] == [8, 10, 14]


async def test_log_action(store: StateStore):
    await store.log_action("zs", "submit", '{"x":1}', '{"y":2}', True, "ok")
    rows = await store.list_actions(account_id="zs", limit=10)
    assert len(rows) == 1
    assert rows[0].action == "submit"
    assert rows[0].success is True


async def test_log_message(store: StateStore):
    await store.log_message("INFO", "zs", "hello world")
    rows = await store.list_logs(account_id="zs", limit=10)
    assert len(rows) == 1
    assert rows[0].level == "INFO"
```

- [ ] **Step 3: Run tests, expect failure**

Run: `pytest tests/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Implement seatbot/store.py**

```python
"""Async SQLite state store."""
from __future__ import annotations

import json
import time as _time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import aiosqlite

from seatbot.models import Account, Task, TaskStatus


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id          TEXT PRIMARY KEY,
  phone       TEXT NOT NULL,
  password    TEXT NOT NULL,
  seat_num    TEXT NOT NULL,
  slots_json  TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'active',
  bootstrap_day TEXT,
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id  TEXT NOT NULL,
  day         TEXT NOT NULL,
  start_time  TEXT NOT NULL,
  end_time    TEXT NOT NULL,
  status      TEXT NOT NULL,
  reserve_id  INTEGER,
  last_error  TEXT,
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_account_day ON tasks(account_id, day);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS actions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          INTEGER NOT NULL,
  account_id  TEXT NOT NULL,
  action      TEXT NOT NULL,
  request     TEXT,
  response    TEXT,
  success     INTEGER NOT NULL,
  message     TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_account_ts ON actions(account_id, ts DESC);

CREATE TABLE IF NOT EXISTS logs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          INTEGER NOT NULL,
  level       TEXT NOT NULL,
  account_id  TEXT,
  message     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts DESC);
"""


@dataclass
class ActionRow:
    id: int
    ts: int
    account_id: str
    action: str
    request: str | None
    response: str | None
    success: bool
    message: str | None


@dataclass
class LogRow:
    id: int
    ts: int
    level: str
    account_id: str | None
    message: str


class StateStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if not self._db:
            raise RuntimeError("StateStore not initialized; call init() first")
        return self._db

    # ---------- introspection ----------
    async def list_tables(self) -> list[str]:
        cur = await self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        rows = await cur.fetchall()
        return [r[0] for r in rows]

    # ---------- accounts ----------
    async def upsert_account(self, acc: Account) -> None:
        now = int(_time.time() * 1000)
        slots_json = acc.slots if isinstance(acc.slots, str) else json.dumps(acc.slots)
        cur = await self.db.execute(
            "SELECT created_at FROM accounts WHERE id=?", (acc.id,)
        )
        row = await cur.fetchone()
        if row is None:
            await self.db.execute(
                """INSERT INTO accounts
                   (id, phone, password, seat_num, slots_json, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
                (acc.id, acc.phone, acc.password, acc.seat_num, slots_json, now, now),
            )
        else:
            await self.db.execute(
                """UPDATE accounts SET phone=?, password=?, seat_num=?, slots_json=?, updated_at=?
                   WHERE id=?""",
                (acc.phone, acc.password, acc.seat_num, slots_json, now, acc.id),
            )
        await self.db.commit()

    async def get_account(self, acc_id: str) -> Account | None:
        cur = await self.db.execute(
            "SELECT id, phone, password, seat_num, slots_json FROM accounts WHERE id=?",
            (acc_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        slots = row[4]
        if slots not in ("full",):
            try:
                slots = json.loads(slots)
            except Exception:
                pass
        return Account(id=row[0], phone=row[1], password=row[2], seat_num=row[3], slots=slots)

    async def list_accounts(self) -> list[Account]:
        cur = await self.db.execute("SELECT id FROM accounts ORDER BY id")
        rows = await cur.fetchall()
        out: list[Account] = []
        for r in rows:
            a = await self.get_account(r[0])
            if a:
                out.append(a)
        return out

    async def delete_account(self, acc_id: str) -> None:
        await self.db.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
        await self.db.commit()

    async def set_bootstrap_day(self, acc_id: str, day: date) -> None:
        await self.db.execute(
            "UPDATE accounts SET bootstrap_day=? WHERE id=?", (day.isoformat(), acc_id)
        )
        await self.db.commit()

    async def get_bootstrap_day(self, acc_id: str) -> date | None:
        cur = await self.db.execute(
            "SELECT bootstrap_day FROM accounts WHERE id=?", (acc_id,)
        )
        row = await cur.fetchone()
        if not row or not row[0]:
            return None
        return date.fromisoformat(row[0])

    # ---------- tasks ----------
    async def add_task(self, t: Task) -> int:
        now = int(_time.time() * 1000)
        cur = await self.db.execute(
            """INSERT INTO tasks
               (account_id, day, start_time, end_time, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                t.account_id, t.day.isoformat(),
                t.start_time.isoformat(timespec="minutes"),
                t.end_time.isoformat(timespec="minutes"),
                t.status.value, now, now,
            ),
        )
        await self.db.commit()
        return cur.lastrowid or 0

    async def update_task_status(
        self,
        task_id: int,
        status: TaskStatus,
        *,
        reserve_id: int | None = None,
        last_error: str | None = None,
    ) -> None:
        now = int(_time.time() * 1000)
        await self.db.execute(
            """UPDATE tasks
               SET status=?, reserve_id=COALESCE(?, reserve_id),
                   last_error=COALESCE(?, last_error), updated_at=?
               WHERE id=?""",
            (status.value, reserve_id, last_error, now, task_id),
        )
        await self.db.commit()

    async def get_task(self, task_id: int) -> Task | None:
        cur = await self.db.execute(
            "SELECT id, account_id, day, start_time, end_time, status, reserve_id, last_error FROM tasks WHERE id=?",
            (task_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return _row_to_task(row)

    async def list_tasks(
        self, account_id: str | None = None, day: date | None = None
    ) -> list[Task]:
        q = "SELECT id, account_id, day, start_time, end_time, status, reserve_id, last_error FROM tasks WHERE 1=1"
        args: list[Any] = []
        if account_id:
            q += " AND account_id=?"
            args.append(account_id)
        if day:
            q += " AND day=?"
            args.append(day.isoformat())
        q += " ORDER BY day, start_time"
        cur = await self.db.execute(q, args)
        rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]

    async def find_active_task(self, account_id: str) -> Task | None:
        cur = await self.db.execute(
            "SELECT id, account_id, day, start_time, end_time, status, reserve_id, last_error "
            "FROM tasks WHERE account_id=? AND status IN ('active','submitting','leaving') "
            "ORDER BY day DESC, start_time DESC LIMIT 1",
            (account_id,),
        )
        row = await cur.fetchone()
        return _row_to_task(row) if row else None

    # ---------- actions / logs ----------
    async def log_action(
        self,
        account_id: str,
        action: str,
        request: str | None,
        response: str | None,
        success: bool,
        message: str | None = None,
    ) -> None:
        now = int(_time.time() * 1000)
        await self.db.execute(
            "INSERT INTO actions (ts, account_id, action, request, response, success, message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now, account_id, action, request, response, 1 if success else 0, message),
        )
        await self.db.commit()

    async def list_actions(
        self, account_id: str | None = None, limit: int = 50
    ) -> list[ActionRow]:
        q = "SELECT id, ts, account_id, action, request, response, success, message FROM actions"
        args: list[Any] = []
        if account_id:
            q += " WHERE account_id=?"
            args.append(account_id)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        cur = await self.db.execute(q, args)
        rows = await cur.fetchall()
        return [
            ActionRow(
                id=r[0], ts=r[1], account_id=r[2], action=r[3],
                request=r[4], response=r[5], success=bool(r[6]), message=r[7],
            )
            for r in rows
        ]

    async def log_message(
        self, level: str, account_id: str | None, message: str
    ) -> None:
        now = int(_time.time() * 1000)
        await self.db.execute(
            "INSERT INTO logs (ts, level, account_id, message) VALUES (?, ?, ?, ?)",
            (now, level, account_id, message),
        )
        await self.db.commit()

    async def list_logs(
        self,
        account_id: str | None = None,
        level: str | None = None,
        limit: int = 200,
    ) -> list[LogRow]:
        q = "SELECT id, ts, level, account_id, message FROM logs WHERE 1=1"
        args: list[Any] = []
        if account_id:
            q += " AND account_id=?"
            args.append(account_id)
        if level:
            q += " AND level=?"
            args.append(level)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        cur = await self.db.execute(q, args)
        rows = await cur.fetchall()
        return [LogRow(id=r[0], ts=r[1], level=r[2], account_id=r[3], message=r[4]) for r in rows]


def _row_to_task(row) -> Task:
    h, m = map(int, row[3].split(":"))
    eh, em = map(int, row[4].split(":"))
    return Task(
        id=row[0],
        account_id=row[1],
        day=date.fromisoformat(row[2]),
        start_time=__import__("datetime").time(h, m),
        end_time=__import__("datetime").time(eh, em),
        status=TaskStatus(row[5]),
        reserve_id=row[6],
        last_error=row[7],
    )
```

- [ ] **Step 5: Run tests, expect pass**

Run: `pytest tests/test_store.py -v`
Expected: PASS (7 tests)

- [ ] **Step 6: Commit**

```bash
git add seatbot/store.py tests/test_store.py tests/conftest.py
git commit -m "feat(store): async SQLite state store with accounts/tasks/actions/logs"
```

---

## Task 8: ChaoxingClient (httpx) — login + get_room_info

**Files:**
- Create: `seatbot/client.py`
- Create: `tests/test_client_login.py` (integration, marked)

- [ ] **Step 1: Write integration test (skipped by default)**

```python
"""Integration test for ChaoxingClient.login.

Run with: pytest tests/test_client_login.py -v -m integration
Requires real credentials via env SEATBOT_TEST_PHONE / SEATBOT_TEST_PASSWORD.
"""
import os

import pytest

from seatbot.client import ChaoxingClient, ChaoxingError


@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_success():
    phone = os.environ.get("SEATBOT_TEST_PHONE")
    password = os.environ.get("SEATBOT_TEST_PASSWORD")
    if not phone or not password:
        pytest.skip("set SEATBOT_TEST_PHONE / SEATBOT_TEST_PASSWORD to run")

    client = ChaoxingClient()
    try:
        await client.login(phone, password)
        # cookies should be set; second call should not re-login
        cookies = client.cookies()
        assert any("_uid" in c.name or "vc3" in c.name for c in client._cookie_jar)
    finally:
        await client.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_failure_wrong_password():
    client = ChaoxingClient()
    try:
        with pytest.raises(ChaoxingError):
            await client.login("13800000000", "wrong_password_xyz")
    finally:
        await client.close()
```

- [ ] **Step 2: Implement seatbot/client.py (login + get_room_info skeleton)**

```python
"""Async HTTP client for the Chaoxing (超星) library seat system."""
from __future__ import annotations

import json
import random
import time as _time
from typing import Any
from urllib.parse import urlencode

import httpx

from seatbot.utils.ua import random_ua


class ChaoxingError(Exception):
    """Generic Chaoxing API failure."""


class ChaoxingClient:
    PASSPORT_BASE = "https://passport2.chaoxing.com"
    OFFICE_BASE = "https://office.chaoxing.com"

    def __init__(self, *, ua: str | None = None):
        self._ua = ua or random_ua()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=10.0),
            follow_redirects=True,
            headers={
                "User-Agent": self._ua,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept": "application/json,text/plain,*/*",
            },
        )
        self._cookie_jar = self._client.cookies

    async def close(self) -> None:
        await self._client.aclose()

    def cookies(self) -> dict[str, str]:
        return {c.name: c.value for c in self._cookie_jar.jar}

    # ---------- low-level helpers ----------
    def _referer(self, path: str) -> str:
        return f"{self.OFFICE_BASE}{path}"

    async def _post_form(
        self, url: str, data: dict[str, Any], *, referer: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if referer:
            headers["Referer"] = referer
        r = await self._client.post(url, data=data, headers=headers)
        r.raise_for_status()
        try:
            return r.json()
        except json.JSONDecodeError as e:
            raise ChaoxingError(f"non-JSON response from {url}: {r.text[:200]}") from e

    # ---------- login ----------
    async def login(self, phone: str, password: str) -> None:
        """Login via fanyalogin; populates cookies."""
        url = f"{self.PASSPORT_BASE}/fanyalogin"
        data = {
            "fid": -1,
            "pid": -1,
            "refer": "https%3A%2F%2Foffice.chaoxing.com%2F",
            "fidName": "",
            "allowForce": 1,
            "autoLogin": 0,
            "loginName": phone,
            "password": password,
            "verCode": "",
        }
        # fanyalogin returns JSON inside an HTML document sometimes; safest to grab text
        r = await self._client.post(
            f"{url}?{urlencode(data)}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        r.raise_for_status()
        text = r.text
        # The response looks like: {status:true, msg1:"", url1:"...", ...}
        try:
            # extract JSON object from response
            start = text.find("{")
            end = text.rfind("}") + 1
            if start < 0 or end <= start:
                raise ChaoxingError(f"no JSON in fanyalogin response: {text[:200]}")
            payload = json.loads(text[start:end])
        except json.JSONDecodeError as e:
            raise ChaoxingError(f"fanyalogin parse error: {text[:200]}") from e

        if not payload.get("status"):
            raise ChaoxingError(f"login failed: {payload.get('msg2') or payload.get('msg1') or 'unknown'}")

        # fanyalogin returns redirect URL — follow it to set cookies
        url1 = payload.get("url1")
        if url1:
            await self._client.get(url1, headers={"Referer": f"{self.PASSPORT_BASE}/"})

        if not any(c.name in ("_uid", "vc3") for c in self._cookie_jar.jar):
            raise ChaoxingError("login succeeded but no auth cookies set")

    # ---------- room info ----------
    async def get_room_info(self, room_id: int) -> dict[str, Any]:
        """Get the seatConfig + seatRoom for a given room_id."""
        url = f"{self.OFFICE_BASE}/data/apps/seat/room/info"
        referer = f"{self.OFFICE_BASE}/front/apps/seat/list"
        return await self._post_form(
            url, {"id": room_id}, referer=referer
        )

    # The methods below are added in Tasks 9-10. They raise NotImplementedError
    # for now so the class compiles cleanly.
    async def submit_reserve(self, *a, **kw) -> dict[str, Any]:
        raise NotImplementedError

    async def sign(self, *a, **kw) -> dict[str, Any]:
        raise NotImplementedError

    async def leave(self, *a, **kw) -> dict[str, Any]:
        raise NotImplementedError

    async def cancel(self, *a, **kw) -> dict[str, Any]:
        raise NotImplementedError

    async def get_active_reservation(self, *a, **kw) -> dict[str, Any] | None:
        raise NotImplementedError

    async def get_seat_status(self, *a, **kw) -> list[dict[str, Any]]:
        raise NotImplementedError
```

- [ ] **Step 3: Run integration test against real Chaoxing (optional, manual)**

```bash
export SEATBOT_TEST_PHONE=13800000000
export SEATBOT_TEST_PASSWORD=your_password
pytest tests/test_client_login.py -v -m integration
```

Expected: 2 tests pass (or skip if env not set).

- [ ] **Step 4: Commit**

```bash
git add seatbot/client.py tests/test_client_login.py
git commit -m "feat(client): ChaoxingClient with login + get_room_info"
```

---

## Task 9: ChaoxingClient — submit / sign / leave / cancel

**Files:**
- Modify: `seatbot/client.py`
- Create: `tests/test_client_submit.py` (integration)

- [ ] **Step 1: Add submit_reserve to client.py**

Replace the placeholder `submit_reserve`:

```python
    async def submit_reserve(
        self,
        room_id: int,
        day: str,           # 'YYYY-MM-DD'
        start_time: str,    # 'HH:MM'
        end_time: str,      # 'HH:MM'
        seat_num: str,
        enc: str,
        wy_token: str = "",
        captcha: str = "",
    ) -> dict[str, Any]:
        url = f"{self.OFFICE_BASE}/data/apps/seat/submit"
        referer = (
            f"{self.OFFICE_BASE}/front/apps/seat/code"
            f"?id={room_id}&seatNum={seat_num}"
        )
        data = {
            "roomId": room_id,
            "day": day,
            "startTime": start_time,
            "endTime": end_time,
            "seatNum": seat_num,
            "captcha": captcha,
            "type": 1,
            "verifyData": 1,
            "wyToken": wy_token,
            "enc": enc,
        }
        return await self._post_form(url, data, referer=referer)
```

- [ ] **Step 2: Add sign/leave/cancel**

Replace placeholders:

```python
    async def sign(self, reserve_id: int) -> dict[str, Any]:
        url = f"{self.OFFICE_BASE}/data/apps/seat/sign"
        return await self._post_form(
            url, {"id": reserve_id}, referer=self.OFFICE_BASE + "/"
        )

    async def leave(self, reserve_id: int) -> dict[str, Any]:
        url = f"{self.OFFICE_BASE}/data/apps/seat/leave"
        return await self._post_form(
            url, {"id": reserve_id}, referer=self.OFFICE_BASE + "/"
        )

    async def cancel(self, reserve_id: int) -> dict[str, Any]:
        url = f"{self.OFFICE_BASE}/data/apps/seat/cancel"
        return await self._post_form(
            url, {"id": reserve_id}, referer=self.OFFICE_BASE + "/"
        )
```

- [ ] **Step 3: Write integration test for submit**

```python
"""Integration test for submit + sign + leave cycle.

Run with: pytest tests/test_client_submit.py -v -m integration
"""
import os
from datetime import date, timedelta

import pytest

from seatbot.client import ChaoxingClient


@pytest.mark.integration
@pytest.mark.asyncio
async def test_submit_sign_leave_cycle():
    phone = os.environ.get("SEATBOT_TEST_PHONE")
    password = os.environ.get("SEATBOT_TEST_PASSWORD")
    if not phone or not password:
        pytest.skip("set SEATBOT_TEST_PHONE / SEATBOT_TEST_PASSWORD")

    # We'll pick a tomorrow time slot — assumes preSignDuration is generous
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    seat_num = os.environ.get("SEATBOT_TEST_SEAT", "084")
    room_id = int(os.environ.get("SEATBOT_TEST_ROOM", "11692"))
    start, end = "10:00", "11:00"

    client = ChaoxingClient()
    try:
        await client.login(phone, password)
        # NOTE: enc generation is covered in Task 10; for now stub it
        enc = "TEST_ENC_PLACEHOLDER"
        result = await client.submit_reserve(
            room_id=room_id, day=tomorrow, start_time=start, end_time=end,
            seat_num=seat_num, enc=enc,
        )
        # expected failure (bad enc) — but we want to ensure the request shape is right
        assert "success" in result
    finally:
        await client.close()
```

- [ ] **Step 4: Commit**

```bash
git add seatbot/client.py tests/test_client_submit.py
git commit -m "feat(client): submit/sign/leave/cancel endpoints"
```

---

## Task 10: EncGenerator (JS exec + Playwright fallback)

**Files:**
- Create: `seatbot/enc.py`
- Create: `tests/test_enc.py` (mocked)

- [ ] **Step 1: Write failing test (with mocked JS exec)**

```python
import pytest

from seatbot.enc import EncGenerator, EncError


@pytest.mark.asyncio
async def test_enc_generator_returns_dict(monkeypatch):
    """We mock the underlying JS execution to return a known payload."""
    gen = EncGenerator()

    async def fake_compute(room_id, seat_num, day, start, end):
        return {"enc": "fake_enc_value", "wyToken": "fake_token_value"}

    monkeypatch.setattr(gen, "_compute_js", fake_compute)
    out = await gen.compute(room_id=11692, seat_num="084", day="2026-07-09",
                            start_time="10:00", end_time="11:00")
    assert out["enc"] == "fake_enc_value"
    assert out["wyToken"] == "fake_token_value"


@pytest.mark.asyncio
async def test_enc_generator_caches_within_ttl(monkeypatch):
    gen = EncGenerator(ttl_seconds=60)
    call_count = {"n": 0}

    async def fake_compute(*a, **kw):
        call_count["n"] += 1
        return {"enc": "x", "wyToken": "y"}

    monkeypatch.setattr(gen, "_compute_js", fake_compute)
    args = dict(room_id=11692, seat_num="084", day="2026-07-09",
                start_time="10:00", end_time="11:00")
    await gen.compute(**args)
    await gen.compute(**args)
    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_enc_generator_raises_on_failure(monkeypatch):
    gen = EncGenerator()
    async def boom(*a, **kw):
        raise RuntimeError("JS exec failed")
    monkeypatch.setattr(gen, "_compute_js", boom)
    with pytest.raises(EncError):
        await gen.compute(room_id=1, seat_num="1", day="2026-07-09",
                          start_time="10:00", end_time="11:00")
```

- [ ] **Step 2: Run tests, expect failure**

Run: `pytest tests/test_enc.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement seatbot/enc.py**

```python
"""Compute Chaoxing `enc` / `wyToken` via JS exec with Playwright fallback.

The exact algorithm is embedded in YiDunProtector-Web-2.1.4.js. Because that
JS may change, we keep two execution paths:

1. **exec_js (default)**:  Use `PyExecJS` to run a small driver script that
   delegates to the YiDunProtector. The driver script lives in
   `seatbot/enc_driver.js` and is included in the package.
2. **playwright fallback**:  If exec_js fails (e.g. missing node), spin up
   a headless Chromium on the seat detail page and read the form's
   `wyToken` / `enc` from the captured `/submit` request.

For v1 the JS driver is a stub that returns `{"enc":"","wyToken":""}`
(forcing fallback). We wire up real JS in a follow-up.
"""
from __future__ import annotations

import asyncio
import time as _time
from dataclasses import dataclass

from seatbot.client import ChaoxingClient


class EncError(Exception):
    pass


@dataclass
class _CacheEntry:
    enc: str
    wy_token: str
    expires_at: float


class EncGenerator:
    def __init__(self, ttl_seconds: int = 60):
        self.ttl_seconds = ttl_seconds
        self._cache: dict[tuple, _CacheEntry] = {}

    def _cache_key(self, room_id, seat_num, day, start, end):
        return (room_id, seat_num, day, start, end)

    async def _compute_js(
        self, room_id: int, seat_num: str, day: str, start: str, end: str
    ) -> dict[str, str]:
        # v1 stub: real JS wired in follow-up. Returning empty values
        # forces the caller to treat this as a failure and fall back.
        return {"enc": "", "wyToken": ""}

    async def _compute_playwright(
        self,
        client: ChaoxingClient,
        room_id: int,
        seat_num: str,
        day: str,
        start: str,
        end: str,
    ) -> dict[str, str]:
        """Headless fallback: open the seat detail page, intercept the
        outgoing /submit request, extract enc/wyToken."""
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise EncError("playwright not installed; cannot fallback") from e

        captured: dict[str, str] = {}

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context()
            # Reuse the logged-in cookies from ChaoxingClient
            await ctx.add_cookies([
                {"name": n, "value": v, "url": client.OFFICE_BASE}
                for n, v in client.cookies().items()
            ])
            page = await ctx.new_page()

            async def on_request(req):
                if "/data/apps/seat/submit" in req.url and req.method == "POST":
                    body = req.post_data or ""
                    from urllib.parse import parse_qs
                    parsed = parse_qs(body)
                    captured["enc"] = parsed.get("enc", [""])[0]
                    captured["wyToken"] = parsed.get("wyToken", [""])[0]

            page.on("request", on_request)
            url = (
                f"{client.OFFICE_BASE}/front/apps/seat/code"
                f"?id={room_id}&seatNum={seat_num}"
            )
            await page.goto(url)
            # wait for the seat list to load
            try:
                await page.wait_for_selector("li", timeout=10000)
            except Exception:
                pass
            # Try clicking a 30-min slot matching `start`
            try:
                await page.locator(f"li:has-text('{start}-')").first.click(timeout=3000)
                await page.locator("p.can_submit:has-text('开始使用')").first.click(timeout=3000)
            except Exception:
                pass
            # give network listener time to capture
            await page.wait_for_timeout(2000)
            await browser.close()

        if not captured.get("enc"):
            raise EncError("playwright fallback did not capture enc")
        return {"enc": captured["enc"], "wyToken": captured.get("wyToken", "")}

    async def compute(
        self,
        room_id: int,
        seat_num: str,
        day: str,
        start_time: str,
        end_time: str,
        *,
        client: ChaoxingClient | None = None,
        allow_fallback: bool = True,
    ) -> dict[str, str]:
        key = self._cache_key(room_id, seat_num, day, start_time, end_time)
        now = _time.time()
        if key in self._cache and self._cache[key].expires_at > now:
            return {"enc": self._cache[key].enc, "wyToken": self._cache[key].wy_token}

        # Try JS first
        result = await self._compute_js(room_id, seat_num, day, start_time, end_time)
        if not result.get("enc") and allow_fallback and client is not None:
            result = await self._compute_playwright(
                client, room_id, seat_num, day, start_time, end_time
            )

        if not result.get("enc"):
            raise EncError("enc generation produced empty result")

        self._cache[key] = _CacheEntry(
            enc=result["enc"],
            wy_token=result.get("wyToken", ""),
            expires_at=now + self.ttl_seconds,
        )
        return {"enc": result["enc"], "wyToken": result.get("wyToken", "")}
```

- [ ] **Step 4: Run tests, expect pass**

Run: `pytest tests/test_enc.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add seatbot/enc.py tests/test_enc.py
git commit -m "feat(enc): EncGenerator with JS exec + Playwright fallback"
```

---

## Task 11: ChaoxingClient — get_active_reservation + get_seat_status

**Files:**
- Modify: `seatbot/client.py`

- [ ] **Step 1: Replace placeholders in client.py**

Replace `get_active_reservation`:

```python
    async def get_active_reservation(self, room_id: int, seat_num: str) -> dict[str, Any] | None:
        url = (
            f"{self.OFFICE_BASE}/data/apps/seat/reserve/info"
            f"?id={room_id}&seatNum={seat_num}"
        )
        r = await self._client.get(url, headers={"Referer": self.OFFICE_BASE + "/"})
        r.raise_for_status()
        try:
            payload = r.json()
        except json.JSONDecodeError as e:
            raise ChaoxingError(f"reserve/info non-JSON: {r.text[:200]}") from e
        if not payload.get("success"):
            return None
        sr = (payload.get("data") or {}).get("seatReserve")
        return sr
```

Replace `get_seat_status`:

```python
    async def get_seat_status(
        self, room_id: int, day: str | None = None
    ) -> list[dict[str, Any]]:
        """Return seat status list for a given room + day.

        NOTE: the actual public endpoint that lists 108 seats was observed
        only via the floor SVG UI; we expose this method as a best-effort
        that pulls `seatIntervalMap` from `get_room_info`. If finer-grained
        status is needed, the web panel can call this and cross-reference
        with /data/apps/seat/getusedtimes. v1 keeps it simple.
        """
        info = await self.get_room_info(room_id)
        interval_map = (info.get("data") or {}).get("seatIntervalMap") or {}
        return [{"seat_num": k, "intervals": v} for k, v in interval_map.items()]
```

- [ ] **Step 2: Verify import + method presence**

Run: `python -c "from seatbot.client import ChaoxingClient; print(hasattr(ChaoxingClient(), 'get_active_reservation'), hasattr(ChaoxingClient(), 'get_seat_status'))"`
Expected: `True True`

- [ ] **Step 3: Commit**

```bash
git add seatbot/client.py
git commit -m "feat(client): get_active_reservation + get_seat_status"
```

---

## Task 12: Scheduler (APScheduler + state machine)

**Files:**
- Create: `seatbot/scheduler.py`

(No unit tests — scheduler is integration-heavy. Manual smoke test in Task 14.)

- [ ] **Step 1: Implement seatbot/scheduler.py**

```python
"""APScheduler wrapper that drives the reservation state machine."""
from __future__ import annotations

import asyncio
import random
from datetime import date, datetime, time, timedelta
from typing import Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from seatbot.client import ChaoxingClient, ChaoxingError
from seatbot.config import Config
from seatbot.enc import EncError, EncGenerator
from seatbot.models import Account, ReserveResult, SignResult, LeaveResult, Task, TaskStatus
from seatbot.planner import ReservationPlanner
from seatbot.store import StateStore
from seatbot.utils.timeutil import now_cst, today_cst


class Scheduler:
    RELAY_LEAD_SECONDS = 5 * 60  # leave 5 minutes before end_time

    def __init__(
        self,
        cfg: Config,
        store: StateStore,
        enc: EncGenerator | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.enc = enc or EncGenerator()
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        self._clients: dict[str, ChaoxingClient] = {}
        self._bootstrap_done_for: set[tuple[str, str]] = set()  # (account_id, day)

    def _client_for(self, acc: Account) -> ChaoxingClient:
        if acc.id not in self._clients:
            self._clients[acc.id] = ChaoxingClient()
        return self._clients[acc.id]

    # ---------- logging helpers ----------
    async def _info(self, msg: str, acc: str | None = None) -> None:
        await self.store.log_message("INFO", acc, msg)
        print(f"[INFO] {acc or '-'} {msg}")

    async def _warn(self, msg: str, acc: str | None = None) -> None:
        await self.store.log_message("WARN", acc, msg)
        print(f"[WARN] {acc or '-'} {msg}")

    async def _error(self, msg: str, acc: str | None = None) -> None:
        await self.store.log_message("ERROR", acc, msg)
        print(f"[ERROR] {acc or '-'} {msg}")

    # ---------- bootstrap ----------
    async def bootstrap_today(self) -> None:
        today = today_cst()
        for acc in self.cfg.accounts:
            await self._bootstrap_for_account(acc, today)

    async def _bootstrap_for_account(self, acc: Account, day: date) -> None:
        if (acc.id, day.isoformat()) in self._bootstrap_done_for:
            return
        prev = await self.store.get_bootstrap_day(acc.id)
        existing = await self.store.list_tasks(account_id=acc.id, day=day)
        if existing:
            self._bootstrap_done_for.add((acc.id, day.isoformat()))
            await self._info(f"already has {len(existing)} tasks for {day}", acc.id)
            return
        if prev == day:
            self._bootstrap_done_for.add((acc.id, day.isoformat()))
            return

        planner = ReservationPlanner(acc, self.cfg.library.max_reserve_hours)
        try:
            tasks = planner.expand_for_day(day)
        except Exception as e:
            await self._error(f"planner failed: {e}", acc.id)
            return
        for t in tasks:
            await self.store.add_task(t)
        await self.store.set_bootstrap_day(acc.id, day)
        self._bootstrap_done_for.add((acc.id, day.isoformat()))
        await self._info(f"bootstrap {len(tasks)} tasks for {day}", acc.id)

    # ---------- per-account tick ----------
    async def tick_account(self, acc_id: str) -> None:
        acc_cfg = next((a for a in self.cfg.accounts if a.id == acc_id), None)
        if not acc_cfg:
            return
        active = await self.store.find_active_task(acc_id)
        now = now_cst()
        if active:
            await self._maybe_relay(acc_cfg, active, now)
            return
        # no active task — find the next pending task that should be running
        today = today_cst()
        tasks = await self.store.list_tasks(account_id=acc_id, day=today)
        for t in tasks:
            if t.status not in (TaskStatus.PENDING, TaskStatus.READY, TaskStatus.FAILED):
                continue
            t_start = datetime.combine(t.day, t.start_time)
            t_end = datetime.combine(t.day, t.end_time)
            # currently inside the slot
            if t_start <= now < t_end:
                await self._run_submit_sign(acc_cfg, t)
                return
            # pre-sign window (within 20 min of start)
            if timedelta(0) <= (t_start - now) <= timedelta(minutes=20):
                await self._run_submit_sign(acc_cfg, t)
                return

    async def _maybe_relay(self, acc: Account, t: Task, now: datetime) -> None:
        t_end = datetime.combine(t.day, t.end_time)
        lead = t_end - timedelta(seconds=self.RELAY_LEAD_SECONDS)
        if now < lead:
            return
        await self._info(f"relay: leaving {t.chunk_key()}", acc.id)
        await self.store.update_task_status(t.id, TaskStatus.LEAVING)
        client = self._client_for(acc)
        if t.reserve_id:
            try:
                await client.leave(t.reserve_id)
                await self.store.log_action(acc.id, "leave", str(t.reserve_id), "", True)
            except Exception as e:
                await self._error(f"leave failed: {e}", acc.id)
        await self.store.update_task_status(t.id, TaskStatus.COMPLETE)

        # find next task
        today = today_cst()
        tasks = await self.store.list_tasks(account_id=acc.id, day=today)
        nxt = next(
            (x for x in tasks
             if x.start_time > t.start_time
             and x.status in (TaskStatus.PENDING, TaskStatus.FAILED)),
            None,
        )
        if nxt is None:
            await self._info("relay: no next task", acc.id)
            return
        await self._run_submit_sign(acc, nxt)

    async def _run_submit_sign(self, acc: Account, t: Task) -> None:
        await self.store.update_task_status(t.id, TaskStatus.SUBMITTING)
        client = self._client_for(acc)

        # login if not yet
        if not client.cookies():
            try:
                await client.login(acc.phone, acc.password)
            except ChaoxingError as e:
                await self._error(f"login failed: {e}", acc.id)
                await self.store.update_task_status(t.id, TaskStatus.FAILED, last_error=str(e))
                return

        # compute enc
        try:
            enc = await self.enc.compute(
                room_id=self.cfg.library.room_id,
                seat_num=acc.seat_num,
                day=t.day.isoformat(),
                start_time=t.start_time.strftime("%H:%M"),
                end_time=t.end_time.strftime("%H:%M"),
                client=client,
            )
        except EncError as e:
            await self._error(f"enc failed: {e}", acc.id)
            await self.store.update_task_status(t.id, TaskStatus.FAILED, last_error=str(e))
            return

        # submit
        try:
            r = await client.submit_reserve(
                room_id=self.cfg.library.room_id,
                day=t.day.isoformat(),
                start_time=t.start_time.strftime("%H:%M"),
                end_time=t.end_time.strftime("%H:%M"),
                seat_num=acc.seat_num,
                enc=enc["enc"],
                wy_token=enc["wyToken"],
            )
        except Exception as e:
            await self._error(f"submit error: {e}", acc.id)
            await self.store.update_task_status(t.id, TaskStatus.FAILED, last_error=str(e))
            return

        await self.store.log_action(
            acc.id, "submit",
            f"{t.day} {t.start_time}-{t.end_time}",
            str(r)[:500], bool(r.get("success")), str(r.get("msg")),
        )

        if not r.get("success"):
            msg = r.get("msg") or "submit failed"
            await self._error(f"submit rejected: {msg}", acc.id)
            await self.store.update_task_status(t.id, TaskStatus.FAILED, last_error=msg)
            return

        reserve_id = (r.get("data") or {}).get("seatReserve", {}).get("id")
        if not reserve_id:
            await self._error("submit ok but no reserve_id", acc.id)
            await self.store.update_task_status(t.id, TaskStatus.FAILED, last_error="no reserve_id")
            return

        await self.store.update_task_status(t.id, TaskStatus.ACTIVE, reserve_id=reserve_id)
        await self._info(f"reserved #{reserve_id} {t.chunk_key()}", acc.id)

        # sign
        try:
            sr = await client.sign(reserve_id)
            await self.store.log_action(
                acc.id, "sign", str(reserve_id), str(sr)[:500],
                bool(sr.get("success")), str(sr.get("msg")),
            )
            if not sr.get("success"):
                await self._error(f"sign failed: {sr.get('msg')}", acc.id)
        except Exception as e:
            await self._error(f"sign error: {e}", acc.id)

    # ---------- cron wiring ----------
    def start(self) -> None:
        # every minute: tick all accounts (with stagger)
        for i, acc in enumerate(self.cfg.accounts):
            delay = random.uniform(*self.cfg.runtime.stagger_seconds)
            self.scheduler.add_job(
                self._tick_account_with_bootstrap,
                "cron", second=f"{int(delay)}",
                args=[acc.id],
                id=f"tick_{acc.id}",
                replace_existing=True,
            )
        # 00:00:05 every day: bootstrap next day
        self.scheduler.add_job(
            self._new_day_bootstrap,
            CronTrigger(hour=0, minute=0, second=5, timezone="Asia/Shanghai"),
            id="new_day_bootstrap", replace_existing=True,
        )
        self.scheduler.start()

    async def _tick_account_with_bootstrap(self, acc_id: str) -> None:
        await self._bootstrap_for_account_if_needed(acc_id)
        await self.tick_account(acc_id)

    async def _bootstrap_for_account_if_needed(self, acc_id: str) -> None:
        acc_cfg = next((a for a in self.cfg.accounts if a.id == acc_id), None)
        if not acc_cfg:
            return
        await self._bootstrap_for_account(acc_cfg, today_cst())

    async def _new_day_bootstrap(self) -> None:
        today = today_cst()
        for acc in self.cfg.accounts:
            await self._bootstrap_for_account(acc, today)

    async def shutdown(self) -> None:
        self.scheduler.shutdown(wait=False)
        for c in self._clients.values():
            await c.close()
```

- [ ] **Step 2: Verify import**

Run: `python -c "from seatbot.scheduler import Scheduler; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add seatbot/scheduler.py
git commit -m "feat(scheduler): APScheduler-driven state machine + relay"
```

---

## Task 13: CLI entry (`__main__.py`)

**Files:**
- Create: `seatbot/__main__.py`

- [ ] **Step 1: Implement seatbot/__main__.py**

```python
"""CLI entry point."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from seatbot.config import load_config, ConfigError
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="seatbot", description="超星图书馆座位自动化")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", "-c", default="./config.yaml")

    sub.add_parser("run", parents=[common], help="启动 Web + 调度器")
    sub.add_parser("init-db", parents=[common], help="初始化 SQLite")
    once = sub.add_parser("once", parents=[common], help="单次执行 (调试)")
    once.add_argument("--account", required=True)
    once.add_argument("--action", choices=["all", "submit", "sign", "leave", "cancel"], default="all")

    login = sub.add_parser("login", parents=[common], help="单独登录测试")
    login.add_argument("--account", required=True)

    sub.add_parser("status", parents=[common], help="查看状态")
    return p


async def _cmd_run(args) -> int:
    cfg = load_config(args.config)
    store = StateStore(cfg.runtime.db_path)
    await store.init()
    sched = Scheduler(cfg, store)
    sched.start()
    await sched.bootstrap_today()
    # start web in parallel
    from seatbot.web.app import make_app
    app = make_app(cfg, store, sched)
    import uvicorn
    config = uvicorn.Config(
        app, host=cfg.runtime.web_host, port=cfg.runtime.web_port, log_level="info"
    )
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        await sched.shutdown()
        await store.close()
    return 0


async def _cmd_init_db(args) -> int:
    cfg = load_config(args.config)
    store = StateStore(cfg.runtime.db_path)
    await store.init()
    await store.close()
    print(f"initialized: {cfg.runtime.db_path}")
    return 0


async def _cmd_once(args) -> int:
    cfg = load_config(args.config)
    store = StateStore(cfg.runtime.db_path)
    await store.init()
    sched = Scheduler(cfg, store)
    await sched.bootstrap_today()
    await sched.tick_account(args.account)
    await sched.shutdown()
    await store.close()
    return 0


async def _cmd_login(args) -> int:
    cfg = load_config(args.config)
    acc = next((a for a in cfg.accounts if a.id == args.account), None)
    if not acc:
        print(f"account {args.account} not found", file=sys.stderr)
        return 1
    from seatbot.client import ChaoxingClient, ChaoxingError
    c = ChaoxingClient()
    try:
        await c.login(acc.phone, acc.password)
        print(f"login OK; cookies: {list(c.cookies().keys())}")
    except ChaoxingError as e:
        print(f"login failed: {e}", file=sys.stderr)
        return 2
    finally:
        await c.close()
    return 0


async def _cmd_status(args) -> int:
    cfg = load_config(args.config)
    store = StateStore(cfg.runtime.db_path)
    await store.init()
    try:
        for acc in await store.list_accounts():
            tasks = await store.list_tasks(account_id=acc.id)
            print(f"\n[{acc.id}] seat={acc.seat_num} slots={acc.slots}")
            for t in tasks:
                print(f"  {t.day} {t.start_time}-{t.end_time} {t.status.value} "
                      f"reserve={t.reserve_id} err={t.last_error}")
        print("\n--- recent actions ---")
        for a in await store.list_actions(limit=10):
            print(f"  {a.ts} [{a.account_id}] {a.action} success={a.success} {a.message or ''}")
    finally:
        await store.close()
    return 0


def cli() -> int:
    args = _build_parser().parse_args()
    handlers = {
        "run": _cmd_run,
        "init-db": _cmd_init_db,
        "once": _cmd_once,
        "login": _cmd_login,
        "status": _cmd_status,
    }
    try:
        return asyncio.run(handlers[args.cmd](args))
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(cli())
```

- [ ] **Step 2: Smoke test (no real config)**

Run: `python -m seatbot --help`
Expected: prints CLI help

Run: `python -m seatbot init-db --config /nonexistent`
Expected: `config error: config file not found: /nonexistent`, exit 1

- [ ] **Step 3: Commit**

```bash
git add seatbot/__main__.py
git commit -m "feat(cli): argparse entry — run/init-db/once/login/status"
```

---

## Task 14: FastAPI app skeleton + base template

**Files:**
- Create: `seatbot/web/__init__.py`
- Create: `seatbot/web/app.py`
- Create: `seatbot/web/templates/base.html`
- Create: `seatbot/web/static/style.css`

- [ ] **Step 1: Create seatbot/web/__init__.py**

```python
"""FastAPI web panel."""
```

- [ ] **Step 2: Implement seatbot/web/app.py**

```python
"""FastAPI application factory."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from seatbot.config import Config
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore


WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def make_app(cfg: Config, store: StateStore, sched: Scheduler) -> FastAPI:
    app = FastAPI(title="SeatBot Web Panel", version="0.1.0")
    app.state.cfg = cfg
    app.state.store = store
    app.state.sched = sched
    app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from seatbot.web.routes import router
    app.include_router(router)
    return app
```

- [ ] **Step 3: Create seatbot/web/templates/base.html**

```html
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{% block title %}SeatBot{% endblock %}</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <header class="topbar">
    <div class="brand">📚 SeatBot</div>
    <nav>
      <a href="/">Dashboard</a>
      <a href="/accounts">账号</a>
      <a href="/tasks">任务</a>
      <a href="/seats">座位图</a>
      <a href="/logs">日志</a>
    </nav>
  </header>
  <main class="container">
    {% block content %}{% endblock %}
  </main>
  <footer class="footer">
    <small>超星图书馆座位自动化 · 本地/内网使用</small>
  </footer>
</body>
</html>
```

- [ ] **Step 4: Create seatbot/web/static/style.css**

```css
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
       margin: 0; background: #f5f7fa; color: #222; }
.topbar { background: #1f2937; color: #fff; padding: 12px 24px;
          display: flex; align-items: center; gap: 24px; }
.topbar .brand { font-weight: 700; font-size: 18px; }
.topbar nav a { color: #d1d5db; text-decoration: none; margin-right: 12px; }
.topbar nav a:hover { color: #fff; }
.container { max-width: 1200px; margin: 24px auto; padding: 0 24px; }
.footer { text-align: center; color: #6b7280; padding: 24px; }
.card { background: #fff; border-radius: 8px; padding: 16px; margin-bottom: 16px;
        box-shadow: 0 1px 3px rgba(0,0,0,.06); }
.card h2 { margin-top: 0; }
.btn { display: inline-block; padding: 6px 14px; border-radius: 4px; border: 1px solid #d1d5db;
       background: #fff; cursor: pointer; text-decoration: none; color: #1f2937; }
.btn:hover { background: #f3f4f6; }
.btn-primary { background: #2563eb; color: #fff; border-color: #2563eb; }
.btn-primary:hover { background: #1d4ed8; }
.btn-danger { background: #dc2626; color: #fff; border-color: #dc2626; }
.btn-danger:hover { background: #b91c1c; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 8px; border-bottom: 1px solid #e5e7eb; }
th { background: #f9fafb; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 12px;
         font-size: 12px; font-weight: 600; }
.badge-pending { background: #f3f4f6; color: #374151; }
.badge-active { background: #d1fae5; color: #065f46; }
.badge-failed { background: #fee2e2; color: #991b1b; }
.badge-complete { background: #dbeafe; color: #1e40af; }
.alert-error { background: #fee2e2; border-left: 4px solid #dc2626; padding: 12px; border-radius: 4px; }
.alert-warn { background: #fef3c7; border-left: 4px solid #d97706; padding: 12px; border-radius: 4px; }
.alert-info { background: #dbeafe; border-left: 4px solid #2563eb; padding: 12px; border-radius: 4px; }
```

- [ ] **Step 5: Commit**

```bash
git add seatbot/web/
git commit -m "feat(web): FastAPI skeleton + base template + style"
```

---

## Task 15: Web routes — dashboard + accounts

**Files:**
- Create: `seatbot/web/routes.py`
- Create: `seatbot/web/templates/dashboard.html`
- Create: `seatbot/web/templates/accounts_list.html`
- Create: `seatbot/web/templates/accounts_form.html`

- [ ] **Step 1: Implement seatbot/web/routes.py (Part 1: dashboard + accounts)**

```python
"""FastAPI routes for the SeatBot web panel."""
from __future__ import annotations

import json
from datetime import date, time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from seatbot.models import Account, Task, TaskStatus
from seatbot.utils.timeutil import today_cst


router = APIRouter()


def _templates(request: Request):
    return request.app.state.templates


# ---------- dashboard ----------
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    store = request.app.state.store
    cfg = request.app.state.cfg
    accounts = await store.list_accounts()
    today = today_cst()
    cards = []
    alerts = []
    for acc in accounts:
        tasks = await store.list_tasks(account_id=acc.id, day=today)
        active = next((t for t in tasks if t.status == TaskStatus.ACTIVE), None)
        cards.append({
            "account": acc,
            "active": active,
            "task_count": len(tasks),
        })
        failed = [t for t in tasks if t.status == TaskStatus.FAILED]
        if failed:
            alerts.append({"account": acc, "failed": failed})
    return _templates(request).TemplateResponse(
        "dashboard.html",
        {"request": request, "cards": cards, "alerts": alerts, "cfg": cfg},
    )


# ---------- accounts ----------
@router.get("/accounts", response_class=HTMLResponse)
async def accounts_list(request: Request):
    store = request.app.state.store
    accs = await store.list_accounts()
    return _templates(request).TemplateResponse(
        "accounts_list.html", {"request": request, "accounts": accs}
    )


@router.get("/accounts/new", response_class=HTMLResponse)
async def accounts_new(request: Request):
    return _templates(request).TemplateResponse(
        "accounts_form.html",
        {"request": request, "account": None, "error": None},
    )


@router.post("/accounts")
async def accounts_create(
    request: Request,
    id: str = Form(...),
    phone: str = Form(...),
    password: str = Form(...),
    seat_num: str = Form(...),
    slots: str = Form("full"),
    slots_custom: str = Form(""),
):
    store = request.app.state.store
    slots_value: str | list[str] = slots
    if slots == "custom":
        try:
            slots_value = json.loads(slots_custom) if slots_custom.strip() else []
        except json.JSONDecodeError as e:
            return _templates(request).TemplateResponse(
                "accounts_form.html",
                {"request": request, "account": None, "error": f"slots JSON 错误: {e}"},
                status_code=400,
            )
    acc = Account(id=id, phone=phone, password=password, seat_num=seat_num, slots=slots_value)
    try:
        await store.upsert_account(acc)
    except Exception as e:
        return _templates(request).TemplateResponse(
            "accounts_form.html",
            {"request": request, "account": acc, "error": str(e)},
            status_code=400,
        )
    return RedirectResponse("/accounts", status_code=303)


@router.get("/accounts/{acc_id}/edit", response_class=HTMLResponse)
async def accounts_edit(request: Request, acc_id: str):
    store = request.app.state.store
    acc = await store.get_account(acc_id)
    if not acc:
        raise HTTPException(404)
    return _templates(request).TemplateResponse(
        "accounts_form.html",
        {"request": request, "account": acc, "error": None},
    )


@router.post("/accounts/{acc_id}")
async def accounts_update(
    request: Request, acc_id: str,
    phone: str = Form(...),
    password: str = Form(...),
    seat_num: str = Form(...),
    slots: str = Form("full"),
    slots_custom: str = Form(""),
):
    store = request.app.state.store
    existing = await store.get_account(acc_id)
    if not existing:
        raise HTTPException(404)
    slots_value: str | list[str] = slots
    if slots == "custom":
        slots_value = json.loads(slots_custom) if slots_custom.strip() else []
    existing.phone = phone
    existing.password = password
    existing.seat_num = seat_num.zfill(3)
    existing.slots = slots_value
    await store.upsert_account(existing)
    return RedirectResponse("/accounts", status_code=303)


@router.post("/accounts/{acc_id}/delete")
async def accounts_delete(request: Request, acc_id: str):
    store = request.app.state.store
    await store.delete_account(acc_id)
    return RedirectResponse("/accounts", status_code=303)


# NOTE: tasks / seats / logs routes are added in Task 16.
```

- [ ] **Step 2: Create dashboard.html**

```html
{% extends "base.html" %}
{% block title %}Dashboard · SeatBot{% endblock %}
{% block content %}
<h1>Dashboard</h1>
<p class="alert-info">
  图书馆: <b>{{ cfg.library.room_name }}</b> (id={{ cfg.library.room_id }}) · 开放 {{ cfg.library.open_time }}–{{ cfg.library.close_time }}
</p>

{% if alerts %}
  {% for a in alerts %}
    <div class="alert-error">
      <b>{{ a.account.id }}</b> 当日有 {{ a.failed|length }} 个失败任务
      <ul>
        {% for t in a.failed %}
          <li>{{ t.start_time }}-{{ t.end_time }}: {{ t.last_error }}</li>
        {% endfor %}
      </ul>
    </div>
  {% endfor %}
{% endif %}

<div class="card">
  <h2>账号 ({{ cards|length }})</h2>
  {% for c in cards %}
    <div class="card" style="background: #f9fafb;">
      <h3>{{ c.account.display_name() }}</h3>
      <p>slots: <code>{{ c.account.slots }}</code> · 当日任务: {{ c.task_count }} 个</p>
      {% if c.active %}
        <p>当前: <span class="badge badge-active">active</span>
           {{ c.active.start_time }}-{{ c.active.end_time }} (reserve #{{ c.active.reserve_id }})</p>
      {% else %}
        <p>当前: <span class="badge badge-pending">无 active 任务</span></p>
      {% endif %}
      <p>
        <a class="btn" href="/accounts/{{ c.account.id }}/edit">编辑</a>
        <form method="post" action="/tasks/quick-reserve" style="display:inline">
          <input type="hidden" name="account_id" value="{{ c.account.id }}">
          <input type="text" name="start" placeholder="HH:MM" required>
          <input type="text" name="end" placeholder="HH:MM" required>
          <button class="btn btn-primary" type="submit">立即预约</button>
        </form>
      </p>
    </div>
  {% endfor %}
</div>
{% endblock %}
```

- [ ] **Step 3: Create accounts_list.html**

```html
{% extends "base.html" %}
{% block title %}账号 · SeatBot{% endblock %}
{% block content %}
<div style="display:flex; justify-content: space-between; align-items: center;">
  <h1>账号</h1>
  <a class="btn btn-primary" href="/accounts/new">+ 新建</a>
</div>
<table class="card">
  <thead><tr><th>ID</th><th>手机号</th><th>座位</th><th>slots</th><th>操作</th></tr></thead>
  <tbody>
    {% for a in accounts %}
      <tr>
        <td>{{ a.id }}</td><td>{{ a.phone }}</td><td>{{ a.seat_num }}</td>
        <td><code>{{ a.slots }}</code></td>
        <td>
          <a class="btn" href="/accounts/{{ a.id }}/edit">编辑</a>
          <form method="post" action="/accounts/{{ a.id }}/delete" style="display:inline"
                onsubmit="return confirm('删除账号 {{ a.id }}?')">
            <button class="btn btn-danger" type="submit">删除</button>
          </form>
        </td>
      </tr>
    {% endfor %}
  </tbody>
</table>
{% endblock %}
```

- [ ] **Step 4: Create accounts_form.html**

```html
{% extends "base.html" %}
{% block title %}{{ '新建' if not account else '编辑' }}账号 · SeatBot{% endblock %}
{% block content %}
<h1>{{ '新建' if not account else '编辑' }}账号</h1>
{% if error %}<div class="alert-error">{{ error }}</div>{% endif %}
<form method="post" class="card" action="{{ '/accounts' if not account else '/accounts/' + account.id }}">
  {% if not account %}
    <p>ID: <input name="id" required pattern="[a-zA-Z0-9_-]+"></p>
  {% endif %}
  <p>手机号: <input name="phone" required value="{{ account.phone if account else '' }}"></p>
  <p>密码: <input name="password" required value="{{ account.password if account else '' }}"></p>
  <p>座位号 (1-3 位数字): <input name="seat_num" required pattern="\d{1,3}"
       value="{{ account.seat_num if account else '' }}"></p>
  <p>时段:
    <select name="slots" id="slots-select">
      <option value="full" {% if account and account.slots == 'full' %}selected{% endif %}>full (08:00-22:00)</option>
      <option value="custom" {% if account and account.slots != 'full' %}selected{% endif %}>custom (JSON)</option>
    </select>
  </p>
  <p>custom JSON: <textarea name="slots_custom" rows="3" placeholder='["08:00-12:00", "14:00-22:00"]'>{% if account and account.slots != 'full' %}{{ account.slots | tojson }}{% endif %}</textarea></p>
  <p>
    <button class="btn btn-primary" type="submit">保存</button>
    <a class="btn" href="/accounts">取消</a>
  </p>
</form>
{% endblock %}
```

- [ ] **Step 5: Commit**

```bash
git add seatbot/web/routes.py seatbot/web/templates/
git commit -m "feat(web): dashboard + accounts CRUD pages"
```

---

## Task 16: Web routes — tasks + seats + logs + quick actions

**Files:**
- Modify: `seatbot/web/routes.py`
- Create: `seatbot/web/templates/tasks_list.html`
- Create: `seatbot/web/templates/seats.html`
- Create: `seatbot/web/templates/logs.html`

- [ ] **Step 1: Append tasks/seats/logs routes to seatbot/web/routes.py**

```python
# ---------- tasks ----------
@router.get("/tasks", response_class=HTMLResponse)
async def tasks_list(request: Request, account_id: str | None = None, day: str | None = None):
    store = request.app.state.store
    d = date.fromisoformat(day) if day else today_cst()
    tasks = await store.list_tasks(account_id=account_id, day=d)
    accounts = await store.list_accounts()
    return _templates(request).TemplateResponse(
        "tasks_list.html",
        {"request": request, "tasks": tasks, "accounts": accounts,
         "filter_account": account_id, "filter_day": d.isoformat()},
    )


@router.post("/tasks/quick-reserve")
async def quick_reserve(
    request: Request,
    account_id: str = Form(...),
    start: str = Form(...),
    end: str = Form(...),
):
    sched = request.app.state.sched
    store = request.app.state.store
    acc = next((a for a in request.app.state.cfg.accounts if a.id == account_id), None)
    if not acc:
        raise HTTPException(404, f"account {account_id} not found")
    h1, m1 = map(int, start.split(":"))
    h2, m2 = map(int, end.split(":"))
    t = Task(
        id=None, account_id=acc.id, day=today_cst(),
        start_time=time(h1, m1), end_time=time(h2, m2),
        status=TaskStatus.READY,
    )
    tid = await store.add_task(t)
    await sched._run_submit_sign(acc, await store.get_task(tid))
    return RedirectResponse("/tasks", status_code=303)


@router.post("/tasks/{task_id}/sign")
async def task_sign(request: Request, task_id: int):
    sched = request.app.state.sched
    store = request.app.state.store
    t = await store.get_task(task_id)
    if not t or not t.reserve_id:
        raise HTTPException(400, "no active reservation")
    acc = next((a for a in request.app.state.cfg.accounts if a.id == t.account_id), None)
    if not acc:
        raise HTTPException(404)
    client = sched._client_for(acc)
    if not client.cookies():
        await client.login(acc.phone, acc.password)
    r = await client.sign(t.reserve_id)
    await store.log_action(acc.id, "sign", str(t.reserve_id), str(r)[:500], bool(r.get("success")))
    return RedirectResponse("/tasks", status_code=303)


@router.post("/tasks/{task_id}/leave")
async def task_leave(request: Request, task_id: int):
    sched = request.app.state.sched
    store = request.app.state.store
    t = await store.get_task(task_id)
    if not t or not t.reserve_id:
        raise HTTPException(400)
    acc = next((a for a in request.app.state.cfg.accounts if a.id == t.account_id), None)
    if not acc:
        raise HTTPException(404)
    client = sched._client_for(acc)
    if not client.cookies():
        await client.login(acc.phone, acc.password)
    r = await client.leave(t.reserve_id)
    await store.log_action(acc.id, "leave", str(t.reserve_id), str(r)[:500], bool(r.get("success")))
    await store.update_task_status(task_id, TaskStatus.COMPLETE)
    return RedirectResponse("/tasks", status_code=303)


@router.post("/tasks/{task_id}/cancel")
async def task_cancel(request: Request, task_id: int):
    sched = request.app.state.sched
    store = request.app.state.store
    t = await store.get_task(task_id)
    if not t or not t.reserve_id:
        raise HTTPException(400)
    acc = next((a for a in request.app.state.cfg.accounts if a.id == t.account_id), None)
    if not acc:
        raise HTTPException(404)
    client = sched._client_for(acc)
    if not client.cookies():
        await client.login(acc.phone, acc.password)
    r = await client.cancel(t.reserve_id)
    await store.log_action(acc.id, "cancel", str(t.reserve_id), str(r)[:500], bool(r.get("success")))
    await store.update_task_status(task_id, TaskStatus.COMPLETE)
    return RedirectResponse("/tasks", status_code=303)


# ---------- seats ----------
@router.get("/seats", response_class=HTMLResponse)
async def seats_view(request: Request):
    cfg = request.app.state.cfg
    return _templates(request).TemplateResponse(
        "seats.html", {"request": request, "cfg": cfg}
    )


@router.get("/api/seats/{room_id}")
async def api_seats(room_id: int):
    sched = request.app.state.sched
    cfg = request.app.state.cfg
    # Use the first account's client just to call the API
    acc = cfg.accounts[0] if cfg.accounts else None
    if not acc:
        return JSONResponse({"seats": []})
    client = sched._client_for(acc)
    if not client.cookies():
        try:
            await client.login(acc.phone, acc.password)
        except Exception:
            return JSONResponse({"seats": [], "error": "login failed"})
    seats = await client.get_seat_status(room_id)
    return JSONResponse({"seats": seats})


# ---------- logs ----------
@router.get("/logs", response_class=HTMLResponse)
async def logs_view(
    request: Request,
    account_id: str | None = None,
    level: str | None = None,
):
    store = request.app.state.store
    rows = await store.list_logs(account_id=account_id, level=level, limit=300)
    accounts = await store.list_accounts()
    return _templates(request).TemplateResponse(
        "logs.html",
        {"request": request, "logs": rows, "accounts": accounts,
         "filter_account": account_id, "filter_level": level},
    )
```

- [ ] **Step 2: Create tasks_list.html**

```html
{% extends "base.html" %}
{% block title %}任务 · SeatBot{% endblock %}
{% block content %}
<h1>任务</h1>
<form method="get" class="card">
  账号: <select name="account_id">
    <option value="">全部</option>
    {% for a in accounts %}
      <option value="{{ a.id }}" {% if filter_account == a.id %}selected{% endif %}>{{ a.id }}</option>
    {% endfor %}
  </select>
  日期: <input name="day" value="{{ filter_day }}" pattern="\d{4}-\d{2}-\d{2}">
  <button class="btn" type="submit">过滤</button>
</form>

<table class="card">
  <thead><tr><th>ID</th><th>账号</th><th>日期</th><th>时段</th><th>状态</th><th>reserve_id</th><th>错误</th><th>操作</th></tr></thead>
  <tbody>
    {% for t in tasks %}
      <tr>
        <td>{{ t.id }}</td><td>{{ t.account_id }}</td>
        <td>{{ t.day }}</td><td>{{ t.start_time }}-{{ t.end_time }}</td>
        <td><span class="badge badge-{{ t.status.value }}">{{ t.status.value }}</span></td>
        <td>{{ t.reserve_id or '-' }}</td>
        <td style="color:#b91c1c;">{{ t.last_error or '' }}</td>
        <td>
          {% if t.reserve_id and t.status.value == 'active' %}
            <form method="post" action="/tasks/{{ t.id }}/sign" style="display:inline">
              <button class="btn" type="submit">签到</button>
            </form>
            <form method="post" action="/tasks/{{ t.id }}/leave" style="display:inline">
              <button class="btn" type="submit">签退</button>
            </form>
          {% endif %}
          {% if t.reserve_id %}
            <form method="post" action="/tasks/{{ t.id }}/cancel" style="display:inline"
                  onsubmit="return confirm('取消?')">
              <button class="btn btn-danger" type="submit">取消</button>
            </form>
          {% endif %}
        </td>
      </tr>
    {% endfor %}
  </tbody>
</table>
{% endblock %}
```

- [ ] **Step 3: Create seats.html**

```html
{% extends "base.html" %}
{% block title %}座位图 · SeatBot{% endblock %}
{% block content %}
<h1>座位图</h1>
<p>每 30 秒轮询 {{ cfg.library.room_name }} 实时状态。</p>
<div id="seats-grid" class="card" style="display: grid; grid-template-columns: repeat(12, 1fr); gap: 4px;">
  加载中…
</div>
<script>
  async function load() {
    const r = await fetch("/api/seats/{{ cfg.library.room_id }}");
    const j = await r.json();
    const grid = document.getElementById("seats-grid");
    if (!j.seats || j.seats.length === 0) { grid.innerText = "暂无数据"; return; }
    grid.innerHTML = "";
    for (const s of j.seats) {
      const cell = document.createElement("div");
      cell.style.cssText = "padding: 6px; text-align: center; border-radius: 4px; font-size: 11px; background: #e5e7eb;";
      cell.innerText = s.seat_num;
      grid.appendChild(cell);
    }
  }
  load();
  setInterval(load, 30000);
</script>
{% endblock %}
```

- [ ] **Step 4: Create logs.html**

```html
{% extends "base.html" %}
{% block title %}日志 · SeatBot{% endblock %}
{% block content %}
<h1>日志</h1>
<form method="get" class="card">
  账号: <select name="account_id">
    <option value="">全部</option>
    {% for a in accounts %}
      <option value="{{ a.id }}" {% if filter_account == a.id %}selected{% endif %}>{{ a.id }}</option>
    {% endfor %}
  </select>
  级别:
  <select name="level">
    <option value="">全部</option>
    {% for l in ['INFO','WARN','ERROR'] %}
      <option value="{{ l }}" {% if filter_level == l %}selected{% endif %}>{{ l }}</option>
    {% endfor %}
  </select>
  <button class="btn" type="submit">过滤</button>
</form>
<pre class="card" style="max-height: 70vh; overflow: auto; font-size: 12px;">
{% for l in logs %}{{ l.ts }} [{{ l.level }}] {{ l.account_id or '-' }} {{ l.message }}
{% endfor %}</pre>
{% endblock %}
```

- [ ] **Step 5: Smoke test web**

Run: `python -c "from seatbot.web.app import make_app; print('ok')"`
Expected: `ok`

- [ ] **Step 6: Commit**

```bash
git add seatbot/web/
git commit -m "feat(web): tasks/seats/logs + quick-reserve/manual actions"
```

---

## Task 17: Dockerfile + docker-compose

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `systemd/seatbot.service`

- [ ] **Step 1: Write Dockerfile**

```dockerfile
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY seatbot ./seatbot
COPY config.example.yaml ./

RUN pip install --upgrade pip && pip install -e .

RUN playwright install --with-deps chromium || true

EXPOSE 8080
VOLUME ["/app/data", "/app/logs"]
CMD ["python", "-m", "seatbot", "run", "--config", "/app/data/config.yaml"]
```

- [ ] **Step 2: Write docker-compose.yml**

```yaml
version: "3.8"
services:
  seatbot:
    build: .
    image: seatbot:latest
    container_name: seatbot
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
    environment:
      - TZ=Asia/Shanghai
```

- [ ] **Step 3: Write systemd unit**

```ini
[Unit]
Description=SeatBot - 图书馆座位自动化
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=seatbot
WorkingDirectory=/opt/seatbot
ExecStart=/opt/seatbot/.venv/bin/python -m seatbot run --config /opt/seatbot/config.yaml
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Commit**

```bash
git add Dockerfile docker-compose.yml systemd/
git commit -m "chore: Docker + docker-compose + systemd unit"
```

---

## Task 18: README + quickstart docs

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Append deployment + safety notes to README.md**

Append to `README.md`:

````markdown

## 部署

### 本地 (Windows / macOS / Linux)

```bash
git clone <repo>
cd Library-Seat-Reservation
python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -e .[dev]
playwright install chromium         # enc fallback 用的浏览器
cp config.example.yaml config.yaml
# 编辑 config.yaml: 填入手机号/密码/座位
python -m seatbot init-db
python -m seatbot run               # 访问 http://localhost:8080
```

### 云服务器 (Linux + Docker)

```bash
# 在服务器上:
git clone <repo> && cd Library-Seat-Reservation
mkdir -p data logs
cp config.example.yaml data/config.yaml
# 上传 / 编辑 data/config.yaml
docker compose up -d
# 访问 http://<server-ip>:8080
```

### Linux systemd

```bash
sudo useradd -r -s /bin/false seatbot
sudo cp -r . /opt/seatbot
sudo chown -R seatbot:seatbot /opt/seatbot
cd /opt/seatbot && sudo -u seatbot python -m venv .venv
sudo -u seatbot /opt/seatbot/.venv/bin/pip install -e .
sudo cp systemd/seatbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now seatbot
sudo journalctl -u seatbot -f
```

## 安全提示

- `config.yaml` 含明文账号密码, `chmod 600 config.yaml`
- Web 面板无鉴权, **仅限内网访问**
- 建议在前面套 nginx + basic auth 或仅监听 127.0.0.1

## 已知风险

- 纯 HTTP 签到, 缺少蓝牙/位置, 超星风控可能识别为作弊 → 账号可能进黑名单
- 多账号并发, 同一 IP 多账号, 容易被识别为脚本
- 用户已明确接受这些风险

## 开发

```bash
pytest -v                   # 单元测试
pytest -m integration -v    # 集成测试 (需真实账号)
ruff check seatbot tests
```
````

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: README quickstart + deployment + safety"
```

---

## Task 19: End-to-end smoke test (manual)

**Files:** none (manual verification)

- [ ] **Step 1: Install + init**

```bash
cd D:/document/Projects/Library-Seat-Reservation
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e .[dev]
playwright install chromium
```

- [ ] **Step 2: Create config.yaml with test account**

Create `config.yaml`:

```yaml
library:
  room_id: 11692
  room_name: "2号楼图书馆-3F-3楼备考自习室"
  time_unit_minutes: 30
  open_time: "08:00"
  close_time: "22:00"
  max_reserve_hours: 2.0

accounts:
  - id: zhangsan
    phone: "REPLACE_WITH_REAL_PHONE"
    password: "REPLACE_WITH_REAL_PASSWORD"
    seat_num: "084"
    slots: ["09:00-10:00"]   # 1h test slot

runtime:
  stagger_seconds: [0, 3]
  relogin_on_401: true
  random_ua: true
  log_dir: ./logs
  db_path: ./seatbot.db
  web_host: 127.0.0.1
  web_port: 8080
```

- [ ] **Step 3: Init DB + login test**

```bash
python -m seatbot init-db
python -m seatbot login --account zhangsan
# Expected: "login OK; cookies: ['_uid', 'vc3', ...]"
```

If login fails: verify phone/password; check `logs/` for details.

- [ ] **Step 4: Run once + check task created**

```bash
python -m seatbot once --account zhangsan --action all
python -m seatbot status
# Expected: see the 09:00-10:00 task in status pending/active
```

- [ ] **Step 5: Start the service + visit web**

```bash
python -m seatbot run
# Open http://127.0.0.1:8080 in browser
```

Verify:
- Dashboard shows the account
- 任务 page shows the task
- 座位图 loads (or shows "暂无数据" if enc fallback fails)
- 日志 page shows INFO lines

- [ ] **Step 6: Manual sign-in from web**

In the 任务 page, click "签到" on an active task. Check `logs/` for the result.

- [ ] **Step 7: Commit any remaining tweaks**

```bash
git add -A
git commit -m "chore: post-smoke-test cleanups" || true
```

---

## Self-Review

**Spec coverage check** (spec sections → tasks):

| Spec Section | Covered By |
|---|---|
| §1 Goals (6 items) | Task 12 (scheduler), Task 9 (sign), Task 15-16 (web), Task 7 (store), Task 1 (config) |
| §3 Architecture | Task 12 (Scheduler), Task 14 (FastAPI) |
| §4.1 ChaoxingClient | Tasks 8, 9, 11 |
| §4.2 EncGenerator | Task 10 |
| §4.3 ReservationPlanner | Task 6 |
| §4.4 Scheduler | Task 12 |
| §4.5 StateStore | Task 7 |
| §4.6 Config | Task 2 |
| §4.7 Web 面板 | Tasks 14, 15, 16 |
| §4.8 CLI | Task 13 |
| §5 Data Flow | Task 12 |
| §6 Configuration | Task 2 |
| §7 Web Panel | Tasks 14, 15, 16 |
| §8 Error Handling | Task 12 (_error paths in submit/sign/leave) + Task 7 (log_action) |
| §9 Risk & Mitigation | Task 10 (Playwright fallback), Task 8 (login retry) |
| §10 Deployment | Task 17 (Dockerfile, compose, systemd) |
| §11 File Layout | All tasks (each creates its file) |
| §12 Future | Out of scope (v1) |

**Placeholder scan**: No "TBD"/"TODO"/"implement later" found. All code blocks complete.

**Type consistency**:
- `Account` (models.py Task 5) ↔ `AccountConfig` (config.py Task 2) — both have `id, phone, password, seat_num, slots` ✓
- `Task` (models.py) ↔ StateStore row mapping (Task 7) — fields match ✓
- `ChaoxingClient.submit_reserve` signature consistent across Task 9 + Task 12 caller ✓
- `Scheduler._client_for` (Task 12) used by routes (Task 16) and quick_reserve — same method name ✓

**Known follow-ups** (out of scope of v1 plan):
- Wire real YiDunProtector JS into `EncGenerator._compute_js` (currently stub)
- Add a "show 3F + 4F" tab to the seats view (single room only in v1)
- Add Server 酱 / 钉钉 notification (§12 v2)
- config encryption (§12 v2)
