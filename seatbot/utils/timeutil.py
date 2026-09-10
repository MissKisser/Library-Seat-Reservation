"""Time helpers: HH:MM parsing, slot expansion, CST."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone


CST = timezone(timedelta(hours=8), name="CST")


def now_cst() -> datetime:
    return datetime.now(CST)


def today_cst() -> date:
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
    except Exception:
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


def split_into_chunks(
    range_: tuple[time, time], max_hours: float
) -> list[tuple[time, time]]:
    """Split a (start, end) range into chunks of at most max_hours.

    Raises ValueError if max_hours is not positive.
    """
    if max_hours <= 0:
        raise ValueError(f"max_hours must be > 0, got {max_hours!r}")
    start, end = range_
    start_min = start.hour * 60 + start.minute
    end_min = end.hour * 60 + end.minute
    chunk_min = int(max_hours * 60)
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