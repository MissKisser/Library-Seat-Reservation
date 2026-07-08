"""Verify ChaoxingClient.submit_in_browser() against the production server.

This wraps the entire headless click → /submit flow into one seatbot API
call. After this works we will plug submit_in_browser into the scheduler's
_run_submit_sign() and the system becomes deployable end-to-end.

Test plan:
  [1] login headless into the existing ChaoxingClient
  [2] call client.submit_in_browser() to reserve 21:00-22:00 seat 84
  [3] if success, immediately call client.sign() and client.leave()
  [4] print summary
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date

from seatbot.client import ChaoxingClient


PHONE = "151XXXX0087"
PASSWORD = "[已清除]"
ROOM_ID = 11692
SEAT_NUM = "84"


async def main() -> int:
    client = ChaoxingClient()
    try:
        print(f"[1/4] login headless {PHONE}...")
        await client.login(PHONE, PASSWORD)
        cookies = client.cookies()
        if not ({"_uid", "vc3"} & set(cookies)):
            print(f"  FAIL — no auth cookies: {list(cookies)}")
            return 1
        print(f"  OK; auth cookie keys: {sorted(k for k in cookies if k in ('_uid','vc3'))}")

        today = date.today().isoformat()
        print(f"[2/4] client.submit_in_browser(seat=84, {today} 21:00-22:00)...")
        r = await client.submit_in_browser(
            phone=PHONE,
            password=PASSWORD,
            room_id=ROOM_ID,
            seat_num=SEAT_NUM,
            day=today,
            start_time="21:00",
            end_time="22:00",
        )
        print(f"  result: success={r['success']} reserve_id={r['reserve_id']} msg={r['msg']!r}")
        if not r["success"]:
            return 2
        reserve_id = r["reserve_id"]

        print(f"[3/4] sign + leave reservation #{reserve_id}...")
        s = await client.sign(reserve_id)
        print(f"  sign: {s}")
        if not s.get("success"):
            return 3
        lv = await client.leave(reserve_id)
        print(f"  leave: {lv}")
        if not lv.get("success"):
            return 4

        print("[4/4] verify clean state...")
        active = await client.get_active_reservation(ROOM_ID, SEAT_NUM)
        if active:
            print(f"  WARN still active: {active.get('id')}")
            return 5
        print("  84 free ✓")
        print("\nALL OK ✓ (in-browser submit + sign + leave all green)")
        return 0
    except Exception as e:
        print(f"\nERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
