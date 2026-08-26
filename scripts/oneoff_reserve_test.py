"""一次性预约测试: 用指定账号预约指定座位/时段。

用法:
  .venv/Scripts/python.exe scripts/oneoff_reserve_test.py <account_id> <seat_num> <YYYY-MM-DD> <HH:MM> <HH:MM>

特点:
  - 不经过 scheduler、不写 seatbot.db (凭证只读)
  - 走 submit_in_browser (与 scheduler._run_submit 相同路径)

⚠️ 高危: 会用真实账号登录超星并发起真实预约请求。
   依据 AGENTS.md 第一条禁令, 仅当用户在当前会话明确要求时才可运行。
"""
import asyncio
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seatbot.client import ChaoxingClient  # noqa: E402
from seatbot.config import load_config  # noqa: E402


async def main() -> int:
    if len(sys.argv) != 6:
        print(__doc__)
        return 2
    account_id, seat_num, day, start, end = sys.argv[1:6]
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "config.yaml")

    con = sqlite3.connect(f"file:{cfg.runtime.db_path}?mode=ro", uri=True)
    row = con.execute(
        "SELECT phone, password FROM accounts WHERE id=?", (account_id,)
    ).fetchone()
    con.close()
    if row is None:
        print(f"[oneoff] account {account_id!r} not found in DB")
        return 2
    phone, password = row  # 不打印

    print(
        f"[oneoff] account={account_id} seat={seat_num} day={day} "
        f"{start}-{end} room={cfg.library.room_id}"
    )
    client = ChaoxingClient()
    try:
        t0 = datetime.now()
        r = await client.submit_in_browser(
            phone=phone, password=password,
            room_id=cfg.library.room_id, seat_num=seat_num,
            day=day, start_time=start, end_time=end,
        )
        dt = (datetime.now() - t0).total_seconds()
        print(
            f"[oneoff] {dt:.1f}s success={r.get('success')} "
            f"reserve_id={r.get('reserve_id')} msg={r.get('msg')}"
        )
        print(f"[oneoff] raw={r.get('raw')}")
        return 0 if r.get("success") else 1
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
