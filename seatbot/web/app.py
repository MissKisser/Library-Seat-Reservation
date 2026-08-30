"""FastAPI application factory."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from seatbot.config import Config
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore


WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

TOKEN_COOKIE = "seatbot_token"
LOOPBACK_CLIENT_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _host_without_port(host_header: str) -> str:
    """从 Host 头剥离端口 ('127.0.0.1:8080' → '127.0.0.1'; '[::1]:8080' → '::1')。"""
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end > 0 else host
    if host.count(":") == 1:
        host = host.split(":")[0]
    return host


def _presented_token(request: Request) -> str | None:
    """按 header(Bearer / X-Auth-Token) → query(?token=) → cookie 顺序提取令牌。"""
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    header_tok = request.headers.get("x-auth-token")
    if header_tok:
        return header_tok.strip()
    query_tok = request.query_params.get("token")
    if query_tok:
        return query_tok.strip()
    return request.cookies.get(TOKEN_COOKIE)


def auth_decision(
    *,
    web_token: str,
    client_host: str | None,
    host_header: str,
    presented: str | None,
    allowed_hosts: set[str],
) -> tuple[bool, int, str, bool]:
    """面板访问判定 (纯函数, 便于单测)。

    返回 (允许, 拒绝状态码, 拒绝说明, 需要种 cookie)。
    规则:
      - 配置了 web_token: 必须携带正确令牌, 否则 401 (不区分来源地址)。
      - 未配置 web_token: 仅放行本机回环客户端; 非回环 403;
        同时校验 Host 头防 DNS rebinding (不在白名单 → 400)。
    """
    token = (web_token or "").strip()
    if token:
        if presented == token:
            return True, 0, "", True
        return False, 401, "unauthorized: missing or invalid token", False
    host = _host_without_port(host_header)
    if host and host not in allowed_hosts:
        return False, 400, f"bad request: host '{host}' not allowed", False
    if client_host is not None and client_host not in LOOPBACK_CLIENT_HOSTS:
        return False, 403, "forbidden: panel has no token set and only allows loopback clients", False
    if client_host is None:
        return False, 403, "forbidden: client address unavailable", False
    return True, 0, "", False


class PanelAuthMiddleware(BaseHTTPMiddleware):
    """面板认证/来源加固中间件 (令牌模式或回环白名单模式)。"""

    def __init__(self, app, web_token: str, allowed_hosts: set[str]):
        super().__init__(app)
        self.web_token = web_token
        self.allowed_hosts = allowed_hosts

    async def dispatch(self, request: Request, call_next):
        client_host = request.client.host if request.client else None
        via_query = "token" in request.query_params
        ok, status, reason, set_cookie = auth_decision(
            web_token=self.web_token,
            client_host=client_host,
            host_header=request.headers.get("host") or "",
            presented=_presented_token(request),
            allowed_hosts=self.allowed_hosts,
        )
        if not ok:
            return PlainTextResponse(reason, status_code=status)
        response = await call_next(request)
        if set_cookie and via_query:
            response.set_cookie(
                TOKEN_COOKIE, self.web_token,
                max_age=30 * 24 * 3600, httponly=True, samesite="lax",
            )
        return response


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

    allowed_hosts = {"localhost", "127.0.0.1", "::1", _host_without_port(cfg.runtime.web_host)}
    app.add_middleware(
        PanelAuthMiddleware,
        web_token=(cfg.runtime.web_token or "").strip(),
        allowed_hosts=allowed_hosts,
    )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from seatbot.web.routes import router
    app.include_router(router)
    return app