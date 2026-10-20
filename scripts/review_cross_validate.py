"""A 维度交叉验证：生产库只读（uri mode=ro），离线跑 bindings 校验器。

不写任何库、不发任何网络请求。输出：矩阵约束违例、覆盖缺口（语义级）、冗余段、
绑定页字符串匹配法 vs 语义法差异。
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seatbot.bindings import validate_matrix
from seatbot.utils.timeutil import parse_range
from seatbot.utils.weekly import WEEKDAY_KEYS

con = sqlite3.connect("file:seatbot.db?mode=ro", uri=True)
cur = con.cursor()

max_seg, daily_limit = 2.0, 5.0
try:
    rows = dict(cur.execute("SELECT key, value FROM app_settings").fetchall())
    if "max_reserve_hours" in rows:
        max_seg = float(rows["max_reserve_hours"])
    if "daily_reserve_hours_limit" in rows:
        daily_limit = float(rows["daily_reserve_hours_limit"])
except Exception as e:
    print("settings 读取失败，用默认:", e)
print(f"校验参数: 单段<={max_seg}h, 每日<={daily_limit}h\n")

accounts = []
for aid, raw, status in cur.execute(
        "SELECT id, seat_slots_json, status FROM accounts").fetchall():
    if status == "disabled":
        continue
    data = json.loads(raw or "{}")
    accounts.append((aid, data))

seats = {}
for sn, uni, wk in cur.execute(
        "SELECT seat_num, desired_slots_json, desired_slots_weekly_json "
        "FROM target_seats WHERE enabled=1").fetchall():
    seats[sn] = {"uniform": json.loads(uni) if uni else None,
                 "weekly": json.loads(wk) if wk else None}

print(f"账号 {len(accounts)} 个（enabled 座位 {len(seats)} 个）")
violations = []
for aid, matrix in accounts:
    try:
        validate_matrix(matrix, max_seg_hours=max_seg, daily_limit_hours=daily_limit)
    except ValueError as e:
        violations.append(f"[矩阵违例] {aid}: {e}")

# 语义级覆盖：每座位每天 desired 区间并集 vs 绑定区间并集
def to_min(s):
    if hasattr(s, "hour"):
        return s.hour * 60 + s.minute
    h, m = s.split(":")
    return int(h) * 60 + int(m)

def union(ranges):
    ivs = sorted((to_min(s), to_min(e)) for s, e in (parse_range(r) for r in ranges))
    out = []
    for s, e in ivs:
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out

gaps, redundant, exact_mismatch = [], [], []
bound_by_seat_wd = {}
for aid, matrix in accounts:
    for seat, val in matrix.items():
        for wd in WEEKDAY_KEYS:
            day_val = val.get(wd, []) if isinstance(val, dict) else (val if isinstance(val, list) else [])
            if day_val == "full":
                continue
            for r in day_val:
                bound_by_seat_wd.setdefault((seat, wd), []).append(r)

for sn, cfgs in seats.items():
    for wd in WEEKDAY_KEYS:
        desired = cfgs["weekly"] or cfgs["uniform"] or {}
        day_val = desired.get(wd, []) if isinstance(desired, dict) else (desired if isinstance(desired, list) else [])
        if day_val == "full" or not day_val:
            continue
        want = union(day_val)
        bound = bound_by_seat_wd.get((sn, wd), [])
        got = union(bound)
        # 缺口：期望并集 − 绑定并集
        for ws, we in want:
            cov = sum(max(0, min(we, ge) - max(ws, gs)) for gs, ge in got)
            if cov < (we - ws):
                gaps.append(f"{sn} {wd} 期望 {ws//60:02d}:{ws%60:02d}-{we//60:02d}:{we%60:02d} 语义覆盖 {cov}/{we-ws} 分钟")
        # 冗余：绑定不在任何期望区间内
        for gs, ge in got:
            cov = sum(max(0, min(ge, we) - max(gs, ws)) for ws, we in want)
            if cov < (ge - gs):
                redundant.append(f"{sn} {wd} 绑定 {gs//60:02d}:{gs%60:02d}-{ge//60:02d}:{ge%60:02d} 有 {(ge-gs)-cov} 分钟不在期望内")
        # 字符串精确匹配差异（绑定页判定法）
        exact_missing = [r for r in day_val if r not in bound]
        if exact_missing:
            exact_mismatch.append(f"{sn} {wd} 字符串法报缺: {exact_missing}（语义并集可能已覆盖）")

print(f"\n=== 矩阵硬约束违例: {len(violations)} ===")
for v in violations:
    print(" ", v)
print(f"\n=== 语义覆盖缺口: {len(gaps)} ===")
for g in gaps:
    print(" ", g)
print(f"\n=== 冗余/越期望绑定段: {len(redundant)} ===")
for r in redundant:
    print(" ", r)
print(f"\n=== 字符串精确匹配法与语义法的差异（绑定页会误报的项）: {len(exact_mismatch)} ===")
for m in exact_mismatch:
    print(" ", m)

# 悬空引用：任务的账号/座位是否仍存在
dangling_acc = cur.execute(
    "SELECT DISTINCT t.account_id FROM tasks t WHERE t.status IN ('pending','ready','submitting','active','signed','leaving') "
    "AND t.account_id NOT IN (SELECT id FROM accounts WHERE status!='disabled')").fetchall()
dangling_seat = cur.execute(
    "SELECT DISTINCT t.seat_num FROM tasks t WHERE t.status IN ('pending','ready','submitting','active','signed','leaving') "
    "AND t.day >= date('now','localtime') AND t.seat_num NOT IN (SELECT seat_num FROM target_seats WHERE enabled=1)").fetchall()
print(f"\n=== 悬空引用 ===")
print("在途任务指向已删账号:", dangling_acc or "无")
print("未来任务指向已删座位:", dangling_seat or "无")
con.close()
