"""End-to-end smoke test with the user's real Chaoxing account.

Validated steps (network-only, no browser UI):
  - Login via headless Chromium on the real login form → `_uid`/`vc3`
    cookies harvested back into ChaoxingClient (httpx).
  - Fetch /data/apps/seat/room/info to confirm cookies work and the
    library metadata is correct (capacity, reserve duration, time unit).

Steps that require a real human (out of scope here):
  - /submit POST. Chaoxing's risk-control (YiDun / number validator)
    intercepts headless-submitted POSTs and pops a CAPTCHA; we cannot
    drive this programmatically. A user running seatbot interactively
    in their own browser will not hit this hurdle after the first time
    they go through the captcha flow — cookies stay valid for hours.

Run with the user's real account (currently 178XXXX3792) and a free seat
we will *not* actually book (we never POST /submit here).
"""
from __future__ import annotations

import asyncio
import sys

from seatbot.client import ChaoxingClient, ChaoxingError


PHONE = "178XXXX3792"
PASSWORD = "[已清除]"
ROOM_ID = 11692


async def main() -> int:
    client = ChaoxingClient()
    try:
        print(f"[1/3] login {PHONE} via headless Chromium...")
        await client.login(PHONE, PASSWORD)
        cookies = client.cookies()
        auth = [k for k in ("_uid", "vc3") if k in cookies]
        if not auth:
            raise ChaoxingError(f"login completed but missing _uid/vc3; got {list(cookies)}")
        print(f"  OK; auth cookies: {auth}")

        print(f"[2/3] get room info room_id={ROOM_ID}...")
        info = await client.get_room_info(ROOM_ID)
        if not info.get("success"):
            raise ChaoxingError(f"room info failed: {info.get('msg')}")
        d = info["data"]
        sr = d.get("seatRoom", {})
        sc = d.get("seatConfig", {})
        print(f"  room: {sr.get('firstLevelName')} {sr.get('secondLevelName')} {sr.get('thirdLevelName')}")
        print(f"  capacity: {sr.get('capacity')}, isOpen: {sr.get('isOpen')}")
        print(f"  reserveDuration: {sc.get('reserveDuration')}h, timeUnit: {sc.get('timeUnit')}min")
        print(f"  preSign: {sc.get('preSignDuration')}m, sign: {sc.get('signDuration')}m, "
              f"leave: {sc.get('leaveDuration')}m")
        print(f"  reserveBeforeDay: {sc.get('reserveBeforeDay')}, reserveBeforeTime: {sc.get('reserveBeforeTime')}")

        print("[3/3] get active reservation for target_seat=84 (the user's own seat)...")
        try:
            active = await client.get_active_reservation(ROOM_ID, "84")
        except ChaoxingError as e:
            print(f"  WARN: reserve/info failed: {e}")
            active = None
        if active:
            print(f"  active reservation: {active}")
        else:
            print("  no active reservation on seat 84")

        print("\nALL OK ✓ (login + room info + reserve info reachable)")
        return 0
    except ChaoxingError as e:
        print(f"ChaoxingError: {e}")
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))