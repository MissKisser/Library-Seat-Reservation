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