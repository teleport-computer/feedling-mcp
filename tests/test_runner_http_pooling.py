"""runner 打后端必须走连接池，不能用模块级 httpx.get/post。

`httpx.get(...)` 这类模块级便捷函数每次调用都会**新建一个 Client、发完即弃**，
于是每个请求都付一次完整 TCP+TLS 握手。runner 的 consumer 是常驻进程、以秒级
频率轮询后端（/v1/proactive/jobs/poll、/v1/chat/poll），所以这条路上的握手是
纯浪费——实测同一批 10 个请求，模块级 5203ms/请求 vs 共享 Client 973ms/请求。

Two invariants for the independent resident consumer:
1. 不许出现裸的 httpx.<verb>(——新增调用点必须走池化 client。
2. 客户端的 keepalive_expiry 必须 **短于** 服务端的 keepalive（gunicorn_conf.py
   = 75s）。否则客户端会留着一条服务端已经关掉的连接，把我们刚在服务端修掉的
   那个 stale-socket 竞态原样搬到客户端来。
"""

import pathlib
import re

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent
_RUNNER_FILES = [
    _REPO / "tools" / "chat_resident_consumer.py",
]

# 模块级便捷函数（httpx.get/post/...），不含 client.get 这类方法调用。
_BARE_CALL = re.compile(r"(?<![\w.])httpx\.(get|post|put|patch|delete|request|stream)\s*\(")


@pytest.mark.parametrize("path", _RUNNER_FILES, ids=lambda p: p.name)
def test_no_bare_module_level_httpx_calls(path):
    hits = [
        f"{path.name}:{i}"
        for i, line in enumerate(path.read_text().splitlines(), 1)
        if _BARE_CALL.search(line)
    ]
    assert not hits, (
        "这些调用点每次都新建 Client、零连接复用，每个请求白付一次 TLS 握手；"
        f"改用池化的模块级 client：{hits}"
    )


@pytest.mark.parametrize("path", _RUNNER_FILES, ids=lambda p: p.name)
def test_pooled_client_retires_sockets_before_the_server_does(path):
    src = path.read_text()
    expiry = re.search(r"keepalive_expiry\s*=\s*([\d.]+)", src)
    assert expiry, f"{path.name} 的池化 client 必须显式设 keepalive_expiry（httpx 默认只有 5s）"

    import importlib

    gconf = importlib.import_module("gunicorn_conf")
    assert float(expiry.group(1)) < gconf.keepalive
