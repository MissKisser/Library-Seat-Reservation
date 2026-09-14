"""手动预约页（/manual）的纯函数校验与窗口工具。

所有规则以规格 `docs/superpowers/specs/2026-09-12-manual-reserve-page-design.md`
§4 为唯一来源；本模块只放纯逻辑（不发起网络、不读写数据库），路由层
负责装配数据并把违规原因以 quote 重定向回页。
"""
from __future__ import annotations

from datetime import date, datetime, time

from seatbot.bindings import matrix_windows
from seatbot.utils.timeutil import RESERVE_WINDOW_HOUR, parse_hhmm
from seatbot.utils.weekly import weekday_key


#: 时段开始到签到截止的容忍窗口（分钟）；超过该窗口仍可签到但平台会判违约。
SIGN_DEADLINE_MINUTES = 20


def day_window_blocked(now: datetime, day: date) -> str | None:
    """规则 2：日期窗口提示。

    day == 今天：始终允许（占座窗口全天开）。
    day == 明天：now.hour < 14 时返回禁选提示；否则允许。
    其他日期：返回拒绝说明。
    """
    today = now.date()
    if day == today:
        return None
    if day == today.fromordinal(today.toordinal() + 1):
        if now.hour < RESERVE_WINDOW_HOUR:
            return f"次日预约需 {RESERVE_WINDOW_HOUR:02d}:00 起开放，当前尚不可选"
        return None
    return "手动预约仅支持今天或明天"


def _to_min(t: time) -> int:
    return t.hour * 60 + t.minute


def _overlaps(a_start: time, a_end: time, b_start: time, b_end: time) -> bool:
    return _to_min(a_start) < _to_min(b_end) and _to_min(b_start) < _to_min(a_end)


def validate_manual_submission(
    *,
    day: date,
    start: time,
    end: time,
    seat_num: str,
    seat_slots: dict | None,
    existing_tasks: list,
    max_seg_hours: float,
    daily_limit_hours: float,
    open_time: str,
    close_time: str,
    now: datetime,
) -> list[str]:
    """手动预约提交前的 8 条硬约束校验（矩阵占用仅拦未来日）。

    入参均为纯数据；调用方负责把 ``existing_tasks`` 过滤为当日非终态任务
    （status ∈ {pending, ready, submitting, active, signed, leaving}；任务元素
    含 ``start_time`` / ``end_time`` / ``seat_num`` 字段）。返回可展示的中文
    违规原因列表，空列表 = 全部通过；任一原因非空 = 拒绝提交。
    """
    issues: list[str] = []
    sn = (seat_num or "").strip()
    if not sn.isdigit() or not (1 <= len(sn) <= 4):
        issues.append("座位号必须是 1-4 位数字")
        return issues

    blocked = day_window_blocked(now, day)
    if blocked:
        issues.append(blocked)

    if end <= start:
        issues.append("时段结束需晚于开始")
        return issues

    try:
        op_t, cl_t = parse_hhmm(open_time), parse_hhmm(close_time)
    except ValueError as exc:
        issues.append(f"营业时间配置错误: {exc}")
        op_t, cl_t = time(8, 0), time(22, 0)
    if start < op_t or end > cl_t:
        issues.append(
            f"时段必须在营业时间 {open_time}-{close_time} 内")
    if start.minute not in (0, 30) or end.minute not in (0, 30):
        issues.append("起止时间必须为整点或半点（30 分钟步进）")
        return issues

    duration_h = (end.hour * 60 + end.minute - start.hour * 60 - start.minute) / 60.0
    if duration_h > max_seg_hours + 1e-9:
        issues.append(
            f"时段 {start.strftime('%H:%M')}-{end.strftime('%H:%M')} 长 "
            f"{duration_h:g}h，超过单段上限 {max_seg_hours:g}h")

    used_h = 0.0
    cross_seat_clash = False
    for t in existing_tasks:
        t_s, t_e, t_sn = t.start_time, t.end_time, getattr(t, "seat_num", "")
        if _overlaps(start, end, t_s, t_e):
            if t_sn != sn:
                cross_seat_clash = True
        dur = (t_e.hour * 60 + t_e.minute - t_s.hour * 60 - t_s.minute) / 60.0
        used_h += dur
    if used_h + duration_h > daily_limit_hours + 1e-9:
        issues.append(
            f"账号当日已排 {used_h:g}h，加本段 {duration_h:g}h "
            f"将超每日限额 {daily_limit_hours:g}h")

    if cross_seat_clash:
        issues.append("该时段与账号当日其他座位的任务时间重叠")

    if day > now.date():
        wd = weekday_key(day)
        matrix_overlap = False
        try:
            windows = matrix_windows(seat_slots, wd)
        except ValueError as exc:
            issues.append(f"守护矩阵解析失败: {exc}")
            windows = []
        for _ms, ms, me, _h in windows:
            if _overlaps(start, end, ms, me):
                matrix_overlap = True
                break
        if matrix_overlap:
            issues.append("该时段已被守护矩阵占用（将由系统自动预约，仅支持矩阵之外时段）")

    if day == now.date():
        deadline = now.replace(
            hour=start.hour, minute=start.minute, second=0, microsecond=0,
        )
        elapsed = (now - deadline).total_seconds() / 60.0
        if elapsed >= SIGN_DEADLINE_MINUTES:
            issues.append(
                f"签到窗口已过：开始 {start.strftime('%H:%M')} 后 "
                f"{SIGN_DEADLINE_MINUTES} 分钟内必须签到")

    return issues


def parse_manual_form(
    *,
    day_raw: str,
    start_raw: str,
    end_raw: str,
    seat_raw: str,
) -> tuple[date, time, time, str] | tuple[None, None, None, str]:
    """解析表单四字段：日期 / 起止 / 座位号。全部成功返回四元组，失败返回 (None, None, None, reason)。"""
    try:
        d = date.fromisoformat((day_raw or "").strip())
    except ValueError:
        return None, None, None, "日期格式错误（应为 YYYY-MM-DD）"
    try:
        s = parse_hhmm((start_raw or "").strip())
        e = parse_hhmm((end_raw or "").strip())
    except ValueError as exc:
        return None, None, None, f"时间格式错误: {exc}"
    sn_raw = (seat_raw or "").strip()
    if not sn_raw:
        return None, None, None, "座位号不能为空"
    sn = sn_raw.zfill(3)
    if not sn.isdigit() or not (1 <= len(sn) <= 4):
        return None, None, None, "座位号必须是 1-4 位数字"
    return d, s, e, sn


def used_hours_for_account_day(
    tasks: list, day: date, account_id: str,
) -> float:
    """该账号当日所有非终态任务累计时长（小时）。"""
    total = 0.0
    for t in tasks:
        if t.account_id != account_id or t.day != day:
            continue
        if t.status in ("failed", "complete"):
            continue
        s, e = t.start_time, t.end_time
        total += (e.hour * 60 + e.minute - s.hour * 60 - s.minute) / 60.0
    return total
