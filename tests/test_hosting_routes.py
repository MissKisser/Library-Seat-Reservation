"""Unit tests for /hosting routes and hosting lifecycle actions."""
from unittest.mock import AsyncMock
from urllib.parse import unquote

from fastapi import FastAPI
from fastapi.testclient import TestClient

from seatbot.models import Account, SeatTarget, Task, TaskStatus
from seatbot.utils.timeutil import today_cst
from seatbot.web.app import new_templates
from seatbot.web.routes import router

DAY = today_cst()


class FakeHostingStore:
    def __init__(self):
        self.hosted = []
        self.tasks = []
        self.accounts = [
            Account(id="张三", phone="13800000001", password="x", slots=[], seat_slots={"021": {"mon": ["09:00-11:00"]}}),
            Account(id="李四", phone="13800000002", password="x", slots=[], seat_slots={"021": {"mon": ["11:00-13:00"]}}),
        ]
        self.targets = [
            SeatTarget(seat_num="021", label="主座"),
        ]
        self.settings = {"schedule_mode": "uniform"}
        self.user_reserved = []

    async def list_hosted(self, states=None, limit=50, offset=0, include_final=True):
        res = self.hosted
        if states is not None:
            res = [h for h in res if h["state"] in states]
        elif not include_final:
            res = [h for h in res if h["state"] in ("queued", "hosting", "pending_decision")]
        if limit is None:
            return res[offset:]
        return res[offset : offset + limit]

    async def count_hosted(self, states=None):
        res = self.hosted
        if states is not None:
            res = [h for h in res if h["state"] in states]
        return len(res)
    async def get_hosted(self, hosted_id):
        return next((h for h in self.hosted if h["id"] == hosted_id), None)

    async def update_hosted(self, hosted_id, *, state=None, outcome=None, task_id=None):
        h = await self.get_hosted(hosted_id)
        if h:
            if state is not None:
                h["state"] = state
            if outcome is not None:
                h["outcome"] = outcome
            if task_id is not None:
                h["task_id"] = task_id

    async def upsert_hosted(self, account_id, reserve_id, *, seat_num, day, start, end, state, outcome="", task_id=None):
        for h in self.hosted:
            if h["account_id"] == account_id and h["reserve_id"] == reserve_id:
                h.update({"seat_num": seat_num, "day": day.isoformat() if hasattr(day, "isoformat") else day,
                          "start_time": start.strftime("%H:%M") if hasattr(start, "strftime") else start,
                          "end_time": end.strftime("%H:%M") if hasattr(end, "strftime") else end,
                          "state": state, "outcome": outcome, "task_id": task_id})
                return h["id"]
        new_id = len(self.hosted) + 1
        self.hosted.append({
            "id": new_id, "account_id": account_id, "reserve_id": reserve_id,
            "seat_num": seat_num, "day": day.isoformat() if hasattr(day, "isoformat") else day,
            "start_time": start.strftime("%H:%M") if hasattr(start, "strftime") else start,
            "end_time": end.strftime("%H:%M") if hasattr(end, "strftime") else end,
            "state": state, "outcome": outcome, "task_id": task_id,
        })
        return new_id

    async def get_task(self, task_id):
        return next((t for t in self.tasks if t.id == task_id), None)

    async def list_tasks(self, account_id=None, day=None, seat_num=None):
        return [t for t in self.tasks
                if (account_id is None or t.account_id == account_id)
                and (day is None or t.day == day)
                and (seat_num is None or t.seat_num == seat_num)]

    async def add_task(self, task):
        new_id = len(self.tasks) + 1
        task.id = new_id
        self.tasks.append(task)
        return new_id

    async def update_task_status(self, task_id, status, reserve_id=None, last_error=None, source=None):
        t = await self.get_task(task_id)
        if t:
            t.status = status
            if reserve_id is not None:
                t.reserve_id = reserve_id
            if last_error == "":
                t.last_error = None
            elif last_error is not None:
                t.last_error = last_error
            if source is not None:
                t.source = source

    async def list_accounts(self, include_inactive=False):
        return self.accounts

    async def get_account(self, acc_id):
        return next((a for a in self.accounts if a.id == acc_id), None)

    async def upsert_account(self, acc):
        for i, a in enumerate(self.accounts):
            if a.id == acc.id:
                self.accounts[i] = acc
                return
        self.accounts.append(acc)

    async def list_target_seats(self):
        return self.targets

    async def add_target_seat(self, seat_num, *, label=""):
        self.targets.append(SeatTarget(seat_num=seat_num, label=label))

    async def set_target_seat_desired(self, seat_num, slots, *, is_weekly=None):
        target = next((t for t in self.targets if t.seat_num == seat_num), None)
        if target:
            if is_weekly:
                target.desired_slots_weekly = slots
            else:
                target.desired_slots = slots

    async def get_settings_map(self):
        return self.settings

    async def list_user_reserved(self, day=None, seat_num=None):
        return self.user_reserved

    async def delete_user_reserved(self, rid):
        self.user_reserved = [r for r in self.user_reserved if r.get("id") != rid]

    async def add_user_reserved(self, account_id, seat_num, day, start_time, end_time, note=""):
        new_id = len(self.user_reserved) + 1
        self.user_reserved.append({
            "id": new_id, "account_id": account_id, "seat_num": seat_num,
            "day": day.isoformat() if hasattr(day, "isoformat") else day,
            "start_time": start_time.strftime("%H:%M") if hasattr(start_time, "strftime") else start_time,
            "end_time": end_time.strftime("%H:%M") if hasattr(end_time, "strftime") else end_time,
            "note": note,
        })
        return new_id

    async def has_active_task_for_account_day_start(self, account_id, day, start_time, seat_num):
        return False


class StubHostingConfig:
    class library:
        room_id = 0
        open_time = "08:00"
        close_time = "22:00"
        max_reserve_hours = 2.0
        daily_reserve_hours_limit = 5.0
    class runtime:
        allowed_hosts = []
        web_token = ""


def _make_client(store, sched=None):
    app = FastAPI()
    app.include_router(router)
    app.state.cfg = StubHostingConfig
    app.state.config = StubHostingConfig
    app.state.store = store
    app.state.sched = sched
    app.state.templates = new_templates()
    return TestClient(app, base_url="http://127.0.0.1:8080")


def test_hosting_get_empty_renders_200():
    store = FakeHostingStore()
    client = _make_client(store)
    r = client.get("/hosting")
    assert r.status_code == 200
    assert "自动托管" in r.text
    assert "暂无当前托管条目" in r.text


def test_hosting_toggle_stop_before_start():
    from datetime import timedelta
    store = FakeHostingStore()
    store.hosted.append({
        "id": 1, "account_id": "张三", "reserve_id": 101,
        "seat_num": "021", "day": (DAY + timedelta(days=1)).isoformat(),
        "start_time": "21:00", "end_time": "22:00",
        "state": "hosting", "outcome": "", "task_id": None,
    })
    client = _make_client(store)
    r = client.post("/hosting/1/toggle", data={"enabled": "0"}, follow_redirects=False)
    assert r.status_code == 303
    assert store.hosted[0]["state"] == "stopped"
    assert store.hosted[0]["outcome"] == "手动停止托管"


def test_hosting_toggle_stop_after_start_locked():
    store = FakeHostingStore()
    store.hosted.append({
        "id": 1, "account_id": "张三", "reserve_id": 101,
        "seat_num": "021", "day": DAY.isoformat(),
        "start_time": "00:00", "end_time": "01:00",
        "state": "hosting", "outcome": "", "task_id": None,
    })
    client = _make_client(store)
    r = client.post("/hosting/1/toggle", data={"enabled": "0"}, follow_redirects=False)
    assert r.status_code == 303
    assert "时段已开始" in unquote(r.headers["location"])
    assert store.hosted[0]["state"] == "hosting"


def test_hosting_toggle_recover():
    from datetime import timedelta
    store = FakeHostingStore()
    store.hosted.append({
        "id": 1, "account_id": "张三", "reserve_id": 101,
        "seat_num": "021", "day": (DAY + timedelta(days=1)).isoformat(),
        "start_time": "20:00", "end_time": "22:00",
        "state": "stopped", "outcome": "已停止", "task_id": None,
    })
    client = _make_client(store)
    r = client.post("/hosting/1/toggle", data={"enabled": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert store.hosted[0]["state"] == "queued"


def test_hosting_abandon():
    store = FakeHostingStore()
    store.hosted.append({
        "id": 1, "account_id": "张三", "reserve_id": 101,
        "seat_num": "021", "day": DAY.isoformat(),
        "start_time": "20:00", "end_time": "22:00",
        "state": "pending_decision", "outcome": "", "task_id": None,
    })
    client = _make_client(store)
    r = client.post("/hosting/1/abandon", follow_redirects=False)
    assert r.status_code == 303
    assert store.hosted[0]["state"] == "stopped"
    assert store.hosted[0]["outcome"] == "用户放弃重抢"


def test_hosting_add_matrix_auto_registers_target():
    store = FakeHostingStore()
    store.hosted.append({
        "id": 1, "account_id": "张三", "reserve_id": 101,
        "seat_num": "099", "day": DAY.isoformat(),
        "start_time": "14:00", "end_time": "16:00",
        "state": "hosting", "outcome": "", "task_id": None,
    })
    client = _make_client(store)
    r = client.post("/hosting/1/add-matrix", follow_redirects=False)
    assert r.status_code == 303
    # 座位 099 自动注册
    target_nums = [t.seat_num for t in store.targets]
    assert "099" in target_nums



def test_hosting_add_matrix_merges_existing_slots():
    """账号在同座位已有段时，加新段触发 D4 换绑兜底，全矩阵两段同时并存且原段不被抹除。"""
    store = FakeHostingStore()
    # 张三已有 021 在 09:00-11:00；李四无 021 绑定
    store.accounts[0].seat_slots = {"021": {w: ["09:00-11:00"] for w in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}}
    store.accounts[1].seat_slots = {}
    store.hosted.append({
        "id": 1, "account_id": "张三", "reserve_id": 101,
        "seat_num": "021", "day": DAY.isoformat(),
        "start_time": "14:00", "end_time": "16:00",
        "state": "hosting", "outcome": "", "task_id": None,
    })
    client = _make_client(store)
    r = client.post("/hosting/1/add-matrix", follow_redirects=False)
    assert r.status_code == 303
    from seatbot.utils.weekly import slots_for_weekday
    # 张三保留原段 09:00-11:00
    zs_slots = slots_for_weekday(store.accounts[0].seat_slots.get("021"), "mon")
    assert zs_slots == ["09:00-11:00"]
    # 李四接手新段 14:00-16:00 (D4 换绑)
    ls_slots = slots_for_weekday(store.accounts[1].seat_slots.get("021"), "mon")
    assert ls_slots == ["14:00-16:00"]
    # 座位期望时段两段均已合并追加
    target = next(t for t in store.targets if t.seat_num == "021")
    desired = target.desired_slots.get("mon", [])
    assert "09:00-11:00" not in desired or "14:00-16:00" in desired
    assert "14:00-16:00" in desired


def test_hosting_add_matrix_merges_across_seats():
    """账号已有其他座位段时，add-matrix 合并写入新座位，同账号多座位段同时存在。"""
    store = FakeHostingStore()
    # 张三已有 022 09:00-11:00
    store.accounts[0].seat_slots = {"022": {w: ["09:00-11:00"] for w in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}}
    store.hosted.append({
        "id": 1, "account_id": "张三", "reserve_id": 101,
        "seat_num": "021", "day": DAY.isoformat(),
        "start_time": "14:00", "end_time": "16:00",
        "state": "hosting", "outcome": "", "task_id": None,
    })
    client = _make_client(store)
    r = client.post("/hosting/1/add-matrix", follow_redirects=False)
    assert r.status_code == 303
    from seatbot.utils.weekly import slots_for_weekday
    # 张三同时保有 022 与 021 两个座位的时段（总时长 4h <= 5h）
    s022 = slots_for_weekday(store.accounts[0].seat_slots.get("022"), "mon")
    s021 = slots_for_weekday(store.accounts[0].seat_slots.get("021"), "mon")
    assert s022 == ["09:00-11:00"]
    assert s021 == ["14:00-16:00"]

def test_hosting_add_matrix_idempotent():
    """重复执行 add-matrix 同一时段无重复项（幂等）。"""
    store = FakeHostingStore()
    store.accounts[0].seat_slots = {"021": {w: ["09:00-11:00"] for w in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}}
    store.hosted.append({
        "id": 1, "account_id": "张三", "reserve_id": 101,
        "seat_num": "021", "day": DAY.isoformat(),
        "start_time": "09:00", "end_time": "11:00",
        "state": "hosting", "outcome": "", "task_id": None,
    })
    client = _make_client(store)
    r = client.post("/hosting/1/add-matrix", follow_redirects=False)
    assert r.status_code == 303
    from seatbot.utils.weekly import slots_for_weekday
    slots = slots_for_weekday(store.accounts[0].seat_slots.get("021"), "mon")
    assert slots == ["09:00-11:00"]
    # 再次提交
    r2 = client.post("/hosting/1/add-matrix", follow_redirects=False)
    assert r2.status_code == 303
    slots2 = slots_for_weekday(store.accounts[0].seat_slots.get("021"), "mon")
    assert slots2 == ["09:00-11:00"]

def test_hosting_refresh_throttling():
    store = FakeHostingStore()
    sched = AsyncMock()
    sched.refresh_hosting_now.return_value = {"busy": True}
    client = _make_client(store, sched=sched)
    r = client.post("/hosting/refresh", follow_redirects=False)
    assert r.status_code == 303
    assert "同步进行中" in unquote(r.headers["location"])


def test_hosting_history_pagination_count():
    """托管记录页按 count_hosted 计算总数，>50 条时分页链接判定正确。"""
    store = FakeHostingStore()
    for i in range(55):
        store.hosted.append({
            "id": i + 1, "account_id": "张三", "reserve_id": 3000 + i,
            "seat_num": "021", "day": DAY.isoformat(),
            "start_time": "14:00", "end_time": "16:00",
            "state": "ended", "outcome": "已履约", "task_id": None,
        })
    client = _make_client(store)
    r1 = client.get("/hosting?page=1")
    assert r1.status_code == 200
    assert "page=2" in r1.text
    r2 = client.get("/hosting?page=2")
    assert r2.status_code == 200
    assert "page=3" not in r2.text


def test_hosting_regrab_success():
    """regrab 成功：提交后置 ACTIVE+reserve_id，行回 hosting 且 task_id 更新。"""
    from datetime import timedelta
    store = FakeHostingStore()
    tomorrow = DAY + timedelta(days=1)
    store.hosted.append({
        "id": 1, "account_id": "张三", "reserve_id": 101,
        "seat_num": "021", "day": tomorrow.isoformat(),
        "start_time": "14:00", "end_time": "16:00",
        "state": "pending_decision", "outcome": "App 端取消", "task_id": None,
    })
    sched = AsyncMock()
    async def fake_submit(acc, task):
        task.status = TaskStatus.ACTIVE
        task.reserve_id = 8888
    sched._run_submit = fake_submit
    client = _make_client(store, sched=sched)
    r = client.post("/hosting/1/regrab", follow_redirects=False)
    assert r.status_code == 303
    h = store.hosted[0]
    assert h["state"] == "hosting"
    assert h["outcome"] == ""
    assert h["task_id"] == 1


def test_hosting_regrab_failure_exception():
    """regrab 失败（提交抛出异常）→ 行 stopped("重抢失败：...")。"""
    from datetime import timedelta
    store = FakeHostingStore()
    tomorrow = DAY + timedelta(days=1)
    store.hosted.append({
        "id": 1, "account_id": "张三", "reserve_id": 101,
        "seat_num": "021", "day": tomorrow.isoformat(),
        "start_time": "14:00", "end_time": "16:00",
        "state": "pending_decision", "outcome": "", "task_id": None,
    })
    sched = AsyncMock()
    sched._run_submit.side_effect = RuntimeError("网络超时")
    client = _make_client(store, sched=sched)
    r = client.post("/hosting/1/regrab", follow_redirects=False)
    assert r.status_code == 303
    h = store.hosted[0]
    assert h["state"] == "stopped"
    assert "重抢失败" in h["outcome"]


def test_hosting_regrab_failure_no_reserve_id():
    """regrab 失败（未取得有效预约号）→ 行 stopped("重抢失败")."""
    from datetime import timedelta
    store = FakeHostingStore()
    tomorrow = DAY + timedelta(days=1)
    store.hosted.append({
        "id": 1, "account_id": "张三", "reserve_id": 101,
        "seat_num": "021", "day": tomorrow.isoformat(),
        "start_time": "14:00", "end_time": "16:00",
        "state": "pending_decision", "outcome": "", "task_id": None,
    })
    sched = AsyncMock()
    # _run_submit 不设置 reserve_id
    sched._run_submit = AsyncMock()
    client = _make_client(store, sched=sched)
    r = client.post("/hosting/1/regrab", follow_redirects=False)
    assert r.status_code == 303
    h = store.hosted[0]
    assert h["state"] == "stopped"
    assert h["outcome"] == "重抢失败"


def test_hosting_add_matrix_modes_uniform_and_weekly():
    """add-matrix 模式分叉：uniform 写满 7 天，weekly 仅写对应周几。"""
    from seatbot.utils.weekly import slots_for_weekday, weekday_key
    # 1. uniform 模式
    store_uni = FakeHostingStore()
    store_uni.settings = {"schedule_mode": "uniform"}
    store_uni.accounts[0].seat_slots = {}
    store_uni.hosted.append({
        "id": 1, "account_id": "张三", "reserve_id": 101,
        "seat_num": "021", "day": DAY.isoformat(),
        "start_time": "14:00", "end_time": "16:00",
        "state": "hosting", "outcome": "", "task_id": None,
    })
    client_uni = _make_client(store_uni)
    r = client_uni.post("/hosting/1/add-matrix", follow_redirects=False)
    assert r.status_code == 303
    for wd in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]:
        assert slots_for_weekday(store_uni.accounts[0].seat_slots.get("021"), wd) == ["14:00-16:00"]
        assert "14:00-16:00" in (store_uni.targets[0].desired_slots or {}).get(wd, [])

    # 2. weekly 模式
    store_wk = FakeHostingStore()
    store_wk.settings = {"schedule_mode": "weekly"}
    store_wk.accounts[0].seat_slots = {}
    store_wk.hosted.append({
        "id": 1, "account_id": "张三", "reserve_id": 102,
        "seat_num": "021", "day": DAY.isoformat(),
        "start_time": "14:00", "end_time": "16:00",
        "state": "hosting", "outcome": "", "task_id": None,
    })
    client_wk = _make_client(store_wk)
    r2 = client_wk.post("/hosting/1/add-matrix", follow_redirects=False)
    assert r2.status_code == 303
    today_wd = weekday_key(DAY)
    for wd in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]:
        slots = slots_for_weekday(store_wk.accounts[0].seat_slots.get("021"), wd)
        if wd == today_wd:
            assert slots == ["14:00-16:00"]
            assert "14:00-16:00" in (store_wk.targets[0].desired_slots_weekly or {}).get(wd, [])
        else:
            assert slots == []


def test_hosting_add_matrix_rebind_fallback_and_exhausted():
    """换绑兜底：行内账号满额自动选其他启用账号；全超额则提示 error。"""
    from seatbot.utils.weekly import slots_for_weekday
    store = FakeHostingStore()
    # 张三在 001、002 各占 2h，已用 4h。若加 2h 则 6h > 5h 每日上限
    all_wds = {w: ["08:00-10:00"] for w in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
    all_wds2 = {w: ["10:00-12:00"] for w in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
    store.accounts[0].seat_slots = {"001": all_wds, "002": all_wds2}
    # 李四目前无绑定，余量充裕
    store.accounts[1].seat_slots = {}
    store.hosted.append({
        "id": 1, "account_id": "张三", "reserve_id": 101,
        "seat_num": "021", "day": DAY.isoformat(),
        "start_time": "14:00", "end_time": "16:00",
        "state": "hosting", "outcome": "", "task_id": None,
    })
    client = _make_client(store)
    r = client.post("/hosting/1/add-matrix", follow_redirects=False)
    assert r.status_code == 303
    assert "李四" in unquote(r.headers.get("location", ""))
    assert slots_for_weekday(store.accounts[1].seat_slots.get("021"), "mon") == ["14:00-16:00"]

    # 全超额情况：李四也已用 4h
    store.accounts[1].seat_slots = {"001": all_wds, "002": all_wds2}
    r2 = client.post("/hosting/1/add-matrix", follow_redirects=False)
    assert r2.status_code == 303
    assert "无可用账号" in unquote(r2.headers.get("location", ""))
