"""一次性只读自检脚本：核对 2026-08-29 座位切换日的任务状态。

按时段边界推导每条任务的期望状态，与 seatbot.db 实际状态比对，
把报告追加写入 logs/verify_0829.log。仅读数据库，不做任何预约操作。
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime

DB_PATH = r"D:\document\Projects\Library-Seat-Reservation\seatbot.db"
LOG_PATH = r"D:\document\Projects\Library-Seat-Reservation\logs\verify_0829.log"

# (task_id, 账号, 座位, 开始, 结束, 预约号)
TASKS = [
    (34, "xiongjt", "104", "09:00", "11:00", 189646233),
    (37, "zhaozh", "105", "09:00", "11:00", 189646281),
    (32, "wangh", "104", "15:00", "17:00", 189646184),
    (35, "xiongjt", "105", "15:00", "17:00", 189646249),
    (38, "zhaozh", "030", "15:30", "17:00", 189684296),
    (33, "wangh", "105", "19:00", "21:00", 189646208),
    (36, "zhaozh", "104", "19:00", "21:00", 189646266),
    (39, "xiongjt", "030", "19:00", "21:00", 189684316),
]

TOMORROW = [
    ("wangh", "030", "09:00", "11:00"),
    ("xiongjt", "031", "09:00", "11:00"),
    ("wangh", "031", "15:00", "17:00"),
    ("zhaozh", "030", "15:30", "17:00"),
    ("zhaozh", "031", "19:00", "21:00"),
    ("xiongjt", "030", "19:00", "21:00"),
]


def _expected(now: str, start: str, end: str) -> str:
    """按当前时刻推导期望状态：开始前 active，开始后 signed，结束后 complete。"""
    signback_at = f"{end[:3]}{int(end[3:5]) + 1:02d}"  # 签退约在时段结束 1 分钟后
    if now < start:
        return "active"
    if now < signback_at:
        return "signed"
    return "complete"


def main() -> int:
    now = datetime.now().strftime("%H:%M")
    lines = [f"\n===== 自检 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====="]
    problems = 0

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    for tid, acc, seat, start, end, rid in TASKS:
        row = cur.execute("SELECT status, last_error FROM tasks WHERE id=?", (tid,)).fetchone()
        if not row:
            lines.append(f"[缺] #{tid} {acc}/{seat} {start}-{end}: 任务行不存在")
            problems += 1
            continue
        exp = _expected(now, start, end)
        mark = "OK " if row["status"] == exp else "异常"
        if row["status"] != exp:
            problems += 1
        err = f" last_error={row['last_error']}" if row["last_error"] else ""
        lines.append(f"[{mark}] #{tid} {acc}/{seat} {start}-{end} #{rid}: "
                     f"期望={exp} 实际={row['status']}{err}")

    if now >= "14:05":
        rows = cur.execute(
            "SELECT account_id, seat_num, start_time, status, reserve_id "
            "FROM tasks WHERE day='2026-08-30' ORDER BY seat_num, start_time"
        ).fetchall()
        got = {(r["account_id"], r["seat_num"], r["start_time"]) for r in rows}
        want = {(a, s, st) for a, s, st, _ in TOMORROW}
        if got == want and all(r["status"] == "active" and r["reserve_id"] for r in rows):
            lines.append(f"[OK ] 明日(08-30) {len(rows)} 条预约全部在库且已提交")
        else:
            problems += 1
            for r in rows:
                lines.append(f"[异常] 明日 {r['account_id']}/{r['seat_num']} {r['start_time']} "
                             f"status={r['status']} reserve_id={r['reserve_id']}")
            for miss in sorted(want - got):
                lines.append(f"[缺] 明日应有一但不存在: {miss}")

    midnight_ms = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    for r in cur.execute(
        "SELECT ts, level, account_id, message FROM logs "
        "WHERE level IN ('WARN','ERROR') AND ts >= ? ORDER BY ts DESC LIMIT 5",
        (midnight_ms,),
    ):
        lines.append(f"[日志] {r['level']} {r['account_id']}: {r['message'][:120]}")

    con.close()
    lines.append(f"===== 结论: {'全部正常' if problems == 0 else f'{problems} 项异常，请人工跟进'} =====")
    report = "\n".join(lines)
    print(report)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(report + "\n")
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
