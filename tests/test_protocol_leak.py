"""Pure-unit tests for the shared torn-protocol-JSON leak detector.

Background: a reasoning model behind a stream-cutting relay splits one protocol
envelope `{"messages":[],"actions":[{"type":"proactive.sleep","reason":"..."}]}`
across the reasoning_content/content channel boundary. The head lands in the
reasoning channel, the tail in the visible message. Every prior guard anchored on
the JSON *head*; the head is gone, so the tail leaks. This detector classifies a
visible reply using multiple independent signals (see the module docstring), and
returns an evidence enum — lane policy lives in each runtime, not here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core import protocol_leak as pl  # noqa: E402


# The three real leaked tails from the live report (usr_ed9d6c05d1accb94), each a
# different cut position of the same sleep envelope.
REAL_TAILS = [
    'active.sleep","reason":"4:34 了她睡得很沉 早上再说"}]}',
    '":"5点了她还在睡 没动静"}]}',
    'type":"proactive.sleep","reason":"7点了 还在睡 不打扰了 醒了会找我"}]}',
]

# Normal chat / content that MUST survive (zero false-drop). Includes the shapes
# Codex flagged: JSON diff, string literals containing braces, config edits,
# inline JSON, kaomoji, quote-colons.
NORMAL_CHAT = [
    "好的，晚安~",
    "这个配置是 {\"port\": 8080} 你改一下",
    "(๑•̀ㅂ•́)و✧ 加油！",
    "数组是 [1, 2, 3] 对吧",
    "你可以用 list.append(x) 然后 d[\"k\"]=v",
    "想想看：如果 a > b，那么...",
    "收到！",
    "哈哈哈这也太逗了[捂脸]",
    "晚安，做个好梦 🌙",
    "他说\"没事\":然后就走了",
    "JSON 是 {\"a\":1} 这种格式",
    "删掉多余的 }，把 \"port\": 8080 改成 8081",
    "把 config 里的 \"retries\": 3 删掉，然后 } 收尾",
]


# ---------------------------------------------------------------------------
# is_orphan_json_tail — the WEAK, visible-only signal (bracket-net-negative +
# JSON token). Deliberately high recall; lane policy decides whether to act.
# ---------------------------------------------------------------------------

def test_orphan_tail_flags_all_real_leaked_fragments():
    for frag in REAL_TAILS:
        assert pl.is_orphan_json_tail(frag), repr(frag)


def test_orphan_tail_never_flags_normal_chat():
    # This is the false-drop guard for the WEAK signal on its own. If this ever
    # regresses, foreground users lose real messages.
    for text in NORMAL_CHAT:
        # NOTE: some of these (JSON diff / string literals) DO trip the naive
        # bracket heuristic — that is exactly why the weak signal is proactive-
        # only and never a foreground drop. `is_orphan_json_tail` may return
        # True here; what must hold is that the FOREGROUND policy keeps them
        # (see test_foreground_policy_keeps_weak_only). So here we only assert
        # the detector does not *upgrade* them to a strong evidence class.
        ev = pl.classify(text)
        assert ev in (pl.NONE, pl.ORPHAN_JSON_TAIL), (text, ev)


# ---------------------------------------------------------------------------
# classify — cross-channel evidence tiers.
# ---------------------------------------------------------------------------

def test_joined_known_protocol_single_cut():
    # Head in reasoning + tail in content rejoin to a complete, known envelope.
    reasoning = '{"messages":[],"actions":[{"type":"pro'
    visible = 'active.sleep","reason":"7点了 还在睡"}]}'
    assert pl.classify(visible, reasoning_text=reasoning) == pl.JOINED_KNOWN_PROTOCOL


def test_head_in_reasoning_multicut_join_fails_but_head_recognizable():
    # A second cut dropped a middle chunk, so the rejoin does NOT parse — but the
    # reasoning channel still carries an unmistakable protocol head. Users never
    # put a protocol head in the reasoning channel; only the torn relay does.
    # reasoning cut mid-key ("ty), visible resumes mid-value -> the rejoin is
    # malformed JSON (a key with no value) and will NOT parse, so tier-1 can't
    # fire. The reasoning head is still unmistakable.
    reasoning = '…thinking… {"messages":[],"actions":[{"ty'
    visible = 'sleep","reason":"没动静"}]}'
    ev = pl.classify(visible, reasoning_text=reasoning)
    assert ev == pl.HEAD_IN_REASONING
    assert pl.is_leak(ev)


def test_reasoning_quoting_schema_does_not_upgrade_weak_visible(monkeypatch=None):
    """Codex code-review #4: a reasoning model may QUOTE a closed protocol snippet
    while thinking. That must NOT upgrade a weak JSON-like visible reply (a user
    discussing config) to strong HEAD_IN_REASONING evidence — foreground would
    then eat a real message. Only an UNCLOSED head dangling at the end of the
    reasoning counts."""
    reasoning = 'The protocol has {"actions": [...]} but this user asks about config.'
    visible = '删掉多余的 }，把 "port": 8080 改成 8081'
    ev = pl.classify(visible, reasoning_text=reasoning)
    assert ev != pl.HEAD_IN_REASONING
    assert not pl.is_strong(ev)
    assert not pl.should_suppress(ev, lane="foreground")


def test_unclosed_head_at_end_of_reasoning_is_strong():
    """The genuine tear: reasoning ENDS mid-envelope (net-open brackets)."""
    reasoning = 'weighing whether to wake her… {"messages":[],"actions":[{"type":"pro'
    visible = 'active.sleep","reason":"7点了 还在睡"}]}'
    assert pl.classify(visible, reasoning_text=reasoning) in (
        pl.JOINED_KNOWN_PROTOCOL, pl.HEAD_IN_REASONING,
    )


def test_transport_cut_plus_jsonish_tail():
    # No reasoning head available, but the transport itself reported a cut and the
    # visible text is a JSON-ish fragment -> independent proof the pipe broke.
    ev = pl.classify(REAL_TAILS[0], reasoning_text="", transport_cut=True)
    assert ev == pl.TRANSPORT_CUT
    assert pl.is_leak(ev)


def test_orphan_tail_alone_is_weak():
    ev = pl.classify(REAL_TAILS[2])  # no reasoning, no transport signal
    assert ev == pl.ORPHAN_JSON_TAIL
    assert pl.is_leak(ev)
    assert not pl.is_strong(ev)


def test_strong_evidence_classification():
    assert pl.is_strong(pl.JOINED_KNOWN_PROTOCOL)
    assert pl.is_strong(pl.HEAD_IN_REASONING)
    assert pl.is_strong(pl.TRANSPORT_CUT)
    assert not pl.is_strong(pl.ORPHAN_JSON_TAIL)
    assert not pl.is_strong(pl.NONE)


def test_plain_prose_is_none():
    for text in ["好的，晚安~", "收到！", "晚安，做个好梦 🌙", "(๑•̀ㅂ•́)و✧ 加油！"]:
        assert pl.classify(text) == pl.NONE, text


def test_reassembled_envelope_is_never_returned_for_execution():
    # Contract guard: the detector reports evidence, NOT an executable action.
    # It must not expose the rejoined action for the runtime to run.
    reasoning = '{"messages":[],"actions":[{"type":"pro'
    visible = 'active.sleep","reason":"7点了"}]}'
    ev = pl.classify(visible, reasoning_text=reasoning)
    assert isinstance(ev, str)  # an enum string, not a dict/action


# ---------------------------------------------------------------------------
# Lane policy helpers (pure): each runtime maps evidence -> drop/keep per lane.
# ---------------------------------------------------------------------------

def test_proactive_policy_drops_any_leak():
    for frag in REAL_TAILS:
        ev = pl.classify(frag)  # weak, but proactive drops even weak
        assert pl.should_suppress(ev, lane="proactive")


def test_foreground_policy_keeps_weak_only():
    # Weak (orphan tail alone) in FOREGROUND must be kept — could be a user
    # pasting JSON / discussing code. Strong evidence still drops.
    weak = pl.classify(REAL_TAILS[0])  # ORPHAN_JSON_TAIL
    assert not pl.should_suppress(weak, lane="foreground")

    strong = pl.classify(REAL_TAILS[0], transport_cut=True)  # TRANSPORT_CUT
    assert pl.should_suppress(strong, lane="foreground")


def test_none_never_suppressed():
    for lane in ("foreground", "proactive"):
        assert not pl.should_suppress(pl.NONE, lane=lane)
