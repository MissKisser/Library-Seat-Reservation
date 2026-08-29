"""一次性 14:00:03 下午批量预约。

常驻服务的 afternoon_bootstrap cron 为启动时注册的 second=10 且服务
运行于提权上下文无法即时重启;本脚本以完全相同的代码路径在 14:00:03
先行执行。常驻服务 14:00:10 的同款 cron 因 (account, day, seat) 防重
与 PENDING 过滤而空转,不会产生双提交;若本脚本失败,常驻服务自然兜底。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime

sys.path.insert(0, ".")

from seatbot.config import load_config
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore


async def main() -> int:
    target = datetime.now().replace(hour=14, minute=0, second=3, microsecond=0)
    await asyncio.sleep(max(0.0, (target - datetime.now()).total_seconds()))

    cfg = load_config("config.yaml")
    store = StateStore(cfg.runtime.db_path, legacy_target_seat_num=None)
    await store.init()
    sched = Scheduler(cfg, store)
    try:
        print(f"[{datetime.now():%H:%M:%S.%f}] 开始下午批量 (一次性 14:00:03)", flush=True)
        await sched._afternoon_bootstrap()
        print(f"[{datetime.now():%H:%M:%S}] 完成", flush=True)
    finally:
        for client in list(sched._clients.values()):
            try:
                await client.close()
            except Exception:
                pass
        await store.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
