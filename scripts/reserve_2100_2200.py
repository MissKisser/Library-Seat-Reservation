"""Reserve seat 84, 21:00-22:00, leave it ACTIVE (no cancel).
Then check we can hit /sign and /leave with the same login session."""
from __future__ import annotations

import asyncio
import json
import sys
from urllib.parse import parse_qs

from playwright.async_api import async_playwright
from seatbot.client import ChaoxingClient

PHONE = "151XXXX0087"
PASSWORD = "[已清除]"
ROOM_ID = 11692
SEAT_NUM = "84"


async def main() -> int:
    client = ChaoxingClient()
    try:
        # login headless to get cookies
        await client.login(PHONE, PASSWORD)
        cookies = client.cookies()
        print(f"login ok; {len(cookies)} cookies")

        # share cookies into a Playwright browser
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
            await ctx.add_cookies([
                {"name": k, "value": v, "url": "https://office.chaoxing.com"}
                for k, v in cookies.items()
                if k not in ("route",)  # skip passport2-only cookies
            ])
            page = await ctx.new_page()

            captured: dict = {}
            async def on_req(req):
                if "/data/apps/seat/submit" in req.url and req.method == "POST":
                    body = req.post_data or ""
                    p = parse_qs(body)
                    captured["enc"] = p.get("enc", [""])[0]
                    captured["body"] = body
            page.on("request", on_req)

            print("navigate seat detail…")
            await page.goto(
                f"https://office.chaoxing.com/front/apps/seat/code"
                f"?id={ROOM_ID}&seatNum={SEAT_NUM}"
            )
            await page.wait_for_timeout(2500)

            print("click 21:00-21:30…")
            clicked1 = False
            for lbl in ("21:00-21:30", "21:00"):
                try:
                    await page.locator(f"li:has-text('{lbl}')").first.click(timeout=2000)
                    print(f"  clicked {lbl}")
                    clicked1 = True
                    break
                except Exception:
                    continue
            if not clicked1:
                print("  ERROR: no 21:00 cell")

            await page.wait_for_timeout(300)
            print("click 21:30-22:00…")
            try:
                await page.locator("li:has-text('21:30-22:00')").first.click(timeout=2000)
                print("  clicked 21:30-22:00")
            except Exception as e:
                print(f"  WARN: {e}")
            await page.wait_for_timeout(500)

            print("click 开始使用…")
            try:
                await page.locator("p.can_submit:has-text('开始使用')").first.click(timeout=5000)
            except Exception as e:
                print(f"  ERR: {e}")
            await page.wait_for_timeout(5000)

            if "enc" in captured:
                print(f"enc captured: {captured['enc']}")
                print(f"body: {captured['body'][:300]}")
            else:
                print("no enc captured")

            # check reserve/info
            raw = await page.evaluate("""
              async () => {
                const r = await fetch(
                  'https://office.chaoxing.com/data/apps/seat/reserve/info?id=11692&seatNum=84',
                  {credentials: 'include'}
                );
                return await r.text();
              }
            """)
            data = json.loads(raw)
            sr = data.get("data", {}).get("seatReserve", {})
            if sr:
                print("NEW active reservation:")
                print(f"  id={sr.get('id')} seat={sr.get('seatNum')} "
                      f"start={sr.get('startTime')} end={sr.get('endTime')}")
                print(f"  status={sr.get('status')} duration={sr.get('duration')}h")
                print(f"  uid={sr.get('uid')}")
                # do NOT cancel
            else:
                print("no active reservation")
                print(f"raw: {raw[:300]}")

            await browser.close()
    finally:
        await client.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
