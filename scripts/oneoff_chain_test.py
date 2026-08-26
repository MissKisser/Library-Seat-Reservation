"""链路测试: 预约/签到/签退 一步一命令, 跨进程复用 cookie (session_cache)。

用法:
  .venv/Scripts/python.exe scripts/oneoff_chain_test.py reserve <acct> <seat> <YYYY-MM-DD> <HH:MM> <HH:MM>
  .venv/Scripts/python.exe scripts/oneoff_chain_test.py sign   <acct> <reserve_id>
  .venv/Scripts/python.exe scripts/oneoff_chain_test.py leave  <acct> <reserve_id>

设计: 每个子命令 = 独立进程; 登录态从 .session_cache/<acct>.json 复用,
仅当缓存缺失/失效时才真实登录。整条 预约→签到→签退 链路目标只登录 1 次。

⚠️ 高危: reserve/sign/leave 都是对真实账号的真实操作。
   依据 AGENTS.md 第一条禁令, 仅当用户在当前会话明确要求时才可运行。
"""
import asyncio
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from seatbot.client import ChaoxingClient  # noqa: E402
from seatbot.config import load_config  # noqa: E402
from session_cache import authenticated_client, save  # noqa: E402

PROBE_SEAT = "104"  # 会话探活用的座位号 (getusedtimes)


def get_creds(cfg, acct: str) -> tuple[str, str]:
    con = sqlite3.connect(f"file:{cfg.runtime.db_path}?mode=ro", uri=True)
    row = con.execute(
        "SELECT phone, password FROM accounts WHERE id=?", (acct,)
    ).fetchone()
    con.close()
    if row is None:
        raise SystemExit(f"[chain] account {acct!r} not found in DB")
    return row


async def probe(client: ChaoxingClient) -> bool:
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    await client.get_used_times(cfg.library.room_id, PROBE_SEAT, date.today().isoformat())
    return True  # ChaoxingError (success:false) 会被上层当作缓存失效


async def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    action, acct = sys.argv[1], sys.argv[2]
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "config.yaml")
    phone, password = get_creds(cfg, acct)

    client, src = await authenticated_client(acct, phone, password, probe)
    print(f"[chain] session={src} cookies={len(client.cookies())}")
    rc = 1
    try:
        if action == "reserve":
            seat, day, start, end = sys.argv[3:7]
            r = await client.submit_in_browser(
                phone=phone, password=password,
                room_id=cfg.library.room_id, seat_num=seat,
                day=day, start_time=start, end_time=end,
            )
            print(f"[chain] reserve success={r.get('success')} "
                  f"reserve_id={r.get('reserve_id')} msg={r.get('msg')}")
            rc = 0 if r.get("success") else 1
        elif action in ("sign", "leave", "signback"):
            rid = int(sys.argv[3])
            fn = {"sign": client.sign, "leave": client.leave,
                  "signback": client.signback}[action]
            r = await fn(rid)
            print(f"[chain] {action} response={r}")
            rc = 0 if r.get("success") else 1
        else:
            print(f"[chain] unknown action {action!r}")
            return 2
    finally:
        save(acct, client)  # submit_in_browser 可能刷新了 cookies, 回写缓存
        await client.close()
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
