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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from chat import chat_core  # noqa: E402


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
