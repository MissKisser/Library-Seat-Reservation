"""一次性救援: 2026-08-30 14:25 复查 08-31 全部未完成任务并补约。

服务自带三轮重试预计 14:10 前收敛; 本脚本作为第四道保险, 仅处理
状态仍为 PENDING/READY/SUBMITTING/FAILED 的 08-31 任务。锚点固定取
另一目标座位 (030↔031 互为锚点), 复用 Scheduler._run_submit 状态机。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date

sys.path.insert(0, ".")

from seatbot.client import ChaoxingClient
from seatbot.config import load_config
from seatbot.models import TaskStatus
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore

TARGET_DAY = date(2026, 8, 31)
RUN_DATE = date(2026, 8, 30)

_orig_submit = ChaoxingClient.submit_via_page_rewrite


async def _submit_with_anchor(self, phone, password, room_id, seat_num,
                              day, start_time, end_time):
    anchor = "031" if seat_num == "030" else "030"
    return await _orig_submit(
        self, phone, password, room_id, seat_num, day,
        start_time, end_time, anchor_seat=anchor,
    )


async def main() -> int:
    from seatbot.utils.timeutil import today_cst
    if today_cst() != RUN_DATE:
        print(f"非约定执行日 ({today_cst()} != {RUN_DATE}), 退出")
        return 0
    ChaoxingClient.submit_via_page_rewrite = _submit_with_anchor
    cfg = load_config("config.yaml")
    store = StateStore(cfg.runtime.db_path, legacy_target_seat_num=None)
    await store.init()
    sched = Scheduler(cfg, store)
    ok = fail = 0
    try:
        todo = [t for t in await store.list_tasks(day=TARGET_DAY)
                if t.status in (TaskStatus.PENDING, TaskStatus.READY,
                                TaskStatus.SUBMITTING, TaskStatus.FAILED)]
        print(f"[rescue] 待补约: {len(todo)} 条 (锚点 030↔031 互备)")
        if not todo:
            return 0
        for t in sorted(todo, key=lambda x: (x.account_id, x.start_time)):
            acc = await store.get_account(t.account_id)
            if not acc:
                print(f"[SKIP] #{t.id} 账号 {t.account_id} 不存在", flush=True)
                continue
            await store.update_task_status(t.id, TaskStatus.READY, last_error="")
            fresh = await store.get_task(t.id)
            await sched._run_submit(acc, fresh)
            after = await store.get_task(t.id)
            good = after.status == TaskStatus.ACTIVE and bool(after.reserve_id)
            print(f"[{'OK ' if good else 'FAIL'}] #{t.id} {t.account_id}/{t.seat_num} "
                  f"{t.start_time}-{t.end_time} -> {after.status.value} "
                  f"rid={after.reserve_id} {after.last_error or ''}", flush=True)
            ok += int(good)
            fail += int(not good)
            await asyncio.sleep(3)
        print(f"[rescue] 完成: ok={ok} fail={fail}", flush=True)
    finally:
        for client in list(sched._clients.values()):
            try:
                await client.close()
            except Exception:
                pass
        await store.close()
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
