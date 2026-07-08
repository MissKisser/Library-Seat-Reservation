"""Render dashboard.html via Jinja2 directly to surface any template errors."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from seatbot.config import load_config
from seatbot.coverage import compute_coverage
from seatbot.models import TaskStatus
from seatbot.store import StateStore
from seatbot.utils.timeutil import now_cst, today_cst


TEMPLATES = Path(__file__).resolve().parent.parent / "seatbot" / "web" / "templates"


async def main() -> int:
    cfg = load_config("config.yaml")
    store = StateStore(cfg.runtime.db_path)
    try:
        await store.init()
        accounts = await store.list_accounts()
        today = today_cst()
        cov = compute_coverage(
            accounts, today,
            open_time=cfg.library.open_time,
            close_time=cfg.library.close_time,
        )
        annotated = []
        for c in cov.cells:
            accs_info: list[dict] = []
            for aid in c.accounts:
                tasks = await store.list_tasks(account_id=aid, day=today)
                status = "pending"
                for t in tasks:
                    if (
                        t.start_time == c.start
                        and t.end_time == c.end
                        and t.status in (
                            TaskStatus.ACTIVE, TaskStatus.SUBMITTING,
                            TaskStatus.LEAVING, TaskStatus.FAILED,
                        )
                    ):
                        status = t.status.value
                        break
                accs_info.append({"id": aid, "status": status})
            annotated.append({"start": c.start, "end": c.end, "accounts": accs_info})
    finally:
        await store.close()

    env = Environment(loader=FileSystemLoader(str(TEMPLATES)))
    try:
        tmpl = env.get_template("dashboard.html")
        rendered = tmpl.render(
            cfg=cfg,
            cells=annotated,
            gaps=[list(g) for g in cov.gaps],
            overlaps=[list(o) for o in cov.overlaps],
            today=today.isoformat(),
            accounts=accounts,
            now_hhmm=now_cst().strftime("%H:%M"),
        )
        print(f"OK — rendered {len(rendered)} bytes")
        Path("dash.html").write_text(rendered, encoding="utf-8")
        print("written to dash.html")
        return 0
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
