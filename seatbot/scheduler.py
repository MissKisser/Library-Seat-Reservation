"""APScheduler wrapper (v2: per-task seat_num, cross-account/cross-seat relay)."""
from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import time as _time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from seatbot import settings as _settings
from seatbot.client import ChaoxingClient, ChaoxingError
from seatbot.config import Config
from seatbot.ha import NullHaRuntime
from seatbot.models import Account, Task, TaskStatus
from seatbot.planner import ReservationPlanner
from seatbot.store import StateStore
from seatbot.utils.timeutil import at_cst, now_cst, today_cst


def _overlap(s1, e1, s2, e2) -> bool:
    """Half-open interval overlap: [s, e) ⋂ [s', e') ≠ ∅"""
    return not (e1 <= s2 or s1 >= e2)


_RECOVERABLE_MSG_KEYS = ("no selectable cell", "page load", "timeout", "seed not found")


def _is_recoverable_page_error(msg: str | None) -> bool:
    """判定页面错误消息是否属于可通过锚点座位重试恢复的瞬态/页面结构错误。

    入参:
        msg: 服务端或客户端返回的错误字符串。

    返回值:
        包含任一可恢复关键字时返回 True，否则返回 False。
    """
    m = (msg or "").lower()
    return any(k in m for k in _RECOVERABLE_MSG_KEYS)


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
    RELAY_LEAD_SECONDS = 300  # 类级默认值；可在系统设置页调整

    def __init__(
        self,
        cfg: Config,
        store: StateStore,
    ):
        self.cfg = cfg
        self.store = store
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        self._clients: dict[str, ChaoxingClient] = {}
        self._bootstrap_done_for: set[tuple[str, str, str]] = set()  # (account_id, day, seat_num)
        self._fail_streak: dict[int, int] = {}  # task_id → 签到/签退连续失败次数
        self._supervise_seen: dict[int, str] = {}  # reserve_id → detected/resolved
        self.relay_lead_seconds: int = int(getattr(cfg.runtime, "relay_lead_seconds", 300))
        self.tick_interval_seconds: int = int(getattr(cfg.runtime, "tick_interval_seconds", 30))
        self.stagger_seconds: list[int] = list(getattr(cfg.runtime, "stagger_seconds", [0, 3]))
        self.anchor_retry_enabled: bool = bool(getattr(cfg.runtime, "anchor_retry_enabled", True))
        self.anchor_scan_limit: int = int(getattr(cfg.runtime, "anchor_scan_limit", 12))
        self.submit_strategy: str = str(getattr(cfg.runtime, "submit_strategy", "direct_first"))
        self.reconcile_interval_seconds: int = int(cfg.runtime.reconcile_interval_seconds)
        self._reconcile_last_at: datetime | None = None
        self._reconcile_running: bool = False
        self._reconcile_flagged: set[int] = set()  # 已告警未恢复的任务 id
        # 当日补约节拍器状态：间隔秒与上次执行时刻（间隔热读，重注册免）
        self.today_backfill_interval_seconds: int = 900
        self._today_backfill_last_at: datetime | None = None
        self._today_backfill_running: bool = False
        # 14:00 批量预约两个入口 (cron misfire 补跑 / 启动补跑) 共用的互斥闸，
        # 防止同批 PENDING 任务被两条路径并发提交成双份真实预约
        self._bootstrap_gate = asyncio.Lock()
        self._relogin_cooldown: dict[str, float] = {}  # account_id -> monotonic timestamp
        self._relogin_fail_count: dict[str, int] = {}  # account_id -> consecutive fail count
        # HA 闸门：默认 NullHaRuntime（can_act 永真），主备模式下换成 HaRuntime
        self.ha = NullHaRuntime()
        self._ha_notify_at: dict[str, float] = {}  # action -> last notify monotonic

    async def load_runtime_settings(self) -> dict[str, object]:
        try:
            rows = await self.store.get_settings_map()
        except Exception:
            rows = {}
        eff = _settings.effective(rows, self.cfg)
        try:
            self.cfg.library.max_reserve_hours = float(eff.get("max_reserve_hours", self.cfg.library.max_reserve_hours))
            self.cfg.library.daily_reserve_hours_limit = float(eff.get("daily_reserve_hours_limit", self.cfg.library.daily_reserve_hours_limit))
        except Exception:
            pass
        try:
            self.cfg.runtime.notify_webhook = str(eff.get("notify_webhook", self.cfg.runtime.notify_webhook))
        except Exception:
            pass
        self.relay_lead_seconds = int(eff.get("relay_lead_seconds", self.relay_lead_seconds))
        self.tick_interval_seconds = int(eff.get("tick_interval_seconds", self.tick_interval_seconds))
        self.stagger_seconds = list(eff.get("stagger_seconds", self.stagger_seconds))  # type: ignore[arg-type]
        self.anchor_retry_enabled = bool(eff.get("anchor_retry_enabled", self.anchor_retry_enabled))
        self.anchor_scan_limit = int(eff.get("anchor_scan_limit", self.anchor_scan_limit))
        self.submit_strategy = str(eff.get("submit_strategy", self.submit_strategy))
        self.reconcile_interval_seconds = int(eff.get("reconcile_interval_seconds", self.reconcile_interval_seconds))
        try:
            self._reschedule_tick_interval()
        except Exception:
            pass
        return eff

    def _reschedule_tick_interval(self) -> None:
        try:
            job = self.scheduler.get_job("sync_jobs")
            if job is None:
                return
            cur = None
            try:
                cur = int(job.trigger.interval.total_seconds())  # type: ignore[attr-defined]
            except Exception:
                pass
            if cur is not None and cur == int(self.tick_interval_seconds):
                return
            self.scheduler.reschedule_job("sync_jobs", trigger="interval", seconds=int(self.tick_interval_seconds))
        except Exception:
            pass

    def _ha_blocked(self, action: str) -> bool:
        """闸门：返回 True 表示本次真实动作必须被拦截。

        拦截时：限频（每 10 分钟最多一条通知）写一条 warn 日志与通知，
        不写任务状态，避免污染审计线索。
        """
        try:
            if self.ha.can_act():
                return False
        except Exception:
            return False
        now = _time.monotonic()
        last = self._ha_notify_at.get(action, 0.0)
        if now - last >= 600:
            self._ha_notify_at[action] = now
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(self._notify_ha_blocked(action))
                else:
                    # 同步回退：直接落日志
                    print(f"[INFO] scheduler - ha-blocked {action}")
            except Exception:
                pass
        return True

    async def _notify_ha_blocked(self, action: str) -> None:
        try:
            await self._notify(
                f"调度动作被拦截（{action}）",
                "当前实例不在可调度状态（HA 闸门）；待命备用或暂停主力不会执行真实操作。",
                level="warn",
            )
        except Exception:
            pass

    def _client_for(self, acc: Account) -> ChaoxingClient:
        if acc.id not in self._clients:
            self._clients[acc.id] = ChaoxingClient()
        return self._clients[acc.id]

    async def client_ready(self, acc: Account) -> ChaoxingClient:
        """取账号 client；jar 为空时优先用持久化 cookie 恢复会话（不做网络请求）。

        会话是否真的有效由后续 API 调用判定——失效路径由调用方
        reset_session + login_and_persist 自愈，避免每次都走无头浏览器登录。
        """
        client = self._client_for(acc)
        if not client.cookies():
            persisted = await self.store.load_account_cookies(acc.id)
            if persisted and client.set_cookies(persisted):
                await self._info(f"会话恢复（{len(persisted)} cookies，来自本地持久化）", acc.id)
        return client

    async def login_and_persist(self, acc: Account, client: ChaoxingClient, label: str = "登录") -> bool:
        """浏览器登录一次并把 cookie 写入持久化存储；成功返回 True。"""
        try:
            await client.login(acc.phone, acc.password)
        except ChaoxingError as e:
            await self._warn(f"{label}预登录失败（{e}）", acc.id)
            return False
        await self.store.save_account_cookies(acc.id, client.cookies())
        await self._info(f"{label}成功（{len(client.cookies())} cookies，已持久化）", acc.id)
        return True

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

    async def _notify(self, title: str, body: str = "", level: str = "warn") -> None:
        """用户需要看到的事件: 保存到看板并按需推送到外部通知。"""
        await self.store.add_notification(title, body, level)
        print(f"[NOTIFY] {title} {body}")
        url = (self.cfg.runtime.notify_webhook or "").strip()
        if not url:
            return
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as hc:
                await hc.post(url, json={"title": title, "content": body})
        except Exception as e:
            await self._warn(f"通知 webhook 外推失败: {e}")

    async def _track_signleave_failure(self, t: Task, action: str, reason: str) -> None:
        """签到/签退失败连击计数; 连续 3 次及此后每 10 次推送一次告警。"""
        n = self._fail_streak.get(t.id, 0) + 1
        self._fail_streak[t.id] = n
        if n == 3 or n % 10 == 0:
            await self._notify(
                f"{action}连续失败 {n} 次",
                f"账号={t.account_id} 座位={t.seat_num} {t.day} "
                f"{t.start_time}-{t.end_time}: {reason}",
                level="error",
            )

    def _clear_fail_streak(self, task_id: int | None) -> None:
        if task_id is not None:
            self._fail_streak.pop(task_id, None)

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
                await self._info(f"已存在 {len(existing)} 条任务，{day} 座位={seat_num}，跳过重复生成", acc.id)
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
                open_time=self.cfg.library.open_time,
                close_time=self.cfg.library.close_time,
            )
            try:
                tasks = planner.expand_for_day(day)
            except Exception as e:
                await self._error(f"排程计划生成失败: {e}", acc.id)
                return
            for t in tasks:
                if t.seat_num != seat_num:
                    continue
                # ★ 跳过与 user_reserved 重叠的 task
                if any(_overlap(t.start_time, t.end_time, rs, re_)
                       for rs, re_ in reserved_slots):
                    await self._info(
                        f"跳过用户硬预约时段 座位={seat_num} {t.start_time.strftime('%H:%M')}-{t.end_time.strftime('%H:%M')}",
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
                        f"跳过重复任务 座位={seat_num} {start_t.strftime('%H:%M')}（E4 防护）",
                        acc.id,
                    )
                    continue
                await self.store.add_task(t)
            self._bootstrap_done_for.add(key)
            await self._info(f"已为 {day} 座位={seat_num} 生成任务", acc.id)
        # 更新 bootstrap_day 一次 (一个账号每天只一次)
        await self.store.set_bootstrap_day(acc.id, day)

    # ---------- per-account tick ----------
    async def tick_account(self, acc_id: str) -> None:
        """每分钟: 只处理**今天**的在途任务 (active/signed/leaving)。

        v0.6 重写要点:
          - 按 day=today 遍历, 不再用 find_active_task — 旧实现在 14:00 批量
            提交后会抓到"明日最后一条 ACTIVE 任务"并 early-return, 导致当天
            14:00 后的所有 sign/leave 被饿死。
          - 移除"单日段数上限"闸门: tick 已不再 submit, 该闸门只会拦截进行中
            任务的 sign/leave (limit=1 时自锁); 段数上限由 planner 生成端保证。
          - sign 幂等: 成功 (或窗口已过) 置 SIGNED, 不再每分钟重复打 sign API。
          - PENDING/FAILED 不在此 submit: 今天的交给 web quick-reserve,
            明天的交给 _afternoon_bootstrap (14:00)。
        """
        acc_cfg = await self.store.get_account(acc_id)
        if not acc_cfg:
            return
        now = now_cst()
        today = today_cst()
        # 排干守卫: 禁用中账号仅在有在途任务时继续服务签到/签退, 排干后空转。
        # 禁用账号的 tick job 不被 sync_jobs 摘除 (见 sync_jobs), 靠此处守卫零动作。
        if acc_cfg.status == "inactive":
            todays = await self.store.list_tasks(account_id=acc_id, day=today)
            if not any(
                t.status in (TaskStatus.ACTIVE, TaskStatus.SIGNED, TaskStatus.LEAVING)
                for t in todays
            ):
                return
        has_live_seat = False
        for t in await self.store.list_tasks(account_id=acc_id, day=today):
            if t.status not in (TaskStatus.ACTIVE, TaskStatus.SIGNED, TaskStatus.LEAVING):
                continue
            t_start = at_cst(t.day, t.start_time)
            t_end = at_cst(t.day, t.end_time)
            # 1. 到签退时机 (end 前 relay_lead_seconds=5min) → 签退
            if now >= t_end - timedelta(seconds=self.relay_lead_seconds):
                await self._maybe_relay(t, now)
                continue
            # 2. 时段进行中且未签 → 签到 (成功后置 SIGNED)
            if t.status == TaskStatus.ACTIVE and t_start <= now < t_end:
                await self._run_sign(acc_cfg, t)
                has_live_seat = True
                continue
            # 3. SIGNED 在时段内 = 正持有座位; LEAVING 未到点 → 不动
            if t.status == TaskStatus.SIGNED and t_start <= now < t_end:
                has_live_seat = True
        # 4. 持有座位期间轮询监督状态, 被监督则立即自动落座
        if has_live_seat:
            await self._check_supervision(acc_cfg)

    async def _check_supervision(self, acc: Account) -> None:
        """检测当前账号"被监督中"的预约并自动重新签到解除。

        上游无独立"落座确认"端点: reservelist 出现 status=5 即被监督,
        持该预约号调 sign() 与官方扫码落座等效; 20 分钟窗口内失败可
        重试, 因此跟随 tick 每轮执行。仅在账号实际持有座位时被
        tick_account 调用; 会话 cookie 为空时跳过 (登录由签到/签退
        动作路径负责, 避免每分钟触发浏览器登录风暴)。

        每个监督回合: 首次检测写日志并发 warn 通知, 解除成功写日志、
        发 info 通知并将匹配到的本地 ACTIVE 任务置 SIGNED; 解除失败保持下轮重试。
        """
        if self._ha_blocked("supervision"):
            return
        client = await self.client_ready(acc)
        if not client.cookies():
            return
        try:
            supervised = await client.supervised_reservations()
        except ChaoxingError as e:
            if "未登录" not in str(e):
                await self._warn(f"监督检测失败: {e}", acc.id)
                return
            await self._warn("监督检测: 会话过期 → 重置会话并重登重试", acc.id)
            client.reset_session()
            if not await self.login_and_persist(acc, client, "监督检测重登"):
                return
            try:
                supervised = await client.supervised_reservations()
            except Exception as e2:
                await self._warn(f"监督检测失败(重登后): {e2}", acc.id)
                return
        except Exception as e:
            await self._warn(f"监督检测失败: {e}", acc.id)
            return
        if not supervised:
            self._supervise_seen.clear()
            return
        today = today_cst()
        local_tasks = {
            t.reserve_id: t
            for t in await self.store.list_tasks(account_id=acc.id, day=today)
            if t.reserve_id
        }
        for rec in supervised:
            rid = rec.get("id")
            if not rid:
                continue
            seat = str(rec.get("seatNum") or "?")
            if self._supervise_seen.get(rid) != "detected":
                self._supervise_seen[rid] = "detected"
                await self._warn(
                    f"检测到监督: 座位={seat} 预约号#{rid}，"
                    f"20 分钟窗口内自动重新签到", acc.id)
                await self._notify(
                    "检测到监督：正在自动落座",
                    f"账号={acc.id} 座位={seat} 预约号#{rid}，"
                    f"20 分钟窗口内自动重新签到",
                    level="warn",
                )
            try:
                sr = await self._act_with_relogin(
                    client, acc, client.sign, rid, "supervise-sign")
            except Exception as e:
                await self._error(
                    f"监督落座异常: 预约号#{rid} {type(e).__name__}: {e}", acc.id)
                continue
            await self.store.log_action(
                acc.id, "supervise_sign", str(rid), str(sr)[:500],
                bool(sr.get("success")), str(sr.get("msg")),
            )
            if sr.get("success"):
                self._supervise_seen[rid] = "resolved"
                await self._info(
                    f"监督已解除: 座位={seat} 预约号#{rid} 自动落座成功", acc.id)
                await self.store.add_notification(
                    "监督已解除",
                    f"账号={acc.id} 座位={seat} 预约号#{rid} 自动落座成功",
                    level="info",
                )
                t = local_tasks.get(rid)
                if t is not None and t.status == TaskStatus.ACTIVE:
                    await self.store.update_task_status(
                        t.id, TaskStatus.SIGNED, last_error="")
            else:
                await self._warn(
                    f"监督落座被拒（{sr.get('msg')}）: 预约号#{rid}，"
                    f"下个周期重试", acc.id)
        live = {r.get("id") for r in supervised}
        for rid in [k for k in self._supervise_seen if k not in live]:
            del self._supervise_seen[rid]

    async def peek_next_relay(self) -> NextRelay | None:
        now = now_cst()
        today = today_cst()
        best: tuple[datetime, NextRelay] | None = None
        for acc in await self.store.list_accounts():
            for t in await self.store.list_tasks(account_id=acc.id, day=today):
                t_start = at_cst(t.day, t.start_time)
                t_end = at_cst(t.day, t.end_time)
                if t.status in (TaskStatus.ACTIVE, TaskStatus.SIGNED, TaskStatus.SUBMITTING, TaskStatus.LEAVING):
                    fire_at = t_end - timedelta(seconds=self.relay_lead_seconds)
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
        lead = t_end - timedelta(seconds=self.relay_lead_seconds)
        if now < lead:
            return
        acc = await self.store.get_account(t.account_id)
        if not acc:
            return
        await self._info(f"接力签退: {t.chunk_key()}", acc.id)
        await self.store.update_task_status(t.id, TaskStatus.LEAVING)
        # leave 当前段
        await self._run_leave(acc, t)
        # ★ v0.5+: 不再 submit 下一段 — 由 _afternoon_bootstrap 14:00 统一处理
        nxt = await self.store.find_next_task_after(
            t.day, t.end_time,
            statuses=("pending", "ready", "failed"),
        )
        if nxt is None:
            await self._info("接力: 无下一段任务（不提交，等 14:00 批量预约）", acc.id)
            return
        await self._info(
            f"接力: 签退完成，下一段 任务={nxt.id}（{nxt.account_id} {nxt.seat_num} {nxt.day} "
            f"{nxt.start_time.strftime('%H:%M')}-{nxt.end_time.strftime('%H:%M')}）状态={nxt.status.value} "
            f"预约号={nxt.reserve_id or '无'}（不提交，等 14:00 批量预约）",
            acc.id,
        )

    async def _pick_anchor_seat(self, client: ChaoxingClient, exclude: str) -> str | None:
        """挑一个当前空闲且真实可约的座位作页面表单锚点。

        座位页仅在座位当前未被使用时渲染格子网格; 目标座位正被使用时
        页面是详情面板, 无格可点。锚点只决定开哪个座位页, 真实座位/日期/
        时段由网络层改写覆盖。有未来预约不影响页面此刻呈格子, 故判据是
        "当前不在任何占用窗口内"; 要求当天有过占用记录以排除特殊不可
        约座位。候选顺序: 其余目标座位 → 相邻号 → 间隔扫描号。

        Returns: 座位号; 无合格候选时 None。
        """
        now_hm = now_cst().strftime("%H:%M")
        today = today_cst().isoformat()
        n = int(exclude) if exclude.isdigit() else 0
        adjacent = [f"{n + d:03d}" for d in (-2, -1, 1, 2) if 0 < n + d < 1000]
        candidates = [s.seat_num for s in await self.store.list_target_seats()
                      if s.seat_num != exclude]
        candidates += adjacent + [f"{i:03d}" for i in range(1, 102, 10)]
        tried: set[str] = set()
        for seat in candidates:
            if seat == exclude or seat in tried or len(tried) >= self.anchor_scan_limit:
                continue
            tried.add(seat)
            try:
                used = await client.get_used_times(
                    self.cfg.library.room_id, seat, today)
            except Exception:
                continue
            if used and not any(s <= now_hm < e for s, e in used):
                return seat
        return None

    async def run_submit(self, acc: Account, t: Task) -> None:
        """公开提交入口：CLI 手动预约等外部调用方走同一通道分流。"""
        await self._run_submit(acc, t)

    async def _run_submit(self, acc: Account, t: Task) -> None:
        """提交预约 (不签到)。

        调用时机:
          - 14:00 批量预约 ( _afternoon_bootstrap ) 时段离开始还远,不能签到
          - _maybe_relay 接力:leave 完上一段后,预约下一段 (下一段时段还未开始)
          - tick_account pre_sign 窗口:如果 task 还没有 reserve_id (14:00 未预约成功)

        submit 成功后 task.status = ACTIVE, store 存 reserve_id。
        sign 由 _run_sign 在时段开始后再调用。
        """
        if self._ha_blocked("submit"):
            return
        await self.store.update_task_status(t.id, TaskStatus.SUBMITTING)
        client = await self.client_ready(acc)

        if not client.cookies():
            await self.login_and_persist(acc, client, "登录")

        try:
            strat = self.submit_strategy
            if strat == "page_rewrite_only":
                r = await client.submit_via_page_rewrite(
                    phone=acc.phone,
                    password=acc.password,
                    room_id=self.cfg.library.room_id,
                    seat_num=t.seat_num,
                    day=t.day.isoformat(),
                    start_time=t.start_time.strftime("%H:%M"),
                    end_time=t.end_time.strftime("%H:%M"),
                )
                if self.anchor_retry_enabled and not r.get("success") and _is_recoverable_page_error(r.get("msg")):
                    anchor = await self._pick_anchor_seat(client, t.seat_num)
                    if anchor:
                        await self._warn(
                            f"座位 {t.seat_num} 页面不可用（{r.get('msg')}）, 改用锚点座位 {anchor} 重试",
                            acc.id,
                        )
                        r = await client.submit_via_page_rewrite(
                            phone=acc.phone, password=acc.password,
                            room_id=self.cfg.library.room_id, seat_num=t.seat_num,
                            day=t.day.isoformat(),
                            start_time=t.start_time.strftime("%H:%M"),
                            end_time=t.end_time.strftime("%H:%M"),
                            anchor_seat=anchor,
                        )
            elif strat == "direct_only":
                r = await client.submit_direct(
                    phone=acc.phone, password=acc.password,
                    room_id=self.cfg.library.room_id, seat_num=t.seat_num,
                    day=t.day.isoformat(),
                    start_time=t.start_time.strftime("%H:%M"),
                    end_time=t.end_time.strftime("%H:%M"),
                )
            elif strat == "page_rewrite_first":
                r = await client.submit_via_page_rewrite(
                    phone=acc.phone, password=acc.password,
                    room_id=self.cfg.library.room_id, seat_num=t.seat_num,
                    day=t.day.isoformat(),
                    start_time=t.start_time.strftime("%H:%M"),
                    end_time=t.end_time.strftime("%H:%M"),
                )
                if self.anchor_retry_enabled and not r.get("success") and _is_recoverable_page_error(r.get("msg")):
                    anchor = await self._pick_anchor_seat(client, t.seat_num)
                    if anchor:
                        await self._warn(
                            f"座位 {t.seat_num} 页面不可用（{r.get('msg')}）, 改用锚点座位 {anchor} 重试",
                            acc.id,
                        )
                        r = await client.submit_via_page_rewrite(
                            phone=acc.phone, password=acc.password,
                            room_id=self.cfg.library.room_id, seat_num=t.seat_num,
                            day=t.day.isoformat(),
                            start_time=t.start_time.strftime("%H:%M"),
                            end_time=t.end_time.strftime("%H:%M"),
                            anchor_seat=anchor,
                        )
                if not r.get("success"):
                    await self._warn(f"模拟点击未成（{r.get('msg')}），回退直连通道", acc.id)
                    r2 = await client.submit_direct(
                        phone=acc.phone, password=acc.password,
                        room_id=self.cfg.library.room_id, seat_num=t.seat_num,
                        day=t.day.isoformat(),
                        start_time=t.start_time.strftime("%H:%M"),
                        end_time=t.end_time.strftime("%H:%M"),
                    )
                    if r2.get("success"):
                        r = r2
            else:  # direct_first (default, 全日期统一优先直连提交)
                async def _do_submit_direct():
                    return await client.submit_direct(
                        phone=acc.phone,
                        password=acc.password,
                        room_id=self.cfg.library.room_id,
                        seat_num=t.seat_num,
                        day=t.day.isoformat(),
                        start_time=t.start_time.strftime("%H:%M"),
                        end_time=t.end_time.strftime("%H:%M"),
                    )

                r = await _do_submit_direct()
                r = await self._handle_unauthorized_retry(
                    acc, client, "直连提交", r, _do_submit_direct
                )
                if self.anchor_retry_enabled and not r.get("success") and "seed not found" in str(r.get("msg") or ""):
                    anchor = await self._pick_anchor_seat(client, t.seat_num)
                    if anchor:
                        await self._warn(
                            f"座位 {t.seat_num} 自身扫码页无种子, 改用锚点 {anchor} 取种子直连重试",
                            acc.id,
                        )
                        r_anchor = await client.submit_direct(
                            phone=acc.phone,
                            password=acc.password,
                            room_id=self.cfg.library.room_id,
                            seat_num=t.seat_num,
                            day=t.day.isoformat(),
                            start_time=t.start_time.strftime("%H:%M"),
                            end_time=t.end_time.strftime("%H:%M"),
                            anchor_seat=anchor,
                        )
                        if r_anchor.get("success"):
                            r = r_anchor
                if not r.get("success"):
                    await self._warn(
                        f"直连提交未成（{r.get('msg')}），改走页面改写通道",
                        acc.id,
                    )
                    r = await client.submit_via_page_rewrite(
                        phone=acc.phone,
                        password=acc.password,
                        room_id=self.cfg.library.room_id,
                        seat_num=t.seat_num,
                        day=t.day.isoformat(),
                        start_time=t.start_time.strftime("%H:%M"),
                        end_time=t.end_time.strftime("%H:%M"),
                    )
                    if self.anchor_retry_enabled and not r.get("success") and _is_recoverable_page_error(r.get("msg")):
                        anchor = await self._pick_anchor_seat(client, t.seat_num)
                        if anchor:
                            await self._warn(
                                f"座位 {t.seat_num} 页面不可用（{r.get('msg')}）, 改用锚点座位 {anchor} 重试",
                                acc.id,
                            )
                            r = await client.submit_via_page_rewrite(
                                phone=acc.phone,
                                password=acc.password,
                                room_id=self.cfg.library.room_id,
                                seat_num=t.seat_num,
                                day=t.day.isoformat(),
                                start_time=t.start_time.strftime("%H:%M"),
                                end_time=t.end_time.strftime("%H:%M"),
                                anchor_seat=anchor,
                            )
        except Exception as e:
            await self._error(f"提交异常: {e}", acc.id)
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
            await self._error(f"提交被拒: {msg}（座位={t.seat_num}）", acc.id)
            await self.store.update_task_status(t.id, TaskStatus.FAILED, last_error=msg)
            return

        reserve_id = r.get("reserve_id")
        if not reserve_id:
            await self._error("提交成功但未返回预约号", acc.id)
            await self.store.update_task_status(t.id, TaskStatus.FAILED, last_error="未返回预约号")
            return

        await self.store.update_task_status(t.id, TaskStatus.ACTIVE, reserve_id=reserve_id, last_error="")
        await self._info(f"预约成功 #{reserve_id} 座位={t.seat_num} {t.chunk_key()}", acc.id)

        # ★ 事后核验 (仅跨天): 不信提交回执, 用服务端占用状态复核。
        # 核验不一致时保留 ACTIVE (预约号是服务端发的, 大概率真实存在,
        # 标 FAILED 反而会留下无人管理的真预约), 只打 ERROR 进人工视野。
        # 提交后服务端占用查询存在落库延迟 (实测 14:00 高峰 3/12 撞上),
        # 首查未覆盖时等待后重查一次再定论。
        if t.day > today_cst():
            s, e = t.start_time.strftime("%H:%M"), t.end_time.strftime("%H:%M")
            for attempt in (1, 2):
                try:
                    used = await client.get_used_times(
                        self.cfg.library.room_id, t.seat_num, t.day.isoformat(),
                    )
                except Exception as ex:
                    await self._warn(f"占用核验异常: {ex}", acc.id)
                    break
                if any(us < e and ue > s for us, ue in used):
                    await self._info(
                        f"占用核验一致: {t.day} 座位={t.seat_num} {s}-{e} 预约号#{reserve_id}",
                        acc.id,
                    )
                    break
                if attempt == 1:
                    await asyncio.sleep(5)
                else:
                    await self._error(
                        f"占用核验未反映（重查后）: 预约号#{reserve_id} {t.day} 座位={t.seat_num} "
                        f"{s}-{e}（服务端 used={used}）——预约号已受理，大概率真实，"
                        f"保持进行中待实况核对复核",
                        acc.id,
                    )

    async def _handle_unauthorized_retry(
        self,
        acc: Account,
        client: ChaoxingClient,
        label: str,
        result: dict,
        retry_fn,
    ) -> dict:
        """处理未登录结果的重登重试与冷却保护。

        入参:
            acc: 目标账号实例。
            client: 超星客户端实例。
            label: 动作日志标识。
            result: 初次执行返回的字典结果。
            retry_fn: 无参异步调用，重登成功后重新执行原操作。

        返回值:
            重试后的结果字典，或跳过/失败时的原/新结果字典。
        """
        msg = str(result.get("msg") or "")
        if not result.get("success") and "未登录" in msg:
            now_m = _time.monotonic()
            cd = self._relogin_cooldown.get(acc.id, 0.0)
            if now_m < cd:
                await self._warn(
                    f"{label}: 会话过期但在重登冷却期内（剩余 {int(cd - now_m)}s），跳过浏览器重登",
                    acc.id,
                )
                return result

            await self._warn(f"{label}: 会话过期（未登录）→ 重置会话并重登重试", acc.id)
            client.reset_session()
            login_ok = await self.login_and_persist(acc, client, f"{label}重登")
            if login_ok:
                new_result = await retry_fn()
                new_msg = str(new_result.get("msg") or "")
                if new_result.get("success") or "未登录" not in new_msg:
                    self._relogin_fail_count[acc.id] = 0
                    self._relogin_cooldown.pop(acc.id, None)
                    return new_result
                result = new_result

            fails = self._relogin_fail_count.get(acc.id, 0) + 1
            self._relogin_fail_count[acc.id] = fails
            if fails >= 2:
                self._relogin_cooldown[acc.id] = _time.monotonic() + 300.0
                await self._warn(
                    f"{label}: 账号 {acc.id} 连续 {fails} 次重登后仍未生效，进入 5 分钟重登冷却",
                    acc.id,
                )
        return result

    async def _act_with_relogin(
        self, client: ChaoxingClient, acc: Account, fn, reserve_id: int, label: str,
    ) -> dict:
        """执行 sign/leave; 服务端报未登录时清 cookie 重登并重试一次，防频繁重登死循环。"""
        sr = await fn(reserve_id)
        return await self._handle_unauthorized_retry(
            acc, client, label, sr, lambda: fn(reserve_id)
        )

    async def _run_sign(self, acc: Account, t: Task) -> None:
        """签到 (幂等: 成功或签到窗口已过 → SIGNED, 终止每分钟重试)。"""
        if self._ha_blocked("sign"):
            return
        if not t.reserve_id:
            await self._warn(f"跳过签到: 无预约号 座位={t.seat_num} {t.chunk_key()}", acc.id)
            return
        client = await self.client_ready(acc)
        # ★ lazy login — jar 与持久化都为空才走浏览器登录 (陈旧 cookie 由 _act_with_relogin 自愈)
        if not client.cookies():
            await self.login_and_persist(acc, client, "签到登录")
        try:
            sr = await self._act_with_relogin(client, acc, client.sign, t.reserve_id, "sign")
        except Exception as e:
            await self._error(f"签到异常: {e}", acc.id)
            await self.store.update_task_status(t.id, t.status, last_error=f"签到异常 {type(e).__name__}: {e}")
            await self._track_signleave_failure(t, "签到", f"异常 {type(e).__name__}: {e}")
            return
        await self.store.log_action(
            acc.id, "sign", str(t.reserve_id), str(sr)[:500],
            bool(sr.get("success")), str(sr.get("msg")),
        )
        if sr.get("success"):
            await self.store.update_task_status(t.id, TaskStatus.SIGNED, last_error="")
            self._clear_fail_streak(t.id)
            await self._info(f"签到成功 预约号#{t.reserve_id} 座位={t.seat_num}", acc.id)
            await self.store.add_notification(
                "签到成功",
                f"账号={acc.id} 座位={t.seat_num} 预约号#{t.reserve_id}",
                level="info",
            )
            return
        msg = str(sr.get("msg") or "")
        if "不在签到时间" in msg:
            # 消息无法区分"未到签到时间"(任务时间早于预约真实开始)与
            # "窗口已关闭"。开始后 60 分钟内保持重试以自愈时间偏差,
            # 超过则预约必然已生效或失效, 置 SIGNED 终止无效调用。
            deadline = at_cst(t.day, t.start_time) + timedelta(minutes=60)
            if now_cst() < deadline:
                await self._warn(f"签到被拒（{msg}）；窗口状态不明，保持重试", acc.id)
                await self.store.update_task_status(t.id, t.status, last_error=msg)
                await self._track_signleave_failure(t, "签到", f"窗口状态不明（{msg}）")
                return
            await self.store.update_task_status(t.id, TaskStatus.SIGNED, last_error="")
            self._clear_fail_streak(t.id)
            await self._warn(f"签到窗口已关闭（{msg}）；标记已签到，停止重试", acc.id)
            return
        if "不存在" in msg:
            # 预约已在服务端消失 (被取消/退座), 永远签不上 → 终态, 停止空转
            await self.store.update_task_status(
                t.id, TaskStatus.FAILED,
                last_error=f"签到: 服务端预约已不存在（{msg}）",
            )
            self._clear_fail_streak(t.id)
            await self._warn(f"签到终止: 预约在服务端已不存在（{msg}），任务标失败", acc.id)
            await self._notify(
                "签到终止：预约在服务端已不存在",
                f"账号={acc.id} 座位={t.seat_num} {t.day} "
                f"{t.start_time}-{t.end_time} 已标失败",
                level="error",
            )
            return
        await self._error(f"签到失败: {msg}（保持进行中，下个周期重试）", acc.id)
        # ★ 持久化失败原因：覆盖图需据此标红（否则 active 仍被视作成功）
        await self.store.update_task_status(t.id, t.status, last_error=msg)
        await self._track_signleave_failure(t, "签到", msg)

    async def _run_leave(self, acc: Account, t: Task) -> None:
        """签退 (不 submit/sign)。失败保留在途状态, 由下次 tick 重试。

        真正的签退端点是 /signback (退座, 时段进行中任意时刻可用), 优先调用;
        /leave 是"暂离", 硬性要求剩余 ≥20min, 仅在剩余充足时作为回退通道 —
        临近结束回退暂离必然失败, 且其"剩余不足"消息会命中幂等收尾, 掐断重试。
        """
        if self._ha_blocked("leave"):
            return
        if not t.reserve_id:
            await self._warn(f"跳过签退: 无预约号 座位={t.seat_num} {t.chunk_key()}", acc.id)
            # ★ 从未预约成功的任务不应伪装 COMPLETE (虚假完成态会误导审计)
            await self.store.update_task_status(
                t.id, TaskStatus.FAILED, last_error="签退: 无预约号",
            )
            await self._notify(
                "签退失败：任务无预约号",
                f"账号={acc.id} 座位={t.seat_num} {t.day} "
                f"{t.start_time}-{t.end_time} 已标失败",
                level="error",
            )
            return
        client = await self.client_ready(acc)
        # ★ lazy login — _run_leave 是独立调用路径
        if not client.cookies():
            await self.login_and_persist(acc, client, "签退登录")
        try:
            sr = await self._act_with_relogin(client, acc, client.signback, t.reserve_id, "signback")
        except Exception as e:
            await self._error(f"签退(signback)异常: {e}", acc.id)
            sr = {}
        if not sr.get("success"):
            sb_msg = str(sr.get("msg") or "")
            # signback 报终态特征 (预约已不在/已签退/已结束/已取消/剩余不足暂离)
            # = 服务端预约已终结, 直接收尾; 不能带终态消息去走临近结束守卫或
            # 暂离回退 (会把可终结的死号任务永远留在重试循环里)
            sb_terminal = any(
                k in sb_msg for k in (
                    "不存在", "已签退", "已结束", "已取消", "剩余时长小于暂离时长",
                )
            )
            if sb_terminal:
                await self._info(f"签退幂等收尾（{sb_msg}）→ 已完成", acc.id)
                await self.store.log_action(
                    acc.id, "signback", str(t.reserve_id), str(sr)[:500], False, sb_msg,
                )
                await self.store.update_task_status(
                    t.id, TaskStatus.COMPLETE,
                    last_error=f"服务端预约已终结, 幂等收尾（{sb_msg}）")
                self._clear_fail_streak(t.id)
                await self.store.add_notification(
                    "签退完成（预约已收尾）",
                    f"账号={acc.id} 座位={t.seat_num} 预约号#{t.reserve_id}（{sb_msg}）",
                    level="info",
                )
                return
            # 暂离要求剩余 ≥20min; 剩余不足时不回退, 保持 ACTIVE 下个 tick 重试 signback
            t_end = at_cst(t.day, t.end_time)
            if t_end - now_cst() < timedelta(minutes=20):
                await self._warn(f"签退未成功（{sb_msg}），临近结束不走暂离，下个周期重试", acc.id)
                await self.store.update_task_status(t.id, TaskStatus.ACTIVE)
                await self._track_signleave_failure(t, "签退", sb_msg)
                return
            await self._warn(f"签退未成功（{sb_msg}），回退暂离通道", acc.id)
            try:
                sr = await self._act_with_relogin(client, acc, client.leave, t.reserve_id, "leave")
            except Exception as e:
                await self._error(f"暂离异常: {e}", acc.id)
                await self.store.update_task_status(t.id, TaskStatus.ACTIVE)
                await self._track_signleave_failure(t, "签退", f"暂离通道异常 {type(e).__name__}: {e}")
                return
        await self.store.log_action(
            acc.id, "signback", str(t.reserve_id), str(sr)[:500],
            bool(sr.get("success")), str(sr.get("msg")),
        )
        msg = str(sr.get("msg") or "")
        if sr.get("success"):
            await self.store.update_task_status(t.id, TaskStatus.COMPLETE, last_error="")
            self._clear_fail_streak(t.id)
            await self.store.add_notification(
                "签退成功",
                f"账号={acc.id} 座位={t.seat_num} 预约号#{t.reserve_id}",
                level="info",
            )
            return
        # 幂等收尾: 预约已在服务端终结, 继续重试无意义 → COMPLETE 停止循环。
        # ("剩余时长小于暂离时长" = 离结束不足 leaveDuration, 预约将自然到期)
        idempotent = any(
            k in msg for k in ("已签退", "已结束", "已取消", "不存在", "剩余时长小于暂离时长")
        )
        if idempotent:
            await self._info(f"签退幂等收尾（{msg}）→ 已完成", acc.id)
            await self.store.update_task_status(t.id, TaskStatus.COMPLETE, last_error="")
            self._clear_fail_streak(t.id)
            await self.store.add_notification(
                "签退完成（预约已收尾）",
                f"账号={acc.id} 座位={t.seat_num} 预约号#{t.reserve_id}（{msg}）",
                level="info",
            )
            return
        await self._error(f"签退失败: {msg}（保持进行中，下个周期重试）", acc.id)
        await self._track_signleave_failure(t, "签退", msg)
        # ★ leave 失败时 **不要** 标 COMPLETE — 留给下次 tick 重试
        await self.store.update_task_status(t.id, TaskStatus.ACTIVE)

    # ---------- cron wiring ----------
    async def reconcile_tick(self) -> None:
        """每分钟：实况核对节拍器——判断是否到期，到期跑一轮 sweep。

        仅在馆舍开放时段（open_time–close_time，默认 08:00–22:00）内
        核对，闭馆时段静默；到期 = 距上次 ≥ 间隔（秒级可配）；另有任务
        将在 30 分钟内开段（签到窗口前）且距上次 ≥ 5min 时提前核对。
        间隔经 load_runtime_settings 热生效，无需重注册 job。
        """
        if self._reconcile_running:
            return
        now = now_cst()
        now_hm = now.strftime("%H:%M")
        if not (self.cfg.library.open_time <= now_hm < self.cfg.library.close_time):
            return
        last = self._reconcile_last_at
        if last is not None and now - last < timedelta(seconds=self.reconcile_interval_seconds):
            if now - last < timedelta(minutes=5) or not await self._pre_sign_due(now):
                return
        self._reconcile_running = True
        try:
            await self.reconcile_sweep()
            await self.sync_user_reserved()
        finally:
            self._reconcile_running = False

    async def _pre_sign_due(self, now: datetime) -> bool:
        """今天是否有进行前任务将在 30 分钟内开始（签到窗口前核对）。"""
        for t in await self.store.list_tasks(day=today_cst()):
            if t.status not in (TaskStatus.ACTIVE, TaskStatus.SUBMITTING):
                continue
            lead = at_cst(t.day, t.start_time) - now
            if timedelta(0) < lead <= timedelta(minutes=30):
                return True
        return False

    # ---------- 当日补约（14:00 后改矩阵/提交失败的当日 PENDING 自动补提交） ----------

    async def today_backfill_tick(self) -> None:
        """每分钟醒来判断是否到期（默认 15 分钟，秒级可配），到期跑一轮当日补提交。

        范围收窄在"今天"：明天的 PENDING 归 _afternoon_bootstrap (14:00) 管，
        本节拍只兜底 14:00 后新增/改绑产生的当日任务与当日提交失败后的重试候选，
        且只补时段完全未开始的段（已开始的段页面格子不可点，补提交必然被拒）。
        经 _bootstrap_gate 与 14:00 批量/启动补跑互斥；不碰 FAILED（自动重试
        已失败提交会追加违约记录，须人工确认——与启动补跑同一原则）。
        仅在预约窗口（14:00 起）与馆舍开放时段内执行，闭馆静默。
        """
        if self._today_backfill_running:
            return
        now = now_cst()
        # 预约窗口 14:00 起；闭馆后无意义
        if now.hour < 14:
            return
        now_hm = now.strftime("%H:%M")
        if not (self.cfg.library.open_time <= now_hm < self.cfg.library.close_time):
            return
        last = self._today_backfill_last_at
        if last is not None and now - last < timedelta(seconds=self.today_backfill_interval_seconds):
            return
        if not await self._has_backfillable_today(now):
            return
        self._today_backfill_running = True
        self._today_backfill_last_at = now
        try:
            async with self._bootstrap_gate:
                await self._today_backfill_locked(now)
        finally:
            self._today_backfill_running = False

    async def _has_backfillable_today(self, now: datetime) -> bool:
        """今天存在可补提交的 PENDING 任务（时段**完全未开始**）。

        已开始/进行中的段在超星页面上格子不可点，补提交必然被拒
        （实弹 14:01 三连拒实证），故只认 start 晚于当前的时段。
        """
        today = today_cst()
        for t in await self.store.list_tasks(day=today):
            if t.status != TaskStatus.PENDING:
                continue
            if at_cst(t.day, t.start_time) > now:
                return True
        return False

    async def _today_backfill_locked(self, now: datetime) -> None:
        """补提交当日时段完全未开始的 PENDING 任务（调用方已持 _bootstrap_gate）。"""
        if self._ha_blocked("submit"):
            return
        today = today_cst()
        accounts = {a.id: a for a in await self.store.list_accounts()}
        n_ok = n_skip = 0
        for t in await self.store.list_tasks(day=today):
            if t.status != TaskStatus.PENDING or at_cst(t.day, t.start_time) <= now:
                continue
            acc = accounts.get(t.account_id)
            if not acc:
                n_skip += 1
                continue
            try:
                await self._run_submit(acc, t)
                n_ok += 1
            except Exception as e:
                await self._error(
                    f"当日补约: 提交抛出异常 {type(e).__name__}: {e} 任务={t.id} 座位={t.seat_num}",
                    acc.id,
                )
                n_skip += 1
            await asyncio.sleep(2)
        if n_ok:
            await self._info(f"当日补约: 补提交 {n_ok} 条当日任务", "scheduler")

    async def reconcile_sweep(self, write: bool = True) -> dict:
        """一轮实况核对：今天+明天非终态任务 vs 服务端真实占用。

        按 (day, seat) 分组查询，一轮至多 2×目标座位数 个只读请求；
        账号动态选取（会话最新者优先），全部分组查询失败且非刚登录时
        重置会话重登一次再试（与 Web 占用查询的自愈策略一致）。
        write=True 时失守沿写带标记的 last_error 并告警一次，恢复沿
        只清自己写的标记；不改任务状态。两种模式都落 reconcile_results
        快照。返回 {"checked", "mismatch", "fetch_ok"}。
        """
        from seatbot.reconcile import pick_read_account

        days = [today_cst(), today_cst() + timedelta(days=1)]
        live = (TaskStatus.ACTIVE, TaskStatus.SIGNED,
                TaskStatus.SUBMITTING, TaskStatus.LEAVING)
        tasks = [t for d in days
                 for t in await self.store.list_tasks(day=d)
                 if t.status in live]
        now_m = _time.monotonic()
        cooling = {aid for aid, until in self._relogin_cooldown.items() if now_m < until}
        acc = pick_read_account(
            await self.store.list_accounts(),
            await self.store.cookie_recency(),
            exclude_ids=cooling,
        )
        self._reconcile_last_at = now_cst()
        if acc is None or not tasks:
            return {"checked": 0, "mismatch": 0, "fetch_ok": True}

        client = await self.client_ready(acc)
        just_logged_in = False
        if not client.cookies():
            if not await self.login_and_persist(acc, client, "实况核对登录"):
                await self._warn("实况核对: 无可用登录会话，本轮跳过", acc.id)
                return {"checked": 0, "mismatch": 0, "fetch_ok": False}
            just_logged_in = True

        groups: dict[tuple[date, str], list[Task]] = {}
        for t in tasks:
            groups.setdefault((t.day, t.seat_num), []).append(t)

        out = await self._sweep_once(acc, client, groups, write)
        if not just_logged_in and not out["fetch_ok"] and out["checked"] == 0:
            client.reset_session()
            relogin_ok = await self.login_and_persist(acc, client, "实况核对重登")
            if relogin_ok:
                out = await self._sweep_once(acc, client, groups, write)
            if not relogin_ok or (not out["fetch_ok"] and out["checked"] == 0):
                self._relogin_cooldown[acc.id] = _time.monotonic() + 300.0
        return out

    async def _sweep_once(self, acc: Account, client,
                          groups: dict[tuple[date, str], list[Task]],
                          write: bool) -> dict:
        """执行一轮全部分组查询；异常分组记失败快照，不写任何任务字段。"""
        from seatbot.reconcile import classify_task

        checked = mismatch = 0
        fetch_ok = True
        for (day, seat_num), group in groups.items():
            try:
                used = await client.get_used_times(
                    self.cfg.library.room_id, seat_num, day.isoformat(),
                )
            except Exception as e:
                fetch_ok = False
                await self._warn(
                    f"实况核对: {day} 座位={seat_num} 查询失败 "
                    f"{type(e).__name__}: {e}", acc.id)
                await self.store.save_reconcile_result(
                    day, seat_num, False,
                    json.dumps({"error": str(e)[:200]}, ensure_ascii=False))
                continue
            rows: list[dict] = []
            for t in group:
                ratio, bad = classify_task(
                    t.start_time.strftime("%H:%M"),
                    t.end_time.strftime("%H:%M"), used)
                checked += 1
                if bad:
                    mismatch += 1
                rows.append({
                    "id": t.id, "account": t.account_id,
                    "win": f"{t.start_time:%H:%M}-{t.end_time:%H:%M}",
                    "status": t.status.value,
                    "ratio": round(ratio, 2), "bad": bad,
                })
                if write:
                    await self._apply_verdict(acc, t, ratio, bad)
            await self.store.save_reconcile_result(
                day, seat_num, all(not r["bad"] for r in rows),
                json.dumps({"server": used, "tasks": rows},
                           ensure_ascii=False))
        return {"checked": checked, "mismatch": mismatch, "fetch_ok": fetch_ok}

    async def sync_user_reserved(self) -> dict:
        """实况同步：把各账号 reservelist 中未被本地任务跟踪的生效预约
        自动补登为 user_reserved，并清退已失效的自动同步行；同时按
        启用账号维度自动托管未跟踪预约（建 hosted_reservations 行 +
        ACTIVE/SIGNED 托管任务）。

        随实况核对节拍运行；reservelist 只返回登录账号本人的记录，故
        逐账号各发 1 个只读 GET。补登走与手动登记同一张表（note 带实况
        同步标记），排程生成任务时据此跳过冲突时段；预约取消/履约/违约
        后从 reservelist 消失，对应自动行在下一轮被清退，手动登记行不受
        影响。单账号查询失败仅跳过该账号（本轮不清退其行，防止误删），
        记 warn 告警。返回 {"added", "pruned", "adopted", "queued", "pending", "ended"} 计数。
        """
        from seatbot.reconcile import (
            AUTO_SYNC_NOTE,
            diff_user_reserved,
            parse_reservations,
            plan_adoption,
        )
        from seatbot.models import (
            Task as _Task, TaskStatus,
            TASK_SOURCE_ADOPT, TASK_SOURCE_ADOPT_MATRIX,
        )

        days = {today_cst(), today_cst() + timedelta(days=1)}
        added = pruned = adopted = queued = pending = ended = 0
        now_dt = now_cst()
        for acc in await self.store.list_accounts():
            if not (acc.phone and acc.password):
                continue
            try:
                client = await self.client_ready(acc)
                if not client.cookies():
                    if not await self.login_and_persist(acc, client, "实况同步登录"):
                        continue
                entries = await client.reserve_list()
                parsed = parse_reservations(entries, days)
                # ★ 已采纳占位纳入 known：托管期间由采纳方显式维护占位行，
                # 实况同步不再为已托管预约补登 user_reserved。
                known = {
                    t.reserve_id for t in await self.store.list_tasks(account_id=acc.id)
                    if t.reserve_id
                }
                hosted_rows = await self.store.list_hosted(limit=None)
                for h in hosted_rows:
                    if h["account_id"] == acc.id and h["state"] == "hosting":
                        known.add(int(h["reserve_id"]))
                existing = [r for r in await self.store.list_user_reserved()
                            if r["account_id"] == acc.id]
                to_add, to_del = diff_user_reserved(parsed, known, existing)
                for p in to_add:
                    await self.store.add_user_reserved(
                        acc.id, p["seat_num"], p["day"], p["start"], p["end"],
                        note=AUTO_SYNC_NOTE)
                for rid in to_del:
                    await self.store.delete_user_reserved(rid)
                added += len(to_add)
                pruned += len(to_del)
                actions: list = []
                # ★ 托管采纳：仅启用账号；禁用账号保留既有 user_reserved 同步
                if acc.status == "active":
                    stopped_ids: set[int] = set()
                    queued_ids: set[int] = set()
                    for h in hosted_rows:
                        if h["account_id"] != acc.id:
                            continue
                        if h["state"] == "stopped":
                            stopped_ids.add(int(h["reserve_id"]))
                        elif h["state"] == "queued":
                            queued_ids.add(int(h["reserve_id"]))
                    tasks_for_acc = await self.store.list_tasks(account_id=acc.id)
                    account_tasks = [
                        {
                            "id": t.id, "account_id": t.account_id,
                            "day": t.day, "seat_num": t.seat_num,
                            "start_time": t.start_time,
                            "status": t.status.value,
                            "reserve_id": t.reserve_id,
                        }
                        for t in tasks_for_acc
                    ]
                    actions = plan_adoption(
                        acc.id, parsed, known,
                        stopped_ids, queued_ids, account_tasks,
                        now=now_dt,
                    )
                    parsed_reserve_ids = {int(p["reserve_id"]) for p in parsed}

                    for act in actions:
                        kind = act["kind"]
                        if kind == "conflict":
                            await self._warn(
                                f"托管冲突 reserve_id={act['reserve_id']} "
                                f"任务={act.get('task_id')} 上游 status={act['upstream_status']}",
                                acc.id)
                            continue
                        rid = int(act["reserve_id"])
                        if kind == "convert":
                            await self.store.update_task_status(
                                int(act["task_id"]),
                                TaskStatus(act["status"]),
                                reserve_id=rid,
                                source=TASK_SOURCE_ADOPT_MATRIX,
                                last_error="",
                            )
                        else:  # create
                            try:
                                tid = await self.store.add_task(_Task(
                                    id=None, account_id=acc.id,
                                    day=act["day"], start_time=act["start"],
                                    end_time=act["end"], seat_num=act["seat_num"],
                                    status=TaskStatus(act["status"]),
                                    reserve_id=rid,
                                    source=TASK_SOURCE_ADOPT,
                                ))
                                act["_task_id"] = tid
                            except Exception as e:
                                await self._warn(
                                    f"托管采纳建任务失败 reserve_id={rid} "
                                    f"{type(e).__name__}: {e}", acc.id)
                                continue
                        await self.store.upsert_hosted(
                            acc.id, rid,
                            seat_num=act["seat_num"],
                            day=act["day"], start=act["start"], end=act["end"],
                            state="hosting",
                            outcome="",
                            task_id=act.get("_task_id") or act.get("task_id"),
                        )
                        # 占位行（仅在该 (seat, day) 无重叠 AUTO_SYNC 行时）
                        slots = await self.store.get_user_reserved_slot_set(
                            act["day"], act["seat_num"])

                        new_s = act["start"]
                        new_e = act["end"]
                        overlap = False
                        for s, e in slots:
                            if not (new_e <= s or new_s >= e):
                                overlap = True
                                break
                        if not overlap:
                            await self.store.add_user_reserved(
                                acc.id, act["seat_num"], act["day"],
                                new_s, new_e, note=AUTO_SYNC_NOTE)
                        await self.store.log_action(
                            acc.id, "host_adopt", str(rid),
                            f"{act['seat_num']} {act['day']} "
                            f"{new_s.strftime('%H:%M')}-{new_e.strftime('%H:%M')}",
                            True, f"status={act['status']}",
                        )
                        await self._notify(
                            "已自动托管手动预约",
                            f"账号={acc.id} 座位={act['seat_num']} "
                            f"{act['day']} {new_s.strftime('%H:%M')}-"
                            f"{new_e.strftime('%H:%M')}",
                            level="info",
                        )
                        adopted += 1

                    # 消失检测：托管行 hosting 但本轮 parsed 不见
                    for h in hosted_rows:
                        if h["account_id"] != acc.id:
                            continue
                        if h["state"] != "hosting":
                            continue
                        rid = int(h["reserve_id"])
                        if rid in parsed_reserve_ids:
                            continue
                        from datetime import date as _d3, time as _t3
                        start_dt = at_cst(_d3.fromisoformat(h["day"]), _t3(*map(int, h["start_time"].split(":"))))
                        end_dt = at_cst(_d3.fromisoformat(h["day"]), _t3(*map(int, h["end_time"].split(":"))))
                        # 任务状态
                        task_id = h.get("task_id")
                        task = await self.store.get_task(task_id) if task_id else None
                        if task is None or task.status.value in ("complete", "failed"):
                            # 归档
                            outcome = "已履约"
                            if task and task.status.value == "failed":
                                outcome = task.last_error or "履约失败"
                            await self.store.update_hosted(
                                h["id"], state="ended", outcome=outcome,
                            )
                            ended += 1
                            continue
                        if now_dt < start_dt:
                            await self.store.update_task_status(
                                task_id, TaskStatus.FAILED,
                                last_error="App 端预约已取消",
                            )
                            await self.store.update_hosted(
                                h["id"], state="pending_decision",
                                outcome="App 端预约已取消",
                            )
                            await self._notify(
                                "自动托管预约已取消，待决",
                                f"账号={acc.id} 座位={h['seat_num']} "
                                f"{h['day']} {h['start_time']}-{h['end_time']} "
                                f"可在自动托管页重抢或放弃",
                                level="warn",
                            )
                            pending += 1
                        # now ≥ start：不动（tick 终态自愈），随后归档

                    # 待决超时：pending_decision 行 now ≥ start → stopped
                    for h in hosted_rows:
                        if h["account_id"] != acc.id:
                            continue
                        if h["state"] != "pending_decision":
                            continue
                        from datetime import date as _d3, time as _t3
                        start_dt = at_cst(_d3.fromisoformat(h["day"]), _t3(*map(int, h["start_time"].split(":"))))
                        if now_dt < start_dt:
                            continue
                        await self.store.update_hosted(
                            h["id"], state="stopped",
                            outcome="未确认自动放弃",
                        )
                        await self._notify(
                            "自动托管待决超时已放弃",
                            f"账号={acc.id} 座位={h['seat_num']} "
                            f"{h['day']} {h['start_time']}-{h['end_time']}",
                            level="warn",
                        )

                    # queued 行：本轮 parsed 仍见且时段未结束 → 走采纳；否则 stopped
                    for h in hosted_rows:
                        if h["account_id"] != acc.id:
                            continue
                        if h["state"] != "queued":
                            continue
                        rid = int(h["reserve_id"])
                        from datetime import date as _d3, time as _t3
                        end_dt = at_cst(_d3.fromisoformat(h["day"]), _t3(*map(int, h["end_time"].split(":"))))
                        if rid in parsed_reserve_ids and now_dt < end_dt:
                            # 复用采纳路径：状态由上游决定
                            from seatbot.reconcile import ADOPT_STATUS_MAP
                            upstream = 0
                            for p in parsed:
                                if int(p["reserve_id"]) == rid:
                                    upstream = int(p["status"])
                                    break
                            mapped = ADOPT_STATUS_MAP.get(upstream, "active")
                            await self.store.upsert_hosted(
                                acc.id, rid,
                                seat_num=h["seat_num"], day=h["day"],
                                start=_t3(*map(int, h["start_time"].split(":"))),
                                end=_t3(*map(int, h["end_time"].split(":"))),
                                state="hosting", outcome="",
                                task_id=h.get("task_id"),
                            )
                            if h.get("task_id"):
                                await self.store.update_task_status(
                                    h["task_id"], TaskStatus(mapped),
                                    reserve_id=rid,
                                    source=TASK_SOURCE_ADOPT,
                                    last_error="",
                                )
                            queued += 1
                        else:
                            await self.store.update_hosted(
                                h["id"], state="stopped", outcome="预约已失效",
                            )
                if to_add or to_del or actions:
                    await self._info(
                        f"实况同步: 补登 {len(to_add)} 清退 {len(to_del)} "
                        f"采纳 {len(actions)} (账号共 {len(parsed)} 条生效预约)",
                        acc.id)
            except Exception as e:
                await self._warn(
                    f"实况同步: {type(e).__name__}: {e}", acc.id)
        return {
            "added": added, "pruned": pruned,
            "adopted": adopted, "queued": queued,
            "pending": pending, "ended": ended,
        }

    async def refresh_hosting_now(self) -> dict:
        """Web「立即刷新」入口：跑一轮实况同步，与 reconcile_tick 共用
`_reconcile_running` 互斥（避免与节拍并发重叠）。"""
        if self._reconcile_running:
            return {"busy": True}
        self._reconcile_running = True
        try:
            return await self.sync_user_reserved()
        finally:
            self._reconcile_running = False


    async def _apply_verdict(self, acc: Account, t: Task,
                             ratio: float, bad: bool) -> None:
        """失守沿：标记 last_error + 告警一次；恢复沿：只清自己写的标记。"""
        from seatbot.reconcile import RECONCILE_ERROR_PREFIX

        if bad and t.id not in self._reconcile_flagged:
            self._reconcile_flagged.add(t.id)
            await self.store.update_task_status(
                t.id, t.status,
                last_error=f"{RECONCILE_ERROR_PREFIX}"
                           f"服务端该时段未见完整占用（覆盖 {ratio:.0%}）")
            await self._notify(
                "实况核对：预约在服务端未生效",
                f"任务={t.id} {t.account_id} {t.seat_num} {t.day} "
                f"{t.start_time:%H:%M}-{t.end_time:%H:%M} 覆盖 {ratio:.0%}",
                level="error")
        elif not bad and t.id in self._reconcile_flagged:
            self._reconcile_flagged.discard(t.id)
            fresh = await self.store.get_task(t.id)
            if fresh and (fresh.last_error or "").startswith(RECONCILE_ERROR_PREFIX):
                await self.store.update_task_status(t.id, fresh.status, last_error="")
            await self._info(
                f"实况核对: 恢复一致 任务={t.id} {t.seat_num} {t.day}", acc.id)

    async def sync_jobs(self) -> None:
        # 含禁用中账号: 其 tick job 不摘除, 排干守卫 (tick_account) 保证无在途任务时零动作
        accounts = await self.store.list_accounts(include_inactive=True)
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
            delay = random.uniform(*self.stagger_seconds)
            self.scheduler.add_job(
                self._tick_account_with_bootstrap,
                "cron", second=f"{int(delay)}",
                args=[acc.id],
                id=f"tick_{acc.id}",
                replace_existing=True,
                misfire_grace_time=30, coalesce=True,
            )

    def start(self) -> None:
        self.scheduler.add_job(
            self.sync_jobs,
            "interval", seconds=self.tick_interval_seconds,
            id="sync_jobs", replace_existing=True,
            misfire_grace_time=60, coalesce=True,
        )
        self.scheduler.add_job(
            self._new_day_bootstrap,
            CronTrigger(hour=0, minute=0, second=5, timezone="Asia/Shanghai"),
            id="new_day_bootstrap", replace_existing=True,
            misfire_grace_time=600, coalesce=True,
        )
        # ★ 每天14:00触发：为明天生成预约任务（超星14:00后开放次日预约窗口）
        self.scheduler.add_job(
            self._afternoon_bootstrap,
            CronTrigger(hour=14, minute=0, second=3, timezone="Asia/Shanghai"),
            id="afternoon_bootstrap", replace_existing=True,
            misfire_grace_time=3600, coalesce=True,
        )

        # 实况核对节拍器：每分钟醒来判断是否到期，间隔/开关热读配置
        self.scheduler.add_job(
            self.reconcile_tick,
            CronTrigger(second=45, timezone="Asia/Shanghai"),
            id="reconcile_tick", replace_existing=True,
            misfire_grace_time=30, coalesce=True,
        )
        # 当日补约节拍器：每分钟醒来判断是否到期（默认 15min），到期补提交当日 PENDING
        self.scheduler.add_job(
            self.today_backfill_tick,
            CronTrigger(second=15, timezone="Asia/Shanghai"),
            id="today_backfill_tick", replace_existing=True,
            misfire_grace_time=30, coalesce=True,
        )
        self.scheduler.start()

    async def startup_reconcile(self) -> None:
        """崩溃恢复: 上次进程死亡时卡在瞬态的任务回退到可驱动状态。

        SUBMITTING 无任何周期作业驱赶 (tick 只处理 ACTIVE/SIGNED/LEAVING),
        不回退会永久悬挂; LEAVING 回退 ACTIVE 后由 tick 重走签退。
        """
        n_submit = n_leave = 0
        # 含禁用中账号: 其 PENDING 已无人提交, 启动时收口为 FAILED,
        # 封堵"禁用瞬间恰有 SUBMITTING、对账回退成 PENDING 后悬空"的缝隙
        for acc in await self.store.list_accounts(include_inactive=True):
            for t in await self.store.list_tasks(account_id=acc.id):
                if acc.status == "inactive" and t.status == TaskStatus.PENDING:
                    await self.store.update_task_status(
                        t.id, TaskStatus.FAILED, last_error="账号已禁用，任务作废",
                    )
                elif t.status == TaskStatus.SUBMITTING:
                    await self.store.update_task_status(
                        t.id, TaskStatus.PENDING,
                        last_error="启动对账: 从『提交中』回退, 待补跑",
                    )
                    n_submit += 1
                elif t.status == TaskStatus.LEAVING:
                    await self.store.update_task_status(t.id, TaskStatus.ACTIVE, last_error="")
                    n_leave += 1
        if n_submit or n_leave:
            await self._warn(
                f"启动对账: 提交中→待提交 {n_submit} 条, 签退中→进行中 {n_leave} 条",
                "scheduler",
            )

    async def startup_afternoon_catchup(self) -> None:
        """错过 14:00 批量的补跑: 预约窗口已开且明天仍有未生成/未提交任务时立即批量。

        经 _bootstrap_gate 与 cron 补跑 (_afternoon_bootstrap) 互斥：
        misfire_grace=3600 使 14:05–15:00 之间重启时 cron 补跑与启动补跑
        可能同时就绪，此闸保证同批 PENDING 只会被一条路径提交。
        """
        async with self._bootstrap_gate:
            await self._startup_afternoon_catchup_locked()

    async def _startup_afternoon_catchup_locked(self) -> None:
        """错过 14:00 批量的补跑: 预约窗口已开且明天仍有未生成/未提交任务时立即批量。

        只补 PENDING (生成后从未提交), 不自动重试 FAILED ——
        失败任务可能撞账号周违约上限, 自动重试会追加违约记录, 必须人工确认。
        14:00–14:05 之间不补 (该窗口属于常驻进程的 cron, 避免双跑竞态)。
        """
        if self._ha_blocked("submit"):
            return
        now = now_cst()
        if now.hour < 14 or (now.hour == 14 and now.minute < 5):
            return
        tomorrow = today_cst() + timedelta(days=1)
        accounts = await self.store.list_accounts()
        seats = [s.seat_num for s in await self.store.list_target_seats()]
        pending: list[Task] = []
        has_any = False
        for acc in accounts:
            tasks = await self.store.list_tasks(account_id=acc.id, day=tomorrow)
            if tasks:
                has_any = True
                pending.extend(t for t in tasks if t.status == TaskStatus.PENDING)
        if not has_any:
            await self._info(
                f"启动补跑: 明日 {tomorrow} 无任何任务 (14:00 批量从未运行) → 生成并提交",
                "scheduler",
            )
            for acc in accounts:
                await self._bootstrap_for_account(acc, tomorrow, seats)
            pending = [
                t for acc in accounts
                for t in await self.store.list_tasks(account_id=acc.id, day=tomorrow)
                if t.status == TaskStatus.PENDING
            ]
        if not pending:
            return
        await self._info(
            f"启动补跑: 预约窗口已开, 明日 {tomorrow} 有 {len(pending)} 条未提交任务 → 立即批量提交",
            "scheduler",
        )
        for acc in accounts:
            todo = [
                t for t in await self.store.list_tasks(account_id=acc.id, day=tomorrow)
                if t.status == TaskStatus.PENDING
            ]
            for t in todo:
                try:
                    await self._run_submit(acc, t)
                except Exception as e:
                    await self._error(
                        f"启动补跑: 提交抛出异常 {type(e).__name__}: {e} "
                        f"任务={t.id} 座位={t.seat_num}",
                        acc.id,
                    )
                await asyncio.sleep(2)
        still = [
            t for acc in accounts
            for t in await self.store.list_tasks(account_id=acc.id, day=tomorrow)
            if t.status == TaskStatus.PENDING
        ]
        if still:
            await self._notify(
                f"启动补跑后明日仍有缺口（{tomorrow}）",
                f"未提交 {len(still)} 条 "
                f"(账号: {', '.join(sorted({t.account_id for t in still}))})。",
                level="error",
            )

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

        经 _bootstrap_gate 与启动补跑 (startup_afternoon_catchup) 互斥，
        重启落在预约窗口内时同批 PENDING 只会被一条路径提交。
        """
        async with self._bootstrap_gate:
            await self._afternoon_bootstrap_locked()

    async def _afternoon_bootstrap_locked(self) -> None:
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
        if self._ha_blocked("submit"):
            return

        from datetime import timedelta
        from seatbot.models import TaskStatus
        tomorrow = today_cst() + timedelta(days=1)

        # 14:00 批量先幂等地为明天生成任务, 再批量提交。
        seats = [s.seat_num for s in await self.store.list_target_seats()]
        accounts = await self.store.list_accounts()
        for acc in accounts:
            await self._bootstrap_for_account(acc, tomorrow, seats)

        for round_no in range(3):
            if round_no > 0:
                await self._info(
                    f"下午批量重试轮 {round_no}: 等待 90 秒后重提未完成任务 日期={tomorrow}",
                    "scheduler",
                )
                await asyncio.sleep(90)
            progressed = False
            for acc in accounts:
                tasks = await self.store.list_tasks(account_id=acc.id, day=tomorrow)
                todo = [
                    t for t in tasks
                    if t.status == TaskStatus.PENDING
                    or (round_no > 0 and t.status == TaskStatus.FAILED)
                ]
                if not todo:
                    continue
                # 账号间错开 3 秒,避免 Playwright 资源冲突 / 风控检测
                if progressed:
                    await asyncio.sleep(3)
                progressed = True
                for t in todo:
                    if t.status == TaskStatus.FAILED:
                        await self.store.update_task_status(
                            t.id, TaskStatus.READY, last_error="")
                        t = await self.store.get_task(t.id)
                    # 单 task 独立 try/except — 一个失败不影响其他
                    try:
                        await self._run_submit(acc, t)
                        # 检查结果:reserve_id 写入 = 成功,否则 _run_submit 已标 FAILED
                        after = await self.store.get_task(t.id)
                        if not (after and after.status == TaskStatus.ACTIVE
                                and after.reserve_id):
                            await self._warn(
                                f"下午批量: 提交未成功 任务={t.id} "
                                f"账号={acc.id} 座位={t.seat_num} {t.day} "
                                f"{t.start_time}-{t.end_time}",
                                acc.id,
                            )
                    except Exception as e:
                        await self._error(
                            f"下午批量: 提交抛出异常 {type(e).__name__}: {e} "
                            f"任务={t.id} 账号={acc.id} 座位={t.seat_num} {t.day} "
                            f"{t.start_time}-{t.end_time}",
                            acc.id,
                        )
                    # 单账号内 task 间错开 2 秒,避免连续 submit 触发风控
                    await asyncio.sleep(2)
            unfinished = [
                t for acc in accounts
                for t in await self.store.list_tasks(account_id=acc.id, day=tomorrow)
                if t.status in (TaskStatus.PENDING, TaskStatus.FAILED)
            ]
            if not unfinished:
                break
        final_tasks = [
            t for acc in accounts
            for t in await self.store.list_tasks(account_id=acc.id, day=tomorrow)
        ]
        submitted = sum(
            1 for t in final_tasks
            if t.status == TaskStatus.ACTIVE and t.reserve_id
        )
        failed = sum(1 for t in final_tasks if t.status == TaskStatus.FAILED)
        await self._info(
            f"下午批量预约完成: 成功={submitted} 失败={failed} 日期={tomorrow}",
            "scheduler",
        )
        # ★ 批量结果进看板: 全部约上 → info「全部成功」; 有未约 → error 并列出失败项
        unresolved = [
            t for t in final_tasks
            if not (t.status == TaskStatus.ACTIVE and t.reserve_id)
        ]
        if submitted > 0 and not unresolved:
            await self.store.add_notification(
                "明日预约全部成功",
                f"{tomorrow} 共 {submitted} 段已全部约上",
                level="info",
            )
        elif unresolved:
            items = [
                f"{t.seat_num} {t.start_time}-{t.end_time} {t.account_id}: "
                f"{(t.last_error or '未预约').replace(chr(10), ' ')[:40]}"
                for t in unresolved[:6]
            ]
            await self._notify(
                "明日预约存在失败项",
                f"{tomorrow} 成功 {submitted} / 未约 {len(unresolved)}："
                f"{'；'.join(items)}{'…' if len(unresolved) > 6 else ''}。"
                f"可在任务看板用「改绑重试」换账号，或调整矩阵后等下个周期。",
                level="error",
            )

    async def shutdown(self) -> None:
        self.scheduler.shutdown(wait=False)
        for c in self._clients.values():
            await c.close()
