"""Pure-unit coverage for chat send idempotency parsing and side effects."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from chat import idempotency  # noqa: E402
from core import store as core_store  # noqa: E402
from core import wake_bus  # noqa: E402
from proactive import capture_scheduler  # noqa: E402


def _envelope(msg_id: str) -> dict:
    return {
        "id": msg_id,
        "v": 1,
        "body_ct": f"ct-{msg_id}",
        "nonce": "nonce",
        "K_user": "user-key",
        "K_enclave": "enclave-key",
        "visibility": "shared",
        "owner_user_id": "usr_unit",
    }


def _bare_store() -> core_store.UserStore:
    store = core_store.UserStore.__new__(core_store.UserStore)
    store.user_id = "usr_unit"
    store.chat_lock = threading.RLock()
    store.chat_messages = []
    return store


def test_parse_client_msg_id_absent_invalid_and_canonical():
    assert idempotency.parse_client_msg_id({}) == (None, None)
    value, error = idempotency.parse_client_msg_id(
        {"client_msg_id": "2B6B5D80-53DA-4AD3-9662-4434959D0505"}
    )
    assert value == "2b6b5d80-53da-4ad3-9662-4434959d0505"
    assert error is None
    assert idempotency.parse_client_msg_id({"client_msg_id": "nope"}) == (
        None,
        (
            {
                "error": "client_msg_id_invalid",
                "detail": "client_msg_id must be a UUID string",
            },
            400,
        ),
    )


def test_store_duplicate_reconciles_cache_without_second_side_effect(monkeypatch):
    store = _bare_store()
    winner: dict = {}
    calls = {"db": 0, "wake": 0, "capture": 0}

    def _append(_uid, _msg_id, _ts, doc, _max, **_kwargs):
        calls["db"] += 1
        if not winner:
            winner.update(doc)
            return dict(winner), True
        return dict(winner), False

    monkeypatch.setattr(core_store.db, "chat_append_idempotent", _append)
    monkeypatch.setattr(
        wake_bus, "notify", lambda *_args, **_kwargs: calls.__setitem__("wake", calls["wake"] + 1)
    )
    monkeypatch.setattr(
        capture_scheduler,
        "record_chat_append",
        lambda *_args, **_kwargs: calls.__setitem__("capture", calls["capture"] + 1),
    )

    first, first_inserted = store.append_chat_idempotent(
        "user",
        "chat",
        _envelope("first-envelope"),
        client_msg_id="2b6b5d80-53da-4ad3-9662-4434959d0505",
        window_sec=600,
    )
    retry, retry_inserted = store.append_chat_idempotent(
        "user",
        "chat",
        _envelope("retry-envelope"),
        client_msg_id="2b6b5d80-53da-4ad3-9662-4434959d0505",
        window_sec=600,
    )

    assert first_inserted is True
    assert retry_inserted is False
    assert retry == first
    assert [row["id"] for row in store.chat_messages] == ["first-envelope"]
    assert calls == {"db": 2, "wake": 1, "capture": 1}
