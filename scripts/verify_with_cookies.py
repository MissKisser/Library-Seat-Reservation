"""Full-chain verification — uses real cookies from the production browser
captured via Playwright CDP (including HttpOnly ones).

Run:
    .venv/Scripts/python.exe scripts/verify_with_cookies.py
"""
from __future__ import annotations

import asyncio
import json
import sys

from seatbot.client import ChaoxingClient


# Captured via Playwright CDP on 2026-07-08 (session active for user 熊金涛 / _uid=314500121)
COOKIE_PAIRS = {
    "fid": "29318",
    "xxtenc": "360f22b5095a0b3ac9e1ab28d4a8364f",
    "route": "fb0878d2b253f576b9614a77ccc901db",
    "retainlogin": "2",
    "_uid": "314500121",
    "UID": "314500121",
    "oa_deptid": "29318",
    "oa_uid": "314500121",
    "oa_name": "%E7%86%8A%E9%87%91%E6%B6%9B",
    "oa_enc": "af6ab19e6a66ce14939af1a9e0a94164",
    "JSESSIONID": "02902F238CF4137B6B055927B86603CC.reserve_web_127",
    "_d": "1783511432302",
    "vc3": "d96Fx%2BZZBvqu2%2B6hrm35GplGFnBQ8Dud2%2FKSsQelOs8lOAbS%2BT%2FjNmYtk%2FVKQrMIy5hWyiX5kLE8Sa3Qglwr3Qt8We4w3a5oOtUoU5Jt6XRHfnoAx7rKApZ26%2BJ%2BM8dQGUzV3WFdE7YSG%2Bzo07lsUsHYRuptmTsqh%2F2su0wfN3Y%3Ddcc909d2ac806e25c1c130f64bb02556",
    "uf": "da0883eb5260151e79902f2270f8522c2218ad8d04b27b93fb2f696650b4ebc9333f816b0d8c6da891d9b9463ac503ed2288078c94f43c22c49d67c0c30ca5043ad701c8b4cc548c0234d89f51c3dccfc08eb3faa4eca57d713028f1ec42bf71b1188854805578cc7d27fc781beae33f57aa6c780f06647cf05acb2ee93230c521cfbeb0861a6a56bc86acebc60a639c4df7ff280fcb29d10d8a4c92b12beb4b0026218f561027acaef047ad3aaf7da02c45595b0e2f018de7fafd565af53bf2",
    "cx_p_token": "43b594256d46150f3af36ffa248a2a6b",
    "p_auth_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1aWQiOiIzMTQ1MDAxMjEiLCJsb2dpblRpbWUiOjE3ODM1MTE0MzIzMDMsImV4cCI6MTc4NDExNjIzMn0._Ctkd7NRCVHjkmV_ndS1Fz3Pn3CvF8fCD8k_8GaFNGQ",
    "DSSTASH_LOG": "C_38-UN_558-US_314500121-T_1783511432304",
}

ROOM_ID = 11692
TARGET_SEAT = "84"


def inject_cookies(client: ChaoxingClient, cookies: dict[str, str]) -> None:
    """Inject cookies directly into the httpx cookie jar (skip login)."""
    for name, value in cookies.items():
        # Pick a generic .chaoxing.com domain so server-side per-domain
        # cookies (office.chaoxing.com, passport2.chaoxing.com) all match.
        if name in ("oa_deptid", "oa_uid", "oa_name", "oa_enc", "JSESSIONID"):
            domain = "office.chaoxing.com"
        elif name in ("route", "retainlogin"):
            domain = "passport2.chaoxing.com"
        else:
            domain = ".chaoxing.com"
        client._cookie_jar.set(name, value, domain=domain, path="/")


async def main() -> int:
    client = ChaoxingClient()
    try:
        inject_cookies(client, COOKIE_PAIRS)
        have = set(client.cookies())
        print(f"Injected {len(have)} cookies: {sorted(have)[:5]}...")

        print(f"\n[1/5] get_room_info room_id={ROOM_ID}...")
        info = await client.get_room_info(ROOM_ID)
        if not info.get("success"):
            print(f"  FAIL: {info.get('msg')}")
            return 1
        sr = info["data"]["seatRoom"]
        sc = info["data"]["seatConfig"]
        print(f"  OK: {sr.get('firstLevelName')} {sr.get('secondLevelName')} {sr.get('thirdLevelName')}")
        print(f"      capacity={sr.get('capacity')} isOpen={sr.get('isOpen')}")
        print(f"      reserveDuration={sc.get('reserveDuration')}h timeUnit={sc.get('timeUnit')}min")

        print(f"\n[2/5] get_seat_status room_id={ROOM_ID}...")
        try:
            seats = await client.get_seat_status(ROOM_ID)
            cnt = len(seats) if isinstance(seats, list) else "N/A"
            print(f"  OK: {cnt} seats")
        except Exception as e:
            print(f"  WARN: {type(e).__name__}: {e}")

        print(f"\n[3/5] get_active_reservation target_seat={TARGET_SEAT}...")
        active = await client.get_active_reservation(ROOM_ID, TARGET_SEAT)
        if active:
            print(f"  ACTIVE on seat {TARGET_SEAT}:")
            print(f"    id={active.get('id')}")
            print(f"    start={active.get('startTime')} end={active.get('endTime')}")
            print(f"    raw: {json.dumps(active, ensure_ascii=False, default=str)[:600]}")
        else:
            print(f"  no active reservation on seat {TARGET_SEAT}")

        print("\n[4/5] GET seat detail page HTML...")
        r = await client._client.get(
            f"{client.OFFICE_BASE}/front/apps/seat/code",
            params={"id": ROOM_ID, "seatNum": TARGET_SEAT},
            headers={"Referer": f"{client.OFFICE_BASE}/front/apps/seat/list"},
        )
        body = r.text
        print(f"  HTTP {r.status_code} {len(body)} bytes")
        print(f"  contains '当前预约' (logged-in indicator): {'当前预约' in body or '正在使用' in body}")
        print(f"  contains '登录' (login page indicator): {'登录' in body}")

        print("\n[5/5] GET seat list page (calendar/dates view)...")
        r = await client._client.get(
            f"{client.OFFICE_BASE}/front/apps/seat/list",
            headers={"Referer": client.OFFICE_BASE},
        )
        body = r.text
        print(f"  HTTP {r.status_code} {len(body)} bytes")
        is_login = "登录" in body and "正在使用" not in body
        print(f"  logged-in?: {'yes' if not is_login else 'no'}")

        print("\nALL OK ✓ (every read-only endpoint reachable with real session)")
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
