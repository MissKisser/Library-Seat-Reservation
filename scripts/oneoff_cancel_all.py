"""清空所有账号的服务端预约: 拉取 reservelist → cancel 未完结项 → 复查。

用法:
  .venv/Scripts/python.exe scripts/oneoff_cancel_all.py <account_id> [<account_id> ...]

- 会话: 优先 .session_cache/<acct>.json 复用; 缺失/失效则全新登录 (client.py 已含
  协议弹窗修复) 并回写缓存。
- 只 cancel status ∈ {0 待履约, 1 使用中} 的记录; 已结束的历史记录 (2/7) 不动。
- 全程只读 DB (凭证), 不写 tasks/actions 表。

⚠️ 高危: 对真实账号执行取消操作, 仅当用户在当前会话明确要求时运行。
"""
import asyncio
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from seatbot.config import load_config  # noqa: E402
from seatbot.client import ChaoxingClient  # noqa: E402
from session_cache import authenticated_client, save  # noqa: E402

STATUS_MEANING = {0: "待履约", 1: "使用中", 2: "已履约", 7: "已取消/违约"}


def get_creds(cfg, acct: str) -> tuple[str, str]:
    con = sqlite3.connect(f"file:{cfg.runtime.db_path}?mode=ro", uri=True)
    row = con.execute(
        "SELECT phone, password FROM accounts WHERE id=?", (acct,)
    ).fetchone()
    con.close()
    if row is None:
        raise SystemExit(f"[cancel] account {acct!r} not found in DB")
    return row


async def fetch_reservelist(client: ChaoxingClient) -> list[dict]:
    r = await client._client.get(
        f"{client.OFFICE_BASE}/data/apps/seat/reservelist",
        params={"indexId": 0, "pageSize": 20, "type": -1,
                "fidEnc": client.FID_ENC_MOBILE},
        headers={"Referer": client.OFFICE_BASE + "/"},
    )
    payload = r.json()
    if not payload.get("success"):
        raise RuntimeError(f"reservelist failed: {payload}")
    return (payload.get("data") or {}).get("reserveList") or []


async def process_account(cfg, acct: str) -> None:
    phone, password = get_creds(cfg, acct)

    async def probe(client):
        await client.get_used_times(
            cfg.library.room_id, "104", date.today().isoformat())
        return True

    client, src = await authenticated_client(acct, phone, password, probe)
    print(f"[cancel] {acct}: session={src}")
    try:
        items = await fetch_reservelist(client)
        for it in items:
            print(f"[cancel]   {acct} 持有: id={it.get('id')} seat={it.get('seatNum')} "
                  f"status={it.get('status')}({STATUS_MEANING.get(it.get('status'), '?')}) "
                  f"day={it.get('today')} start={it.get('startTime')} end={it.get('endTime')}")
        targets = [it for it in items if it.get("status") in (0, 1)]
        for it in targets:
            r = await client.cancel(it["id"])
            print(f"[cancel]   {acct} cancel {it['id']} -> {r}")
        remain = await fetch_reservelist(client)
        leftover = [it for it in remain if it.get("status") in (0, 1)]
        print(f"[cancel] {acct}: 未完结预约剩余 {len(leftover)} 条"
              + (f" -> {[it['id'] for it in leftover]}" if leftover else " ✓ 已清零"))
    finally:
        save(acct, client)
        await client.close()


async def main() -> int:
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    for acct in sys.argv[1:]:
        try:
            await process_account(cfg, acct)
        except Exception as e:
            print(f"[cancel] {acct}: ERROR {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
