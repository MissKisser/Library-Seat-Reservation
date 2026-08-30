"""seatbot.db 定时备份 CLI (供 schtasks 每日调用, 亦可手动执行)。

用法: python scripts/backup_db.py [--db PATH] [--dir DIR] [--keep N]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from seatbot.store import backup_database


def main() -> int:
    parser = argparse.ArgumentParser(description="SeatBot SQLite 备份 (保留策略内置)")
    parser.add_argument("--db", default=str(ROOT / "seatbot.db"), help="源数据库路径")
    parser.add_argument("--dir", default=str(ROOT / "logs" / "backup"), help="备份目录")
    parser.add_argument("--keep", type=int, default=7, help="保留份数")
    args = parser.parse_args()
    try:
        dest = backup_database(args.db, args.dir, keep=args.keep)
    except Exception as e:  # noqa: BLE001 — 计划任务里任何失败都要以非零码暴露
        print(f"backup FAILED: {e}", file=sys.stderr)
        return 1
    if dest is None:
        print("backup skipped (already done today or db missing)")
        return 0
    print(f"backup ok -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
