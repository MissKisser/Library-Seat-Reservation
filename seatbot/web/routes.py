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


# NOTE: tasks / seats / logs routes are added in Task 16.
