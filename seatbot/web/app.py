"""FastAPI application factory."""
from __future__ import annotations

import hmac
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from seatbot import __version__
from seatbot.config import Config
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore


WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

TOKEN_COOKIE = "seatbot_token"


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
    client_host: str | None = None,
    host_header: str = "",
    presented: str | None = None,
    allowed_hosts: set[str] | None = None,
) -> tuple[bool, int, str, bool]:
    """面板访问判定 (纯函数, 便于单测)。

    返回 (允许, 拒绝状态码, 拒绝说明, 需要种 cookie)。
    规则:
      - 配置了 web_token: 必须携带正确令牌, 否则 401 (不区分来源 IP 与 Host)。
      - 未配置 web_token:
          - 若显式配置了 allowed_hosts, 则校验 Host 头 (不在白名单 → 400);
          - 若未配置 allowed_hosts, 则直接放行, 不做 Host/IP 二次拦截。
    """
    token = (web_token or "").strip()
    if token:
        if hmac.compare_digest((presented or "").encode(), token.encode()):
            return True, 0, "", True
        return False, 401, "unauthorized: missing or invalid token", False
    if allowed_hosts:
        host = _host_without_port(host_header)
        if host and host not in allowed_hosts:
            return False, 400, f"bad request: host '{host}' not allowed", False
    return True, 0, "", False

def _origin_netloc(value: str) -> str:
    """从 Origin/Referer 头提取 host:port（小写，无 scheme/path）；解析失败返回空串。"""
    try:
        return urlsplit(value.strip()).netloc.lower()
    except ValueError:
        return ""


def csrf_decision(
    *,
    method: str,
    origin: str | None,
    referer: str | None,
    host_header: str,
) -> tuple[bool, str]:
    """写请求跨站防护判定 (纯函数, 便于单测)。

    返回 (放行, 拒绝说明)。规则:
      - 非写方法 (POST/PUT/DELETE/PATCH 之外) 一律放行;
      - 浏览器发起的跨站写请求必带 Origin (form POST) 或 Referer——
        两者均缺失视为非浏览器调用 (curl/服务间), 交由认证层把守, 放行;
      - 带了来源头时, 其 host:port 必须与 Host 头同源, 否则拒绝。
    同源判定按 netloc 精确比较: 攻击页 Origin=evil.com ≠ 本机 Host → 403,
    即便请求源 IP 是用户本机 (回环模式) 也无法触发写端点。
    """
    if method.upper() not in {"POST", "PUT", "DELETE", "PATCH"}:
        return True, ""
    source = (origin or "").strip() or (referer or "").strip()
    if not source:
        return True, ""
    src = _origin_netloc(source)
    dst = _origin_netloc(f"//{(host_header or '').strip()}")
    if not src or not dst:
        return False, "cross-origin write rejected: unparseable origin header"
    if src == dst:
        return True, ""
    return False, "cross-origin write rejected"


class PanelAuthMiddleware(BaseHTTPMiddleware):
    """面板认证/来源加固中间件 (令牌模式或回环白名单模式)。"""

    def __init__(self, app, web_token: str, allowed_hosts: set[str]):
        super().__init__(app)
        self.web_token = web_token
        self.allowed_hosts = allowed_hosts

    async def dispatch(self, request: Request, call_next):
        # /api/ha/* 由独立 X-HA-Key 鉴权，不走面板 web_token
        if request.url.path.startswith("/api/ha/"):
            return await call_next(request)
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
        csrf_ok, csrf_reason = csrf_decision(
            method=request.method,
            origin=request.headers.get("origin"),
            referer=request.headers.get("referer"),
            host_header=request.headers.get("host") or "",
        )
        if not csrf_ok:
            return PlainTextResponse(csrf_reason, status_code=403)
        response = await call_next(request)
        if set_cookie and via_query:
            response.set_cookie(
                TOKEN_COOKIE, self.web_token,
                max_age=30 * 24 * 3600, httponly=True, samesite="lax",
            )
        return response


# ----- HA: 备用待命期写保护 -----

WRITE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}


class HaWriteGuardMiddleware(BaseHTTPMiddleware):
    """备用实例待命期（mode==backup and backup_state==standby）拦截写请求。

    豁免:
      - /api/ha/* 控制面（前置已豁免 PanelAuth；中间件保留豁免以防双层挂载误判）
      - /settings（必须能改自己的 HA 配置）
      - GET / HEAD / OPTIONS
    """

    async def dispatch(self, request: Request, call_next):
        try:
            ha = getattr(request.app.state, "ha", None)
        except Exception:
            ha = None
        if ha is None:
            return await call_next(request)
        try:
            standby = (ha.mode == "backup" and ha.backup_state == "standby")
        except Exception:
            standby = False
        if not standby:
            return await call_next(request)
        if request.method.upper() not in WRITE_METHODS:
            return await call_next(request)
        path = request.url.path or ""
        if path.startswith("/api/ha/"):
            return await call_next(request)
        if path == "/settings":
            return await call_next(request)
        # API 路径 / reset / .json → JSON；表单路径 → 303 重定向回来源页
        if path.startswith("/api/") or path.endswith(".json") or path == "/settings/reset":
            from starlette.responses import JSONResponse as _JR
            return _JR(
                {"detail": "备用待命期禁止写入操作；请切到主力或等待自动回切。"},
                status_code=409,
            )
        # 表单提交 → 重定向回 referer（若有）否则首页
        from urllib.parse import quote
        referer = request.headers.get("referer") or "/"
        target = f"{referer}?ha_readonly=1"
        from starlette.responses import RedirectResponse as _RR
        return _RR(url=target, status_code=303)


_STATIC_DIR = TEMPLATES_DIR.parent / "static"


def _static_v(name: str) -> str:
    """静态资源的 mtime 数字版本号: 内容一变 URL 即变, 浏览器缓存自然失效。"""
    return str(int((_STATIC_DIR / name).stat().st_mtime))


def new_templates() -> Jinja2Templates:
    """统一模板实例: 挂好全局函数, 测试桩与 make_app 共用同一配置。"""
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["epoch_ms"] = _epoch_ms
    templates.env.globals["static_v"] = _static_v
    return templates


def _epoch_ms(value) -> str:
    """epoch 毫秒 → 'YYYY-MM-DD HH:MM:SS' (北京时间, 与超星 API 时区一致)。"""
    try:
        dt = datetime.fromtimestamp(int(value) / 1000, tz=timezone(timedelta(hours=8)))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return str(value)


def make_app(cfg: Config, store: StateStore, sched: Scheduler) -> FastAPI:
    app = FastAPI(title="SeatBot Web Panel", version=__version__)
    app.state.cfg = cfg
    app.state.store = store
    app.state.sched = sched
    templates = new_templates()
    app.state.templates = templates

    allowed_hosts = {_host_without_port(h) for h in (cfg.runtime.allowed_hosts or []) if h}
    app.add_middleware(
        PanelAuthMiddleware,
        web_token=(cfg.runtime.web_token or "").strip(),
        allowed_hosts=allowed_hosts,
    )
    app.add_middleware(HaWriteGuardMiddleware)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from seatbot.web.routes import router
    app.include_router(router)

    from seatbot.ha import NullHaRuntime
    from seatbot.web.ha_routes import router as ha_router
    app.include_router(ha_router)
    ha_obj = getattr(sched, "ha", None) if sched is not None else None
    app.state.ha = ha_obj if ha_obj is not None else NullHaRuntime()
    app.state.ha_supervisor_task = None
    return app
