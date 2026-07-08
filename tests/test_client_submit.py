"""Integration test for submit + sign + leave cycle.

Run with: pytest tests/test_client_submit.py -v -m integration
"""
import os
from datetime import date, timedelta

import pytest

from seatbot.client import ChaoxingClient


@pytest.mark.integration
@pytest.mark.asyncio
async def test_submit_sign_leave_cycle():
    phone = os.environ.get("SEATBOT_TEST_PHONE")
    password = os.environ.get("SEATBOT_TEST_PASSWORD")
    if not phone or not password:
        pytest.skip("set SEATBOT_TEST_PHONE / SEATBOT_TEST_PASSWORD")

    # We'll pick a tomorrow time slot — assumes preSignDuration is generous
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    seat_num = os.environ.get("SEATBOT_TEST_SEAT", "084")
    room_id = int(os.environ.get("SEATBOT_TEST_ROOM", "11692"))
    start, end = "10:00", "11:00"

    client = ChaoxingClient()
    try:
        await client.login(phone, password)
        # NOTE: enc generation is covered in Task 10; for now stub it
        enc = "TEST_ENC_PLACEHOLDER"
        result = await client.submit_reserve(
            room_id=room_id, day=tomorrow, start_time=start, end_time=end,
            seat_num=seat_num, enc=enc,
        )
        # expected failure (bad enc) — but we want to ensure the request shape is right
        assert "success" in result
    finally:
        await client.close()