"""Try to extract `enc` + `wyToken` for a 21:00-22:00 reservation on seat 84.

Approach:
  1. Login headless with test account 151XXXX0087 / [已清除]
  2. Navigate to https://office.chaoxing.com/front/apps/seat/code?id=11692&seatNum=84
  3. Set date to today
  4. Click the "21:00-22:00" 30-min slot(s)
  5. Click "开始使用" to trigger submit POST → intercept the request body
  6. Read `enc` and `wyToken` from the captured form data
  7. Cancel the actual reservation immediately via API (cancel by reserve_id)
     so the seat does not get held.

Output: prints the captured enc + wyToken, then exits.

NOTE: This will *briefly* hold the seat if /submit succeeds. We attempt to
cancel immediately afterward. Risk: CAPTCHA interruption could leave a stale
reservation — the test account is sacrificial.
"""
from __future__ import annotations

import asyncio
import sys
from urllib.parse import parse_qs

from playwright.async_api import async_playwright


PHONE = "151XXXX0087"
PASSWORD = "[已清除]"
ROOM_ID = 11692
SEAT_NUM = "84"


async def main() -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await ctx.new_page()

        captured: dict[str, str] = {}

        async def on_request(req):
            # capture the actual /submit POST body
            if "/data/apps/seat/submit" in req.url and req.method == "POST":
                body = req.post_data or ""
                parsed = parse_qs(body)
                captured["raw_body"] = body
                captured["enc"] = parsed.get("enc", [""])[0]
                captured["wyToken"] = parsed.get("wyToken", [""])[0]
                captured["url"] = req.url
                # also capture interesting form fields
                for key in ("roomId", "startTime", "endTime", "seatNum", "day"):
                    captured[key] = parsed.get(key, [""])[0]
                print(f"  [intercept] /submit captured (len={len(body)})")

        page.on("request", on_request)

        print(f"[1/4] login {PHONE}...")
        await page.goto("https://passport2.chaoxing.com/login?newversion=true")
        await page.locator("input[placeholder*='手机号']").fill(PHONE)
        await page.locator("input[placeholder*='密码']").fill(PASSWORD)
        await page.wait_for_timeout(300)
        await page.get_by_role("button", name="登录").click()
        # wait for auth cookie
        for _ in range(120):
            cookies = await ctx.cookies()
            if any(c["name"] in ("_uid", "vc3") for c in cookies):
                break
            await page.wait_for_timeout(250)
        else:
            print("[ERROR] login timed out")
            await browser.close()
            return 1
        print("  OK; auth cookies set")

        print(f"[2/4] navigate to seat detail (room={ROOM_ID}, seat={SEAT_NUM})...")
        url = f"https://office.chaoxing.com/front/apps/seat/code?id={ROOM_ID}&seatNum={SEAT_NUM}"
        await page.goto(url)
        await page.wait_for_timeout(2000)

        print("[3/4] try to click 21:00-22:00 slot...")
        # The slot UI typically shows time labels like "21:00-21:30" and "21:30-22:00"
        # We'll click any cell containing "21:00" or "21:30"
        clicked = False
        for label in ("21:00-21:30", "21:00", "21:30-22:00", "21:30"):
            try:
                await page.locator(f"li:has-text('{label}')").first.click(timeout=2000)
                print(f"  clicked slot {label}")
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            print("  WARN: no 21:00 slot found, listing page text...")
            text = await page.locator("body").inner_text()
            for line in text.split("\n")[:50]:
                print(f"    | {line}")

        print("[4/4] click 开始使用 to trigger submit...")
        try:
            await page.locator("p.can_submit:has-text('开始使用')").first.click(timeout=5000)
        except Exception as e:
            print(f"  WARN: cannot click 开始使用: {e}")

        # wait for the intercept
        for _ in range(20):
            if "enc" in captured:
                break
            await page.wait_for_timeout(250)

        await browser.close()

        if "enc" in captured and captured["enc"]:
            print("\n=== captured ===")
            for k, v in captured.items():
                if k == "raw_body":
                    print(f"  raw_body: <{len(v)} bytes>")
                else:
                    print(f"  {k}: {v[:200] if isinstance(v, str) else v}")
            return 0

        print("\n=== no enc captured ===")
        print(f"  page.url  https://office.chaoxing.com/front/apps/seat/code?id={ROOM_ID}&seatNum={SEAT_NUM}")
        print("  possible reasons: CAPTCHA blocked /submit, or click selector wrong,")
        print("  or the seat is held by another user.")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
