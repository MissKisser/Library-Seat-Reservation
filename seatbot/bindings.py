"""绑定矩阵的约束校验、余量计算与自动分配（纯逻辑，无 IO）。

约束（AGENTS.md 超星预约机制规范）:
  - 每个账号每天对同一座位最多 1 个时段, 单段时长 ≤ max_seg_hours
  - 每个账号每天累计预约时长 ≤ daily_limit_hours
  - 同一账号不同座位之间的时段不得重叠（一个账号同一时刻只能持有一个预约）
"""
from __future__ import annotations

from datetime import date, datetime, time

from seatbot.models import Account
from seatbot.utils.timeutil import parse_range

#: 座位未配置期望时段时的默认守护时段
DEFAULT_DESIRED_SLOTS = ["09:00-11:00", "15:00-17:00", "19:00-21:00"]

_BASE = date(2000, 1, 1)


def _hours(s: time, e: time) -> float:
    """两个时刻的时长（小时）。"""
    return (
        datetime.combine(_BASE, e) - datetime.combine(_BASE, s)
    ).total_seconds() / 3600


def matrix_windows(
    seat_slots: dict[str, list[str]] | None,
) -> list[tuple[str, time, time, float]]:
    """把单账号矩阵展开成 (座位, 开始, 结束, 时长) 列表；格式非法抛 ValueError。"""
    out: list[tuple[str, time, time, float]] = []
    for seat, ranges in (seat_slots or {}).items():
        if isinstance(ranges, str):
            raise ValueError(f"座位 {seat} 的时段必须是字符串列表")
        for r in ranges or []:
            try:
                s, e = parse_range(r)
            except Exception as exc:
                raise ValueError(f"座位 {seat} 时段 {r!r} 格式错误") from exc
            out.append((seat, s, e, _hours(s, e)))
    return out


def validate_matrix(
    seat_slots: dict[str, list[str]] | None,
    *,
    max_seg_hours: float,
    daily_limit_hours: float,
) -> None:
    """校验单账号绑定矩阵，违反任一约束抛 ValueError（消息可直接展示）。"""
    for seat, ranges in (seat_slots or {}).items():
        if len(ranges or []) > 1:
            raise ValueError(
                f"座位 {seat} 每天最多绑定 1 个时段（当前 {len(ranges)} 个）")
    windows = matrix_windows(seat_slots)
    total = 0.0
    for seat, s, e, h in windows:
        if e <= s:
            raise ValueError(f"座位 {seat} 时段结束需晚于开始")
        if h > max_seg_hours + 1e-9:
            raise ValueError(
                f"座位 {seat} 时段 {s.strftime('%H:%M')}-{e.strftime('%H:%M')}"
                f" 长 {h:g}h，超过单段上限 {max_seg_hours:g}h")
        total += h
    if total > daily_limit_hours + 1e-9:
        raise ValueError(
            f"每日总时长 {total:g}h 超过限额 {daily_limit_hours:g}h")
    ordered = sorted(
        ((s, e, seat) for seat, s, e, _ in windows), key=lambda x: (x[0], x[1])
    )
    for (s1, e1, seat1), (s2, e2, seat2) in zip(ordered, ordered[1:]):
        if s2 < e1:
            raise ValueError(
                f"座位 {seat1} {s1.strftime('%H:%M')}-{e1.strftime('%H:%M')} 与 "
                f"座位 {seat2} {s2.strftime('%H:%M')}-{e2.strftime('%H:%M')} 时段重叠")


def used_hours(seat_slots: dict[str, list[str]] | None) -> float:
    """单账号已绑定总时长（小时）。"""
    return sum(h for _, _, _, h in matrix_windows(seat_slots))


def account_margins(
    accounts: list[Account], *, daily_limit_hours: float
) -> list[dict]:
    """每账号余量摘要：已用 / 剩余 / 还能容纳的完整 2h 段数 / 已绑定座位。"""
    out: list[dict] = []
    for a in accounts:
        used = used_hours(a.seat_slots)
        remaining = max(0.0, daily_limit_hours - used)
        out.append({
            "id": a.id,
            "used_hours": used,
            "remaining_hours": remaining,
            "full_slots_left": int(remaining // 2),
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
) -> list[dict]:
    """某 (座位, 时段) 的可改绑账号清单：余量够、时段不撞、该座位未绑。

    返回 [{id, remaining_hours}]，按余量降序（改绑重试与前端提示共用）。
    """
    dur = _hours(start, end)
    out: list[dict] = []
    for a in accounts:
        if a.id == exclude_id:
            continue
        if seat in (a.seat_slots or {}):
            continue
        if used_hours(a.seat_slots) + dur > daily_limit_hours + 1e-9:
            continue
        if any(start < e and s < end
               for _seat, s, e, _h in matrix_windows(a.seat_slots)):
            continue
        out.append({
            "id": a.id,
            "remaining_hours": max(0.0, daily_limit_hours - used_hours(a.seat_slots)),
        })
    out.sort(key=lambda x: -x["remaining_hours"])
    return out


def desired_slots_of(seat_target) -> list[str]:
    """座位的期望守护时段；未配置时用默认三段。"""
    slots = getattr(seat_target, "desired_slots", None)
    return list(slots) if slots else list(DEFAULT_DESIRED_SLOTS)


def auto_assign(
    accounts: list[Account],
    desired: dict[str, list[str]],
    *,
    max_seg_hours: float,
    daily_limit_hours: float,
) -> tuple[dict[str, dict[str, list[str]]], list[str]]:
    """为未被精确覆盖的 (座位, 期望时段) 自动挑选账号绑定。

    既有绑定一律保留；在约束内做最大填充的精确匹配（回溯 + 剪枝，
    任务与账号规模都很小）。候选资格：该座位尚无绑定、加上该时段后
    不超每日限额、且与既有时段不重叠；同层候选优先已用时长最少者。
    返回 (每账号完整新矩阵 {account_id: seat_slots}, 未覆盖说明列表)。
    """
    eps = 1e-9
    matrices = {a.id: dict(a.seat_slots or {}) for a in accounts}
    used = {a.id: used_hours(a.seat_slots) for a in accounts}
    windows = {
        a.id: [(s, e) for _, s, e, _ in matrix_windows(a.seat_slots)]
        for a in accounts
    }

    jobs: list[tuple[str, str, time, time, float]] = []
    bad: list[str] = []
    for seat in sorted(desired):
        for want in desired[seat]:
            if any(matrices[aid].get(seat) == [want] for aid in matrices):
                continue
            try:
                ws, we = parse_range(want)
                wh = _hours(ws, we)
            except Exception:
                bad.append(f"{seat} 期望时段 {want!r} 格式非法")
                continue
            jobs.append((seat, want, ws, we, wh))

    best: dict = {"plan": []}

    def dfs(i: int, plan: list[tuple[str, str, str]]) -> None:
        if len(plan) + (len(jobs) - i) <= len(best["plan"]):
            return
        if i == len(jobs):
            best["plan"] = list(plan)
            return
        seat, want, ws, we, wh = jobs[i]
        cands = sorted(
            (a for a in accounts
             if seat not in matrices[a.id]
             and used[a.id] + wh <= daily_limit_hours + eps
             and not any(ws < e and s < we for s, e in windows[a.id])),
            key=lambda a: (used[a.id], a.id),
        )
        for a in cands:
            matrices[a.id][seat] = [want]
            used[a.id] += wh
            windows[a.id].append((ws, we))
            plan.append((seat, want, a.id))
            dfs(i + 1, plan)
            plan.pop()
            windows[a.id].pop()
            used[a.id] -= wh
            del matrices[a.id][seat]
        dfs(i + 1, plan)  # 跳过该时段（把资源留给后续时段可能更优）

    dfs(0, [])

    filled = {(seat, want): aid for seat, want, aid in best["plan"]}
    for (seat, want, aid) in best["plan"]:
        matrices[aid][seat] = [want]
    unfillable = [
        f"{seat} {want}：无可用账号（余量不足或时段冲突）"
        for seat, want, *_ in jobs if (seat, want) not in filled
    ]
    return matrices, bad + unfillable
