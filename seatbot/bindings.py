"""绑定矩阵的约束校验、余量计算与自动分配（纯逻辑，无 IO）。

矩阵值为按星期几的 dict（mon..sun，缺天 = 该天无时段）；list 视为全周统一。
约束（AGENTS.md 超星预约机制规范）按星期几逐天独立检查：
  - 每个账号每天对同一座位最多 1 个时段, 单段时长 ≤ max_seg_hours
  - 每个账号每天累计预约时长 ≤ daily_limit_hours
  - 同一账号同一天内不同座位时段不得重叠
"""
from __future__ import annotations

from datetime import date, datetime, time

from seatbot.models import Account, SeatTarget
from seatbot.utils.timeutil import parse_range
from seatbot.utils.weekly import (
    WEEKDAY_KEYS,
    WEEKDAY_LABELS,
    slots_for_weekday,
)


_BASE = date(2000, 1, 1)


def _hours(s: time, e: time) -> float:
    """两个时刻的时长（小时）。"""
    return (
        datetime.combine(_BASE, e) - datetime.combine(_BASE, s)
    ).total_seconds() / 3600


def _day_ranges(seat: str, val, wd: str) -> list[str]:
    """某座位某天的时段列表；"full" 等非法形态抛 ValueError。"""
    day_val = slots_for_weekday(val, wd)
    if not isinstance(day_val, list):
        raise ValueError(f"座位 {seat} 的时段必须是字符串列表")
    return day_val


def matrix_windows(
    seat_slots: dict | None,
    wd: str | None = None,
) -> list[tuple[str, time, time, float]]:
    """把矩阵展开成 (座位, 开始, 结束, 时长) 列表；格式非法抛 ValueError。

    wd 给定时只展开该天的时段；wd=None 展开全周 7 天合集（统计用）。
    """
    out: list[tuple[str, time, time, float]] = []
    if wd is None:
        # 全周合集：list 形态去重（只展开一次），dict 形态展开 7 天
        for seat, val in (seat_slots or {}).items():
            if isinstance(val, list):
                for r in val:
                    try:
                        s, e = parse_range(r)
                    except Exception as exc:
                        raise ValueError(f"座位 {seat} 时段 {r!r} 格式错误") from exc
                    out.append((seat, s, e, _hours(s, e)))
            else:
                for d in WEEKDAY_KEYS:
                    for r in _day_ranges(seat, val, d):
                        try:
                            s, e = parse_range(r)
                        except Exception as exc:
                            raise ValueError(
                                f"座位 {seat} {WEEKDAY_LABELS[d]}时段 {r!r} 格式错误") from exc
                        out.append((seat, s, e, _hours(s, e)))
        return out
    # 指定星期：只展开该天
    for seat, val in (seat_slots or {}).items():
        for r in _day_ranges(seat, val, wd):
            try:
                s, e = parse_range(r)
            except Exception as exc:
                raise ValueError(
                    f"座位 {seat} {WEEKDAY_LABELS[wd]}时段 {r!r} 格式错误") from exc
            out.append((seat, s, e, _hours(s, e)))
    return out


def validate_matrix(
    seat_slots: dict | None,
    *,
    max_seg_hours: float,
    daily_limit_hours: float,
) -> None:
    """校验单账号绑定矩阵，逐星期几独立检查，违者抛 ValueError（可直接展示）。"""
    for wd in WEEKDAY_KEYS:
        windows = matrix_windows(seat_slots, wd)
        if not windows:
            continue
        label = WEEKDAY_LABELS[wd]
        per_seat: dict[str, int] = {}
        for seat, _s, _e, _h in windows:
            per_seat[seat] = per_seat.get(seat, 0) + 1
        for seat, n in per_seat.items():
            if n > 1:
                raise ValueError(
                    f"{label}：座位 {seat} 每天最多绑定 1 个时段（当前 {n} 个）")
        total = 0.0
        for seat, s, e, h in windows:
            if e <= s:
                raise ValueError(f"{label}：座位 {seat} 时段结束需晚于开始")
            if h > max_seg_hours + 1e-9:
                raise ValueError(
                    f"{label}：座位 {seat} 时段 {s.strftime('%H:%M')}-{e.strftime('%H:%M')}"
                    f" 长 {h:g}h，超过单段上限 {max_seg_hours:g}h")
            total += h
        if total > daily_limit_hours + 1e-9:
            raise ValueError(
                f"{label}：每日总时长 {total:g}h 超过限额 {daily_limit_hours:g}h")
        ordered = sorted(
            ((s, e, seat) for seat, s, e, _ in windows), key=lambda x: (x[0], x[1])
        )
        for (s1, e1, seat1), (s2, e2, seat2) in zip(ordered, ordered[1:]):
            if s2 < e1:
                raise ValueError(
                    f"{label}：座位 {seat1} {s1.strftime('%H:%M')}-{e1.strftime('%H:%M')} 与 "
                    f"座位 {seat2} {s2.strftime('%H:%M')}-{e2.strftime('%H:%M')} 时段重叠")


def used_hours(seat_slots: dict | None, wd: str) -> float:
    """单账号某天已绑定总时长（小时）。"""
    return sum(h for _, _, _, h in matrix_windows(seat_slots, wd))


def account_margins(
    accounts: list[Account], *, daily_limit_hours: float
) -> list[dict]:
    """每账号逐天余量摘要：days[wd] = {used_hours, remaining_hours, full_slots_left}。

    另提供聚合字段 used_hours / remaining_hours / full_slots_left（分别为周内峰值已用、
    最紧剩余、可容纳完整 2h 段最小值），供紧凑药丸卡片直接展示；days 保留逐天明细供
    悬停提示展开。
    """
    out: list[dict] = []
    for a in accounts:
        days: dict[str, dict] = {}
        for wd in WEEKDAY_KEYS:
            used = used_hours(a.seat_slots, wd)
            remaining = max(0.0, daily_limit_hours - used)
            days[wd] = {
                "used_hours": used,
                "remaining_hours": remaining,
                "full_slots_left": int(remaining // 2),
            }
        # 聚合：周内最紧约束（便于药丸卡单行展示）
        agg_used = max((d["used_hours"] for d in days.values()), default=0.0)
        agg_remaining = min((d["remaining_hours"] for d in days.values()), default=daily_limit_hours)
        agg_slots = min((d["full_slots_left"] for d in days.values()), default=int(daily_limit_hours // 2))
        out.append({
            "id": a.id,
            "days": days,
            "used_hours": agg_used,
            "remaining_hours": agg_remaining,
            "full_slots_left": agg_slots,
            "seats": sorted((a.seat_slots or {}).keys()),
        })
    return out


def candidate_accounts(
    accounts: list[Account],
    *,
    seat: str,
    start: time,
    end: time,
    exclude_id: str,
    daily_limit_hours: float,
    weekday: str,
) -> list[dict]:
    """某 (星期几, 座位, 时段) 的可改绑账号清单：当天余量够、时段不撞、该座位当天未绑。

    返回 [{id, remaining_hours}]，按当天余量降序（改绑重试与前端提示共用）。
    """
    dur = _hours(start, end)
    out: list[dict] = []
    for a in accounts:
        if a.id == exclude_id:
            continue
        if _day_ranges(seat, (a.seat_slots or {}).get(seat), weekday):
            continue
        if used_hours(a.seat_slots, weekday) + dur > daily_limit_hours + 1e-9:
            continue
        if any(start < e and s < end
               for _seat, s, e, _h in matrix_windows(a.seat_slots, weekday)):
            continue
        out.append({
            "id": a.id,
            "remaining_hours": max(
                0.0, daily_limit_hours - used_hours(a.seat_slots, weekday)),
        })
    out.sort(key=lambda x: -x["remaining_hours"])
    return out


def desired_slots_of(seat_target: SeatTarget, wd: str, *, is_weekly: bool | None = None) -> list[str]:
    """座位在某天的期望守护时段；未配置 → []（不检查）；dict 缺/空天 → []。

    is_weekly=None 时自动按字段存在度选择（优先 weekly 列若非 None），
    供存量/迁移期兼容；显式 True/False 用于按 schedule_mode 精确取列。
    """
    # 独立双配置：优先按显式模式取列
    if is_weekly is True:
        slots = getattr(seat_target, "desired_slots_weekly", None)
        if slots is None:
            return []
        day_val = slots_for_weekday(slots, wd)
        return list(day_val) if isinstance(day_val, list) else []
    if is_weekly is False:
        slots = getattr(seat_target, "desired_slots", None)
        if slots is None:
            return []
        day_val = slots_for_weekday(slots, wd)
        return list(day_val) if isinstance(day_val, list) else []
    # is_weekly=None：兼容旧库（单列复用）
    weekly = getattr(seat_target, "desired_slots_weekly", None)
    if weekly is not None:
        day_val = slots_for_weekday(weekly, wd)
        return list(day_val) if isinstance(day_val, list) else []
    slots = getattr(seat_target, "desired_slots", None)
    if slots is None:
        return []
    day_val = slots_for_weekday(slots, wd)
    return list(day_val) if isinstance(day_val, list) else []

def auto_assign(
    accounts: list[Account],
    desired: dict[str, dict[str, list[str]]],
    *,
    max_seg_hours: float,
    daily_limit_hours: float,
) -> tuple[dict[str, dict[str, dict[str, list[str]]]], list[str]]:
    """为未被精确覆盖的 (星期几, 座位, 期望时段) 自动挑选账号绑定。

    既有绑定一律保留；在当天约束内做最大填充的精确匹配（回溯 + 剪枝）。
    desired 形状 {seat: {wd: [want, ...]}}；返回 (每账号规范矩阵
    {aid: {seat: {wd: [want]}}, 未覆盖说明列表)。
    """
    eps = 1e-9

    def _canon(val) -> dict[str, list[str] | str]:
        from seatbot.utils.weekly import normalize_weekly
        return normalize_weekly(val, allow_full=True) or {
            w: [] for w in WEEKDAY_KEYS}

    matrices = {
        a.id: {seat: _canon(val) for seat, val in (a.seat_slots or {}).items()}
        for a in accounts
    }
    used = {
        aid: {wd: used_hours(a.seat_slots, wd) for wd in WEEKDAY_KEYS}
        for aid, a in zip(matrices.keys(), accounts)
    }
    windows = {
        aid: {wd: [(s, e) for _, s, e, _ in matrix_windows(a.seat_slots, wd)]
              for wd in WEEKDAY_KEYS}
        for aid, a in zip(matrices.keys(), accounts)
    }

    jobs: list[tuple[str, str, str, time, time, float]] = []
    bad: list[str] = []
    for seat in sorted(desired):
        for wd in WEEKDAY_KEYS:
            for want in desired[seat].get(wd, []):
                if any(matrices[aid].get(seat, {}).get(wd) == [want]
                       for aid in matrices):
                    continue
                try:
                    ws, we = parse_range(want)
                    wh = _hours(ws, we)
                except Exception:
                    bad.append(
                        f"{WEEKDAY_LABELS[wd]}{seat} 期望时段 {want!r} 格式非法")
                    continue
                jobs.append((wd, seat, want, ws, we, wh))

    best: dict = {"plan": []}

    def dfs(i: int, plan: list[tuple[str, str, str, str]]) -> None:
        if len(plan) + (len(jobs) - i) <= len(best["plan"]):
            return
        if i == len(jobs):
            best["plan"] = list(plan)
            return
        wd, seat, want, ws, we, wh = jobs[i]
        cands = sorted(
            (a for a in accounts
             if not matrices[a.id].get(seat, {}).get(wd)
             and used[a.id][wd] + wh <= daily_limit_hours + eps
             and not any(ws < e and s < we for s, e in windows[a.id][wd])),
            key=lambda a: (used[a.id][wd], a.id),
        )
        for a in cands:
            matrices[a.id].setdefault(seat, {})[wd] = [want]
            used[a.id][wd] += wh
            windows[a.id][wd].append((ws, we))
            plan.append((wd, seat, want, a.id))
            dfs(i + 1, plan)
            plan.pop()
            windows[a.id][wd].pop()
            used[a.id][wd] -= wh
            matrices[a.id][seat].pop(wd, None)
        dfs(i + 1, plan)

    dfs(0, [])

    filled = {(wd, seat, want): aid for wd, seat, want, aid in best["plan"]}
    for (wd, seat, want, _aid) in best["plan"]:
        matrices[_aid].setdefault(seat, {})[wd] = [want]
    unfillable = [
        f"{WEEKDAY_LABELS[wd]}{seat} {want}：无可用账号（余量不足或时段冲突）"
        for wd, seat, want, _, _, _ in jobs if (wd, seat, want) not in filled
    ]
    # 规范 7 键：已分配的有值；回溯试过但最终未分配的座位条目（全空）直接移除
    for aid in matrices:
        for seat in list(matrices[aid].keys()):
            day_map = {w: matrices[aid][seat].get(w, []) for w in WEEKDAY_KEYS}
            if any(day_map[w] for w in WEEKDAY_KEYS):
                matrices[aid][seat] = day_map
            else:
                del matrices[aid][seat]
    return matrices, bad + unfillable
