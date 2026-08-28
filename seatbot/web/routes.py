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

from seatbot.client import ChaoxingClient, ChaoxingError
from seatbot.coverage import compute_seat_coverage
from seatbot.models import Account, Task, TaskStatus
from seatbot.scheduler import NextRelay
from seatbot.utils.timeutil import now_cst, parse_hhmm, parse_range, today_cst


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


def _parse_seat_slots(raw: str, max_hours: float) -> dict[str, list[str]] | None:
    """解析并校验表单提交的 seat_slots JSON。

    合法: {"104": ["09:00-11:00"], ...} — key 为 1-4 位数字, value 为
    字符串数组且单段 ≤ max_hours (与 _validate_custom_slots 同一规则)。
    空 / 非法为空对象时返回 None (回退扁平 slots 模式); 校验失败抛 ValueError。
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
    out: dict[str, list[str]] = {}
    for seat, slots in data.items():
        sn = str(seat).strip()
        if not sn.isdigit() or not (1 <= len(sn) <= 4):
            raise ValueError(f"seat_slots 座位号非法: {seat!r}")
        if not isinstance(slots, list) or not all(isinstance(x, str) for x in slots):
            raise ValueError(f"seat_slots[{sn}] 必须是字符串数组")
        for r in slots:
            try:
                s, e = parse_range(r)
            except ValueError as exc:
                raise ValueError(f"seat_slots[{sn}] 时段格式错误: {exc}") from exc
            dur_h = (_dt.combine(date.today(), e) - _dt.combine(date.today(), s)).total_seconds() / 3600
            if dur_h > max_hours:
                raise ValueError(
                    f"seat_slots[{sn}] 时段 {r} 长 {dur_h}h 超过单段上限 {max_hours}h"
                )
        out[sn.zfill(3)] = slots
    return out


async def _validate_custom_slots(request: Request, slots_value, template, ctx_account):
    """★ v0.6: 校验自定义时段 — 格式合法且单段 ≤ max_reserve_hours (不自动拆段)。

    返回 None 表示通过; 否则返回 400 TemplateResponse。
    与 planner._reject_overlong_ranges 同一业务规则 (AGENTS.md 2026-08-24):
    超长 range 自动拆开会破坏精确守护矩阵, 必须在输入处拒绝。
    """
    if slots_value == "full" or not slots_value:
        return None
    max_hours = request.app.state.cfg.library.max_reserve_hours
    for r in slots_value:
        try:
            s, e = parse_range(r)
        except ValueError as exc:
            return _templates(request).TemplateResponse(
                request, template,
                await _ctx(request, account=ctx_account,
                            error=f"时段格式错误: {exc}", active_page="accounts"),
                status_code=400,
            )
        dur_h = (_dt.combine(date.today(), e) - _dt.combine(date.today(), s)).total_seconds() / 3600
        if dur_h > max_hours:
            return _templates(request).TemplateResponse(
                request, template,
                await _ctx(request, account=ctx_account,
                            error=(f"时段 {r} 长 {dur_h}h, 超过单段上限 {max_hours}h — "
                                   f"请在表单里拆成多段 (自动拆段已禁用, 避免破坏守护矩阵)"),
                            active_page="accounts"),
                status_code=400,
            )
    return None


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

    Auth: 优先复用 scheduler._client_for(acc) 的已登录 cookie 缓存，
    避免每次页面加载都触发一次无头浏览器登录。
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

    if sched is not None:
        client = sched._client_for(acc)
    else:
        client = ChaoxingClient()

    if not client.cookies():
        try:
            await client.login(acc.phone, acc.password)
        except Exception as e:
            return [], f"silent login failed: {type(e).__name__}: {e}"

    day_str = day.isoformat()
    out: list[tuple[str, _time, _time]] = []
    last_err: str | None = None
    # 并行拉所有座位的 usedtimes (互不依赖)
    cfg = request.app.state.cfg
    results = await asyncio.gather(
        *(client.get_used_times(cfg.library.room_id, sn, day_str)
          for sn in seat_nums),
        return_exceptions=True,
    )
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
    others_occupied, occ_err = await _fetch_others_occupied(
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
    gap_count = sum(
        1 for sc in rows for c in sc.coverage.cells
        if not c.accounts and not c.user_reserved and not c.others_occupied
    )
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

    任务是 planner 产出的 1-2h 块，格子是 30min，因此按区间重叠匹配
    (t.start < c.end and t.end > c.start)，而非起止完全相等。
    当日任务只查一次，按 (account_id, seat_num) 索引后内存匹配。
    """
    ACTIVE_STATUSES = (
        TaskStatus.ACTIVE, TaskStatus.SIGNED, TaskStatus.SUBMITTING,
        TaskStatus.LEAVING, TaskStatus.FAILED, TaskStatus.COMPLETE,
    )
    DETAIL_STATUSES = ("active", "signed", "submitting", "failed", "leaving", "complete")
    tasks_by_acc_seat: dict[tuple[str, str], list[Task]] = {}
    for t in await store.list_tasks(day=day):
        if t.status in ACTIVE_STATUSES:
            tasks_by_acc_seat.setdefault((t.account_id, t.seat_num), []).append(t)

    out: list[dict] = []
    for sc in rows:
        cells: list[dict] = []
        for c in sc.coverage.cells:
            accs_info: list[dict] = []
            for aid in c.accounts:
                match_status = "pending"
                match_task: Task | None = None
                for t in tasks_by_acc_seat.get((aid, sc.seat.seat_num), []):
                    if t.start_time < c.end and t.end_time > c.start:
                        match_status = t.status.value
                        match_task = t
                        break
                info: dict = {"id": aid, "status": match_status}
                if match_status in DETAIL_STATUSES and match_task is not None:
                    info["task_id"] = match_task.id
                    info["day"] = match_task.day.isoformat()
                    info["start_time"] = match_task.start_time.isoformat(timespec="minutes")
                    info["end_time"] = match_task.end_time.isoformat(timespec="minutes")
                    info["updated_at"] = match_task.updated_at
                accs_info.append(info)
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
    # 首屏 JSON 与 /api/dashboard-data 同源，前端组件拿到即可数据驱动渲染。
    initial = await _build_dashboard_data(request)
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
        "recent_logs": [{
            "ts": l.ts, "level": l.level,
            "account_id": l.account_id, "message": l.message,
        } for l in recent_logs],
        "target_seat_count": len(target_seats),
        "account_count": len(accounts),
    }


@router.get("/api/dashboard-data")
async def api_dashboard_data(request: Request):
    """Dashboard 局部刷新用的 JSON 视图 (每 30s 拉一次, 含今天+明天两块)。"""
    return JSONResponse(await _build_dashboard_data(request))


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


@router.get("/api/tasks")
async def api_tasks(request: Request, day: str | None = None):
    """任务看板 JSON 视图，仅读本地 DB，不发起超星请求。"""
    store = request.app.state.store
    try:
        d = date.fromisoformat(day) if day else today_cst()
    except ValueError:
        raise HTTPException(400, "day must be YYYY-MM-DD")
    tasks = await store.list_tasks(day=d)
    return JSONResponse({
        "day": d.isoformat(),
        "tasks": [_serialize_task(t) for t in tasks],
    })


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
        await _ctx(request, seats=seats, used_by=used_by, error=None,
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
    id: str = Form(...),
    phone: str = Form(...),
    password: str = Form(...),
    slots: str = Form("full"),
    slots_custom: str = Form(""),
    bound_seats: list[str] = Form(default=[]),
    seat_slots: str = Form("{}"),
):
    store = request.app.state.store
    slots_value: str | list[str] = slots
    if slots == "custom":
        try:
            slots_value = json.loads(slots_custom) if slots_custom.strip() else []
        except json.JSONDecodeError as e:
            return _templates(request).TemplateResponse(
                request, "accounts_form.html",
                await _ctx(request, account=None,
                            error=f"slots JSON 错误: {e}",
                            active_page="accounts"),
                status_code=400,
            )
        err_resp = await _validate_custom_slots(request, slots_value, "accounts_form.html", None)
        if err_resp is not None:
            return err_resp
    try:
        seat_slots_val = _parse_seat_slots(
            seat_slots, request.app.state.cfg.library.max_reserve_hours)
    except ValueError as e:
        return _templates(request).TemplateResponse(
            request, "accounts_form.html",
            await _ctx(request, account=None, error=str(e), active_page="accounts"),
            status_code=400,
        )
    acc = Account(
        id=id, phone=phone, password=password,
        slots=slots_value,
        bound_seats=list(bound_seats),
        seat_slots=seat_slots_val,
    )
    try:
        await store.upsert_account(acc)
    except Exception as e:
        return _templates(request).TemplateResponse(
            request, "accounts_form.html",
            await _ctx(request, account=acc, error=str(e), active_page="accounts"),
            status_code=400,
        )
    return RedirectResponse("/accounts?created=1", status_code=303)


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
    slots: str = Form("full"),
    slots_custom: str = Form(""),
    bound_seats: list[str] = Form(default=[]),
    seat_slots: str = Form("{}"),
):
    store = request.app.state.store
    existing = await store.get_account(acc_id)
    if not existing:
        raise HTTPException(404)
    slots_value: str | list[str] = slots
    if slots == "custom":
        try:
            slots_value = json.loads(slots_custom) if slots_custom.strip() else []
        except json.JSONDecodeError as e:
            return _templates(request).TemplateResponse(
                request, "accounts_form.html",
                await _ctx(request, account=existing,
                            error=f"slots JSON 错误: {e}",
                            active_page="accounts"),
                status_code=400,
            )
        err_resp = await _validate_custom_slots(request, slots_value, "accounts_form.html", existing)
        if err_resp is not None:
            return err_resp
    try:
        seat_slots_val = _parse_seat_slots(
            seat_slots, request.app.state.cfg.library.max_reserve_hours)
    except ValueError as e:
        return _templates(request).TemplateResponse(
            request, "accounts_form.html",
            await _ctx(request, account=existing, error=str(e), active_page="accounts"),
            status_code=400,
        )
    existing.phone = phone
    if password.strip():
        existing.password = password
    existing.slots = slots_value
    existing.bound_seats = list(bound_seats)
    existing.seat_slots = seat_slots_val
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
    tasks = await store.list_tasks(day=d)
    payload = {
        "day": d.isoformat(),
        "tasks": [_serialize_task(t) for t in tasks],
    }
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
    client = sched._client_for(acc)
    if not client.cookies():
        await client.login(acc.phone, acc.password)
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
    client = sched._client_for(acc)
    if not client.cookies():
        await client.login(acc.phone, acc.password)
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
    client = sched._client_for(acc)
    if not client.cookies():
        await client.login(acc.phone, acc.password)
    r = await client.signback(t.reserve_id)
    await store.log_action(acc.id, "signback", str(t.reserve_id), str(r)[:500], bool(r.get("success")), str(r.get("msg")))
    if r.get("success"):
        await store.update_task_status(task_id, TaskStatus.COMPLETE)
    else:
        await store.update_task_status(task_id, t.status, last_error=f"签退失败: {r.get('msg')}")
    return RedirectResponse("/?left=1", status_code=303)


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
