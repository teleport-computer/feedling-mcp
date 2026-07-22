"""Pure-unit tests for rename pairing validation (no DB required)."""
from identity import card_policy


def test_rename_without_intro_rejected():
    ok, err = card_policy.validate_rename_pairing({"agent_name": "老8"})
    assert (ok, err) == (False, "rename_requires_self_introduction")


def test_rename_with_intro_ok():
    ok, _ = card_policy.validate_rename_pairing(
        {"agent_name": "老8", "self_introduction": "我是老8"}
    )
    assert ok


def test_intro_only_ok():
    ok, _ = card_policy.validate_rename_pairing({"self_introduction": "hi"})
    assert ok


def test_empty_name_not_a_rename():
    ok, _ = card_policy.validate_rename_pairing({"agent_name": "  "})
    assert ok  # 空名交给既有 agent_name_empty 校验管,不归本规则


def test_punctuation_only_name_not_a_rename():
    # Backtick-only name is not considered a rename (falls through to
    # agent_name_empty validation downstream). Validation must use the same
    # punctuation strip set as actions.py normalization to prevent mismatch.
    ok, _ = card_policy.validate_rename_pairing({"agent_name": "`"})
    assert ok  # 纯标点名字也不归本规则,让下游 agent_name_empty 处理


def test_stripped_agent_name_curly_quotes():
    # Verify curly quotes are properly stripped. This was a regression:
    # the original charset had U+201C/U+201D ("") but a retyping mistake
    # dropped them, causing \u201c老8\u201d to not strip to "老8".
    # Use Unicode escapes to prevent tools from flattening the curly quotes.
    stripped = card_policy.stripped_agent_name("\u201c老8\u201d")
    assert stripped == "老8", f"Expected '老8' but got {repr(stripped)}"


def test_curly_quotes_only_not_a_rename():
    # Curly-quote-only name should not be considered a rename.
    # This tests the specific bug where dropping curly quotes from the
    # charset caused \u201c\u201d to count as non-empty.
    ok, _ = card_policy.validate_rename_pairing({"agent_name": "\u201c\u201d"})
    assert ok  # Curly quotes only, should be treated as empty name