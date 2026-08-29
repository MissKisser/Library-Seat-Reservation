"""一次性补约: 030 座位 2026-08-29 晚间 19:00-20:00。

原晚间预约 (rid 189684316) 已在服务端消失, 19:00-20:00 出现空档。
17:05 起每 15 分钟尝试一次 (至 18:45): 若该时段已被占 (含用户手动补约)
则直接收工; 否则经锚点座位页改写通道提交, 无锚点时退回目标座位页直提。
成功后任务 #46 转 ACTIVE, 由常驻服务在 19:00 自动签到。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, time, timedelta

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

from seatbot.client import ChaoxingClient
from seatbot.config import load_config
from seatbot.models import TaskStatus
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore
from session_cache import authenticated_client, save

ACCOUNT = "xiongjt"
SEAT = "030"
TASK_ID = 46
START, END = "19:00", "20:00"
TRY_TIMES = ("17:05", "17:20", "17:40", "18:00", "18:20", "18:45")


def _hm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


async def main() -> int:
    for attempt, ts in enumerate(TRY_TIMES, 1):
        target = datetime.now().replace(hour=_hm(ts).hour, minute=_hm(ts).minute,
                                         second=0, microsecond=0)
        await asyncio.sleep(max(0.0, (target - datetime.now()).total_seconds()))
        print(f"===== 第 {attempt} 轮 @ {datetime.now():%H:%M:%S} =====", flush=True)

        cfg = load_config("config.yaml")
        import sqlite3
        con = sqlite3.connect(f"file:{cfg.runtime.db_path}?mode=ro", uri=True)
        phone, pw = con.execute(
            "SELECT phone, password FROM accounts WHERE id=?", (ACCOUNT,)).fetchone()
        con.close()

        async def probe(c):
            await c.get_used_times(cfg.library.room_id, SEAT, date.today().isoformat())
            return True
        cached, src = await authenticated_client(ACCOUNT, phone, pw, probe)

        used = await cached.get_used_times(cfg.library.room_id, SEAT, date.today().isoformat())
        covered = any(s <= START and e >= END for s, e in used)
        print(f"session={src} {SEAT} 占用={used}")
        if covered:
            print("19:00-20:00 已被覆盖 (可能用户已手动补约), 收工")
            await cached.close()
            return 0
        if any(s < END and e > START for s, e in used):
            print("19:00-20:00 出现部分重叠占用, 无法整段补约, 收工")
            await cached.close()
            return 0

        store = StateStore(cfg.runtime.db_path, legacy_target_seat_num=None)
        await store.init()
        sched = Scheduler(cfg, store)
        sched._clients[ACCOUNT] = cached

        anchor = await sched._pick_anchor_seat(cached, SEAT)
        print(f"锚点={anchor}")
        _orig_prw = ChaoxingClient.submit_via_page_rewrite

        async def _browser_via_rewrite(self, phone, password, room_id, seat_num,
                                        day, start_time, end_time):
            return await _orig_prw(self, phone, password, room_id, seat_num, day,
                                   start_time, end_time, anchor_seat=anchor)
        ChaoxingClient.submit_in_browser = _browser_via_rewrite

        acc = await store.get_account(ACCOUNT)
        await store.update_task_status(TASK_ID, TaskStatus.READY, last_error="")
        fresh = await store.get_task(TASK_ID)
        await sched._run_submit(acc, fresh)
        after = await store.get_task(TASK_ID)
        print(f"#46: status={after.status.value} rid={after.reserve_id} "
              f"err={after.last_error}", flush=True)
        if after.status == TaskStatus.ACTIVE and after.reserve_id:
            used2 = await cached.get_used_times(
                cfg.library.room_id, SEAT, date.today().isoformat())
            print("核验占用:", used2)
            save(ACCOUNT, cached)
            await cached.close()
            await store.close()
            return 0
        await cached.close()
        await store.close()
    print("全部轮次用尽, 未能补约")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
