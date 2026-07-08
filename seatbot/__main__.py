"""CLI entry point."""
from __future__ import annotations

import argparse
import asyncio
import sys

from seatbot.config import load_config, ConfigError
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="seatbot", description="超星图书馆座位自动化")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", "-c", default="./config.yaml")

    sub.add_parser("run", parents=[common], help="启动 Web + 调度器")
    sub.add_parser("init-db", parents=[common], help="初始化 SQLite")
    once = sub.add_parser("once", parents=[common], help="单次执行 (调试)")
    once.add_argument("--account", required=True)
    once.add_argument("--action", choices=["all", "submit", "sign", "leave", "cancel"], default="all")

    login = sub.add_parser("login", parents=[common], help="单独登录测试")
    login.add_argument("--account", required=True)

    sub.add_parser("status", parents=[common], help="查看状态")
    return p


async def _cmd_run(args) -> int:
    cfg = load_config(args.config)
    store = StateStore(cfg.runtime.db_path)
    await store.init()
    n = await store.sync_accounts(cfg.accounts)
    print(f"synced {n} account(s) from config (web UI is the primary source)")
    sched = Scheduler(cfg, store)
    sched.start()
    await sched.bootstrap_today()
    await sched.sync_jobs()  # register tick jobs for all DB accounts
    # start web in parallel
    from seatbot.web.app import make_app
    app = make_app(cfg, store, sched)
    import uvicorn
    config = uvicorn.Config(
        app, host=cfg.runtime.web_host, port=cfg.runtime.web_port, log_level="info"
    )
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        await sched.shutdown()
        await store.close()
    return 0


async def _cmd_init_db(args) -> int:
    cfg = load_config(args.config)
    store = StateStore(cfg.runtime.db_path)
    await store.init()
    n = await store.sync_accounts(cfg.accounts)
    await store.close()
    print(f"initialized: {cfg.runtime.db_path}; synced {n} account(s)")
    return 0


async def _cmd_once(args) -> int:
    cfg = load_config(args.config)
    store = StateStore(cfg.runtime.db_path)
    await store.init()
    await store.sync_accounts(cfg.accounts)
    sched = Scheduler(cfg, store)
    await sched.bootstrap_today()
    await sched.tick_account(args.account)
    await sched.shutdown()
    await store.close()
    return 0


async def _cmd_login(args) -> int:
    cfg = load_config(args.config)
    store = StateStore(cfg.runtime.db_path)
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
    store = StateStore(cfg.runtime.db_path)
    await store.init()
    try:
        for acc in await store.list_accounts():
            tasks = await store.list_tasks(account_id=acc.id)
            print(f"\n[{acc.id}] phone={acc.phone} slots={acc.slots}")
            for t in tasks:
                print(f"  {t.day} {t.start_time}-{t.end_time} {t.status.value} "
                      f"reserve={t.reserve_id} err={t.last_error}")
        print("\n--- recent actions ---")
        for a in await store.list_actions(limit=10):
            print(f"  {a.ts} [{a.account_id}] {a.action} success={a.success} {a.message or ''}")
    finally:
        await store.close()
    return 0


def cli() -> int:
    args = _build_parser().parse_args()
    handlers = {
        "run": _cmd_run,
        "init-db": _cmd_init_db,
        "once": _cmd_once,
        "login": _cmd_login,
        "status": _cmd_status,
    }
    try:
        return asyncio.run(handlers[args.cmd](args))
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(cli())
