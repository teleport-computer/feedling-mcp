import asyncio
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))
from model_api_runtime.v2 import compaction

def _fake_llm_returning(text):
    async def _llm(cfg, messages, **kw):
        _llm.seen = messages
        return {"reply": text}
    return _llm


def _fake_llm_result(result):
    async def _llm(cfg, messages, **kw):
        return result
    return _llm

def test_compact_appends_new_bullets_preserving_old():
    llm = _fake_llm_returning("- user asked about dogs")
    out = asyncio.run(compaction.compact(provider_config=object(), current_summary="- talked about cats",
                                         old_messages=[{"role":"user","content":"tell me about dogs"}], llm=llm))
    assert "- talked about cats" in out and "- user asked about dogs" in out
    assert out.index("cats") < out.index("dogs")

def test_compact_empty_summary_is_just_new():
    llm = _fake_llm_returning("- first thing")
    out = asyncio.run(compaction.compact(provider_config=object(), current_summary="",
                                         old_messages=[{"role":"user","content":"x"}], llm=llm))
    assert out.strip() == "- first thing"

def test_compact_blank_llm_reply_is_noop():
    llm = _fake_llm_returning("   ")
    out = asyncio.run(compaction.compact(provider_config=object(), current_summary="- keep me",
                                         old_messages=[{"role":"user","content":"x"}], llm=llm))
    assert out == "- keep me"

def test_compact_passes_old_messages_to_llm():
    llm = _fake_llm_returning("- ok")
    asyncio.run(compaction.compact(provider_config=object(), current_summary="",
                                   old_messages=[{"role":"user","content":"SECRET_MARKER"}], llm=llm))
    assert "SECRET_MARKER" in str(llm.seen)


@pytest.mark.parametrize("reply", [
    "Here is the summary:\n- new item",
    "* wrong markdown marker",
    "- valid item\ncontinuation prose",
    "- ",
    "```\n- fenced item\n```",
])
def test_compact_malformed_output_is_full_noop(reply):
    current = "- keep me exactly  "
    out = asyncio.run(compaction.compact(
        provider_config=object(), current_summary=current,
        old_messages=[{"role": "user", "content": "x"}],
        llm=_fake_llm_returning(reply),
    ))
    assert out == current


@pytest.mark.parametrize("reply", [
    "- talked about cats",
    "-   TALKED   about CATS  ",
    "- genuinely new\n- talked about cats",
])
def test_compact_duplicate_existing_output_is_full_noop(reply):
    current = "- talked about cats"
    out = asyncio.run(compaction.compact(
        provider_config=object(), current_summary=current,
        old_messages=[{"role": "user", "content": "x"}],
        llm=_fake_llm_returning(reply),
    ))
    assert out == current


def test_compact_duplicate_within_new_batch_is_full_noop():
    current = "- old"
    out = asyncio.run(compaction.compact(
        provider_config=object(), current_summary=current,
        old_messages=[{"role": "user", "content": "x"}],
        llm=_fake_llm_returning("- new fact\n- NEW   FACT"),
    ))
    assert out == current


@pytest.mark.parametrize("reply", [
    "\n".join(f"- item {i}" for i in range(compaction._MAX_NEW_BULLETS + 1)),
    "- " + "x" * (compaction._MAX_NEW_BULLET_CHARS + 1),
    "\n".join(f"- item-{i}-" + "x" * 890 for i in range(9)),
])
def test_compact_out_of_bounds_output_is_full_noop(reply):
    current = "- old"
    out = asyncio.run(compaction.compact(
        provider_config=object(), current_summary=current,
        old_messages=[{"role": "user", "content": "x"}],
        llm=_fake_llm_returning(reply),
    ))
    assert out == current


def test_compact_normalizes_valid_multiline_batch_before_append():
    out = asyncio.run(compaction.compact(
        provider_config=object(), current_summary="- old",
        old_messages=[{"role": "user", "content": "x"}],
        llm=_fake_llm_returning("\n- first new item   \n- second new item\n"),
    ))
    assert out == "- old\n- first new item\n- second new item"


def test_compact_valid_append_preserves_existing_summary_byte_for_byte():
    current = "- old item with trailing spaces  \n"
    out = asyncio.run(compaction.compact(
        provider_config=object(), current_summary=current,
        old_messages=[{"role": "user", "content": "x"}],
        llm=_fake_llm_returning("- new item"),
    ))
    assert out.startswith(current)
    assert out == current + "- new item"


def test_compact_non_string_provider_reply_is_noop():
    out = asyncio.run(compaction.compact(
        provider_config=object(), current_summary="- old",
        old_messages=[{"role": "user", "content": "x"}],
        llm=_fake_llm_returning(["- not", "a string"]),
    ))
    assert out == "- old"


@pytest.mark.parametrize("result", [None, [], "- not a response object"])
def test_compact_malformed_provider_result_is_noop(result):
    out = asyncio.run(compaction.compact(
        provider_config=object(), current_summary="- old",
        old_messages=[{"role": "user", "content": "x"}],
        llm=_fake_llm_result(result),
    ))
    assert out == "- old"
