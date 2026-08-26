"""座位页只读诊断: 验证 day 参数与多格选区行为 (绝不点击"开始使用")。

用法:
  .venv/Scripts/python.exe scripts/debug_seat_page.py <account_id> <seat_num> <day>

输出: 页面实际展示的日期、格子 DOM 摘要、逐格点击后的选区指示, + 截图。
⚠️ 只加载页面与客户端点选格子, 不点开始使用, 不产生预约。
"""
import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.async_api import async_playwright  # noqa: E402

from seatbot.config import load_config  # noqa: E402
from seatbot.utils.ua import random_ua  # noqa: E402
from session_cache import authenticated_client  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "logs_debug"


async def probe(client):
    cfg = load_config("config.yaml")
    await client.get_used_times(cfg.library.room_id, "104", date.today().isoformat())
    return True


async def main() -> int:
    acct, seat, day = sys.argv[1], sys.argv[2], sys.argv[3]
    cfg = load_config("config.yaml")

    import sqlite3
    con = sqlite3.connect(f"file:{cfg.runtime.db_path}?mode=ro", uri=True)
    phone, pw = con.execute(
        "SELECT phone, password FROM accounts WHERE id=?", (acct,)
    ).fetchone()
    con.close()

    client, src = await authenticated_client(acct, phone, pw, probe)
    print(f"[diag] session={src}")
    OUT.mkdir(exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900},
                                        user_agent=random_ua())
        await ctx.add_cookies([
            {"name": k, "value": v, "url": client.OFFICE_BASE}
            for k, v in client.cookies().items()
        ])
        page = await ctx.new_page()
        url = (f"{client.OFFICE_BASE}/front/apps/seat/code"
               f"?id={cfg.library.room_id}&seatNum={seat}&day={day}")
        await page.goto(url)
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(800)

        print(f"[diag] 请求 day={day}")
        print(f"[diag] 落地 URL: {page.url}")
        print(f"[diag] 页面标题: {await page.title()}")

        # 页面上可见的日期文本 (判断页面到底显示哪天)
        body = await page.inner_text("body")
        for line in body.splitlines():
            line = line.strip()
            if day in line or "今天" in line or "明天" in line or "08-2" in line or "08/2" in line:
                print(f"[diag] 日期线索: {line[:80]}")
                break

        # 格子 DOM 摘要: 找出含 15:00 / 16:00 等 label 的 li
        cells = page.locator("li")
        n = await cells.count()
        print(f"[diag] li 总数: {n}")
        shown = 0
        for i in range(min(n, 400)):
            txt = (await cells.nth(i).inner_text()).strip().replace("\n", " ")
            if any(k in txt for k in ("15:00", "15:30", "16:00", "16:30", "17:00")):
                cls = await cells.nth(i).get_attribute("class")
                print(f"[diag]   li[{i}] class={cls!r} text={txt[:40]!r}")
                shown += 1
                if shown >= 10:
                    break

        # 逐格点击 (纯客户端选区, 不提交)
        for label in ("15:00-15:30", "15:30-16:00", "16:00-16:30", "16:30-17:00"):
            try:
                await page.locator(f"li:has-text('{label}')").first.click(timeout=2000)
                await page.wait_for_timeout(300)
                sel = await page.inner_text("body")
                hint = ""
                for line in sel.splitlines():
                    line = line.strip()
                    if ("已选" in line or "选择" in line or "开始使用" in line) and ":" in line:
                        hint = line[:80]
                        break
                print(f"[diag] 点击 {label} → 指示: {hint or '(未找到选区指示文本)'}")
            except Exception as e:
                print(f"[diag] 点击 {label} 失败: {type(e).__name__}")

        await page.screenshot(path=str(OUT / "seat_page_diag.png"), full_page=True)
        print(f"[diag] 截图: {OUT / 'seat_page_diag.png'}")
        await browser.close()
    await client.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
