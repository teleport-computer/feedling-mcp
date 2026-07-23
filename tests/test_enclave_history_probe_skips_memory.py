"""decrypt-health 探活必须是轻量的:GET /v1/chat/history?probe=1 只验证解密
路径可达,不能触发 enclave 给普通 history 读附带的记忆卡片 fan-out。

背景:resident consumer 每 DECRYPT_HEALTH_REFRESH_SEC(默认 120s)对 enclave 打
一次 /v1/chat/history?limit=1 做解密健康探活。enclave 的该端点对每次读都无条件
`create_task(backend_get("/v1/memory/list", limit=200))` + `_build_context_memories`
(线程池),即使 limit=1。全生产约 200 个 consumer ⇒ 每 120s 一波「拉 200 卡片 +
context 构建」砸在 enclave 单忙路径上,而探活只读 HTTP 状态、根本不看这些卡片。

修复:探活带 probe=1,enclave 见到就跳过整个 memory fan-out,只保留 limit=1 的
解密往返(探活真正要验证的)。iOS / model_api 调用方不带 probe,行为不变。

Run:  python -m pytest tests/test_enclave_history_probe_skips_memory.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402

from asgi_test_client import _AsgiTestClient  # noqa: E402
from enclave import auth as enclave_auth  # noqa: E402
from enclave import backend_client, keys  # noqa: E402
from enclave import state as enclave_state  # noqa: E402
from enclave.routes import build_app  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    enclave_auth.reset_cache()
    return _AsgiTestClient(build_app())


@pytest.fixture()
def called_paths(monkeypatch):
    """Record every backend path the enclave hits while serving the request."""
    called: list[str] = []

    async def fake_backend_get(path, headers, params=None):
        called.append(path)
        if path == "/v1/users/whoami":
            return {"user_id": "usr_a"}
        if path == "/v1/chat/history":
            return {"messages": [], "total": 0}
        if path == "/v1/memory/list":
            return {"moments": [], "total": 0}
        return {}

    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)

    async def fake_sk():
        return object()

    monkeypatch.setattr(keys, "get_content_sk", fake_sk)
    return called


def test_probe_skips_the_memory_fanout(client, called_paths):
    r = client.get("/v1/chat/history?limit=1&probe=1", headers={"X-API-Key": "k"})
    assert r.status_code == 200, r.body
    assert "/v1/chat/history" in called_paths, "探活仍必须走一次解密往返"
    assert "/v1/memory/list" not in called_paths, (
        f"probe=1 不应触发记忆 fan-out,实际调用了:{called_paths}"
    )


def test_normal_read_still_fans_out(client, called_paths):
    """对照组:不带 probe 的普通 history 读仍附带记忆卡片(契约不变)。"""
    r = client.get("/v1/chat/history?limit=1", headers={"X-API-Key": "k"})
    assert r.status_code == 200, r.body
    assert "/v1/memory/list" in called_paths, (
        f"普通读应仍拉记忆卡片,实际:{called_paths}"
    )
