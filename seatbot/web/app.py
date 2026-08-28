"""FastAPI application factory."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


def _epoch_ms(value) -> str:
    """epoch 毫秒 → 'YYYY-MM-DD HH:MM:SS' (北京时间, 与超星 API 时区一致)。"""
    try:
        dt = datetime.fromtimestamp(int(value) / 1000, tz=timezone(timedelta(hours=8)))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return str(value)


def make_app(cfg: Config, store: StateStore, sched: Scheduler) -> FastAPI:
    app = FastAPI(title="SeatBot Web Panel", version="0.1.0")
    app.state.cfg = cfg
    app.state.store = store
    app.state.sched = sched
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["epoch_ms"] = _epoch_ms
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from seatbot.web.routes import router
    app.include_router(router)
    return app