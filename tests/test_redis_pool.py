"""backend/redis_pool.py 的行为测试（纯单元，不连真 Redis、不需要 DB）。

构造客户端不建立连接（redis-py 在首条命令时才连），所以可以在没有 Redis 的
机器上断言：配置校验 fail-closed、单例复用、TLS 参数正确、池上界生效。
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import redis_pool  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    # 每个用例前后都丢弃单例，避免相互串扰（env 改了但客户端被缓存）。
    redis_pool._reset_for_test()
    yield
    redis_pool._reset_for_test()


def _set_env(monkeypatch, ca_file: str, **over):
    env = {
        "REDIS_HOST": "app123-6379s.dstack-pha-prod9.phala.network",
        "REDIS_PASSWORD": "a" * 64,
        "REDIS_CA_FILE": ca_file,
    }
    env.update(over)
    for k in ("REDIS_HOST", "REDIS_PASSWORD", "REDIS_CA_FILE", "REDIS_CA_B64",
              "REDIS_PORT", "REDIS_MAX_CONNECTIONS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_redis_configured_reflects_host(monkeypatch):
    monkeypatch.delenv("REDIS_HOST", raising=False)
    assert redis_pool.redis_configured() is False
    monkeypatch.setenv("REDIS_HOST", "h")
    assert redis_pool.redis_configured() is True


def test_get_redis_raises_when_host_missing(monkeypatch):
    monkeypatch.delenv("REDIS_HOST", raising=False)
    with pytest.raises(RuntimeError, match="redis_not_configured"):
        redis_pool.get_redis()


def test_get_redis_fails_closed_without_password(monkeypatch, tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n")
    _set_env(monkeypatch, str(ca))
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="redis_password_missing"):
        redis_pool.get_redis()


def test_get_redis_fails_closed_without_ca(monkeypatch):
    _set_env(monkeypatch, ca_file="")
    monkeypatch.delenv("REDIS_CA_FILE", raising=False)
    with pytest.raises(RuntimeError, match="redis_ca_missing"):
        redis_pool.get_redis()


def _conn_kwargs(client):
    # redis-py 把连接参数存在 connection_pool.connection_kwargs。
    return client.connection_pool.connection_kwargs


def test_get_redis_builds_tls_client_and_is_a_singleton(monkeypatch, tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n")
    _set_env(monkeypatch, str(ca))

    c1 = redis_pool.get_redis()
    c2 = redis_pool.get_redis()
    assert c1 is c2, "get_redis() 必须返回同一个进程内单例（复用连接池）"

    kw = _conn_kwargs(c1)
    assert kw["host"] == "app123-6379s.dstack-pha-prod9.phala.network"
    assert kw["port"] == 443
    # TLS 必开、校验证书链 + 主机名（不能降级成「只加密不校验」）。
    # redis-py 在 ssl=True 时把 host 用作 TLS server_hostname(=SNI)，正好等于
    # 完整 gateway 主机名 → gateway 能路由到后端。
    assert kw.get("ssl_context") is not None or kw.get("ssl_cert_reqs") is not None
    assert kw["ssl_ca_certs"] == str(ca)


def test_pool_has_bounded_max_connections(monkeypatch, tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n")
    _set_env(monkeypatch, str(ca), REDIS_MAX_CONNECTIONS="7")
    c = redis_pool.get_redis()
    assert c.connection_pool.max_connections == 7


def test_port_override(monkeypatch, tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n")
    _set_env(monkeypatch, str(ca), REDIS_PORT="6380")
    assert _conn_kwargs(redis_pool.get_redis())["port"] == 6380


def test_ca_b64_is_decoded_to_a_file(monkeypatch):
    pem = b"-----BEGIN CERTIFICATE-----\nbase64path\n-----END CERTIFICATE-----\n"
    b64 = base64.b64encode(pem).decode()
    for k in ("REDIS_CA_FILE",):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("REDIS_HOST", "h-6379s.dstack-pha-prod9.phala.network")
    monkeypatch.setenv("REDIS_PASSWORD", "a" * 64)
    monkeypatch.setenv("REDIS_CA_B64", b64)

    c = redis_pool.get_redis()
    ca_path = _conn_kwargs(c)["ssl_ca_certs"]
    assert Path(ca_path).read_bytes() == pem


def test_close_redis_clears_singleton(monkeypatch, tmp_path):
    # 用 asyncio.run 而非 pytest.mark.asyncio：本仓没配 pytest-asyncio，
    # 纯单元 async 一律 asyncio.run（照抄 tests/ 里的现有用法）。
    import asyncio

    ca = tmp_path / "ca.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n")
    _set_env(monkeypatch, str(ca))
    c1 = redis_pool.get_redis()
    asyncio.run(redis_pool.close_redis())
    assert redis_pool._client is None
    c2 = redis_pool.get_redis()
    assert c2 is not c1, "close 之后应重建，不复用已关闭的客户端"
    asyncio.run(redis_pool.close_redis())
