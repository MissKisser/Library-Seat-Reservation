"""CLI entry point (v2: multi-seat)."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from seatbot.config import load_config, ConfigError
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="seatbot", description="超星图书馆座位自动化 v2")
    p.add_argument("--config", "-c", default="./config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run", help="启动 Web + 调度器")
    sub.add_parser("init-db", help="初始化 SQLite + 同步 target_seats")
    once = sub.add_parser("once", help="单次执行 (调试: bootstrap + tick 该账号)")
    once.add_argument("--account", required=True)

    login = sub.add_parser("login", help="单独登录测试")
    login.add_argument("--account", required=True)

    sub.add_parser("status", help="查看状态")

    # ★ v2: 目标座位管理
    tgt = sub.add_parser("targets", help="目标座位增删查")
    tgt_sub = tgt.add_subparsers(dest="targets_cmd", required=True)
    tgt_sub.add_parser("list", help="列出所有 enabled 目标座位")
    tgt_add = tgt_sub.add_parser("add", help="新增一个目标座位")
    tgt_add.add_argument("seat_num")
    tgt_add.add_argument("--label", default="")
    tgt_del = tgt_sub.add_parser("del", help="删除一个目标座位")
    tgt_del.add_argument("seat_num")

    # ★ v2+: 用户硬预约段 (scheduler 跳过)
    ur = sub.add_parser("user-reserved", help="用户硬预约段增删查")
    ur_sub = ur.add_subparsers(dest="user_reserved_cmd", required=True)
    ur_sub.add_parser("list", help="列出所有 user_reserved 段")
    ur_add = ur_sub.add_parser("add", help="登记一段用户硬预约")
    ur_add.add_argument("--account", required=True)
    ur_add.add_argument("--seat", required=True)
    ur_add.add_argument("--day", required=True, help="YYYY-MM-DD")
    ur_add.add_argument("--start", required=True, help="HH:MM")
    ur_add.add_argument("--end", required=True, help="HH:MM")
    ur_add.add_argument("--note", default="")
    ur_del = ur_sub.add_parser("del", help="删除一段 (按 ID)")
    ur_del.add_argument("id", type=int)

    # ★ 手动触发单次预约（可控性/调试/补救场景）
    reserve = sub.add_parser("reserve", help="手动触发单次预约")
    reserve.add_argument("--account", required=True, help="账号 ID")
    reserve.add_argument("--seat", required=True, help="座位号 (如 084)")
    reserve.add_argument("--date", dest="day", default=None, help="YYYY-MM-DD，默认明天")
    reserve.add_argument("--start", required=True, help="开始时间 HH:MM")
    reserve.add_argument("--end", required=True, help="结束时间 HH:MM")
    return p


def _make_store(cfg) -> StateStore:
    """创建状态存储，按配置文件初始化目标座位（已设置的保持不变）。"""
    # v2 不再有全局 target_seat_num;legacy 总是 None
    return StateStore(cfg.runtime.db_path, legacy_target_seat_num=None)


async def _cmd_run(args) -> int:
    cfg = load_config(args.config)
    # 启动即备份 (当日已有备份则跳过); 备份失败不阻断启动
    try:
        from seatbot.store import backup_database
        bak = backup_database(cfg.runtime.db_path, str(Path(cfg.runtime.log_dir) / "backup"))
        if bak:
            print(f"db backup -> {bak}")
    except Exception as e:
        print(f"[WARN] db backup failed (继续启动): {e}")
    store = _make_store(cfg)
    await store.init()
    # 按配置文件初始化目标座位（已在页面设置的保持不变）
    for s in cfg.target_seats:
        await store.seed_target_seat(s.seat_num, label=s.label)
    n = await store.sync_accounts(cfg.accounts)
    print(f"synced {n} account(s); {len(cfg.target_seats)} target seat(s) from config")
    sched = Scheduler(cfg, store)
    # 加载页面已保存的系统设置
    try:
        await sched.load_runtime_settings()
    except Exception as e:
        print(f"[WARN] load runtime settings failed: {e}")
    sched.start()

    # 崩溃恢复 + 错过 14:00 窗口的补跑放后台执行, 不阻塞面板起服
    async def _startup_recovery() -> None:
        try:
            await sched.startup_reconcile()
            await sched.bootstrap_today()
            await sched.startup_afternoon_catchup()
            await sched.sync_jobs()
        except Exception as e:
            print(f"[ERROR] startup recovery failed: {type(e).__name__}: {e}")

    asyncio.create_task(_startup_recovery())
    from seatbot.web.app import make_app
    app = make_app(cfg, store, sched)
    import uvicorn
    uvcfg = uvicorn.Config(
        app, host=cfg.runtime.web_host, port=cfg.runtime.web_port, log_level="info"
    )
    server = uvicorn.Server(uvcfg)
    try:
        await server.serve()
    finally:
        await sched.shutdown()
        await store.close()
    return 0


async def _cmd_init_db(args) -> int:
    cfg = load_config(args.config)
    store = _make_store(cfg)
    await store.init()
    for s in cfg.target_seats:
        await store.seed_target_seat(s.seat_num, label=s.label)
    n = await store.sync_accounts(cfg.accounts)
    await store.close()
    print(f"initialized: {cfg.runtime.db_path}; {len(cfg.target_seats)} seat(s); {n} account(s)")
    return 0


async def _cmd_once(args) -> int:
    cfg = load_config(args.config)
    store = _make_store(cfg)
    await store.init()
    for s in cfg.target_seats:
        await store.seed_target_seat(s.seat_num, label=s.label)
    await store.sync_accounts(cfg.accounts)
    sched = Scheduler(cfg, store)
    await sched.bootstrap_today()
    await sched.tick_account(args.account)
    await sched.shutdown()
    await store.close()
    return 0


async def _cmd_login(args) -> int:
    cfg = load_config(args.config)
    store = _make_store(cfg)
    await store.init()
    try:
        acc = await store.get_account(args.account)
        if not acc:
            print(f"account {args.account} not found in DB", file=sys.stderr)
            return 1
        from seatbot.client import ChaoxingClient, ChaoxingError
        c = ChaoxingClient()
        try:
            await c.login(acc.phone, acc.password)
            print(f"login OK; cookies: {list(c.cookies().keys())}")
        except ChaoxingError as e:
            print(f"login failed: {e}", file=sys.stderr)
            return 2
        finally:
            await c.close()
    finally:
        await store.close()
    return 0


async def _cmd_status(args) -> int:
    cfg = load_config(args.config)
    store = _make_store(cfg)
    await store.init()
    try:
        # seed user_reserved (idempotent)
        from datetime import date as _date, time as _time
        for u in cfg.user_reserved:
            d = _date.fromisoformat(u.day)
            s = _time(*map(int, u.start_time.split(":")))
            e = _time(*map(int, u.end_time.split(":")))
            existing = await store.list_user_reserved(seat_num=u.seat_num, day=d)
            if not any(r["account_id"] == u.account_id
                       and r["start_time"] == s.strftime("%H:%M")
                       for r in existing):
                await store.add_user_reserved(u.account_id, u.seat_num, d, s, e, u.note)
        seats = await store.list_target_seats()
        if seats:
            print("--- target seats ---")
            for s in seats:
                print(f"  {s.seat_num}  label={s.label!r}  enabled={s.enabled}")
        else:
            print("(no target seats; add via config or web)")
        urs = await store.list_user_reserved()
        if urs:
            print("\n--- user_reserved ---")
            for u in urs:
                print(f"  #{u['id']:3d}  {u['account_id']:10s}  {u['seat_num']}  "
                      f"{u['day']}  {u['start_time']}-{u['end_time']}  {u['note']!r}")
        for acc in await store.list_accounts():
            tasks = await store.list_tasks(account_id=acc.id)
            print(f"\n[{acc.id}] phone={acc.phone} slots={acc.slots} bound_seats={acc.bound_seats}")
            for t in tasks:
                print(f"  seat={t.seat_num} {t.day} {t.start_time}-{t.end_time} "
                      f"{t.status.value} reserve={t.reserve_id} err={t.last_error}")
        print("\n--- recent actions ---")
        for a in await store.list_actions(limit=10):
            print(f"  {a.ts} [{a.account_id}] {a.action} success={a.success} {a.message or ''}")
    finally:
        await store.close()
    return 0


async def _cmd_targets(args) -> int:
    cfg = load_config(args.config)
    store = _make_store(cfg)
    await store.init()
    try:
        if args.targets_cmd == "list":
            seats = await store.list_target_seats()
            for s in seats:
                print(f"{s.seat_num}\t{s.label}")
        elif args.targets_cmd == "add":
            await store.add_target_seat(args.seat_num, label=args.label or "")
            print(f"added target seat {args.seat_num} (label={args.label!r})")
        elif args.targets_cmd == "del":
            await store.delete_target_seat(args.seat_num)
            print(f"deleted target seat {args.seat_num}")
    finally:
        await store.close()
    return 0


async def _cmd_reserve(args) -> int:
    """手动触发单次预约（可控性/调试/补救场景）。"""
    from datetime import date as _date, time as _time, timedelta
    cfg = load_config(args.config)
    store = _make_store(cfg)
    await store.init()
    await store.sync_accounts(cfg.accounts)
    try:
        acc = await store.get_account(args.account)
        if not acc:
            print(f"account {args.account!r} not found", file=sys.stderr)
            return 1
        day = _date.fromisoformat(args.day) if args.day else (_date.today() + timedelta(days=1))
        start_t = _time(*map(int, args.start.split(":")))
        end_t = _time(*map(int, args.end.split(":")))
        if end_t <= start_t:
            print("end must be after start", file=sys.stderr)
            return 2
        seat_num = args.seat.zfill(3) if args.seat.isdigit() else args.seat
        from seatbot.models import Task, TaskStatus
        t = Task(
            id=None,
            account_id=acc.id,
            day=day,
            start_time=start_t,
            end_time=end_t,
            seat_num=seat_num,
            status=TaskStatus.READY,
        )
        tid = await store.add_task(t)
        loaded = await store.get_task(tid)
        if not loaded:
            print("failed to create task", file=sys.stderr)
            return 3
        sched = Scheduler(cfg, store)
        print(f"submitting: {acc.id} seat={seat_num} {day} {args.start}-{args.end}")
        await sched._run_submit(acc, loaded)
        await sched.shutdown()
        print("done")
        return 0
    finally:
        await store.close()


async def _cmd_user_reserved(args) -> int:
    cfg = load_config(args.config)
    store = _make_store(cfg)
    await store.init()
    try:
        # 同时 seed config.yaml 里的 user_reserved (idempotent: 用 (account,seat,day,start) 去重)
        from datetime import date as _date, time as _time
        for u in cfg.user_reserved:
            d = _date.fromisoformat(u.day)
            s = _time(*map(int, u.start_time.split(":")))
            e = _time(*map(int, u.end_time.split(":")))
            existing = await store.list_user_reserved(seat_num=u.seat_num, day=d)
            if any(r["account_id"] == u.account_id and r["start_time"] == s.strftime("%H:%M")
                   for r in existing):
                continue
            await store.add_user_reserved(
                account_id=u.account_id,
                seat_num=u.seat_num,
                day=d, start_time=s, end_time=e,
                note=u.note,
            )
        if args.user_reserved_cmd == "list":
            rows = await store.list_user_reserved()
            for r in rows:
                print(f"#{r['id']:3d}  {r['account_id']:10s}  {r['seat_num']}  {r['day']}  "
                      f"{r['start_time']}-{r['end_time']}  {r['note']!r}")
        elif args.user_reserved_cmd == "add":
            d = _date.fromisoformat(args.day)
            s = _time(*map(int, args.start.split(":")))
            e = _time(*map(int, args.end.split(":")))
            if e <= s:
                print("end must be after start", file=sys.stderr)
                return 2
            rid = await store.add_user_reserved(
                account_id=args.account,
                seat_num=args.seat,
                day=d, start_time=s, end_time=e,
                note=args.note,
            )
            print(f"added user_reserved #{rid} for {args.account} {args.seat} {args.day} {args.start}-{args.end}")
        elif args.user_reserved_cmd == "del":
            await store.delete_user_reserved(args.id)
            print(f"deleted user_reserved #{args.id}")
    finally:
        await store.close()
    return 0


def cli() -> int:
    args = _build_parser().parse_args()
    handlers: dict = {
        "run": _cmd_run,
        "init-db": _cmd_init_db,
        "once": _cmd_once,
        "login": _cmd_login,
        "status": _cmd_status,
        "targets": _cmd_targets,
        "user-reserved": _cmd_user_reserved,
        "reserve": _cmd_reserve,
    }
    try:
        return asyncio.run(handlers[args.cmd](args))
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(cli())
