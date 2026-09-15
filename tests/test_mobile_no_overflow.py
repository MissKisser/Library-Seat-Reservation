"""移动端页级零溢出守卫（spec §7 完成定义）。

- 无浏览器环境：整文件 pytest.skip
- 有浏览器：以 375 / 320 两档 viewport 真实渲染 12 页，
  逐页断言 documentElement.scrollWidth ≤ innerWidth + 1
- 桌面（1280）作为无回归基线同步验证（防 CSS 改动误伤）

全程零真实账号操作：仅本地 TestClient + 隔离库 + 中性种子，与
test_no_500.py 共享 _client 装配方式；外呼 mock 同。
"""
import asyncio
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="需要 playwright 才能渲染移动端 CSS")

from fastapi.testclient import TestClient  # noqa: E402

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


def _client(tmp_path: Path, monkeypatch) -> tuple[TestClient, StateStore]:
    cfg = load_config("config.test.yaml")
    cfg.runtime.db_path = str(tmp_path / "mobile.db")
    cfg.runtime.web_token = TOKEN
    store = StateStore(cfg.runtime.db_path)
    asyncio.run(store.init())

    async def seed():
        await store.add_target_seat("021", label="座位 030")
        await store.add_target_seat("022", label="座位 031")
        from seatbot.models import Account
        # 中性种子（AGENTS.md §9）：张三 / 李四，绑定示例
        await store.upsert_account(Account(id="张三", phone="13800000001", password="x",
                                           slots=[], seat_slots={"021": {"mon": ["09:00-11:00"]}}))
        await store.upsert_account(Account(id="李四", phone="13800000002", password="x",
                                           slots=[], seat_slots={"022": {"mon": ["10:00-12:00"]}}))

    asyncio.run(seed())
    sched = Scheduler(cfg, store)
    app = make_app(cfg, store, sched)
    # 占用查询 mock：零真实请求
    import seatbot.web.routes as R

    async def fake_fetch(request, store_, day, seat_nums, fresh=False):
        return [], None

    monkeypatch.setattr(R, "_fetch_others_occupied_cached", fake_fetch)
    return TestClient(app, base_url="http://127.0.0.1:8080"), store


def _render(client: TestClient, path: str) -> str:
    sep = "&" if "?" in path else "?"
    r = client.get(f"{path}{sep}token={TOKEN}",
                   headers={"Host": "127.0.0.1:8080"},
                   follow_redirects=False)
    assert r.status_code == 200, f"GET {path} -> {r.status_code}"
    return r.text


def _overflow_count(page, viewport: dict) -> tuple[int, int]:
    """返回 (scrollWidth, innerWidth)。scrollWidth > innerWidth+1 即视为溢出。"""
    sw = page.evaluate("document.documentElement.scrollWidth")
    iw = page.evaluate("window.innerWidth")
    return sw, iw


def test_mobile_no_overflow_at_375(tmp_path, monkeypatch):
    """375 viewport：12 页全部 sw ≤ iw + 1。"""
    from playwright.sync_api import sync_playwright

    c, store = _client(tmp_path, monkeypatch)
    try:
        # 预热：先 GET / 让 Alpine 加载路径已知
        _ = _render(c, "/")
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                ctx = browser.new_context(viewport={"width": 375, "height": 800},
                                          device_scale_factor=1, locale="zh-CN")
                page = ctx.new_page()
                # 共享会话：登录态只需一次
                page.goto("http://127.0.0.1:8080/?token=" + TOKEN, wait_until="networkidle")
                page.wait_for_timeout(3000)
                offenders = []
                for path in PAGES:
                    page.goto("http://127.0.0.1:8080" + path + "?token=" + TOKEN,
                              wait_until="networkidle")
                    # Alpine 初始化 + 首屏 boot-overlay 揭幕
                    page.wait_for_timeout(2500)
                    sw, iw = _overflow_count(page, {"width": 375})
                    delta = sw - iw
                    if delta > 1:
                        offenders.append((path, sw, iw, delta))
                assert not offenders, (
                    f"@375 viewport 页级溢出：{offenders}；"
                    f"按 §7 验收：12 页 × 375/320 零页级溢出。"
                )
                ctx.close()
            finally:
                browser.close()
    finally:
        asyncio.run(store.close())


def test_mobile_no_overflow_at_320(tmp_path, monkeypatch):
    """320 viewport：极窄屏（iPhone SE 1st 等）12 页全部 sw ≤ iw + 1。"""
    from playwright.sync_api import sync_playwright

    c, store = _client(tmp_path, monkeypatch)
    try:
        _ = _render(c, "/")
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                ctx = browser.new_context(viewport={"width": 320, "height": 700},
                                          device_scale_factor=1, locale="zh-CN")
                page = ctx.new_page()
                page.goto("http://127.0.0.1:8080/?token=" + TOKEN, wait_until="networkidle")
                page.wait_for_timeout(3000)
                offenders = []
                for path in PAGES:
                    page.goto("http://127.0.0.1:8080" + path + "?token=" + TOKEN,
                              wait_until="networkidle")
                    page.wait_for_timeout(2500)
                    sw, iw = _overflow_count(page, {"width": 320})
                    delta = sw - iw
                    if delta > 1:
                        offenders.append((path, sw, iw, delta))
                assert not offenders, (
                    f"@320 viewport 页级溢出：{offenders}；"
                    f"按 §7 验收：12 页 × 375/320 零页级溢出。"
                )
                ctx.close()
            finally:
                browser.close()
    finally:
        asyncio.run(store.close())


def test_desktop_no_regression_at_1280(tmp_path, monkeypatch):
    """1280 desktop：12 页全部 sw ≤ iw + 1（CSS 改动防误伤桌面）。"""
    from playwright.sync_api import sync_playwright

    c, store = _client(tmp_path, monkeypatch)
    try:
        _ = _render(c, "/")
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                ctx = browser.new_context(viewport={"width": 1280, "height": 800},
                                          device_scale_factor=1, locale="zh-CN")
                page = ctx.new_page()
                page.goto("http://127.0.0.1:8080/?token=" + TOKEN, wait_until="networkidle")
                page.wait_for_timeout(3000)
                offenders = []
                for path in PAGES:
                    page.goto("http://127.0.0.1:8080" + path + "?token=" + TOKEN,
                              wait_until="networkidle")
                    page.wait_for_timeout(1500)
                    sw, iw = _overflow_count(page, {"width": 1280})
                    delta = sw - iw
                    if delta > 1:
                        offenders.append((path, sw, iw, delta))
                assert not offenders, (
                    f"@1280 viewport 页级溢出（桌面零回归失败）：{offenders}。"
                )
                ctx.close()
            finally:
                browser.close()
    finally:
        asyncio.run(store.close())
