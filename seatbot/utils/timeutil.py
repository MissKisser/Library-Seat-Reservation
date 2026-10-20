"""Time helpers: HH:MM parsing, slot expansion, CST."""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8), name="CST")

#: 超星次日预约开放时刻（CST 14:00）。/manual 等"提交未来预约"的入口
#  在 14:00 之前禁止选择明天，避免抢预约窗口未开导致的盲提交。
RESERVE_WINDOW_HOUR = 14


def now_cst() -> datetime:
    """返回当前北京时间（带时区）。"""
    return datetime.now(CST)


def today_cst() -> date:
    """返回当前北京日期。"""
    return now_cst().date()


def at_cst(d: date, t: time) -> datetime:
    """Combine a date and a time into a tz-aware CST datetime.

    Use this whenever you need to compare a (date, time-of-day) pair
    with anything `now_cst()` produces. Bare `datetime.combine()` produces
    a naive datetime which raises TypeError on comparison.
    """
    return datetime.combine(d, t, tzinfo=CST)


def epoch_ms_to_cst_str(value: int | float | None) -> str:
    """epoch 毫秒 → 'YYYY-MM-DD HH:MM:SS' (CST)。"""
    if not value:
        return "—"
    try:
        dt = datetime.fromtimestamp(int(value) / 1000, tz=CST)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        logger.debug("epoch 毫秒 %r 无法转换为 CST 时间: %s", value, e)
        return "—"

def parse_hhmm(s: str) -> time:
    """Parse 'HH:MM' into a time. Reject 'H:MM' (must be 2-digit hour)."""
    parts = s.split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid HH:MM: {s!r}")
    h, m = parts
    if len(h) != 2 or len(m) != 2:
        raise ValueError(f"HH and MM must be 2 digits: {s!r}")
    return time(int(h), int(m))


def parse_range(r: str) -> tuple[time, time]:
    """Parse 'HH:MM-HH:MM' into (start, end). end must be > start."""
    if r.count("-") != 1:
        raise ValueError(f"invalid range: {r!r}")
    s, e = r.split("-")
    start, end = parse_hhmm(s.strip()), parse_hhmm(e.strip())
    if end <= start:
        raise ValueError(f"end must be after start: {r!r}")
    return start, end


def overlaps(a_start, a_end, b_start, b_end) -> bool:
    """判断两个半开区间 [a_start, a_end) 与 [b_start, b_end) 是否相交。

    入参:
        a_start: 第一个区间的开始时刻（datetime.time 或 datetime.datetime）。
        a_end: 第一个区间的结束时刻。
        b_start: 第二个区间的开始时刻。
        b_end: 第二个区间的结束时刻。

    返回值:
        两区间有交集时返回 True；端点相接（前一段结束时刻等于后一段开始
        时刻）不算重叠，返回 False。
    """
    return a_start < b_end and b_start < a_end


def split_into_chunks(
    range_: tuple[time, time], max_hours: float
) -> list[tuple[time, time]]:
    """Split a (start, end) range into chunks of at most max_hours.

    Raises ValueError if max_hours does not yield at least one whole minute,
    which would make the chunk cursor unable to advance.
    """
    if max_hours <= 0:
        raise ValueError(f"max_hours must be > 0, got {max_hours!r}")
    start, end = range_
    start_min = start.hour * 60 + start.minute
    end_min = end.hour * 60 + end.minute
    chunk_min = int(max_hours * 60)
    if chunk_min < 1:
        raise ValueError(
            f"max_hours must be at least 1 minute, got {max_hours!r}"
        )
    chunks: list[tuple[time, time]] = []
    cur = start_min
    while cur < end_min:
        nxt = min(cur + chunk_min, end_min)
        sh, sm = divmod(cur, 60)
        eh, em = divmod(nxt, 60)
        chunks.append((time(sh, sm), time(eh, em)))
        cur = nxt
    return chunks


def expand_full_day(
    open_hhmm: str, close_hhmm: str, max_hours: float
) -> list[tuple[time, time]]:
    """Expand the full operating day into chunks of at most max_hours."""
    return split_into_chunks(
        (parse_hhmm(open_hhmm), parse_hhmm(close_hhmm)),
        max_hours=max_hours,
    )


def expand_account_slots(
    slots: str | list[str],
    max_hours: float,
    open_time: str = "08:00",
    close_time: str = "22:00",
) -> list[tuple[time, time]]:
    """Expand a SlotSpec ('full' or list of 'HH:MM-HH:MM') into chunks.

    "full" 按馆舍 open_time/close_time 展开；显式 range 原样分块。
    """
    if slots == "full":
        return expand_full_day(open_time, close_time, max_hours=max_hours)
    return [c for r in slots for c in split_into_chunks(parse_range(r), max_hours)]