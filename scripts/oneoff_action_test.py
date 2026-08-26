"""一次性签到/签退/取消测试: 对指定 reserve_id 执行 action。

用法:
  .venv/Scripts/python.exe scripts/oneoff_action_test.py <sign|leave|cancel> <account_id> <reserve_id>

特点:
  - 不经过 scheduler、不写 seatbot.db (凭证只读)
  - lazy login (Playwright 登录取 cookies) + httpx 调 action 端点,
    与 scheduler._run_sign/_run_leave 相同路径

⚠️ 高危: 会用真实账号登录超星并发起真实签到/签退/取消请求。
   依据 AGENTS.md 第一条禁令, 仅当用户在当前会话明确要求时才可运行。
"""
import asyncio
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seatbot.client import ChaoxingClient, ChaoxingError  # noqa: E402
from seatbot.config import load_config  # noqa: E402


async def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    action, account_id, reserve_id = sys.argv[1], sys.argv[2], int(sys.argv[3])
    if action not in ("sign", "leave", "cancel"):
        print(f"[oneoff] unknown action {action!r}")
        return 2

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

    client = ChaoxingClient()
    try:
        print(f"[oneoff] login account={account_id} ...")
        try:
            await client.login(phone, password)
            print(f"[oneoff] login ok ({len(client.cookies())} cookies)")
        except ChaoxingError as e:
            print(f"[oneoff] login failed: {e}")
            return 1

        fn = {"sign": client.sign, "leave": client.leave, "cancel": client.cancel}[action]
        print(f"[oneoff] {action} reserve_id={reserve_id} ...")
        r = await fn(reserve_id)
        print(f"[oneoff] response={r}")
        return 0 if r.get("success") else 1
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
