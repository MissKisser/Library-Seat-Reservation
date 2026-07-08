"""Try to reserve 21:00-22:00 seat 84 with test account 151XXXX0087.

Uses the *real* enc value captured from the production browser via
extract_enc.py earlier in this session.

Plan:
  [1] login headless
  [2] submit_reserve(roomId=11692, day=today, startTime=21:00, endTime=22:00, enc=<captured>)
  [3] If success → cancel immediately so the seat is not held
  [4] Print final status
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date

from seatbot.client import ChaoxingClient, ChaoxingError


PHONE = "151XXXX0087"
PASSWORD = "[已清除]"
ROOM_ID = 11692
SEAT_NUM = "84"

# Captured from a real /submit via Playwright on 2026-07-08
# Note: enc values are time-bound (often expire within minutes).
# If this no longer works, re-run extract_enc.py to refresh.
CAPTURED_ENC = "007eefc29015f08020bb57ca9a156ac0"


async def main() -> int:
    client = ChaoxingClient()
    try:
        print(f"[1/3] login {PHONE}...")
        await client.login(PHONE, PASSWORD)
        cookies = client.cookies()
        auth = [k for k in ("_uid", "vc3") if k in cookies]
        if not auth:
            raise ChaoxingError(f"login failed: {list(cookies)}")
        print(f"  OK; auth cookies: {auth}")

        today = date.today().isoformat()
        print(f"[2/3] submit_reserve roomId={ROOM_ID} seatNum={SEAT_NUM} "
              f"day={today} 21:00-22:00...")
        try:
            r = await client.submit_reserve(
                room_id=ROOM_ID,
                day=today,
                start_time="21:00",
                end_time="22:00",
                seat_num=SEAT_NUM,
                enc=CAPTURED_ENC,
                wy_token="",
            )
            print(f"  raw response: {r}")
        except ChaoxingError as e:
            print(f"  submit error: {e}")
            return 1

        if not r.get("success"):
            print(f"  SUBMIT REJECTED: msg={r.get('msg')!r}")
            return 2

        data = r.get("data") or {}
        sr = data.get("seatReserve") or {}
        reserve_id = sr.get("id")
        print(f"  SUBMIT OK ✓  reserve_id={reserve_id}  seat={sr.get('seatNum')}  "
              f"start={sr.get('startTime')} end={sr.get('endTime')}")

        if not reserve_id:
            print("  no reserve_id in response; nothing to cancel")
            return 3

        print(f"[3/3] cancel reservation #{reserve_id} so seat remains free...")
        cr = await client.cancel(reserve_id)
        print(f"  cancel response: {cr}")
        if cr.get("success"):
            print("  CANCEL OK ✓")
            return 0
        print("  CANCEL FAILED")
        return 4

    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
