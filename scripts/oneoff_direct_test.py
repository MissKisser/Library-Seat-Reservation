"""S4 实弹验证 (修复计划 docs/2026-08-26-cross-day-booking-fix-plan.md §5):
页面内改写通道 (B1) 约"明天" → 服务端双重核验 → 取消释放。

用法:
  .venv/Scripts/python.exe scripts/oneoff_direct_test.py <account_id> <seat_num> <YYYY-MM-DD> <HH:MM> <HH:MM>

⚠️ 高危: 对真实账号发起真实预约 (随后立即取消)。
   依据 AGENTS.md 测试账号规则, 仅允许 xiongjt, 其他账号需用户点名。
"""
import asyncio
import sqlite3
import sys
from datetime import date
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from seatbot.client import ChaoxingClient  # noqa: E402
from seatbot.config import load_config  # noqa: E402
from session_cache import authenticated_client, save  # noqa: E402


def get_creds(cfg, acct: str) -> tuple[str, str]:
    con = sqlite3.connect(f"file:{cfg.runtime.db_path}?mode=ro", uri=True)
    row = con.execute(
        "SELECT phone, password FROM accounts WHERE id=?", (acct,)
    ).fetchone()
    con.close()
    if row is None:
        raise SystemExit(f"[direct-test] account {acct!r} not found in DB")
    return row


async def probe(client: ChaoxingClient) -> bool:
    cfg = load_config(root / "config.yaml")
    await client.get_used_times(cfg.library.room_id, "104", date.today().isoformat())
    return True


async def main() -> int:
    if len(sys.argv) != 6:
        print(__doc__)
        return 2
    acct, seat, day, start, end = sys.argv[1:6]
    if acct != "xiongjt":
        print("[direct-test] 仅允许 xiongjt (AGENTS.md 测试账号规则)")
        return 2

    cfg = load_config(root / "config.yaml")
    phone, password = get_creds(cfg, acct)
    client, src = await authenticated_client(acct, phone, password, probe)
    print(f"[direct-test] session={src} cookies={len(client.cookies())}")

    room = cfg.library.room_id
    try:
        # 0. 预检: 目标时段当前应为空
        used0 = await client.get_used_times(room, seat, day)
        print(f"[direct-test] before: {day} seat={seat} used={used0}")

        # 1. 直连预约
        r = await client.submit_via_page_rewrite(
            phone=phone, password=password, room_id=room,
            seat_num=seat, day=day, start_time=start, end_time=end,
        )
        print(f"[direct-test] submit_via_page_rewrite success={r.get('success')} "
              f"reserve_id={r.get('reserve_id')} msg={r.get('msg')}")
        if not r.get("success"):
            print(f"[direct-test] raw={str(r.get('raw'))[:400]}")
            return 1
        rid = r["reserve_id"]

        # 2. 核验 A: 占用查询应包含目标时段
        used1 = await client.get_used_times(room, seat, day)
        covered = any(s < end and e > start for s, e in used1)
        print(f"[direct-test] after reserve: used={used1} covered={covered}")

        # 3. 取消释放 (未开始时段可取消)
        c = await client.cancel(rid)
        print(f"[direct-test] cancel success={c.get('success')} msg={c.get('msg')}")
        used2 = await client.get_used_times(room, seat, day)
        print(f"[direct-test] after cancel: used={used2}")
        save(acct, client)

        ok = covered and c.get("success") and not any(
            s < end and e > start for s, e in used2
        )
        print(f"[direct-test] RESULT: {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
