"""一次性重试: 王海艳名下 2026-08-31 的 FAILED 任务 (违约上限解除后补约)。"""
from __future__ import annotations

import asyncio
import sys
from datetime import date

sys.path.insert(0, ".")

from seatbot.config import load_config
from seatbot.models import TaskStatus
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore

TARGET_DAY = date(2026, 8, 31)
RUN_DATE = date(2026, 8, 31)


async def main() -> int:
    from seatbot.utils.timeutil import today_cst
    if today_cst() != RUN_DATE:
        print(f"非约定执行日 ({today_cst()} != {RUN_DATE}), 退出")
        return 0
    cfg = load_config("config.yaml")
    store = StateStore(cfg.runtime.db_path, legacy_target_seat_num=None)
    await store.init()
    sched = Scheduler(cfg, store)
    ok = fail = 0
    try:
        todo = [t for t in await store.list_tasks(day=TARGET_DAY)
                if t.status == TaskStatus.FAILED and t.account_id == "王海艳"]
        print(f"[retry] 待重试: {len(todo)} 条", flush=True)
        for t in todo:
            acc = await store.get_account(t.account_id)
            await store.update_task_status(t.id, TaskStatus.READY, last_error="")
            fresh = await store.get_task(t.id)
            await sched._run_submit(acc, fresh)
            after = await store.get_task(t.id)
            good = after.status == TaskStatus.ACTIVE and bool(after.reserve_id)
            print(f"[{'OK ' if good else 'FAIL'}] #{t.id} {t.account_id}/{t.seat_num} "
                  f"{t.start_time}-{t.end_time} rid={after.reserve_id} "
                  f"{after.last_error or ''}", flush=True)
            ok += int(good)
            fail += int(not good)
            await asyncio.sleep(3)
        print(f"[retry] 完成: ok={ok} fail={fail}", flush=True)
        if ok and fail == 0:
            await sched._notify(
                "缺口已补约（2026-08-31）",
                "030 15-17 与 031 19-21 重试成功，明日 16 段全满，"
                "自动签到签退已就绪（王海艳 cron 已随重启注册）。",
                level="info",
            )
        elif fail:
            await sched._notify(
                "周一重试仍失败（2026-08-31）",
                f"成功 {ok} / 失败 {fail}，违约上限可能尚未清零；"
                f"可稍后在任务看板原账号重试。",
                level="error",
            )
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
