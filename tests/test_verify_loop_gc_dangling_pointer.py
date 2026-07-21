"""Regression: verify_loop GC must NOT delete a real reply that merely landed
after the synthetic ping — doing so orphans the parent's reply_message_id.

Prod/test bug (DIAGNOSIS_hosted_reply_dangling_pointer_2026-07-20):

  supervisor._maybe_autoverify POSTs /v1/chat/verify_loop for a freshly-hosted
  user WHILE the agent-runner consumer is answering that user's first real
  message. The real reply is atomically inserted by finalize_chat_reply_once
  (parent -> reply_status=replied + reply_message_id=X), lands with ts>ping_ts,
  and verify_loop's cleanup — which matched "the first agent reply after the
  ping" purely by role+ts, with NO source guard — deleted X as if it were the
  ping's ack. Result: parent points at reply_message_id=X but row X is gone
  (dangling pointer, silent lost reply; the round never retries).

Genuine verify acks ALWAYS carry source="verify_ping" (the resident AND hosted
consumer, tools/chat_resident_consumer.py, post them that way in a dedicated
branch). So GC must key ONLY off source="verify_ping" and never off "some agent
reply landed after the ping". These tests pin that invariant.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from chat import chat_core  # noqa: E402
from bootstrap import gates as boot_gates  # noqa: E402


def test_gc_keeps_real_reply_deletes_only_verify_ping_rows():
    """The dangling-pointer regression: a real chat reply that landed after the
    ping must survive GC; only source='verify_ping' rows (ping + ack) are removed."""
    messages = [
        {"id": "ping1", "role": "user", "source": "verify_ping"},
        {"id": "ack1", "role": "openclaw", "source": "verify_ping"},
        # A real user turn answered concurrently during the verify window:
        {"id": "parent1", "role": "user", "source": "chat",
         "reply_status": "replied", "reply_message_id": "real_reply1"},
        {"id": "real_reply1", "role": "openclaw", "source": "chat"},
    ]

    gc_ids = chat_core._verify_synthetic_ids_to_gc(messages)

    assert set(gc_ids) == {"ping1", "ack1"}
    assert "real_reply1" not in gc_ids, (
        "verify_loop GC deleted a real reply -> parent1.reply_message_id would "
        "dangle (silent lost reply)"
    )


def test_gc_collects_ping_ack_even_when_no_real_traffic():
    """Normal case: with only the synthetic exchange present, both synthetic
    rows are GC'd (unchanged behavior)."""
    messages = [
        {"id": "ping1", "role": "user", "source": "verify_ping"},
        {"id": "ack1", "role": "openclaw", "source": "verify_ping"},
    ]
    assert set(chat_core._verify_synthetic_ids_to_gc(messages)) == {"ping1", "ack1"}


def test_gc_ignores_malformed_rows_and_missing_ids():
    messages = [
        "not-a-dict",
        {"role": "openclaw", "source": "verify_ping"},  # no id -> skip
        {"id": "", "source": "verify_ping"},            # empty id -> skip
        {"id": "chat1", "role": "openclaw", "source": "chat"},
    ]
    assert chat_core._verify_synthetic_ids_to_gc(messages) == []


def test_verify_success_requires_source_and_exact_reply_parent():
    base = {
        "id": "ack1",
        "role": "openclaw",
        "source": "verify_ping",
        "reply_to_message_id": "ping1",
        "ts": 11.0,
    }
    assert chat_core._verify_reply_matches_ping(
        base, ping_id="ping1", ping_ts=10.0
    ) is True

    for change in (
        {"source": "chat"},
        {"reply_to_message_id": "other-ping"},
        {"reply_to_message_id": ""},
        {"ts": 9.0},
        {"role": "user"},
    ):
        candidate = {**base, **change}
        assert chat_core._verify_reply_matches_ping(
            candidate, ping_id="ping1", ping_ts=10.0
        ) is False


def test_pending_verify_ping_is_consumed_only_by_its_exact_hidden_ack():
    class Store:
        chat_lock = threading.RLock()
        chat_messages = [
            {"id": "ping1", "role": "user", "source": "verify_ping", "ts": 10.0},
            {"id": "real", "role": "openclaw", "source": "chat", "ts": 11.0},
            {
                "id": "wrong",
                "role": "openclaw",
                "source": "verify_ping",
                "reply_to_message_id": "other",
                "ts": 12.0,
            },
        ]

    store = Store()
    assert boot_gates._reply_is_for_pending_verify_ping(store) is True
    store.chat_messages.append(
        {
            "id": "ack1",
            "role": "openclaw",
            "source": "verify_ping",
            "reply_to_message_id": "ping1",
            "ts": 13.0,
        }
    )
    assert boot_gates._reply_is_for_pending_verify_ping(store) is False


def test_verify_loop_requires_decrypt_health_before_minting_sticky_green(monkeypatch):
    class Store:
        user_id = "usr_verify_decrypt_health"

        def __init__(self):
            self.chat_lock = threading.RLock()
            self.chat_messages: list[dict] = []
            self.acked = False

        def append_chat(self, role, source, envelope):
            msg = {
                **envelope,
                "id": "ping1",
                "role": role,
                "source": source,
                "ts": 100.0,
            }
            self.chat_messages.append(msg)
            return msg

        def notify_chat_waiters(self):
            return None

        def first_chat_ok_at(self):
            return ""

    def run(status: str):
        store = Store()
        clock = {"now": 100.0}
        logged: list[str] = []

        def sleep(_seconds):
            clock["now"] += 0.1
            if not store.acked:
                store.chat_messages.append(
                    {
                        "id": "ack1",
                        "role": "openclaw",
                        "source": "verify_ping",
                        "reply_to_message_id": "ping1",
                        "ts": 100.05,
                    }
                )
                store.acked = True

        if status == "ok":
            health = chat_core.chat_consumer._decrypt_health_from_state(
                {
                    "decrypt_status": "ok",
                    "decrypt_checked_at_epoch": "100",
                },
                now_epoch=100.0,
            )
        else:
            health = chat_core.chat_consumer._decrypt_health_from_state(
                {
                    "decrypt_status": status,
                    "decrypt_checked_at_epoch": "100",
                },
                now_epoch=100.0,
            )
        resident = {"passing": True, "decrypt_health": health}
        monkeypatch.setattr(chat_core.time, "time", lambda: clock["now"])
        monkeypatch.setattr(chat_core.time, "sleep", sleep)
        monkeypatch.setattr(
            chat_core.accounts_onboarding,
            "_load_onboarding_route",
            lambda _store: "resident",
        )
        monkeypatch.setattr(
            chat_core.chat_consumer,
            "_consumer_validation_state",
            lambda _store: resident,
        )
        monkeypatch.setattr(
            chat_core.boot_gates,
            "_log_bootstrap_event",
            lambda _store, event_type, success: logged.append(event_type),
        )
        monkeypatch.setattr(chat_core, "_maybe_enqueue_resident_introduction", lambda _store: None)
        monkeypatch.setattr(chat_core.db, "chat_delete", lambda *_args: None)
        body, code = chat_core.verify_loop(store, {"timeout_sec": 2})
        return body, code, logged

    healthy, code, logged = run("ok")
    assert code == 200
    assert healthy["loop_alive"] is True
    assert healthy["passing"] is True
    assert logged == ["chat_loop_verified"]

    unconfigured, code, logged = run("unconfigured")
    assert code == 200
    assert unconfigured["loop_alive"] is True
    assert unconfigured["passing"] is False
    assert unconfigured["reason"] == "decrypt_source_unconfigured"
    assert logged == []
