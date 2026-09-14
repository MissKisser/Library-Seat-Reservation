"""绑定矩阵的约束校验、余量计算与自动分配（纯逻辑，无 IO）。

矩阵值为按星期几的 dict（mon..sun，缺天 = 该天无时段）；list 视为全周统一。
约束（AGENTS.md 超星预约机制规范）按星期几逐天独立检查：
  - 单段时长 ≤ max_seg_hours
  - 每个账号每天累计预约时长 ≤ daily_limit_hours
  - 同一账号同一天内所有座位时段不得重叠（含同座位多段之间）
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
        for (s1, e1, seat1), (s2, e2, seat2) in zip(ordered, ordered[1:]):
            if s2 < e1:
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

    def dfs(i: int, plan: list[tuple[str, str, str, str]]) -> None:
        if len(plan) + (len(jobs) - i) <= len(best["plan"]):
            return
        if i == len(jobs):
            best["plan"] = list(plan)
            return
        wd, seat, want, ws, we, wh = jobs[i]
        cands = sorted(
            (a for a in accounts
             if used[a.id][wd] + wh <= daily_limit_hours + eps
             and not any(ws < e and s < we for s, e in windows[a.id][wd])),
            key=lambda a: ((used[a.id][wd], a.id) if mode == "safe"
                           else (-used[a.id][wd], a.id)),
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


def plan_matrix(
    accounts: list[Account],
    desired: dict[str, dict[str, list[str]]],
    *,
    max_seg_hours: float,
    daily_limit_hours: float,
    mode: str,
    account_order: list[str] | None = None,
) -> tuple[dict[str, dict[str, dict[str, list[str]]]], list[str]]:
    """全量重解守护矩阵（不保留既有绑定）。

    desired 形状 {seat: {wd: [want]}}；返回 (每账号规范矩阵
    {aid: {seat: {wd: [want]}}}, unfillable 列表)。

    mode='safe'（摊薄）：任务按时长降序（LPT）贪心分配给"当天已用小时数最少"
    的账号，无回溯——某天放不下的输出 unfillable；目标 min-max 单账号单日小时数。
    mode='minimal'（打包）：逐天迭代加深 DFS 求"动用账号数最少"解；
    池成员按 account_order 截取（默认 id 稳定序），单池无法覆盖某天时扩大池。
    池超过账号总数 → 输出 unfillable（文案含"需至少 N 个账号"）。
    两模式产出均逐账号通过 validate_matrix。
    """
    eps = 1e-9
    if not accounts:
        return {}, ["无可用账号"]
    if mode not in ("safe", "minimal"):
        return {}, [f"未知分配策略 {mode!r}"]

    n_acc = len(accounts)

    # ---------- 解析所有 (wd, seat, slot, dur) jobs ----------
    jobs_by_day: dict[str, list[tuple[str, str, str, float]]] = {wd: [] for wd in WEEKDAY_KEYS}
    bad: list[str] = []
    for seat in sorted(desired):
        for wd in WEEKDAY_KEYS:
            for want in desired[seat].get(wd, []):
                try:
                    ws, we = parse_range(want)
                    wh = _hours(ws, we)
                except Exception:
                    bad.append(f"{WEEKDAY_LABELS[wd]}{seat} 期望时段 {want!r} 格式非法")
                    continue
                jobs_by_day[wd].append((seat, want, ws, we, wh))

    # ---------- 池成员：account_order 给定时只取前 K 个；未给定时按 id 稳定序 ----------
    if account_order is None:
        pool: list[str] = sorted(a.id for a in accounts)
    else:
        seen: set[str] = set()
        pool = []
        for aid in account_order:
            if aid in seen:
                continue
            seen.add(aid)
            pool.append(aid)
    # 池大小由求解方按 _max_lower()/n_acc 控制（pool[:K] 截取）

    def _empty_matrices() -> dict[str, dict[str, dict[str, list[str]]]]:
        return {a.id: {} for a in accounts}

    def _validate(m: dict[str, dict[str, dict[str, list[str]]]]) -> None:
        for a in accounts:
            seat_slots = m.get(a.id, {})
            if not seat_slots:
                continue
            try:
                validate_matrix(
                    seat_slots,
                    max_seg_hours=max_seg_hours,
                    daily_limit_hours=daily_limit_hours,
                )
            except ValueError as exc:
                raise AssertionError(f"账号 {a.id} 矩阵非法: {exc}") from exc

    def _used_overlap(aid: str, used: dict[str, float],
                      ranges: dict[str, list[tuple[time, time]]],
                      wd: str, dur: float,
                      ws: time, we: time) -> bool:
        return (used[aid][wd] + dur > daily_limit_hours + eps) or any(
            ws < e and s < we for s, e in ranges[aid][wd]
        )

    def _solve_minimal(K: int) -> dict | None:
        """迭代加深 DFS：每天派活给不同账号（同一座位多窗口必须不同账号），
        用 daily_active 计数每天动用账号数；目标最小化 max(daily_active)。
        池成员 = pool[:K]；K 不足以满足某天 L(wd) 时由调用方扩池重试。
        """
        pool_ids = pool[:K]
        # 每账号 × 每天：已绑定座位集合（保证同账号同座位当天 ≤1 段）
        acc_seat: dict[str, dict[str, set[str]]] = {
            aid: {wd: set() for wd in WEEKDAY_KEYS}
            for aid in pool_ids
        }
        # 每账号 × 每天：已用小时数 / 已占时段（容量 + 重叠查重）
        st_used: dict[str, dict[str, float]] = {
            aid: {wd: 0.0 for wd in WEEKDAY_KEYS}
            for aid in pool_ids
        }
        st_ranges: dict[str, dict[str, list[tuple[time, time]]]] = {
            aid: {wd: [] for wd in WEEKDAY_KEYS}
            for aid in pool_ids
        }
        # 每天本天激活账号集合（用于下限计数；不参与「同座位」约束）
        daily_active: dict[str, set[str]] = {wd: set() for wd in WEEKDAY_KEYS}
        # 全周累计已用（tie-break 让负载在账号间轮换）
        week_used: dict[str, float] = {aid: 0.0 for aid in pool_ids}

        # 每天独立：长段优先（减少失败面）
        flat_jobs: list[tuple[str, str, str, time, time, float]] = []
        for wd in WEEKDAY_KEYS:
            for seat, want, ws, we, wh in sorted(
                    jobs_by_day[wd], key=lambda x: -x[4]):
                flat_jobs.append((wd, seat, want, ws, we, wh))

        # 下限 L(wd) = max(ceil(jobs/2), 同座位最多段数)
        from math import ceil

        def _lower(wd: str) -> int:
            n = len(jobs_by_day[wd])
            if n == 0:
                return 0
            per_seat: dict[str, int] = {}
            for seat, _w, _ws, _we, _wh in jobs_by_day[wd]:
                per_seat[seat] = per_seat.get(seat, 0) + 1
            return max(ceil(n / 2), max(per_seat.values()))

        assignment: list[tuple[str, str, str, str]] = []

        def _try(idx: int) -> bool:
            if idx == len(flat_jobs):
                return True
            wd, seat, want, ws, we, wh = flat_jobs[idx]
            limit = _lower(wd)
            cands: list[str] = []
            for aid in pool_ids:
                # 同账号同座位当天 ≤1 段
                if seat in acc_seat[aid][wd]:
                    continue
                # 容量 / 重叠
                if (st_used[aid][wd] + wh > daily_limit_hours + eps
                        or any(ws < e and s < we for s, e in st_ranges[aid][wd])):
                    continue
                # 避免无谓扩展 daily_active：已激活账号随时可接；未激活账号
                # 仅在 active 数 < limit 时才纳入候选。
                if aid not in daily_active[wd] and len(daily_active[wd]) >= limit:
                    continue
                cands.append(aid)
            # tie-break：周累计已用升序 → id 稳定
            cands.sort(key=lambda a: (week_used[a], a))
            for aid in cands:
                # 仅记录本次尝试实际激活的账号：回溯时按实际激活列表 discard
                was_active = aid in daily_active[wd]
                if not was_active:
                    daily_active[wd].add(aid)
                acc_seat[aid][wd].add(seat)
                st_used[aid][wd] += wh
                st_ranges[aid][wd].append((ws, we))
                week_used[aid] += wh
                assignment.append((wd, seat, want, aid))
                if _try(idx + 1):
                    return True
                assignment.pop()
                week_used[aid] -= wh
                st_ranges[aid][wd].pop()
                st_used[aid][wd] -= wh
                acc_seat[aid][wd].discard(seat)
                if not was_active:
                    daily_active[wd].discard(aid)
            return False

        if not _try(0):
            return None
        m = _empty_matrices()
        for wd, seat, want, aid in assignment:
            m[aid].setdefault(seat, {}).setdefault(wd, []).append(want)
        return m

    # ============ safe 模式：贪心（LPT）+ 回溯 ============
    def _solve_safe() -> tuple[dict, list[str]]:
        m = _empty_matrices()
        used = {a.id: {wd: 0.0 for wd in WEEKDAY_KEYS} for a in accounts}
        ranges = {a.id: {wd: [] for wd in WEEKDAY_KEYS} for a in accounts}
        weekly = {a.id: 0.0 for a in accounts}
        # 全部 jobs 按时长降序
        all_jobs: list[tuple[str, str, str, time, time, float]] = []
        for wd in WEEKDAY_KEYS:
            for seat, want, ws, we, wh in jobs_by_day[wd]:
                all_jobs.append((wd, seat, want, ws, we, wh))
        all_jobs.sort(key=lambda x: -x[5])
        for wd, seat, want, ws, we, wh in all_jobs:
            cands = sorted(
                (a for a in accounts
                 if used[a.id][wd] + wh <= daily_limit_hours + eps
                 and not any(ws < e and s < we for s, e in ranges[a.id][wd])
                 and not m[a.id].get(seat, {}).get(wd)),
                key=lambda a: (weekly[a.id], used[a.id][wd], a.id),
            )
            if not cands:
                bad.append(f"{WEEKDAY_LABELS[wd]}{seat} {want}：无可用账号")
                continue
            a = cands[0]
            m[a.id].setdefault(seat, {})[wd] = [want]
            used[a.id][wd] += wh
            ranges[a.id][wd].append((ws, we))
            weekly[a.id] += wh
        return m, bad

    if mode == "safe":
        m, bad_extra = _solve_safe()
        try:
            _validate(m)
        except AssertionError as exc:
            return {}, [str(exc)]
        return m, bad_extra

    # minimal：池大小从 max(L(wd)) 起，到 n_acc
    from math import ceil

    def _max_lower() -> int:
        mx = 0
        for wd in WEEKDAY_KEYS:
            n = len(jobs_by_day[wd])
            if n == 0:
                continue
            per_seat: dict[str, int] = {}
            for seat, _w, _ws, _we, _wh in jobs_by_day[wd]:
                per_seat[seat] = per_seat.get(seat, 0) + 1
            mx = max(mx, max(ceil(n / 2), max(per_seat.values())))
        return mx

    lo = _max_lower()
    for K in range(lo, n_acc + 1):
        m = _solve_minimal(K)
        if m is not None:
            try:
                _validate(m)
            except AssertionError as exc:
                bad.append(str(exc))
                continue
            return m, bad
    bad.append(f"最小模式无可行解：需至少 {lo} 个账号")
    return {}, bad
