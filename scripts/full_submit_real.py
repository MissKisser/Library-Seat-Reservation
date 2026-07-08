"""Try a real reservation end-to-end in one Playwright session:
   login → seat detail → select 21:00-21:30 → click 开始使用
   → wait for response → parse it → optionally cancel.

Headless. We capture both: (a) the captured enc captured from the
outgoing /submit POST, and (b) the JSON response from the server.

This shows that we can actually drive the *full* reservation flow in a
real headless browser, with no human interaction at all.
"""
from __future__ import annotations

import asyncio
import json
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

        captured_req: dict[str, str] = {}
        submit_resp: dict = {}

        async def on_request(req):
            if "/data/apps/seat/submit" in req.url and req.method == "POST":
                body = req.post_data or ""
                parsed = parse_qs(body)
                captured_req["raw"] = body
                captured_req["enc"] = parsed.get("enc", [""])[0]
                captured_req["wyToken"] = parsed.get("wyToken", [""])[0]
                for k in ("roomId", "startTime", "endTime", "seatNum", "day", "captcha"):
                    captured_req[k] = parsed.get(k, [""])[0]

        async def on_response(resp):
            if "/data/apps/seat/submit" in resp.url and resp.request.method == "POST":
                try:
                    txt = await resp.text()
                    submit_resp["url"] = resp.url
                    submit_resp["status"] = resp.status
                    submit_resp["body"] = txt[:3000]
                    try:
                        submit_resp["json"] = json.loads(txt)
                    except Exception:
                        pass
                except Exception as e:
                    submit_resp["err"] = str(e)

        page.on("request", on_request)
        page.on("response", on_response)

        print("[1/5] login 151XXXX0087...")
        await page.goto("https://passport2.chaoxing.com/login?newversion=true")
        await page.locator("input[placeholder*='手机号']").fill(PHONE)
        await page.locator("input[placeholder*='密码']").fill(PASSWORD)
        await page.wait_for_timeout(300)
        await page.get_by_role("button", name="登录").click()
        for i in range(120):
            cs = await ctx.cookies()
            if any(c["name"] in ("_uid", "vc3") for c in cs):
                print(f"  login ok ({i*250}ms)")
                break
            await page.wait_for_timeout(250)

        print(f"[2/5] navigate to seat detail (id={ROOM_ID}, seat={SEAT_NUM})...")
        await page.goto(
            f"https://office.chaoxing.com/front/apps/seat/code"
            f"?id={ROOM_ID}&seatNum={SEAT_NUM}",
        )
        await page.wait_for_load_state("networkidle")
        text = await page.locator("body").inner_text()
        if "本人正在使用中" in text:
            print("  WARN: another reservation is already active for this user")
        if "目前无人使用" in text:
            print("  ok: 84 is free")

        print("[3/5] click '21:00-21:30' slot...")
        # try several label formats
        clicked = False
        for label in ("21:00-21:30", "21:00", "21:00-"):
            try:
                await page.locator(f"li:has-text('{label}')").first.click(timeout=2000)
                print(f"  clicked {label}")
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            print("  ERROR: no 21:00 slot clickable")
            print("    visible li texts (first 40):")
            items = await page.locator("li").all_inner_texts()
            for i, t in enumerate(items[:40]):
                print(f"      [{i}] {t!r}")
            await browser.close()
            return 1

        await page.wait_for_timeout(500)

        print("[4/5] click '开始使用' → wait for /submit...")
        try:
            await page.locator("p.can_submit:has-text('开始使用')").first.click(timeout=5000)
        except Exception as e:
            print(f"  click error: {e}")

        # wait for response
        for i in range(60):  # 30s
            if "json" in submit_resp or "body" in submit_resp:
                print(f"  /submit response received ({i*500}ms)")
                break
            await page.wait_for_timeout(500)
        else:
            print("  WARN: no /submit response within 30s")

        # wait for navigation / state change
        await page.wait_for_timeout(2000)

        print("[5/5] inspect result + cancel if reserved...")
        if "json" in submit_resp:
            data = submit_resp["json"]
            print(f"  /submit status={submit_resp['status']} body={json.dumps(data, ensure_ascii=False)[:300]}")
            if data.get("success"):
                rid = (data.get("data") or {}).get("seatReserve", {}).get("id")
                print(f"  reservation #{rid} created — cancelling...")
                # cancel via /cancel API by reusing cookies
                import httpx
                jar = httpx.Cookies()
                for c in await ctx.cookies():
                    jar.set(c["name"], c["value"],
                            domain=c["domain"] if c["domain"].startswith(".") else "." + c["domain"],
                            path=c["path"])
                async with httpx.AsyncClient(cookies=jar, timeout=15) as h:
                    r = await h.post(
                        "https://office.chaoxing.com/data/apps/seat/cancel",
                        data={"id": str(rid)},
                        headers={"Referer": "https://office.chaoxing.com/"},
                    )
                    print(f"  cancel: HTTP {r.status_code} body={r.text[:200]}")
                return 0
            else:
                print(f"  submit FAILED: {data.get('msg')}")
                return 2
        else:
            print("  no /submit response captured")
            if captured_req:
                print("  request was intercepted though:")
                for k, v in captured_req.items():
                    print(f"    {k}: {v[:200] if isinstance(v, str) else v}")
            return 3

        await browser.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
