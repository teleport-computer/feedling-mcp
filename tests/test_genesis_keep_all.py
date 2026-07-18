"""A: long-term-memory archive uploads get a "keep_all" (尽量收) directive appended to the
fact_map / fact_write prompts. Chat-history and onboarding (keep_all=False) MUST be byte-identical
to before — the directive is purely additive so it can never bleed into the normal flow.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from genesis import prompts  # noqa: E402


def test_fact_map_keep_all_off_is_unchanged():
    off = prompts.fact_map_messages("chunk")[0]["content"]
    assert off == prompts.FACT_MAP_PROMPT + prompts._STRICT_JSON_SUFFIX


def test_fact_map_keep_all_on_appends_directive():
    on = prompts.fact_map_messages("chunk", keep_all=True)[0]["content"]
    assert prompts.FACT_MAP_KEEP_ALL_SUFFIX in on
    assert on.startswith(prompts.FACT_MAP_PROMPT)


def test_fact_write_keep_all_off_is_unchanged():
    off = prompts.fact_write_messages([])[0]["content"]
    assert off == prompts.FACT_WRITE_PROMPT + prompts._STRICT_JSON_SUFFIX
    assert '"occurred_at":"YYYY-MM-DD or empty"' in off


def test_fact_write_keep_all_on_appends_directive():
    on = prompts.fact_write_messages([], keep_all=True)[0]["content"]
    assert prompts.FACT_WRITE_KEEP_ALL_SUFFIX in on
    assert on.startswith(prompts.FACT_WRITE_PROMPT)
    assert "date 或 occurred_at" in on
    assert "tags" in on
    assert "threads" in on


def test_fact_write_memory_summary_auto_enables_keep_all_directive():
    on = prompts.fact_write_messages([], memory_summary="2024-05-01: 用户整理好的长期记忆")[0]["content"]
    assert prompts.FACT_WRITE_KEEP_ALL_SUFFIX in on
