"""按星期几的时段配置工具：双形态（全周 list / 按天 dict）归一与取值。

形态 A（全周统一）: list[str]，如 ["09:00-11:00"]
形态 B（按天独立）: dict[mon..sun, list[str] | "full"]，缺天 = 该天无时段
"""
from __future__ import annotations

from datetime import date
from typing import Any

WEEKDAY_KEYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

WEEKDAY_LABELS: dict[str, str] = {
    "mon": "周一", "tue": "周二", "wed": "周三", "thu": "周四",
    "fri": "周五", "sat": "周六", "sun": "周日",
}


def weekday_key(day: date) -> str:
    """日期 → 星期键（mon..sun，与 date.weekday() 0=周一 对应）。"""
    return WEEKDAY_KEYS[day.weekday()]


def _norm_day_value(v: Any, *, allow_full: bool) -> list[str] | str:
    """归一单天时段值：list[str] 原样拷贝；"full" 仅 allow_full 时合法。"""
    if isinstance(v, str):
        if allow_full and v == "full":
            return "full"
        raise ValueError("时段必须是字符串数组或按星期的对象")
    if isinstance(v, list):
        return [str(x) for x in v]
    if v is None:
        return []
    raise ValueError("时段必须是字符串数组或按星期的对象")


def normalize_weekly(
    value: Any, *, allow_full: bool = False,
) -> dict[str, list[str] | str] | None:
    """归一为 7 键显式 dict（缺失天补 []）；None 原样返回。

    value 允许：None / list[str]（全周统一）/ "full"（仅 allow_full）/
    dict[星期键, list[str] | "full" | None]。非法星期键或非法天数抛 ValueError。
    """
    if value is None:
        return None
    if isinstance(value, str):
        day = _norm_day_value(value, allow_full=allow_full)
        return {w: day for w in WEEKDAY_KEYS}
    if isinstance(value, list):
        day = _norm_day_value(value, allow_full=allow_full)
        return {w: list(day) for w in WEEKDAY_KEYS}
    if isinstance(value, dict):
        out: dict[str, list[str] | str] = {}
        for k, v in value.items():
            key = str(k).strip().lower()
            if key not in WEEKDAY_KEYS:
                raise ValueError(f"非法星期键: {k!r}")
            out[key] = _norm_day_value(v, allow_full=allow_full)
        return {w: out.get(w, []) for w in WEEKDAY_KEYS}
    raise ValueError("时段必须是字符串数组或按星期的对象")


def slots_for_weekday(value: Any, wd: str) -> list[str] | str:
    """取某天的时段：形态 A 全周统一原样；dict 取该天（缺/None → []）。"""
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        v = value.get(wd)
        if v is None:
            return []
        return v
    raise ValueError(f"非法时段形态: {type(value).__name__}")
