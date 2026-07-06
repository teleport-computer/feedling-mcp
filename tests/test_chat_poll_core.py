"""Parity for the framework-neutral chat poll core (ASGI-migration plan §7.5).

Locks the pending/claim semantics and the response contract that the Flask route
and the forthcoming FastAPI async poll route both go through
(``chat.poll_core``). If these drift, the two backends would return different
chat-poll payloads — a silent, client-visible divergence under a no-fallback
cutover.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from chat import poll_core as chat_poll_core  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    return core_store.get_store(res.get_json()["user_id"])


def _append_user_message(store, msg_id: str) -> None:
    store.append_chat(
        role="user",
        source="chat",
        envelope={"id": msg_id, "v": 1, "body_ct": "x", "nonce": "x", "K_user": "x"},
    )


def test_pending_messages_returns_claimable_message(store):
    _append_user_message(store, "m1")
    pending = chat_poll_core.pending_messages(store, since=0.0, consumer_id="c-A", claim=True)
    assert [m["id"] for m in pending] == ["m1"]


def test_pending_messages_claim_is_exclusive(store):
    """The claim moved into the core must stay a real CAS: a second consumer
    does not re-receive an already-claimed message (no double delivery)."""
    _append_user_message(store, "m2")
    first = chat_poll_core.pending_messages(store, since=0.0, consumer_id="c-A", claim=True)
    second = chat_poll_core.pending_messages(store, since=0.0, consumer_id="c-B", claim=True)
    assert "m2" in [m["id"] for m in first]
    assert "m2" not in [m["id"] for m in second]


def test_pending_messages_no_claim_leaves_it_pending(store):
    _append_user_message(store, "m3")
    peek = chat_poll_core.pending_messages(store, since=0.0, consumer_id="c-A", claim=False)
    claimed = chat_poll_core.pending_messages(store, since=0.0, consumer_id="c-B", claim=True)
    assert "m3" in [m["id"] for m in peek]
    assert "m3" in [m["id"] for m in claimed]  # peek did not consume the claim


def test_poll_context_shape(store):
    ctx = chat_poll_core.poll_context(store)
    assert set(ctx) == {"runtime_v2", "client_release"}
    assert "expected_consumer_commit" in ctx["client_release"]


def test_build_response_contract(store):
    ctx = chat_poll_core.poll_context(store)
    resp = chat_poll_core.build_response(
        messages=[{"id": "m"}], context=ctx, consumer_id="c-A", claim=True, timed_out=False
    )
    assert set(resp) == {"messages", "runtime_v2", "client_release", "timed_out", "consumer_id", "claimed"}
    assert resp["messages"] == [{"id": "m"}]
    assert resp["consumer_id"] == "c-A"
    assert resp["claimed"] is True
    assert resp["timed_out"] is False
    assert resp["runtime_v2"] == ctx["runtime_v2"]
    assert resp["client_release"] == ctx["client_release"]
