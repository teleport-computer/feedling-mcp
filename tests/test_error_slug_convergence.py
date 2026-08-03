"""自由文本错误 → slug + detail 收敛（spec Phase A / A3）。

Covers the 6 core-function error sites converged by this task:
  - chat_core.write_message   (envelope_missing_fields)          — HTTP /v1/chat/message
  - chat_core.write_response  (envelope_missing_fields)          — core-level (bypasses bootstrap gate)
  - chat_core.write_response  (thinking_envelope_missing_fields) — core-level
  - memory_core.add           (envelope_missing_fields)          — HTTP /v1/memory/add
  - memory_core.retype        (anchor_required)                  — HTTP /v1/memory/retype
  - memory/actions._memory_validate_write (anchor_required)      — HTTP /v1/memory/actions

None of these six functions go through the api_error() helper (Task 1) — they
keep returning framework-neutral ``(dict, status)`` tuples. The actions batch
preserves its HTTP-200 aggregate response while exposing the validation status
and stable error slug on the failed item.

Run:  python -m pytest tests/test_error_slug_convergence.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from asgi_test_client import make_client  # noqa: E402
import base64  # noqa: E402

from chat import chat_core  # noqa: E402
from core import store as core_store  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _register():
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _dummy_envelope(uid: str, **overrides) -> dict:
    env = {
        "body_ct": "ct",
        "nonce": "n",
        "K_user": "k",
        "visibility": "local_only",
        "owner_user_id": uid,
    }
    env.update(overrides)
    return env


# --------------------------------------------------------------------------- #
# chat_core.write_message — HTTP /v1/chat/message (given in task brief)
# --------------------------------------------------------------------------- #

def test_chat_message_missing_envelope_fields_is_slug(backend_env):
    uid, key = _register()
    res = make_client().post(
        "/v1/chat/message",
        headers={"X-API-Key": key},
        json={"envelope": {"v": 1, "id": "m1"}},   # 缺 body_ct/nonce/K_user…
    )
    assert res.status_code == 400
    body = res.get_json()
    assert body["error"] == "envelope_missing_fields"        # slug，非自由文本
    assert isinstance(body["detail"], list) and "body_ct" in body["detail"]


# --------------------------------------------------------------------------- #
# chat_core.write_response — core-level (bootstrap gate is checked by the
# adapter, not by write_response itself, so calling the core function
# directly exercises the envelope/thinking_envelope validation without
# needing to fake a consumer identity or monkeypatch the gate).
# --------------------------------------------------------------------------- #

def test_chat_response_missing_envelope_fields_is_slug(backend_env):
    uid, key = _register()
    store = core_store.get_store(uid)
    body, status = chat_core.write_response(
        store,
        {"envelope": {"v": 1, "id": "r1"}},   # 缺 body_ct/nonce/K_user…
        consumer_id="agent-1",
        consumer_info={},
        allow_verify_reply=True,
    )
    assert status == 400
    assert body["error"] == "envelope_missing_fields"
    assert isinstance(body["detail"], list) and "body_ct" in body["detail"]


def test_chat_response_thinking_envelope_missing_fields_is_slug(backend_env):
    uid, key = _register()
    store = core_store.get_store(uid)
    body, status = chat_core.write_response(
        store,
        {
            "envelope": _dummy_envelope(uid, id="r2"),
            "thinking_envelope": {"v": 1, "id": "t1"},   # 缺 body_ct/nonce/K_user…
        },
        consumer_id="agent-1",
        consumer_info={},
        allow_verify_reply=True,
    )
    assert status == 400
    assert body["error"] == "thinking_envelope_missing_fields"
    assert isinstance(body["detail"], list) and "body_ct" in body["detail"]


# --------------------------------------------------------------------------- #
# memory_core.add — HTTP /v1/memory/add
#
# ⚠️ 实现者决定：memory_core 的 add() 有直达 HTTP 路由
# (POST /v1/memory/add, wired in memory/routes_asgi.py), so this test
# drives it end-to-end via make_client() rather than falling back to a
# core-level unit test — the brief's fallback note doesn't apply here.
# --------------------------------------------------------------------------- #

def test_memory_add_missing_envelope_fields_is_slug(backend_env):
    uid, key = _register()
    res = make_client().post(
        "/v1/memory/add",
        headers={"X-API-Key": key},
        json={"envelope": {"v": 1, "id": "c1"}},   # 缺 body_ct/nonce/K_user…
    )
    assert res.status_code == 400
    body = res.get_json()
    assert body["error"] == "envelope_missing_fields"
    assert isinstance(body["detail"], list) and "body_ct" in body["detail"]


# --------------------------------------------------------------------------- #
# memory_core.retype — HTTP /v1/memory/retype
# --------------------------------------------------------------------------- #

def test_memory_retype_anchor_required_is_slug(backend_env):
    uid, key = _register()
    client = make_client()
    added = client.post(
        "/v1/memory/add",
        headers={"X-API-Key": key},
        json={
            "envelope": {
                **_dummy_envelope(uid),
                "type": "fact",
                "occurred_at": "2026-01-01T00:00:00",
            }
        },
    )
    assert added.status_code == 201, added.get_json()
    memory_id = added.get_json()["moment"]["id"]

    res = client.post(
        "/v1/memory/retype",
        headers={"X-API-Key": key},
        json={"id": memory_id, "type": "insight"},   # 无 anchor_memory_ids
    )
    assert res.status_code == 400
    body = res.get_json()
    assert body["error"] == "anchor_required"          # slug，非 "insight_requires_anchor"
    assert body["detail"] == {"mem_type": "insight"}


# --------------------------------------------------------------------------- #
# memory/actions._memory_validate_write — HTTP /v1/memory/actions
# via the prebuilt-envelope memory.add path (_memory_add_envelope_action ->
# _memory_validate_prebuilt_envelope -> _memory_validate_write), which is the
# shared validator behind add / content_patch / retype / supersede actions.
# --------------------------------------------------------------------------- #

def test_memory_actions_add_anchor_required_is_slug(backend_env):
    uid, key = _register()
    res = make_client().post(
        "/v1/memory/actions",
        headers={"X-API-Key": key},
        json={
            "actions": [
                {
                    "type": "memory.add",
                    "envelope": {
                        **_dummy_envelope(uid),
                        "type": "insight",
                        "occurred_at": "2026-01-01T00:00:00",
                        # no anchor_memory_ids
                    },
                }
            ]
        },
    )
    # Contract (Seven arbitration 2026-08-01/02, restored in e320d0fd): a batch
    # that applied NOTHING and reported at least one hard failure is an outer
    # HTTP 400 with the first failed item's slug promoted top-level — callers
    # using the outer status as their failure signal must never mistake an
    # all-failed write for success. Partial/complete success stays HTTP 200
    # with independent per-item outcomes. Both layers are asserted here.
    # (72de68dc briefly flipped this to 200 in a parallel edit unaware of the
    # arbitration; do not re-flip without a new product decision.)
    assert res.status_code == 400
    body = res.get_json()
    assert body["error"] == "anchor_required"           # top-level, slug
    assert body["status"] == "failed"
    result0 = body["results"][0]
    assert result0["http_status"] == 400
    assert result0["error"] == "anchor_required"
    assert result0["detail"] == {"mem_type": "insight"}
