"""页面零 500 烟雾：带令牌真实渲染全 GET 200、全 POST 空提交不 500。

占用查询外呼被打断（mock），全程零真实网络请求；配合 py_compile 卡点。
"""
import asyncio
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
    for rel in ["seatbot/web/routes.py", "seatbot/store.py", "seatbot/bindings.py"]:
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
    # 预置最小可用数据：2 座位 + 1 账号，避免 /bindings 因空数据 302 干扰
    async def seed():
        await store.add_target_seat("021", label="座位 030")
        await store.add_target_seat("022", label="座位 031")
        from seatbot.models import Account
        await store.upsert_account(Account(id="a1", phone="13800000000", password="x", slots=[], seat_slots={"021": {"mon": ["09:00-11:00"]}}))
    asyncio.run(seed())
    sched = Scheduler(cfg, store)
    app = make_app(cfg, store, sched)
    # 占用查询是页面链路唯一外呼点：mock 掉，保证烟雾测试零真实请求
    import seatbot.web.routes as R

    async def fake_fetch(request, store_, day, seat_nums, fresh=False):
        return [], None

    monkeypatch.setattr(R, "_fetch_others_occupied_cached", fake_fetch)
    return TestClient(app, base_url="http://127.0.0.1:8080"), store


def test_all_get_render_200(tmp_path, monkeypatch):
    c, store = _client(tmp_path, monkeypatch)
    try:
        for path in ["/", "/bindings", "/targets", "/accounts", "/tasks",
                     "/settings", "/audit", "/logs", "/reservations"]:
            sep = "&" if "?" in path else "?"
            r = c.get(f"{path}{sep}token={TOKEN}",
                      headers={"Host": "127.0.0.1:8080"},
                      follow_redirects=False)
            assert r.status_code == 200, \
                f"GET {path} -> {r.status_code}:\n{r.text[:600]}"
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


def test_all_post_empty_not_500(tmp_path, monkeypatch):
    c, store = _client(tmp_path, monkeypatch)
    try:
        # 空提交：全不勾 = 清空（双列），不得 500
        cases = [
            ("/bindings/desired", {"seat_num": "021", "mode": "uniform"}),
            ("/bindings/desired", {"seat_num": "021", "mode": "weekly"}),
            ("/bindings/desired", {"seat_num": "__ALL__", "mode": "uniform"}),
            ("/bindings/desired", {"seat_num": "__ALL__", "mode": "weekly"}),
            ("/bindings/auto", {}),
            ("/bindings/mode", {"mode": "uniform"}),
            ("/bindings/mode", {"mode": "weekly"}),
            ("/bindings/replan/preview", {}),
            ("/bindings/replan/apply", {}),
        ]
        for path, data in cases:
            sep = "&" if "?" in path else "?"
            r = c.post(f"{path}{sep}token={TOKEN}", data=data,
                       headers={"Host": "127.0.0.1:8080"},
                       follow_redirects=False)
            assert r.status_code != 500, \
                f"POST {path} {data} 500:\n{r.text[:800]}"
    finally:
        asyncio.run(store.close())
