"""Regression guard (Codex I4): raw-text memory lanes are NOT a torn-protocol
leak vector.

The capture/dream lanes read only the provider `content` channel (`raw_text=True`
bypasses the chat-bubble sanitizer). A stream-cut relay can drop a torn protocol
tail into that channel. This must produce NO memory write — the lanes' own JSON
parsers already reject a bare tail. We assert that here so a future change to the
memory parsers (or the provider extractor) can't silently start persisting torn
debris as a card / consolidation.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from memory.capture_prompt_v1 import parse_capture_cards  # noqa: E402
from memory.dream_prompt_v1 import parse_dream_consolidations  # noqa: E402

TORN_TAILS = [
    'active.sleep","reason":"4:34 了她睡得很沉 早上再说"}]}',
    '":"5点了她还在睡 没动静"}]}',
    'type":"proactive.sleep","reason":"7点了 还在睡 不打扰了 醒了会找我"}]}',
]


def test_capture_lane_never_writes_a_torn_tail():
    for tail in TORN_TAILS:
        cards, error = parse_capture_cards(tail)
        assert cards == [], tail          # nothing persisted
        assert error is not None, tail    # and it is flagged as a parse failure


def test_dream_lane_never_writes_a_torn_tail():
    for tail in TORN_TAILS:
        cons, _questions, error = parse_dream_consolidations(tail)
        assert not cons, tail
        assert error is not None, tail
