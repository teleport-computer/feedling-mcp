# tests/test_enclave_body_helper.py
"""enclave read_json_payload 必须与主 backend 的 asgi.http.read_json_silent
同一契约（都复刻 Flask get_json(silent=True)）——两份手写实现已出现漂移：

1. +json 门槛：Flask/Werkzeug 的 is_json 只认 ``application/*+json``；enclave
   副本此前接受任意 ``*+json``（如 text/vnd.foo+json）。
2. ClientDisconnect：主 backend 在 prod 500 噪音事故后显式吞掉客户端中途断开
   （iOS 后台上报）；enclave 副本靠裸 except 顺带盖住，一旦重写即丢。

修法是包装复用 read_json_silent，本文件钉住这两个语义。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import asyncio  # noqa: E402

from starlette.requests import Request  # noqa: E402

from enclave.routes._body import read_json_payload  # noqa: E402


def _req(content_type: str, body: bytes = b'{"a": 1}', disconnect: bool = False) -> Request:
    scope = {
        "type": "http", "method": "POST", "path": "/",
        "headers": [(b"content-type", content_type.encode("latin-1"))],
        "query_string": b"",
    }

    async def receive():
        if disconnect:
            return {"type": "http.disconnect"}
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def test_plus_json_gate_requires_application_prefix():
    # Flask is_json：application/json 或 application/*+json；text/*+json 不算
    assert asyncio.run(read_json_payload(_req("application/json"))) == {"a": 1}
    assert asyncio.run(read_json_payload(_req("application/vnd.foo+json"))) == {"a": 1}
    assert asyncio.run(read_json_payload(_req("text/vnd.foo+json"))) == {}


def test_non_json_content_type_and_bad_body_normalize_to_empty():
    assert asyncio.run(read_json_payload(_req("text/plain"))) == {}
    assert asyncio.run(read_json_payload(_req("application/json", body=b"{bad"))) == {}
    assert asyncio.run(read_json_payload(_req("application/json", body=b"[1,2]"))) == {}


def test_client_disconnect_mid_upload_returns_empty_not_500():
    # iOS 后台上报中途断开：不得向路由层抛 ClientDisconnect（→500 噪音）
    assert asyncio.run(read_json_payload(
        _req("application/json", disconnect=True))) == {}


def test_bounded_reader_preserves_content_type_disconnect_and_exact_limit():
    import pytest
    import memory_search_contract
    assert asyncio.run(read_json_payload(_req("application/json"), max_bytes=8)) == {"a": 1}
    assert asyncio.run(read_json_payload(_req("text/plain"), max_bytes=1)) == {}
    assert asyncio.run(read_json_payload(_req("application/json", disconnect=True), max_bytes=1)) == {}
    with pytest.raises(memory_search_contract.SearchLimitExceeded):
        asyncio.run(read_json_payload(_req("application/json"), max_bytes=7))


def test_bounded_reader_counts_chunked_upload_not_content_length():
    import pytest
    import memory_search_contract
    chunks = iter([b'{"a":', b'123}'])
    async def receive():
        body = next(chunks)
        return {"type": "http.request", "body": body, "more_body": body == b'{"a":'}
    scope = {"type": "http", "headers": [(b"content-type", b"application/json"),
                                         (b"content-length", b"1")]}
    with pytest.raises(memory_search_contract.SearchLimitExceeded):
        asyncio.run(read_json_payload(Request(scope, receive), max_bytes=8))
