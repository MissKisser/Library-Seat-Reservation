import re

from seatbot.utils.ua import random_ua, CHROME_UAS, MOBILE_UAS


def test_pool_nonempty():
    assert len(CHROME_UAS) >= 5
    assert len(MOBILE_UAS) >= 3


def test_random_ua_returns_valid_string():
    for _ in range(50):
        ua = random_ua()
        assert isinstance(ua, str)
        assert re.match(r"^Mozilla/\d+\.\d+", ua)


def test_random_ua_can_be_desktop_or_mobile():
    seen = {random_ua() for _ in range(100)}
    assert any("Chrome" in u or "Safari" in u for u in seen)
