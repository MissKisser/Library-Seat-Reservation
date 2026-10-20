"""绑定矩阵的约束校验、余量计算与自动分配（纯逻辑，无 IO）。

矩阵值为按星期几的 dict（mon..sun，缺天 = 该天无时段）；list 视为全周统一。
约束（AGENTS.md 超星预约机制规范）按星期几逐天独立检查：
  - 单段时长 ≤ max_seg_hours
  - 每个账号每天累计预约时长 ≤ daily_limit_hours
  - 同一账号同一天内所有座位时段不得重叠（含同座位多段之间）
自动分配（auto_assign / plan_matrix）遵循座位独占优先：座位与账号一一对应，
同一座位优先由一个账号整座承接，账号之间不交叉。
"""
from __future__ import annotations

from datetime import date, datetime, time

from seatbot.models import Account, SeatTarget
from seatbot.utils.timeutil import overlaps, parse_range
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


def set_day_slots(
    matrix: dict | None, seat: str, wd: str, slots: list[str],
) -> dict:
    """返回新矩阵：设置 seat 在 wd 的时段，保留其余星期与其他座位。

    兼容旧形态（直接 list 值）与新形态（{wd: list} 嵌套）；某天全部清空时
    顺带删除该座位的空条目。不修改入参。
    """
    result: dict = {}
    for k, v in (matrix or {}).items():
        result[k] = dict(v) if isinstance(v, dict) else list(v or [])
    cur: dict[str, list[str]] = {w: [] for w in WEEKDAY_KEYS}
    old = result.get(seat)
    if isinstance(old, dict):
        for w in WEEKDAY_KEYS:
            if old.get(w):
                cur[w] = list(old[w])
    elif isinstance(old, list):
        for w in WEEKDAY_KEYS:
            cur[w] = list(old)
    cur[wd] = list(slots)
    if any(cur[w] for w in WEEKDAY_KEYS):
        result[seat] = cur
    else:
        result.pop(seat, None)
    return result

def add_day_slot(matrix: dict | None, seat: str, wd: str, rng: str) -> dict:
    """返回新矩阵：把 rng 追加到 seat 在 wd 的时段列表末尾（已存在则原样）。"""
    cur = _day_ranges(seat, (matrix or {}).get(seat), wd)
    if rng in cur:
        return {k: dict(v) if isinstance(v, dict) else list(v or [])
                for k, v in (matrix or {}).items()}
    return set_day_slots(matrix, seat, wd, cur + [rng])


def remove_day_slot(matrix: dict | None, seat: str, wd: str, rng: str) -> dict:
    """返回新矩阵：从 seat 在 wd 的时段列表中移除 rng（不存在则原样）。"""
    cur = _day_ranges(seat, (matrix or {}).get(seat), wd)
    if rng not in cur:
        return {k: dict(v) if isinstance(v, dict) else list(v or [])
                for k, v in (matrix or {}).items()}
    return set_day_slots(matrix, seat, wd, [r for r in cur if r != rng])


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
        for (s1, e1, seat1), (s2, e2, seat2) in zip(ordered, ordered[1:], strict=False):
            if overlaps(s1, e1, s2, e2):
                raise ValueError(
                    f"{label}：座位 {seat1} {s1.strftime('%H:%M')}-{e1.strftime('%H:%M')} 与 "
                    f"座位 {seat2} {s2.strftime('%H:%M')}-{e2.strftime('%H:%M')} 时段重叠")


def used_hours(seat_slots: dict | None, wd: str) -> float:
    """单账号某天已绑定总时长（小时）。"""
    return sum(h for _, _, _, h in matrix_windows(seat_slots, wd))


def account_margins(
    accounts: list[Account], *, daily_limit_hours: float, max_seg_hours: float = 2.0
) -> list[dict]:
    """每账号逐天余量摘要：days[wd] = {used_hours, remaining_hours, full_slots_left}。

    另提供聚合字段 used_hours / remaining_hours / full_slots_left（分别为周内峰值已用、
    最紧剩余、可容纳完整单段最小值），供紧凑药丸卡片直接展示；days 保留逐天明细供
    悬停提示展开。
    """
    out: list[dict] = []
    for a in accounts:
        days: dict[str, dict] = {}
        for wd in WEEKDAY_KEYS:
            used = used_hours(a.seat_slots, wd)
            remaining = max(0.0, daily_limit_hours - used)
            seg = max_seg_hours if max_seg_hours > 1e-9 else 2.0
            days[wd] = {
                "used_hours": used,
                "remaining_hours": remaining,
                "full_slots_left": int(remaining // seg),
            }
        # 聚合：周内最紧约束（便于药丸卡单行展示）
        seg = max_seg_hours if max_seg_hours > 1e-9 else 2.0
        agg_used = max((d["used_hours"] for d in days.values()), default=0.0)
        agg_remaining = min((d["remaining_hours"] for d in days.values()), default=daily_limit_hours)
        agg_slots = min((d["full_slots_left"] for d in days.values()), default=int(daily_limit_hours // seg))
        out.append({
            "id": a.id,
            "days": days,
            "used_hours": agg_used,
            "remaining_hours": agg_remaining,
            "full_slots_left": agg_slots,
            "seats": sorted((a.seat_slots or {}).keys()),
        })
    return out


def _seat_has_slots(val: object) -> bool:
    """座位条目是否含任何时段（list 形态或按星期几的 dict 形态）。"""
    if isinstance(val, list):
        return bool(val)
    if isinstance(val, dict):
        return any(bool(day) for day in val.values())
    return False


def exclusive_health(
    accounts: list[Account], seat_nums: list[str],
) -> dict:
    """专属分配体检：座位数、账号数、跨座账号、缺口座位。

    入参:
        accounts: 参与守护的账号列表（通常只含启用账号）。
        seat_nums: 目标座位号列表。

    返回值:
        dict：seat_count / account_count / cross_seat_accounts / cross_seat_count /
        uncovered_seats / uncovered_count / one_to_one。
        one_to_one 为真表示既无跨座账号、也无缺口座位（严格一一对应）。
    """
    covered: set[str] = set()
    cross: list[str] = []
    for a in accounts:
        held = {
            seat for seat, val in (a.seat_slots or {}).items()
            if _seat_has_slots(val)
        }
        covered |= held
        if len(held) >= 2:
            cross.append(a.id)
    uncovered = [s for s in seat_nums if s not in covered]
    return {
        "seat_count": len(seat_nums),
        "account_count": len(accounts),
        "cross_seat_accounts": sorted(cross),
        "cross_seat_count": len(cross),
        "uncovered_seats": list(uncovered),
        "uncovered_count": len(uncovered),
        "one_to_one": not cross and not uncovered,
    }


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
    """某 (星期几, 座位, 时段) 的可改绑账号清单：当天余量够、与既有时段（含同座）不撞。
    返回 [{id, remaining_hours}]，按当天余量降序（改绑重试与前端提示共用）。
    """
    dur = _hours(start, end)
    out: list[dict] = []
    for a in accounts:
        if a.id == exclude_id:
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


def rebind_candidates(
    accounts: list[Account],
    *,
    seat: str,
    rng: str,
    weekday: str,
    source_id: str,
    daily_limit_hours: float,
    max_seg_hours: float = 2.0,
) -> list[dict]:
    """某 (星期几, 座位, 时段) 可接手换绑的账号清单（排除原账号）。

    候选条件即超星硬限制：当天累计 ≤ daily_limit_hours、与既有时段不重叠、
    时段本身长于 max_seg_hours 时无候选。
    时段格式非法抛 ValueError。返回 [{id, remaining_hours}]，按当天余量降序。
    """
    try:
        start, end = parse_range(rng)
    except Exception as exc:
        raise ValueError(f"时段 {rng!r} 格式错误") from exc
    if _hours(start, end) > max_seg_hours + 1e-9:
        return []
    return candidate_accounts(
        accounts, seat=seat, start=start, end=end,
        exclude_id=source_id, daily_limit_hours=daily_limit_hours,
        weekday=weekday,
    )


def rebind_candidates_for_days(
    accounts: list[Account],
    *,
    seat: str,
    rng: str,
    weekdays: list[str],
    source_id: str,
    daily_limit_hours: float,
    max_seg_hours: float = 2.0,
) -> list[dict]:
    """多天换绑的可接手账号：各天候选的交集，余量取各天最小值。

    全周统一形态的换绑要一次改 7 天，账号必须每天都满足超星限制才可选。
    返回 [{id, remaining_hours}]，按最小余量降序。
    """
    per_day = [
        {c["id"]: c["remaining_hours"] for c in rebind_candidates(
            accounts, seat=seat, rng=rng, weekday=wd,
            source_id=source_id, daily_limit_hours=daily_limit_hours,
            max_seg_hours=max_seg_hours)}
        for wd in weekdays
    ]
    if not per_day:
        return []
    common = set(per_day[0])
    for m in per_day[1:]:
        common &= set(m)
    out = [{"id": aid, "remaining_hours": min(m[aid] for m in per_day)}
           for aid in common]
    out.sort(key=lambda x: (-x["remaining_hours"], x["id"]))
    return out


def rebind_matrices(
    source: Account,
    target: Account,
    *,
    seat: str,
    rng: str,
    weekdays: list[str],
    max_seg_hours: float,
    daily_limit_hours: float,
) -> tuple[dict, dict]:
    """把 seat 在 weekdays 各天的 rng 段从 source 迁给 target。

    先构造新矩阵再逐账号校验（单段 ≤ max_seg_hours、
    每日累计 ≤ daily_limit_hours、跨座位不重叠），任一方不通过则抛
    ValueError 且两个账号的矩阵都不返回，保证不产生半成品状态。
    """
    for wd in weekdays:
        if wd not in WEEKDAY_KEYS:
            raise ValueError(f"非法星期: {wd!r}")
    src_matrix = dict(source.seat_slots or {})
    dst_matrix = dict(target.seat_slots or {})
    for wd in weekdays:
        dst_matrix = add_day_slot(dst_matrix, seat, wd, rng)
        src_matrix = remove_day_slot(src_matrix, seat, wd, rng)
    validate_matrix(
        dst_matrix, max_seg_hours=max_seg_hours,
        daily_limit_hours=daily_limit_hours,
    )
    validate_matrix(
        src_matrix, max_seg_hours=max_seg_hours,
        daily_limit_hours=daily_limit_hours,
    )
    return src_matrix, dst_matrix


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
    mode: str = "safe",
) -> tuple[dict[str, dict[str, dict[str, list[str]]]], list[str]]:
    """为未被精确覆盖的 (星期几, 座位, 期望时段) 自动挑选账号绑定。

    既有绑定一律保留；在当天约束内做最大填充的精确匹配（回溯 + 剪枝）。
    挑选账号时遵循座位独占优先：已持有该座位的账号优先承接该座位的其余分段，
    避免同一座位被摊薄到多个账号。
    dedicated 档额外禁止引入跨座：已持有其他座位的账号不接手（宁可留缺口）。
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
        for aid, a in zip(matrices.keys(), accounts, strict=False)
    }
    windows = {
        aid: {wd: [(s, e) for _, s, e, _ in matrix_windows(a.seat_slots, wd)]
              for wd in WEEKDAY_KEYS}
        for aid, a in zip(matrices.keys(), accounts, strict=False)
    }

    jobs: list[tuple[str, str, str, time, time, float]] = []
    bad: list[str] = []
    for seat in sorted(desired):
        for wd in WEEKDAY_KEYS:
            for want in desired[seat].get(wd, []):
                if any(want in matrices[aid].get(seat, {}).get(wd, [])
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

    def _holds_seat(aid: str, seat: str) -> bool:
        """账号当前是否已持有该座位的时段（含回溯过程中的临时占用）。"""
        daymap = matrices.get(aid, {}).get(seat) or {}
        return any(daymap.get(w) for w in WEEKDAY_KEYS)

    def _held_seats(aid: str) -> set[str]:
        """账号当前实际持有时段的所有座位（忽略全空残留条目）。"""
        return {
            s for s, daymap in matrices.get(aid, {}).items()
            if any(daymap.get(w) for w in WEEKDAY_KEYS)
        }

    def dfs(i: int, plan: list[tuple[str, str, str, str]]) -> None:
        """回溯搜索：在容量与跨座不重叠约束下求最优分配。"""
        if len(plan) + (len(jobs) - i) <= len(best["plan"]):
            return
        if i == len(jobs):
            best["plan"] = list(plan)
            return
        wd, seat, want, ws, we, wh = jobs[i]
        cands = sorted(
            (a for a in accounts
             if used[a.id][wd] + wh <= daily_limit_hours + eps
             and not any(ws < e and s < we for s, e in windows[a.id][wd])
             # 专属档：绝不引入跨座（已持有别的座位的账号不接手）
             and (mode != "dedicated" or not (_held_seats(a.id) - {seat}))),
            # 座位独占优先：已持有该座位的账号优先，其次按策略平衡负载
            key=lambda a: (
                0 if _holds_seat(a.id, seat) else 1,
                (used[a.id][wd], a.id) if mode in ("safe", "dedicated")
                else (-used[a.id][wd], a.id),
            ),
        )
        for a in cands:
            matrices[a.id].setdefault(seat, {}).setdefault(wd, []).append(want)
            used[a.id][wd] += wh
            windows[a.id][wd].append((ws, we))
            plan.append((wd, seat, want, a.id))
            dfs(i + 1, plan)
            plan.pop()
            windows[a.id][wd].pop()
            used[a.id][wd] -= wh
            lst = matrices[a.id][seat][wd]
            lst.remove(want)
            if not lst:
                matrices[a.id][seat].pop(wd, None)
        dfs(i + 1, plan)

    dfs(0, [])

    filled = {(wd, seat, want): aid for wd, seat, want, aid in best["plan"]}
    for (wd, seat, want, _aid) in best["plan"]:
        matrices[_aid].setdefault(seat, {}).setdefault(wd, []).append(want)
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


def diff_matrices(
    old: dict[str, dict[str, dict[str, list[str]]]],
    new: dict[str, dict[str, dict[str, list[str]]]],
) -> dict[str, list[dict]]:
    """对比两个 7 键矩阵，返回 {added, removed}（元素为 dict: account/seat/weekday/slot）。

    仅以"现有绑定段"为单位对比；全空座位条目（7 天都无安排）不参与。
    """

    def _flat(m: dict) -> set[tuple[str, str, str, str]]:
        out: set[tuple[str, str, str, str]] = set()
        for aid, spec in m.items():
            for sz, daymap in spec.items():
                if not isinstance(daymap, dict):
                    continue
                for wd, slots in daymap.items():
                    if not slots:
                        continue
                    for s in slots:
                        out.add((aid, sz, wd, s))
        return out

    a, b = _flat(old), _flat(new)
    def _as_dicts(items: set[tuple[str, str, str, str]]) -> list[dict]:
        return [
            {"account": aid, "seat": sz, "weekday": wd, "slot": s}
            for aid, sz, wd, s in sorted(items)
        ]
    return {
        "added": _as_dicts(b - a),
        "removed": _as_dicts(a - b),
    }


def _parse_jobs(
    desired: dict[str, dict[str, list[str]]],
    seats: list[str],
    *,
    max_seg_hours: float,
) -> tuple[dict[str, dict[str, list[tuple[str, time, time, float]]]], list[str]]:
    """把期望时段展开成 jobs[seat][wd] = [(时段文本, 开始, 结束, 时长)]。

    入参:
        desired: {seat: {wd: [时段文本]}}。
        seats: 座位列表（字典序）。
        max_seg_hours: 单段时长上限（小时）。

    返回值:
        (jobs, bad)：jobs 为展开结果；bad 为格式非法或超过单段上限的说明列表。
    """
    eps = 1e-9
    jobs: dict[str, dict[str, list[tuple[str, time, time, float]]]] = {
        seat: {wd: [] for wd in WEEKDAY_KEYS} for seat in seats
    }
    bad: list[str] = []
    for seat in seats:
        for wd in WEEKDAY_KEYS:
            seen_want: set[str] = set()
            for want in desired[seat].get(wd, []):
                if want in seen_want:
                    continue
                seen_want.add(want)
                try:
                    ws, we = parse_range(want)
                    wh = _hours(ws, we)
                except Exception:
                    bad.append(f"{WEEKDAY_LABELS[wd]}{seat} 期望时段 {want!r} 格式非法")
                    continue
                if wh > max_seg_hours + eps:
                    bad.append(
                        f"{WEEKDAY_LABELS[wd]}{seat} {want}："
                        f"超过单段上限 {max_seg_hours:g}h")
                    continue
                jobs[seat][wd].append((want, ws, we, wh))
    return jobs, bad


def _plan_dedicated(
    accounts: list[Account],
    desired: dict[str, dict[str, list[str]]],
    *,
    max_seg_hours: float,
    daily_limit_hours: float,
    order: list[str],
    seats: list[str],
) -> tuple[dict[str, dict[str, dict[str, list[str]]]], list[str]]:
    """专属分配：座位与账号严格一一对应，装不下就留缺口，绝不跨座。

    入参:
        accounts: 参与分配的账号列表（仅取 id 做最终校验）。
        desired: {seat: {wd: [时段文本]}}。
        max_seg_hours: 单段时长上限（小时）。
        daily_limit_hours: 每账号每日总时长上限（小时）。
        order: 账号优先级顺序（已去重，且全部在 accounts 内）。
        seats: 座位列表（字典序）。

    返回值:
        (每账号规范矩阵 {aid: {seat: {wd: [want]}}}, unfillable 列表)。
    """
    eps = 1e-9
    jobs, bad = _parse_jobs(desired, seats, max_seg_hours=max_seg_hours)
    mats: dict[str, dict[str, dict[str, list[str]]]] = {aid: {} for aid in order}
    used = {aid: {wd: 0.0 for wd in WEEKDAY_KEYS} for aid in order}
    ranges: dict[str, dict[str, list[tuple[time, time]]]] = {
        aid: {wd: [] for wd in WEEKDAY_KEYS} for aid in order
    }
    # 只有存在期望时段的座位才需要专属账号
    active = [s for s in seats if any(jobs[s][wd] for wd in WEEKDAY_KEYS)]
    primary = {seat: order[i] for i, seat in enumerate(active) if i < len(order)}
    for seat in active:
        owner = primary.get(seat)
        if owner is None:
            # 账号不足：整座留缺口，不转交他人
            for wd in WEEKDAY_KEYS:
                for want, _ws, _we, _wh in jobs[seat][wd]:
                    bad.append(
                        f"{WEEKDAY_LABELS[wd]}{seat} {want}："
                        f"缺专属账号（留缺口，不跨座）")
            continue
        for wd in WEEKDAY_KEYS:
            for want, ws, we, wh in jobs[seat][wd]:
                if used[owner][wd] + wh > daily_limit_hours + eps or any(
                        ws < e and s < we for s, e in ranges[owner][wd]):
                    # 主账号装不下：留缺口，不转交他人
                    bad.append(
                        f"{WEEKDAY_LABELS[wd]}{seat} {want}："
                        f"主账号 {owner} 当日装不下（留缺口，不转交他人）")
                    continue
                mats[owner].setdefault(seat, {}).setdefault(wd, []).append(want)
                used[owner][wd] += wh
                ranges[owner][wd].append((ws, we))
    # 规范化输出：每天的时段按开始时刻排序
    for aid in order:
        for daymap in mats[aid].values():
            for wd, slots in daymap.items():
                daymap[wd] = sorted(
                    slots, key=lambda s: (parse_range(s)[0], parse_range(s)[1], s))
    # 校验兜底
    for a in accounts:
        seat_slots = mats.get(a.id) or {}
        if not seat_slots:
            continue
        try:
            validate_matrix(
                seat_slots,
                max_seg_hours=max_seg_hours,
                daily_limit_hours=daily_limit_hours,
            )
        except ValueError as exc:
            return {}, [f"账号 {a.id} 矩阵非法: {exc}"]
    if len(active) > len(order):
        bad.append(f"需至少 {len(active)} 个账号（当前 {len(order)} 个）")
    return mats, bad


def plan_matrix(
    accounts: list[Account],
    desired: dict[str, dict[str, list[str]]],
    *,
    max_seg_hours: float,
    daily_limit_hours: float,
    mode: str,
    account_order: list[str] | None = None,
) -> tuple[dict[str, dict[str, dict[str, list[str]]]], list[str]]:
    """全量重解守护矩阵（座位独占优先，不保留既有绑定）。

    desired 形状 {seat: {wd: [want]}}；返回 (每账号规范矩阵
    {aid: {seat: {wd: [want]}}}, unfillable 列表)。

    分配原则（座位独占优先）：

      - 座位按期望序（seat_num 字典序）、账号按 account_order（未给则 id 稳定序）
        一一对应指派主账号；账号多于座位时多余账号保持空矩阵；
      - 每个座位的全部分段优先整座交给主账号，账号之间互不交叉；
      - 主账号当日限额装不下整座时，溢出段仍留在该座位内交给其他账号
        （safe：当日已用最少者优先；minimal：优先复用已参与该座的账号）；
      - 账号少于座位时，额外座位由已指派账号按顺序整座承接，跨座不可避免时
        尽量减少跨座账号数；
      - dedicated 档是严格版：座位↔账号一一对应，账号不足或主账号额度不够时
        一律留缺口，任何情况下都不跨座。

    硬约束在分配过程中即满足：单段 ≤ max_seg_hours、跨座同时段不重叠、
    每账号每日总时长 ≤ daily_limit_hours；分不下的分段进入 unfillable，
    账号数不足时附"需至少 N 个账号"说明。
    """
    eps = 1e-9
    if not accounts:
        return {}, ["无可用账号"]
    if mode not in ("safe", "minimal", "dedicated"):
        return {}, [f"未知分配策略 {mode!r}"]

    # ---------- 账号优先级顺序 ----------
    acc_ids = {a.id for a in accounts}
    order: list[str] = []
    seen: set[str] = set()
    for aid in account_order or []:
        if aid in acc_ids and aid not in seen:
            seen.add(aid)
            order.append(aid)
    for aid in sorted(acc_ids - seen):
        order.append(aid)
    rank = {aid: i for i, aid in enumerate(order)}
    n_acc = len(order)

    seats = sorted(desired)

    # ---------- 专属分配（严格一一对应，绝不跨座） ----------
    if mode == "dedicated":
        return _plan_dedicated(
            accounts, desired,
            max_seg_hours=max_seg_hours,
            daily_limit_hours=daily_limit_hours,
            order=order, seats=seats,
        )

    # ---------- 解析期望时段 ----------
    # jobs[seat][wd] = [(时段文本, 开始, 结束, 时长)]
    jobs, bad = _parse_jobs(desired, seats, max_seg_hours=max_seg_hours)

    # ---------- 主账号指派：座位 ↔ 账号 一一对应 ----------
    primary: dict[str, str] = {}
    for i, seat in enumerate(seats):
        if i < n_acc:
            primary[seat] = order[i]

    # 每账号 × 每天：已用小时数 / 已占时段；held = 实际持有（部分也算）的座位；
    # planned = 计划持有（主账号指派 + 已承接），用于判断是否引入新的跨座。
    used = {aid: {wd: 0.0 for wd in WEEKDAY_KEYS} for aid in order}
    ranges = {aid: {wd: [] for wd in WEEKDAY_KEYS} for aid in order}
    mats: dict[str, dict[str, dict[str, list[str]]]] = {aid: {} for aid in order}
    held: dict[str, set[str]] = {aid: set() for aid in order}
    planned: dict[str, set[str]] = {aid: set() for aid in order}
    for seat, aid in primary.items():
        planned[aid].add(seat)

    def _fits(aid: str, wd: str, ws: time, we: time, wh: float) -> bool:
        """账号 aid 当天能否再装下该分段（容量 + 时段不重叠）。"""
        if used[aid][wd] + wh > daily_limit_hours + eps:
            return False
        return not any(ws < e and s < we for s, e in ranges[aid][wd])

    def _put(aid: str, seat: str, wd: str, want: str,
             ws: time, we: time, wh: float) -> None:
        """把一个分段写入账号矩阵与状态。"""
        mats[aid].setdefault(seat, {}).setdefault(wd, []).append(want)
        used[aid][wd] += wh
        ranges[aid][wd].append((ws, we))
        held[aid].add(seat)
        planned[aid].add(seat)

    def _can_take_whole(aid: str, seat: str) -> bool:
        """账号 aid 能否整座承接该座位的全部分段（逐天累计容量并检查重叠）。"""
        for wd in WEEKDAY_KEYS:
            total = used[aid][wd]
            own: list[tuple[time, time]] = []
            for _want, ws, we, wh in jobs[seat][wd]:
                if total + wh > daily_limit_hours + eps:
                    return False
                if any(ws < e and s < we for s, e in ranges[aid][wd]):
                    return False
                if any(ws < e and s < we for s, e in own):
                    return False
                own.append((ws, we))
                total += wh
        return True

    def _pick_overflow(wd: str, ws: time, we: time, wh: float,
                       seat: str) -> str | None:
        """为溢出分段挑选同座位的承接账号。

        优先不引入新的跨座（不持有其他座位的账号在前），其次按策略：
        safe=当日已用最少者优先；minimal=优先复用已参与该座的账号。
        """
        best_key: tuple | None = None
        best_aid: str | None = None
        for aid in order:
            if not _fits(aid, wd, ws, we, wh):
                continue
            tier = 0 if not (planned[aid] - {seat}) else 1
            if mode == "safe":
                key = (tier, used[aid][wd], rank[aid])
            else:
                key = (tier, 0 if seat in held[aid] else 1, used[aid][wd], rank[aid])
            if best_key is None or key < best_key:
                best_key, best_aid = key, aid
        return best_aid

    def _greedy_take(aid: str, seat: str,
                     day_items: dict[str, list[tuple[str, time, time, float]]],
                     ) -> None:
        """账号先取走装得下的分段（长段优先），装不下的转交溢出承接。"""
        for wd in WEEKDAY_KEYS:
            items = day_items.get(wd) or []
            for want, ws, we, wh in sorted(items, key=lambda it: (-it[3], it[1], it[0])):
                if _fits(aid, wd, ws, we, wh):
                    _put(aid, seat, wd, want, ws, we, wh)
                    continue
                target = _pick_overflow(wd, ws, we, wh, seat)
                if target is None:
                    bad.append(
                        f"{WEEKDAY_LABELS[wd]}{seat} {want}："
                        f"无可用账号（余量不足或时段冲突）")
                    continue
                _put(target, seat, wd, want, ws, we, wh)

    def _need_accounts() -> int:
        """覆盖全部期望时段所需账号数的下界。"""
        from math import ceil

        need = 1
        for wd in WEEKDAY_KEYS:
            items = [
                (seat, ws, we, wh)
                for seat in seats
                for _want, ws, we, wh in jobs[seat][wd]
            ]
            if not items:
                continue
            total = sum(x[3] for x in items)
            need = max(need, int(ceil(total / daily_limit_hours - eps)))
            for _seat, ws, _we, _wh in items:
                peak = sum(1 for o in items if o[1] <= ws < o[2])
                need = max(need, peak)
        for seat in seats:
            for wd in WEEKDAY_KEYS:
                total = sum(x[3] for x in jobs[seat][wd])
                if total <= 0:
                    continue
                need = max(need, int(ceil(total / daily_limit_hours - eps)))
        return need

    # ---------- 逐座位分配 ----------
    for seat in seats:
        day_items = {wd: items for wd, items in jobs[seat].items() if items}
        if not day_items:
            continue
        owner = primary.get(seat)
        if owner is not None:
            # 主账号：能整座承接就整座交给它，否则主账号先取部分、其余溢出
            if _can_take_whole(owner, seat):
                for wd in WEEKDAY_KEYS:
                    for want, ws, we, wh in day_items.get(wd, []):
                        _put(owner, seat, wd, want, ws, we, wh)
                continue
            _greedy_take(owner, seat, day_items)
            continue
        # 账号数 < 座位数：额外座位优先整座交给已持座的账号
        whole = [aid for aid in order if _can_take_whole(aid, seat)]
        if whole:
            # 优先已持座的账号（跨座不可避免时最小化跨座账号数）
            aid = min(whole, key=lambda a: (0 if held[a] else 1, rank[a]))
            for wd in WEEKDAY_KEYS:
                for want, ws, we, wh in day_items.get(wd, []):
                    _put(aid, seat, wd, want, ws, we, wh)
            continue
        # 无人能整座承接：由首个能承接分段的账号部分承接，其余溢出
        owner = next(
            (aid for aid in order
             if any(_fits(aid, wd, ws, we, wh)
                    for wd in WEEKDAY_KEYS
                    for _want, ws, we, wh in day_items.get(wd, []))),
            None,
        )
        if owner is None:
            for wd in WEEKDAY_KEYS:
                for want, _ws, _we, _wh in day_items.get(wd, []):
                    bad.append(
                        f"{WEEKDAY_LABELS[wd]}{seat} {want}："
                        f"无可用账号（余量不足或时段冲突）")
            continue
        _greedy_take(owner, seat, day_items)

    # ---------- 规范化输出：每天的时段按开始时刻排序 ----------
    for aid in order:
        for daymap in mats[aid].values():
            for wd, slots in daymap.items():
                daymap[wd] = sorted(
                    slots, key=lambda s: (parse_range(s)[0], parse_range(s)[1], s))

    # ---------- 校验兜底 ----------
    for a in accounts:
        seat_slots = mats.get(a.id) or {}
        if not seat_slots:
            continue
        try:
            validate_matrix(
                seat_slots,
                max_seg_hours=max_seg_hours,
                daily_limit_hours=daily_limit_hours,
            )
        except ValueError as exc:
            return {}, [f"账号 {a.id} 矩阵非法: {exc}"]

    if bad:
        need = _need_accounts()
        if need > n_acc:
            bad.append(f"需至少 {need} 个账号（当前 {n_acc} 个）")
    return mats, bad
