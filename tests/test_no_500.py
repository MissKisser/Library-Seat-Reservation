"""页面零 500 烟雾：带令牌真实渲染全 GET 200、全 POST 空提交不 500。

占用查询外呼被打断（mock），全程零真实网络请求；配合 py_compile 卡点。
"""
import asyncio
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from seatbot.config import load_config
from seatbot.scheduler import Scheduler
from seatbot.store import StateStore
from seatbot.web.app import make_app

TOKEN = "smoke-token"


def test_py_compile_no_syntax_error():
    """改 py 后必须 py_compile 通过（拦截 NameError 前置）。"""
    for rel in ["seatbot/web/routes.py", "seatbot/store.py", "seatbot/bindings.py",
                "seatbot/manual.py", "seatbot/models.py",
                "seatbot/utils/timeutil.py"]:
        p = Path(rel)
        assert p.exists(), rel
        # 抛异常即失败
        compile(p.read_text(encoding="utf-8"), str(p), "exec")


def test_pyflakes_no_undefined_name():
    """拦截 undefined name（如误删 scope/before）。"""
    # 优先 ruff，其次 pyflakes，都没有则跳过（不阻断 CI）
    for cmd in (
        [sys.executable, "-m", "ruff", "check", "seatbot/web/routes.py", "--select", "F821"],
        [sys.executable, "-m", "pyflakes", "seatbot/web/routes.py"],
    ):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (FileNotFoundError, OSError):
            continue
        # ruff 未安装时会 1 但输出 "not found"；pyflakes 未安装同理——跳过
        if r.returncode != 0 and "No module named" in (r.stderr or ""):
            continue
        assert r.returncode == 0, f"{' '.join(cmd)}:\n{r.stdout}\n{r.stderr}"
        # ruff 成功时输出 "All checks passed!"，pyflakes 成功时 stdout 为空——
        # 两者均视为通过；其余非空输出即 undefined name 告警
        out = (r.stdout or "").strip()
        assert not out or "All checks passed" in out, f"undefined name:\n{r.stdout}"
        return
    pytest.skip("ruff/pyflakes not available")


def _client(tmp_path: Path, monkeypatch) -> tuple[TestClient, StateStore]:
    cfg = load_config("config.test.yaml")
    # 用隔离库，避免污染 seatbot.db
    cfg.runtime.db_path = str(tmp_path / "no500.db")
    cfg.runtime.web_token = TOKEN
    store = StateStore(cfg.runtime.db_path)
    asyncio.run(store.init())
    # 预置最小可用数据：2 座位 + 3 账号，避免 /bindings 因空数据 302 干扰
    async def seed():
        await store.add_target_seat("001", label="座位 001")
        await store.add_target_seat("002", label="座位 002")
        from seatbot.models import Account
        await store.upsert_account(Account(id="a1", phone="13800000000", password="x", slots=[], seat_slots={"001": {"mon": ["09:00-11:00"]}}))
        await store.upsert_account(Account(id="a2", phone="13800000000", password="x", slots=[]))
        await store.upsert_account(Account(id="a3", phone="13800000000", password="x", slots=[]))
    asyncio.run(seed())
    sched = Scheduler(cfg, store)
    app = make_app(cfg, store, sched)
    # 占用查询是页面链路唯一外呼点：mock 掉，保证烟雾测试零真实请求
    import seatbot.web.routes as R

    async def fake_fetch(request, store_, day, seat_nums, fresh=False):
        return [], None

    monkeypatch.setattr(R, "_fetch_others_occupied_cached", fake_fetch)
    # 实况核对 / 实况同步会直接打超星（getusedtimes / reservelist）：一并打桩，
    # 保证本文件全程零真实网络请求。
    async def fake_reconcile_sweep(self, write: bool = True) -> dict:
        return {"checked": 0, "mismatch": 0, "fetch_ok": True}

    async def fake_sync_user_reserved(self) -> dict:
        return {"added": 0, "pruned": 0, "adopted": 0, "queued": 0,
                "pending": 0, "ended": 0}

    monkeypatch.setattr(Scheduler, "reconcile_sweep", fake_reconcile_sweep)
    monkeypatch.setattr(Scheduler, "sync_user_reserved", fake_sync_user_reserved)
    return TestClient(app, base_url="http://127.0.0.1:8080"), store


def test_all_get_render_200(tmp_path, monkeypatch):
    c, store = _client(tmp_path, monkeypatch)
    try:
        for path in ["/", "/bindings", "/targets", "/accounts", "/tasks",
                     "/hosting", "/manual", "/settings", "/audit", "/logs",
                     "/reservations"]:
            sep = "&" if "?" in path else "?"
            r = c.get(f"{path}{sep}token={TOKEN}",
                      headers={"Host": "127.0.0.1:8080"},
                      follow_redirects=False)
            assert r.status_code == 200, \
                f"GET {path} -> {r.status_code}:\n{r.text[:600]}"
    finally:
        asyncio.run(store.close())

def test_logs_day_query_render_200(tmp_path, monkeypatch):
    c, store = _client(tmp_path, monkeypatch)
    try:
        for day_param in ["2026-09-09", "2026-01-01", "invalid-date", ""]:
            r = c.get(f"/logs?day={day_param}&token={TOKEN}",
                      headers={"Host": "127.0.0.1:8080"},
                      follow_redirects=False)
            assert r.status_code == 200, f"GET /logs?day={day_param} -> {r.status_code}"
    finally:
        asyncio.run(store.close())


def test_removed_user_reserved_page_is_404(tmp_path, monkeypatch):
    """用户已约时段页已下线：登记与查看由实况同步自动完成，无手动入口。"""
    c, store = _client(tmp_path, monkeypatch)
    try:
        r = c.get(f"/user-reserved?token={TOKEN}",
                  headers={"Host": "127.0.0.1:8080"},
                  follow_redirects=False)
        assert r.status_code == 404, r.status_code
    finally:
        asyncio.run(store.close())


#: 全量 POST 端点的空提交用例清单（覆盖 routes.py 的全部 @router.post）。
#: 路径参数一律用「不存在的 id」或「缺凭据/缺字段」形态，确保只走参数解析、
#: 查库与错误分支，绝不触发真实预约/签到/签退/登录。
POST_CASES: list[tuple[str, dict]] = [
    # ---------- targets ----------
    ("/targets", {"seat_num": "", "label": ""}),
    ("/targets/999/delete", {}),
    ("/targets/replace", {}),
    # ---------- settings ----------
    ("/settings", {}),
    ("/settings/reset", {}),
    # ---------- audit / notifications ----------
    ("/audit/check", {}),
    ("/api/notifications/dismiss-all", {}),
    ("/api/notifications/424242/dismiss", {}),
    ("/api/notifications/abc/dismiss", {}),
    # ---------- accounts ----------
    ("/accounts", {}),
    ("/accounts/a1", {}),
    ("/accounts/a2/toggle", {}),
    ("/accounts/a3/delete", {}),
    ("/accounts/ghost/test-login", {}),
    # ---------- tasks ----------
    ("/tasks/import", {}),
    ("/tasks/quick-reserve", {}),
    ("/tasks/99999/sign", {}),
    ("/tasks/99999/cancel", {}),
    ("/tasks/99999/leave", {}),
    ("/tasks/99999/reassign", {"account_id": ""}),
    # ---------- hosting ----------
    ("/hosting/refresh", {}),
    ("/hosting/1/abandon", {}),
    ("/hosting/1/add-matrix", {}),
    ("/hosting/1/regrab", {}),
    ("/hosting/1/toggle", {"enabled": "0"}),
    # ---------- manual ----------
    ("/manual/reserve", {"account_id": "", "seat_num": "",
                         "day": "", "start": "", "end": ""}),
    # ---------- bindings ----------
    ("/bindings/desired", {"seat_num": "001", "mode": "uniform"}),
    ("/bindings/desired", {"seat_num": "001", "mode": "weekly"}),
    ("/bindings/desired", {"seat_num": "__ALL__", "mode": "uniform"}),
    ("/bindings/desired", {"seat_num": "__ALL__", "mode": "weekly"}),
    ("/bindings/auto", {}),
    ("/bindings/mode", {"mode": "uniform"}),
    ("/bindings/mode", {"mode": "weekly"}),
    ("/bindings/replan/preview", {}),
    ("/bindings/replan/apply", {}),
    ("/bindings/rebind", {}),
    ("/bindings/manual", {}),
    ("/bindings/delete", {}),
    ("/bindings/assign-seat", {}),
]


def _post_route_paths() -> set[str]:
    """从 routes.py 的 AST 取出全部 @router.post 路径（路径参数归一为 *）。"""
    import ast

    tree = ast.parse(Path("seatbot/web/routes.py").read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "post" and dec.args):
                out.add(re.sub(r"\{[^}]+\}", "*", ast.literal_eval(dec.args[0])))
    return out


def _path_matches(pattern: str, concrete: str) -> bool:
    """判断具体路径是否命中带占位符的路由模式（分段比较，* 匹配任意段）。"""
    p = pattern.split("/")
    c = concrete.split("/")
    if len(p) != len(c):
        return False
    return all(pp == "*" or pp == cc for pp, cc in zip(p, c, strict=True))


def test_post_case_covers_every_route():
    """烟雾清单必须覆盖 routes.py 的全部 POST 端点（新增端点即触发失败）。"""
    declared = _post_route_paths()
    cases = [path for path, _ in POST_CASES]
    missing = sorted(
        pat for pat in declared
        if not any(_path_matches(pat, path) for path in cases)
    )
    assert not missing, f"以下 POST 端点未纳入空提交烟雾: {missing}"


def test_all_post_empty_not_500(tmp_path, monkeypatch):
    """全部 POST 端点空提交（或指向不存在资源）不得返回 500。"""
    c, store = _client(tmp_path, monkeypatch)
    try:
        for path, data in POST_CASES:
            sep = "&" if "?" in path else "?"
            r = c.post(f"{path}{sep}token={TOKEN}", data=data,
                       headers={"Host": "127.0.0.1:8080"},
                       follow_redirects=False)
            assert r.status_code != 500, \
                f"POST {path} {data} 500:\n{r.text[:800]}"
    finally:
        asyncio.run(store.close())
