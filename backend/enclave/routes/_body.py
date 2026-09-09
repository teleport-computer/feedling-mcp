"""共享请求体读取：复刻旧 Flask ``get_json(silent=True)`` 的语义。

实现直接包装主 backend 的 ``asgi.http.read_json_silent``（同一契约：Flask 的
content-type 门槛 ``application/json`` / ``application/*+json``、解析失败归 None、
显式吞 ClientDisconnect——prod 500 噪音事故的修复），避免两份手写实现漂移；
enclave 侧只加"非 dict 归一为 {}"（与各路由既有的"非对象 body → 空负载 →
400"一致）。"""

from __future__ import annotations

from starlette.requests import Request

from asgi.http import read_json_silent
import memory_search_contract as search_contract


async def read_json_payload(request: Request, *, max_bytes: int | None = None) -> dict:
    """读请求体为 JSON dict。Content-Type 非 JSON、解析失败、客户端中途断开、
    或结果非 dict → ``{}``。"""
    if max_bytes is not None:
        received = 0
        original = request

        async def bounded_receive():
            nonlocal received
            message = await original.receive()
            received += len(message.get("body", b""))
            if received > max_bytes:
                raise search_contract.SearchLimitExceeded()
            return message

        request = Request(original.scope, receive=bounded_receive)
    payload = await read_json_silent(request)
    return payload if isinstance(payload, dict) else {}
