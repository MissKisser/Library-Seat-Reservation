"""实况核对与实况同步的纯函数层。

实况核对：用超星 getusedtimes 的真实占用核验本地任务；实况同步：用
reservelist 的真实预约记录补登/清退 user_reserved。判定与选号函数均为
纯函数，离线可测；网络与写库由 Scheduler（reconcile_sweep /
sync_user_reserved）承担。本模块不发起任何网络请求。
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import TYPE_CHECKING

from seatbot.utils.timeutil import CST

if TYPE_CHECKING:
    from seatbot.models import Account

#: 任务 last_error 中实况核对标记前缀；恢复时仅清自己写的标记
RECONCILE_ERROR_PREFIX = "实况核对: "

#: reservelist 状态码中仍占用座位的生效状态（0 待履约/1 使用中/3 暂离中/5 被监督中）
ACTIVE_RESERVE_STATUSES = frozenset({0, 1, 3, 5})

#: 自动同步行在 user_reserved.note 中的标记；清退只动带此标记的行
AUTO_SYNC_NOTE = "实况自动同步"


def _to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def classify_task(
    start: str, end: str, server_windows: list[tuple[str, str]],
) -> tuple[float, bool]:
    """任务窗口 [start, end) 被服务端占用区间覆盖的比例与是否失守。

    入参均为 "HH:MM" 字符串；server_windows 为 getusedtimes 返回的占用区间。
    covered_ratio == 1.0 判一致，其余（含部分覆盖）均判失守——部分失守
    意味着时段后半段他人可抢，同样需要人工介入。
    返回 (covered_ratio, mismatch)。
    """
    s, e = _to_min(start), _to_min(end)
    span = e - s
    if span <= 0:
        return 1.0, False
    covered = 0
    for ws, we in server_windows:
        lo, hi = max(s, _to_min(ws)), min(e, _to_min(we))
        if hi > lo:
            covered += hi - lo
    ratio = min(1.0, covered / span)
    return ratio, ratio < 1.0


def pick_read_account(
    accounts: list["Account"],
    cookie_recency: dict[str, int] | None = None,
) -> "Account | None":
    """选只读查询账号：优先会话最新的账号，回退第一个有凭据的账号。

    cookie_recency: account_id → account_cookies.updated_at，由调用方
    动态查库传入；不依赖任何具体账号，账号增删后自动重选，已删账号
    的残留记录被自然忽略。并列时取 accounts 列表序（list_accounts
    为 ORDER BY id，稳定）。
    """
    cred = [a for a in accounts if a.phone and a.password]
    if not cred:
        return None
    rec = cookie_recency or {}
    return max(cred, key=lambda a: rec.get(a.id, 0))


def parse_reservations(
    entries: list[dict], days: set[date],
) -> list[dict]:
    """reservelist 原始条目 → 实况同步候选记录。

    entries: client.reserve_list() 返回的原始条目；days: 参与守护的
    日期集合（今天/明天）。仅保留状态生效（ACTIVE_RESERVE_STATUSES）
    且预约日期落在 days 内的条目；startTime/endTime 毫秒时间戳按 CST
    转为 time。返回字典含 reserve_id / seat_num（三位补零）/ day /
    start / end。
    """
    out: list[dict] = []
    for e in entries:
        if e.get("status") not in ACTIVE_RESERVE_STATUSES:
            continue
        day = date.fromisoformat(e["today"])
        if day not in days:
            continue
        start = datetime.fromtimestamp(e["startTime"] / 1000, tz=CST).time()
        end = datetime.fromtimestamp(e["endTime"] / 1000, tz=CST).time()
        seat = str(e["seatNum"])
        out.append({
            "reserve_id": int(e["id"]),
            "seat_num": seat.zfill(3) if seat.isdigit() else seat,
            "day": day, "start": start, "end": end,
            "status": int(e["status"]),
        })
    return out

def diff_user_reserved(
    parsed: list[dict],
    known_reserve_ids: set[int],
    existing_rows: list[dict],
) -> tuple[list[dict], list[int]]:
    """比对出需补登与清退的 user_reserved 行。

    parsed: parse_reservations 输出；known_reserve_ids: 本地任务已跟踪
    的预约号（调度器提交或手动导入的预约均有本地签到托管，不再补登）；
    existing_rows: 该账号现有 user_reserved 行（list_user_reserved 格式，
    day/start_time/end_time 为字符串）。补登条件：预约号未被跟踪，且
    与该 (seat_num, day) 现有行无时间重叠。清退：仅清 note 为
    AUTO_SYNC_NOTE 的行，其 (seat_num, day, 起, 止) 键不在本轮 parsed
    键集合中（预约已取消/履约/违约即从 reservelist 消失）；手动登记行
    永不清退。返回 (to_add, to_delete_ids)。
    """
    def _mins(t: time) -> int:
        return t.hour * 60 + t.minute

    by_seat_day: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for r in existing_rows:
        by_seat_day.setdefault((r["seat_num"], r["day"]), []).append(
            (_to_min(r["start_time"]), _to_min(r["end_time"])))

    to_add: list[dict] = []
    for p in parsed:
        if p["reserve_id"] in known_reserve_ids:
            continue
        s, e = _mins(p["start"]), _mins(p["end"])
        spans = by_seat_day.get((p["seat_num"], p["day"].isoformat()), [])
        if any(s < we and ws < e for ws, we in spans):
            continue
        to_add.append(p)

    parsed_keys = {(p["seat_num"], p["day"].isoformat(),
                    _mins(p["start"]), _mins(p["end"])) for p in parsed}
    to_delete: list[int] = []
    for r in existing_rows:
        if r.get("note") != AUTO_SYNC_NOTE:
            continue
        key = (r["seat_num"], r["day"],
               _to_min(r["start_time"]), _to_min(r["end_time"]))
        if key not in parsed_keys:
            to_delete.append(r["id"])
    return to_add, to_delete


# ---------- 托管采纳：纯函数 ----------

#: 状态映射：上游生效状态 → 托管任务初始 status。
# 1 使用中 / 3 暂离中 = 已签到（任务直入 SIGNED，仅管签退）。
# 0 待履约 / 5 被监督中 = 待签到（任务入 ACTIVE，待时段开始 sign）。
ADOPT_STATUS_MAP: dict[int, str] = {1: "signed", 3: "signed", 0: "active", 5: "active"}


def plan_adoption(
    account_id: str,
    parsed: list[dict],
    known_reserve_ids: set[int],
    stopped_reserve_ids: set[int],
    queued_reserve_ids: set[int],
    account_tasks: list[dict],
    now: "datetime | None" = None,
) -> list[dict]:
    """为实况同步产出的 parsed 候选逐条计算采纳动作。

    入参:
        parsed: parse_reservations 输出（每条含 reserve_id/ seat_num/day/start/end/status）。
        known_reserve_ids: 已被本地 tasks.reserve_id 跟踪的预约号（任务层接入，不再托管）。
        stopped_reserve_ids: hosted_reservations 中 state=stopped 的预约号集合
            （防"停止→下一轮同步重采纳"死循环；仅经页面 queued 恢复才再采纳）。
        queued_reserve_ids: hosted_reservations 中 state=queued 的预约号集合
            （恢复语义：原托管行被转 queued 后本轮同步若仍见则再采纳）。
        account_tasks: 该账号当日 (account_id, day, seat_num, start_time, status) 的本地任务简表
            （list[dict]，status 字符串）；供同键查找用。
        now: 当前 CST 时间；为 None 时取 datetime.now(CST)。仅用于"时段已结束"判断。

    返回: 动作列表，每条形如
        {"kind": "create"|"convert"|"conflict", "account_id", "reserve_id",
         "seat_num", "day", "start", "end", "status", "task_id"?, "span"?, "upstream_status"}
    行为:
        - reserve_id ∈ known_reserve_ids → 跳过（已被任务托管）。
        - reserve_id ∈ stopped_reserve_ids 且不在 queued_reserve_ids → 跳过（被用户停止，防重采纳）。
        - 上游 status 不在 ADOPT_STATUS_MAP → 跳过（防御性，调用方通常已按 ACTIVE_RESERVE_STATUSES 过滤）。
        - now ≥ end → 跳过（时段已结束，不建任务/行）。
        - 同键 (account_id, day, seat_num, start) 已有本地任务:
            - 状态 ∈ {pending, ready} → "convert"（原地接附 reserve_id/status/source）。
            - 状态 ∈ {failed, complete} → 不阻塞，"create"（新建）。
            - 状态 ∈ 活跃态且 reserve_id 不同 → "conflict"（理论不可能，平台同座位每天 1 段）。
        - 未命中 → "create"。
    """
    from datetime import datetime
    if now is None:
        from seatbot.utils.timeutil import CST
        now = datetime.now(CST)

    out: list[dict] = []

    for p in parsed:
        rid = int(p["reserve_id"])
        if rid in known_reserve_ids:
            continue
        if rid in stopped_reserve_ids and rid not in queued_reserve_ids:
            continue
        upstream = int(p.get("status", 0))
        mapped = ADOPT_STATUS_MAP.get(upstream)
        if mapped is None:
            continue
        end_t = p["end"]
        # 时段已结束（end_t 为 time；与 now 比较）
        if hasattr(end_t, "hour") and not hasattr(end_t, "date"):
            from datetime import datetime as _dt
            from seatbot.utils.timeutil import CST as _CST
            end_dt = _dt.combine(p["day"], end_t).replace(tzinfo=_CST) if isinstance(p["day"], date) else None
            if end_dt is not None and now >= end_dt:
                continue
        same_key = [
            t for t in account_tasks
            if t.get("account_id") == account_id
            and t["day"] == p["day"]
            and t["seat_num"] == p["seat_num"]
            and t["start_time"] == p["start"]
        ]
        if same_key:
            for t in same_key:
                if t["status"] in ("pending", "ready"):
                    out.append({
                        "kind": "convert",
                        "account_id": account_id,
                        "reserve_id": rid,
                        "seat_num": p["seat_num"],
                        "day": p["day"],
                        "start": p["start"],
                        "end": p["end"],
                        "status": mapped,
                        "task_id": t["id"],
                        "upstream_status": upstream,
                    })
                    break
                elif t["status"] in ("failed", "complete"):
                    out.append({
                        "kind": "create",
                        "account_id": account_id,
                        "reserve_id": rid,
                        "seat_num": p["seat_num"],
                        "day": p["day"],
                        "start": p["start"],
                        "end": p["end"],
                        "status": mapped,
                        "upstream_status": upstream,
                    })
                    break
                else:
                    if t.get("reserve_id") and int(t["reserve_id"]) != rid:
                        out.append({
                            "kind": "conflict",
                            "account_id": account_id,
                            "reserve_id": rid,
                            "seat_num": p["seat_num"],
                            "day": p["day"],
                            "start": p["start"],
                            "end": p["end"],
                            "task_id": t["id"],
                            "existing_reserve_id": int(t["reserve_id"]),
                            "upstream_status": upstream,
                        })
                    break
        else:
            out.append({
                "kind": "create",
                "account_id": account_id,
                "reserve_id": rid,
                "seat_num": p["seat_num"],
                "day": p["day"],
                "start": p["start"],
                "end": p["end"],
                "status": mapped,
                "upstream_status": upstream,
            })
    return out

