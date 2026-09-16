"""移动端页级零溢出守卫（spec §7 完成定义）。

- 无浏览器环境：整文件 pytest.skip
- 有浏览器：线程内 uvicorn 承载种子应用（port=0 由内核分配随机端口，
  绝不触碰固定端口/生产实例），Playwright 以 375 / 320 / 1280 三档
  真实渲染 12 页，逐页断言：
  - documentElement.scrollWidth ≤ innerWidth + 1（页级零溢出）
  - 横滚容器（.gantt-strip / .scrollx）可达性：自身可横向滚动，
    否则内容必须完整放下（防 overflow-hidden 裁剪造成的假阴性）

全程零真实账号操作：隔离临时库 + 中性种子 + ChaoxingClient 全网络
方法打桩，页面链路不产生任何真实外呼。
"""
import asyncio
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="需要 playwright 才能渲染移动端 CSS")
pytest.importorskip("uvicorn", reason="需要 uvicorn 承载隔离渲染服务")

import uvicorn  # noqa: E402

from seatbot.config import load_config  # noqa: E402
from seatbot.scheduler import Scheduler  # noqa: E402
from seatbot.store import StateStore  # noqa: E402
from seatbot.web.app import make_app  # noqa: E402

TOKEN = "smoke-token"

# 12 页路由：覆盖 spec §2 全部页面
PAGES = [
    "/", "/targets", "/bindings", "/accounts", "/accounts/new", "/tasks",
    "/hosting", "/manual", "/reservations", "/logs", "/settings", "/audit",
]

# 横滚容器可达性：不可滚的横滚容器必须完整放下内容（否则被裁剪不可达）
REACHABILITY_JS = """() => {
  const offenders = [];
  document.querySelectorAll('.gantt-strip, .scrollx').forEach((el) => {
    const scrollable = /(auto|scroll)/.test(getComputedStyle(el).overflowX);
    const fits = el.scrollWidth <= el.clientWidth + 1;
    if (!scrollable && !fits) {
      offenders.push(el.className + ' sw=' + el.scrollWidth + ' cw=' + el.clientWidth);
    }
  });
  return offenders;
}"""


def _stub_chaoxing(monkeypatch) -> None:
    """打桩 ChaoxingClient 全部网络方法，页面链路零真实外呼。"""
    from seatbot.client import ChaoxingClient

    async def _login(self, *args, **kwargs):
        return True

    def _cookies(self):
        return {"stub": "1"}

    async def _get_used_times(self, *args, **kwargs):
        return []

    async def _reserve_list(self, *args, **kwargs):
        return []

    monkeypatch.setattr(ChaoxingClient, "login", _login)
    monkeypatch.setattr(ChaoxingClient, "cookies", _cookies)
    monkeypatch.setattr(ChaoxingClient, "get_used_times", _get_used_times)
    monkeypatch.setattr(ChaoxingClient, "reserve_list", _reserve_list)


class RenderServer:
    """线程内 uvicorn 承载种子应用；随机端口，用后按序关闭。"""

    def __init__(self, tmp_path: Path, monkeypatch):
        _stub_chaoxing(monkeypatch)
        cfg = load_config("config.test.yaml")
        cfg.runtime.db_path = str(tmp_path / "mobile.db")
        cfg.runtime.web_token = TOKEN
        self.store = StateStore(cfg.runtime.db_path)
        asyncio.run(self.store.init())
        self._seed()
        # 占用查询同样打桩，保证/dashboard 链路零外呼
        import seatbot.web.routes as R

        async def fake_fetch(request, store_, day, seat_nums, fresh=False):
            return [], None

        monkeypatch.setattr(R, "_fetch_others_occupied_cached", fake_fetch)

        sched = Scheduler(cfg, self.store)
        app = make_app(cfg, self.store, sched)
        config = uvicorn.Config(app, host="127.0.0.1", port=0,
                                log_level="error", lifespan="off")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 15
        while not getattr(self.server, "started", False):
            if time.time() > deadline:
                raise RuntimeError("隔离渲染服务启动超时")
            time.sleep(0.05)
        sock = self.server.servers[0].sockets[0]
        self.base_url = f"http://127.0.0.1:{sock.getsockname()[1]}"

    def _seed(self) -> None:
        """中性种子（AGENTS.md 第九条）：2 座位 + 2 账号，无真实实例字面量。"""
        from seatbot.models import Account

        async def seed():
            await self.store.add_target_seat("021", label="靠窗主座")
            await self.store.add_target_seat("022", label="安静角")
            await self.store.upsert_account(Account(
                id="张三", phone="13800000001", password="x", slots=[],
                seat_slots={"021": {"mon": ["09:00-11:00"]}}))
            await self.store.upsert_account(Account(
                id="李四", phone="13800000002", password="x", slots=[],
                seat_slots={"022": {"mon": ["10:00-12:00"]}}))

        asyncio.run(seed())

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        asyncio.run(self.store.close())


def _assert_pages(base_url: str, width: int, height: int) -> None:
    """按指定视口渲染全部页面，断言页级零溢出 + 横滚容器可达。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            ctx = browser.new_context(viewport={"width": width, "height": height},
                                      device_scale_factor=1, locale="zh-CN")
            page = ctx.new_page()
            offenders = []
            for path in PAGES:
                page.goto(f"{base_url}{path}?token={TOKEN}", wait_until="networkidle")
                # Alpine 初始化 + 首屏 boot-overlay 揭幕
                page.wait_for_timeout(2500)
                sw = page.evaluate("document.documentElement.scrollWidth")
                iw = page.evaluate("window.innerWidth")
                if sw > iw + 1:
                    offenders.append((path, f"页面横向溢出 sw={sw} iw={iw}"))
                for bad in page.evaluate(REACHABILITY_JS):
                    offenders.append((path, f"横滚容器被裁剪不可达: {bad}"))
            assert not offenders, f"@{width} 视口验收失败：{offenders}"
            ctx.close()
        finally:
            browser.close()


def test_mobile_no_overflow_at_375(tmp_path, monkeypatch):
    """375 viewport：12 页页级零溢出 + 横滚容器可达。"""
    srv = RenderServer(tmp_path, monkeypatch)
    try:
        _assert_pages(srv.base_url, 375, 800)
    finally:
        srv.close()


def test_mobile_no_overflow_at_320(tmp_path, monkeypatch):
    """320 viewport（极窄屏）：12 页页级零溢出 + 横滚容器可达。"""
    srv = RenderServer(tmp_path, monkeypatch)
    try:
        _assert_pages(srv.base_url, 320, 700)
    finally:
        srv.close()


def test_desktop_no_regression_at_1280(tmp_path, monkeypatch):
    """1280 desktop：12 页页级零溢出（CSS 改动防误伤桌面）。"""
    srv = RenderServer(tmp_path, monkeypatch)
    try:
        _assert_pages(srv.base_url, 1280, 800)
    finally:
        srv.close()
