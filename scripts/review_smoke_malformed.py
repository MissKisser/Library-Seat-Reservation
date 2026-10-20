"""畸形输入实测（只读审查用，进程内 TestClient，零网络零 cron）。

构造：全新 tmp DB + config.test.yaml（空账号）+ Scheduler 不 start()（无 cron）
+ _fetch_others_occupied_cached 打桩（零外呼）。逐端点投喂畸形输入，记录状态码。
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from seatbot.config import load_config
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore
from seatbot.web.app import make_app

results = []


def run(name, method, path, **kw):
    kw.setdefault("headers", {})
    kw["headers"].setdefault("X-Auth-Token", "review-token")
    try:
        r = getattr(c, method)(path, follow_redirects=False, **kw)
        code = r.status_code
        body = r.text[:120].replace("\n", " ")
    except Exception as e:
        code, body = "EXC", f"{type(e).__name__}: {e}"
    results.append((name, method, path, code, body))


tmp = Path(tempfile.mkdtemp(prefix="review_smoke_"))
cfg = load_config("config.test.yaml")
cfg.runtime.db_path = str(tmp / "t.db")
cfg.runtime.web_token = "review-token"
store = StateStore(cfg.runtime.db_path)
asyncio.run(store.init())
# 预置一个座位与一个账号，让正常分支可达
async def seed():
    from seatbot.models import Account
    await store.add_target_seat("001", label="t")
    await store.upsert_account(Account(id="a1", phone="13800000000", password="x", slots=[]))
asyncio.run(seed())
sched = Scheduler(cfg, store)  # 不 start() —— 无 cron、无 tick
app = make_app(cfg, store, sched)

import seatbot.web.routes as R
async def fake_fetch(request, store_, day, seat_nums, fresh=False):
    return [], None
R._fetch_others_occupied_cached = fake_fetch

c = TestClient(app, base_url="http://127.0.0.1:8080")

# ---------- GET 页面 ----------
for p in ["/", "/bindings", "/targets", "/accounts", "/accounts/new",
          "/accounts/a1/edit", "/tasks", "/settings", "/audit", "/logs",
          "/reservations", "/api/dashboard-data", "/api/version", "/api/status",
          "/api/tasks?day=garbage", "/tasks?day=2026-13-99"]:
    run("", "get", p)

# ---------- targets ----------
run("targets-空", "post", "/targets", data={})
run("targets-非数字", "post", "/targets", data={"seat_num": "abc", "label": "x"})
run("targets-超长", "post", "/targets", data={"seat_num": "1234567890", "label": "x" * 500})
run("targets-delete-未知", "post", "/targets/999/delete", data={})
run("replace-缺字段", "post", "/targets/replace", data={})
run("replace-旧席不存在", "post", "/targets/replace", data={"old_seat": "998", "new_seat": "999"})
run("replace-新席非法", "post", "/targets/replace", data={"old_seat": "001", "new_seat": "xyz"})
run("replace-新旧相同", "post", "/targets/replace", data={"old_seat": "001", "new_seat": "001"})

# ---------- bindings ----------
run("mode-非法", "post", "/bindings/mode", data={"mode": "monthly"})
run("mode-空", "post", "/bindings/mode", data={})
run("manual-未知账号", "post", "/bindings/manual", data={"account_id": "nobody", "seat_num": "001", "start": "09:00", "end": "10:00"})
run("manual-坏时间", "post", "/bindings/manual", data={"account_id": "a1", "seat_num": "001", "start": "9:0", "end": "10:00"})
run("manual-逆序时间", "post", "/bindings/manual", data={"account_id": "a1", "seat_num": "001", "start": "10:00", "end": "09:00"})
run("manual-超2h", "post", "/bindings/manual", data={"account_id": "a1", "seat_num": "001", "start": "09:00", "end": "12:00"})
run("manual-未知座位", "post", "/bindings/manual", data={"account_id": "a1", "seat_num": "888", "start": "09:00", "end": "10:00"})
run("manual-非法星期", "post", "/bindings/manual", data={"account_id": "a1", "seat_num": "001", "start": "09:00", "end": "10:00", "weekday": "funday"})
run("desired-空块", "post", "/bindings/desired", data={"seat_num": "001", "mode": "uniform"})
run("desired-坏块", "post", "/bindings/desired", data={"seat_num": "001", "mode": "uniform", "blocks": ["abc"]})
run("desired-越界块", "post", "/bindings/desired", data={"seat_num": "001", "mode": "uniform", "blocks": ["23:30"]})
run("desired-超长字符串", "post", "/bindings/desired", data={"seat_num": "0" * 300, "mode": "uniform", "blocks": ["09:00"] * 200})
run("desired-未知座位", "post", "/bindings/desired", data={"seat_num": "777", "mode": "weekly", "blocks_mon": ["09:00"]})
run("delete-无绑定", "post", "/bindings/delete", data={"account_id": "a1", "seat_num": "001"})
run("delete-未知账号", "post", "/bindings/delete", data={"account_id": "ghost", "seat_num": "001"})
run("auto-空", "post", "/bindings/auto", data={})
run("replan-preview-空", "post", "/bindings/replan/preview", data={})
run("replan-apply-空", "post", "/bindings/replan/apply", data={})

# ---------- accounts ----------
run("acc-create-坏手机号", "post", "/accounts", data={"phone": "abc", "password": "p"})
run("acc-create-短手机号", "post", "/accounts", data={"phone": "1", "password": "p"})
run("acc-create-空", "post", "/accounts", data={})
run("acc-update-坏JSON", "post", "/accounts/a1", data={"phone": "13800000000", "seat_slots": "{bad json"})
run("acc-update-非法矩阵", "post", "/accounts/a1", data={"phone": "13800000000", "seat_slots": '{"001": ["18:00-21:00"]}'})
run("acc-update-未知账号", "post", "/accounts/ghost", data={"phone": "13800000000", "seat_slots": "{}"})
run("acc-delete-未知", "post", "/accounts/ghost/delete", data={})

# ---------- tasks ----------
run("quick-未知账号", "post", "/tasks/quick-reserve", data={"account_id": "ghost", "seat_num": "001", "start": "09:00", "end": "10:00"})
run("quick-坏时间", "post", "/tasks/quick-reserve", data={"account_id": "a1", "seat_num": "001", "start": "xx", "end": "10:00"})
run("quick-未知座位", "post", "/tasks/quick-reserve", data={"account_id": "a1", "seat_num": "777", "start": "09:00", "end": "10:00"})
run("quick-缺字段", "post", "/tasks/quick-reserve", data={})
run("import-缺字段", "post", "/tasks/import", data={})
run("import-坏日期", "post", "/tasks/import", data={"account_id": "a1", "seat_num": "001", "day": "notadate", "start": "09:00", "end": "10:00", "reserve_id": "123"})
run("import-非数字rid", "post", "/tasks/import", data={"account_id": "a1", "seat_num": "001", "day": "2026-09-04", "start": "09:00", "end": "10:00", "reserve_id": "abc"})
run("import-负rid", "post", "/tasks/import", data={"account_id": "a1", "seat_num": "001", "day": "2026-09-04", "start": "09:00", "end": "10:00", "reserve_id": "-5"})
run("import-过去日期", "post", "/tasks/import", data={"account_id": "a1", "seat_num": "001", "day": "2020-01-01", "start": "09:00", "end": "10:00", "reserve_id": "1"})
run("reassign-未知任务", "post", "/tasks/99999/reassign", data={"account_id": "a1"})
run("sign-未知任务", "post", "/tasks/99999/sign", data={})
run("cancel-未知任务", "post", "/tasks/99999/cancel", data={})
run("leave-未知任务", "post", "/tasks/99999/leave", data={})

# ---------- settings ----------
run("settings-全空", "post", "/settings", data={})
run("settings-非法策略", "post", "/settings", data={"submit_strategy": "yolo"})
run("settings-越界relay", "post", "/settings", data={"relay_lead_seconds": "99999"})
run("settings-越界max", "post", "/settings", data={"max_reserve_hours": "99"})
run("settings-坏webhook", "post", "/settings", data={"notify_webhook": "ftp://x"})
run("settings-坏mode", "post", "/settings", data={"schedule_mode": "hourly"})
run("settings-坏stagger", "post", "/settings", data={"stagger_seconds": "5,1"})
run("settings-reset", "post", "/settings/reset", data={})

# ---------- notifications / audit ----------
run("notif-dismiss-404", "post", "/api/notifications/424242/dismiss", data={})
run("notif-dismissall", "post", "/api/notifications/dismiss-all", data={})
run("notif-dismiss-非数字", "post", "/api/notifications/abc/dismiss", data={})
run("audit-check", "post", "/audit/check", data={})

asyncio.run(store.close())

print(f"{'端点':<28} {'方法':<5} {'码':<5} 备注")
bad = []
for name, method, path, code, body in results:
    flag = ""
    if code == 500 or code == "EXC":
        flag = "  <<< 500/EXC"
        bad.append((name, path, code, body))
    print(f"{(name or path):<28} {method:<5} {str(code):<5}{flag}")
print(f"\n总计 {len(results)} 例，500/异常 {len(bad)} 例")
for name, path, code, body in bad:
    print("BAD:", name, path, code, body[:300])
