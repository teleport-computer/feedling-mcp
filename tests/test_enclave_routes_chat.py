from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402

from asgi_test_client import _AsgiTestClient  # noqa: E402
from enclave import auth as enclave_auth  # noqa: E402
from enclave import backend_client, envelope as envmod, keys  # noqa: E402
from enclave import state as enclave_state  # noqa: E402
from enclave.routes import build_app  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    enclave_auth.reset_cache()
    return _AsgiTestClient(build_app())


def _wire(monkeypatch, messages, moments=None):
    async def fake_backend_get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": "usr_a"}
        if path == "/v1/chat/history":
            return {"messages": messages, "total": len(messages)}
        if path == "/v1/memory/list":
            return {"moments": moments or [], "total": 0}
        raise AssertionError(path)
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)


def test_memory_list_fetch_overlaps_history_decrypt(client, monkeypatch):
    """/v1/memory/list 拉取不依赖 history 解密结果，async 化后应与解密并行
    （旧同步 Flask 只能串行；串行让每个请求多付一次 backend RTT）。
    测法：解密故意放慢，断言 memory/list 的回环在解密结束前就已发出。"""
    import threading
    import time as _time

    order = []

    async def fake_backend_get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": "usr_a"}
        if path == "/v1/chat/history":
            return {"messages": [
                {"id": "m1", "role": "user", "ts": 1.0, "v": 1, "source": "ios",
                 "K_enclave": "x", "body_ct": "x", "nonce": "x",
                 "owner_user_id": "usr_a"},
            ], "total": 1}
        if path == "/v1/memory/list":
            order.append("memlist_fetch")
            return {"moments": [], "total": 0}
        raise AssertionError(path)
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)

    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)

    def slow_decrypt(e, u, s):
        _time.sleep(0.2)  # 在 to_thread 里跑，不堵事件循环
        order.append("decrypt_end")
        return b"hello"
    monkeypatch.setattr(envmod, "decrypt_envelope", slow_decrypt)

    r = client.get("/v1/chat/history", headers={"X-API-Key": "k"})
    assert r.status_code == 200
    assert r.get_json()["messages"][0]["content"] == "hello"
    assert "memlist_fetch" in order and "decrypt_end" in order
    assert order.index("memlist_fetch") < order.index("decrypt_end"), \
        f"memory/list 没有与解密并行: {order}"


def test_history_head_supported(client, monkeypatch):
    # Flask 给每个 GET 路由自动挂 HEAD；FastAPI 的 APIRoute 不会。HEAD 必须
    # 返回与 GET 相同的状态/头、空 body（HeadBodyStripMiddleware 负责剥体），
    # 而不是 405。
    _wire(monkeypatch, [])
    r = client.open("/v1/chat/history", method="HEAD",
                    headers={"X-API-Key": "k"})
    assert r.status_code == 200
    assert r.data == b""


def test_history_decrypts_text_messages(client, monkeypatch):
    _wire(monkeypatch, [
        {"id": "m1", "role": "user", "ts": 1.0, "v": 1, "source": "ios",
         "K_enclave": "x", "body_ct": "x", "nonce": "x", "owner_user_id": "usr_a"},
    ])
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: b"hello")
    r = client.get("/v1/chat/history", headers={"X-API-Key": "k"})
    assert r.status_code == 200
    body = r.get_json()
    m = body["messages"][0]
    assert m["content"] == "hello"
    assert m["decrypt_status"] == "ok"
    assert body["decrypt_errors"] == []
    assert body["user_id"] == "usr_a"
    assert "context_memories" in body


def test_history_decrypts_resident_maintenance_message(client, monkeypatch):
    prompt = "【Feedling 系统维护提醒】\n请更新 resident consumer。"
    _wire(monkeypatch, [
        {"id": "rm1", "role": "user", "ts": 1.0, "v": 1, "source": "resident_maintenance",
         "K_enclave": "x", "body_ct": "x", "nonce": "x", "owner_user_id": "usr_a",
         "visibility": "shared", "content_type": "text"},
    ])
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: prompt.encode("utf-8"))
    r = client.get("/v1/chat/history", headers={"X-API-Key": "k"})
    assert r.status_code == 200
    m = r.get_json()["messages"][0]
    assert m["source"] == "resident_maintenance"
    assert m["content"] == prompt
    assert m["decrypt_status"] == "ok"


def test_local_only_placeholder(client, monkeypatch):
    _wire(monkeypatch, [
        {"id": "m1", "role": "user", "ts": 1.0, "v": 1,
         "visibility": "local_only", "content_type": "text"},
    ])
    r = client.get("/v1/chat/history", headers={"X-API-Key": "k"})
    m = r.get_json()["messages"][0]
    assert m["content"] is None
    assert m["decrypt_status"] == "local_only_agent_cannot_read"


def test_per_item_decrypt_error_not_500(client, monkeypatch):
    _wire(monkeypatch, [
        {"id": "bad", "role": "user", "ts": 1.0, "v": 1,
         "K_enclave": "x", "body_ct": "x", "nonce": "x", "owner_user_id": "usr_a"},
    ])
    def boom(env, uid, sk):
        raise envmod.DecryptFailure("AEAD verify: nope")
    monkeypatch.setattr(envmod, "decrypt_envelope", boom)
    r = client.get("/v1/chat/history", headers={"X-API-Key": "k"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["messages"][0]["decrypt_status"].startswith("error: AEAD")
    assert body["decrypt_errors"][0]["id"] == "bad"


def test_image_message_with_caption(client, monkeypatch):
    _wire(monkeypatch, [
        {"id": "img1", "role": "user", "ts": 1.0, "v": 1, "content_type": "image",
         "K_enclave": "x", "body_ct": "x", "nonce": "x", "owner_user_id": "usr_a",
         "image_mime": "image/png",
         "caption_body_ct": "y", "caption_nonce": "y", "caption_K_enclave": "y"},
    ])
    jpeg = b"\x89PNG fake"
    def fake_decrypt(env, uid, sk):
        return b"what is this?" if env.get("body_ct") == "y" else jpeg
    monkeypatch.setattr(envmod, "decrypt_envelope", fake_decrypt)
    r = client.get("/v1/chat/history", headers={"X-API-Key": "k"})
    m = r.get_json()["messages"][0]
    assert base64.b64decode(m["image_b64"]) == jpeg
    assert m["image_mime"] == "image/png"
    assert m["content"] == "what is this?"


def test_file_message_with_caption(client, monkeypatch):
    _wire(monkeypatch, [
        {"id": "f1", "role": "user", "ts": 1.0, "v": 1, "content_type": "file",
         "K_enclave": "x", "body_ct": "x", "nonce": "x", "owner_user_id": "usr_a",
         "file_mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         "file_name": "报告.docx",
         "caption_body_ct": "y", "caption_nonce": "y", "caption_K_enclave": "y"},
    ])
    raw = b"raw doc bytes"
    def fake_decrypt(env, uid, sk):
        return b"summarize this?" if env.get("body_ct") == "y" else raw
    monkeypatch.setattr(envmod, "decrypt_envelope", fake_decrypt)
    m = client.get("/v1/chat/history", headers={"X-API-Key": "k"}).get_json()["messages"][0]
    import base64 as _b64
    assert _b64.b64decode(m["file_b64"]) == raw
    assert m["file_mime"].endswith("wordprocessingml.document")
    assert m["file_name"] == "报告.docx"
    assert m["content"] == "summarize this?"


def test_file_message_without_caption(client, monkeypatch):
    _wire(monkeypatch, [
        {"id": "f2", "role": "user", "ts": 1.0, "v": 1, "content_type": "file",
         "K_enclave": "x", "body_ct": "x", "nonce": "x", "owner_user_id": "usr_a",
         "file_mime": "text/markdown", "file_name": "notes.md"},
    ])
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e,u,s: b"# notes\n")
    m = client.get("/v1/chat/history", headers={"X-API-Key": "k"}).get_json()["messages"][0]
    import base64 as _b64
    assert _b64.b64decode(m["file_b64"]) == b"# notes\n"
    assert m["content"] == ""


def test_context_memories_best_effort_on_failure(client, monkeypatch):
    _wire(monkeypatch, [])
    from enclave import readside
    def boom(*a, **kw):
        raise RuntimeError("selector exploded")
    monkeypatch.setattr(readside, "moments_to_cards", boom)
    r = client.get("/v1/chat/history", headers={"X-API-Key": "k"})
    assert r.status_code == 200  # context_memories 失败绝不 500
    assert r.get_json()["context_memories"] == []


# --------------------------------------------------------------------------- #
# Oversized-history containment (B): the resident must be able to pull the text
# transcript WITHOUT every image body riding along. A window holding five 1.4MB
# images serialized to a 4.4MB response that the CVM egress truncated mid-body
# ("peer closed connection ... received 196608, expected 4433378") — the resident
# then skipped the whole cycle, the cursor never advanced, and the next window
# was guaranteed to contain the same images again. Pixels now come back one
# message at a time via /v1/chat/messages/<id>/body.
# --------------------------------------------------------------------------- #


def test_history_forwards_include_image_body_to_backend(client, monkeypatch):
    """The enclave used to forward only since/limit, silently dropping
    include_image_body — so callers could not opt out of the image bodies."""
    seen: dict = {}

    async def fake_backend_get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": "usr_a"}
        if path == "/v1/chat/history":
            seen["params"] = params or {}
            return {"messages": [], "total": 0}
        if path == "/v1/memory/list":
            return {"moments": [], "total": 0}
        raise AssertionError(path)

    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)

    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)

    r = client.get(
        "/v1/chat/history?since=5&limit=20&include_image_body=false",
        headers={"X-API-Key": "k"},
    )
    assert r.status_code == 200
    assert seen["params"]["include_image_body"] == "false"
    assert seen["params"]["since"] == "5"
    assert seen["params"]["limit"] == "20"


def test_omitted_image_body_degrades_without_a_decrypt_error(client, monkeypatch):
    """A body-omitted image row has no body_ct. It must NOT be reported as a
    decrypt failure — the caption still decrypts, so the agent keeps the user's
    actual question and just learns the pixels are fetched separately."""
    _wire(monkeypatch, [
        {"id": "i1", "role": "user", "ts": 1.0, "v": 1, "content_type": "image",
         "body_omitted": True, "body_omitted_reason": "image_body", "body_ct_len": 1425288,
         "caption_body_ct": "c", "caption_nonce": "cn", "caption_K_enclave": "ck",
         "owner_user_id": "usr_a", "image_mime": "image/jpeg"},
    ])
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: b"what is wrong here?")

    body = client.get("/v1/chat/history", headers={"X-API-Key": "k"}).get_json()
    m = body["messages"][0]

    assert m["decrypt_status"] == "ok"
    assert m["image_omitted"] is True
    assert "image_b64" not in m
    assert m["content"] == "what is wrong here?"   # caption survived the omission
    assert m["content_type"] == "image"
    assert body["decrypt_errors"] == []            # not a failure — an opt-out


def test_message_body_route_decrypts_one_image(client, monkeypatch):
    """Single-message body fetch: bounded payload (one image), so a wedged
    window can never form."""
    async def fake_backend_get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": "usr_a"}
        if path == "/v1/chat/messages/i1/body":
            return {"message": {
                "id": "i1", "role": "user", "ts": 1.0, "v": 1, "content_type": "image",
                "body_ct": "BODY", "nonce": "n", "K_enclave": "k",
                "owner_user_id": "usr_a", "image_mime": "image/png",
                "caption_body_ct": "CAP", "caption_nonce": "cn", "caption_K_enclave": "ck",
            }}
        raise AssertionError(path)

    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)

    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)

    def fake_decrypt(env, uid, sk):
        return b"CAPTION-TEXT" if env.get("body_ct") == "CAP" else b"\x89PNG-RAW-BYTES"
    monkeypatch.setattr(envmod, "decrypt_envelope", fake_decrypt)

    r = client.get("/v1/chat/messages/i1/body", headers={"X-API-Key": "k"})
    assert r.status_code == 200
    m = r.get_json()["message"]
    assert base64.b64decode(m["image_b64"]) == b"\x89PNG-RAW-BYTES"
    assert m["image_mime"] == "image/png"
    assert m["content"] == "CAPTION-TEXT"
    assert m["decrypt_status"] == "ok"


def test_message_body_route_propagates_backend_404(client, monkeypatch):
    import httpx

    async def fake_backend_get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": "usr_a"}
        if path == "/v1/chat/messages/nope/body":
            req = httpx.Request("GET", "http://backend/v1/chat/messages/nope/body")
            raise httpx.HTTPStatusError(
                "404", request=req, response=httpx.Response(404, request=req))
        raise AssertionError(path)

    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)

    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)

    r = client.get("/v1/chat/messages/nope/body", headers={"X-API-Key": "k"})
    assert r.status_code == 404
