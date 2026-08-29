"""一次性 14:00:03 下午批量预约 (2026-08-30)。

常驻服务运行于提权上下文无法从当前会话重启,其内存中的提交通道不含
锚点回退与重试轮;本脚本以修复后的代码在 14:00:03 先行执行同一条
_afternoon_bootstrap 路径。常驻服务 14:00:10 的同款 cron 因防重与
PENDING 过滤而空转;若本脚本先成功,常驻服务纯兜底。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from seatbot.config import load_config
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore


async def main() -> int:
    target = datetime.now().replace(
        hour=14, minute=0, second=3, microsecond=0
    ) + timedelta(days=1)
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
