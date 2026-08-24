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
            self.chat_messages: list[dict] = [
                {"id": "ordinary1", "role": "openclaw", "source": "chat", "ts": 99.0}
            ]
            self.chat_waiters: list[threading.Event] = []
            self.chat_waiters_lock = threading.Lock()
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

        def reload(self):
            raise AssertionError("verify_loop must not reload the whole store")

    def run(status: str):
        store = Store()
        clock = {"now": 100.0}
        logged: list[str] = []

        wait_durations: list[float] = []

        def wait_without_notify(_waiter, seconds):
            # Simulate a lost NOTIFY: the ack becomes durable while the waiter
            # times out, and the next bounded point read must discover it.
            wait_durations.append(seconds)
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
            return False

        def verify_reply(_user_id, ping_id, ping_ts):
            return next(
                (
                    row for row in store.chat_messages
                    if chat_core._verify_reply_matches_ping(
                        row, ping_id=ping_id, ping_ts=ping_ts
                    )
                ),
                None,
            )

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
        monkeypatch.setattr(chat_core, "_wait_for_verify_wake", wait_without_notify)
        monkeypatch.setattr(chat_core.db, "chat_verify_reply_strict", verify_reply)
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
        assert wait_durations and max(wait_durations) <= 2.0
        assert [row["id"] for row in store.chat_messages] == ["ordinary1"]
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


def test_two_concurrent_verify_loops_cleanup_only_their_own_exchange(monkeypatch):
    class Store:
        user_id = "usr_concurrent_verify"

        def __init__(self):
            self.chat_lock = threading.RLock()
            self.chat_messages: list[dict] = []
            self.chat_waiters: list[threading.Event] = []
            self.chat_waiters_lock = threading.Lock()

        def append_chat(self, role, source, envelope):
            row = {**envelope, "role": role, "source": source, "ts": 100.0}
            with self.chat_lock:
                self.chat_messages.append(row)
            return row

        def notify_chat_waiters(self):
            return None

    store = Store()
    both_queries = threading.Barrier(2)
    deleted: list[tuple[str, str]] = []
    deleted_lock = threading.Lock()

    def exact_reply(_user_id, ping_id, _ping_ts):
        both_queries.wait(timeout=2)
        ack = {
            "id": f"ack-{ping_id}",
            "role": "openclaw",
            "source": "verify_ping",
            "reply_to_message_id": ping_id,
            "ts": 100.1,
        }
        with store.chat_lock:
            store.chat_messages.append(ack)
        return ack

    def delete(_user_id, message_id):
        with deleted_lock:
            deleted.append((threading.current_thread().name, message_id))
        return True

    from hosted import config_store as hosted_config_store

    monkeypatch.setattr(hosted_config_store, "load_active_route", lambda _store: None)
    monkeypatch.setattr(chat_core.db, "chat_verify_reply_strict", exact_reply)
    monkeypatch.setattr(chat_core.db, "chat_delete", delete)
    monkeypatch.setattr(
        chat_core.accounts_onboarding, "_load_onboarding_route", lambda _store: "model_api"
    )
    monkeypatch.setattr(chat_core.boot_gates, "_log_bootstrap_event", lambda *_a, **_k: None)
    monkeypatch.setattr(chat_core, "_maybe_enqueue_resident_introduction", lambda _store: None)

    results: dict[str, tuple[dict, int]] = {}

    def run():
        results[threading.current_thread().name] = chat_core.verify_loop(
            store, {"timeout_sec": 2}
        )

    threads = [threading.Thread(target=run, name=name) for name in ("verify-a", "verify-b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()

    assert all(body["loop_alive"] and code == 200 for body, code in results.values())
    by_thread = {
        name: {message_id for owner, message_id in deleted if owner == name}
        for name in results
    }
    assert all(len(ids) == 2 for ids in by_thread.values())
    assert by_thread["verify-a"].isdisjoint(by_thread["verify-b"])
    assert store.chat_messages == []
    assert store.chat_waiters == []


def test_verify_timeout_cleans_exact_ping_and_waiter(monkeypatch):
    class Store:
        user_id = "usr_verify_timeout"
        chat_lock = threading.RLock()
        chat_messages: list[dict] = []
        chat_waiters: list[threading.Event] = []
        chat_waiters_lock = threading.Lock()

        def append_chat(self, role, source, envelope):
            row = {**envelope, "role": role, "source": source, "ts": 100.0}
            self.chat_messages.append(row)
            return row

        def notify_chat_waiters(self):
            return None

    store = Store()
    deleted: list[str] = []
    from hosted import config_store as hosted_config_store

    monkeypatch.setattr(hosted_config_store, "load_active_route", lambda _store: None)
    monkeypatch.setattr(chat_core.db, "chat_verify_reply_strict", lambda *_args: None)
    monkeypatch.setattr(
        chat_core.db, "chat_delete", lambda _user_id, message_id: deleted.append(message_id)
    )
    monkeypatch.setattr(
        chat_core.accounts_onboarding, "_load_onboarding_route", lambda _store: "model_api"
    )

    body, code = chat_core.verify_loop(store, {"timeout_sec": 0})

    assert code == 200
    assert body["loop_alive"] is False
    assert len(deleted) == 1
    assert store.chat_messages == []
    assert store.chat_waiters == []
