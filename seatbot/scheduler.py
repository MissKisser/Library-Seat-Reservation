"""APScheduler wrapper (v2: per-task seat_num, cross-account/cross-seat relay)."""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from seatbot.client import ChaoxingClient, ChaoxingError
from seatbot.config import Config
from seatbot.enc import EncGenerator
from seatbot.models import Account, Task, TaskStatus
from seatbot.planner import ReservationPlanner
from seatbot.store import StateStore
from seatbot.utils.timeutil import at_cst, now_cst, today_cst


def _overlap(s1, e1, s2, e2) -> bool:
    """Half-open interval overlap: [s, e) ⋂ [s', e') ≠ ∅"""
    return not (e1 <= s2 or s1 >= e2)


@dataclass
class NextRelay:
    at: datetime
    delta_minutes: int
    account_id: str
    task_id: int
    seat_num: str
    start_time: time
    end_time: time
    status: str


class Scheduler:
    RELAY_LEAD_SECONDS = -60  # leave 推迟到 end_time 之后 60s (用户要求"到点再签退", 加 60s 缓冲避免服务端拒绝)

    def __init__(
        self,
        cfg: Config,
        store: StateStore,
        enc: EncGenerator | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.enc = enc or EncGenerator()
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        self._clients: dict[str, ChaoxingClient] = {}
        self._bootstrap_done_for: set[tuple[str, str, str]] = set()  # (account_id, day, seat_num)

    def _client_for(self, acc: Account) -> ChaoxingClient:
        if acc.id not in self._clients:
            self._clients[acc.id] = ChaoxingClient()
        return self._clients[acc.id]

    # ---------- logging helpers ----------
    async def _info(self, msg: str, acc: str | None = None) -> None:
        await self.store.log_message("INFO", acc, msg)
        print(f"[INFO] {acc or '-'} {msg}")

    async def _warn(self, msg: str, acc: str | None = None) -> None:
        await self.store.log_message("WARN", acc, msg)
        print(f"[WARN] {acc or '-'} {msg}")

    async def _error(self, msg: str, acc: str | None = None) -> None:
        await self.store.log_message("ERROR", acc, msg)
        print(f"[ERROR] {acc or '-'} {msg}")

    # ---------- bootstrap ----------
    async def bootstrap_today(self) -> None:
        today = today_cst()
        target_seats = await self.store.list_target_seats()
        for acc in await self.store.list_accounts():
            await self._bootstrap_for_account(acc, today, [s.seat_num for s in target_seats])

    async def _bootstrap_for_account(self, acc: Account, day: date, fallback_seats: list[str]) -> None:
        # 一个 (account, day, seat_num) 三元组视为一个 bootstrap 单位
        seats = acc.bound_seats or fallback_seats
        for seat_num in seats:
            key = (acc.id, day.isoformat(), seat_num)
            if key in self._bootstrap_done_for:
                continue
            existing = await self.store.list_tasks(account_id=acc.id, day=day, seat_num=seat_num)
            if existing:
                self._bootstrap_done_for.add(key)
                await self._info(f"already has {len(existing)} tasks for {day} seat={seat_num}", acc.id)
                continue
            prev = await self.store.get_bootstrap_day(acc.id)
            if prev == day:
                self._bootstrap_done_for.add(key)
                continue

            # ★ 拉取该 (day, seat) 上 user_reserved 段,跳过冲突
            reserved_slots = await self.store.get_user_reserved_slot_set(day, seat_num)

            planner = ReservationPlanner(
                account=acc,
                bound_seats=acc.bound_seats,
                fallback_seats=fallback_seats,
                max_reserve_hours=self.cfg.library.max_reserve_hours,
            )
            try:
                tasks = planner.expand_for_day(day)
            except Exception as e:
                await self._error(f"planner failed: {e}", acc.id)
                return
            for t in tasks:
                if t.seat_num != seat_num:
                    continue
                # ★ 跳过与 user_reserved 重叠的 task
                if any(_overlap(t.start_time, t.end_time, rs, re_)
                       for rs, re_ in reserved_slots):
                    await self._info(
                        f"skip user_reserved seat={seat_num} {t.start_time.strftime('%H:%M')}-{t.end_time.strftime('%H:%M')}",
                        acc.id,
                    )
                    continue
                # ★ E4 防护: 同 (account_id, day, start_time) 已存在任务则跳过
                from datetime import time as _t
                start_t = _t(t.start_time.hour, t.start_time.minute)
                if await self.store.has_active_task_for_account_day_start(
                    acc.id, day, start_t, seat_num,
                ):
                    await self._info(
                        f"skip dup task seat={seat_num} {start_t.strftime('%H:%M')} (E4 guard)",
                        acc.id,
                    )
                    continue
                await self.store.add_task(t)
            self._bootstrap_done_for.add(key)
            await self._info(f"bootstrap for {day} seat={seat_num}", acc.id)
        # 更新 bootstrap_day 一次 (一个账号每天只一次)
        await self.store.set_bootstrap_day(acc.id, day)

    # ---------- per-account tick ----------
    async def tick_account(self, acc_id: str) -> None:
        acc_cfg = await self.store.get_account(acc_id)
        if not acc_cfg:
            return
        # 检查该账号是否已超出单日并发段 (v0.5+: 只检查 ACTIVE,不再含 pending/ready)
        # 原因: limit 本意是限制服务端并发段数,pending 是还没抢到的,
        # 把 pending 算进去会导致已经 bootstrap 过的账号永远被拦截, sign/leave 永远不触发
        today = today_cst()
        n_active = await self.store.count_tasks_for_account_day(
            acc_id, today,
            statuses=("active", "submitting", "leaving"),
        )
        limit = acc_cfg.one_account_max_concurrent_segments_per_day
        if n_active >= limit and limit > 0:
            # 已有 N 段激活中,不再触发更多 (避免超出后端上限)
            return
        active = await self.store.find_active_task(acc_id)
        now = now_cst()
        if active:
            t_start = at_cst(active.day, active.start_time)
            t_end = at_cst(active.day, active.end_time)
            # 1. 时段结束前 → leave (自动签退+接力)
            if now >= t_end - timedelta(seconds=self.RELAY_LEAD_SECONDS):
                await self._maybe_relay(active, now)
                return
            # 2. 时段进行中 → sign (补签)
            if t_start <= now < t_end:
                await self._run_sign(acc_cfg, active)
                return
            # 3. ACTIVE 但时段未开始 (异常: 提前 activate 了) → 不动
            return
        # no active task — 检查今天 pending tasks 是否时段进行中需补签
        # ★ v0.5+: tick 不再 submit — submit 完全交给 _afternoon_bootstrap (每天 14:00) 一次性提交
        # 这里只负责 sign (时段进行中) + leave (时段结束前)
        tasks = await self.store.list_tasks(account_id=acc_id, day=today)
        for t in tasks:
            if t.status == TaskStatus.ACTIVE:
                t_start = at_cst(t.day, t.start_time)
                t_end = at_cst(t.day, t.end_time)
                # 时段进行中 → sign (补签)
                if t_start <= now < t_end:
                    await self._run_sign(acc_cfg, t)
                    return
                # 时段结束前 → leave
                if now >= t_end - timedelta(seconds=self.RELAY_LEAD_SECONDS):
                    await self._maybe_relay(t, now)
                    return
            # PENDING task: 如果没 reserve_id 是 14:00 submit 失败,什么都不做(等下一个 tick)
            # 如果有 reserve_id 但 status 还不是 ACTIVE(异常状态),同样不动

    async def peek_next_relay(self) -> NextRelay | None:
        now = now_cst()
        today = today_cst()
        best: tuple[datetime, NextRelay] | None = None
        for acc in await self.store.list_accounts():
            for t in await self.store.list_tasks(account_id=acc.id, day=today):
                t_start = at_cst(t.day, t.start_time)
                t_end = at_cst(t.day, t.end_time)
                if t.status in (TaskStatus.ACTIVE, TaskStatus.SUBMITTING, TaskStatus.LEAVING):
                    fire_at = t_end - timedelta(seconds=self.RELAY_LEAD_SECONDS)
                elif t.status in (TaskStatus.PENDING, TaskStatus.READY, TaskStatus.FAILED):
                    fire_at = t_start
                else:
                    continue
                if fire_at <= now:
                    fire_at = now + timedelta(minutes=1)
                if best is None or fire_at < best[0]:
                    delta = max(0, int((fire_at - now).total_seconds() // 60))
                    best = (
                        fire_at,
                        NextRelay(
                            at=fire_at,
                            delta_minutes=delta,
                            account_id=acc.id,
                            task_id=t.id or 0,
                            seat_num=t.seat_num,
                            start_time=t.start_time,
                            end_time=t.end_time,
                            status=t.status.value,
                        ),
                    )
        return best[1] if best else None

    async def _maybe_relay(self, t: Task, now: datetime) -> None:
        """(v2) 跨 account / 跨 seat 的接力:leave 当前段。

        注意 (v0.5+): 接力只 leave 当前段,**不** submit 下一段。
        下一段已经在 _afternoon_bootstrap (14:00) 被预约过,无需再次 submit。
        sign 由下一段的 pre_sign 窗口 tick_account 触发。
        """
        t_end = at_cst(t.day, t.end_time)
        lead = t_end - timedelta(seconds=self.RELAY_LEAD_SECONDS)
        if now < lead:
            return
        acc = await self.store.get_account(t.account_id)
        if not acc:
            return
        await self._info(f"relay: leaving {t.chunk_key()}", acc.id)
        await self.store.update_task_status(t.id, TaskStatus.LEAVING)
        # leave 当前段
        await self._run_leave(acc, t)
        # ★ v0.5+: 不再 submit 下一段 — 由 _afternoon_bootstrap 14:00 统一处理
        nxt = await self.store.find_next_task_after(
            t.day, t.end_time,
            statuses=("pending", "ready", "failed"),
        )
        if nxt is None:
            await self._info("relay: no next task (不 submit,等 14:00 bootstrap)", acc.id)
            return
        await self._info(
            f"relay: leave 完成,下一段 task={nxt.id} ({nxt.account_id} {nxt.seat_num} {nxt.day} "
            f"{nxt.start_time.strftime('%H:%M')}-{nxt.end_time.strftime('%H:%M')}) 状态={nxt.status.value} "
            f"reserve_id={nxt.reserve_id or 'None'} (不 submit,等 14:00 bootstrap)",
            acc.id,
        )

    async def _run_submit(self, acc: Account, t: Task) -> None:
        """提交预约 (不签到)。

        调用时机:
          - 14:00 批量预约 ( _afternoon_bootstrap ) 时段离开始还远,不能签到
          - _maybe_relay 接力:leave 完上一段后,预约下一段 (下一段时段还未开始)
          - tick_account pre_sign 窗口:如果 task 还没有 reserve_id (14:00 未预约成功)

        submit 成功后 task.status = ACTIVE, store 存 reserve_id。
        sign 由 _run_sign 在时段开始后再调用。
        """
        await self.store.update_task_status(t.id, TaskStatus.SUBMITTING)
        client = self._client_for(acc)

        if not client.cookies():
            try:
                await client.login(acc.phone, acc.password)
                await self._info(f"login ok ({len(client.cookies())} cookies)", acc.id)
            except ChaoxingError as e:
                await self._warn(f"login prefetch failed ({e}); trying in-browser login", acc.id)

        try:
            r = await client.submit_in_browser(
                phone=acc.phone,
                password=acc.password,
                room_id=self.cfg.library.room_id,
                seat_num=t.seat_num,           # ★ v2: per-task
                day=t.day.isoformat(),
                start_time=t.start_time.strftime("%H:%M"),
                end_time=t.end_time.strftime("%H:%M"),
            )
        except Exception as e:
            await self._error(f"submit_in_browser exception: {e}", acc.id)
            await self.store.update_task_status(t.id, TaskStatus.FAILED, last_error=str(e))
            return

        await self.store.log_action(
            acc.id, "submit",
            f"seat={t.seat_num} {t.day} {t.start_time}-{t.end_time}",
            str(r.get("raw"))[:500],
            bool(r.get("success")),
            r.get("msg"),
        )

        if not r.get("success"):
            msg = r.get("msg") or "submit failed"
            await self._error(f"submit rejected: {msg} (seat={t.seat_num})", acc.id)
            await self.store.update_task_status(t.id, TaskStatus.FAILED, last_error=msg)
            return

        reserve_id = r.get("reserve_id")
        if not reserve_id:
            await self._error("submit ok but no reserve_id", acc.id)
            await self.store.update_task_status(t.id, TaskStatus.FAILED, last_error="no reserve_id")
            return

        await self.store.update_task_status(t.id, TaskStatus.ACTIVE, reserve_id=reserve_id)
        await self._info(f"reserved #{reserve_id} seat={t.seat_num} {t.chunk_key()}", acc.id)

    async def _run_sign(self, acc: Account, t: Task) -> None:
        """签到 (不 submit)。

        调用时机:
          - tick_account pre_sign 窗口 (时段开始前 ≤20min):task 应已 ACTIVE (14:00 已预约)
          - tick_account 时段进行中:补签 (如果之前没签上)

        需要 t.reserve_id。如果还没 reserve_id,说明 submit 还没成功,先调 _run_submit。
        """
        if not t.reserve_id:
            await self._warn(f"sign skipped: no reserve_id seat={t.seat_num} {t.chunk_key()}", acc.id)
            return
        client = self._client_for(acc)
        # ★ v0.5+: lazy login — _run_sign 是独立调用路径,_run_submit 已登录过则 cookies 复用;否则这里登录
        if not client.cookies():
            try:
                await client.login(acc.phone, acc.password)
                await self._info(f"sign login ok ({len(client.cookies())} cookies)", acc.id)
            except ChaoxingError as e:
                await self._warn(f"sign login prefetch failed ({e})", acc.id)
        try:
            sr = await client.sign(t.reserve_id)
            await self.store.log_action(
                acc.id, "sign", str(t.reserve_id), str(sr)[:500],
                bool(sr.get("success")), str(sr.get("msg")),
            )
            if not sr.get("success"):
                await self._error(f"sign failed: {sr.get('msg')}", acc.id)
        except Exception as e:
            await self._error(f"sign error: {e}", acc.id)
    async def _run_leave(self, acc: Account, t: Task) -> None:
        """签退 (不 submit/sign)。"""
        if not t.reserve_id:
            await self._warn(f"leave skipped: no reserve_id seat={t.seat_num} {t.chunk_key()}", acc.id)
            await self.store.update_task_status(t.id, TaskStatus.COMPLETE)
            return
        client = self._client_for(acc)
        # ★ v0.5+: lazy login — _run_leave 是独立调用路径
        if not client.cookies():
            try:
                await client.login(acc.phone, acc.password)
                await self._info(f"leave login ok ({len(client.cookies())} cookies)", acc.id)
            except ChaoxingError as e:
                await self._warn(f"leave login prefetch failed ({e})", acc.id)
        try:
            sr = await client.leave(t.reserve_id)
            await self.store.log_action(
                acc.id, "leave", str(t.reserve_id), str(sr)[:500],
                bool(sr.get("success")), str(sr.get("msg")),
            )
            if not sr.get("success"):
                await self._error(f"leave failed: {sr.get('msg')}", acc.id)
                # ★ leave 失败时 **不要** 标 COMPLETE — 留给下次 tick 重试
                await self.store.update_task_status(t.id, TaskStatus.ACTIVE)
                return
        except Exception as e:
            await self._error(f"leave error: {e}", acc.id)
            await self.store.update_task_status(t.id, TaskStatus.ACTIVE)
            return
        # leave 成功 → 标 COMPLETE
        await self.store.update_task_status(t.id, TaskStatus.COMPLETE)

    # ---------- cron wiring ----------
    async def sync_jobs(self) -> None:
        accounts = await self.store.list_accounts()
        wanted_ids = {a.id for a in accounts}
        existing_ids = {
            j.id.removeprefix("tick_")
            for j in self.scheduler.get_jobs()
            if j.id.startswith("tick_")
        }
        for stale in existing_ids - wanted_ids:
            try:
                self.scheduler.remove_job(f"tick_{stale}")
            except Exception:
                pass
        for i, acc in enumerate(accounts):
            if acc.id in existing_ids:
                continue
            delay = random.uniform(*self.cfg.runtime.stagger_seconds)
            self.scheduler.add_job(
                self._tick_account_with_bootstrap,
                "cron", second=f"{int(delay)}",
                args=[acc.id],
                id=f"tick_{acc.id}",
                replace_existing=True,
            )

    def start(self) -> None:
        self.scheduler.add_job(
            self.sync_jobs,
            "interval", seconds=30,
            id="sync_jobs", replace_existing=True,
        )
        self.scheduler.add_job(
            self._new_day_bootstrap,
            CronTrigger(hour=0, minute=0, second=5, timezone="Asia/Shanghai"),
            id="new_day_bootstrap", replace_existing=True,
        )
        # ★ 每天14:00触发：为明天生成预约任务（超星14:00后开放次日预约窗口）
        self.scheduler.add_job(
            self._afternoon_bootstrap,
            CronTrigger(hour=14, minute=0, second=10, timezone="Asia/Shanghai"),
            id="afternoon_bootstrap", replace_existing=True,
        )
        self.scheduler.start()

    async def _tick_account_with_bootstrap(self, acc_id: str) -> None:
        await self._bootstrap_for_account_if_needed(acc_id)
        await self.tick_account(acc_id)

    async def _bootstrap_for_account_if_needed(self, acc_id: str) -> None:
        acc_cfg = await self.store.get_account(acc_id)
        if not acc_cfg:
            return
        today = today_cst()
        seats = await self.store.list_target_seats()
        await self._bootstrap_for_account(
            acc_cfg, today, [s.seat_num for s in seats]
        )

    async def _new_day_bootstrap(self) -> None:
        today = today_cst()
        seats = await self.store.list_target_seats()
        for acc in await self.store.list_accounts():
            await self._bootstrap_for_account(acc, today, [s.seat_num for s in seats])

    async def _afternoon_bootstrap(self) -> None:
        """每天14:00触发：为明天生成预约任务并立即提交。

        超星预约系统在14:00后开放次日预约窗口(`reserveBeforeTime: 14:00`),
        此时立即提交预约请求,服务端按 `reserveBeforeTime` 校验后接受/拒绝。
        bootstrap_for_account 内部有 (account_id, day, seat_num) 三元组防重保护,
        重复调用时幂等。

        错误恢复 (v0.5+):
          - 每个 task 的 _run_submit 独立 try/except
          - 一个 task 失败不中断后续 task
          - 失败的 task 状态保持 PENDING (让明天的 _afternoon_bootstrap 重试)
          - 显式 ERROR 日志
        """
        from datetime import timedelta
        from seatbot.models import TaskStatus
        tomorrow = today_cst() + timedelta(days=1)
        submitted = 0
        failed = 0
        for acc in await self.store.list_accounts():
            tasks = await self.store.list_tasks(account_id=acc.id, day=tomorrow)
            pending_tasks = [t for t in tasks if t.status == TaskStatus.PENDING]
            if not pending_tasks:
                continue
            # 账号间错开 3 秒,避免 Playwright 资源冲突 / 风控检测
            if submitted > 0:
                await asyncio.sleep(3)
            for t in pending_tasks:
                # 单 task 独立 try/except — 一个失败不影响其他
                try:
                    await self._run_submit(acc, t)
                    # 检查结果:reserve_id 写入 = 成功,否则 _run_submit 已标 FAILED
                    after = await self.store.get_task(t.id)
                    if after and after.status == TaskStatus.ACTIVE and after.reserve_id:
                        submitted += 1
                    else:
                        failed += 1
                        await self._warn(
                            f"afternoon_bootstrap: submit 不成功, task={t.id} "
                            f"acc={acc.id} seat={t.seat_num} {t.day} {t.start_time}-{t.end_time}",
                            acc.id,
                        )
                except Exception as e:
                    failed += 1
                    await self._error(
                        f"afternoon_bootstrap: _run_submit 抛异常 {type(e).__name__}: {e} "
                        f"task={t.id} acc={acc.id} seat={t.seat_num} {t.day} {t.start_time}-{t.end_time}",
                        acc.id,
                    )
                    # 任务保持 PENDING, 让明天的 _afternoon_bootstrap 重试
                # 单账号内 task 间错开 2 秒,避免连续 submit 触发风控
                await asyncio.sleep(2)
        await self._info(
            f"afternoon_bootstrap 完成: submitted={submitted}, failed={failed}, day={tomorrow}",
            "scheduler",
        )
    async def shutdown(self) -> None:
        self.scheduler.shutdown(wait=False)
        for c in self._clients.values():
            await c.close()
