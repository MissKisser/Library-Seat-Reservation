"""跨进程 cookie 缓存 — 一次性脚本复用登录态, 减少真实登录次数。

背景: 2026-08-25 实测, 超星 passport 对"同账号短间隔重复登录"有账号级风控
(触发后静默弹验证码, 表现为 browser login "no auth cookie")。
scheduler/web 进程内已有 per-account client 复用, 但一次性脚本每次全新登录,
是登录频率的主要放大器。本模块把 cookies 落盘到 .session_cache/<acct>.json,
让独立进程之间共享会话。

⚠️ .session_cache/ 含登录凭证等效物, 已加入 .gitignore, 禁止提交/外传。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Awaitable, Callable

from seatbot.client import ChaoxingClient

CACHE_DIR = Path(__file__).resolve().parents[1] / ".session_cache"


def _cache_path(acct: str) -> Path:
    return CACHE_DIR / f"{acct}.json"


def save(acct: str, client: ChaoxingClient) -> None:
    cookies = client.cookies()
    if not cookies:
        return
    CACHE_DIR.mkdir(exist_ok=True)
    _cache_path(acct).write_text(json.dumps(cookies), encoding="utf-8")


async def authenticated_client(
    acct: str,
    phone: str,
    password: str,
    probe: Callable[[ChaoxingClient], Awaitable[bool]],
) -> tuple[ChaoxingClient, str]:
    """返回带有效会话的 client。来源: 'cache' (复用) 或 'fresh-login' (新登录)。

    probe 拿 client 试一个轻量鉴权接口; 缓存 cookie 无效时不复用 (避免把死
    cookie 灌进 submit_in_browser 的浏览器导致流程卡死), 而是新建 client 登录。
    """
    client = ChaoxingClient()
    p = _cache_path(acct)
    if p.exists():
        try:
            for name, value in json.loads(p.read_text(encoding="utf-8")).items():
                client._cookie_jar.set(name, value, domain=".chaoxing.com", path="/")
        except Exception:
            client = ChaoxingClient()
        else:
            try:
                if await probe(client):
                    return client, "cache"
            except Exception:
                pass
        # 缓存失效 → 干净的 client 重新登录
        await client.close()
        client = ChaoxingClient()
    await client.login(phone, password)
    save(acct, client)
    return client, "fresh-login"
