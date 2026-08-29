"""座位页格子状态只读探针: 统计可选/noSelect 格子分布 (不点击不提交)。

用于诊断 submit_via_page_rewrite 的 "today-page has no selectable cell"
前置失败: 该通道的锚点扫描仅看前 30 个 li, 若低号座位全天约满则触发失败。
"""
import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.async_api import async_playwright  # noqa: E402

from seatbot.config import load_config  # noqa: E402
from seatbot.utils.ua import random_ua  # noqa: E402
from session_cache import authenticated_client  # noqa: E402


async def main() -> int:
    acct = sys.argv[1] if len(sys.argv) > 1 else "xiongjt"
    seat_param = sys.argv[2] if len(sys.argv) > 2 else "030"
    cfg = load_config("config.yaml")

    import sqlite3
    con = sqlite3.connect(f"file:{cfg.runtime.db_path}?mode=ro", uri=True)
    phone, pw = con.execute(
        "SELECT phone, password FROM accounts WHERE id=?", (acct,)
    ).fetchone()
    con.close()

    async def probe(client):
        await client.get_used_times(cfg.library.room_id, "104", date.today().isoformat())
        return True

    client, src = await authenticated_client(acct, phone, pw, probe)
    print(f"[probe] session={src} cookies={len(client.cookies())}")

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
        url = f"{client.OFFICE_BASE}/front/apps/seat/code?id={cfg.library.room_id}&seatNum={seat_param}"
        await page.goto(url)
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(500)
        if "passport2.chaoxing.com" in page.url:
            print(f"[probe] 会话失效被重定向: {page.url}")
            return 1

        stats = await page.evaluate(
            """() => {
                const lis = [...document.querySelectorAll('li')];
                let noSelect = 0, firstFree = -1, first30NoSelect = 0;
                const samples = [];
                lis.forEach((li, i) => {
                    const cls = li.className || '';
                    if (cls.includes('noSelect')) {
                        noSelect++;
                        if (i < 30) first30NoSelect++;
                    } else if (firstFree < 0) {
                        firstFree = i;
                    }
                    if (samples.length < 5 && cls) samples.push(i + ':' + cls);
                });
                return {total: lis.length, noSelect, firstFree, first30NoSelect, samples};
            }"""
        )
        print(f"[probe] li总数={stats['total']} noSelect={stats['noSelect']} "
              f"第一个可选格index={stats['firstFree']} 前30格中noSelect={stats['first30NoSelect']}")
        print(f"[probe] 样例: {stats['samples']}")

        deep = await page.evaluate(
            """() => ({
                title: document.title,
                url: location.href,
                htmlLen: document.documentElement.outerHTML.length,
                iframes: document.querySelectorAll('iframe').length,
                bodyText: (document.body.innerText || '').replace(/\\s+/g, ' ').slice(0, 400),
                tagCounts: (() => {
                    const c = {};
                    for (const el of document.querySelectorAll('*')) {
                        c[el.tagName] = (c[el.tagName] || 0) + 1;
                    }
                    return Object.fromEntries(
                        Object.entries(c).sort((a, b) => b[1] - a[1]).slice(0, 12));
                })(),
            })"""
        )
        print(f"[probe] title={deep['title']}")
        print(f"[probe] url={deep['url']}")
        print(f"[probe] htmlLen={deep['htmlLen']} iframes={deep['iframes']}")
        print(f"[probe] tags={deep['tagCounts']}")
        print(f"[probe] bodyText={deep['bodyText']}")
        out = Path("logs_debug")
        out.mkdir(exist_ok=True)
        shot = out / "seat_page_0829.png"
        await page.screenshot(path=str(shot), full_page=True)
        print(f"[probe] screenshot={shot.resolve()}")
        if stats['firstFree'] >= 30:
            print("[probe] ★ 证实: 存在可选格但在 30 格扫描上限之外")
        elif stats['firstFree'] == -1:
            print("[probe] ★ 证实: 全页无可选格 (当天全满)")
        else:
            print("[probe] 前 30 格内有可选格 — 假设不成立, 需另查")
        await browser.close()
    await client.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
