"""E2E test using the user's *new* test account 151XXXX0087.

Tests login + room info + reserve state. Does NOT POST /submit (YiDun).
"""
from __future__ import annotations

import asyncio
import sys

from seatbot.client import ChaoxingClient, ChaoxingError


PHONE = "151XXXX0087"
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
            raise ChaoxingError(f"login failed: missing _uid/vc3; got {list(cookies)}")
        print(f"  OK; auth cookies: {auth}")

        print(f"[2/3] get room info room_id={ROOM_ID}...")
        info = await client.get_room_info(ROOM_ID)
        if not info.get("success"):
            raise ChaoxingError(f"room info failed: {info.get('msg')}")
        sr = info["data"]["seatRoom"]
        sc = info["data"]["seatConfig"]
        print(f"  room: {sr.get('firstLevelName')} {sr.get('secondLevelName')} {sr.get('thirdLevelName')}")
        print(f"  capacity: {sr.get('capacity')}, isOpen: {sr.get('isOpen')}")
        print(f"  reserveDuration: {sc.get('reserveDuration')}h, timeUnit: {sc.get('timeUnit')}min")
        print(f"  preSign: {sc.get('preSignDuration')}m, sign: {sc.get('signDuration')}m, "
              f"leave: {sc.get('leaveDuration')}m")

        print("[3/3] get my own active reservation (any seat)...")
        try:
            active = await client.get_active_reservation(ROOM_ID, "84")
        except Exception as e:
            active = None
            print(f"  WARN reserve/info seat 84: {e}")
        if active:
            print(f"  active on seat 84: id={active.get('id')} "
                  f"{active.get('startTime')}-{active.get('endTime')}")
        else:
            print("  no active reservation on seat 84")

        print("\nALL OK ✓ (login + room info reachable with new test account)")
        return 0
    except ChaoxingError as e:
        print(f"\nChaoxingError: {e}")
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
