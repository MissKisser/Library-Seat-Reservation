"""诊断登录: 复刻 _login_via_browser, 超时则截图 + dump 可见文本, 用于定性拦截原因。

用法:
  .venv/Scripts/python.exe scripts/debug_login_shot.py <account_id>

输出: logs_debug/<acct>_<HHMMSS>.png + 页面文本摘要; 若登录成功则打印 cookies 数。
⚠️ 真实账号登录尝试, 仅在用户要求诊断时运行。
"""
import asyncio
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import async_playwright  # noqa: E402

from seatbot.config import load_config  # noqa: E402
from seatbot.utils.ua import random_ua  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[1] / "logs_debug"


async def main() -> int:
    acct = sys.argv[1]
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "config.yaml")
    con = sqlite3.connect(f"file:{cfg.runtime.db_path}?mode=ro", uri=True)
    phone, password = con.execute(
        "SELECT phone, password FROM accounts WHERE id=?", (acct,)
    ).fetchone()
    con.close()

    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    png = OUT_DIR / f"{acct}_{stamp}.png"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(
            viewport={"width": 1024, "height": 768},
            user_agent=random_ua(),
        )
        page = await ctx.new_page()
        await page.goto(
            "https://passport2.chaoxing.com/login?newversion=true"
            "&refer=https://office.chaoxing.com/"
        )
        await page.locator("input[placeholder*='手机号']").fill(phone)
        await page.locator("input[placeholder*='密码']").fill(password)
        await page.wait_for_timeout(300)
        await page.get_by_role("button", name="登录").click()
        # 与 client.py 修复保持一致: 自动点掉隐私协议弹窗 (诊断脚本=生产路径的忠实复刻)
        for sel in ("button:has-text('已阅读并同意')", "a:has-text('已阅读并同意')", "text='已阅读并同意'"):
            try:
                loc = page.locator(sel)
                if await loc.count():
                    await loc.first.click(timeout=1500)
                    await page.wait_for_timeout(300)
                    break
            except Exception:
                continue

        waited = 0
        ok = False
        while waited < 20000:
            cookies = await ctx.cookies()
            if any(c["name"] in ("_uid", "vc3") for c in cookies):
                ok = True
                break
            await page.wait_for_timeout(500)
            waited += 500

        await page.screenshot(path=str(png), full_page=True)
        title = await page.title()
        body = await page.inner_text("body")
        cookies = await ctx.cookies()
        names = sorted({c["name"] for c in cookies})
        if ok:
            # 成功 → 落盘 cookie (与 scripts/session_cache.py 同格式), 供链式脚本复用
            cache = Path(__file__).resolve().parents[1] / ".session_cache"
            cache.mkdir(exist_ok=True)
            (cache / f"{acct}.json").write_text(
                json.dumps({c["name"]: c["value"] for c in cookies}), encoding="utf-8"
            )
            print(f"[diag] cookies saved -> .session_cache/{acct}.json")
        await browser.close()

    print(f"[diag] account={acct} login_ok={ok}")
    print(f"[diag] url_title={title!r}")
    print(f"[diag] cookies({len(names)}): {','.join(names)}")
    print(f"[diag] screenshot={png}")
    print(f"[diag] body_text (first 600 chars) >>>")
    print("\n".join(l for l in body.splitlines() if l.strip())[:600])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
