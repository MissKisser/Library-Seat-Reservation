"""FastAPI routes for the SeatBot web panel (v2: multi-seat + per-task seat_num).

Dashboard 覆盖图以"目标座位"为行(取代 v1 的"账号"为行)。
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime as _dt, timedelta
from datetime import time as _time

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from seatbot.bindings import (
    account_margins, auto_assign, candidate_accounts,
    desired_slots_of, validate_matrix,
)
from seatbot.utils.weekly import (
    WEEKDAY_KEYS, WEEKDAY_LABELS, normalize_weekly, slots_for_weekday, weekday_key,
)
from seatbot import settings as _settings
from seatbot.client import ChaoxingClient, ChaoxingError
from seatbot.coverage import compute_seat_coverage
from seatbot.models import Account, Task, TaskStatus
from seatbot.scheduler import NextRelay
from seatbot.utils.timeutil import (
    at_cst, now_cst, parse_hhmm, parse_range, today_cst,
)


router = APIRouter()

# AGENTS.md 约定的默认测试账号: 只读查询 (getusedtimes / reserve info)
# 优先使用它, 避免无谓动用其他守护账号的会话。
PREFERRED_READ_ACCOUNT = "xiongjt"


def _pick_read_account(accounts: list[Account]) -> Account | None:
    """优先选默认只读账号, 否则回退到第一个有凭据的账号。"""
    return (
        next((a for a in accounts
              if a.id == PREFERRED_READ_ACCOUNT and a.phone and a.password), None)
        or next((a for a in accounts if a.phone and a.password), None)
    )


def _parse_seat_slots(
    raw: str, max_hours: float, daily_limit: float,
) -> dict[str, dict[str, list[str] | str]] | None:
    """解析并校验表单提交的 seat_slots JSON（接受全周 list 或按星期 dict 两种形态）。

    返回规范形态 {座位: {mon..sun: 时段列表}}；空 / 非法为空对象时返回 None；
    校验失败抛 ValueError（消息可直接展示）。
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"seat_slots JSON 错误: {e}") from e
    if not isinstance(data, dict) or not data:
        return None
    out: dict[str, dict[str, list[str] | str]] = {}
    for seat, val in data.items():
        sn = str(seat).strip()
        if not sn.isdigit() or not (1 <= len(sn) <= 4):
            raise ValueError(f"seat_slots 座位号非法: {seat!r}")
        if not isinstance(val, (list, dict)):
            raise ValueError(f"seat_slots[{sn}] 必须是字符串数组或按星期的对象")
        try:
            out[sn.zfill(3)] = normalize_weekly(val, allow_full=False)
        except ValueError as e:
            raise ValueError(f"seat_slots[{sn}] {e}") from e
    validate_matrix(
        out, max_seg_hours=max_hours,
        daily_limit_hours=daily_limit,
    )
    return out


def _matrix_set_day(
    matrix: dict,
    seat: str,
    wd: str,
    slots: list[str],
) -> dict:
    """返回新 matrix：设置 seat 在 wd 的 slots，保留其余星期与其他座位。

    兼容旧形态（直接 list 值）与新形态（{wd: list} 嵌套）。
    """
    result: dict = {}
    for k, v in matrix.items():
        if isinstance(v, dict):
            result[k] = dict(v)
        else:
            result[k] = list(v) if v else []
    if seat in result and isinstance(result[seat], dict):
        cur = {wk: result[seat].get(wk, []) for wk in WEEKDAY_KEYS}
        result[seat] = cur
    else:
        cur = {wk: [] for wk in WEEKDAY_KEYS}
        if seat in result:
            old = result[seat]
            if isinstance(old, dict):
                cur.update(old)
            elif isinstance(old, list):
                for wk in WEEKDAY_KEYS:
                    cur[wk] = list(old)
        result[seat] = cur
    result[seat][wd] = list(slots)
    if not any(result[seat][wk] for wk in WEEKDAY_KEYS):
        del result[seat]
    return result


def _fmt_weekly(val) -> list[str]:
    """把某座位的星期形态时段值压成展示摘要列表（"周一 09:00-11:00"）。

    兼容旧 list 形态（显示为"每天 …"）与 "full"；空值返回 []。
    """
    if val is None:
        return []
    if isinstance(val, str):
        return [f"每天 {val}"]
    if isinstance(val, list):
        return [f"每天 {'、'.join(val)}"] if val else []
    out: list[str] = []
    for wd in WEEKDAY_KEYS:
        day_val = slots_for_weekday(val, wd)
        if isinstance(day_val, list) and day_val:
            out.append(f"{WEEKDAY_LABELS[wd]} {'、'.join(day_val)}")
    return out


def _templates(request: Request):
    return request.app.state.templates


async def _ctx(request: Request, **extra) -> dict:
    """Common template context: cfg + target_seats + accounts (sidebar 渲染需要)。"""
    store = request.app.state.store
    target_seats = await store.list_target_seats()
    accounts = await store.list_accounts()
    return {
        "request": request,
        "cfg": request.app.state.cfg,
        "target_seats": target_seats,
        "accounts": accounts,
        **extra,
    }


async def _fetch_others_occupied(
    request: Request,
    store,
    day: date,
    seat_nums: list[str],
) -> tuple[list[tuple[str, time, time]], str | None]:
    """Call POST /getusedtimes (mobile fidEnc) for each target seat and
    collect (seat_num, start_time, end_time) tuples for every occupied
    30-min cell that day.

    Returns ([], err_msg) when the chaoxing client is unavailable, not
    logged in, or every seat lookup failed; callers should still render
    the dashboard, just without the "others_occupied" overlay.

    Auth: 优先复用调度器已保存的登录会话，
    仅在没有可用会话时才触发一次登录；
    全部座位查询抛异常（会话失效特征）时重登一次并重试。
    """
    if not seat_nums:
        return [], None

    # 从 scheduler 的 client 池中取已登录的 client，复用 cookie 缓存。
    sched = getattr(request.app.state, "sched", None)
    try:
        accounts = await store.list_accounts()
    except Exception:
        accounts = []

    acc = _pick_read_account(accounts)
    if acc is None:
        return [], "无可用账号"

    cfg = request.app.state.cfg
    if sched is not None:
        client = await sched.client_ready(acc)
    else:
        client = ChaoxingClient()

    just_logged_in = False
    if not client.cookies():
        if sched is not None:
            just_logged_in = await sched.login_and_persist(acc, client, "占用查询登录")
            if not just_logged_in:
                return [], "silent login failed: browser login failed (see logs)"
        else:
            try:
                await client.login(acc.phone, acc.password)
                just_logged_in = True
            except Exception as e:
                return [], f"silent login failed: {type(e).__name__}: {e}"

    day_str = day.isoformat()

    async def _gather() -> list:
        return await asyncio.gather(
            *(client.get_used_times(cfg.library.room_id, sn, day_str)
              for sn in seat_nums),
            return_exceptions=True,
        )

    results = await _gather()
    # 所有座位查询都抛异常且本次调用未刚登录过 → 会话大概率失效:
    # 重置会话并重登一次再试 (区分"查到空数据"——空数据是正常结果不重试)
    if (sched is not None and not just_logged_in and results
            and all(isinstance(r, Exception) for r in results)):
        client.reset_session()
        if await sched.login_and_persist(acc, client, "占用查询重登"):
            results = await _gather()

    out: list[tuple[str, _time, time]] = []
    last_err: str | None = None
    for sn, r in zip(seat_nums, results):
        if isinstance(r, Exception):
            last_err = f"{sn}: {type(r).__name__}: {r}"
            continue
        for s, e in r:
            out.append((sn, parse_hhmm(s), parse_hhmm(e)))
    return out, last_err


def _filter_self_occupied(
    others: list[tuple[str, _time, _time]],
    own_intervals: list[tuple[str, _time, _time]],
) -> list[tuple[str, _time, _time]]:
    """剔除已被本系统成功预约占据的时段，避免误标为「他人占用」。

    getusedtimes 返回的是全量占用（含本系统账号的预约），
    若某 30min 格已有 ACTIVE/SIGNED/LEAVING/COMPLETE 任务，则该占用
    来自本系统，不应再标记为 others_occupied。
    用区间重叠判断而非严格相等，兼容聚合返回。
    """
    if not others or not own_intervals:
        return others
    def _overlap(a_s, a_e, b_s, b_e) -> bool:
        return not (a_e <= b_s or a_s >= b_e)
    out: list[tuple[str, _time, _time]] = []
    for sn, s, e in others:
        blocked = False
        for osn, os_, oe in own_intervals:
            if sn == osn and _overlap(s, e, os_, oe):
                blocked = True
                break
        if not blocked:
            out.append((sn, s, e))
    return out


async def _collect_user_reserved(store, day: date) -> list[tuple[str, _time, _time]]:
    """把 user_reserved 表行转成 compute_seat_coverage 需要的三元组。"""
    rows = await store.list_user_reserved(day=day)
    out: list[tuple[str, _time, _time]] = []
    for u in rows:
        try:
            sh, sm = map(int, u["start_time"].split(":"))
            eh, em = map(int, u["end_time"].split(":"))
            out.append((u["seat_num"], _time(sh, sm), _time(eh, em)))
        except Exception:
            continue
    return out


async def _collect_own_intervals(store, day: date) -> list[tuple[str, _time, _time]]:
    """收集当日已成功预约的 (seat_num, start, end)。"""
    own: list[tuple[str, time, time]] = []
    for t in await store.list_tasks(day=day):
        if t.status in (
            TaskStatus.ACTIVE, TaskStatus.SIGNED, TaskStatus.LEAVING, TaskStatus.COMPLETE
        ):
            own.append((t.seat_num, t.start_time, t.end_time))
    return own


# =========================================================================
# Dashboard — coverage Gantt, 今天 / 明天 两块同屏
# =========================================================================
async def _collect_day_bundle(
    request: Request, store, cfg,
    accounts: list[Account], target_seats: list[SeatTarget],
    view_day: date,
) -> dict:
    """构建单日覆盖图所需的全部数据。

    返回 {
      'rows':      compute+_annotate 产物 (seat 对象 + time 对象, 供 Jinja),
      'gap_count': 未涂色且无叠加标记的格子数,
      'occ_err':   他人占用查询失败原因 (None 表示成功),
    }
    """
    user_reserved = await _collect_user_reserved(store, view_day)
    others_occupied, occ_err = await _fetch_others_occupied_cached(
        request, store, view_day, [s.seat_num for s in target_seats],
    )
    own_intervals = await _collect_own_intervals(store, view_day)
    # user_reserved 的占用也是“自己人”，同样不应标为他人
    own_intervals = own_intervals + [(sn, s, e) for sn, s, e in user_reserved]
    others_occupied = _filter_self_occupied(others_occupied, own_intervals)

    rows = compute_seat_coverage(
        accounts, target_seats, view_day,
        open_time=cfg.library.open_time,
        close_time=cfg.library.close_time,
        user_reserved=user_reserved,
        others_occupied=others_occupied,
    )
    annotated = await _annotate_rows(rows, store, view_day)
    # 缺口只统计**期望时段内**的未覆盖块：需有成功任务或用户/他人占用才算覆盖
    # 成功 = active/signed/etc 且无 last_error；future 的 pending 视为已计划覆盖
    desired_blocks: dict[str, set[str]] = {}
    view_wd = weekday_key(view_day)
    for s in target_seats:
        blocks: set[str] = set()
        for r in desired_slots_of(s, view_wd):
            try:
                rs, re_ = parse_range(r)
            except Exception:
                continue
            cur, end = rs.hour * 60 + rs.minute, re_.hour * 60 + re_.minute
            while cur < end:
                blocks.add(f"{cur // 60:02d}:{cur % 60:02d}")
                cur += 30
        desired_blocks[s.seat_num] = blocks
    now_time = now_cst().time()
    is_today_view = view_day == today_cst()
    SUCCESS = {"active", "signed", "submitting", "leaving", "complete"}
    gap_count = 0
    for row in annotated:
        seat_num = row["seat"].seat_num
        desired = desired_blocks.get(seat_num, set())
        for c in row["cells"]:
            if c["start"].strftime("%H:%M") not in desired:
                continue
            has_success = any(
                info.get("status") in SUCCESS and not info.get("last_error")
                for info in c["accounts_info"]
            )
            # future pending 视为已计划覆盖
            if not has_success:
                is_future = (not is_today_view) or (c["end"] > now_time)
                if is_future and any(info.get("status") == "pending" for info in c["accounts_info"]):
                    has_success = True
            if not has_success and not c["user_reserved"] and not c["others_occupied"]:
                gap_count += 1
    return {"rows": annotated, "gap_count": gap_count, "occ_err": occ_err}


def _rows_to_json(annotated_rows: list[dict]) -> list[dict]:
    """把 _annotate_rows 产物压成 JSON 可序列化形状 (time → 'HH:MM')。"""
    return [
        {"seat_num": r["seat"].seat_num, "label": r["seat"].label,
         "cells": [
             {"start": c["start"].strftime("%H:%M"),
              "end": c["end"].strftime("%H:%M"),
              "accounts_info": c["accounts_info"],
              "user_reserved": c["user_reserved"],
              "others_occupied": c["others_occupied"]}
             for c in r["cells"]
         ]}
        for r in annotated_rows
    ]


async def _annotate_rows(rows, store, day: date) -> list[dict]:
    """把当日 tasks 的真实状态标注到覆盖图格子上。

    真实优先: 格子归属以**当日真实任务**为准 (同格多条时新 id 优先,
    避免被重排后遗留的旧终态任务遮挡), 与矩阵归属无关; 无真实任务时——
      - 当天已过去的格子: 不标注 (诚实显示空缺, 不渲染幻影"待执行")
      - 未来格子/未来日期: 按矩阵归属显示计划态 (pending)
    任务是 planner 产出的 1-2h 块，格子是 30min，按区间重叠匹配。
    """
    REAL_STATUSES = (
        TaskStatus.ACTIVE, TaskStatus.SIGNED, TaskStatus.SUBMITTING,
        TaskStatus.LEAVING, TaskStatus.FAILED, TaskStatus.COMPLETE,
    )
    real_by_seat: dict[str, list[Task]] = {}
    for t in await store.list_tasks(day=day):
        if t.status in REAL_STATUSES:
            real_by_seat.setdefault(t.seat_num, []).append(t)
    for tasks in real_by_seat.values():
        tasks.sort(key=lambda x: x.id or 0, reverse=True)

    now = now_cst().time()
    is_today = day == today_cst()
    out: list[dict] = []
    for sc in rows:
        cells: list[dict] = []
        for c in sc.coverage.cells:
            accs_info: list[dict] = []
            for t in real_by_seat.get(sc.seat.seat_num, []):
                if t.start_time < c.end and t.end_time > c.start:
                    accs_info.append({
                        "id": t.account_id, "status": t.status.value,
                        "task_id": t.id, "day": t.day.isoformat(),
                        "start_time": t.start_time.isoformat(timespec="minutes"),
                        "end_time": t.end_time.isoformat(timespec="minutes"),
                        "updated_at": t.updated_at,
                        "last_error": t.last_error or "",
                    })
            if not accs_info and not (is_today and c.end <= now):
                for aid in c.accounts:
                    accs_info.append({"id": aid, "status": "pending"})
            cells.append({
                "start": c.start, "end": c.end,
                "accounts_info": accs_info,
                "user_reserved": c.user_reserved,
                "others_occupied": c.others_occupied,
            })
        out.append({"seat": sc.seat, "cells": cells})
    return out


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    store = request.app.state.store
    accounts = await store.list_accounts()
    target_seats = await store.list_target_seats()
    # 首屏骨架立返: 不在此处等超星占用查询 (冷启动登录可达数十秒),
    # 覆盖数据由前端 boot 动画期间经 /api/dashboard-data 异步拉取补齐。
    today = today_cst()
    tomorrow = today + timedelta(days=1)

    def _empty_shell(day: date) -> dict:
        return {"view_day": day.isoformat(), "rows": [], "gap_count": 0, "occ_err": None}

    # 启动动画的时段徽章: 取 view_day 那天的期望时段（今天或明天的星期键）
    boot_slots: list[str] = []
    view_wd_today = weekday_key(today)
    view_wd_tomorrow = weekday_key(tomorrow)
    for s in target_seats:
        for r in desired_slots_of(s, view_wd_today):
            label = r.split("-")[0][:2] + "–" + r.split("-")[1][:2]
            if label not in boot_slots:
                boot_slots.append(label)
        for r in desired_slots_of(s, view_wd_tomorrow):
            label = r.split("-")[0][:2] + "–" + r.split("-")[1][:2]
            if label not in boot_slots:
                boot_slots.append(label)

    initial = {
        "today": _empty_shell(today),
        "tomorrow": _empty_shell(tomorrow),
        "now_hhmm": now_cst().strftime("%H:%M"),
        "recent_logs": [],
        "boot_slots": boot_slots,
        "target_seat_count": len(target_seats),
        "account_count": len(accounts),
    }
    return _templates(request).TemplateResponse(
        request, "dashboard.html",
        {
            "request": request,
            "cfg": request.app.state.cfg,
            "initial": initial,
            "active_page": "dashboard",
            "accounts": accounts,
            "target_seats": target_seats,
        },
    )


# =========================================================================
# Dashboard data — 局部刷新用的 JSON endpoint
# =========================================================================
async def _build_dashboard_data(request: Request) -> dict:
    """Collect everything dashboard.html renders, as JSON-ready dict.

    与 dashboard() 共用 _collect_day_bundle 链路，避免双份逻辑漂移。
    今天 / 明天 两块数据一并返回; 耗时点 (others_occupied) 30s 才触发一次。
    """
    cfg = request.app.state.cfg
    store = request.app.state.store
    accounts = await store.list_accounts()
    target_seats = await store.list_target_seats()

    today = today_cst()
    tomorrow = today + timedelta(days=1)
    bundle_today = await _collect_day_bundle(
        request, store, cfg, accounts, target_seats, today,
    )
    bundle_tomorrow = await _collect_day_bundle(
        request, store, cfg, accounts, target_seats, tomorrow,
    )

    recent_logs = await store.list_logs(limit=8)
    notifications = await store.list_notifications(limit=3)
    return {
        "today": {
            "view_day": today.isoformat(),
            "rows": _rows_to_json(bundle_today["rows"]),
            "gap_count": bundle_today["gap_count"],
            "occ_err": bundle_today["occ_err"],
        },
        "tomorrow": {
            "view_day": tomorrow.isoformat(),
            "rows": _rows_to_json(bundle_tomorrow["rows"]),
            "gap_count": bundle_tomorrow["gap_count"],
            "occ_err": bundle_tomorrow["occ_err"],
        },
        "now_hhmm": now_cst().strftime("%H:%M"),
        "now_ms": int(_dt.now().timestamp() * 1000),
        "recent_logs": [{
            "ts": l.ts, "level": l.level,
            "account_id": l.account_id, "message": l.message,
        } for l in recent_logs],
        "notifications": notifications,
        "target_seat_count": len(target_seats),
        "account_count": len(accounts),
    }


@router.get("/api/dashboard-data")
async def api_dashboard_data(request: Request):
    """Dashboard 局部刷新用的 JSON 视图 (前端按数据版本变化时才拉取)。"""
    return JSONResponse(await _build_dashboard_data(request))


@router.post("/api/notifications/dismiss-all")
async def api_notification_dismiss_all(request: Request):
    """一键清空全部看板通知 (积压的旧告警逐条忽略体验极差)。"""
    store = request.app.state.store
    dismissed = await store.dismiss_all_notifications()
    return JSONResponse({"ok": True, "dismissed": dismissed})


@router.post("/api/notifications/{nid}/dismiss")
async def api_notification_dismiss(request: Request, nid: int):
    """忽略一条看板通知; 只删本地记录, 不影响任何任务与账号状态。"""
    store = request.app.state.store
    if not await store.dismiss_notification(nid):
        return JSONResponse({"ok": False, "error": "notification not found"}, status_code=404)
    return JSONResponse({"ok": True})


# 他人占用查询缓存: (day, seats) -> (monotonic_ts, ok, payload)
# 命中即不触超星; 成功结果 TTL 90s, 失败结果 TTL 30s (尽快重试)
_OCC_CACHE: dict = {}


async def _fetch_others_occupied_cached(
    request: Request, store, day: date, seat_nums: list[str],
) -> tuple[list[tuple[str, _time, time]], str | None]:
    """带 TTL 的他人占用查询包装, 把超星请求频率与页面刷新解耦。"""
    import time as _time_mod
    key = (day.isoformat(), tuple(seat_nums))
    now = _time_mod.monotonic()
    hit = _OCC_CACHE.get(key)
    if hit is not None:
        ts, ok, payload = hit
        if (now - ts) < (90 if ok else 30):
            return payload
    payload = await _fetch_others_occupied(request, store, day, seat_nums)
    _OCC_CACHE[key] = (now, payload[1] is None, payload)
    return payload


@router.get("/api/version")
async def api_version(request: Request):
    """覆盖图数据版本探针: 纯本地读取, 不发起超星请求。

    v = 内存写计数 + tasks.updated_at 最大值, 两个分量均单调不减:
    库侧分量让绕过本进程的带外写入 (如人工补约脚本直改数据库) 也能
    触发前端刷新; 内存计数覆盖 tasks 之外的写 (账号/座位/通知等)。
    store 不支持库侧查询时退回纯内存计数。
    """
    store = request.app.state.store
    v = getattr(store, "data_version", 0)
    try:
        v += await store.max_task_updated_at()
    except Exception:
        pass
    return JSONResponse({
        "v": v,
        "now": now_cst().isoformat(timespec="seconds"),
        "now_ms": int(_dt.now().timestamp() * 1000),
    })


def _serialize_task(t: Task) -> dict:
    """Task → 任务看板/前端消费的 JSON 形状 (时间统一 HH:MM)。"""
    return {
        "id": t.id,
        "account_id": t.account_id,
        "seat_num": t.seat_num,
        "day": t.day.isoformat(),
        "start": t.start_time.isoformat(timespec="minutes"),
        "end": t.end_time.isoformat(timespec="minutes"),
        "status": t.status.value,
        "reserve_id": t.reserve_id,
        "last_error": t.last_error,
        "updated_at": t.updated_at,
    }


async def _tasks_payload(store, cfg, d: date) -> dict:
    """任务看板数据（HTML 首屏与 JSON 局部刷新共用）。

    失败/待定任务附可改绑账号清单（余量够/时段不撞/该座位未绑），
    前端渲染「改绑重试」下拉。
    """
    tasks = await store.list_tasks(day=d)
    payload = {
        "day": d.isoformat(),
        "tasks": [_serialize_task(t) for t in tasks],
    }
    accounts = await store.list_accounts()
    for t, tj in zip(tasks, payload["tasks"]):
        if t.status in (TaskStatus.FAILED, TaskStatus.PENDING):
            tj["candidates"] = candidate_accounts(
                accounts, seat=t.seat_num, start=t.start_time, end=t.end_time,
                exclude_id=t.account_id,
                daily_limit_hours=cfg.library.daily_reserve_hours_limit,
                weekday=weekday_key(d),
            )
    return payload


@router.get("/api/tasks")
async def api_tasks(request: Request, day: str | None = None):
    """任务看板 JSON 视图，仅读本地 DB，不发起超星请求。"""
    store = request.app.state.store
    try:
        d = date.fromisoformat(day) if day else today_cst()
    except ValueError:
        raise HTTPException(400, "day must be YYYY-MM-DD")
    return JSONResponse(await _tasks_payload(store, request.app.state.cfg, d))


# =========================================================================
# Targets — 目标座位 CRUD (v2 取代 v1 /seat-config)
# =========================================================================

@router.get("/targets", response_class=HTMLResponse)
async def targets_list(request: Request):
    store = request.app.state.store
    seats = await store.list_target_seats()
    accounts = await store.list_accounts()
    # 提前组装 "每个账号用了哪些 seats" 的反向索引
    used_by: dict[str, list[str]] = {s.seat_num: [] for s in seats}
    for a in accounts:
        # bound_seats (扁平模式) 与 seat_slots keys (精确矩阵模式) 都算"被占用"
        seen: set[str] = set()
        for sn in list(a.bound_seats) + list((a.seat_slots or {}).keys()):
            if sn in seen:
                continue
            seen.add(sn)
            used_by.setdefault(sn, []).append(a.id)
    return _templates(request).TemplateResponse(
        request, "targets_list.html",
        await _ctx(request, seats=seats, used_by=used_by,
                   error=request.query_params.get("error"),
                   active_page="targets"),
    )


@router.post("/targets")
async def targets_create(
    request: Request,
    seat_num: str = Form(...),
    label: str = Form(""),
):
    store = request.app.state.store
    sn = seat_num.strip()
    if not sn.isdigit() or not (1 <= len(sn) <= 4):
        # 校验失败：重定向到列表页并提示（不再渲染已删除的 targets_form.html）
        from urllib.parse import quote
        msg = quote(f"座位号必须是 1-4 位数字：{seat_num!r}")
        return RedirectResponse(f"/targets?error={msg}", status_code=303)
    await store.add_target_seat(sn.zfill(3), label=label)
    return RedirectResponse("/targets?created=1", status_code=303)


@router.post("/targets/{seat_num}/delete")
async def targets_delete(request: Request, seat_num: str):
    store = request.app.state.store
    await store.delete_target_seat(seat_num)
    return RedirectResponse("/targets?deleted=1", status_code=303)


# =========================================================================
# Targets — 换座（整席迁移事务）
# =========================================================================

def _split_range_ceiling(s: _time, e: _time, max_hours: float) -> list[tuple[_time, _time]]:
    """把时段按单段时长上限切成子段列表（服务端 max_reserve_hours 硬约束）。"""
    base = date(2000, 1, 1)
    cur = _dt.combine(base, s)
    end = _dt.combine(base, e)
    step = timedelta(hours=max_hours)
    out: list[tuple[_time, _time]] = []
    while cur < end:
        nxt = min(cur + step, end)
        out.append((cur.time(), nxt.time()))
        cur = nxt
    return out


def _overlaps(a1: _dt, a2: _dt, b1: _dt, b2: _dt) -> bool:
    """两个 [start, end) 窗口是否有交集。"""
    return a1 < b2 and b1 < a2


async def _signback_task_now(sched, store, acc: Account, t: Task) -> tuple[bool, str]:
    """立即对在约任务执行真签退（signback 通道），返回 (是否成功, 消息)。"""
    client = await sched.client_ready(acc)
    try:
        if not client.cookies() and not await sched.login_and_persist(acc, client, "签退登录"):
            return False, "登录失败，无法签退"
        r = await client.signback(t.reserve_id)
    except Exception as e:
        return False, f"登录/签退异常: {e}"
    ok = bool(r.get("success"))
    msg = str(r.get("msg") or ("ok" if ok else "signback failed"))
    await store.log_action(
        acc.id, "signback", str(t.reserve_id), str(r)[:500], ok, msg,
    )
    return ok, msg


@router.post("/targets/replace")
async def targets_replace(
    request: Request,
    old_seat: str = Form(...),
    new_seat: str = Form(""),
    rebook_today: str = Form(""),
    rebook_tomorrow: str = Form(""),
):
    """整席换防：矩阵迁移 → 旧席在约释放/任务清理 → 新席注册 → 可选补约。

    会对旧座位上仍在进行中的预约立即发起真签退（真实账号操作，
    仅应由用户在页面上明确点击触发）。
    """
    from urllib.parse import quote

    store = request.app.state.store
    sched = request.app.state.sched
    max_h = request.app.state.cfg.library.max_reserve_hours

    old_sn = old_seat.strip().zfill(3)
    new_raw = new_seat.strip()
    if not new_raw.isdigit() or not (1 <= len(new_raw) <= 4):
        return RedirectResponse(
            f"/targets?error={quote('新座位号必须是 1-4 位数字')}", status_code=303)
    new_sn = new_raw.zfill(3)

    seat_map = {s.seat_num: s for s in await store.list_target_seats()}
    if old_sn not in seat_map:
        return RedirectResponse(
            f"/targets?error={quote(f'旧座位 {old_sn} 不是已注册目标')}", status_code=303)
    if new_sn == old_sn:
        return RedirectResponse(
            f"/targets?error={quote('新旧座位号相同')}", status_code=303)
    if new_sn in seat_map:
        return RedirectResponse(
            f"/targets?error={quote(f'新座位 {new_sn} 已注册，请先删除再替换')}",
            status_code=303)

    report: dict = {
        "old": old_sn, "new": new_sn,
        "accounts": [], "released": [], "closed": [],
        "janitors": [], "rebooked": [], "skipped": [], "errors": [],
    }
    now = now_cst()
    today = today_cst()
    tomorrow = today + timedelta(days=1)

    # 1) 迁移守护矩阵（seat_slots 键改名 + bound_seats 替换）
    for acc in await store.list_accounts():
        changed = False
        if acc.seat_slots and old_sn in acc.seat_slots:
            slots = dict(acc.seat_slots)
            moved = slots.pop(old_sn)
            if new_sn in slots:
                report["errors"].append(
                    f"{acc.id}: 新座位已有时段配置，矩阵未迁移（请手动核对）")
                continue
            slots[new_sn] = moved
            acc.seat_slots = slots
            changed = True
        if old_sn in (acc.bound_seats or []):
            acc.bound_seats = [new_sn if x == old_sn else x
                               for x in acc.bound_seats]
            changed = True
        if changed:
            await store.upsert_account(acc)
            report["accounts"].append({
                "id": acc.id,
                "slots": _fmt_weekly((acc.seat_slots or {}).get(new_sn)),
            })

    # 2) 清理旧座位在途任务（今天及未来）
    old_tasks = [t for t in await store.list_tasks(seat_num=old_sn)
                 if t.day >= today]
    for t in sorted(old_tasks, key=lambda x: (x.day, x.start_time)):
        desc = (f"#{t.id} {t.account_id} {t.day.isoformat()} "
                f"{t.start_time.strftime('%H:%M')}-{t.end_time.strftime('%H:%M')}")
        t_start = at_cst(t.day, t.start_time)
        t_end = at_cst(t.day, t.end_time)

        if t.status in (TaskStatus.PENDING, TaskStatus.READY,
                        TaskStatus.SUBMITTING, TaskStatus.FAILED) or not t.reserve_id:
            await store.update_task_status(
                t.id, TaskStatus.FAILED, last_error="换座迁移: 旧座位任务已关闭")
            report["closed"].append({"desc": desc, "note": t.status.value})
            continue
        if now >= t_end:
            await store.update_task_status(t.id, TaskStatus.COMPLETE)
            report["closed"].append({"desc": desc, "note": "时段已结束, 置完成"})
            continue
        if now >= t_start:
            acc = await store.get_account(t.account_id)
            if not acc:
                report["errors"].append(f"{desc}: 账号不存在, 无法签退（任务保留）")
                continue
            ok, msg = await _signback_task_now(sched, store, acc, t)
            if ok:
                await store.update_task_status(t.id, TaskStatus.COMPLETE)
                report["released"].append({"desc": desc, "msg": msg})
            else:
                report["errors"].append(
                    f"签退失败 {desc}: {msg}（任务保留, 可在任务页手动签退）")
            continue
        # 未来时段：转为释放型任务 —— 到点自动签到, 短持后自动签退
        new_end_dt = min(
            _dt.combine(t.day, t.end_time),
            _dt.combine(t.day, t.start_time) + timedelta(minutes=20),
        )
        if new_end_dt <= _dt.combine(t.day, t.start_time) + timedelta(minutes=5):
            report["skipped"].append(
                {"desc": desc, "reason": "时段过短, 保留原任务到点正常签退"})
            continue
        await store.shorten_task_end(t.id, new_end_dt.time())
        report["janitors"].append({
            "desc": desc,
            "new_end": new_end_dt.strftime("%H:%M"),
        })

    # 3) 换目标座位行（注册新席继承标签与期望时段 + 软删旧席）
    await store.add_target_seat(
        new_sn, label=seat_map[old_sn].label,
        desired_slots=seat_map[old_sn].desired_slots,
    )
    await store.delete_target_seat(old_sn)

    # 4) 可选补约（新座位矩阵今天/明天的时段, 跳过账号被占用的窗口）
    rebook_days: list[date] = []
    if rebook_today:
        rebook_days.append(today)
    if rebook_tomorrow:
        rebook_days.append(tomorrow)
    for day in rebook_days:
        for acc in await store.list_accounts():
            lib_ = request.app.state.cfg.library
            day_val = slots_for_weekday(
                (acc.seat_slots or {}).get(new_sn), weekday_key(day))
            if day_val == "full":
                ranges = [f"{lib_.open_time}-{lib_.close_time}"]
            elif isinstance(day_val, list):
                ranges = day_val
            else:
                ranges = []
            if not ranges:
                continue
            acc_tasks = await store.list_tasks(account_id=acc.id, day=day)
            busy = [
                (at_cst(x.day, x.start_time), at_cst(x.day, x.end_time))
                for x in acc_tasks
                if x.reserve_id and x.status in (
                    TaskStatus.ACTIVE, TaskStatus.SIGNED, TaskStatus.LEAVING)
            ]
            new_seat_starts = {
                x.start_time for x in acc_tasks if x.seat_num == new_sn
            }
            for rng in ranges:
                try:
                    rs, re_ = parse_range(rng)
                except Exception:
                    report["errors"].append(
                        f"{acc.id}: 时段 {rng!r} 无法解析, 跳过")
                    continue
                for s, e in _split_range_ceiling(rs, re_, max_h):
                    seg_desc = (f"{acc.id} {new_sn} {day.isoformat()} "
                                f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}")
                    if day == today and now >= at_cst(day, e):
                        report["skipped"].append(
                            {"desc": seg_desc, "reason": "今日该时段已过"})
                        continue
                    if s in new_seat_starts:
                        report["skipped"].append(
                            {"desc": seg_desc, "reason": "已存在同段任务"})
                        continue
                    seg_s, seg_e = at_cst(day, s), at_cst(day, e)
                    holder = next((b for b in busy if _overlaps(seg_s, seg_e, *b)), None)
                    if holder:
                        report["skipped"].append({
                            "desc": seg_desc,
                            "reason": (f"账号当日被旧预约占用至 "
                                       f"{holder[1].strftime('%H:%M')}，需手动补约"),
                        })
                        continue
                    tid = await store.add_task(Task(
                        id=None, account_id=acc.id, day=day,
                        start_time=s, end_time=e, seat_num=new_sn,
                        status=TaskStatus.READY,
                    ))
                    loaded = await store.get_task(tid)
                    try:
                        await sched._run_submit(acc, loaded)
                        final = await store.get_task(tid)
                        ok = bool(final and final.reserve_id)
                        report["rebooked"].append({
                            "desc": seg_desc, "ok": ok,
                            "msg": (f"预约号 {final.reserve_id}" if ok
                                    else (final.last_error if final else "任务丢失")),
                        })
                    except Exception as ex:
                        report["errors"].append(f"补约异常 {seg_desc}: {ex}")

    ctx = await _ctx(request, active_page="targets")
    ctx["report"] = report
    return _templates(request).TemplateResponse(
        request, "targets_replace_result.html", ctx,
    )


# =========================================================================
# Bindings — 座位绑定矩阵管理（余量 / 自动绑定 / 手动绑定）
# =========================================================================

def _count_bindings(matrices: dict[str, dict[str, dict[str, list[str]]]]) -> int:
    """统计一批矩阵里的绑定总数（支持新星期嵌套形态）。"""
    total = 0
    for m in matrices.values():
        for spec in m.values():
            if isinstance(spec, dict):
                total += sum(len(v) for v in spec.values())
            else:
                total += len(spec)   # 旧 list 形态兼容
    return total


@router.get("/bindings", response_class=HTMLResponse)
async def bindings_list(request: Request):
    """绑定矩阵页：各账号余量 + 各座位期望时段覆盖情况。"""
    store = request.app.state.store
    lib = request.app.state.cfg.library
    limit = lib.daily_reserve_hours_limit
    seats = await store.list_target_seats()
    accounts = await store.list_accounts()
    # 星期列（用于 Jinja 循环 7 天）
    weekday_cols = list(zip(WEEKDAY_KEYS, [WEEKDAY_LABELS[w] for w in WEEKDAY_KEYS]))

    desired: dict[str, dict[str, list[str]]] = {
        s.seat_num: {wd: desired_slots_of(s, wd) for wd in WEEKDAY_KEYS}
        for s in seats
    }
    seat_bindings: dict[str, list[dict]] = {s.seat_num: [] for s in seats}
    for a in accounts:
        for seat, val in (a.seat_slots or {}).items():
            for wd in WEEKDAY_KEYS:
                day_val = slots_for_weekday(val, wd)
                if not isinstance(day_val, list):
                    continue
                for r in day_val:
                    seat_bindings.setdefault(seat, []).append(
                        {"account_id": a.id, "range": r, "weekday": wd})
    uncovered: dict[str, dict[str, list[str]]] = {}
    for seat, per_day in desired.items():
        uncovered[seat] = {}
        for wd in WEEKDAY_KEYS:
            bound = {b["range"] for b in seat_bindings.get(seat, [])
                     if b["weekday"] == wd}
            uncovered[seat][wd] = [
                s_ for s_ in per_day[wd] if s_ not in bound]

    def _blocks_of(ranges: list[str]) -> list[str]:
        """把 HH:MM-HH:MM 时段展开成 30 分钟块起点列表（复选框预勾选用）。"""
        out: list[str] = []
        for r in ranges:
            try:
                s, e = parse_range(r)
            except Exception:
                continue
            cur = _dt.combine(date.today(), s)
            end = _dt.combine(date.today(), e)
            while cur < end:
                out.append(cur.strftime("%H:%M"))
                cur += timedelta(minutes=30)
        return sorted(set(out))

    ticks: list[tuple[str, str]] = []
    t = _dt.combine(date.today(), parse_hhmm(lib.open_time))
    close = _dt.combine(date.today(), parse_hhmm(lib.close_time))
    while t < close:
        nxt = t + timedelta(minutes=30)
        ticks.append((t.strftime("%H:%M"), nxt.strftime("%H:%M")))
        t = nxt
    from seatbot.bindings import DEFAULT_DESIRED_SLOTS

    desired_blocks: dict[str, dict[str, list[str]]] = {
        s.seat_num: {
            wd: _blocks_of(desired_slots_of(s, wd)) for wd in WEEKDAY_KEYS
        }
        for s in seats
    }
    using_default = {
        s.seat_num: not s.desired_slots for s in seats
    }
    margins = account_margins(accounts, daily_limit_hours=limit)
    ctx = await _ctx(request, active_page="bindings")
    ctx.update(
        weekday_cols=weekday_cols,
        seats=seats, desired=desired, seat_bindings=seat_bindings,
        uncovered=uncovered, margins=margins,
        desired_blocks=desired_blocks, using_default=using_default,
        ticks=ticks,
        total_remaining=sum(
            sum(d["remaining_hours"] for d in m["days"].values()) for m in margins),
        total_used=sum(
            sum(d["used_hours"] for d in m["days"].values()) for m in margins),
        accounts=accounts, daily_limit=limit,
        max_seg=lib.max_reserve_hours,
        gap_total={s.seat_num: sum(
            len(uncovered[s.seat_num][wd]) for wd in WEEKDAY_KEYS)
            for s in seats},
        msg=request.query_params.get("msg"),
        error=request.query_params.get("error"),
    )
    return _templates(request).TemplateResponse(request, "bindings_list.html", ctx)


@router.post("/bindings/auto")
async def bindings_auto(request: Request):
    """自动绑定：为所有未覆盖的 (星期几, 座位, 期望时段) 挑账号，保留既有绑定。"""
    from urllib.parse import quote

    store = request.app.state.store
    lib = request.app.state.cfg.library
    seats = await store.list_target_seats()
    accounts = await store.list_accounts()
    if not seats or not accounts:
        return RedirectResponse(
            f"/bindings?error={quote('没有目标座位或守护账号')}", status_code=303)
    # desired_slots_of() 按星期几返回；auto_assign 需要 {seat: {wd: [...]}} 形态
    desired = {}
    for s in seats:
        desired[s.seat_num] = {
            wd: desired_slots_of(s, wd) for wd in WEEKDAY_KEYS
        }
    before = _count_bindings({a.id: (a.seat_slots or {}) for a in accounts})
    matrices, unfillable = auto_assign(
        accounts, desired,
        max_seg_hours=lib.max_reserve_hours,
        daily_limit_hours=lib.daily_reserve_hours_limit,
    )
    changed = 0
    for a in accounts:
        new = matrices.get(a.id)
        if new is None or new == (a.seat_slots or {}):
            continue
        a.seat_slots = new
        a.bound_seats = sorted(new.keys())
        await store.upsert_account(a)
        changed += 1
    filled = _count_bindings(matrices) - before
    msg = f"自动绑定完成：新增 {filled} 条，更新 {changed} 个账号"
    if unfillable:
        msg += "；仍无法覆盖 " + "；".join(unfillable)
    return RedirectResponse(f"/bindings?msg={quote(msg)}", status_code=303)


@router.post("/bindings/manual")
async def bindings_manual(
    request: Request,
    account_id: str = Form(...),
    seat_num: str = Form(...),
    start: str = Form(...),
    end: str = Form(...),
    weekday: str = Form("mon"),
):
    """手动绑定/切换：设置某账号在某座位某天的时段。"""
    from urllib.parse import quote

    store = request.app.state.store
    lib = request.app.state.cfg.library
    acc = await store.get_account(account_id)
    if not acc:
        raise HTTPException(404, f"account {account_id} not found")
    wd = weekday.strip().lower()
    if wd not in WEEKDAY_KEYS:
        return RedirectResponse(
            f"/bindings?error={quote(f'非法星期: {weekday!r}')}", status_code=303)
    sn = seat_num.strip().zfill(3)
    if sn not in {s.seat_num for s in await store.list_target_seats()}:
        return RedirectResponse(
            f"/bindings?error={quote(f'座位 {sn} 不是已注册目标')}", status_code=303)
    try:
        s = parse_hhmm(start)
        e = parse_hhmm(end)
        rng = f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}"
    except (ValueError, TypeError) as exc:
        return RedirectResponse(
            f"/bindings?error={quote(f'时间格式错误: {exc}')}", status_code=303)
    matrix = _matrix_set_day(dict(acc.seat_slots or {}), sn, wd, [rng])
    try:
        validate_matrix(
            matrix, max_seg_hours=lib.max_reserve_hours,
            daily_limit_hours=lib.daily_reserve_hours_limit,
        )
    except ValueError as exc:
        return RedirectResponse(
            f"/bindings?error={quote(str(exc))}", status_code=303)
    acc.seat_slots = matrix
    acc.bound_seats = sorted(matrix.keys())
    await store.upsert_account(acc)
    return RedirectResponse(
        f"/bindings?msg={quote(f'已绑定 {account_id} → {sn} {WEEKDAY_LABELS[wd]} {rng}')}",
        status_code=303)


@router.post("/bindings/delete")
async def bindings_delete(
    request: Request,
    account_id: str = Form(...),
    seat_num: str = Form(...),
    weekday: str = Form(""),
):
    """解绑：移除某账号在某座位的时段；给定 weekday 只解绑该天，否则整座位解绑。"""
    from urllib.parse import quote

    store = request.app.state.store
    acc = await store.get_account(account_id)
    if not acc:
        raise HTTPException(404)
    sn = seat_num.strip().zfill(3)
    matrix = dict(acc.seat_slots or {})
    if sn not in matrix:
        return RedirectResponse(
            f"/bindings?error={quote(f'{account_id} 在 {sn} 无绑定')}",
            status_code=303)
    if weekday:
        wd = weekday.strip().lower()
        if wd not in WEEKDAY_KEYS:
            return RedirectResponse(
                f"/bindings?error={quote(f'非法星期: {weekday!r}')}", status_code=303)
        matrix = _matrix_set_day(matrix, sn, wd, [])
    else:
        matrix.pop(sn, None)
    acc.seat_slots = matrix
    acc.bound_seats = sorted(matrix.keys())
    await store.upsert_account(acc)
    day_part = f" {WEEKDAY_LABELS.get(wd, wd)}" if weekday else ""
    return RedirectResponse(
        f"/bindings?msg={quote(f'已解绑 {account_id} × {sn}{day_part}')}", status_code=303)


@router.post("/bindings/desired")
async def bindings_desired(
    request: Request,
    seat_num: str = Form(...),
    blocks_mon: list[str] = Form(default=[]),
    blocks_tue: list[str] = Form(default=[]),
    blocks_wed: list[str] = Form(default=[]),
    blocks_thu: list[str] = Form(default=[]),
    blocks_fri: list[str] = Form(default=[]),
    blocks_sat: list[str] = Form(default=[]),
    blocks_sun: list[str] = Form(default=[]),
):
    """编辑期望守护时段（按星期几，30 分钟块复选，连续块自动合并）。

    seat_num 为 "__ALL__" 时把同组块应用到所有已启用座位；
    7 天全不勾 = 清空自定义恢复默认三段；部分天勾选时未勾的天 = 该天不检查缺口。
    """
    from urllib.parse import quote

    store = request.app.state.store
    lib = request.app.state.cfg.library
    seats = await store.list_target_seats()
    sn_raw = seat_num.strip()
    if sn_raw == "__ALL__":
        targets = [s.seat_num for s in seats]
        if not targets:
            return RedirectResponse(
                f"/bindings?error={quote('没有已启用座位')}", status_code=303)
    else:
        sn = sn_raw.zfill(3)
        if sn not in {s.seat_num for s in seats}:
            return RedirectResponse(
                f"/bindings?error={quote(f'座位 {sn} 不存在')}", status_code=303)
        targets = [sn]

    open_t = parse_hhmm(lib.open_time)
    close_t = parse_hhmm(lib.close_time)

    def _merge(block_list: list[str]) -> tuple[list[str], str | None]:
        """30 分钟块起点 → 合并时段段列表；超长返回错误消息。"""
        starts: list[_time] = []
        for b in block_list:
            try:
                t = parse_hhmm(b.strip())
            except Exception:
                return [], f"时间块 {b!r} 格式错误"
            e = (_dt.combine(date.today(), t) + timedelta(minutes=30)).time()
            if t < open_t or e > close_t:
                return [], f"时间块 {b} 超出开放时间 {lib.open_time}-{lib.close_time}"
            if t not in starts:
                starts.append(t)
        starts.sort()
        merged: list[tuple[_time, _time]] = []
        for t in starts:
            e = (_dt.combine(date.today(), t) + timedelta(minutes=30)).time()
            if merged and merged[-1][1] == t:
                merged[-1] = (merged[-1][0], e)
            else:
                merged.append((t, e))
        ranges: list[str] = []
        for s, e in merged:
            rng = f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}"
            h = (_dt.combine(date.today(), e) - _dt.combine(date.today(), s)
                 ).total_seconds() / 3600
            if h > lib.max_reserve_hours + 1e-9:
                return [], (f"连续勾选形成时段 {rng} 长 {h:g}h，"
                            f"超过单段上限 {lib.max_reserve_hours:g}h——请在中间断开勾选")
            ranges.append(rng)
        return ranges, None

    per_day: dict[str, list[str]] = {}
    raw_blocks = {
        "mon": blocks_mon, "tue": blocks_tue, "wed": blocks_wed,
        "thu": blocks_thu, "fri": blocks_fri, "sat": blocks_sat, "sun": blocks_sun,
    }
    for wd in WEEKDAY_KEYS:
        ranges, err = _merge(raw_blocks[wd])
        if err:
            return RedirectResponse(
                f"/bindings?error={quote(err)}", status_code=303)
        per_day[wd] = ranges

    for sn in targets:
        if all(not per_day[wd] for wd in WEEKDAY_KEYS):
            await store.set_target_seat_desired(sn, None)   # 全空 → 恢复默认
        else:
            await store.set_target_seat_desired(sn, per_day)
    scope = "所有座位" if sn_raw == "__ALL__" else "、".join(targets)
    filled = sum(1 for wd in WEEKDAY_KEYS if per_day[wd])
    msg = f"已更新 {scope} 的期望时段（{filled}/7 天有守护块）"
    return RedirectResponse(f"/bindings?msg={quote(msg)}", status_code=303)


# =========================================================================
# User Reserved (用户亲述已预约段, scheduler 跳过)
# =========================================================================
@router.get("/user-reserved", response_class=HTMLResponse)
async def user_reserved_list(request: Request):
    store = request.app.state.store
    rows = await store.list_user_reserved()
    return _templates(request).TemplateResponse(
        request, "user_reserved_list.html",
        await _ctx(request, reserved=rows, error=None, active_page="user-reserved"),
    )


@router.post("/user-reserved")
async def user_reserved_create(
    request: Request,
    account_id: str = Form(...),
    seat_num: str = Form(...),
    day: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    note: str = Form(""),
):
    from datetime import date as _date, time as _time
    store = request.app.state.store
    try:
        d = _date.fromisoformat(day)
        s = _time(*map(int, start_time.split(":")))
        e = _time(*map(int, end_time.split(":")))
        if e <= s:
            raise ValueError("end must be after start")
        sn = seat_num.strip()
        if not sn.isdigit():
            raise ValueError("seat_num must be digits")
    except Exception as ex:
        rows = await store.list_user_reserved()
        return _templates(request).TemplateResponse(
            request, "user_reserved_list.html",
            await _ctx(request, reserved=rows,
                        error=f"input error: {ex}",
                        active_page="user-reserved"),
            status_code=400,
        )
    if not await store.get_account(account_id):
        rows = await store.list_user_reserved()
        return _templates(request).TemplateResponse(
            request, "user_reserved_list.html",
            await _ctx(request, reserved=rows,
                        error=f"账号不存在: {account_id}",
                        active_page="user-reserved"),
            status_code=400,
        )
    if d < today_cst():
        rows = await store.list_user_reserved()
        return _templates(request).TemplateResponse(
            request, "user_reserved_list.html",
            await _ctx(request, reserved=rows,
                        error="日期不能早于今天",
                        active_page="user-reserved"),
            status_code=400,
        )
    await store.add_user_reserved(
        account_id=account_id,
        seat_num=sn.zfill(3),
        day=d, start_time=s, end_time=e,
        note=note,
    )
    return RedirectResponse("/user-reserved?created=1", status_code=303)


@router.post("/user-reserved/{rid}/delete")
async def user_reserved_delete(request: Request, rid: int):
    store = request.app.state.store
    await store.delete_user_reserved(rid)
    return RedirectResponse("/user-reserved?deleted=1", status_code=303)


# =========================================================================
# Accounts CRUD (v2: 包含 bound_seats)
# =========================================================================
@router.get("/accounts", response_class=HTMLResponse)
async def accounts_list(request: Request):
    return _templates(request).TemplateResponse(
        request, "accounts_list.html",
        await _ctx(request, active_page="accounts"),
    )


async def _matrix_ctx(request: Request, store, account: Account | None) -> dict:
    """账号表单矩阵编辑器所需的上下文（座位清单 + 全账号时段用于覆盖预览）。"""
    accounts = await store.list_accounts()
    target_seats = await store.list_target_seats()
    ctx = await _ctx(request, account=account, error=None, active_page="accounts")
    ctx["matrix_seats"] = [s.seat_num for s in target_seats]
    ctx["matrix_initial"] = (account.seat_slots if account and account.seat_slots else {})
    ctx["matrix_others"] = [
        {"id": a.id, "seatSlots": a.seat_slots or {}}
        for a in accounts if a.id != (account.id if account else None)
    ]
    lib = request.app.state.cfg.library
    ctx["matrix_open"] = lib.open_time
    ctx["matrix_close"] = lib.close_time
    ctx["matrix_max_hours"] = lib.max_reserve_hours
    return ctx


@router.get("/accounts/new", response_class=HTMLResponse)
async def accounts_new(request: Request):
    store = request.app.state.store
    return _templates(request).TemplateResponse(
        request, "accounts_form.html",
        await _matrix_ctx(request, store, None),
    )


@router.post("/accounts")
async def accounts_create(
    request: Request,
    phone: str = Form(...),
    password: str = Form(...),
):
    """新建守护账号：仅凭手机号+密码，实际登录校验通过后以平台实名信息作为账号 ID。

    实名信息来自办公端 `oa_name` cookie；重名账号自动追加序号，
    实名信息不可得时退回 u{uid}；同一手机号不可重复导入。
    """
    import re

    from urllib.parse import unquote

    store = request.app.state.store
    phone = phone.strip()
    if not phone.isdigit() or not (8 <= len(phone) <= 20):
        return _templates(request).TemplateResponse(
            request, "accounts_form.html",
            await _ctx(request, account=None,
                       error="手机号必须是 8-20 位数字",
                       active_page="accounts"),
            status_code=400,
        )
    client = ChaoxingClient()
    uid: str | None = None
    name: str = ""
    try:
        try:
            await client.login(phone, password)
            jar = client.cookies()
            uid = jar.get("uid") or jar.get("_uid")
            name = unquote(jar.get("oa_name") or "")
            if not name:
                # 登录链路可能未途经办公端鉴权, 读一次座位占用接口补齐身份 cookie
                try:
                    await client.get_used_times(
                        request.app.state.cfg.library.room_id, "001",
                        today_cst().isoformat())
                except Exception:
                    pass
                name = unquote(client.cookies().get("oa_name") or "")
        except ChaoxingError as e:
            return _templates(request).TemplateResponse(
                request, "accounts_form.html",
                await _ctx(request, account=None,
                           error=f"学习通登录失败：{e}",
                           active_page="accounts"),
                status_code=400,
            )
    finally:
        await client.close()
    if not uid or not str(uid).strip().isdigit():
        return _templates(request).TemplateResponse(
            request, "accounts_form.html",
            await _ctx(request, account=None,
                       error="登录成功但未能从学习通获取 uid，未保存",
                       active_page="accounts"),
            status_code=400,
        )
    uid = str(uid).strip()

    for a in await store.list_accounts():
        if a.phone == phone:
            return _templates(request).TemplateResponse(
                request, "accounts_form.html",
                await _ctx(request, account=None,
                           error=f"该手机号已导入为账号 {a.id}，无需重复创建",
                           active_page="accounts"),
                status_code=400,
            )

    base = re.sub(r"[\s/?#%&+]", "", name)[:32] or f"u{uid}"
    acc_id = base
    suffix = 2
    while await store.get_account(acc_id):
        acc_id = f"{base}{suffix}"
        suffix += 1

    acc = Account(
        id=acc_id, phone=phone, password=password,
        slots="full", bound_seats=[], seat_slots=None,
    )
    try:
        await store.upsert_account(acc)
    except Exception as e:
        return _templates(request).TemplateResponse(
            request, "accounts_form.html",
            await _ctx(request, account=acc, error=str(e), active_page="accounts"),
            status_code=400,
        )
    await store.log_action(
        acc_id, "import", uid,
        f"新建账号: uid={uid} -> id={acc_id}", True, "",
    )
    return RedirectResponse(f"/accounts/{acc_id}/edit?created=1", status_code=303)


@router.get("/accounts/{acc_id}/edit", response_class=HTMLResponse)
async def accounts_edit(request: Request, acc_id: str):
    store = request.app.state.store
    acc = await store.get_account(acc_id)
    if not acc:
        raise HTTPException(404)
    return _templates(request).TemplateResponse(
        request, "accounts_form.html",
        await _matrix_ctx(request, store, acc),
    )


@router.post("/accounts/{acc_id}")
async def accounts_update(
    request: Request, acc_id: str,
    phone: str = Form(...),
    password: str = Form(""),
    seat_slots: str = Form("{}"),
):
    """更新账号凭据与座位绑定矩阵（bound_seats 由矩阵键自动推导）。"""
    store = request.app.state.store
    existing = await store.get_account(acc_id)
    if not existing:
        raise HTTPException(404)
    lib = request.app.state.cfg.library
    try:
        seat_slots_val = _parse_seat_slots(
            seat_slots, lib.max_reserve_hours, lib.daily_reserve_hours_limit)
    except ValueError as e:
        return _templates(request).TemplateResponse(
            request, "accounts_form.html",
            await _ctx(request, account=existing, error=str(e), active_page="accounts"),
            status_code=400,
        )
    existing.phone = phone
    if password.strip():
        existing.password = password
    existing.slots = "full"
    existing.seat_slots = seat_slots_val
    existing.bound_seats = sorted((seat_slots_val or {}).keys())
    await store.upsert_account(existing)
    return RedirectResponse("/accounts?updated=1", status_code=303)


@router.post("/accounts/{acc_id}/delete")
async def accounts_delete(request: Request, acc_id: str):
    store = request.app.state.store
    await store.delete_account(acc_id)
    return RedirectResponse("/accounts?deleted=1", status_code=303)


@router.post("/accounts/{acc_id}/test-login")
async def accounts_test_login(request: Request, acc_id: str):
    store = request.app.state.store
    db_acc = await store.get_account(acc_id)
    if not db_acc:
        return JSONResponse({"ok": False, "error": "account not found"}, status_code=404)
    client = ChaoxingClient()
    try:
        await client.login(db_acc.phone, db_acc.password)
        await store.save_account_cookies(db_acc.id, client.cookies())
        return JSONResponse({"ok": True})
    except ChaoxingError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    finally:
        await client.close()


# =========================================================================
# Tasks CRUD (含 seat_num 列)
# =========================================================================
@router.get("/tasks", response_class=HTMLResponse)
async def tasks_list(
    request: Request,
    day: str | None = None,
):
    store = request.app.state.store
    try:
        d = date.fromisoformat(day) if day else today_cst()
    except ValueError:
        raise HTTPException(400, "day must be YYYY-MM-DD")
    payload = await _tasks_payload(store, request.app.state.cfg, d)
    ctx = await _ctx(request, active_page="tasks")
    # 传 dict 由模板 |tojson 一次性编码; 传已 dumps 的字符串会被二次编码致前端解析成字符串
    ctx["tasks_initial"] = payload
    ctx["prev_day"] = (d - timedelta(days=1)).isoformat()
    ctx["next_day"] = (d + timedelta(days=1)).isoformat()
    return _templates(request).TemplateResponse(
        request, "tasks_list.html", ctx,
    )


@router.post("/tasks/quick-reserve")
async def quick_reserve(
    request: Request,
    account_id: str = Form(...),
    seat_num: str = Form(...),
    start: str = Form(...),
    end: str = Form(...),
):
    sched = request.app.state.sched
    store = request.app.state.store
    acc = await store.get_account(account_id)
    if not acc:
        raise HTTPException(404, f"account {account_id} not found")
    try:
        s = parse_hhmm(start)
        e = parse_hhmm(end)
        if e <= s:
            raise ValueError("end must be after start")
    except (ValueError, AttributeError) as exc:
        raise HTTPException(400, f"时间格式错误: {exc}")
    sn = seat_num.zfill(3)
    # 校验这个 seat 是注册的 target
    seats = [s.seat_num for s in await store.list_target_seats()]
    if sn not in seats:
        raise HTTPException(400, f"seat {sn} not a registered target")
    t = Task(
        id=None, account_id=acc.id, day=today_cst(),
        start_time=s, end_time=e, seat_num=sn,
        status=TaskStatus.READY,
    )
    tid = await store.add_task(t)
    loaded = await store.get_task(tid)
    if loaded:
        await sched._run_submit(acc, loaded)
    return RedirectResponse("/tasks?reserved=1", status_code=303)


@router.post("/tasks/import")
async def tasks_import(
    request: Request,
    account_id: str = Form(...),
    seat_num: str = Form(...),
    day: str = Form(...),
    start: str = Form(...),
    end: str = Form(...),
    reserve_id: str = Form(...),
):
    """导入用户在 App 手动抢到的预约号，接入自动签到/签退生命周期。

    仅写入本地任务状态（status=ACTIVE + reserve_id），不发起任何超星请求；
    scheduler 会在时段开始时自动签到、结束前自动签退。
    """
    store = request.app.state.store
    acc = await store.get_account(account_id)
    if not acc:
        raise HTTPException(404, f"account {account_id} not found")
    try:
        d = date.fromisoformat(day)
        s = parse_hhmm(start)
        e = parse_hhmm(end)
        if e <= s:
            raise ValueError("end must be after start")
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, f"日期/时间格式错误: {exc}")
    rid_raw = reserve_id.strip()
    if not rid_raw.isdigit():
        raise HTTPException(400, "预约号必须是数字")
    rid = int(rid_raw)
    if rid <= 0:
        raise HTTPException(400, "预约号非法")
    if d < today_cst():
        raise HTTPException(400, "日期不能早于今天")
    now = now_cst()
    if d == today_cst() and now >= at_cst(d, e):
        raise HTTPException(400, "该时段今天已结束，无法导入")
    sn = seat_num.strip().zfill(3)
    seats = [x.seat_num for x in await store.list_target_seats()]
    if sn not in seats:
        raise HTTPException(400, f"seat {sn} not a registered target")
    for t in await store.list_tasks(account_id=acc.id, day=d, seat_num=sn):
        if t.start_time == s and t.reserve_id == rid:
            raise HTTPException(400, "该预约号已导入过")
        if t.start_time == s and t.reserve_id:
            raise HTTPException(
                400, f"同账号同座位同时段已存在任务 #{t.id}（预约号 {t.reserve_id}）")
    tid = await store.add_task(Task(
        id=None, account_id=acc.id, day=d,
        start_time=s, end_time=e, seat_num=sn,
        status=TaskStatus.ACTIVE, reserve_id=rid,
    ))
    await store.log_action(
        acc.id, "import", str(rid),
        f"手动导入预约: seat={sn} {d.isoformat()} "
        f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')} → task#{tid}",
        True, "",
    )
    return RedirectResponse(f"/tasks?day={d.isoformat()}&imported=1", status_code=303)


@router.post("/tasks/{task_id}/reassign")
async def task_reassign(
    request: Request,
    task_id: int,
    account_id: str = Form(...),
):
    """失败任务改绑重试：把该 (座位, 时段) 换给新账号并立即重新提交。

    同步迁移守护矩阵（旧账号释放该座位绑定、新账号接手），保持
    矩阵与实际持约一致；新账号校验不过（余量/重叠/每座位1段）则拒绝。
    """
    from urllib.parse import quote

    store = request.app.state.store
    sched = request.app.state.sched
    lib = request.app.state.cfg.library
    t = await store.get_task(task_id)
    if not t:
        raise HTTPException(404)
    if t.status not in (TaskStatus.FAILED, TaskStatus.PENDING):
        return RedirectResponse(
            f"/tasks?day={t.day.isoformat()}&error="
            f"{quote(f'任务 #{task_id} 状态为 {t.status.value}，只有失败/待定任务可改绑')}",
            status_code=303)
    new_acc = await store.get_account(account_id)
    if not new_acc:
        raise HTTPException(404, f"account {account_id} not found")
    if account_id == t.account_id:
        return RedirectResponse(
            f"/tasks?day={t.day.isoformat()}&error={quote('不能改绑给当前账号')}",
            status_code=303)
    # 以服务端实时候选为准, 防止页面滞留旧清单导致越权改绑
    candidates = candidate_accounts(
        await store.list_accounts(), seat=t.seat_num,
        start=t.start_time, end=t.end_time, exclude_id=t.account_id,
        daily_limit_hours=lib.daily_reserve_hours_limit,
        weekday=weekday_key(t.day),
    )
    if account_id not in [c["id"] for c in candidates]:
        return RedirectResponse(
            f"/tasks?day={t.day.isoformat()}&error="
            f"{quote(f'{account_id} 不满足接手条件（余量不足/时段冲突/该座位已绑）')}",
            status_code=303)

    old_acc = await store.get_account(t.account_id)
    rng = f"{t.start_time.strftime('%H:%M')}-{t.end_time.strftime('%H:%M')}"
    wd = weekday_key(t.day)
    new_matrix = _matrix_set_day(dict(new_acc.seat_slots or {}), t.seat_num, wd, [rng])
    try:
        validate_matrix(
            new_matrix, max_seg_hours=lib.max_reserve_hours,
            daily_limit_hours=lib.daily_reserve_hours_limit,
        )
    except ValueError as e:
        return RedirectResponse(
            f"/tasks?day={t.day.isoformat()}&error={quote(str(e))}", status_code=303)
    if old_acc:
        old_matrix = _matrix_set_day(dict(old_acc.seat_slots or {}), t.seat_num, wd, [])
        old_acc.seat_slots = old_matrix
        old_acc.bound_seats = sorted(set(old_matrix.keys()))
        await store.upsert_account(old_acc)
    new_acc.seat_slots = new_matrix
    new_acc.bound_seats = sorted(set(new_matrix.keys()))
    await store.upsert_account(new_acc)
    await store.update_task_account(task_id, account_id)
    await store.update_task_status(task_id, TaskStatus.READY, last_error="")
    fresh = await store.get_task(task_id)
    await sched._run_submit(new_acc, fresh)
    after = await store.get_task(task_id)
    if after and after.reserve_id:
        msg = f"#{task_id} 已改绑 {account_id} 并预约成功（预约号 {after.reserve_id}）"
    else:
        msg = (f"#{task_id} 已改绑 {account_id}，但提交仍失败："
               f"{(after.last_error if after else '') or '见任务板'}")
    await store.log_action(
        account_id, "reassign", str(task_id),
        f"改绑重试: seat={t.seat_num} {t.day} {rng} → {account_id}", True, "",
    )
    return RedirectResponse(
        f"/tasks?day={t.day.isoformat()}&msg={quote(msg)}", status_code=303)


@router.post("/tasks/{task_id}/sign")
async def task_sign(request: Request, task_id: int):
    sched = request.app.state.sched
    store = request.app.state.store
    t = await store.get_task(task_id)
    if not t or not t.reserve_id:
        raise HTTPException(400, "no active reservation")
    acc = await store.get_account(t.account_id)
    if not acc:
        raise HTTPException(404)
    client = await sched.client_ready(acc)
    if not client.cookies() and not await sched.login_and_persist(acc, client, "手动签到"):
        raise HTTPException(400, "登录失败，无法签到")
    r = await client.sign(t.reserve_id)
    await store.log_action(acc.id, "sign", str(t.reserve_id), str(r)[:500], bool(r.get("success")))
    return RedirectResponse("/tasks?signed=1", status_code=303)


@router.post("/tasks/{task_id}/cancel")
async def task_cancel(request: Request, task_id: int):
    sched = request.app.state.sched
    store = request.app.state.store
    t = await store.get_task(task_id)
    if not t or not t.reserve_id:
        raise HTTPException(400)
    acc = await store.get_account(t.account_id)
    if not acc:
        raise HTTPException(404)
    client = await sched.client_ready(acc)
    if not client.cookies() and not await sched.login_and_persist(acc, client, "取消登录"):
        raise HTTPException(400, "登录失败，无法取消")
    r = await client.cancel(t.reserve_id)
    await store.log_action(acc.id, "cancel", str(t.reserve_id), str(r)[:500], bool(r.get("success")), str(r.get("msg")))
    if r.get("success"):
        await store.update_task_status(task_id, TaskStatus.COMPLETE)
    else:
        await store.update_task_status(task_id, t.status, last_error=f"取消失败: {r.get('msg')}")
    return RedirectResponse("/tasks?cancelled=1", status_code=303)


@router.post("/tasks/{task_id}/leave")
async def task_leave(request: Request, task_id: int):
    """手动签退当前时段（内部走 signback 真签退通道，非暂离）。"""
    sched = request.app.state.sched
    store = request.app.state.store
    t = await store.get_task(task_id)
    if not t or not t.reserve_id:
        raise HTTPException(400, "no active reservation")
    acc = await store.get_account(t.account_id)
    if not acc:
        raise HTTPException(404)
    client = await sched.client_ready(acc)
    if not client.cookies() and not await sched.login_and_persist(acc, client, "手动签退"):
        raise HTTPException(400, "登录失败，无法签退")
    r = await client.signback(t.reserve_id)
    await store.log_action(acc.id, "signback", str(t.reserve_id), str(r)[:500], bool(r.get("success")))
    if r.get("success"):
        await store.update_task_status(task_id, TaskStatus.COMPLETE)
    else:
        await store.update_task_status(task_id, t.status, last_error=f"签退失败: {r.get('msg')}")
    return RedirectResponse("/?left=1", status_code=303)


# =========================================================================
# Reservations — 预约记录（对齐官方 App 预约记录页）
# =========================================================================

#: (ts, items, error) 按账号缓存 60s, 避免高频打开页面反复打超星只读接口
_RESERVE_CACHE: dict[str, tuple[float, list[dict] | None, str | None]] = {}
_RESERVE_TTL = 60.0
_RESERVE_TABS = [
    ("all", "全部", None),
    ("pending", "待履约", (0, 1)),
    ("done", "已履约", (2,)),
    ("cancelled", "已取消", (7,)),
    ("violation", "违约", (8,)),
]
_RESERVE_STATUS = {
    0: ("待履约", "chip-accent"),
    1: ("使用中", "chip-accent"),
    2: ("已履约", "chip-success"),
    7: ("已取消", "chip-muted"),
    8: ("违约", "chip-danger"),
}


def _fmt_ms(ms) -> str:
    """毫秒时间戳 → HH:MM；空值显示占位符。"""
    from datetime import datetime as _dtm
    if not ms:
        return "—"
    return _dtm.fromtimestamp(ms / 1000).strftime("%H:%M")


@router.get("/reservations", response_class=HTMLResponse)
async def reservations_page(
    request: Request,
    account_id: str | None = None,
    tab: str = "all",
):
    """预约记录页：切换任意守护账号，按官方五页签查看其预约情况。

    每次刷新/切换账号对超星发 **1 个只读 GET**（reservelist，60s TTL 缓存）；
    接口只返回登录人本人的记录, 故查谁就用谁的会话（懒登录）。
    """
    store = request.app.state.store
    sched = request.app.state.sched
    accounts = await store.list_accounts()
    ctx = await _ctx(request, active_page="reservations")
    if not accounts:
        return _templates(request).TemplateResponse(
            request, "reservations_list.html", ctx)
    acc = next((a for a in accounts if a.id == account_id), accounts[0])

    now = _dt.now().timestamp()
    cached = _RESERVE_CACHE.get(acc.id)
    if cached and now - cached[0] < _RESERVE_TTL:
        items, err = cached[1], cached[2]
    else:
        items, err = None, None
        try:
            client = await sched.client_ready(acc)
            if not client.cookies() and not await sched.login_and_persist(acc, client, "预约记录登录"):
                raise ChaoxingError("登录失败")
            items = await client.reserve_list()
        except Exception as e:
            err = str(e)
        _RESERVE_CACHE[acc.id] = (now, items, err)

    rows = []
    for it in (items or []):
        code = it.get("status")
        label, chip = _RESERVE_STATUS.get(code, (str(code), "chip-muted"))
        rows.append({
            "day": it.get("today") or "",
            "start": _fmt_ms(it.get("startTime")),
            "end": _fmt_ms(it.get("endTime")),
            "seat": it.get("seatNum"),
            "room": it.get("thirdLevelName") or "",
            "rid": it.get("id"),
            "code": code,
            "label": label,
            "chip": chip,
            "signin": _fmt_ms(it.get("signInTime")),
            "signout": _fmt_ms(it.get("signBackTime")),
            "start_ms": it.get("startTime") or 0,
        })
    rows.sort(key=lambda r: r["start_ms"], reverse=True)
    counts = {t[0]: 0 for t in _RESERVE_TABS}
    for r in rows:
        for t_id, _label, codes in _RESERVE_TABS:
            if codes is None or r["code"] in codes:
                counts[t_id] += 1
    tab_def = next((t for t in _RESERVE_TABS if t[0] == tab), _RESERVE_TABS[0])
    if tab_def[2] is not None:
        rows = [r for r in rows if r["code"] in tab_def[2]]

    ctx.update(
        accounts=accounts, account=acc,
        tabs=_RESERVE_TABS, tab_id=tab_def[0], tab_label=tab_def[1],
        counts=counts, rows=rows, error=err,
    )
    return _templates(request).TemplateResponse(
        request, "reservations_list.html", ctx)


# =========================================================================
# Status
# =========================================================================
@router.get("/api/status")
async def api_status(request: Request):
    sched = request.app.state.sched
    store = request.app.state.store
    now = now_cst()
    nxt: NextRelay | None = await sched.peek_next_relay()
    last_logs = await store.list_logs(limit=1)
    last = last_logs[0] if last_logs else None
    seats = await store.list_target_seats()
    return JSONResponse({
        "now": now.isoformat(timespec="seconds"),
        "now_ms": int(_dt.now().timestamp() * 1000),
        "next_relay_at": (nxt.at.isoformat(timespec="minutes") if nxt else None),
        "next_relay_in_min": (nxt.delta_minutes if nxt else None),
        "next_relay_account_id": (nxt.account_id if nxt else None),
        "next_relay_task_id": (nxt.task_id if nxt else None),
        "next_relay_seat_num": (nxt.seat_num if nxt else None),
        "next_relay_status": (nxt.status if nxt else None),
        "target_seat_count": len(seats),
        "last_log": ({
            "ts": last.ts, "level": last.level,
            "account_id": last.account_id, "message": last.message,
        } if last else None),
    })


# =========================================================================
# Logs
# =========================================================================
@router.get("/logs", response_class=HTMLResponse)
async def logs_view(
    request: Request,
    account_id: str | None = None,
    level: str | None = None,
):
    store = request.app.state.store
    rows = await store.list_logs(account_id=account_id, level=level, limit=300)
    return _templates(request).TemplateResponse(
        request, "logs.html",
        await _ctx(request, logs=rows,
                    filter_account=account_id, filter_level=level,
                    active_page="logs"),
    )


# =========================================================================
# Settings — 系统配置（保存后立即生效）
# =========================================================================
async def _effective_and_raw(request: Request) -> tuple[dict, dict[str, str]]:
    store = request.app.state.store
    cfg = request.app.state.cfg
    rows = await store.get_settings_map()
    eff = _settings.effective(rows, cfg)
    return eff, rows


@router.get("/settings", response_class=HTMLResponse)
async def settings_view(request: Request):
    eff, rows = await _effective_and_raw(request)
    # 是否已在页面自定义（用于“已自定义”角标）
    overridden = set(rows.keys())
    return _templates(request).TemplateResponse(
        request, "settings.html",
        await _ctx(request,
            eff=eff,
            rows=rows,
            overridden=overridden,
            compare=_settings.SUBMIT_CHANNEL_COMPARE,
            strategy_help=_settings.SUBMIT_STRATEGY_HELP,
            strategy_labels=_settings.SUBMIT_STRATEGY_LABELS,
            relay_options=_settings.RELAY_LEAD_OPTIONS,
            tick_options=_settings.TICK_INTERVAL_OPTIONS,
            defaults=_settings.DEFAULTS,
            saved=request.query_params.get("saved"),
            reset_done=request.query_params.get("reset"),
            error=request.query_params.get("error"),
            active_page="settings"),
    )


@router.post("/settings")
async def settings_save(request: Request):
    store = request.app.state.store
    sched = getattr(request.app.state, "sched", None)
    form = await request.form()
    # 收集可写键
    patch_raw: dict[str, object] = {}
    patch_raw["submit_strategy"] = (form.get("submit_strategy") or "").strip()
    patch_raw["relay_lead_seconds"] = (form.get("relay_lead_seconds") or "").strip()
    patch_raw["stagger_seconds"] = (form.get("stagger_seconds") or "").strip()
    patch_raw["tick_interval_seconds"] = (form.get("tick_interval_seconds") or "").strip()
    # checkbox: 未勾选时 form 无该键
    patch_raw["anchor_retry_enabled"] = "true" if form.get("anchor_retry_enabled") else "false"
    patch_raw["anchor_scan_limit"] = (form.get("anchor_scan_limit") or "").strip()
    patch_raw["max_reserve_hours"] = (form.get("max_reserve_hours") or "").strip()
    patch_raw["daily_reserve_hours_limit"] = (form.get("daily_reserve_hours_limit") or "").strip()
    patch_raw["notify_webhook"] = (form.get("notify_webhook") or "").strip()

    # 滤掉空字符串的“未填”键（notify_webhook 允许空以清空）
    patch: dict[str, object] = {}
    for k, v in patch_raw.items():
        if k == "notify_webhook":
            patch[k] = v
        elif isinstance(v, str) and v == "":
            continue
        else:
            patch[k] = v

    errors = _settings.validate_all(patch)
    # 交叉：单段不应超过日限额（用有效值二次校验）
    if not errors:
        try:
            eff, rows = await _effective_and_raw(request)
            mh_raw = patch.get("max_reserve_hours", eff.get("max_reserve_hours"))
            dh_raw = patch.get("daily_reserve_hours_limit", eff.get("daily_reserve_hours_limit"))
            mh = float(str(mh_raw).strip()) if isinstance(mh_raw, str) else float(mh_raw)  # type: ignore[arg-type]
            dh = float(str(dh_raw).strip()) if isinstance(dh_raw, str) else float(dh_raw)  # type: ignore[arg-type]
            if mh > dh:
                errors["max_reserve_hours"] = "单段上限不应超过每日限额"
        except Exception:
            pass

    if errors:
        # 回显错误：用 303 带参重定向，错误摘要进 query（前端同时展示字段级错误需回填，此处先用 banner）
        from urllib.parse import quote
        first = next(iter(errors.values()))
        return RedirectResponse(f"/settings?error={quote(first)}", status_code=303)

    # 校验通过后保存
    to_store: dict[str, str] = {}
    for k, v in patch.items():
        if k == "submit_strategy":
            v = _settings.normalize_submit_strategy(v)
        elif k == "stagger_seconds":
            v = _settings.normalize_stagger(v)  # type: ignore[arg-type]
            v = _settings.coerce_for_storage(k, v)
            to_store[k] = v
            continue
        elif k == "anchor_retry_enabled":
            # 已是 "true"/"false"
            pass
        to_store[k] = _settings.coerce_for_storage(k, v)

    await store.set_settings(to_store)
    if sched is not None:
        try:
            await sched.load_runtime_settings()
        except Exception:
            pass
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/reset")
async def settings_reset(request: Request):
    store = request.app.state.store
    sched = getattr(request.app.state, "sched", None)
    await store.clear_settings()
    if sched is not None:
        try:
            await sched.load_runtime_settings()
        except Exception:
            pass
    return RedirectResponse("/settings?reset=1", status_code=303)

