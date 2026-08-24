"""APScheduler wrapper (v2: per-task seat_num, cross-account/cross-seat relay)."""
from __future__ import annotations

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
    RELAY_LEAD_SECONDS = 5 * 60  # leave 5 minutes before end_time

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
        # 检查该账号是否已超出单日并发上限
        today = today_cst()
        n_today = await self.store.count_tasks_for_account_day(
            acc_id, today,
            statuses=("pending", "ready", "active", "submitting", "leaving"),
        )
        limit = acc_cfg.one_account_max_concurrent_segments_per_day
        if n_today >= limit and limit > 0:
            # 已经安排了 N 段,不再触发更多 (避免超出后端上限)
            return

        active = await self.store.find_active_task(acc_id)
        now = now_cst()
        if active:
            await self._maybe_relay(active, now)
            return
        # no active task — find the next pending task that should be running
        tasks = await self.store.list_tasks(account_id=acc_id, day=today)
        for t in tasks:
            if t.status not in (TaskStatus.PENDING, TaskStatus.READY, TaskStatus.FAILED):
                continue
            t_start = at_cst(t.day, t.start_time)
            t_end = at_cst(t.day, t.end_time)
            # currently inside the slot — submit (如果还没) + sign
            if t_start <= now < t_end:
                if not t.reserve_id:
                    await self._run_submit(acc_cfg, t)
                    t = await self.store.get_task(t.id)
                if t and t.reserve_id:
                    await self._run_sign(acc_cfg, t)
                return
            # pre_sign 窗口 (时段开始前 ≤20min) — submit (如果还没),不 sign
            # sign 需要到时段开始时刻才被服务端接受
            if timedelta(0) <= (t_start - now) <= timedelta(minutes=20):
                if not t.reserve_id:
                    await self._run_submit(acc_cfg, t)
                # 故意**不**调 _run_sign — 等时段开始的下一个 tick
                return

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
        """(v2) 跨 account / 跨 seat 的接力:leave 当前段,从 DB 查下一段 task。

        注意:接力只 leave 当前 + submit 下一段 (不 sign)。
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

        # 跨 account / 跨 seat 找下一段 (按 start_time 升序)
        nxt = await self.store.find_next_task_after(
            t.day, t.end_time,
            statuses=("pending", "ready", "failed"),
        )
        if nxt is None:
            await self._info("relay: no next task", acc.id)
            return
        nxt_acc = await self.store.get_account(nxt.account_id)
        if not nxt_acc:
            return
        # submit 下一段 (但**不** sign — 下一段时段还未开始)
        await self._run_submit(nxt_acc, nxt)

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
        try:
            await client.leave(t.reserve_id)
            await self.store.log_action(acc.id, "leave", str(t.reserve_id), "", True)
        except Exception as e:
            await self._error(f"leave failed: {e}", acc.id)
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
        """
        from datetime import timedelta
        from seatbot.models import TaskStatus
        submitted = 0
        for acc in await self.store.list_accounts():
            tasks = await self.store.list_tasks(account_id=acc.id, day=tomorrow)
            pending_tasks = [t for t in tasks if t.status == TaskStatus.PENDING]
            for t in pending_tasks:
                # 只 submit — sign 由 tick_account 在时段开始前的 pre_sign 窗口触发
                await self._run_submit(acc, t)
                submitted += 1

    async def shutdown(self) -> None:
        self.scheduler.shutdown(wait=False)
        for c in self._clients.values():
            await c.close()
