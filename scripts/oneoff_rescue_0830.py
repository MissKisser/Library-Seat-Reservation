"""一次性救援: 重提 2026-08-30 全部 FAILED 任务。

14:00 批量因目标座位页无可选格全部失败。本脚本以空闲座位页 (anchor_seat)
作表单触发锚点, 真实座位/日期/时段仍由网络层改写覆盖; 复用 Scheduler
_run_submit 的完整状态机, 成功后任务转 ACTIVE 并写入 reserve_id。
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

ANCHOR_SEAT = "031"
TARGET_DAY = date(2026, 8, 30)

_orig_submit = ChaoxingClient.submit_via_page_rewrite


async def _submit_with_anchor(self, phone, password, room_id, seat_num,
                              day, start_time, end_time):
    return await _orig_submit(
        self, phone, password, room_id, seat_num, day,
        start_time, end_time, anchor_seat=ANCHOR_SEAT,
    )


async def main() -> int:
    ChaoxingClient.submit_via_page_rewrite = _submit_with_anchor
    cfg = load_config("config.yaml")
    store = StateStore(cfg.runtime.db_path, legacy_target_seat_num=None)
    await store.init()
    sched = Scheduler(cfg, store)
    ok = fail = 0
    try:
        tasks = [t for t in await store.list_tasks(day=TARGET_DAY)
                 if t.status == TaskStatus.FAILED]
        print(f"待救援: {len(tasks)} 条 (锚点页={ANCHOR_SEAT})")
        for t in sorted(tasks, key=lambda x: (x.account_id, x.start_time)):
            acc = await store.get_account(t.account_id)
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
        print(f"救援完成: ok={ok} fail={fail}")
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
