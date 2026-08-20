"""backend/redis_pool.py 退役门禁与保留实现的纯单元测试。"""

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


def test_redis_configured_is_false_even_with_legacy_host(monkeypatch):
    monkeypatch.delenv("REDIS_HOST", raising=False)
    assert redis_pool.redis_configured() is False
    monkeypatch.setenv("REDIS_HOST", "h")
    assert redis_pool.redis_configured() is False


def test_get_redis_is_blocked_even_with_complete_legacy_config(monkeypatch, tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n")
    _set_env(monkeypatch, str(ca))
    with pytest.raises(RuntimeError, match="redis_deprecated"):
        redis_pool.get_redis()


def test_retained_builder_fails_closed_without_password(monkeypatch, tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n")
    _set_env(monkeypatch, str(ca))
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="redis_password_missing"):
        redis_pool._build_client()


def test_retained_builder_fails_closed_without_ca(monkeypatch):
    _set_env(monkeypatch, ca_file="")
    monkeypatch.delenv("REDIS_CA_FILE", raising=False)
    with pytest.raises(RuntimeError, match="redis_ca_missing"):
        redis_pool._build_client()


def _conn_kwargs(client):
    # redis-py 把连接参数存在 connection_pool.connection_kwargs。
    return client.connection_pool.connection_kwargs


def test_retained_builder_preserves_tls_configuration(monkeypatch, tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n")
    _set_env(monkeypatch, str(ca))

    kw = _conn_kwargs(redis_pool._build_client())
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
    c = redis_pool._build_client()
    assert c.connection_pool.max_connections == 7


def test_port_override(monkeypatch, tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n")
    _set_env(monkeypatch, str(ca), REDIS_PORT="6380")
    assert _conn_kwargs(redis_pool._build_client())["port"] == 6380


def test_ca_b64_is_decoded_to_a_file(monkeypatch):
    pem = b"-----BEGIN CERTIFICATE-----\nbase64path\n-----END CERTIFICATE-----\n"
    b64 = base64.b64encode(pem).decode()
    for k in ("REDIS_CA_FILE",):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("REDIS_HOST", "h-6379s.dstack-pha-prod9.phala.network")
    monkeypatch.setenv("REDIS_PASSWORD", "a" * 64)
    monkeypatch.setenv("REDIS_CA_B64", b64)

    c = redis_pool._build_client()
    ca_path = _conn_kwargs(c)["ssl_ca_certs"]
    assert Path(ca_path).read_bytes() == pem


def test_close_redis_clears_retained_client(monkeypatch, tmp_path):
    # 用 asyncio.run 而非 pytest.mark.asyncio：本仓没配 pytest-asyncio，
    # 纯单元 async 一律 asyncio.run（照抄 tests/ 里的现有用法）。
    import asyncio

    ca = tmp_path / "ca.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n")
    _set_env(monkeypatch, str(ca))
    redis_pool._client = redis_pool._build_client()
    asyncio.run(redis_pool.close_redis())
    assert redis_pool._client is None
