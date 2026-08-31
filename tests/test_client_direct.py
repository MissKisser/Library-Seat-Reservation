"""submit_direct 直连提交通道单测。

覆盖:
  - enc = md5(按 key 排序的 "[k=v]" 拼接 + "[种子]") 全字段重算
  - 成功回包解析出 reserve_id
  - 座位页无种子 (如会话失效跳登录页) → 明确失败且不发提交请求
"""
from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

from seatbot.client import ChaoxingClient

SEED = "36caf3080081471094de5efb325c5b20_314500212"
PAGE_HTML = f'<input type="hidden" id="submit_enc" value="{SEED}">'


def _make_client(monkeypatch, html: str) -> ChaoxingClient:
    client = ChaoxingClient()
    client.set_cookies({"_uid": "1", "vc3": "x"})

    async def fake_get(url, **kw):
        return SimpleNamespace(text=html)

    monkeypatch.setattr(client._client, "get", fake_get)
    return client


def _run(client: ChaoxingClient) -> dict:
    return asyncio.run(client.submit_direct(
        phone="13800000000", password="pw",
        room_id=11692, seat_num="030",
        day="2026-09-01", start_time="14:00", end_time="16:00",
    ))


def test_submit_direct_signs_full_field_set(monkeypatch):
    client = _make_client(monkeypatch, PAGE_HTML)
    posted: dict = {}

    async def fake_post_form(url, data, *, referer=None):
        posted.update(data)
        posted["_url"] = url
        posted["_referer"] = referer
        return {"success": True, "data": {"seatReserve": {"id": 42}}}

    monkeypatch.setattr(client, "_post_form", fake_post_form)

    result = _run(client)

    fields = {
        "roomId": "11692",
        "day": "2026-09-01",
        "startTime": "14:00",
        "endTime": "16:00",
        "seatNum": "030",
        "captcha": "",
        "type": "1",
        "verifyData": "1",
        "wyToken": "",
    }
    concat = "".join(f"[{k}={fields[k]}]" for k in sorted(fields))
    expected = hashlib.md5((concat + f"[{SEED}]").encode("utf-8")).hexdigest()
    assert posted["enc"] == expected
    assert posted["seatNum"] == "030" and posted["day"] == "2026-09-01"
    assert posted["_url"].endswith("/data/apps/seat/submit")
    assert "seatNum=030" in posted["_referer"]
    assert result["success"] and result["reserve_id"] == 42
    assert result["channel"] == "direct"


def test_submit_direct_missing_seed_fails_without_post(monkeypatch):
    client = _make_client(monkeypatch, "<html>passport login page</html>")

    async def fail_post(*a, **kw):
        raise AssertionError("seed 缺失时不应发提交请求")

    monkeypatch.setattr(client, "_post_form", fail_post)

    result = _run(client)
    assert not result["success"] and result["reserve_id"] is None
    assert "seed not found" in (result["msg"] or "")


def test_submit_direct_rejected_payload_maps_to_failure(monkeypatch):
    client = _make_client(monkeypatch, PAGE_HTML)

    async def fake_post_form(url, data, *, referer=None):
        return {"success": False, "msg": "该时段已被预约"}

    monkeypatch.setattr(client, "_post_form", fake_post_form)

    result = _run(client)
    assert not result["success"]
    assert result["msg"] == "该时段已被预约"
