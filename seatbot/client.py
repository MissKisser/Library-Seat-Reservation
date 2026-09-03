"""Async HTTP client for the Chaoxing (超星) library seat system."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
import httpx

from seatbot.utils.ua import random_ua


class ChaoxingError(Exception):
    """Generic Chaoxing API failure."""


# Beijing time used by Chaoxing APIs (all observed timestamps are UTC+8).
TZ_CN = timezone(timedelta(hours=8))


def ms_pair_to_str(pair: list[int]) -> tuple[str, str]:
    """Convert [start_ms, end_ms] → ('HH:MM', 'HH:MM') in Beijing time."""
    s = datetime.fromtimestamp(pair[0] / 1000, tz=TZ_CN).strftime("%H:%M")
    e = datetime.fromtimestamp(pair[1] / 1000, tz=TZ_CN).strftime("%H:%M")
    return s, e


class ChaoxingClient:
    PASSPORT_BASE = "https://passport2.chaoxing.com"
    OFFICE_BASE = "https://office.chaoxing.com"

    # fidEnc values used by different Chaoxing frontends.
    # The mobile (学习通 app) fidEnc is REQUIRED for /getusedtimes to return
    # others' reservations; the PC web fidEnc (documented in
    # docs/superpowers/specs/2026-07-09-chaoxing-api-reference.md) returns
    # an empty array, so only the mobile value is used in code.
    FID_ENC_MOBILE = "24680a1d287b60c7"

    def __init__(self, *, ua: str | None = None):
        self._ua = ua or random_ua()
        self._logged_at: float | None = None   # time.monotonic() of last successful login
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

    def set_cookies(self, cookies: dict[str, str]) -> bool:
        """注入持久化 cookie 恢复会话；含 `_uid`/`vc3` 等鉴权 cookie 时返回 True。

        用于重启后免浏览器登录：注入成功即可直接调 API，
        会话若已失效由调用方按"请求失败"路径 reset_session + login 自愈。
        """
        from httpx import Cookies
        if not cookies:
            return False
        if not isinstance(self._cookie_jar, Cookies):
            self._cookie_jar = Cookies()
            self._client.cookies = self._cookie_jar
        for name, value in cookies.items():
            self._cookie_jar.set(
                name, value,
                domain=".chaoxing.com", path="/",
            )
        return any(c.name in ("_uid", "vc3") for c in self._cookie_jar.jar)

    def reset_session(self) -> None:
        """Drop all cookies so the next login starts clean.

        陈旧 cookie 的症状: API 返回 "您当前未登录" / 浏览器被重定向到
        passport 登录页。调用方捕获后应 reset_session() + login() 重试一次。
        """
        from httpx import Cookies
        self._cookie_jar = Cookies()
        self._client.cookies = self._cookie_jar
        self._logged_at = None

    # ---------- low-level helpers ----------
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

        The fanyalogin endpoint requires AES-encrypted `uname` / `password`
        fields (key `u2oh6Vu^HWe4_AES`) + several extra fields produced by
        the page's JS. Reproducing that in pure httpx is fragile, so we use
        a **headless Chromium** via Playwright to fill the real login form
        and then harvest the cookies back into httpx.
        """
        cookies = await self._login_via_browser(phone, password)

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
        import time as _time
        self._logged_at = _time.monotonic()

    @staticmethod
    async def _dismiss_consent_dialog(page) -> None:
        """点"登录"后可能弹出隐私协议确认框,必须点"已阅读并同意"登录才会继续。

        2026-08-25 实测: 该弹窗间歇性出现 (与账号无关), 不处理时表现为
        轮询超时无 auth cookie 且页面无错误文案 —— 曾被误判为风控拦截。
        """
        for sel in (
            "button:has-text('已阅读并同意')",
            "a:has-text('已阅读并同意')",
            "text='已阅读并同意'",
        ):
            try:
                loc = page.locator(sel)
                if await loc.count():
                    await loc.first.click(timeout=1500)
                    await page.wait_for_timeout(300)
                    return
            except Exception:
                continue

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
            await self._dismiss_consent_dialog(page)
            # Poll for auth cookies (login success is signalled by `_uid`/`vc3`
            # being set on .chaoxing.com, regardless of which redirect target
            # the server sends us to).
            deadline_ms = 25000
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

    async def submit_in_browser(
        self,
        phone: str,
        password: str,
        room_id: int,
        seat_num: str,
        day: str,            # 'YYYY-MM-DD'
        start_time: str,     # 'HH:MM'
        end_time: str,       # 'HH:MM'
    ) -> dict[str, Any]:
        """End-to-end reservation via Playwright headless Chromium.

        Why this exists:
          The /submit endpoint requires a fresh `enc` token bound to the
          browser session's risk-control state. The token expires within
          a few minutes and cannot be reused from a separate httpx call.
          The only reliable way to obtain a valid `enc` and post /submit
          is to drive the real Chaoxing UI in a real browser.

        Flow:
          1. Launch headless Chromium.
          2. Log in with `phone` / `password`.
          3. Navigate to the seat detail page.
          4. Click the start-time 30-min cell, then any cells up to end_time.
          5. Click "开始使用" which fires the /submit POST.
          6. Intercept the JSON response and parse out the reserve_id.

        Returns:
            {"success": bool, "reserve_id": int|None,
             "msg": str|None, "raw": <server JSON>}

        Caller is expected to log in successfully first (or we log in
        ourselves here). We do NOT re-use an existing session — each
        /submit gets its own browser, fresh enc, fresh cookies.
        """
        from playwright.async_api import async_playwright

        result: dict[str, Any] = {
            "success": False,
            "reserve_id": None,
            "msg": None,
            "raw": None,
        }

        # if this ChaoxingClient already has cookies, prime the browser
        # with them; otherwise we log in fresh.
        existing_cookies = self.cookies()

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=self._ua,
            )

            try:
                if existing_cookies:
                    await ctx.add_cookies([
                        {"name": k, "value": v, "url": self.OFFICE_BASE}
                        for k, v in existing_cookies.items()
                    ])
                else:
                    # log in fresh
                    page = await ctx.new_page()
                    await page.goto(
                        f"{self.PASSPORT_BASE}/login?newversion=true"
                        f"&refer={self.OFFICE_BASE}/"
                    )
                    await page.locator("input[placeholder*='手机号']").fill(phone)
                    await page.locator("input[placeholder*='密码']").fill(password)
                    await page.get_by_role("button", name="登录").click()
                    await self._dismiss_consent_dialog(page)
                    deadline_ms = 25000
                    waited = 0
                    while waited < deadline_ms:
                        cookies = await ctx.cookies()
                        if any(c["name"] in ("_uid", "vc3") for c in cookies):
                            break
                        await page.wait_for_timeout(250)
                        waited += 250
                    else:
                        result["msg"] = "login timed out"
                        return result
                    # harvest cookies into our httpx jar for next time
                    for c in cookies:
                        self._cookie_jar.set(
                            c["name"], c["value"],
                            domain=c["domain"], path=c.get("path", "/"),
                        )

                # build a list of 30-min cell labels to click, e.g.
                #   21:00-22:00 → ["21:00-21:30", "21:30-22:00"]
                cells = self._cell_labels(start_time, end_time)
                submit_response: dict = {}
                page = await ctx.new_page()

                async def on_response(resp):
                    if "/data/apps/seat/submit" in resp.url and resp.request.method == "POST":
                        try:
                            submit_response["status"] = resp.status
                            submit_response["body"] = await resp.text()
                        except Exception as e:
                            submit_response["err"] = str(e)

                page.on("response", on_response)

                url = (
                    f"{self.OFFICE_BASE}/front/apps/seat/code"
                    f"?id={room_id}&seatNum={seat_num}&day={day}"
                )
                await page.goto(url)
                await page.wait_for_load_state("networkidle")
                await page.wait_for_timeout(500)

                # ★ 会话自愈: 预注入的 cookie 已过期时, 座位页会把我们重定向
                # 到 passport 登录页 (症状: 后续 "start cell not clickable")。
                # 在页内补登录后重新加载座位页再继续。
                if "passport2.chaoxing.com" in page.url:
                    if not await self._login_in_page(page, phone, password):
                        result["msg"] = "session expired; in-page relogin failed"
                        return result
                    for c in await ctx.cookies():
                        self._cookie_jar.set(
                            c["name"], c["value"],
                            domain=c["domain"], path=c.get("path", "/"),
                        )
                    import time as _time
                    self._logged_at = _time.monotonic()
                    await page.goto(url)
                    await page.wait_for_load_state("networkidle")
                    await page.wait_for_timeout(500)

                # click start (逐格验证选中态 — R2 加固, 见 _click_cell_verified)
                if not await self._click_cell_verified(page, cells[0]):
                    result["msg"] = f"start cell {cells[0]} not clickable"
                    return result
                # additional cells (if span > 30 min)
                for label in cells[1:]:
                    if not await self._click_cell_verified(page, label):
                        result["msg"] = f"cell {label} not clickable"
                        return result

                await page.wait_for_timeout(300)

                # click 开始使用
                try:
                    btn = page.locator("p.can_submit:has-text('开始使用')")
                    if await btn.count() == 0:
                        # fallback to any "开始使用" text
                        btn = page.locator(":text('开始使用')")
                    await btn.first.click(timeout=5000)
                except Exception as e:
                    result["msg"] = f"begin-click failed: {e}"
                    return result

                # wait up to 12s for /submit response
                for _ in range(24):
                    if "body" in submit_response or "err" in submit_response:
                        break
                    await page.wait_for_timeout(500)

                if "err" in submit_response:
                    result["msg"] = f"network: {submit_response['err']}"
                    return result
                if "body" not in submit_response:
                    result["msg"] = "no /submit response in 12s"
                    return result

                raw_text = submit_response["body"]
                try:
                    payload = json.loads(raw_text)
                except json.JSONDecodeError:
                    result["raw"] = raw_text[:500]
                    result["msg"] = "submit response not JSON"
                    return result

                result["raw"] = payload
                if payload.get("success"):
                    sr = (payload.get("data") or {}).get("seatReserve") or {}
                    rid = sr.get("id")
                    result["success"] = bool(rid)
                    result["reserve_id"] = rid
                    if not rid:
                        result["msg"] = "submit ok but no reserve_id"
                else:
                    result["msg"] = payload.get("msg") or "submit rejected"
                return result
            finally:
                await browser.close()

    @staticmethod
    def _cell_labels(start_time: str, end_time: str) -> list[str]:
        """Return the 30-min cell labels we need to click.
        e.g. 21:00-22:00 → ["21:00-21:30", "21:30-22:00"]
        """
        from datetime import datetime, timedelta
        s = datetime.strptime(start_time, "%H:%M")
        e = datetime.strptime(end_time, "%H:%M")
        out = []
        while s < e:
            n = min(s + timedelta(minutes=30), e)
            out.append(f"{s.strftime('%H:%M')}-{n.strftime('%H:%M')}")
            s = n
        return out

    @staticmethod
    async def _login_in_page(page, phone: str, password: str) -> bool:
        """在当前已打开的 passport 登录页上填写表单并等待 auth cookie。"""
        try:
            await page.locator("input[placeholder*='手机号']").fill(phone)
            await page.locator("input[placeholder*='密码']").fill(password)
            await page.wait_for_timeout(300)
            await page.get_by_role("button", name="登录").click()
            await ChaoxingClient._dismiss_consent_dialog(page)
        except Exception:
            return False
        waited = 0
        while waited < 25000:
            cookies = await page.context.cookies()
            if any(c["name"] in ("_uid", "vc3") for c in cookies):
                return True
            await page.wait_for_timeout(250)
            waited += 250
        return False

    @staticmethod
    async def _click_cell_verified(page, label: str) -> bool:
        """点 30 分钟格子并验证选中态 (2026-08-26 修复计划 通道B)。

        R2 根因: noSelect 格子点击静默无效 (页面吞掉事件且无报错),
        旧行为把"点了"当"选中", 导致只约到尾部时段。
        加固: 点击后该格 class 必须发生变化且不带 noSelect, 否则重试 (最多 3 次)。
        """
        selectors = [f"li:has-text('{label}')"]
        prefix = label.split("-")[0]
        if prefix != label:
            # the page sometimes shows "21:00" instead of "21:00-21:30"
            selectors.append(f"li:has-text('{prefix}')")
        for sel in selectors:
            loc = page.locator(sel).first
            for _ in range(3):
                if await loc.count() == 0:
                    break
                try:
                    before = await loc.get_attribute("class") or ""
                    await loc.click(timeout=2000)
                    await page.wait_for_timeout(300)
                    after = await loc.get_attribute("class") or ""
                    if after != before and "noSelect" not in after:
                        return True
                except Exception:
                    continue
        return False

    @staticmethod
    async def _click_first_selectable_cell(page) -> bool:
        """点页面上第一个可选格子 (供 enc harvest 触发表单构造用)。

        格子由页面 XHR 异步渲染, 预约窗口开闸时段可能延迟数秒, 先等待渲染;
        全量扫描不设条数上限, 避免低号座位全满时误判无格可点。
        """
        for _ in range(8):
            if await page.locator("li").count() > 0:
                break
            await page.wait_for_timeout(1000)
        for sel in ("li:not(.noSelect)", "li"):
            loc = page.locator(sel)
            for i in range(await loc.count()):
                el = loc.nth(i)
                try:
                    cls = await el.get_attribute("class") or ""
                    if "noSelect" in cls:
                        continue
                    await el.click(timeout=1500)
                    await page.wait_for_timeout(300)
                    cls2 = await el.get_attribute("class") or ""
                    if cls2 != cls:
                        return True
                except Exception:
                    continue
        return False

    async def submit_direct(
        self,
        phone: str,
        password: str,
        room_id: int,
        seat_num: str,
        day: str,            # 'YYYY-MM-DD' — 目标日期 (可为未来日期)
        start_time: str,     # 'HH:MM'
        end_time: str,       # 'HH:MM'
    ) -> dict[str, Any]:
        """直连提交通道: 不做任何页面交互, 纯 httpx 构造并提交预约表单。

        账号存在进行中的使用会话时, 座位页不渲染时段格子 (呈现"使用中"
        面板), 页面触发式通道无从点格。本通道仅从座位页 HTML 读取服务端
        渲染的 enc 种子 (#submit_enc), 按页面提交算法对全部字段重算 enc
        后直接 POST /submit, 不依赖格子 DOM; 种子获取与提交共用同一
        httpx 会话 (种子内嵌 uid, 须与提交方同会话)。

        Returns: {success, reserve_id, msg, raw, channel}。
        """
        result: dict[str, Any] = {
            "success": False, "reserve_id": None, "msg": None,
            "raw": None, "channel": "direct",
        }

        if not self.cookies():
            await self.login(phone, password)

        page_url = (
            f"{self.OFFICE_BASE}/front/apps/seat/code"
            f"?id={room_id}&seatNum={seat_num}"
        )
        try:
            page_html = (await self._client.get(page_url)).text
        except Exception as e:
            result["msg"] = f"seat page fetch failed: {e}"
            return result

        seed = None
        for pattern in (
            r"""id=["']submit_enc["'][^>]*value=["']([^"']+)["']""",
            r"""value=["']([^"']+)["'][^>]*id=["']submit_enc["']""",
        ):
            m = re.search(pattern, page_html)
            if m:
                seed = m.group(1)
                break
        if not seed:
            result["msg"] = "submit_enc seed not found on seat page"
            return result

        # 字段集 = 页面 doSubmit 的 paramObj 九件套; enc = md5(按 key 排序的
        # "[k=v]" 拼接 + "[种子]"), 服务端用种子校验全部字段。
        fields = {
            "roomId": str(room_id),
            "day": day,
            "startTime": start_time,
            "endTime": end_time,
            "seatNum": seat_num,
            "captcha": "",
            "type": "1",
            "verifyData": "1",
            "wyToken": "",
        }
        concat = "".join(f"[{k}={fields[k]}]" for k in sorted(fields))
        fields["enc"] = hashlib.md5(
            (concat + f"[{seed}]").encode("utf-8")
        ).hexdigest()

        try:
            payload = await self._post_form(
                f"{self.OFFICE_BASE}/data/apps/seat/submit",
                fields,
                referer=page_url,
            )
        except Exception as e:
            result["msg"] = f"submit request failed: {e}"
            return result

        result["raw"] = payload
        if payload.get("success"):
            rid = ((payload.get("data") or {}).get("seatReserve") or {}).get("id")
            result["success"] = bool(rid)
            result["reserve_id"] = rid
            if not rid:
                result["msg"] = "submit ok but no reserve_id"
        else:
            result["msg"] = payload.get("msg") or "submit rejected"
        return result

    async def submit_via_page_rewrite(
        self,
        phone: str,
        password: str,
        room_id: int,
        seat_num: str,
        day: str,            # 'YYYY-MM-DD' — 目标日期 (可为未来日期)
        start_time: str,     # 'HH:MM'
        end_time: str,       # 'HH:MM'
        anchor_seat: str | None = None,  # 开页用座位号; 目标座位页可能处于占用详情态无格子
    ) -> dict[str, Any]:
        """跨天预约通道 B1 (2026-08-26 下午): 页面内改写放行。

        前代通道 A (abort 截获 enc + httpx 重放) 被 303 风控全量拒绝
        (14:00 批量 0/6): 服务端校验的不只是 enc 字段, 还绑定请求环境
        (TLS 指纹 / header 序列 / 全部 cookie), 跨进程重放无法仿真。

        本通道让页面 JS 在真实 Chromium 里自己构造并发出 /submit,
        在网络层改写 day/时段字段并按页面自有算法 (submitVerify.min.js 逆向:
        md5("[k=v]"按 key 排序拼接 + "[submit_enc种子]") 重算 enc 后放行。
        303 实证: 只改字段不重算 enc, 服务端判为篡改直接拒绝。
        """
        from playwright.async_api import async_playwright
        from urllib.parse import parse_qs, urlencode

        result: dict[str, Any] = {
            "success": False, "reserve_id": None, "msg": None, "raw": None,
            "channel": "page-rewrite",
        }

        if not self.cookies():
            await self.login(phone, password)

        submit_response: dict[str, Any] = {}
        rewrite_info: dict[str, str] = {}

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                ctx = await browser.new_context(
                    viewport={"width": 1280, "height": 800}, user_agent=self._ua,
                )
                cookies = self.cookies()
                if cookies:
                    await ctx.add_cookies([
                        {"name": k, "value": v, "url": self.OFFICE_BASE}
                        for k, v in cookies.items()
                    ])

                page = await ctx.new_page()

                async def on_response(resp):
                    if "/data/apps/seat/submit" in resp.url and resp.request.method == "POST":
                        try:
                            if "body" not in submit_response:
                                submit_response["status"] = resp.status
                                submit_response["body"] = await resp.text()
                        except Exception as e:
                            submit_response.setdefault("err", str(e))

                page.on("response", on_response)

                page_state: dict[str, str] = {}
                async def rewrite_route(route, request):
                    """改写目标字段并按页面算法重算 enc (verifyParam 逆向)。
                    任一环节失败 → abort, 绝不放行原始(今天)请求。"""
                    raw = request.post_data or ""
                    try:
                        parsed = parse_qs(raw, keep_blank_values=True)
                        flat = {k: v[-1] for k, v in parsed.items()}
                        token = page_state.get("enc_seed")
                        if not token:
                            raise ValueError("submit_enc seed not captured")
                        import hashlib
                        # 字段集 = doSubmit 的 paramObj 九件套 (缺则取原值/默认)
                        fields = {
                            "roomId": flat.get("roomId", str(room_id)),
                            "day": day,
                            "startTime": start_time,
                            "endTime": end_time,
                            "seatNum": seat_num,
                            "captcha": flat.get("captcha", ""),
                            "type": flat.get("type", "1"),
                            "verifyData": flat.get("verifyData", "1"),
                            "wyToken": flat.get("wyToken", ""),
                        }
                        concat = "".join(
                            f"[{k}={fields[k]}]" for k in sorted(fields)
                        )
                        new_enc = hashlib.md5(
                            (concat + f"[{token}]").encode("utf-8")
                        ).hexdigest()
                        out = dict(flat)
                        out.update(fields)
                        out["enc"] = new_enc
                    except Exception:
                        # ★ 安全阀: 解析/重签失败绝不能放行 — 宁可失败不可约错日
                        await route.abort()
                        return
                    rewrite_info["original_day"] = flat.get("day", "?")
                    rewrite_info["sent_day"] = day
                    await route.continue_(post_data=urlencode(out))

                # route 必须在 goto 前注册, 覆盖页面生命周期内的所有 /submit
                await page.route("**/data/apps/seat/submit", rewrite_route)

                url = (
                    f"{self.OFFICE_BASE}/front/apps/seat/code"
                    f"?id={room_id}&seatNum={anchor_seat or seat_num}"
                )
                await page.goto(url)
                await page.wait_for_load_state("networkidle")
                await page.wait_for_timeout(500)

                # 会话失效自愈: 页内补登录后重进座位页 (route 注册不受导航影响)
                if "passport2.chaoxing.com" in page.url:
                    if not await self._login_in_page(page, phone, password):
                        result["msg"] = "session expired; in-page relogin failed"
                        return result
                    for c in await ctx.cookies():
                        self._cookie_jar.set(
                            c["name"], c["value"],
                            domain=c["domain"], path=c.get("path", "/"),
                        )
                    import time as _time
                    self._logged_at = _time.monotonic()
                    await page.goto(url)
                    await page.wait_for_load_state("networkidle")
                    await page.wait_for_timeout(500)

                # 捕获服务端渲染的 enc 种子 (隐藏域 #submit_enc = verifyParam 的盐)
                try:
                    page_state["enc_seed"] = await page.locator(
                        "#submit_enc"
                    ).first.input_value(timeout=3000)
                except Exception:
                    page_state["enc_seed"] = ""
                # 随点一个可选格子 — 只为让页面构造出完整请求体 (含新鲜 enc);
                # 真实的日期/时段字段由 rewrite_route 在网络层覆盖。
                if not await self._click_first_selectable_cell(page):
                    result["msg"] = "today-page has no selectable cell; cannot trigger form"
                    return result
                await page.wait_for_timeout(300)

                try:
                    btn = page.locator("p.can_submit:has-text('开始使用')")
                    if await btn.count() == 0:
                        btn = page.locator(":text('开始使用')")
                    await btn.first.click(timeout=5000)
                except Exception as e:
                    result["msg"] = f"begin-click failed: {e}"
                    return result

                # 等被改写放行的 /submit 响应 (最长 12s)
                for _ in range(24):
                    if "body" in submit_response or "err" in submit_response:
                        break
                    await page.wait_for_timeout(500)

                if "body" not in submit_response:
                    result["msg"] = (
                        f"no rewritten /submit response in 12s "
                        f"(original_day={rewrite_info.get('original_day')}, "
                        f"sent_day={rewrite_info.get('sent_day')})"
                    )
                    return result
                if "err" in submit_response:
                    result["msg"] = f"network: {submit_response['err']}"
                    return result

                try:
                    payload = json.loads(submit_response["body"])
                except json.JSONDecodeError:
                    result["raw"] = submit_response["body"][:500]
                    result["msg"] = "submit response not JSON"
                    return result

                result["raw"] = {"server": payload, "rewrite": rewrite_info}
                if payload.get("success"):
                    sr = (payload.get("data") or {}).get("seatReserve") or {}
                    rid = sr.get("id")
                    result["success"] = bool(rid)
                    result["reserve_id"] = rid
                    if not rid:
                        result["msg"] = "submit ok but no reserve_id"
                else:
                    result["msg"] = payload.get("msg") or "submit rejected"
                return result
            finally:
                await browser.close()

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

    async def signback(self, reserve_id: int) -> dict[str, Any]:
        """签退 / 退座 (结束使用)。

        与 leave (暂离) 不同: 暂离要求剩余时长 ≥20min 且只是暂时释放,
        signback 才是真正的"签退", 时段进行中任意时刻可用, 结束使用。
        2026-08-25 实测验证 (来源: github MGJ520/XXT_Library_Web)。
        """
        url = f"{self.OFFICE_BASE}/data/apps/seat/signback"
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

    async def supervise(self, reserve_id: int, photo_object_id: str = "") -> dict[str, Any]:
        """对一条使用中的预约发起监督（真实操作，仅限用户明确授权时调用）。

        Endpoint:  POST /data/apps/seat/supervise
        Params:    id=被监督座位当前预约号; objectId=现场照片 id,
                   仅 seatConfig.supervisePhoto==1 的馆强制，否则传空。
        Returns:   {success, msg}；成功后被监督方收到 20 分钟落座提醒。
        """
        return await self._post_form(
            f"{self.OFFICE_BASE}/data/apps/seat/supervise",
            {"id": reserve_id, "objectId": photo_object_id},
            referer=self.OFFICE_BASE + "/",
        )

    # ---------- reservation records ----------
    #: reservelist 状态码 → 官方含义（详见 docs/api/reservelist.md）
    RESERVE_STATUS = {
        0: "待履约", 1: "使用中", 2: "已履约", 3: "暂离中",
        5: "被监督中", 7: "已取消", 8: "违约",
    }
    #: 被监督中（他人发起监督后进入，20 分钟内需确认落座否则记违约）
    RESERVE_STATUS_SUPERVISED = 5

    async def reserve_list(
        self,
        *,
        index_id: int = 0,
        page_size: int = 50,
        type_: int = -1,
    ) -> list[dict]:
        """查询**当前登录账号**的预约记录（官方 App「预约记录」页数据源）。

        Endpoint:  GET /data/apps/seat/reservelist
        Params:    indexId (0 起的页码), pageSize, type (-1 全部), fidEnc (移动端)
        Auth:      当前会话账号; 接口只返回登录人本人的记录, 无按 uid 查他人参数。
        Returns:   reserveList[] 原始条目列表; status 含义见 RESERVE_STATUS。
        只读查询, 无预约/违约副作用。
        """
        r = await self._client.get(
            f"{self.OFFICE_BASE}/data/apps/seat/reservelist",
            params={"indexId": index_id, "pageSize": page_size, "type": type_,
                    "fidEnc": self.FID_ENC_MOBILE},
            headers={"Referer": self.OFFICE_BASE + "/"},
        )
        r.raise_for_status()
        try:
            payload = r.json()
        except json.JSONDecodeError as e:
            raise ChaoxingError(f"reservelist non-JSON: {r.text[:200]}") from e
        if not payload.get("success"):
            raise ChaoxingError(
                f"reservelist rejected: {payload.get('msg') or payload!r}"
            )
        return (payload.get("data") or {}).get("reserveList") or []

    async def supervised_reservations(self) -> list[dict]:
        """查询当前登录账号处于**被监督中**（status=5）的预约记录。

        依据: reservelist 状态表 status=5 即"被监督中"（他人监督后进入,
        官方流程要求 20 分钟内扫码落座, 否则记违约）。解除方式是持该
        预约号调 sign()（与扫码落座等效, 参考 XXT_Library_Web 的
        Check_Service 同款处理）。

        Returns: reserveList 中 status=5 的条目原样列表, 每条含
                 id / roomId / seatNum / startTime / endTime 等。
        只读查询, 无预约/违约副作用。
        """
        records = await self.reserve_list()
        return [
            r for r in records
            if r.get("status") == self.RESERVE_STATUS_SUPERVISED
        ]

    # ---------- occupancy lookup ----------
    async def get_used_times(
        self,
        room_id: int,
        seat_num: str,
        day: str,
        *,
        fid_enc: str | None = None,
    ) -> list[tuple[str, str]]:
        """Return the occupied 30-min-slot intervals for one seat on one day.

        Endpoint:  POST /data/apps/seat/getusedtimes
        Params:    roomId, seatNum, day ('YYYY-MM-DD'), fidEnc
        Auth:      any logged-in .chaoxing.com cookie works.
        Returns:   list of ('HH:MM', 'HH:MM') in Beijing time. Empty list
                   when nobody has reserved the seat on that day.

        CRITICAL:
            You MUST pass the mobile fidEnc (`self.FID_ENC_MOBILE`,
            i.e. ``24680a1d287b60c7``). The PC-web fidEnc
            (``85a5894481db5d7a``) returns ``data: []`` even when the
            seat is genuinely occupied. Discovered 2026-07-09 by the
            user on the 学习通 mobile app; see
            ``docs/superpowers/specs/2026-07-09-investigation-status.md``.

        Verified on 2026-07-09 with cookies of uid=314500121:
            seat 104 → [("19:30","21:30")]
            seat 105 → [("15:30","17:30"), ("19:00","21:00")]
        """
        url = f"{self.OFFICE_BASE}/data/apps/seat/getusedtimes"
        referer = (
            f"{self.OFFICE_BASE}/front/apps/seat/code"
            f"?id={room_id}&seatNum={seat_num}"
        )
        payload = await self._post_form(
            url,
            {
                "roomId": str(room_id),
                "seatNum": seat_num,
                "day": day,
                "fidEnc": fid_enc or self.FID_ENC_MOBILE,
            },
            referer=referer,
        )
        if not payload.get("success"):
            raise ChaoxingError(
                f"getusedtimes rejected: {payload.get('msg') or payload!r}"
            )
        data = payload.get("data") or []
        out: list[tuple[str, str]] = []
        for pair in data:
            if (
                isinstance(pair, (list, tuple))
                and len(pair) == 2
                and all(isinstance(x, (int, float)) for x in pair)
            ):
                out.append(ms_pair_to_str(list(pair)))
        return out