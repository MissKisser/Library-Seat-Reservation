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
    ):
        self.cfg = cfg
        self.store = store
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

    async def _notify(self, title: str, body: str = "", level: str = "warn") -> None:
        """用户需要看到的事件: 落库(看板 banner) + 可选 webhook 外推。"""
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
        for t in await self.store.list_tasks(account_id=acc_id, day=today):
            if t.status not in (TaskStatus.ACTIVE, TaskStatus.SIGNED, TaskStatus.LEAVING):
                continue
            t_start = at_cst(t.day, t.start_time)
            t_end = at_cst(t.day, t.end_time)
            # 1. 到 leave 时机 (RELAY_LEAD_SECONDS=-60 → end+60s) → 签退
            if now >= t_end - timedelta(seconds=self.RELAY_LEAD_SECONDS):
                await self._maybe_relay(t, now)
                continue
            # 2. 时段进行中且未签 → 签到 (成功后置 SIGNED)
            if t.status == TaskStatus.ACTIVE and t_start <= now < t_end:
                await self._run_sign(acc_cfg, t)
                continue
            # 3. SIGNED / 未开始的 ACTIVE / LEAVING 未到点 → 不动

    async def peek_next_relay(self) -> NextRelay | None:
        now = now_cst()
        today = today_cst()
        best: tuple[datetime, NextRelay] | None = None
        for acc in await self.store.list_accounts():
            for t in await self.store.list_tasks(account_id=acc.id, day=today):
                t_start = at_cst(t.day, t.start_time)
                t_end = at_cst(t.day, t.end_time)
                if t.status in (TaskStatus.ACTIVE, TaskStatus.SIGNED, TaskStatus.SUBMITTING, TaskStatus.LEAVING):
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
            if seat == exclude or seat in tried or len(tried) >= 12:
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
                await self._info(f"登录成功（{len(client.cookies())} cookies）", acc.id)
            except ChaoxingError as e:
                await self._warn(f"预登录失败（{e}），改用页面内登录重试", acc.id)

        try:
            if t.day > today_cst():
                # ★ 跨天任务走页面内改写通道 (2026-08-26 B1):
                # 座位页只渲染今天 (R1); abort+httpx 重放被 303 风控全拒
                # (14:00 实测 0/6) — 改由真实页面发提交, 网络层仅改写字段。
                if not self.cfg.runtime.direct_submit_enabled:
                    await self._error(
                        f"配置已禁用直连提交；拒绝用今日页面预约未"
                        f"来日期 {t.day}（会错约到今天）",
                        acc.id,
                    )
                    await self.store.update_task_status(
                        t.id, TaskStatus.FAILED,
                        last_error="已禁用直连提交；未来日期任务已拒绝",
                    )
                    return
                r = await client.submit_via_page_rewrite(
                    phone=acc.phone,
                    password=acc.password,
                    room_id=self.cfg.library.room_id,
                    seat_num=t.seat_num,
                    day=t.day.isoformat(),
                    start_time=t.start_time.strftime("%H:%M"),
                    end_time=t.end_time.strftime("%H:%M"),
                )
                if not r.get("success") and "no selectable cell" in str(r.get("msg") or ""):
                    anchor = await self._pick_anchor_seat(client, t.seat_num)
                    if anchor:
                        await self._warn(
                            f"座位 {t.seat_num} 页面无格子, 改用锚点座位 {anchor} 重试",
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
            else:
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
        if t.day > today_cst():
            try:
                used = await client.get_used_times(
                    self.cfg.library.room_id, t.seat_num, t.day.isoformat(),
                )
                s, e = t.start_time.strftime("%H:%M"), t.end_time.strftime("%H:%M")
                covered = any(us < e and ue > s for us, ue in used)
                if covered:
                    await self._info(
                        f"占用核验一致: {t.day} 座位={t.seat_num} {s}-{e} 预约号#{reserve_id}",
                        acc.id,
                    )
                else:
                    await self._error(
                        f"占用核验为空: 预约号#{reserve_id} {t.day} 座位={t.seat_num} "
                        f"{s}-{e}（服务端 used={used}）——保持进行中，请人工复核",
                        acc.id,
                    )
            except Exception as e:
                await self._warn(f"占用核验异常: {e}", acc.id)

    async def _act_with_relogin(
        self, client: ChaoxingClient, acc: Account, fn, reserve_id: int, label: str,
    ) -> dict:
        """执行 sign/leave; 服务端报"未登录"时清 cookie 重登并重试一次。

        背景 (2026-08-25 actions #2): httpx jar 里若残留陈旧 cookie,
        `_run_sign` 的 lazy-login 分支 (只在 jar 为空时登录) 不会触发,
        sign 会以 "您当前未登录" 失败且无自愈。
        """
        sr = await fn(reserve_id)
        msg = str(sr.get("msg") or "")
        if not sr.get("success") and "未登录" in msg:
            await self._warn(f"{label}: 会话过期（未登录）→ 重置会话并重登重试", acc.id)
            client.reset_session()
            await client.login(acc.phone, acc.password)
            sr = await fn(reserve_id)
        return sr

    async def _run_sign(self, acc: Account, t: Task) -> None:
        """签到 (幂等: 成功或签到窗口已过 → SIGNED, 终止每分钟重试)。"""
        if not t.reserve_id:
            await self._warn(f"跳过签到: 无预约号 座位={t.seat_num} {t.chunk_key()}", acc.id)
            return
        client = self._client_for(acc)
        # ★ lazy login — jar 为空才登录 (陈旧 cookie 由 _act_with_relogin 自愈)
        if not client.cookies():
            try:
                await client.login(acc.phone, acc.password)
                await self._info(f"签到登录成功（{len(client.cookies())} cookies）", acc.id)
            except ChaoxingError as e:
                await self._warn(f"签到预登录失败（{e}）", acc.id)
        try:
            sr = await self._act_with_relogin(client, acc, client.sign, t.reserve_id, "sign")
        except Exception as e:
            await self._error(f"签到异常: {e}", acc.id)
            return
        await self.store.log_action(
            acc.id, "sign", str(t.reserve_id), str(sr)[:500],
            bool(sr.get("success")), str(sr.get("msg")),
        )
        if sr.get("success"):
            await self.store.update_task_status(t.id, TaskStatus.SIGNED, last_error="")
            await self._info(f"签到成功 预约号#{t.reserve_id} 座位={t.seat_num}", acc.id)
            return
        msg = str(sr.get("msg") or "")
        if "不在签到时间" in msg:
            # 消息无法区分"未到签到时间"(任务时间早于预约真实开始)与
            # "窗口已关闭"。开始后 60 分钟内保持重试以自愈时间偏差,
            # 超过则预约必然已生效或失效, 置 SIGNED 终止无效调用。
            deadline = at_cst(t.day, t.start_time) + timedelta(minutes=60)
            if now_cst() < deadline:
                await self._warn(f"签到被拒（{msg}）；窗口状态不明，保持重试", acc.id)
                return
            await self.store.update_task_status(t.id, TaskStatus.SIGNED, last_error="")
            await self._warn(f"签到窗口已关闭（{msg}）；标记已签到，停止重试", acc.id)
            return
        if "不存在" in msg:
            # 预约已在服务端消失 (被取消/退座), 永远签不上 → 终态, 停止空转
            await self.store.update_task_status(
                t.id, TaskStatus.FAILED,
                last_error=f"签到: 服务端预约已不存在（{msg}）",
            )
            await self._warn(f"签到终止: 预约在服务端已不存在（{msg}），任务标失败", acc.id)
            return
        await self._error(f"签到失败: {msg}（保持进行中，下个周期重试）", acc.id)

    async def _run_leave(self, acc: Account, t: Task) -> None:
        """签退 (不 submit/sign)。失败保留在途状态, 由下次 tick 重试。

        ★ 2026-08-25: /leave 实为"暂离"(要求剩余 ≥20min, 接力时点必不满足);
        真正的签退端点是 /signback (退座)。优先 signback, 失败回退 leave。
        """
        if not t.reserve_id:
            await self._warn(f"跳过签退: 无预约号 座位={t.seat_num} {t.chunk_key()}", acc.id)
            # ★ 从未预约成功的任务不应伪装 COMPLETE (虚假完成态会误导审计)
            await self.store.update_task_status(
                t.id, TaskStatus.FAILED, last_error="签退: 无预约号",
            )
            return
        client = self._client_for(acc)
        # ★ lazy login — _run_leave 是独立调用路径
        if not client.cookies():
            try:
                await client.login(acc.phone, acc.password)
                await self._info(f"签退登录成功（{len(client.cookies())} cookies）", acc.id)
            except ChaoxingError as e:
                await self._warn(f"签退预登录失败（{e}）", acc.id)
        try:
            sr = await self._act_with_relogin(client, acc, client.signback, t.reserve_id, "signback")
        except Exception as e:
            await self._error(f"签退(signback)异常: {e}", acc.id)
            sr = {}
        if not sr.get("success"):
            sb_msg = str(sr.get("msg") or "")
            # signback 报"预约已不在服务端" = 终态, 直接收尾;
            # 不能带着这条消息去回退暂离通道 (暂离的返回消息不含终态特征,
            # 会把可终结的死号任务永远留在重试循环里)
            if "不存在" in sb_msg:
                await self._info(f"签退幂等收尾（{sb_msg}）→ 已完成", acc.id)
                await self.store.log_action(
                    acc.id, "signback", str(t.reserve_id), str(sr)[:500], False, sb_msg,
                )
                await self.store.update_task_status(
                    t.id, TaskStatus.COMPLETE, last_error="服务端预约已不存在, 幂等收尾")
                return
            await self._warn(f"签退未成功（{sb_msg}），回退暂离通道", acc.id)
            try:
                sr = await self._act_with_relogin(client, acc, client.leave, t.reserve_id, "leave")
            except Exception as e:
                await self._error(f"暂离异常: {e}", acc.id)
                await self.store.update_task_status(t.id, TaskStatus.ACTIVE)
                return
        await self.store.log_action(
            acc.id, "signback", str(t.reserve_id), str(sr)[:500],
            bool(sr.get("success")), str(sr.get("msg")),
        )
        msg = str(sr.get("msg") or "")
        if sr.get("success"):
            await self.store.update_task_status(t.id, TaskStatus.COMPLETE, last_error="")
            return
        # 幂等收尾: 预约已在服务端终结, 继续重试无意义 → COMPLETE 停止循环。
        # ("剩余时长小于暂离时长" = 离结束不足 leaveDuration, 预约将自然到期)
        idempotent = any(
            k in msg for k in ("已签退", "已结束", "已取消", "不存在", "剩余时长小于暂离时长")
        )
        if idempotent:
            await self._info(f"签退幂等收尾（{msg}）→ 已完成", acc.id)
            await self.store.update_task_status(t.id, TaskStatus.COMPLETE, last_error="")
            return
        await self._error(f"签退失败: {msg}（保持进行中，下个周期重试）", acc.id)
        # ★ leave 失败时 **不要** 标 COMPLETE — 留给下次 tick 重试
        await self.store.update_task_status(t.id, TaskStatus.ACTIVE)

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
            CronTrigger(hour=14, minute=0, second=3, timezone="Asia/Shanghai"),
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

        # ★ P0 修复 (2026-08-25): 旧代码只提交"明天"的 PENDING 任务, 但没有任何
        # 代码为明天生成任务 (_bootstrap_for_account 的全部调用点都传 today) —
        # 即使系统常驻运行, 14:00 也永远空转。现在先幂等地为明天生成任务, 再批量提交。
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
        if failed:
            await self._notify(
                f"明日批量预约存在缺口（{tomorrow}）",
                f"成功 {submitted} / 失败 {failed}。"
                f"失败任务可在任务看板用「改绑重试」换账号，"
                f"或在绑定页调整矩阵后等下个周期。",
                level="error",
            )

    async def shutdown(self) -> None:
        self.scheduler.shutdown(wait=False)
        for c in self._clients.values():
            await c.close()
