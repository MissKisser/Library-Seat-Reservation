"""FastAPI routes for the SeatBot web panel (v2: multi-seat + per-task seat_num).

Dashboard 覆盖图以"目标座位"为行(取代 v1 的"账号"为行)。
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime as _dt
from datetime import time as _time

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from seatbot.client import ChaoxingClient, ChaoxingError
from seatbot.coverage import Coverage, compute_coverage, compute_seat_coverage
from seatbot.models import Account, SeatTarget, Task, TaskStatus
from seatbot.scheduler import NextRelay
from seatbot.utils.timeutil import now_cst, parse_hhmm, today_cst


router = APIRouter()


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

    acc = next((a for a in accounts if a.phone and a.password), None)
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
    out: list[tuple[str, time, time]] = []
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


# =========================================================================
# Dashboard — coverage Gantt, 行 = 目标座位
# =========================================================================
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    cfg = request.app.state.cfg
    store = request.app.state.store
    today = today_cst()
    accounts = await store.list_accounts()
    target_seats = await store.list_target_seats()
    # ★ 把 user_reserved 索引成 (seat_num, start, end) 三元组
    ur_rows = await store.list_user_reserved(day=today)
    user_reserved: list[tuple[str, _time, _time]] = []
    for u in ur_rows:
        try:
            sh, sm = map(int, u["start_time"].split(":"))
            eh, em = map(int, u["end_time"].split(":"))
            user_reserved.append((u["seat_num"], _time(sh, sm), _time(eh, em)))
        except Exception:
            pass

    # ★ 调用 /getusedtimes (mobile fidEnc) 拿到他人占用的时段
    others_occupied, occ_err = await _fetch_others_occupied(
        request, store, today, [s.seat_num for s in target_seats],
    )

    rows = compute_seat_coverage(
        accounts, target_seats, today,
        open_time=cfg.library.open_time,
        close_time=cfg.library.close_time,
        user_reserved=user_reserved,
        others_occupied=others_occupied,
    )

    # 标注每个 (seat, cell) 的 task 实际状态
    annotated_rows = []
    for sc in rows:
        cells = []
        for c in sc.coverage.cells:
            accs_info: list[dict] = []
            for aid in c.accounts:
                tasks = await store.list_tasks(account_id=aid, day=today)
                match_status = "pending"
                match_task_id = None
                match_day = None
                match_start = None
                match_end = None
                for t in tasks:
                    if (t.seat_num == sc.seat.seat_num
                            and t.start_time == c.start
                            and t.end_time == c.end
                            and t.status in (
                                TaskStatus.ACTIVE,
                                TaskStatus.SUBMITTING,
                                TaskStatus.LEAVING,
                                TaskStatus.FAILED,
                                TaskStatus.COMPLETE,
                            )):
                        match_status = t.status.value
                        match_task_id = t.id
                        match_day = t.day.isoformat()
                        match_start = t.start_time
                        match_end = t.end_time
                        break
                info: dict = {"id": aid, "status": match_status}
                if match_status in ("active", "submitting", "failed", "leaving", "complete") and match_task_id:
                    info["task_id"] = match_task_id
                    info["day"] = match_day
                    info["start_time"] = match_start.isoformat(timespec="minutes") if match_start else ""
                    info["end_time"] = match_end.isoformat(timespec="minutes") if match_end else ""
                accs_info.append(info)
            cells.append({
                "start": c.start, "end": c.end,
                "accounts_info": accs_info,
                "user_reserved": c.user_reserved,
                "others_occupied": c.others_occupied,
            })
        annotated_rows.append({"seat": sc.seat, "cells": cells})

    recent_logs = await store.list_logs(limit=10)
    # JSON 字符串用于 dashboard.html 内嵌到 Alpine x-data, time 对象需预处理。
    # 字段名与 /api/dashboard-data 保持一致 (seat_num / cells[].start 字符串),
    # 让前端组件拿到首屏就立刻能 applyToDom,无需特殊处理。
    rows_json = json.dumps(
        [{"seat_num": r["seat"].seat_num,
          "label": r["seat"].label,
          "cells": [{"start": c["start"].strftime("%H:%M"),
                     "end": c["end"].strftime("%H:%M"),
                     "accounts_info": c["accounts_info"],
                     "user_reserved": c["user_reserved"],
                     "others_occupied": c["others_occupied"]} for c in r["cells"]]}
         for r in annotated_rows],
        ensure_ascii=False,
    )
    return _templates(request).TemplateResponse(
        request, "dashboard.html",
        {
            "request": request,
            "cfg": cfg,
            "rows": annotated_rows,
            "rows_json": rows_json,
            "today": today.isoformat(),
            "now_hhmm": now_cst().strftime("%H:%M"),
            "recent_logs": recent_logs,
            "occ_err": occ_err,
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

    与 dashboard() 视图共用 helper, 避免双份逻辑漂移。
    耗时点 (others_occupied) 30s 才触发一次,可接受。
    """
    cfg = request.app.state.cfg
    store = request.app.state.store
    today = today_cst()
    accounts = await store.list_accounts()
    target_seats = await store.list_target_seats()

    ur_rows = await store.list_user_reserved(day=today)
    user_reserved: list[tuple[str, _time, _time]] = []
    for u in ur_rows:
        try:
            sh, sm = map(int, u["start_time"].split(":"))
            eh, em = map(int, u["end_time"].split(":"))
            user_reserved.append((u["seat_num"], _time(sh, sm), _time(eh, em)))
        except Exception:
            pass
    others_occupied, occ_err = await _fetch_others_occupied(
        request, store, today, [s.seat_num for s in target_seats],
    )

    rows = compute_seat_coverage(
        accounts, target_seats, today,
        open_time=cfg.library.open_time,
        close_time=cfg.library.close_time,
        user_reserved=user_reserved,
        others_occupied=others_occupied,
    )

    annotated_rows = []
    for sc in rows:
        cells = []
        for c in sc.coverage.cells:
            accs_info: list[dict] = []
            for aid in c.accounts:
                tasks = await store.list_tasks(account_id=aid, day=today)
                match_status = "pending"
                match_task_id = None
                match_day = None
                match_start = None
                match_end = None
                for t in tasks:
                    if (t.seat_num == sc.seat.seat_num
                            and t.start_time == c.start
                            and t.end_time == c.end
                            and t.status in (
                                TaskStatus.ACTIVE,
                                TaskStatus.SUBMITTING,
                                TaskStatus.LEAVING,
                                TaskStatus.FAILED,
                                TaskStatus.COMPLETE,
                            )):
                        match_status = t.status.value
                        match_task_id = t.id
                        match_day = t.day.isoformat()
                        match_start = t.start_time
                        match_end = t.end_time
                        break
                info: dict = {"id": aid, "status": match_status}
                if match_status in ("active", "submitting", "failed", "leaving", "complete") and match_task_id:
                    info["task_id"] = match_task_id
                    info["day"] = match_day
                    info["start_time"] = match_start.isoformat(timespec="minutes") if match_start else ""
                    info["end_time"] = match_end.isoformat(timespec="minutes") if match_end else ""
                accs_info.append(info)
            cells.append({
                "start": c.start.isoformat(timespec="minutes"),
                "end": c.end.isoformat(timespec="minutes"),
                "accounts_info": accs_info,
                "user_reserved": c.user_reserved,
                "others_occupied": c.others_occupied,
            })
        annotated_rows.append({"seat_num": sc.seat.seat_num,
                                "label": sc.seat.label,
                                "cells": cells})

    recent_logs = await store.list_logs(limit=8)
    return {
        "today": today.isoformat(),
        "now_hhmm": now_cst().strftime("%H:%M"),
        "occ_err": occ_err,
        "rows": annotated_rows,
        "recent_logs": [{
            "ts": l.ts, "level": l.level,
            "account_id": l.account_id, "message": l.message,
        } for l in recent_logs],
        "gap_count": sum(
            1 for sc in rows for c in sc.coverage.cells
            if not c.accounts and not c.user_reserved and not c.others_occupied
        ),
        "target_seat_count": len(target_seats),
        "account_count": len(accounts),
    }


@router.get("/api/dashboard-data")
async def api_dashboard_data(request: Request):
    """Dashboard 局部刷新用的 JSON 视图 (每 30s 拉一次)。"""
    return JSONResponse(await _build_dashboard_data(request))


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
        for sn in a.bound_seats:
            used_by.setdefault(sn, []).append(a.id)
    return _templates(request).TemplateResponse(
        request, "targets_list.html",
        await _ctx(request, seats=seats, used_by=used_by, error=None,
                    active_page="targets"),
    )


@router.get("/targets/new", response_class=HTMLResponse)
async def targets_new(request: Request):
    # 表单已内联到列表页（targets_list.html），重定向到列表页
    return RedirectResponse("/targets", status_code=303)


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
# Coverage report — 行=座位 (v2)
# =========================================================================
@router.get("/coverage", response_class=HTMLResponse)
async def coverage_view(request: Request, day: str | None = None):
    cfg = request.app.state.cfg
    store = request.app.state.store
    d = date.fromisoformat(day) if day else today_cst()
    accounts = await store.list_accounts()
    target_seats = await store.list_target_seats()
    ur_rows = await store.list_user_reserved(day=d)
    from datetime import time as _time
    user_reserved: list[tuple[str, time, time]] = []
    for u in ur_rows:
        sh, sm = map(int, u["start_time"].split(":"))
        eh, em = map(int, u["end_time"].split(":"))
        user_reserved.append((u["seat_num"], _time(sh, sm), _time(eh, em)))

    others_occupied, occ_err = await _fetch_others_occupied(
        request, store, d, [s.seat_num for s in target_seats],
    )

    if not target_seats:
        return _templates(request).TemplateResponse(
            request, "coverage.html",
            await _ctx(request, cov=None, rows=[], day=d.isoformat(),
                        user_reserved=user_reserved,
                        others_occupied=others_occupied,
                        occ_err=occ_err,
                        active_page="coverage"),
        )
    rows = compute_seat_coverage(
        accounts, target_seats, d,
        open_time=cfg.library.open_time,
        close_time=cfg.library.close_time,
        user_reserved=user_reserved,
        others_occupied=others_occupied,
    )
    # coverage 模板需要 dict 形态 rows (因为 dashboard.html 的 annotated_rows 是 dict,
    # gantt 宏对 dict / dataclass 都能跑,但 coverage.html 里有 row.gaps 访问,
    # gaps 是 SeatCoverage 的 @property — Jinja 不识别;统一转 dict 避免踩坑)
    rendered_rows = [
        {
            "seat": sc.seat,
            "gaps": [[g[0], g[1]] for g in sc.gaps],
            "cells": [
                {
                    "start": c.start, "end": c.end,
                    "accounts_info": [
                        {"id": aid, "status": "pending"}
                        for aid in c.accounts
                    ],
                    "user_reserved": c.user_reserved,
                    "others_occupied": c.others_occupied,
                }
                for c in sc.coverage.cells
            ],
        }
        for sc in rows
    ]
    return _templates(request).TemplateResponse(
        request, "coverage.html",
        await _ctx(request, rows=rendered_rows, day=d.isoformat(),
                    user_reserved=user_reserved,
                    others_occupied=others_occupied,
                    occ_err=occ_err,
                    active_page="coverage"),
    )


# =========================================================================
# Accounts CRUD (v2: 包含 bound_seats, max_segments_per_day)
# =========================================================================
@router.get("/accounts", response_class=HTMLResponse)
async def accounts_list(request: Request):
    return _templates(request).TemplateResponse(
        request, "accounts_list.html",
        await _ctx(request, active_page="accounts"),
    )


@router.get("/accounts/new", response_class=HTMLResponse)
async def accounts_new(request: Request):
    target_seats = await request.app.state.store.list_target_seats()
    return _templates(request).TemplateResponse(
        request, "accounts_form.html",
        await _ctx(request, account=None, error=None, active_page="accounts"),
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
    one_account_max_concurrent_segments_per_day: int = Form(default=1),
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
    acc = Account(
        id=id, phone=phone, password=password,
        slots=slots_value,
        bound_seats=list(bound_seats),
        one_account_max_concurrent_segments_per_day=one_account_max_concurrent_segments_per_day,
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
        await _ctx(request, account=acc, error=None, active_page="accounts"),
    )


@router.post("/accounts/{acc_id}")
async def accounts_update(
    request: Request, acc_id: str,
    phone: str = Form(...),
    password: str = Form(...),
    slots: str = Form("full"),
    slots_custom: str = Form(""),
    bound_seats: list[str] = Form(default=[]),
    one_account_max_concurrent_segments_per_day: int = Form(default=1),
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
    existing.phone = phone
    existing.password = password
    existing.slots = slots_value
    existing.bound_seats = list(bound_seats)
    existing.one_account_max_concurrent_segments_per_day = one_account_max_concurrent_segments_per_day
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
    account_id: str | None = None,
    day: str | None = None,
    seat_num: str | None = None,
):
    d = date.fromisoformat(day) if day else today_cst()
    store = request.app.state.store
    tasks = await store.list_tasks(
        account_id=account_id, day=d, seat_num=seat_num,
    )
    accounts = await store.list_accounts()
    target_seats = await store.list_target_seats()
    return _templates(request).TemplateResponse(
        request, "tasks_list.html",
        await _ctx(request, tasks=tasks,
                    filter_account=account_id, filter_day=d.isoformat(),
                    filter_seat=seat_num,
                    active_page="tasks"),
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


@router.post("/tasks/{task_id}/leave")
async def task_leave(request: Request, task_id: int):
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
    r = await client.leave(t.reserve_id)
    await store.log_action(acc.id, "leave", str(t.reserve_id), str(r)[:500], bool(r.get("success")))
    await store.update_task_status(task_id, TaskStatus.COMPLETE)
    return RedirectResponse("/tasks?left=1", status_code=303)


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
    await store.log_action(acc.id, "cancel", str(t.reserve_id), str(r)[:500], bool(r.get("success")))
    await store.update_task_status(task_id, TaskStatus.COMPLETE)
    return RedirectResponse("/tasks?cancelled=1", status_code=303)


# =========================================================================
# Seats (整馆可视化)
# =========================================================================
@router.get("/seats", response_class=HTMLResponse)
async def seats_view(request: Request):
    return _templates(request).TemplateResponse(
        request, "seats.html",
        await _ctx(request, active_page="seats"),
    )


# =========================================================================
# Availability API — 检查某 seat × 时段 是否空闲
# =========================================================================
@router.get("/api/seats/availability")
async def api_seat_availability(
    request: Request,
    day: str = Query(...),
    seat: str = Query(...),
    start: str = Query(...),
    end: str = Query(...),
):
    """Return whether `seat` is free at [start, end) on `day`."""
    cfg = request.app.state.cfg
    sched = request.app.state.sched
    store = request.app.state.store
    sn = seat.zfill(3) if seat.isdigit() else seat
    accounts = await store.list_accounts()
    if not accounts:
        return JSONResponse({"available": True, "seat": sn, "note": "no accounts"})
    # 任选一个账号作为探针 (登录)
    acc = accounts[0]
    client = sched._client_for(acc)
    if not client.cookies():
        try:
            await client.login(acc.phone, acc.password)
        except Exception as e:
            return JSONResponse(
                {"available": True, "seat": sn, "error": f"login failed: {e}"},
                status_code=200,
            )
    try:
        reserve = await client.get_active_reservation(cfg.library.room_id, sn)
    except Exception as e:
        return JSONResponse({"available": True, "seat": sn, "error": str(e)})
    if not reserve:
        return JSONResponse({"available": True, "seat": sn})
    return JSONResponse({
        "available": False,
        "seat": sn,
        "occupied_by": str(reserve),
        "end_time": (reserve.get("endTime") if isinstance(reserve, dict) else None),
    })


@router.get("/api/coverage")
async def api_coverage(request: Request, day: str | None = None):
    cfg = request.app.state.cfg
    store = request.app.state.store
    d = date.fromisoformat(day) if day else today_cst()
    accounts = await store.list_accounts()
    seats = await store.list_target_seats()
    ur_rows = await store.list_user_reserved(day=d)
    from datetime import time as _time
    user_reserved = [(u["seat_num"],
                      _time(*map(int, u["start_time"].split(":"))),
                      _time(*map(int, u["end_time"].split(":"))))
                     for u in ur_rows]

    others_occupied, occ_err = await _fetch_others_occupied(
        request, store, d, [s.seat_num for s in seats],
    )

    rows = compute_seat_coverage(
        accounts, seats, d,
        open_time=cfg.library.open_time, close_time=cfg.library.close_time,
        user_reserved=user_reserved,
        others_occupied=others_occupied,
    )
    return JSONResponse({
        "day": d.isoformat(),
        "open_time": rows[0].coverage.open_time.isoformat(timespec="minutes") if rows else "08:00",
        "close_time": rows[0].coverage.close_time.isoformat(timespec="minutes") if rows else "22:00",
        "user_reserved": [
            {
                "seat_num": sn,
                "start": s.isoformat(timespec="minutes"),
                "end": e.isoformat(timespec="minutes"),
            }
            for sn, s, e in user_reserved
        ],
        "others_occupied": [
            {
                "seat_num": sn,
                "start": s.isoformat(timespec="minutes"),
                "end": e.isoformat(timespec="minutes"),
            }
            for sn, s, e in others_occupied
        ],
        "occ_err": occ_err,
        "rows": [
            {
                "seat_num": sc.seat.seat_num,
                "label": sc.seat.label,
                "cells": [
                    {
                        "start": c.start.isoformat(timespec="minutes"),
                        "end": c.end.isoformat(timespec="minutes"),
                        "accounts": c.accounts,
                        "user_reserved": bool(getattr(c, "user_reserved", False)),
                        "others_occupied": bool(getattr(c, "others_occupied", False)),
                    }
                    for c in sc.coverage.cells
                ],
                "gaps": [[g[0].isoformat(timespec="minutes"), g[1].isoformat(timespec="minutes")]
                         for g in sc.gaps],
            }
            for sc in rows
        ],
        "by_account": _by_account_view(accounts, d,
                                         open_time=cfg.library.open_time,
                                         close_time=cfg.library.close_time),
    })


def _by_account_view(accounts: list[Account], d: date,
                     open_time: str, close_time: str) -> list[dict]:
    cov = compute_coverage(accounts, d, open_time=open_time, close_time=close_time)
    return [
        {
            "start": c.start.isoformat(timespec="minutes"),
            "end": c.end.isoformat(timespec="minutes"),
            "accounts": c.accounts,
        }
        for c in cov.cells
    ]


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


@router.get("/api/seats/{room_id}")
async def api_seats(room_id: int, request: Request):
    sched = request.app.state.sched
    store = request.app.state.store
    cfg = request.app.state.cfg
    accounts = await store.list_accounts()
    seats = await store.list_target_seats()
    target_seats_nums = [s.seat_num for s in seats]
    acc = accounts[0] if accounts else None
    if not acc:
        return JSONResponse({"seats": [], "targets": target_seats_nums, "error": "no accounts"})

    client = sched._client_for(acc)
    if not client.cookies():
        last_err: str | None = None
        for attempt in range(3):
            try:
                await client.login(acc.phone, acc.password)
                last_err = None
                break
            except Exception as e:
                last_err = str(e)
                await asyncio.sleep(5 + attempt * 5)
        if last_err is not None:
            return JSONResponse({
                "seats": [], "targets": target_seats_nums,
                "error": f"login failed after 3 attempts: {last_err}",
            })

    # Probe around each target seat
    out: dict[str, list[dict]] = {}
    for target in target_seats_nums:
        tn = int(target)
        per_seat: list[dict] = []
        for d in range(-2, 3):
            n = tn + d
            seat_num = f"{n:03d}"
            try:
                res = await client.get_active_reservation(room_id, seat_num)
            except Exception as e:
                per_seat.append({"seat_num": seat_num, "occupied": None, "error": str(e)})
                continue
            if res:
                per_seat.append({
                    "seat_num": seat_num, "occupied": True,
                    "occupier_uid": res.get("uid"),
                    "end_ts": res.get("endTime"),
                    "is_target": (n == tn),
                })
            else:
                per_seat.append({
                    "seat_num": seat_num, "occupied": False,
                    "is_target": (n == tn),
                })
        out[target] = per_seat
    return JSONResponse({"targets": target_seats_nums, "room_id": room_id, "by_target": out})


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
