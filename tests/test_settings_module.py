"""settings 模块单测。

覆盖:
  - effective 三层合并: DEFAULTS → YAML → DB 覆盖
  - 旧 direct_submit_enabled=false 布尔兼容 (DB 未覆盖时映射 page_rewrite_only)
  - validate_all 校验: 未知键 / 越界值 / webhook 协议
  - 存取往返: normalize → coerce → parse_stored 还原类型
"""
from __future__ import annotations

from seatbot import settings as _settings
from seatbot.config import Config, LibraryConfig, RuntimeConfig


def _cfg(**runtime_kw) -> Config:
    return Config(
        library=LibraryConfig(
            room_id=11692, room_name="t",
            max_reserve_hours=2.0, daily_reserve_hours_limit=5.0,
        ),
        runtime=RuntimeConfig(stagger_seconds=[0, 3], **runtime_kw),
    )


def test_effective_db_overrides_yaml():
    cfg = _cfg(submit_strategy="direct_first", relay_lead_seconds=300)
    rows = {"submit_strategy": "direct_only", "relay_lead_seconds": "600"}
    eff = _settings.effective(rows, cfg)
    assert eff["submit_strategy"] == "direct_only"
    assert eff["relay_lead_seconds"] == 600
    assert eff["stagger_seconds"] == [0, 3]     # 未覆盖 → YAML 种子
    assert eff["max_reserve_hours"] == 2.0      # 未覆盖 → YAML 种子


def test_legacy_direct_submit_disabled_maps_to_page_rewrite_only():
    cfg = _cfg(direct_submit_enabled=False)
    assert _settings.effective({}, cfg)["submit_strategy"] == "page_rewrite_only"
    # DB 已显式覆盖时不被旧布尔改写
    rows = {"submit_strategy": "direct_only"}
    assert _settings.effective(rows, cfg)["submit_strategy"] == "direct_only"


def test_validate_all_rejects_unknown_key_and_bad_range():
    errors = _settings.validate_all({"no_such_key": "1", "anchor_scan_limit": "99"})
    assert "no_such_key" in errors and "anchor_scan_limit" in errors
    assert _settings.validate_all({"anchor_scan_limit": "12"}) == {}


def test_notify_webhook_requires_http_scheme():
    assert _settings.validate_all({"notify_webhook": ""}) == {}
    assert "notify_webhook" in _settings.validate_all({"notify_webhook": "ftp://x"})


def test_storage_roundtrip_typed_values():
    assert _settings.parse_stored(
        "stagger_seconds", _settings.coerce_for_storage("stagger_seconds", [2, 5]), None,
    ) == [2, 5]
    assert _settings.parse_stored("anchor_retry_enabled", "true", False) is True
    assert _settings.parse_stored("relay_lead_seconds", "180", 300) == 180
