"""Interactive browser-login helper.

Opens a *headed* Chromium window at the real Chaoxing login page so a human can
solve the CAPTCHA / YiDun number validator. Once the user lands on
office.chaoxing.com authenticated (i.e. _uid + vc3 cookies exist), the script:

1. Saves the cookie jar to ./browser_cookies.json
2. Tries to also probe a real /submit POST against room=11692 seat=84 with a
   configured guard account — so we can see whether the headless submit path
   works *after* a real human has manually passed validation on this IP.

Usage (from project root, with venv active):
    python scripts/browser_login.py

The script blocks on stdin when it's done so you can keep the cookies alive
in the browser while you click around / test the reservation UI manually.
Press Enter on stdin to exit; the cookies are written before that.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

COOKIES_PATH = Path(__file__).resolve().parent.parent / "browser_cookies.json"
LOGIN_URL = "https://office.chaoxing.com/"
OFFICE_BASE = "https://office.chaoxing.com"
SEAT_DETAIL = (
    "https://office.chaoxing.com/front/apps/seat/code"
    "?id=11692&seatNum=84"
)


async def main() -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright not installed; pip install playwright && playwright install chromium",
              file=sys.stderr)
        return 1

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,               # IMPORTANT: headed so user sees CAPTCHA
            args=["--no-sandbox"],
        )
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()
        await page.goto(LOGIN_URL)
        print(f"[info] Browser opened at {LOGIN_URL}")
        print("[info] Please log in manually with phone 178XXXX3792 + password.")
        print("[info] After login you should land on a logged-in office page")
        print("[info] (you'll see _uid + vc3 cookies in devtools).")
        print()

        # 1. Wait until login cookies appear (poll up to 5 minutes)
        deadline = time.time() + 300
        auth_ok = False
        while time.time() < deadline:
            cookies = await context.cookies()
            names = {c["name"] for c in cookies}
            if "_uid" in names and "vc3" in names:
                auth_ok = True
                break
            await asyncio.sleep(1)

        if not auth_ok:
            print("[error] Timeout (5min) waiting for _uid/vc3 cookies.", file=sys.stderr)
            print("[error] Did you complete the login + CAPTCHA?", file=sys.stderr)
            await browser.close()
            return 2

        print(f"[ok] auth cookies detected. domain cookie count: {len(names)}")

        # 2. Open the seat detail page in the *same* browser session so cookies travel
        print(f"[info] Navigating to seat detail page {SEAT_DETAIL} in same browser...")
        await page.goto(SEAT_DETAIL, wait_until="networkidle")
        print("[info] Seat detail loaded. You can now click around / test booking.")
        print("[info] Try clicking a 30-min cell → '开始使用' to verify CAPTCHA is bypassed.")
        print()
        print("[info] Press Enter here when done (cookies will be saved)...")

        # Save cookies eagerly in case the user Ctrl+C's
        cookies = await context.cookies()
        cookie_dict = {c["name"]: c["value"] for c in cookies}
        COOKIES_PATH.write_text(json.dumps(cookie_dict, indent=2, ensure_ascii=False))
        print(f"[ok] cookies saved to {COOKIES_PATH}")
        print(f"[ok] cookie keys: {list(cookie_dict)}")

        # 3. Block until the human says done
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, sys.stdin.readline)

        # Save cookies again (in case the user clicked around and triggered rotations)
        cookies = await context.cookies()
        cookie_dict = {c["name"]: c["value"] for c in cookies}
        COOKIES_PATH.write_text(json.dumps(cookie_dict, indent=2, ensure_ascii=False))
        print(f"[ok] final cookies saved: {list(cookie_dict)}")

        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
