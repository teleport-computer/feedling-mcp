"""T768: V2 records whether the captions typed with this turn's images/files reached
the provider request — counts and lengths only, never the text.

T745 found plaintext captions silently replaced by `[image]` for weeks; prod had no
trace that could show it. Delivery is bound to each attachment's own message: the
probe only accepts the exact content object the prompt builder rendered for that row,
and only when that object is in the request. These tests drive the real
`_ledger_tapped_sink` → `_emit_prompt_frontier_trace` path with a real budget plan;
the process_job/seq wiring is covered in test_v2_worker.py and
test_v2_worker_tool_loop.py (`t768`).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent))

from model_api_runtime.v2 import prompt_frontier  # noqa: E402
from model_api_runtime.v2 import worker  # noqa: E402
from test_v2_worker_unit_telemetry import _minimal_deps  # noqa: E402

CAPTION = "PRIVATE_CAPTION_这是什么"
FILE_CAPTION = "PRIVATE_FILE_CAPTION_看看这份报告"


def _plan():
    limit = prompt_frontier.ModelPromptLimit(
        provider="test", model="caption-test", context_window_tokens=64_000, source="caller")
    return prompt_frontier.plan_provider_round(
        model_limit=limit,
        messages=[{"role": "system", "content": "x" * 100}],
        tools=None,
        output_reserve_tokens=512,
        safety_margin_tokens=512,
        message_component_bytes=(prompt_frontier.PromptByteComponent("system", 100),),
    )


def _sink(probe):
    traces = []
    deps = _minimal_deps()
    deps.emit_debug_trace = lambda user_id, event_type, **fields: traces.append(
        {"type": event_type, **fields})
    sink = worker._ledger_tapped_sink(
        None, deps=deps, user_id="u_caption", lane="chat", caption_probe=probe)
    return sink, traces


def _request(sink, messages):
    asyncio.run(sink("provider_request", {"prompt_frontier": _plan(), "messages": messages}))


def _budgets(traces):
    return [t for t in traces if t["type"] == "v2.prompt_frontier.budget"]


def _image(row_id, caption, content, *, seq=10):
    return {"id": row_id, "seq": seq, "role": "user", "has_image": True,
            "caption": caption, "content": content}


def _file(row_id, caption, content, *, seq=10):
    return {"id": row_id, "seq": seq, "role": "user", "has_file": True,
            "caption": caption, "content": content}


def _history(row_id, content, *, role="user", seq=1):
    return {"id": row_id, "seq": seq, "role": role, "content": content}


def _build(tail, expected, transcript=()):
    """Real prompt builder: returns (probe, messages it would send)."""
    probe = worker._AttachmentCaptionProbe()
    probe.expect(expected)
    build = worker._make_build_messages_fn(
        system_prompt="SYSTEM", summary="", tail=list(tail), caption_probe=probe)
    return probe, build(list(transcript))


def _message_for(messages, content):
    return [m for m in messages if isinstance(m, dict) and m.get("content") is content]


def test_caption_in_its_own_native_image_blocks_counts_as_delivered():
    blocks = [{"type": "image", "source": {"data": "QUFB" * 1000}},
              {"type": "text", "text": CAPTION}]
    row = _image("m1", CAPTION, blocks)
    probe, messages = _build([row], [row])
    sink, traces = _sink(probe)
    _request(sink, messages)
    trace = _budgets(traces)[0]
    assert trace["detail"]["attachment_captions"] == {
        "images": 1, "files": 0, "with_caption": 1, "delivered": 1, "undetermined": 0,
        "caption_chars": len(CAPTION), "delivered_chars": len(CAPTION), "undetermined_chars": 0}
    assert CAPTION not in repr(trace)


def test_same_words_elsewhere_never_stand_in_for_a_missing_caption():
    """codex3 r1 ①: earlier user/assistant text and system text with the same words
    do not count when this turn's own message only carries the marker (T745 shape)."""
    row = _image("m1", CAPTION, "[image]")
    tail = [_history("h1", CAPTION), _history("h2", CAPTION, role="assistant", seq=2), row]
    probe, messages = _build(tail, [row])
    sink, traces = _sink(probe)
    _request(sink, [{"role": "system", "content": "earlier: " + CAPTION}, *messages])
    captions = _budgets(traces)[0]["detail"]["attachment_captions"]
    assert captions["with_caption"] == 1 and captions["delivered"] == 0
    assert captions["delivered_chars"] == 0


def test_two_attachments_sharing_one_string_object_count_per_message():
    """codex3 r2: same caption, same string object; only one message survives."""
    shared = CAPTION
    first, second = _image("m1", shared, shared, seq=10), _image("m2", shared, shared, seq=11)
    probe, messages = _build([first, second], [first, second])
    both = _message_for(messages, shared)
    assert len(both) == 2 and both[0] is not both[1]
    sink, traces = _sink(probe)
    _request(sink, [m for m in messages if m is not both[1]])
    captions = _budgets(traces)[0]["detail"]["attachment_captions"]
    assert captions["with_caption"] == 2 and captions["delivered"] == 1


def test_trimmed_row_is_not_rescued_by_a_history_message_sharing_its_string():
    """codex3 r2: this turn's message was cut, an older message holds the same object."""
    shared = CAPTION
    old = _history("h1", shared, seq=1)
    row = _image("m1", shared, shared, seq=10)
    probe, messages = _build([old, row], [row])
    rendered = _message_for(messages, shared)
    assert len(rendered) == 2
    sink, traces = _sink(probe)
    _request(sink, [m for m in messages if m is not rendered[1]])
    assert _budgets(traces)[0]["detail"]["attachment_captions"]["delivered"] == 0


def test_delivery_survives_the_loops_shallow_message_copies():
    """tool_loop._with_system_suffix copies every message dict before the request;
    the binding must not depend on the dict instance."""
    from model_api_runtime.v2 import tool_loop

    row = _image("m1", CAPTION, CAPTION)
    probe, messages = _build([row], [row])
    copied = tool_loop._with_system_suffix(messages, "retry instruction")
    assert not any(a is b for a in copied for b in messages if isinstance(a, dict))
    sink, traces = _sink(probe)
    _request(sink, copied)
    assert _budgets(traces)[0]["detail"]["attachment_captions"]["delivered"] == 1


def test_trimmed_history_sharing_the_string_does_not_hide_the_kept_row():
    """The usual trim drops the oldest turn; a message between them anchors the row."""
    shared = CAPTION
    old = _history("h1", shared, seq=1)
    between = _history("h2", "something in between", seq=5)
    row = _image("m1", shared, shared, seq=10)
    probe, messages = _build([old, between, row], [row])
    rendered = _message_for(messages, shared)
    assert len(rendered) == 2
    sink, traces = _sink(probe)
    _request(sink, [m for m in messages if m is not rendered[0]])
    assert _budgets(traces)[0]["detail"]["attachment_captions"]["delivered"] == 1


def test_ambiguous_owner_including_a_non_attachment_row_is_never_counted():
    """No anchor can place the kept message, and a row that is not this turn's
    attachment could own it: under-count rather than claim delivery."""
    shared = CAPTION
    row = _image("m1", shared, shared, seq=10)
    later_text = _history("t2", shared, seq=11)
    probe, messages = _build([row, later_text], [row])
    rendered = _message_for(messages, shared)
    assert len(rendered) == 2
    sink, traces = _sink(probe)
    _request(sink, [m for m in messages if m is not rendered[0]])
    assert _budgets(traces)[0]["detail"]["attachment_captions"]["delivered"] == 0


def test_undecidable_owner_is_reported_as_undetermined_not_as_missing():
    """codex3 r3: history and this turn's row share one string object, sit next to
    each other, and both reach the request. Which message is this turn's cannot be
    told apart, so it must read as undetermined — not the T745 'missing' shape."""
    shared = CAPTION
    old = _history("h1", shared, seq=9)
    row = _image("m1", shared, shared, seq=10)
    probe, messages = _build([old, row], [row])
    assert len(_message_for(messages, shared)) == 2
    sink, traces = _sink(probe)
    _request(sink, messages)
    undecided = _budgets(traces)[0]["detail"]["attachment_captions"]
    assert undecided["with_caption"] == 1
    assert undecided["delivered"] == 0 and undecided["undetermined"] == 1
    assert undecided["undetermined_chars"] == len(CAPTION)

    missing_row = _image("m1", CAPTION, "[image]", seq=10)
    probe, messages = _build([_history("h1", CAPTION, seq=9), missing_row], [missing_row])
    sink, traces = _sink(probe)
    _request(sink, messages)
    missing = _budgets(traces)[0]["detail"]["attachment_captions"]
    assert missing["delivered"] == 0 and missing["undetermined"] == 0
    assert undecided != missing


def test_a_row_the_builder_never_rendered_is_not_delivered():
    row = _image("m1", CAPTION, CAPTION)
    probe, messages = _build([], [row])
    sink, traces = _sink(probe)
    _request(sink, [*messages, {"role": "user", "content": CAPTION}])
    assert _budgets(traces)[0]["detail"]["attachment_captions"]["delivered"] == 0


def test_vision_observation_carrier_counts():
    rendered_content = "Image 1:\na cat\n\n以下是用户随这些图片发来的文字:\n" + CAPTION
    row = _image("m1", CAPTION, rendered_content)
    probe, messages = _build([row], [row])
    sink, traces = _sink(probe)
    _request(sink, messages)
    assert _budgets(traces)[0]["detail"]["attachment_captions"]["delivered"] == 1


def test_file_caption_in_its_own_text_carrier_counts():
    row = _file("f1", FILE_CAPTION, FILE_CAPTION + "\n[file text…]")
    probe, messages = _build([row], [row])
    sink, traces = _sink(probe)
    _request(sink, messages)
    captions = _budgets(traces)[0]["detail"]["attachment_captions"]
    assert captions["files"] == 1 and captions["delivered"] == 1


def test_attachment_without_caption_is_counted_but_not_expected():
    row = _image("m1", "", "[image]")
    probe, messages = _build([row], [row])
    sink, traces = _sink(probe)
    _request(sink, messages)
    assert _budgets(traces)[0]["detail"]["attachment_captions"] == {
        "images": 1, "files": 0, "with_caption": 0, "delivered": 0, "undetermined": 0,
        "caption_chars": 0, "delivered_chars": 0, "undetermined_chars": 0}


def test_late_fold_joins_once_and_later_rounds_do_not_double_count():
    """codex3 r1 ②: a row folded in mid-turn is rendered through the transcript."""
    text = _history("t1", "hi", seq=1)
    probe = worker._AttachmentCaptionProbe()
    probe.expect([text])
    build = worker._make_build_messages_fn(
        system_prompt="SYSTEM", summary="", tail=[text], caption_probe=probe)
    sink, traces = _sink(probe)
    _request(sink, build([]))
    folded = _image("m2", CAPTION, CAPTION, seq=2)
    asyncio.run(sink("late_input_fold", {"round": 2, "messages": [folded]}))
    _request(sink, build([folded]))
    asyncio.run(sink("late_input_fold", {"round": 3, "messages": [folded]}))
    _request(sink, build([folded]))
    budgets = _budgets(traces)
    assert "attachment_captions" not in budgets[0]["detail"]
    expected = {"images": 1, "files": 0, "with_caption": 1, "delivered": 1, "undetermined": 0,
                "caption_chars": len(CAPTION), "delivered_chars": len(CAPTION), "undetermined_chars": 0}
    assert budgets[1]["detail"]["attachment_captions"] == expected
    assert budgets[2]["detail"]["attachment_captions"] == expected


def test_turn_without_attachments_keeps_the_budget_shape():
    text = _history("t1", "hi")
    for expected in (None, [], [text]):
        probe, messages = _build([text], expected or [])
        sink, traces = _sink(None if expected is None else probe)
        _request(sink, messages)
        assert "attachment_captions" not in _budgets(traces)[0]["detail"]


def test_assistant_rows_are_not_expected():
    row = {**_image("m1", CAPTION, CAPTION), "role": "assistant"}
    probe, messages = _build([row], [row])
    sink, traces = _sink(probe)
    _request(sink, messages)
    assert "attachment_captions" not in _budgets(traces)[0]["detail"]


def test_a_broken_probe_never_fails_the_builder_the_turn_or_the_budget_trace():
    class Broken(worker._AttachmentCaptionProbe):
        def observe(self, provider_request):
            raise RuntimeError("boom")

        def expect(self, rows):
            raise RuntimeError("boom")

        def note_message(self, row, message):
            raise RuntimeError("boom")

    row = _image("m1", CAPTION, CAPTION)
    build = worker._make_build_messages_fn(
        system_prompt="SYSTEM", summary="", tail=[row], caption_probe=Broken())
    messages = build([row])
    sink, traces = _sink(Broken())
    asyncio.run(sink("late_input_fold", {"messages": [row]}))
    _request(sink, messages)
    assert "attachment_captions" not in _budgets(traces)[0]["detail"]


def test_admin_view_shows_the_counts_and_never_a_string():
    from admin import data_track

    row = _image("m1", CAPTION, CAPTION)
    probe, messages = _build([row], [row])
    sink, traces = _sink(probe)
    _request(sink, messages)
    good = _budgets(traces)[0]
    public = data_track._debug_event_public_json(good)["detail"]
    assert public["attachment_captions"] == good["detail"]["attachment_captions"]
    # The generic redaction is what keeps a future text field out of the admin view.
    leaked = {**good["detail"]["attachment_captions"], "text": CAPTION}
    tampered = {**good, "detail": {**good["detail"], "attachment_captions": leaked}}
    assert CAPTION not in repr(data_track._debug_event_public_json(tampered))


def test_detail_stays_within_the_trace_key_limit():
    from debug_trace import _DETAIL_MAX_KEYS

    row = _image("m1", CAPTION, CAPTION)
    probe, messages = _build([row], [row])
    sink, traces = _sink(probe)
    _request(sink, messages)
    detail = _budgets(traces)[0]["detail"]
    assert len(detail) <= _DETAIL_MAX_KEYS
    assert len(detail["attachment_captions"]) <= _DETAIL_MAX_KEYS
