"""FastAPI routes for the SeatBot web panel.

The panel is organized around the **single-seat protection** model: one
target_seat_num + N guard accounts + per-account slot ranges. Coverage and
availability checks are first-class pages so the user can hand-pick slots
that are actually free at the target seat.
"""
from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from seatbot.client import ChaoxingClient, ChaoxingError
from seatbot.coverage import Coverage, compute_coverage
from seatbot.models import Account, Task, TaskStatus
from seatbot.utils.timeutil import now_cst, parse_hhmm, today_cst


router = APIRouter()


def _templates(request: Request):
    return request.app.state.templates


# =========================================================================
# Dashboard — coverage Gantt (rows = accounts, cols = 30-min cells)
# =========================================================================
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    cfg = request.app.state.cfg
    store = request.app.state.store
    accounts = await store.list_accounts()
    today = today_cst()
    cov: Coverage = compute_coverage(
        accounts, today,
        open_time=cfg.library.open_time,
        close_time=cfg.library.close_time,
    )
    # annotate cells with the latest active task status for that account
    annotated = []
    for c in cov.cells:
        accs_info: list[dict] = []
        for aid in c.accounts:
            tasks = await store.list_tasks(account_id=aid, day=today)
            status = "pending"
            for t in tasks:
                if t.start_time == c.start and t.end_time == c.end and t.status in (
                    TaskStatus.ACTIVE, TaskStatus.SUBMITTING, TaskStatus.LEAVING,
                    TaskStatus.FAILED,
                ):
                    status = t.status.value
                    break
            accs_info.append({"id": aid, "status": status})
        annotated.append({"start": c.start, "end": c.end, "accounts": accs_info})
    return _templates(request).TemplateResponse(
        request, "dashboard.html",
        {
            "request": request,
            "cfg": cfg,
            "cells": annotated,
            "gaps": [list(g) for g in cov.gaps],
            "overlaps": [list(o) for o in cov.overlaps],
            "today": today.isoformat(),
            "accounts": accounts,
            "now_hhmm": now_cst().strftime("%H:%M"),
        },
    )


# =========================================================================
# Seat config (target_seat_num lives in library)
# =========================================================================
@router.get("/seat-config", response_class=HTMLResponse)
async def seat_config_view(request: Request):
    cfg = request.app.state.cfg
    return _templates(request).TemplateResponse(
        request, "seat_config.html",
        {"request": request, "cfg": cfg, "error": None},
    )


@router.post("/seat-config")
async def seat_config_save(
    request: Request,
    target_seat_num: str = Form(...),
):
    cfg = request.app.state.cfg
    try:
        v = target_seat_num.strip()
        if not v.isdigit() or not (1 <= len(v) <= 4):
            raise ValueError("target_seat_num must be 1-4 digit number")
        cfg.library.target_seat_num = v.zfill(3)
    except Exception as e:
        return _templates(request).TemplateResponse(
            request, "seat_config.html",
            {"request": request, "cfg": cfg, "error": str(e)},
            status_code=400,
        )
    return RedirectResponse("/seat-config", status_code=303)


# =========================================================================
# Coverage report (which hours are protected, where are the gaps)
# =========================================================================
@router.get("/coverage", response_class=HTMLResponse)
async def coverage_view(request: Request, day: str | None = None):
    cfg = request.app.state.cfg
    store = request.app.state.store
    d = date.fromisoformat(day) if day else today_cst()
    accounts = await store.list_accounts()
    cov = compute_coverage(
        accounts, d,
        open_time=cfg.library.open_time,
        close_time=cfg.library.close_time,
    )
    return _templates(request).TemplateResponse(
        request, "coverage.html",
        {
            "request": request,
            "cfg": cfg,
            "cov": cov,
            "day": d.isoformat(),
            "accounts": accounts,
        },
    )


# =========================================================================
# Accounts CRUD (no seat_num; slots-only)
# =========================================================================
@router.get("/accounts", response_class=HTMLResponse)
async def accounts_list(request: Request):
    store = request.app.state.store
    accs = await store.list_accounts()
    return _templates(request).TemplateResponse(
        request, "accounts_list.html", {"request": request, "accounts": accs}
    )


@router.get("/accounts/new", response_class=HTMLResponse)
async def accounts_new(request: Request):
    return _templates(request).TemplateResponse(
        request, "accounts_form.html",
        {"request": request, "account": None, "error": None},
    )


@router.post("/accounts")
async def accounts_create(
    request: Request,
    id: str = Form(...),
    phone: str = Form(...),
    password: str = Form(...),
    slots: str = Form("full"),
    slots_custom: str = Form(""),
):
    store = request.app.state.store
    slots_value: str | list[str] = slots
    if slots == "custom":
        try:
            slots_value = json.loads(slots_custom) if slots_custom.strip() else []
        except json.JSONDecodeError as e:
            return _templates(request).TemplateResponse(
                request, "accounts_form.html",
                {"request": request, "account": None, "error": f"slots JSON 错误: {e}"},
                status_code=400,
            )
    acc = Account(id=id, phone=phone, password=password, slots=slots_value)
    try:
        await store.upsert_account(acc)
    except Exception as e:
        return _templates(request).TemplateResponse(
            request, "accounts_form.html",
            {"request": request, "account": acc, "error": str(e)},
            status_code=400,
        )
    return RedirectResponse("/accounts", status_code=303)


@router.get("/accounts/{acc_id}/edit", response_class=HTMLResponse)
async def accounts_edit(request: Request, acc_id: str):
    store = request.app.state.store
    acc = await store.get_account(acc_id)
    if not acc:
        raise HTTPException(404)
    return _templates(request).TemplateResponse(
        request, "accounts_form.html",
        {"request": request, "account": acc, "error": None},
    )


@router.post("/accounts/{acc_id}")
async def accounts_update(
    request: Request, acc_id: str,
    phone: str = Form(...),
    password: str = Form(...),
    slots: str = Form("full"),
    slots_custom: str = Form(""),
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
                {"request": request, "account": existing, "error": f"slots JSON 错误: {e}"},
                status_code=400,
            )
    existing.phone = phone
    existing.password = password
    existing.slots = slots_value
    await store.upsert_account(existing)
    return RedirectResponse("/accounts", status_code=303)


@router.post("/accounts/{acc_id}/delete")
async def accounts_delete(request: Request, acc_id: str):
    store = request.app.state.store
    await store.delete_account(acc_id)
    return RedirectResponse("/accounts", status_code=303)


@router.post("/accounts/{acc_id}/test-login")
async def accounts_test_login(request: Request, acc_id: str):
    """Try to log in with this account's credentials; return JSON result."""
    store = request.app.state.store
    cfg = request.app.state.cfg
    acc_cfg = next((a for a in cfg.accounts if a.id == acc_id), None)
    db_acc = acc_cfg or await store.get_account(acc_id)
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
        # keep scheduler-managed clients untouched


# =========================================================================
# Tasks CRUD
# =========================================================================
@router.get("/tasks", response_class=HTMLResponse)
async def tasks_list(request: Request, account_id: str | None = None, day: str | None = None):
    store = request.app.state.store
    d = date.fromisoformat(day) if day else today_cst()
    tasks = await store.list_tasks(account_id=account_id, day=d)
    accounts = await store.list_accounts()
    return _templates(request).TemplateResponse(
        request, "tasks_list.html",
        {"request": request, "tasks": tasks, "accounts": accounts,
         "filter_account": account_id, "filter_day": d.isoformat()},
    )


@router.post("/tasks/quick-reserve")
async def quick_reserve(
    request: Request,
    account_id: str = Form(...),
    start: str = Form(...),
    end: str = Form(...),
):
    sched = request.app.state.sched
    store = request.app.state.store
    acc = next((a for a in request.app.state.cfg.accounts if a.id == account_id), None)
    if not acc:
        raise HTTPException(404, f"account {account_id} not found")
    try:
        s = parse_hhmm(start)
        e = parse_hhmm(end)
        if e <= s:
            raise ValueError("end must be after start")
    except (ValueError, AttributeError) as exc:
        raise HTTPException(400, f"时间格式错误: {exc}")
    t = Task(
        id=None, account_id=acc.id, day=today_cst(),
        start_time=s, end_time=e,
        status=TaskStatus.READY,
    )
    tid = await store.add_task(t)
    await sched._run_submit_sign(acc, await store.get_task(tid))
    return RedirectResponse("/tasks", status_code=303)


@router.post("/tasks/{task_id}/sign")
async def task_sign(request: Request, task_id: int):
    sched = request.app.state.sched
    store = request.app.state.store
    t = await store.get_task(task_id)
    if not t or not t.reserve_id:
        raise HTTPException(400, "no active reservation")
    acc = next((a for a in request.app.state.cfg.accounts if a.id == t.account_id), None)
    if not acc:
        raise HTTPException(404)
    client = sched._client_for(acc)
    if not client.cookies():
        await client.login(acc.phone, acc.password)
    r = await client.sign(t.reserve_id)
    await store.log_action(acc.id, "sign", str(t.reserve_id), str(r)[:500], bool(r.get("success")))
    return RedirectResponse("/tasks", status_code=303)


@router.post("/tasks/{task_id}/leave")
async def task_leave(request: Request, task_id: int):
    sched = request.app.state.sched
    store = request.app.state.store
    t = await store.get_task(task_id)
    if not t or not t.reserve_id:
        raise HTTPException(400)
    acc = next((a for a in request.app.state.cfg.accounts if a.id == t.account_id), None)
    if not acc:
        raise HTTPException(404)
    client = sched._client_for(acc)
    if not client.cookies():
        await client.login(acc.phone, acc.password)
    r = await client.leave(t.reserve_id)
    await store.log_action(acc.id, "leave", str(t.reserve_id), str(r)[:500], bool(r.get("success")))
    await store.update_task_status(task_id, TaskStatus.COMPLETE)
    return RedirectResponse("/tasks", status_code=303)


@router.post("/tasks/{task_id}/cancel")
async def task_cancel(request: Request, task_id: int):
    sched = request.app.state.sched
    store = request.app.state.store
    t = await store.get_task(task_id)
    if not t or not t.reserve_id:
        raise HTTPException(400)
    acc = next((a for a in request.app.state.cfg.accounts if a.id == t.account_id), None)
    if not acc:
        raise HTTPException(404)
    client = sched._client_for(acc)
    if not client.cookies():
        await client.login(acc.phone, acc.password)
    r = await client.cancel(t.reserve_id)
    await store.log_action(acc.id, "cancel", str(t.reserve_id), str(r)[:500], bool(r.get("success")))
    await store.update_task_status(task_id, TaskStatus.COMPLETE)
    return RedirectResponse("/tasks", status_code=303)


# =========================================================================
# Seats (whole-room visualization)
# =========================================================================
@router.get("/seats", response_class=HTMLResponse)
async def seats_view(request: Request):
    cfg = request.app.state.cfg
    return _templates(request).TemplateResponse(
        request, "seats.html", {"request": request, "cfg": cfg}
    )


# =========================================================================
# Availability API — declared BEFORE /api/seats/{room_id} so it isn't
# shadowed by the path-param route. Used by the slot editor to grey out
# occupied cells.
# =========================================================================
@router.get("/api/seats/availability")
async def api_seat_availability(
    request: Request,
    day: str = Query(...),
    start: str = Query(...),
    end: str = Query(...),
):
    """Return whether target_seat_num is free at [start, end) on `day`.

    Response:
        { available: bool, seat: "084", occupied_by: "..." | null }
    """
    cfg = request.app.state.cfg
    sched = request.app.state.sched
    seat = cfg.library.target_seat_num
    if not cfg.accounts:
        return JSONResponse({"available": True, "seat": seat, "note": "no accounts"})
    acc = cfg.accounts[0]
    client = sched._client_for(acc)
    if not client.cookies():
        try:
            await client.login(acc.phone, acc.password)
        except Exception as e:
            return JSONResponse(
                {"available": True, "seat": seat, "error": f"login failed: {e}"},
                status_code=200,
            )
    try:
        reserve = await client.get_active_reservation(
            cfg.library.room_id, seat
        )
    except Exception as e:
        return JSONResponse({"available": True, "seat": seat, "error": str(e)})
    if not reserve:
        return JSONResponse({"available": True, "seat": seat})
    return JSONResponse({
        "available": False,
        "seat": seat,
        "occupied_by": str(reserve),
        "end_time": (reserve.get("endTime") if isinstance(reserve, dict) else None),
    })


@router.get("/api/seats/{room_id}")
async def api_seats(room_id: int, request: Request):
    sched = request.app.state.sched
    cfg = request.app.state.cfg
    acc = cfg.accounts[0] if cfg.accounts else None
    if not acc:
        return JSONResponse({"seats": []})
    client = sched._client_for(acc)
    if not client.cookies():
        try:
            await client.login(acc.phone, acc.password)
        except Exception:
            return JSONResponse({"seats": [], "error": "login failed"})
    seats = await client.get_seat_status(room_id)
    return JSONResponse({"seats": seats})


# =========================================================================
# Coverage JSON (polled by dashboard)
# =========================================================================
@router.get("/api/coverage")
async def api_coverage(request: Request, day: str | None = None):
    cfg = request.app.state.cfg
    store = request.app.state.store
    accounts = await store.list_accounts()
    d = date.fromisoformat(day) if day else today_cst()
    cov = compute_coverage(
        accounts, d,
        open_time=cfg.library.open_time,
        close_time=cfg.library.close_time,
    )
    return JSONResponse({
        "day": d.isoformat(),
        "open": cov.open_time.isoformat(timespec="minutes"),
        "close": cov.close_time.isoformat(timespec="minutes"),
        "target_seat": cfg.library.target_seat_num,
        "cells": [
            {
                "start": c.start.isoformat(timespec="minutes"),
                "end": c.end.isoformat(timespec="minutes"),
                "accounts": c.accounts,
            }
            for c in cov.cells
        ],
        "gaps": [[g[0].isoformat(timespec="minutes"), g[1].isoformat(timespec="minutes")] for g in cov.gaps],
        "overlaps": [
            [o[0].isoformat(timespec="minutes"), o[1].isoformat(timespec="minutes"), o[2]]
            for o in cov.overlaps
        ],
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
    accounts = await store.list_accounts()
    return _templates(request).TemplateResponse(
        request, "logs.html",
        {"request": request, "logs": rows, "accounts": accounts,
         "filter_account": account_id, "filter_level": level},
    )