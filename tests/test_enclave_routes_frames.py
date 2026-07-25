from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402

from asgi_test_client import _AsgiTestClient  # noqa: E402
from enclave import auth as enclave_auth  # noqa: E402
from enclave import backend_client, envelope as envmod, keys  # noqa: E402
from enclave import state as enclave_state  # noqa: E402
from enclave.routes import build_app  # noqa: E402

FRAME_ID = "ab" * 8
JPEG = b"\xff\xd8\xff" + bytes(range(256)) * 4  # 1027 bytes，可切块


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    enclave_auth.reset_cache()
    return _AsgiTestClient(build_app())


@pytest.fixture()
def _wired(monkeypatch):
    async def fake_backend_get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": "usr_a"}
        assert path == f"/v1/screen/frames/{FRAME_ID}/envelope"
        return {"v": 1, "K_enclave": "x", "body_ct": "x", "nonce": "x",
                "owner_user_id": "usr_a", "ts": 1.0}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)
    inner = {"image": base64.b64encode(JPEG).decode(), "image_mime": "image/jpeg",
             "ocr_text": "text on screen", "app": "Safari", "w": 100, "h": 200}
    monkeypatch.setattr(envmod, "decrypt_envelope",
                        lambda e, u, s: json.dumps(inner).encode())


def test_bad_frame_id_400(client):
    r = client.get("/v1/screen/frames/NOT-HEX/decrypt",
                   headers={"X-API-Key": "k"})
    assert r.status_code == 400
    assert r.get_json() == {"error": "bad frame id"}


def test_not_ready_precedes_frame_id_check(client, monkeypatch):
    # Old Flask order: not_ready is checked BEFORE the frame_id regex, so a
    # malformed frame_id while the enclave isn't ready must still surface as
    # 503 not_ready, not 400 bad frame id.
    monkeypatch.setitem(enclave_state._state, "ready", False)
    monkeypatch.setitem(enclave_state._state, "error", "booting")
    r = client.get("/v1/screen/frames/NOT-HEX/decrypt",
                   headers={"X-API-Key": "k"})
    assert r.status_code == 503
    assert r.get_json() == {"error": "not_ready", "detail": "booting"}


def test_decrypt_include_image_toggle(client, _wired):
    r = client.get(f"/v1/screen/frames/{FRAME_ID}/decrypt?include_image=false",
                   headers={"X-API-Key": "k"})
    body = r.get_json()
    assert body["image_b64"] is None
    assert body["image_bytes_omitted"] is True
    assert body["ocr_text"] == "text on screen"
    r = client.get(f"/v1/screen/frames/{FRAME_ID}/decrypt",
                   headers={"X-API-Key": "k"})
    assert base64.b64decode(r.get_json()["image_b64"]) == JPEG


def test_caption_unconfigured_503(client, _wired, monkeypatch):
    monkeypatch.delenv("FEEDLING_SCREEN_VLM_API_KEY", raising=False)
    r = client.get(f"/v1/screen/frames/{FRAME_ID}/caption",
                   headers={"X-API-Key": "k"})
    assert r.status_code == 503
    assert r.get_json() == {"error": "screen_caption_unconfigured"}


def test_caption_calls_async_vlm(client, _wired, monkeypatch):
    monkeypatch.setenv("FEEDLING_SCREEN_VLM_API_KEY", "vk")
    import provider_client
    seen = {}
    async def fake_async(cfg, messages, **kw):
        seen["provider"] = cfg.provider
        seen["kw"] = kw
        return {"reply": " a caption "}
    monkeypatch.setattr(provider_client, "chat_completion_async", fake_async)
    r = client.get(f"/v1/screen/frames/{FRAME_ID}/caption",
                   headers={"X-API-Key": "k"})
    assert r.status_code == 200
    assert r.get_json()["caption"] == "a caption"
    assert seen["provider"] == "openrouter"
    assert seen["kw"]["max_tokens"] == 160  # 非 full 模式


# ---- /image Range/ETag（spec §6/§7）----

def test_image_full_200(client, _wired):
    r = client.get(f"/v1/screen/frames/{FRAME_ID}/image",
                   headers={"X-API-Key": "k"})
    assert r.status_code == 200
    assert r.data == JPEG
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers.get("etag")


def test_image_single_range_206(client, _wired):
    r = client.get(f"/v1/screen/frames/{FRAME_ID}/image",
                   headers={"X-API-Key": "k", "Range": "bytes=0-99"})
    assert r.status_code == 206
    assert r.data == JPEG[:100]
    assert r.headers["content-range"] == f"bytes 0-99/{len(JPEG)}"


def test_image_parallel_chunks_reassemble(client, _wired):
    n, total = 4, len(JPEG)
    step = (total + n - 1) // n
    chunks = []
    for i in range(n):
        lo, hi = i * step, min((i + 1) * step - 1, total - 1)
        r = client.get(f"/v1/screen/frames/{FRAME_ID}/image",
                       headers={"X-API-Key": "k", "Range": f"bytes={lo}-{hi}"})
        assert r.status_code == 206
        chunks.append(r.data)
    assert b"".join(chunks) == JPEG


def test_image_suffix_range(client, _wired):
    r = client.get(f"/v1/screen/frames/{FRAME_ID}/image",
                   headers={"X-API-Key": "k", "Range": "bytes=-100"})
    assert r.status_code == 206
    assert r.data == JPEG[-100:]


def test_image_multipart_range_falls_back_200(client, _wired):
    r = client.get(f"/v1/screen/frames/{FRAME_ID}/image",
                   headers={"X-API-Key": "k", "Range": "bytes=0-9,20-29"})
    assert r.status_code == 200
    assert r.data == JPEG


def test_image_malformed_range_ignored_200(client, _wired):
    # RFC 7233：语法非法的 Range 头必须忽略、返回 200 全量（旧 Werkzeug
    # send_file(conditional=True) 行为）。"bytes=5--3" 经 partition('-') 会把
    # end 解析成 -3，修复前被当作不可满足区间 416。
    for bad in ("bytes=5--3", "bytes=+0-99", "bytes=0-+9"):
        r = client.get(f"/v1/screen/frames/{FRAME_ID}/image",
                       headers={"X-API-Key": "k", "Range": bad})
        assert r.status_code == 200, (bad, r.status_code)
        assert r.data == JPEG


def test_image_unsatisfiable_416(client, _wired):
    r = client.get(f"/v1/screen/frames/{FRAME_ID}/image",
                   headers={"X-API-Key": "k",
                            "Range": f"bytes={len(JPEG) + 10}-"})
    assert r.status_code == 416
    assert r.headers["content-range"] == f"bytes */{len(JPEG)}"


def test_image_etag_304(client, _wired):
    etag = client.get(f"/v1/screen/frames/{FRAME_ID}/image",
                      headers={"X-API-Key": "k"}).headers["etag"]
    r = client.get(f"/v1/screen/frames/{FRAME_ID}/image",
                   headers={"X-API-Key": "k", "If-None-Match": etag})
    assert r.status_code == 304


def test_image_head_supported(client, _wired):
    # spec §6 HEAD 验收（/healthz、/attestation 已在 health 测试覆盖）
    r = client.open(f"/v1/screen/frames/{FRAME_ID}/image", method="HEAD",
                    headers={"X-API-Key": "k"})
    assert r.status_code == 200
    assert r.data == b""  # Starlette 自动 HEAD：头同 GET、无 body


def _raw_asgi_request(app, method, path, headers):
    """直接驱动 ASGI app，收集真正发出的 body 字节 —— 绕开 httpx.ASGITransport
    （它在客户端把 HEAD 的 body 剥掉，会掩盖 app 层是否真的没发 body）。"""
    import asyncio

    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http", "http_version": "1.1", "method": method,
        "path": path, "raw_path": path.encode(), "query_string": b"",
        "headers": raw_headers, "scheme": "http",
        "server": ("t", 80), "client": ("c", 1),
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    hdrs = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return start["status"], body, hdrs


def test_image_head_emits_no_body_at_asgi_layer(monkeypatch, _wired):
    """HeadBodyStripMiddleware 回归：在原始 ASGI 层，HEAD 必须发出 0 字节 body，
    而不是像修复前那样把 ~1KB 解密后的图片写进 http.response.body（当时靠 uvicorn
    协议层兜底才没上线）。同时头必须与 GET 一致（Content-Length/Content-Type/ETag
    保留），这样 HEAD 仍能当元数据探针用。"""
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    enclave_auth.reset_cache()
    app = build_app()
    path = f"/v1/screen/frames/{FRAME_ID}/image"
    hdr = {"X-API-Key": "k"}

    get_status, get_body, get_hdrs = _raw_asgi_request(app, "GET", path, hdr)
    head_status, head_body, head_hdrs = _raw_asgi_request(app, "HEAD", path, hdr)

    assert get_status == 200 and get_body == JPEG            # GET 照常带完整图
    assert head_status == 200                                 # HEAD 也 200
    assert head_body == b""                                   # 关键：HEAD 不发 body
    # 头与 GET 一致 —— HEAD 仍报告 GET 会返回的 Content-Length/类型/ETag
    assert head_hdrs.get("content-length") == str(len(JPEG))
    assert head_hdrs.get("content-type") == "image/jpeg"
    assert head_hdrs.get("etag") == get_hdrs.get("etag")
