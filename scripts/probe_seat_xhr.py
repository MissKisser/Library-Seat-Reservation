"""座位页 XHR 只读诊断: 定位页面日期/网格数据的来源接口 (绝不提交)。

用法:
  .venv/Scripts/python.exe scripts/probe_seat_xhr.py <account_id> <seat_num> <day>

输出: 页面加载期间的全部请求 URL 清单 + 响应体中含目标日期串的接口明细。
⚠️ 纯只读导航, 不点击不提交。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.async_api import async_playwright  # noqa: E402

from seatbot.config import load_config  # noqa: E402
from seatbot.utils.ua import random_ua  # noqa: E402
from session_cache import authenticated_client  # noqa: E402

TODAY = "2026-08-26"


async def main() -> int:
    acct, seat, day = sys.argv[1], sys.argv[2], sys.argv[3]
    cfg = load_config("config.yaml")

    import sqlite3
    con = sqlite3.connect(f"file:{cfg.runtime.db_path}?mode=ro", uri=True)
    phone, pw = con.execute(
        "SELECT phone, password FROM accounts WHERE id=?", (acct,)
    ).fetchone()
    con.close()

    async def probe(client):
        await client.get_used_times(cfg.library.room_id, "104", TODAY)
        return True

    client, src = await authenticated_client(acct, phone, pw, probe)
    print(f"[xhr] session={src} cookies={len(client.cookies())}")

    hits: list[tuple[str, str]] = []
    reqs: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900}, user_agent=random_ua(),
        )
        await ctx.add_cookies([
            {"name": k, "value": v, "url": client.OFFICE_BASE}
            for k, v in client.cookies().items()
        ])
        page = await ctx.new_page()

        def on_request(req):
            u = req.url
            if "chaoxing.com" in u and not any(
                u.endswith(x) for x in (".png", ".jpg", ".gif", ".css", ".ico")
            ):
                reqs.append(f"{req.method} {u[:160]}")

        async def on_response(resp):
            u = resp.url
            if "chaoxing.com" not in u:
                return
            ct = (resp.headers or {}).get("content-type", "")
            if "json" not in ct and "javascript" not in ct and "html" not in ct:
                return
            try:
                body = await resp.text()
            except Exception:
                return
            if len(body) > 400_000:
                return
            for needle in (TODAY, day):
                if needle in body:
                    hits.append((resp.request.method, f"{u[:140]} :: contains {needle} "
                                                       f"(ct={ct.split(';')[0]}, len={len(body)})"))
                    break

        page.on("request", on_request)
        page.on("response", on_response)

        url = (f"{client.OFFICE_BASE}/front/apps/seat/code"
               f"?id={cfg.library.room_id}&seatNum={seat}&day={day}")
        await page.goto(url)
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1500)

        print(f"[xhr] == 请求清单 ({len(reqs)}) ==")
        for r in reqs:
            print("   ", r)
        print(f"[xhr] == 含日期串的响应 ({len(hits)}) ==")
        for m, h in hits:
            print(f"    [{m}] {h}")
        await browser.close()
    await client.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
