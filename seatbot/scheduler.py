"""APScheduler wrapper that drives the reservation state machine."""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from seatbot.client import ChaoxingClient, ChaoxingError
from seatbot.config import Config
from seatbot.enc import EncGenerator
from seatbot.models import Account, Task, TaskStatus
from seatbot.planner import ReservationPlanner
from seatbot.store import StateStore
from seatbot.utils.timeutil import at_cst, now_cst, today_cst


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
        self._bootstrap_done_for: set[tuple[str, str]] = set()  # (account_id, day)

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
        for acc in await self.store.list_accounts():
            await self._bootstrap_for_account(acc, today)

    async def _bootstrap_for_account(self, acc: Account, day: date) -> None:
        if (acc.id, day.isoformat()) in self._bootstrap_done_for:
            return
        prev = await self.store.get_bootstrap_day(acc.id)
        existing = await self.store.list_tasks(account_id=acc.id, day=day)
        if existing:
            self._bootstrap_done_for.add((acc.id, day.isoformat()))
            await self._info(f"already has {len(existing)} tasks for {day}", acc.id)
            return
        if prev == day:
            self._bootstrap_done_for.add((acc.id, day.isoformat()))
            return

        planner = ReservationPlanner(acc, self.cfg.library.max_reserve_hours)
        try:
            tasks = planner.expand_for_day(day)
        except Exception as e:
            await self._error(f"planner failed: {e}", acc.id)
            return
        for t in tasks:
            await self.store.add_task(t)
        await self.store.set_bootstrap_day(acc.id, day)
        self._bootstrap_done_for.add((acc.id, day.isoformat()))
        await self._info(f"bootstrap {len(tasks)} tasks for {day}", acc.id)

    # ---------- per-account tick ----------
    async def tick_account(self, acc_id: str) -> None:
        acc_cfg = await self.store.get_account(acc_id)
        if not acc_cfg:
            return
        active = await self.store.find_active_task(acc_id)
        now = now_cst()
        if active:
            await self._maybe_relay(acc_cfg, active, now)
            return
        # no active task — find the next pending task that should be running
        today = today_cst()
        tasks = await self.store.list_tasks(account_id=acc_id, day=today)
        for t in tasks:
            if t.status not in (TaskStatus.PENDING, TaskStatus.READY, TaskStatus.FAILED):
                continue
            t_start = at_cst(t.day, t.start_time)
            t_end = at_cst(t.day, t.end_time)
            # currently inside the slot
            if t_start <= now < t_end:
                await self._run_submit_sign(acc_cfg, t)
                return
            # pre-sign window (within 20 min of start)
            if timedelta(0) <= (t_start - now) <= timedelta(minutes=20):
                await self._run_submit_sign(acc_cfg, t)
                return

    async def _maybe_relay(self, acc: Account, t: Task, now: datetime) -> None:
        t_end = at_cst(t.day, t.end_time)
        lead = t_end - timedelta(seconds=self.RELAY_LEAD_SECONDS)
        if now < lead:
            return
        await self._info(f"relay: leaving {t.chunk_key()}", acc.id)
        await self.store.update_task_status(t.id, TaskStatus.LEAVING)
        client = self._client_for(acc)
        if t.reserve_id:
            try:
                await client.leave(t.reserve_id)
                await self.store.log_action(acc.id, "leave", str(t.reserve_id), "", True)
            except Exception as e:
                await self._error(f"leave failed: {e}", acc.id)
        await self.store.update_task_status(t.id, TaskStatus.COMPLETE)

        # find next task
        today = today_cst()
        tasks = await self.store.list_tasks(account_id=acc.id, day=today)
        nxt = next(
            (x for x in tasks
             if x.start_time > t.start_time
             and x.status in (TaskStatus.PENDING, TaskStatus.FAILED)),
            None,
        )
        if nxt is None:
            await self._info("relay: no next task", acc.id)
            return
        await self._run_submit_sign(acc, nxt)

    async def _run_submit_sign(self, acc: Account, t: Task) -> None:
        """Try to book the seat for account `acc` for task `t`.

        v1.1: instead of computing `enc` via the (incomplete) JS exec and
        reusing it across a separate httpx call, we drive the actual
        Chaoxing UI in a headless Chromium via
        `ChaoxingClient.submit_in_browser()`. The browser runs the real
        fanyalogin flow, fills the form, clicks the 30-min cells, clicks
        "开始使用", intercepts the /submit response, and extracts the
        reserve_id. We then immediately sign in the same call chain.
        """
        await self.store.update_task_status(t.id, TaskStatus.SUBMITTING)
        client = self._client_for(acc)

        # login first so the in-browser call has the session cookies.
        # If login itself fails, we still try `submit_in_browser()` —
        # it will log in by itself if cookies are missing.
        if not client.cookies():
            try:
                await client.login(acc.phone, acc.password)
                await self._info(f"login ok ({len(client.cookies())} cookies)", acc.id)
            except ChaoxingError as e:
                await self._warn(f"login prefetch failed ({e}); trying in-browser login", acc.id)

        # submit via real browser interaction
        try:
            r = await client.submit_in_browser(
                phone=acc.phone,
                password=acc.password,
                room_id=self.cfg.library.room_id,
                seat_num=self.cfg.library.target_seat_num,
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
            f"{t.day} {t.start_time}-{t.end_time}",
            str(r.get("raw"))[:500],
            bool(r.get("success")),
            r.get("msg"),
        )

        if not r.get("success"):
            msg = r.get("msg") or "submit failed"
            await self._error(f"submit rejected: {msg}", acc.id)
            await self.store.update_task_status(t.id, TaskStatus.FAILED, last_error=msg)
            return

        reserve_id = r.get("reserve_id")
        if not reserve_id:
            await self._error("submit ok but no reserve_id", acc.id)
            await self.store.update_task_status(t.id, TaskStatus.FAILED, last_error="no reserve_id")
            return

        await self.store.update_task_status(t.id, TaskStatus.ACTIVE, reserve_id=reserve_id)
        await self._info(f"reserved #{reserve_id} {t.chunk_key()}", acc.id)

        # sign
        try:
            sr = await client.sign(reserve_id)
            await self.store.log_action(
                acc.id, "sign", str(reserve_id), str(sr)[:500],
                bool(sr.get("success")), str(sr.get("msg")),
            )
            if not sr.get("success"):
                await self._error(f"sign failed: {sr.get('msg')}", acc.id)
        except Exception as e:
            await self._error(f"sign error: {e}", acc.id)

        if not r.get("success"):
            msg = r.get("msg") or "submit failed"
            await self._error(f"submit rejected: {msg}", acc.id)
            await self.store.update_task_status(t.id, TaskStatus.FAILED, last_error=msg)
            return

        reserve_id = (r.get("data") or {}).get("seatReserve", {}).get("id")
        if not reserve_id:
            await self._error("submit ok but no reserve_id", acc.id)
            await self.store.update_task_status(t.id, TaskStatus.FAILED, last_error="no reserve_id")
            return

        await self.store.update_task_status(t.id, TaskStatus.ACTIVE, reserve_id=reserve_id)
        await self._info(f"reserved #{reserve_id} {t.chunk_key()}", acc.id)

        # sign
        try:
            sr = await client.sign(reserve_id)
            await self.store.log_action(
                acc.id, "sign", str(reserve_id), str(sr)[:500],
                bool(sr.get("success")), str(sr.get("msg")),
            )
            if not sr.get("success"):
                await self._error(f"sign failed: {sr.get('msg')}", acc.id)
        except Exception as e:
            await self._error(f"sign error: {e}", acc.id)

    # ---------- cron wiring ----------
    async def sync_jobs(self) -> None:
        """Re-register tick jobs from the current set of DB accounts.

        Called on startup and every 30s so accounts added via the Web UI
        pick up scheduling without a restart.
        """
        accounts = await self.store.list_accounts()
        wanted_ids = {a.id for a in accounts}
        existing_ids = {
            j.id.removeprefix("tick_")
            for j in self.scheduler.get_jobs()
            if j.id.startswith("tick_")
        }
        # drop tick jobs for accounts that no longer exist
        for stale in existing_ids - wanted_ids:
            try:
                self.scheduler.remove_job(f"tick_{stale}")
            except Exception:
                pass
        # add tick jobs for any new accounts
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
        # initial sync (accounts may be added via Web later)
        self.scheduler.add_job(
            self.sync_jobs,
            "interval", seconds=30,
            id="sync_jobs", replace_existing=True,
        )
        # 00:00:05 every day: bootstrap next day
        self.scheduler.add_job(
            self._new_day_bootstrap,
            CronTrigger(hour=0, minute=0, second=5, timezone="Asia/Shanghai"),
            id="new_day_bootstrap", replace_existing=True,
        )
        self.scheduler.start()

    async def _tick_account_with_bootstrap(self, acc_id: str) -> None:
        await self._bootstrap_for_account_if_needed(acc_id)
        await self.tick_account(acc_id)

    async def _bootstrap_for_account_if_needed(self, acc_id: str) -> None:
        acc_cfg = await self.store.get_account(acc_id)
        if not acc_cfg:
            return
        await self._bootstrap_for_account(acc_cfg, today_cst())

    async def _new_day_bootstrap(self) -> None:
        today = today_cst()
        for acc in await self.store.list_accounts():
            await self._bootstrap_for_account(acc, today)

    async def shutdown(self) -> None:
        self.scheduler.shutdown(wait=False)
        for c in self._clients.values():
            await c.close()
