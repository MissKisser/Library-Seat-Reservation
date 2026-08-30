"""一次性重排: 2026-08-31 预约从 16 碎片段切换为 12 大段 (6 账号矩阵)。

顺序: 更新期望/矩阵 → 逐账号 [取消旧预约 → 立即提交新大段] → 服务端核验。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date, time as _time

sys.path.insert(0, ".")

from seatbot.config import load_config
from seatbot.models import Account, Task, TaskStatus
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore
from seatbot.utils.timeutil import at_cst, today_cst

TARGET_DAY = date(2026, 8, 31)

NEW_MATRIX = {
    "刘恒": {"030": ["08:00-10:00"], "031": ["10:00-12:00"]},
    "朱宇": {"030": ["10:00-12:00"], "031": ["08:00-10:00"]},
    "王杭": {"030": ["14:00-16:00"], "031": ["19:00-21:00"]},
    "缪祥宁": {"030": ["16:00-18:00"], "031": ["14:00-16:00"]},
    "熊金涛": {"030": ["19:00-21:00"], "031": ["21:00-22:00"]},
    "李勇杰": {"030": ["21:00-22:00"], "031": ["16:00-18:00"]},
    "赵吉兴": {},
    "赵正宏": {},
    "王海艳": {},
}
CHUNKS = ["08:00-10:00", "10:00-12:00", "14:00-16:00",
          "16:00-18:00", "19:00-21:00", "21:00-22:00"]


async def main() -> int:
    cfg = load_config("config.yaml")
    store = StateStore(cfg.runtime.db_path, legacy_target_seat_num=None)
    await store.init()
    sched = Scheduler(cfg, store)
    ok = fail = 0
    try:
        # 1) 期望时段 = 6 大段
        for sn in ("030", "031"):
            await store.set_target_seat_desired(sn, CHUNKS)
        print("[1] 期望时段已更新为 6 大段", flush=True)

        # 2) 矩阵重排
        for aid, matrix in NEW_MATRIX.items():
            acc = await store.get_account(aid)
            if not acc:
                print(f"[2] 跳过 {aid}: 不存在", flush=True)
                continue
            acc.seat_slots = matrix or None
            acc.bound_seats = sorted(matrix.keys())
            await store.upsert_account(acc)
        print("[2] 矩阵已重排 (6 账号上岗, 赵吉兴轮休, 赵正宏/王海艳退出)", flush=True)

        # 3) 取消明天全部旧预约
        old_tasks = [t for t in await store.list_tasks(day=TARGET_DAY)
                     if t.status in (TaskStatus.ACTIVE, TaskStatus.SIGNED,
                                     TaskStatus.LEAVING, TaskStatus.FAILED)]
        for t in old_tasks:
            acc = await store.get_account(t.account_id)
            if not acc or not t.reserve_id:
                await store.update_task_status(
                    t.id, TaskStatus.FAILED, last_error="重排方案作废")
                continue
            client = sched._client_for(acc)
            if not client.cookies():
                try:
                    await client.login(acc.phone, acc.password)
                except Exception as e:
                    print(f"[3] {t.account_id} 登录失败: {e}", flush=True)
                    continue
            r = await client.cancel(t.reserve_id)
            good = bool(r.get("success"))
            await store.log_action(
                t.account_id, "cancel", str(t.reserve_id), str(r)[:500],
                good, str(r.get("msg")))
            if good:
                await store.update_task_status(t.id, TaskStatus.COMPLETE, last_error="")
                print(f"[3] 取消 #{t.id} {t.account_id}/{t.seat_num} "
                      f"{t.start_time}-{t.end_time} rid={t.reserve_id}", flush=True)
            else:
                await store.update_task_status(
                    t.id, t.status, last_error=f"取消失败: {r.get('msg')}")
                print(f"[3] 取消失败 #{t.id} {t.account_id} rid={t.reserve_id}: "
                      f"{r.get('msg')}", flush=True)
            await asyncio.sleep(2)

        # 4) 按 6 大段重新预约 (取消一个账号立刻约它的净位置)
        for aid, matrix in NEW_MATRIX.items():
            if not matrix:
                continue
            acc = await store.get_account(aid)
            for seat, (s, e) in ((k, v[0].split("-")) for k, v in matrix.items()):
                exist = [x for x in await store.list_tasks(
                    account_id=aid, day=TARGET_DAY, seat_num=seat)
                    if x.reserve_id and x.status == TaskStatus.ACTIVE]
                if exist:
                    print(f"[4] 跳过 {aid}/{seat}: 已有 rid={exist[0].reserve_id}", flush=True)
                    continue
                st = _time(int(s[:2]), int(s[3:]))
                en = _time(int(e[:2]), int(e[3:]))
                t = Task(id=None, account_id=aid, day=TARGET_DAY,
                         start_time=st, end_time=en,
                         seat_num=seat, status=TaskStatus.READY)
                tid = await store.add_task(t)
                fresh = await store.get_task(tid)
                await sched._run_submit(acc, fresh)
                after = await store.get_task(tid)
                good = after.status == TaskStatus.ACTIVE and bool(after.reserve_id)
                print(f"[4] {'OK ' if good else 'FAIL'} {aid}/{seat} {s}-{e} "
                      f"rid={after.reserve_id} {after.last_error or ''}", flush=True)
                ok += int(good)
                fail += int(not good)
                await asyncio.sleep(3)

        # 5) 服务端核验
        print("[5] 服务端占用核验:", flush=True)
        from seatbot.client import ChaoxingClient
        probe_acc = await store.get_account("刘恒")
        client = ChaoxingClient()
        await client.login(probe_acc.phone, probe_acc.password)
        for seat in ("030", "031"):
            used = await client.get_used_times(cfg.library.room_id, seat, TARGET_DAY.isoformat())
            print(f"    {seat}: {used}", flush=True)
        await client.close()
        print(f"[done] 新大段预约: ok={ok} fail={fail}", flush=True)
    finally:
        for c in list(sched._clients.values()):
            try:
                await c.close()
            except Exception:
                pass
        await store.close()
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
