"""Async HTTP client for the Chaoxing (超星) library seat system."""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

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
        """Login via fanyalogin; populates cookies."""
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
        # fanyalogin returns JSON inside an HTML document sometimes; safest to grab text
        r = await self._client.post(
            f"{url}?{urlencode(data)}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        r.raise_for_status()
        text = r.text
        # The response looks like: {status:true, msg1:"", url1:"...", ...}
        try:
            # extract JSON object from response
            start = text.find("{")
            end = text.rfind("}") + 1
            if start < 0 or end <= start:
                raise ChaoxingError(f"no JSON in fanyalogin response: {text[:200]}")
            payload = json.loads(text[start:end])
        except json.JSONDecodeError as e:
            raise ChaoxingError(f"fanyalogin parse error: {text[:200]}") from e

        if not payload.get("status"):
            raise ChaoxingError(f"login failed: {payload.get('msg2') or payload.get('msg1') or 'unknown'}")

        # fanyalogin returns redirect URL — follow it to set cookies
        url1 = payload.get("url1")
        if url1:
            await self._client.get(url1, headers={"Referer": f"{self.PASSPORT_BASE}/"})

        if not any(c.name in ("_uid", "vc3") for c in self._cookie_jar.jar):
            raise ChaoxingError("login succeeded but no auth cookies set")

    # ---------- room info ----------
    async def get_room_info(self, room_id: int) -> dict[str, Any]:
        """Get the seatConfig + seatRoom for a given room_id."""
        url = f"{self.OFFICE_BASE}/data/apps/seat/room/info"
        referer = f"{self.OFFICE_BASE}/front/apps/seat/list"
        return await self._post_form(
            url, {"id": room_id}, referer=referer
        )

    # The methods below are added in Tasks 9-10. They raise NotImplementedError
    # for now so the class compiles cleanly.
    async def submit_reserve(self, *a, **kw) -> dict[str, Any]:
        raise NotImplementedError

    async def sign(self, *a, **kw) -> dict[str, Any]:
        raise NotImplementedError

    async def leave(self, *a, **kw) -> dict[str, Any]:
        raise NotImplementedError

    async def cancel(self, *a, **kw) -> dict[str, Any]:
        raise NotImplementedError

    async def get_active_reservation(self, *a, **kw) -> dict[str, Any] | None:
        raise NotImplementedError

    async def get_seat_status(self, *a, **kw) -> list[dict[str, Any]]:
        raise NotImplementedError