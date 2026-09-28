"""桌面端页级视觉守卫（与 test_mobile_no_overflow.py 对称的桌面档）。

- 无浏览器环境：整文件 pytest.skip
- 有浏览器：线程内 uvicorn 承载种子应用（port=0 由内核分配随机端口，
  绝不触碰固定端口/生产实例），Playwright 以 1280 / 1440 两档真实渲染，
  断言信息层级、密度与文案一致性，避免排版改动退回「大片空白卡片」。

全程零真实账号操作：隔离临时库 + 中性种子 + ChaoxingClient 全网络
方法打桩，页面链路不产生任何真实外呼。
"""
import asyncio
import json
import re
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="需要 playwright 才能渲染桌面端 CSS")
pytest.importorskip("uvicorn", reason="需要 uvicorn 承载隔离渲染服务")

import uvicorn  # noqa: E402

from seatbot.config import load_config  # noqa: E402
from seatbot.scheduler import Scheduler  # noqa: E402
from seatbot.store import StateStore  # noqa: E402
from seatbot.web.app import make_app  # noqa: E402

TOKEN = "desktop-smoke-token"

PAGES = [
    "/", "/targets", "/bindings", "/accounts", "/accounts/new", "/tasks",
    "/hosting", "/manual", "/reservations", "/logs", "/settings", "/audit",
]

# 任务状态机内部键；这些字符串不得以可见文本形式出现在页面上
RAW_STATUS_KEYS = [
    "pending", "submitting", "leaving", "complete", "signed",
    "active", "failed", "ready",
]

# 页面可交互文本采集：排除 <script>/<style>，避免源码字符串误判
VISIBLE_TEXT_JS = """() => {
  const clone = document.body.cloneNode(true);
  clone.querySelectorAll('script, style, template, [x-cloak]').forEach(n => n.remove());
  return clone.innerText || '';
}"""

OVERFLOW_JS = """() => {
  const de = document.documentElement;
  return { sw: de.scrollWidth, iw: window.innerWidth };
}"""

DASHBOARD_GEOMETRY_JS = """() => {
  const h1 = document.querySelector('h1.page-title');
  const stat = Array.from(document.querySelectorAll('.stat-card'))
    .map(e => Math.round(e.getBoundingClientRect().height));
  const cells = Array.from(document.querySelectorAll('.gantt-cell'))
    .map(e => e.getBoundingClientRect().width);
  // 底部信息区为两列栅格，两卡底边应对齐（原先右栏比左栏高 168px 的参差即此处回归）
  const rail = document.querySelector('.dashboard-rail');
  const railBottoms = rail
    ? Array.from(rail.children).map(c => Math.round(c.getBoundingClientRect().bottom))
    : [];
  return {
    hasPageTitle: !!h1,
    statHeights: stat,
    avgCellWidth: cells.length ? cells.reduce((a, b) => a + b, 0) / cells.length : 0,
    railBottoms: railBottoms,
  };
}"""

TASK_ROW_JS = """() => {
  const rows = Array.from(document.querySelectorAll('.record-row'));
  return rows.map(e => {
    const b = e.getBoundingClientRect();
    return { h: Math.round(b.height), text: (e.innerText || '').trim().length };
  });
}"""

DATE_INPUT_JS = """() => document.querySelectorAll('input[type="date"]').length"""

CRUMB_JS = """() => {
  const sep = document.querySelector('.header-crumb-sep');
  const title = document.querySelector('.header-title');
  const d = el => (el ? getComputedStyle(el).display : null);
  return { sep: d(sep), title: d(title) };
}"""

CONTENT_WIDTH_JS = """() => {
  const content = document.querySelector('.app-content');
  const root = document.querySelector('.app-content > div');
  const w = el => (el ? Math.round(el.getBoundingClientRect().width) : 0);
  return { content: w(content), root: w(root) };
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
        cfg.runtime.db_path = str(tmp_path / "desktop.db")
        cfg.runtime.web_token = TOKEN
        self.store = StateStore(cfg.runtime.db_path)
        asyncio.run(self.store.init())
        self._seed()
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
        """中性种子（AGENTS.md 第九条）：2 座位 + 2 账号 + 当日若干任务，无真实实例字面量。"""
        from datetime import date, time, timedelta

        from seatbot.models import Account, Task, TaskStatus

        async def seed():
            await self.store.add_target_seat("021", label="靠窗主座")
            await self.store.add_target_seat("022", label="安静角")
            await self.store.upsert_account(Account(
                id="张三", phone="13800000001", password="x", slots=[],
                seat_slots={"021": {"mon": ["09:00-11:00", "11:00-13:00"]}}))
            await self.store.upsert_account(Account(
                id="李四", phone="13800000002", password="x", slots=[],
                seat_slots={"022": {"mon": ["10:00-12:00"]}}))
            today = date.today()
            for i in range(6):
                await self.store.add_task(Task(
                    id=None,
                    account_id="张三" if i % 2 else "李四",
                    day=today,
                    start_time=time(9 + i, 0),
                    end_time=time(11 + i, 0),
                    seat_num="021" if i % 2 else "022",
                    status=TaskStatus.PENDING,
                ))

        asyncio.run(seed())

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        asyncio.run(self.store.close())


def _open_browser():
    from playwright.sync_api import sync_playwright

    return sync_playwright()


def test_desktop_pages_zero_overflow_and_density(tmp_path, monkeypatch):
    """桌面两档：12 页零溢出 + 层级/密度/文案一致性守卫。"""
    from playwright.sync_api import sync_playwright

    srv = RenderServer(tmp_path, monkeypatch)
    offenders = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                for width in (1280, 1440):
                    ctx = browser.new_context(
                        viewport={"width": width, "height": 900},
                        device_scale_factor=1, locale="zh-CN")
                    page = ctx.new_page()
                    for path in PAGES:
                        page.goto(f"{srv.base_url}{path}?token={TOKEN}", wait_until="networkidle")
                        page.wait_for_timeout(2500)

                        ov = page.evaluate(OVERFLOW_JS)
                        if ov["sw"] > ov["iw"] + 1:
                            offenders.append(
                                f"@{width} {path} 横向溢出 sw={ov['sw']} iw={ov['iw']}")

                        text = page.evaluate(VISIBLE_TEXT_JS)
                        if path in ("/tasks", "/logs"):
                            for key in RAW_STATUS_KEYS:
                                if re.search(rf"(?<![A-Za-z0-9_]){key}(?![A-Za-z0-9_])", text):
                                    offenders.append(
                                        f"@{width} {path} 状态裸键外露：{key}")

                        crumb = page.evaluate(CRUMB_JS)
                        if width < 1024 and not (crumb["sep"] == "none" and crumb["title"] == "none"):
                            offenders.append(
                                f"@{width} {path} 面包屑未同隐 {json.dumps(crumb, ensure_ascii=False)}")

                        if path == "/":
                            g = page.evaluate(DASHBOARD_GEOMETRY_JS)
                            if not g["hasPageTitle"]:
                                offenders.append(f"@{width} / 缺少 h1.page-title 层级锚点")
                            for h in g["statHeights"]:
                                if h > 90:
                                    offenders.append(
                                        f"@{width} / 指标卡过高 {h}px（内容仅约 40px）")
                            if g["avgCellWidth"] < 32:
                                offenders.append(
                                    f"@{width} / 甘特格均值过窄 {g['avgCellWidth']:.1f}px")
                            if len(g["railBottoms"]) > 1:
                                spread = max(g["railBottoms"]) - min(g["railBottoms"])
                                if spread > 2:
                                    offenders.append(
                                        f"@{width} / 信息区两卡底边参差 {spread}px "
                                        f"{g['railBottoms']}")

                        if path == "/tasks":
                            for row in page.evaluate(TASK_ROW_JS):
                                if row["h"] > 56:
                                    offenders.append(
                                        f"@{width} /tasks 任务行过高 {row['h']}px")
                                if row["text"] < 12:
                                    offenders.append(
                                        f"@{width} /tasks 任务行信息过少 {row['text']} 字")

                        if path == "/logs":
                            n = page.evaluate(DATE_INPUT_JS)
                            if n > 1:
                                offenders.append(
                                    f"@{width} /logs 日期控件重复 {n} 个")

                        if path == "/settings":
                            w = page.evaluate(CONTENT_WIDTH_JS)
                            if w["content"] and w["root"] < w["content"] * 0.85:
                                offenders.append(
                                    f"@{width} /settings 内容宽度 {w['root']} "
                                    f"仅占容器 {w['content']} 的 "
                                    f"{w['root'] / w['content']:.0%}")
                    ctx.close()
            finally:
                browser.close()
    finally:
        srv.close()

    assert not offenders, "桌面视觉守卫失败：\n" + "\n".join(offenders)


def test_status_label_dictionaries_stay_in_sync():
    """前端字典与 Jinja 宏的状态键集合必须一致，防止中文标签两侧漂移。"""
    root = Path(__file__).resolve().parents[1]
    js = (root / "seatbot" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    macro = (root / "seatbot" / "web" / "templates" / "_macros" / "badge.html").read_text(encoding="utf-8")

    block = js.split("const STATUS_LABELS = {", 1)[1].split("};", 1)[0]
    js_keys = set(re.findall(r"^\s*([a-z_]+):", block, re.M))

    macro_block = macro.split("_status_map = {", 1)[1].split("}", 1)[0]
    macro_keys = set(re.findall(r"'([a-z_]+)':", macro_block))

    assert js_keys, "未能从 app.js 解析出状态字典"
    assert js_keys == macro_keys, (
        f"状态字典键集合不一致：仅 app.js 有 {sorted(js_keys - macro_keys)}，"
        f"仅 badge.html 有 {sorted(macro_keys - js_keys)}"
    )
