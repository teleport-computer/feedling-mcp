"""Offline backlog collapse tests for chat_resident_consumer.py.

A consumer that was down/stuck for days used to answer every piled-up user
message with its own agent turn (usr_6c1971, 2026-07-22). A stale pile of
plain-text user messages now merges into ONE agent turn. These tests cover the
trigger conditions, exclusions, prompt bounds, and the merged single-call path.

Network + agent are mocked — no real backend needed.
"""

import os
import sys
import time
import types
from pathlib import Path

import pytest

_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "cli",
    "AGENT_CLI_CMD": "claude --allowed-tools 'x' {mcp} -p {message}",
    "CHECKPOINT_FILE": "/tmp/feedling_test_backlog_collapse_checkpoint.json",
}
for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules.setdefault("content_encryption", _fake_enc)

import tools.chat_resident_consumer as c  # noqa: E402

STALE = time.time() - 3 * 86400  # three days old — comfortably past the age gate
FRESH = time.time() - 30         # half a minute old — an online double-text


@pytest.fixture(autouse=True)
def _reset_seen():
    c._seen_ids.clear()
    c._seen_ids_order.clear()
    yield


def _msg(i, *, ts, content="哥哥在吗", source="chat", content_type="text", role="user"):
    return {"role": role, "content": content, "content_type": content_type,
            "ts": ts, "id": f"m{i}", "source": source}


# ---------------------------------------------------------------------------
# _collapse_stale_backlog — unit behavior
# ---------------------------------------------------------------------------

def test_stale_pile_merges_into_newest_with_deferred_seen_ownership():
    msgs = [_msg(1, ts=STALE), _msg(2, ts=STALE + 60), _msg(3, ts=STALE + 120)]
    out = c._collapse_stale_backlog(msgs)
    assert len(out) == 1
    merged = out[0]
    assert merged["id"] == "m3"  # replaces the newest in place
    assert "3 条消息" in merged["content"]
    assert merged["content"].count("哥哥在吗") == 3
    assert "综合它们的内容和情绪自然地回复一次" in merged["content"]
    # Absorbed keys travel on the carrier and are marked seen only after the
    # carrier's reply lands — never at merge time (a carrier that fails a
    # transient write must leave the whole pile retryable).
    assert merged["_backlog_absorbed_keys"] == ["m1", "m2"]
    assert "m1" not in c._seen_ids and "m2" not in c._seen_ids
    assert "m3" not in c._seen_ids  # the carrier is processed by the main loop


def test_below_min_pile_does_not_merge():
    msgs = [_msg(1, ts=STALE), _msg(2, ts=STALE + 60)]
    assert c._collapse_stale_backlog(msgs) == msgs
    assert not c._seen_ids


def test_fresh_burst_never_merges():
    # An online user double-texting three times must keep per-message replies.
    msgs = [_msg(1, ts=FRESH - 20), _msg(2, ts=FRESH - 10), _msg(3, ts=FRESH)]
    assert c._collapse_stale_backlog(msgs) == msgs


def test_fresh_message_joins_an_already_stale_pile():
    # Once the stale pile triggers, a fresh message in the same batch merges
    # too — one coherent reply instead of backlog-reply + separate fresh-reply.
    msgs = [_msg(1, ts=STALE), _msg(2, ts=STALE + 60), _msg(3, ts=STALE + 120),
            _msg(4, ts=FRESH, content="刚到家啦")]
    out = c._collapse_stale_backlog(msgs)
    assert len(out) == 1
    assert out[0]["id"] == "m4"
    assert "刚到家啦" in out[0]["content"]
    assert "4 条消息" in out[0]["content"]


def test_exclusions_keep_their_own_pipelines():
    msgs = [
        _msg(1, ts=STALE),
        _msg(2, ts=STALE + 1),
        _msg(3, ts=STALE + 2),
        _msg(4, ts=STALE + 3, source="verify_ping"),
        _msg(5, ts=STALE + 4, source=c.RESIDENT_MAINTENANCE_SOURCE),
        _msg(6, ts=STALE + 5, content_type="image", content=""),
        _msg(7, ts=STALE + 6, content=""),          # unreadable — decrypt path
        _msg(8, ts=STALE + 7, role="openclaw"),      # not a user turn
    ]
    out = c._collapse_stale_backlog(msgs)
    ids = [m["id"] for m in out]
    # texts m1-m3 merged into m3; every excluded row still present untouched
    assert ids == ["m3", "m4", "m5", "m6", "m7", "m8"]
    assert "3 条消息" in out[0]["content"]


def test_one_stale_leftover_cannot_swallow_online_double_text():
    # 1 stale + 2 fresh: the trigger counts STALE messages only — a single
    # leftover old turn must not pull an online double-text into a merge.
    msgs = [_msg(1, ts=STALE), _msg(2, ts=FRESH - 10), _msg(3, ts=FRESH)]
    assert c._collapse_stale_backlog(msgs) == msgs


def test_stale_pile_plus_fresh_still_merges():
    # 3 stale (>= MIN) + 1 fresh: pile triggers, fresh joins the carrier.
    msgs = [_msg(1, ts=STALE), _msg(2, ts=STALE + 60), _msg(3, ts=STALE + 120),
            _msg(4, ts=FRESH, content="现在有空啦")]
    out = c._collapse_stale_backlog(msgs)
    assert len(out) == 1
    assert out[0]["id"] == "m4"
    assert "4 条消息" in out[0]["content"]
    assert "现在有空啦" in out[0]["content"]


def test_kill_switch_disables_collapse(monkeypatch):
    monkeypatch.setattr(c, "BACKLOG_COLLAPSE_MIN", 0)
    msgs = [_msg(1, ts=STALE), _msg(2, ts=STALE + 60), _msg(3, ts=STALE + 120)]
    assert c._collapse_stale_backlog(msgs) == msgs


def test_merged_prompt_is_bounded_and_keeps_newest_lines():
    many = [_msg(i, ts=STALE + i, content=f"第{i}条") for i in range(60)]
    long_one = _msg(99, ts=STALE + 999, content="长" * 2000)
    out = c._collapse_stale_backlog(many + [long_one])
    body = out[0]["content"]
    assert "61 条消息" in body
    assert f"更早的 {61 - c._BACKLOG_COLLAPSE_MAX_LINES} 条较旧消息未逐条列出" in body
    assert "第59条" in body        # newest plain line survives trimming
    assert "第0条" not in body     # oldest line trimmed
    assert "…(截断)" in body       # oversized single message truncated
    assert len(body) < c._BACKLOG_COLLAPSE_MAX_LINES * (c._BACKLOG_COLLAPSE_LINE_CHARS + 40)


def test_already_seen_messages_do_not_remerge():
    msgs = [_msg(1, ts=STALE), _msg(2, ts=STALE + 60), _msg(3, ts=STALE + 120)]
    c._mark_seen("m1")
    c._mark_seen("m2")
    out = c._collapse_stale_backlog(msgs)
    assert out == msgs  # only one unseen eligible → below min, no merge


# ---------------------------------------------------------------------------
# Merged turn through _process_messages — exactly one agent call, one reply
# ---------------------------------------------------------------------------

def _neutralize_pipeline(monkeypatch, agent_calls, replies):
    monkeypatch.setattr(c, "FEEDLING_ENCLAVE_URL", "https://enclave.example/")
    monkeypatch.setattr(c, "_reset_proactive_idle_guard", lambda: None)
    monkeypatch.setattr(c, "_clear_proactive_failure", lambda: None)
    monkeypatch.setattr(c, "_emit_debug_trace", lambda *a, **k: None)
    monkeypatch.setattr(c, "_screen_context_for_message", lambda content: ("", [], []))
    monkeypatch.setattr(c, "_worldbook_context_for_foreground", lambda content: "")
    monkeypatch.setattr(c, "_quoted_memory_context", lambda msg: "")
    monkeypatch.setattr(c, "_prepend_time_anchor_foreground", lambda content, ts: content)
    monkeypatch.setattr(c, "_foreground_agent_message", lambda content, current_ts=0: content)
    monkeypatch.setattr(c, "_resident_chat_runtime_v2_enabled", lambda: False)
    monkeypatch.setattr(c, "_consume_reply_parse_failed", lambda: False)
    monkeypatch.setattr(c, "_note_agent_turn_success", lambda: None)
    monkeypatch.setattr(
        c, "call_agent",
        lambda content, *a, **k: agent_calls.append(content) or ["都在的,抱歉让你等啦"],
    )
    monkeypatch.setattr(c, "execute_agent_actions", lambda actions: {"effects": []})
    monkeypatch.setattr(
        c, "post_reply", lambda *a, **k: replies.append((a, k)) or {"ok": True}
    )


def test_process_messages_answers_stale_pile_with_single_agent_turn(monkeypatch):
    agent_calls: list = []
    replies: list = []
    _neutralize_pipeline(monkeypatch, agent_calls, replies)
    msgs = [_msg(1, ts=STALE), _msg(2, ts=STALE + 60),
            _msg(3, ts=STALE + 120, content="怎么不理我")]
    latest = c._process_messages(msgs)
    assert len(agent_calls) == 1, "the pile must produce exactly one agent turn"
    assert "哥哥在吗" in agent_calls[0] and "怎么不理我" in agent_calls[0]
    assert len(replies) == 1
    assert latest == pytest.approx(STALE + 120)


def test_process_messages_below_min_keeps_per_message_turns(monkeypatch):
    agent_calls: list = []
    replies: list = []
    _neutralize_pipeline(monkeypatch, agent_calls, replies)
    msgs = [_msg(1, ts=STALE), _msg(2, ts=STALE + 60)]
    c._process_messages(msgs)
    assert len(agent_calls) == 2
    assert len(replies) == 2


def test_successful_merged_turn_marks_absorbed_seen(monkeypatch):
    agent_calls: list = []
    replies: list = []
    _neutralize_pipeline(monkeypatch, agent_calls, replies)
    msgs = [_msg(1, ts=STALE), _msg(2, ts=STALE + 60), _msg(3, ts=STALE + 120)]
    c._process_messages(msgs)
    assert len(replies) == 1
    # Only after the reply landed are the absorbed messages truly consumed.
    assert "m1" in c._seen_ids and "m2" in c._seen_ids and "m3" in c._seen_ids


def test_transient_post_failure_keeps_whole_pile_retryable(monkeypatch):
    """codex3 fault-injection scenario: a transient reply-write failure must
    keep the checkpoint AND leave the pile re-mergeable — round two re-forms
    the full merged prompt and lands exactly one reply."""
    agent_calls: list = []
    posts: list = []
    _neutralize_pipeline(monkeypatch, agent_calls, posts)

    attempts = {"n": 0}

    def flaky_post(reply, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient 502 from backend")
        posts.append((reply, kwargs))
        return {"ok": True}

    monkeypatch.setattr(c, "post_reply", flaky_post)
    original = [_msg(1, ts=STALE), _msg(2, ts=STALE + 60),
                _msg(3, ts=STALE + 120, content="怎么不理我")]

    first_latest = c._process_messages([dict(m) for m in original])
    assert first_latest == 0.0, "checkpoint must stay behind the failed turn"
    assert posts == []
    assert not ({"m1", "m2", "m3"} & c._seen_ids), "pile must remain retryable"

    second_latest = c._process_messages([dict(m) for m in original])
    assert len(agent_calls) == 2, "retry round must re-run the merged turn"
    assert "哥哥在吗" in agent_calls[1] and "怎么不理我" in agent_calls[1]
    assert len(posts) == 1
    assert second_latest == pytest.approx(STALE + 120)
    assert {"m1", "m2", "m3"} <= c._seen_ids


def test_mixed_batch_earlier_media_failure_defers_carrier(monkeypatch):
    """An excluded media turn failing its reply write must stop the batch:
    the merged carrier is NOT executed that round (its success would advance
    the checkpoint past the failed media and supersede it forever), and the
    whole batch — media and pile — retries next round."""
    agent_calls: list = []
    posts: list = []
    _neutralize_pipeline(monkeypatch, agent_calls, posts)
    monkeypatch.setattr(c, "_image_payloads_from_msg", lambda m: [])
    monkeypatch.setattr(c, "_image_file_paths_for_msg", lambda m: [])

    attempts = {"n": 0}

    def flaky_post(reply, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient write error")
        posts.append((reply, kwargs))
        return {"ok": True}

    monkeypatch.setattr(c, "post_reply", flaky_post)
    original = [
        _msg(0, ts=STALE - 60, content_type="image", content=""),
        _msg(1, ts=STALE),
        _msg(2, ts=STALE + 60),
        _msg(3, ts=STALE + 120),
    ]

    first_latest = c._process_messages([dict(m) for m in original])
    assert first_latest == 0.0
    assert len(agent_calls) == 1, "only the failed media turn ran this round"
    assert "3 条消息" not in agent_calls[0]
    assert not ({"m1", "m2", "m3"} & c._seen_ids)

    second_latest = c._process_messages([dict(m) for m in original])
    assert len(agent_calls) == 3, "retry: media turn + merged carrier turn"
    assert any("3 条消息" in call for call in agent_calls[1:])
    assert len(posts) == 2
    assert second_latest == pytest.approx(STALE + 120)
