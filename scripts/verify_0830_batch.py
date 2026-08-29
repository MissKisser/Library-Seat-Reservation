"""只读核验: 2026-08-30 14:00 批量预约结果 (由重启后常驻服务触发)。

期望 6 条 08-30 任务全部 active 且带预约号; 结果追加写 logs/verify_0830_batch.log。
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime

DB_PATH = r"D:\document\Projects\Library-Seat-Reservation\seatbot.db"
LOG_PATH = r"D:\document\Projects\Library-Seat-Reservation\logs\verify_0830_batch.log"

EXPECT = [
    ("wangh", "030", "09:00", "11:00"),
    ("xiongjt", "031", "09:00", "11:00"),
    ("wangh", "031", "15:00", "17:00"),
    ("zhaozh", "030", "15:30", "17:00"),
    ("xiongjt", "030", "19:00", "21:00"),
    ("zhaozh", "031", "19:00", "21:00"),
]


def main() -> int:
    lines = [f"\n===== 08-30 批量核验 @ {datetime.now():%Y-%m-%d %H:%M:%S} ====="]
    problems = 0
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    got = {}
    for r in con.execute(
        "SELECT account_id, seat_num, start_time, status, reserve_id FROM tasks "
        "WHERE day='2026-08-31'"
    ):
        lines.append(f"[多余] 08-31 出现任务: {r['account_id']}/{r['seat_num']} "
                     f"{r['start_time']} {r['status']}")
        problems += 1
    for r in con.execute(
        "SELECT account_id, seat_num, start_time, status, reserve_id FROM tasks "
        "WHERE day='2026-08-30'"
    ):
        got[(r["account_id"], r["seat_num"], r["start_time"])] = (r["status"], r["reserve_id"])
    for acc, seat, start, _end in EXPECT:
        entry = got.get((acc, seat, start))
        if not entry:
            lines.append(f"[缺] {acc}/{seat} {start} 未生成")
            problems += 1
        elif entry[0] != "active" or not entry[1]:
            lines.append(f"[异常] {acc}/{seat} {start} status={entry[0]} rid={entry[1]}")
            problems += 1
        else:
            lines.append(f"[OK ] {acc}/{seat} {start} rid={entry[1]}")
    for key in sorted(set(got) - {e[:3] for e in EXPECT}):
        lines.append(f"[多余] 08-30 意外任务: {key} {got[key]}")
        problems += 1
    for r in con.execute(
        "SELECT ts, level, account_id, message FROM logs WHERE level IN ('WARN','ERROR') "
        "AND message LIKE '%下午批量%' ORDER BY ts DESC LIMIT 6"
    ):
        lines.append(f"[日志] {r['level']} {r['account_id']}: {r['message'][:130]}")
    con.close()
    lines.append(f"===== 结论: {'6/6 全部就绪' if problems == 0 else f'{problems} 项异常'} =====")
    report = "\n".join(lines)
    print(report)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(report + "\n")
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
