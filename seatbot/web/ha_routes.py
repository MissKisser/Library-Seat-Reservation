"""/api/ha/* 控制面路由。

所有 handler 从 request.app.state.ha 取 HaRuntime，鉴权由 _require_ha_key 依赖完成。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from seatbot.ha import HaRuntime, NullHaRuntime, _constant_time_eq
from seatbot.ha_sync import apply_snapshot


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/ha")


def _get_runtime(request: Request) -> HaRuntime:
    ha = getattr(request.app.state, "ha", None)
    if ha is None:
        ha = NullHaRuntime()
        request.app.state.ha = ha
    return ha


def _require_ha_key(request: Request) -> HaRuntime:
    """FastAPI 依赖：X-HA-Key 缺失/错误 → 401，常数时间比对。"""
    runtime = _get_runtime(request)
    presented = request.headers.get("X-HA-Key", "")
    expected = runtime.cfg.key if runtime and runtime.cfg else ""
    if not expected or not _constant_time_eq(presented, expected):
        raise HTTPException(status_code=401, detail="invalid or missing X-HA-Key")
    return runtime


@router.post("/bk/heartbeat")
async def bk_heartbeat(request: Request, runtime: HaRuntime = Depends(_require_ha_key)) -> dict[str, Any]:
    """主力→备用：刷新 last_heartbeat_seen；若本端 backup 已 active 则转 failback_pending。"""
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    now = runtime.now()
    runtime.last_heartbeat_seen = now
    runtime.last_primary_contact = now
    # 备用正在接管时收到心跳 → 进入 failback 协商
    if runtime.mode == "backup" and runtime.backup_state == "active":
        runtime.backup_state = "failback_pending"
        try:
            logger.info("ha: backup entered failback_pending after heartbeat")
        except Exception:
            pass
    return {
        "role": runtime.mode if runtime.mode in ("primary", "backup") else "standalone",
        "active_since": runtime.active_since,
    }


@router.get("/bk/state")
async def bk_state(request: Request, runtime: HaRuntime = Depends(_require_ha_key)) -> dict[str, Any]:
    """主力→备用：报告本端当前状态机快照。"""
    return runtime.status_payload()


@router.post("/bk/claim")
async def bk_claim(request: Request, runtime: HaRuntime = Depends(_require_ha_key)) -> dict[str, Any]:
    """主力→备用：触发 failback_pending + 启动后台 restore 推送循环。

    占位实现（Task 8 实装真正的 _push_restore_loop 协程）；幂等。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    if runtime.mode == "backup" and runtime.backup_state != "standby":
        runtime.backup_state = "failback_pending"
    elif runtime.mode == "backup":
        # 已经是 standby 时，claim 不触发任何动作
        pass
    # 委托真正的后台循环（Task 8 占位实现）
    store = getattr(request.app.state, "store", None)
    sched = getattr(request.app.state, "sched", None)
    if store is not None and sched is not None and runtime.mode == "backup":
        try:
            from seatbot.ha import _push_restore_loop
            asyncio.create_task(_push_restore_loop(store, runtime))
        except Exception as exc:
            logger.warning("ha: schedule push_restore_loop failed: %s", exc)
    return {"ok": True, "state": runtime.backup_state if runtime.mode == "backup" else runtime.primary_state}


@router.post("/bk/snapshot")
async def bk_snapshot(request: Request, runtime: HaRuntime = Depends(_require_ha_key)) -> dict[str, Any]:
    """主力→备用：接收 gzip 快照 body 并应用。"""
    body = await request.body()
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="store not ready")
    try:
        result = await apply_snapshot(store, body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"snapshot rejected: {exc}") from exc
    return result


@router.get("/status")
async def ha_status(request: Request, runtime: HaRuntime = Depends(_require_ha_key)) -> dict[str, Any]:
    """公开（需 key）：本机身份 + 当前状态。"""
    return runtime.status_payload()


@router.post("/restore")
async def ha_restore(request: Request, runtime: HaRuntime = Depends(_require_ha_key)) -> dict[str, Any]:
    """备用→主力：仅 warming 态接受，apply 后 200，否则 409。"""
    if runtime.mode != "primary" or runtime.primary_state != "warming":
        raise HTTPException(status_code=409, detail="restore only accepted in warming state")
    body = await request.body()
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="store not ready")
    try:
        result = await apply_snapshot(store, body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"restore rejected: {exc}") from exc
    # 应用成功：进入 active（与 _push_restore_loop 互补；这里给端点直调路径兜底）
    runtime.primary_state = "active"
    runtime.active_since = runtime.now()
    return result


__all__ = ["router"]