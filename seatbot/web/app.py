"""FastAPI application factory."""
from __future__ import annotations

import hmac
import logging
import time
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
from seatbot import settings as _settings
from seatbot.config import Config
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

TOKEN_COOKIE = "seatbot_token"

#: 回环主机名 / IP: 未配置令牌与 allowed_hosts 时仅允许这些来源访问面板。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _host_without_port(host_header: str) -> str:
    """从 Host 头剥离端口 ('127.0.0.1:8080' → '127.0.0.1'; '[::1]:8080' → '::1')。"""
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end > 0 else host
    if host.count(":") == 1:
        host = host.split(":")[0]
    return host


def _is_loopback_host(host: str) -> bool:
    """判断主机名是否属于本机回环地址。

    入参:
        host: 已小写并剥离端口的主机名。

    返回值:
        属于回环地址时返回 True，否则返回 False。
    """
    return host in LOOPBACK_HOSTS


def _is_loopback_client(client_host: str | None) -> bool:
    """判断请求来源地址是否属于本机回环。

    入参:
        client_host: 客户端 IP（未知时为 None 或空串）。

    返回值:
        属于回环地址时返回 True，否则返回 False。
    """
    if not client_host:
        return False
    h = client_host.strip().lower()
    if h.startswith("[") and "]" in h:
        h = h[1:h.index("]")]
    return h in LOOPBACK_HOSTS


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


def _effective_host(host_header: str, forwarded_host: str = "") -> str:
    """取本次请求用于访问判定的 Host。

    入参:
        host_header: HTTP Host 头。
        forwarded_host: X-Forwarded-Host 头（反向代理场景；可能含逗号分隔的链）。

    返回值:
        判定用的主机名（已小写并剥离端口）。
    """
    fwd = (forwarded_host or "").split(",")[0].strip()
    return _host_without_port(fwd or host_header)


def auth_decision(
    *,
    web_token: str,
    client_host: str | None = None,
    host_header: str = "",
    forwarded_host: str = "",
    presented: str | None = None,
    allowed_hosts: set[str] | None = None,
) -> tuple[bool, int, str, bool]:
    """面板访问判定 (纯函数, 便于单测)。

    返回 (允许, 拒绝状态码, 拒绝说明, 需要种 cookie)。
    主机名取 X-Forwarded-Host（存在时优先，否则用 Host），已小写并剥离端口。

    规则:
      - 配置了 web_token: 必须携带正确令牌, 否则 401 (不区分来源 IP 与 Host)。
      - 未配置 web_token 且显式配置了 allowed_hosts: 校验主机名
        (不在白名单且非本机回环 → 400)。
      - 未配置 web_token 且未配置 allowed_hosts: 仅允许本机回环访问
        (主机名与来源地址都必须是回环, 否则 403; 面板默认无鉴权, 不得暴露到外网)。

    本机回环豁免同时校验来源地址: Host 头可被伪造, 只有来源 IP 也是回环
    才能认定为真正的本机访问。
    """
    token = (web_token or "").strip()
    if token:
        if hmac.compare_digest((presented or "").encode(), token.encode()):
            return True, 0, "", True
        return False, 401, "unauthorized: missing or invalid token", False
    host = _effective_host(host_header, forwarded_host)
    loopback = _is_loopback_host(host) and _is_loopback_client(client_host)
    if allowed_hosts:
        if host and host not in allowed_hosts and not loopback:
            return False, 400, f"bad request: host '{host}' not allowed", False
        return True, 0, "", False
    if loopback:
        return True, 0, "", False
    return False, 403, "forbidden: panel is restricted to loopback clients", False

def _origin_netloc(value: str) -> str:
    """从 Origin/Referer 头提取 host:port（小写，无 scheme/path）；解析失败返回空串。"""
    try:
        return urlsplit(value.strip()).netloc.lower()
    except ValueError as e:
        logger.debug("来源头 %r 无法解析为 host:port: %s", value, e)
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
        """创建中间件。

        入参:
            app: 被包装的 ASGI 应用。
            web_token: 面板访问令牌；为空表示走回环白名单模式。
            allowed_hosts: 允许访问面板的主机名白名单。
        """
        super().__init__(app)
        self.web_token = web_token
        self.allowed_hosts = allowed_hosts

    async def dispatch(self, request: Request, call_next):
        """校验访问来源与令牌，通过后放行请求。"""
        client_host = request.client.host if request.client else None
        via_query = "token" in request.query_params
        ok, status, reason, set_cookie = auth_decision(
            web_token=self.web_token,
            client_host=client_host,
            host_header=request.headers.get("host") or "",
            forwarded_host=request.headers.get("x-forwarded-host") or "",
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




_STATIC_DIR = TEMPLATES_DIR.parent / "static"


def _static_v(name: str) -> str:
    """静态资源的 mtime 数字版本号: 内容一变 URL 即变, 浏览器缓存自然失效。

    入参为 static 目录下的相对文件名；文件缺失时回落到进程启动时刻，
    不让这个辅助函数把整个渲染链带崩（否则所有页面一起 500）。
    """
    try:
        return str(int((_STATIC_DIR / name).stat().st_mtime))
    except OSError as e:
        logger.debug("静态资源 %s 的 mtime 不可用，回落到进程启动时刻: %s", name, e)
        return str(int(time.time()))


def new_templates() -> Jinja2Templates:
    """统一模板实例: 挂好全局函数与版本号, 测试桩与 make_app 共用同一配置。"""
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["epoch_ms"] = _epoch_ms
    templates.env.globals["static_v"] = _static_v
    templates.env.globals["app_version"] = __version__
    templates.env.globals["settings"] = _settings
    return templates


def _epoch_ms(value) -> str:
    """epoch 毫秒 → 'YYYY-MM-DD HH:MM:SS' (北京时间, 与超星 API 时区一致)。"""
    try:
        dt = datetime.fromtimestamp(int(value) / 1000, tz=timezone(timedelta(hours=8)))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError) as e:
        logger.debug("epoch 毫秒 %r 无法转换为北京时间: %s", value, e)
        return str(value)


def make_app(cfg: Config, store: StateStore, sched: Scheduler) -> FastAPI:
    """组装 FastAPI 应用：挂中间件、静态资源与路由。

    入参:
        cfg: 已加载的配置。
        store: 状态存储实例。
        sched: 调度器实例。

    返回值:
        配置好的 FastAPI 应用。
    """
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

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from seatbot.web.routes import router
    app.include_router(router)

    return app
