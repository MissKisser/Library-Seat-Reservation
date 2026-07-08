"""Async HTTP client for the Chaoxing (超星) library seat system."""
from __future__ import annotations

import json
from typing import Any
import httpx

from seatbot.utils.ua import random_ua


class ChaoxingError(Exception):
    """Generic Chaoxing API failure."""


class ChaoxingClient:
    PASSPORT_BASE = "https://passport2.chaoxing.com"
    OFFICE_BASE = "https://office.chaoxing.com"

    def __init__(self, *, ua: str | None = None):
        self._ua = ua or random_ua()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=10.0),
            follow_redirects=True,
            headers={
                "User-Agent": self._ua,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept": "application/json,text/plain,*/*",
            },
        )
        self._cookie_jar = self._client.cookies

    async def close(self) -> None:
        await self._client.aclose()

    def cookies(self) -> dict[str, str]:
        return {c.name: c.value for c in self._cookie_jar.jar}

    # ---------- low-level helpers ----------
    def _referer(self, path: str) -> str:
        return f"{self.OFFICE_BASE}{path}"

    async def _post_form(
        self, url: str, data: dict[str, Any], *, referer: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if referer:
            headers["Referer"] = referer
        r = await self._client.post(url, data=data, headers=headers)
        r.raise_for_status()
        try:
            return r.json()
        except json.JSONDecodeError as e:
            raise ChaoxingError(f"non-JSON response from {url}: {r.text[:200]}") from e

    # ---------- login ----------
    async def login(self, phone: str, password: str) -> None:
        """Log in to passport2.chaoxing.com and capture auth cookies.

        v1.1: the fanyalogin endpoint now requires AES-encrypted `uname` /
        `password` fields (key `u2oh6Vu^HWe4_AES`) + several extra fields,
        which are produced by the page's JS. Reproducing that in pure httpx
        is fragile, so we use a **headless Chromium** via Playwright to fill
        the real login form and then harvest the cookies back into httpx.

        Falls back to the legacy fanyalogin POST if Playwright is not
        installed (network may reject it; documented for reference only).
        """
        try:
            cookies = await self._login_via_browser(phone, password)
        except ImportError:
            cookies = await self._login_via_httpx(phone, password)

        # Inject cookies into httpx's cookie jar.
        from httpx import Cookies
        if not isinstance(self._cookie_jar, Cookies):
            self._cookie_jar = Cookies()
            self._client.cookies = self._cookie_jar
        for name, value in cookies.items():
            self._cookie_jar.set(
                name, value,
                domain=".chaoxing.com", path="/",
            )

        if not any(c.name in ("_uid", "vc3") for c in self._cookie_jar.jar):
            raise ChaoxingError("login succeeded but no auth cookies set")

    async def _login_via_browser(self, phone: str, password: str) -> dict[str, str]:
        from playwright.async_api import async_playwright

        captured: dict[str, str] = {}
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(
                viewport={"width": 1024, "height": 768},
                user_agent=self._ua,
            )
            page = await ctx.new_page()
            await page.goto(
                f"{self.PASSPORT_BASE}/login?newversion=true"
                f"&refer={self.OFFICE_BASE}/"
            )
            # The login page uses phone + password inputs.
            await page.locator("input[placeholder*='手机号']").fill(phone)
            await page.locator("input[placeholder*='密码']").fill(password)
            # wait briefly for any pre-validation
            await page.wait_for_timeout(300)
            await page.get_by_role("button", name="登录").click()
            # Poll for auth cookies (login success is signalled by `_uid`/`vc3`
            # being set on .chaoxing.com, regardless of which redirect target
            # the server sends us to).
            deadline_ms = 15000
            interval_ms = 250
            waited = 0
            while waited < deadline_ms:
                cookies = await ctx.cookies()
                if any(c["name"] in ("_uid", "vc3") for c in cookies):
                    break
                await page.wait_for_timeout(interval_ms)
                waited += interval_ms
            else:
                err_el = await page.query_selector(".error-msg, .alert-error, [class*=error]")
                err_text = (await err_el.inner_text()) if err_el else "(no auth cookie)"
                await browser.close()
                raise ChaoxingError(f"browser login failed: {err_text!r}")
            for c in await ctx.cookies():
                captured[c["name"]] = c["value"]
            await browser.close()
        return captured

    async def _login_via_httpx(self, phone: str, password: str) -> dict[str, str]:
        """Legacy fanyalogin fallback. The endpoint now requires AES-encrypted
        fields which we don't have, so this will usually fail. Kept only for
        environments without Playwright (e.g. minimal CI).
        """
        url = f"{self.PASSPORT_BASE}/fanyalogin"
        data = {
            "fid": -1,
            "pid": -1,
            "refer": "https%3A%2F%2Foffice.chaoxing.com%2F",
            "fidName": "",
            "allowForce": 1,
            "autoLogin": 0,
            "loginName": phone,
            "password": password,
            "verCode": "",
        }
        r = await self._client.post(
            url, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        r.raise_for_status()
        text = r.text
        try:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start < 0 or end <= start:
                raise ChaoxingError(f"no JSON in fanyalogin response: {text[:200]}")
            payload = json.loads(text[start:end])
        except json.JSONDecodeError as e:
            raise ChaoxingError(f"fanyalogin parse error: {text[:200]}") from e
        if not payload.get("status"):
            raise ChaoxingError(
                f"login failed: {payload.get('msg2') or payload.get('msg1') or 'unknown'}"
            )
        url1 = payload.get("url1")
        if url1:
            await self._client.get(url1, headers={"Referer": f"{self.PASSPORT_BASE}/"})
        return dict(self._client.cookies)

    # ---------- room info ----------
    async def get_room_info(self, room_id: int) -> dict[str, Any]:
        """Get the seatConfig + seatRoom for a given room_id."""
        url = f"{self.OFFICE_BASE}/data/apps/seat/room/info"
        referer = f"{self.OFFICE_BASE}/front/apps/seat/list"
        return await self._post_form(
            url, {"id": room_id}, referer=referer
        )

    # ---------- submit ----------
    async def submit_reserve(
        self,
        room_id: int,
        day: str,           # 'YYYY-MM-DD'
        start_time: str,    # 'HH:MM'
        end_time: str,      # 'HH:MM'
        seat_num: str,
        enc: str,
        wy_token: str = "",
        captcha: str = "",
    ) -> dict[str, Any]:
        url = f"{self.OFFICE_BASE}/data/apps/seat/submit"
        referer = (
            f"{self.OFFICE_BASE}/front/apps/seat/code"
            f"?id={room_id}&seatNum={seat_num}"
        )
        data = {
            "roomId": room_id,
            "day": day,
            "startTime": start_time,
            "endTime": end_time,
            "seatNum": seat_num,
            "captcha": captcha,
            "type": 1,
            "verifyData": 1,
            "wyToken": wy_token,
            "enc": enc,
        }
        return await self._post_form(url, data, referer=referer)

    # ---------- action endpoints ----------
    async def sign(self, reserve_id: int) -> dict[str, Any]:
        url = f"{self.OFFICE_BASE}/data/apps/seat/sign"
        return await self._post_form(
            url, {"id": reserve_id}, referer=self.OFFICE_BASE + "/"
        )

    async def leave(self, reserve_id: int) -> dict[str, Any]:
        url = f"{self.OFFICE_BASE}/data/apps/seat/leave"
        return await self._post_form(
            url, {"id": reserve_id}, referer=self.OFFICE_BASE + "/"
        )

    async def cancel(self, reserve_id: int) -> dict[str, Any]:
        url = f"{self.OFFICE_BASE}/data/apps/seat/cancel"
        return await self._post_form(
            url, {"id": reserve_id}, referer=self.OFFICE_BASE + "/"
        )

    async def get_active_reservation(self, room_id: int, seat_num: str) -> dict[str, Any] | None:
        url = (
            f"{self.OFFICE_BASE}/data/apps/seat/reserve/info"
            f"?id={room_id}&seatNum={seat_num}"
        )
        r = await self._client.get(url, headers={"Referer": self.OFFICE_BASE + "/"})
        r.raise_for_status()
        try:
            payload = r.json()
        except json.JSONDecodeError as e:
            raise ChaoxingError(f"reserve/info non-JSON: {r.text[:200]}") from e
        if not payload.get("success"):
            return None
        sr = (payload.get("data") or {}).get("seatReserve")
        return sr

    async def get_seat_status(
        self, room_id: int, day: str | None = None
    ) -> list[dict[str, Any]]:
        """Return seat status list for a given room + day.

        NOTE: the actual public endpoint that lists 108 seats was observed
        only via the floor SVG UI; we expose this method as a best-effort
        that pulls `seatIntervalMap` from `get_room_info`. If finer-grained
        status is needed, the web panel can call this and cross-reference
        with /data/apps/seat/getusedtimes. v1 keeps it simple.
        """
        info = await self.get_room_info(room_id)
        interval_map = (info.get("data") or {}).get("seatIntervalMap") or {}
        return [{"seat_num": k, "intervals": v} for k, v in interval_map.items()]