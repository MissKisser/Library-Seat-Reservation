"""恢复 2026-08-27 失守任务: 复位 FAILED→PENDING 后重跑 14:00 同款批量提交。

⚠️ 高危: 对 3 个守护账号发起真实预约。
   授权: 用户 2026-08-26 会话明确指示"验证通过就补约"。
用法:
  .venv/Scripts/python.exe scripts/oneoff_recover_0827.py
"""
import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seatbot.config import load_config
from seatbot.models import TaskStatus
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore

DAY = date(2026, 8, 27)


async def main() -> int:
    cfg = load_config("config.yaml")
    store = StateStore(cfg.runtime.db_path)
    await store.init()
    try:
        tasks = await store.list_tasks(day=DAY)
        stale = [t for t in tasks if t.status == TaskStatus.FAILED]
        if not stale:
            print("[recover] no failed 08-27 tasks to recover")
            return 1
        for t in stale:
            await store.update_task_status(t.id, TaskStatus.PENDING)
            print(f"[recover] task={t.id} {t.account_id} seat={t.seat_num} "
                  f"{t.start_time}-{t.end_time}: failed -> pending")

        sched = Scheduler(cfg, store)
        await sched._afternoon_bootstrap()

        ok = 0
        print("[verify] == 08-27 任务终态 ==")
        for t in await store.list_tasks(day=DAY):
            good = t.status == TaskStatus.ACTIVE and t.reserve_id
            ok += bool(good)
            print(f"[verify] {'OK ' if good else 'BAD'} task={t.id} {t.account_id} "
                  f"seat={t.seat_num} {t.start_time}-{t.end_time} "
                  f"status={t.status.value} rid={t.reserve_id}")
        try:
            await sched.shutdown()
        except Exception:
            pass   # 独立进程未 start() 调度器时 shutdown 会抛 SchedulerNotRunningError
        print(f"[recover] RESULT: {ok}/{len(stale)} recovered")
        return 0 if ok == len(stale) else 1
    finally:
        await store.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
