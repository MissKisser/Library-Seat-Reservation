"""Integration test for ChaoxingClient.login.

Run with: pytest tests/test_client_login.py -v -m integration
Requires real credentials via env SEATBOT_TEST_PHONE / SEATBOT_TEST_PASSWORD.
"""
import os

import pytest

from seatbot.client import ChaoxingClient, ChaoxingError


@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_success():
    phone = os.environ.get("SEATBOT_TEST_PHONE")
    password = os.environ.get("SEATBOT_TEST_PASSWORD")
    if not phone or not password:
        pytest.skip("set SEATBOT_TEST_PHONE / SEATBOT_TEST_PASSWORD to run")

    client = ChaoxingClient()
    try:
        await client.login(phone, password)
        # cookies should be set; second call should not re-login
        cookies = client.cookies()
        assert any("_uid" in c.name or "vc3" in c.name for c in client._cookie_jar.jar)
    finally:
        await client.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_failure_wrong_password():
    client = ChaoxingClient()
    try:
        with pytest.raises(ChaoxingError):
            await client.login("13800000000", "wrong_password_xyz")
    finally:
        await client.close()