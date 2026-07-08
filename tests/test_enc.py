import pytest

from seatbot.enc import EncGenerator, EncError


@pytest.mark.asyncio
async def test_enc_generator_returns_dict(monkeypatch):
    """We mock the underlying JS execution to return a known payload."""
    gen = EncGenerator()

    async def fake_compute(room_id, seat_num, day, start, end):
        return {"enc": "fake_enc_value", "wyToken": "fake_token_value"}

    monkeypatch.setattr(gen, "_compute_js", fake_compute)
    out = await gen.compute(room_id=11692, seat_num="084", day="2026-07-09",
                            start_time="10:00", end_time="11:00")
    assert out["enc"] == "fake_enc_value"
    assert out["wyToken"] == "fake_token_value"


@pytest.mark.asyncio
async def test_enc_generator_caches_within_ttl(monkeypatch):
    gen = EncGenerator(ttl_seconds=60)
    call_count = {"n": 0}

    async def fake_compute(*a, **kw):
        call_count["n"] += 1
        return {"enc": "x", "wyToken": "y"}

    monkeypatch.setattr(gen, "_compute_js", fake_compute)
    args = dict(room_id=11692, seat_num="084", day="2026-07-09",
                start_time="10:00", end_time="11:00")
    await gen.compute(**args)
    await gen.compute(**args)
    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_enc_generator_raises_on_failure(monkeypatch):
    gen = EncGenerator()
    async def boom(*a, **kw):
        raise RuntimeError("JS exec failed")
    monkeypatch.setattr(gen, "_compute_js", boom)
    with pytest.raises(EncError):
        await gen.compute(room_id=1, seat_num="1", day="2026-07-09",
                          start_time="10:00", end_time="11:00")
