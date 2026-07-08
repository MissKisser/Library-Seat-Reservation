"""Compute Chaoxing `enc` / `wyToken` via JS exec with Playwright fallback.

The exact algorithm is embedded in YiDunProtector-Web-2.1.4.js. Because that
JS may change, we keep two execution paths:

1. **exec_js (default)**:  Use `PyExecJS` to run a small driver script that
   delegates to the YiDunProtector. The driver script lives in
   `seatbot/enc_driver.js` and is included in the package.
2. **playwright fallback**:  If exec_js fails (e.g. missing node), spin up
   a headless Chromium on the seat detail page and read the form's
   `wyToken` / `enc` from the captured `/submit` request.

For v1 the JS driver is a stub that returns `{"enc":"","wyToken":""}`
(forcing fallback). We wire up real JS in a follow-up.
"""
from __future__ import annotations

import asyncio
import time as _time
from dataclasses import dataclass
from urllib.parse import parse_qs

from seatbot.client import ChaoxingClient


class EncError(Exception):
    pass


@dataclass
class _CacheEntry:
    enc: str
    wy_token: str
    expires_at: float


class EncGenerator:
    def __init__(self, ttl_seconds: int = 60):
        self.ttl_seconds = ttl_seconds
        self._cache: dict[tuple, _CacheEntry] = {}

    def _cache_key(self, room_id, seat_num, day, start, end):
        return (room_id, seat_num, day, start, end)

    async def _compute_js(
        self, room_id: int, seat_num: str, day: str, start: str, end: str
    ) -> dict[str, str]:
        # v1 stub: real JS wired in follow-up. Returning empty values
        # forces the caller to treat this as a failure and fall back.
        return {"enc": "", "wyToken": ""}

    async def _compute_playwright(
        self,
        client: ChaoxingClient,
        room_id: int,
        seat_num: str,
        day: str,
        start: str,
        end: str,
    ) -> dict[str, str]:
        """Headless fallback: open the seat detail page, intercept the
        outgoing /submit request, extract enc/wyToken."""
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise EncError("playwright not installed; cannot fallback") from e

        captured: dict[str, str] = {}

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context()
            # Reuse the logged-in cookies from ChaoxingClient
            await ctx.add_cookies([
                {"name": n, "value": v, "url": client.OFFICE_BASE}
                for n, v in client.cookies().items()
            ])
            page = await ctx.new_page()

            async def on_request(req):
                if "/data/apps/seat/submit" in req.url and req.method == "POST":
                    body = req.post_data or ""
                    parsed = parse_qs(body)
                    captured["enc"] = parsed.get("enc", [""])[0]
                    captured["wyToken"] = parsed.get("wyToken", [""])[0]

            page.on("request", on_request)
            url = (
                f"{client.OFFICE_BASE}/front/apps/seat/code"
                f"?id={room_id}&seatNum={seat_num}"
            )
            await page.goto(url)
            # wait for the seat list to load
            try:
                await page.wait_for_selector("li", timeout=10000)
            except Exception:
                pass
            # Try clicking a 30-min slot matching `start`
            try:
                await page.locator(f"li:has-text('{start}-')").first.click(timeout=3000)
                await page.locator("p.can_submit:has-text('开始使用')").first.click(timeout=3000)
            except Exception:
                pass
            # give network listener time to capture
            await page.wait_for_timeout(2000)
            await browser.close()

        if not captured.get("enc"):
            raise EncError("playwright fallback did not capture enc")
        return {"enc": captured["enc"], "wyToken": captured.get("wyToken", "")}

    async def compute(
        self,
        room_id: int,
        seat_num: str,
        day: str,
        start_time: str,
        end_time: str,
        *,
        client: ChaoxingClient | None = None,
        allow_fallback: bool = True,
    ) -> dict[str, str]:
        key = self._cache_key(room_id, seat_num, day, start_time, end_time)
        now = _time.time()
        if key in self._cache and self._cache[key].expires_at > now:
            return {"enc": self._cache[key].enc, "wyToken": self._cache[key].wy_token}

        # Try JS first; convert any underlying error to EncError so callers
        # only need to handle one exception type.
        try:
            result = await self._compute_js(
                room_id, seat_num, day, start_time, end_time
            )
        except EncError:
            raise
        except Exception as e:
            raise EncError(f"JS exec failed: {e}") from e

        if not result.get("enc") and allow_fallback and client is not None:
            result = await self._compute_playwright(
                client, room_id, seat_num, day, start_time, end_time
            )

        if not result.get("enc"):
            raise EncError("enc generation produced empty result")

        self._cache[key] = _CacheEntry(
            enc=result["enc"],
            wy_token=result.get("wyToken", ""),
            expires_at=now + self.ttl_seconds,
        )
        return {"enc": result["enc"], "wyToken": result.get("wyToken", "")}
