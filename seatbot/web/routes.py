"""FastAPI routes for the SeatBot web panel."""
from __future__ import annotations

import json
from datetime import date, time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from seatbot.models import Account, Task, TaskStatus
from seatbot.utils.timeutil import today_cst


router = APIRouter()


def _templates(request: Request):
    return request.app.state.templates


# ---------- dashboard ----------
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    store = request.app.state.store
    cfg = request.app.state.cfg
    accounts = await store.list_accounts()
    today = today_cst()
    cards = []
    alerts = []
    for acc in accounts:
        tasks = await store.list_tasks(account_id=acc.id, day=today)
        active = next((t for t in tasks if t.status == TaskStatus.ACTIVE), None)
        cards.append({
            "account": acc,
            "active": active,
            "task_count": len(tasks),
        })
        failed = [t for t in tasks if t.status == TaskStatus.FAILED]
        if failed:
            alerts.append({"account": acc, "failed": failed})
    return _templates(request).TemplateResponse(
        "dashboard.html",
        {"request": request, "cards": cards, "alerts": alerts, "cfg": cfg},
    )


# ---------- accounts ----------
@router.get("/accounts", response_class=HTMLResponse)
async def accounts_list(request: Request):
    store = request.app.state.store
    accs = await store.list_accounts()
    return _templates(request).TemplateResponse(
        "accounts_list.html", {"request": request, "accounts": accs}
    )


@router.get("/accounts/new", response_class=HTMLResponse)
async def accounts_new(request: Request):
    return _templates(request).TemplateResponse(
        "accounts_form.html",
        {"request": request, "account": None, "error": None},
    )


@router.post("/accounts")
async def accounts_create(
    request: Request,
    id: str = Form(...),
    phone: str = Form(...),
    password: str = Form(...),
    seat_num: str = Form(...),
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
                "accounts_form.html",
                {"request": request, "account": None, "error": f"slots JSON 错误: {e}"},
                status_code=400,
            )
    acc = Account(id=id, phone=phone, password=password, seat_num=seat_num, slots=slots_value)
    try:
        await store.upsert_account(acc)
    except Exception as e:
        return _templates(request).TemplateResponse(
            "accounts_form.html",
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
        "accounts_form.html",
        {"request": request, "account": acc, "error": None},
    )


@router.post("/accounts/{acc_id}")
async def accounts_update(
    request: Request, acc_id: str,
    phone: str = Form(...),
    password: str = Form(...),
    seat_num: str = Form(...),
    slots: str = Form("full"),
    slots_custom: str = Form(""),
):
    store = request.app.state.store
    existing = await store.get_account(acc_id)
    if not existing:
        raise HTTPException(404)
    slots_value: str | list[str] = slots
    if slots == "custom":
        slots_value = json.loads(slots_custom) if slots_custom.strip() else []
    existing.phone = phone
    existing.password = password
    existing.seat_num = seat_num.zfill(3)
    existing.slots = slots_value
    await store.upsert_account(existing)
    return RedirectResponse("/accounts", status_code=303)


@router.post("/accounts/{acc_id}/delete")
async def accounts_delete(request: Request, acc_id: str):
    store = request.app.state.store
    await store.delete_account(acc_id)
    return RedirectResponse("/accounts", status_code=303)


# ---------- tasks ----------
@router.get("/tasks", response_class=HTMLResponse)
async def tasks_list(request: Request, account_id: str | None = None, day: str | None = None):
    store = request.app.state.store
    d = date.fromisoformat(day) if day else today_cst()
    tasks = await store.list_tasks(account_id=account_id, day=d)
    accounts = await store.list_accounts()
    return _templates(request).TemplateResponse(
        "tasks_list.html",
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
    h1, m1 = map(int, start.split(":"))
    h2, m2 = map(int, end.split(":"))
    t = Task(
        id=None, account_id=acc.id, day=today_cst(),
        start_time=time(h1, m1), end_time=time(h2, m2),
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


# ---------- seats ----------
@router.get("/seats", response_class=HTMLResponse)
async def seats_view(request: Request):
    cfg = request.app.state.cfg
    return _templates(request).TemplateResponse(
        "seats.html", {"request": request, "cfg": cfg}
    )


@router.get("/api/seats/{room_id}")
async def api_seats(room_id: int, request: Request):
    sched = request.app.state.sched
    cfg = request.app.state.cfg
    # Use the first account's client just to call the API
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


# ---------- logs ----------
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
        "logs.html",
        {"request": request, "logs": rows, "accounts": accounts,
         "filter_account": account_id, "filter_level": level},
    )


# NOTE: tasks / seats / logs routes are added in Task 16.
