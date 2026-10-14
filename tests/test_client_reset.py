"""ChaoxingClient 会话重置与 cookie jar 摊平的行为测试。

覆盖:
  - reset_session 换掉底层 AsyncClient 实例并清空全部 cookie
  - 旧客户端 aclose 抛异常时不外泄, 重建结果仍然成立
  - 重建前后 User-Agent 不变
  - 换 jar 后 _cookie_jar 始终是客户端实际持有的那只 (httpx 赋值即复制)
  - set_cookies 注入的持久化 cookie 确实进入客户端
  - _rebuild_flattened_jar 把同名多域 cookie 摊平为单一权威值

全程离线: autouse 的 no_network fixture 把 httpx 的请求入口替换为直接报错,
任何真实请求都会让用例失败。
"""
from __future__ import annotations

import httpx
import pytest

from seatbot.client import ChaoxingClient


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """禁止任何用例经 httpx 发起真实网络请求。

    Returns:
        None。请求入口被替换为抛出 AssertionError 的桩。
    """

    def _blocked(*args, **kwargs):
        raise AssertionError("test attempted a real HTTP request")

    for name in ("request", "send", "get", "post", "stream"):
        monkeypatch.setattr(httpx.AsyncClient, name, _blocked, raising=False)


async def _make_client() -> ChaoxingClient:
    """构造一个带固定 User-Agent 的客户端。

    Returns: 尚未发起任何请求的 ChaoxingClient。
    """
    return ChaoxingClient(ua="seatbot-test-ua/1.0")


async def test_reset_session_replaces_client_and_clears_cookies():
    """重置后底层客户端换新实例且 cookie 全部清空。"""
    client = await _make_client()
    client.set_cookies({"_uid": "u1", "vc3": "v1"})
    old_client = client._client

    await client.reset_session()

    assert client._client is not old_client
    assert client.cookies() == {}
    assert client._client.cookies is client._cookie_jar


async def test_reset_session_swallows_close_error(monkeypatch):
    """旧客户端 aclose 抛异常时不向外传播, 且重建照常完成。"""
    client = await _make_client()
    client.set_cookies({"_uid": "u1"})
    old_client = client._client

    async def _boom():
        raise RuntimeError("close failed")

    monkeypatch.setattr(old_client, "aclose", _boom)

    await client.reset_session()

    assert client._client is not old_client
    assert client.cookies() == {}


async def test_reset_session_jar_stays_wired_to_client():
    """重置后写入 _cookie_jar 的 cookie 必须真正进入客户端。

    httpx 的 `client.cookies = jar` 会复制 jar；若换 jar 后继续往原对象写入，
    客户端将始终不携带任何 cookie，重登后的请求必然被判定为未登录。
    """
    client = await _make_client()

    await client.reset_session()
    client._cookie_jar.set("_uid", "w1", domain=".chaoxing.com", path="/")

    assert client._client.cookies is client._cookie_jar
    assert dict(client._client.cookies) == {"_uid": "w1"}


async def test_set_cookies_reaches_the_client():
    """持久化 cookie 恢复路径同样必须写进客户端实际持有的 jar。"""
    client = await _make_client()

    assert client.set_cookies({"_uid": "u9", "vc3": "v9"}) is True

    assert client._client.cookies is client._cookie_jar
    assert dict(client._client.cookies) == {"_uid": "u9", "vc3": "v9"}


async def test_reset_session_preserves_user_agent():
    """重建前后 User-Agent 与其余默认 headers 保持一致。"""
    client = await _make_client()
    before_ua = client._client.headers["User-Agent"]
    before_headers = dict(client._client.headers)

    await client.reset_session()

    assert client._client.headers["User-Agent"] == before_ua == "seatbot-test-ua/1.0"
    assert dict(client._client.headers) == before_headers


async def test_rebuild_flattened_jar_dedupes_same_name_across_domains():
    """同名多域 cookie 摊平后唯一且取权威值, 其余名字保留。"""
    client = await _make_client()
    client._cookie_jar.set("_uid", "harvested", domain=".chaoxing.com", path="/")
    client._cookie_jar.set(
        "_uid", "polluted", domain="office.chaoxing.com", path="/",
    )
    client._cookie_jar.set("extra", "kept", domain="office.chaoxing.com", path="/")

    client._rebuild_flattened_jar({"_uid": "authoritative"})

    flat = client.cookies()
    assert flat == {"_uid": "authoritative", "extra": "kept"}
    assert [c.domain for c in client._cookie_jar.jar] == [
        ".chaoxing.com", ".chaoxing.com",
    ]
    assert client._client.cookies is client._cookie_jar