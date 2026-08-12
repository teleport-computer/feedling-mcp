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


def test_compact_reports_provider_usage():
    seen = []
    llm = _fake_llm_result({
        "reply": "- cached fact",
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 3,
            "cache_read_tokens": 15,
            "cache_write_tokens": None,
            "cache_miss_tokens": 5,
        },
    })
    out = asyncio.run(compaction.compact(
        provider_config=object(),
        current_summary="",
        old_messages=[{"role": "user", "content": "remember this"}],
        llm=llm,
        usage_out=seen.append,
    ))
    assert out == "- cached fact"
    assert seen == [{
        "prompt_tokens": 20,
        "completion_tokens": 3,
        "cache_read_tokens": 15,
        "cache_write_tokens": None,
        "cache_miss_tokens": 5,
    }]


def test_compact_reports_unknown_usage_when_provider_raises():
    seen = []

    async def _boom(*args, **kwargs):
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError, match="provider down"):
        asyncio.run(compaction.compact(
            provider_config=object(), current_summary="",
            old_messages=[{"role": "user", "content": "x"}], llm=_boom,
            usage_out=seen.append,
        ))
    assert seen == [None]


def test_deterministic_fold_renders_exact_count_without_source_text():
    assert compaction.deterministic_fold(source_message_count=7) == (
        "- [7 条更早的消息已由长期记忆覆盖]"
    )


def test_deterministic_fold_renders_zero_count_legacy_opaque_coverage():
    assert compaction.deterministic_fold(
        source_message_count=0,
        includes_legacy_opaque=True,
    ) == "- [更早的历史摘要已由长期记忆覆盖]"


def test_deterministic_fold_renders_mixed_legacy_and_exact_coverage():
    assert compaction.deterministic_fold(
        source_message_count=9,
        includes_legacy_opaque=True,
    ) == "- [更早的历史摘要及 9 条消息已由长期记忆覆盖]"


@pytest.mark.parametrize("count", [0, -1])
def test_deterministic_fold_requires_positive_source_count(count):
    with pytest.raises(ValueError, match="source_message_count"):
        compaction.deterministic_fold(source_message_count=count)


def test_deterministic_checkpoint_sums_nested_sentinels():
    leaf_a = compaction.deterministic_fold(source_message_count=3)
    leaf_b = compaction.deterministic_fold(source_message_count=11)

    parent = compaction.deterministic_checkpoint([leaf_a, leaf_b])

    assert parent == "- [14 条更早的消息已由长期记忆覆盖]"


@pytest.mark.parametrize(
    "children",
    [
        [],
        ["- model-authored fact"],
        [
            compaction.deterministic_fold(source_message_count=2),
            "- model-authored fact",
        ],
        ["- [0 条更早的消息已由长期记忆覆盖]"],
    ],
)
def test_deterministic_checkpoint_refuses_non_sentinel_children(children):
    assert compaction.deterministic_checkpoint(children) is None


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


# --- Reject codes (2026-07-27) -------------------------------------------
# A rejected fold stalls the summary watermark silently and the caller re-reads
# the identical batch next turn, so the loop repeats until something changes.
# usr_7f30… sat in exactly that loop for three days with `responder_error` as
# the only externally visible symptom. These lock in that every refusal names
# the rule that fired, and that the code stays content-free.

@pytest.mark.parametrize(
    "reply,current_summary,expected_prefix",
    [
        ("好的，以下是摘要：\n- a", "", "line_not_bullet:"),
        ("- a\n- a", "", "duplicate_within_batch:"),
        ("- x", "- x", "duplicate_of_existing_summary:"),
        ("- a\n\n- b", "", "line_not_bullet:"),
        ("\n".join(f"- l{i}" for i in range(40)), "", "line_count_over_budget:"),
        ("- " + "x" * 1_200, "", "bullet_chars_over_budget:"),
        ("", "", "reply_empty"),
        ("   ", "", "reply_empty"),
        # A bullet marker with no body — e.g. output cut off by max_tokens
        # right after the marker. Kept mid-reply because a trailing "- " alone
        # is stripped to "-" and reads as a non-bullet line instead.
        ("- \n- a", "", "bullet_body_empty:0"),
    ],
)
def test_validate_new_bullets_names_the_rule_that_rejected(
    reply, current_summary, expected_prefix
):
    rendered, reject = compaction._validate_new_bullets(
        reply, current_summary=current_summary
    )
    assert rendered is None
    assert reject.startswith(expected_prefix), reject


def test_validate_new_bullets_reject_code_carries_no_content():
    secret = "私密内容不该出现在错误码里"
    _rendered, reject = compaction._validate_new_bullets(
        f"{secret}\n- a", current_summary=""
    )
    assert secret not in reject
    assert reject == "line_not_bullet:0"


def test_validate_new_bullets_accepted_reports_no_reject():
    rendered, reject = compaction._validate_new_bullets(
        "- a\n- b", current_summary=""
    )
    assert rendered == "- a\n- b"
    assert reject == ""


def test_compact_reports_reject_code_to_caller():
    seen = []
    out = asyncio.run(compaction.compact(
        provider_config=object(), current_summary="- old",
        old_messages=[{"role": "user", "content": "x"}],
        llm=_fake_llm_returning("- old"),
        reject_out=seen.append,
    ))
    assert out == "- old"  # unchanged: still a no-op fold
    assert seen == ["duplicate_of_existing_summary:0"]


def _summarizable_batch():
    """A batch above the verbatim threshold, so the provider path is exercised."""
    return [
        {"role": "user", "content": "y" * 500}
        for _ in range(compaction._VERBATIM_FOLD_MAX_CHARS // 500 + 2)
    ]


def test_compact_segment_reports_reject_code_to_caller():
    seen = []
    out = asyncio.run(compaction.compact_segment(
        provider_config=object(),
        old_messages=_summarizable_batch(),
        llm=_fake_llm_returning("抱歉，这些消息没有实质内容。"),
        reject_out=seen.append,
    ))
    assert out is None
    assert seen == ["line_not_bullet:0"]


def test_accepted_fold_reports_no_reject_code():
    seen = []
    out = asyncio.run(compaction.compact_segment(
        provider_config=object(),
        old_messages=_summarizable_batch(),
        llm=_fake_llm_returning("- summarized"),
        reject_out=seen.append,
    ))
    assert out == "- summarized"
    assert seen == []


def test_reject_out_failure_cannot_break_a_fold():
    """Diagnostics must never change the outcome of the fold itself."""
    def _explode(_code):
        raise RuntimeError("reporting is broken")

    out = asyncio.run(compaction.compact(
        provider_config=object(), current_summary="- old",
        old_messages=[{"role": "user", "content": "x"}],
        llm=_fake_llm_returning("- old"),
        reject_out=_explode,
    ))
    assert out == "- old"


# --- V2 provider ledger (2026-07-27) --------------------------------------
# V1's resident consumer has always written a PLAINTEXT provider ledger, so
# "does this user's relay work at all" was one query. V2 recorded the same
# facts only inside the sealed trajectory, so answering it for a V2 user
# needed a break-glass decrypt (usr_90184…). These pin the mirror.

def test_provider_response_is_mirrored_to_the_plaintext_ledger(monkeypatch):
    import sys, pathlib as _p
    sys.path.insert(0, str(_p.Path(__file__).parent.parent / "backend"))
    from model_api_runtime.v2 import worker
    import provider_attempt_ledger

    seen = []
    monkeypatch.setattr(
        provider_attempt_ledger, "record_runtime_attempt",
        lambda uid, **kw: seen.append((uid, kw)) or True,
    )
    worker._note_provider_attempt(
        "usr_test",
        "provider_response",
        {
            "lane": "prompt_catchup",
            "response": {"usage": {"prompt_tokens": 120, "completion_tokens": 34}},
        },
        job_id=77,
    )
    assert len(seen) == 1
    uid, kw = seen[0]
    assert uid == "usr_test"
    assert kw["outcome"] == "ok"
    assert kw["trigger"] == "v2_catchup"       # lane is preserved as a trigger
    assert kw["parent_key"] == "v2job:77"
    assert kw["input_tokens"] == 120 and kw["output_tokens"] == 34


def test_provider_error_is_mirrored_with_class_only(monkeypatch):
    import sys, pathlib as _p
    sys.path.insert(0, str(_p.Path(__file__).parent.parent / "backend"))
    from model_api_runtime.v2 import worker
    import provider_attempt_ledger

    seen = []
    monkeypatch.setattr(
        provider_attempt_ledger, "record_runtime_attempt",
        lambda uid, **kw: seen.append(kw) or True,
    )
    worker._note_provider_attempt(
        "usr_test",
        "provider_error",
        {
            "error_class": "ProviderError",
            "provider_attempt_trace": {"body": "SECRET relay error body"},
        },
        job_id=68,
    )
    assert len(seen) == 1
    kw = seen[0]
    assert kw["outcome"] == "provider_error"
    assert kw["error_class"] == "ProviderError"
    assert kw["trigger"] == "v2_turn"          # no lane → foreground turn
    # The raw upstream body stays in the sealed trajectory, never the ledger.
    assert "SECRET" not in repr(kw)


def test_non_provider_events_are_not_mirrored(monkeypatch):
    import sys, pathlib as _p
    sys.path.insert(0, str(_p.Path(__file__).parent.parent / "backend"))
    from model_api_runtime.v2 import worker
    import provider_attempt_ledger

    seen = []
    monkeypatch.setattr(
        provider_attempt_ledger, "record_runtime_attempt",
        lambda uid, **kw: seen.append(kw) or True,
    )
    for kind in ("turn_started", "provider_request", "tool_call_started"):
        worker._note_provider_attempt("usr_test", kind, {}, job_id=1)
    assert seen == []


def test_ledger_failure_cannot_break_a_turn(monkeypatch):
    """Telemetry must never be able to fail a turn that would succeed."""
    import sys, pathlib as _p
    sys.path.insert(0, str(_p.Path(__file__).parent.parent / "backend"))
    from model_api_runtime.v2 import worker
    import provider_attempt_ledger

    def _explode(*_a, **_kw):
        raise RuntimeError("ledger is down")

    monkeypatch.setattr(
        provider_attempt_ledger, "record_runtime_attempt", _explode
    )
    worker._note_provider_attempt(
        "usr_test", "provider_response", {"response": {}}, job_id=1
    )  # must not raise


def test_ledger_mirror_tolerates_a_recorder_without_user_id():
    """A narrower trajectory sink must degrade to "no ledger row", not raise.

    The mirror wraps the foreground turn's provider calls, so an
    AttributeError here would fail exactly the turns it exists to explain —
    which is what it did to the wake lane's test double before this guard.
    """
    import sys, pathlib as _p
    sys.path.insert(0, str(_p.Path(__file__).parent.parent / "backend"))
    from model_api_runtime.v2 import worker

    class _NarrowSink:
        async def record(self, kind, payload):
            return None

    asyncio.run(worker._mirror_provider_attempt(
        _NarrowSink(), "provider_response", {"response": {}}
    ))  # must not raise


def test_ledger_carries_the_route_announced_at_turn_start():
    """provider/model come from `provider_config_resolved`, not the call event.

    The provider_* events carry no route identity, so a ledger row built only
    from their payload has empty provider/model — which is most of what makes
    the V1-vs-V2 comparison readable. Post-deploy regression on test caught
    exactly that; this pins the fix.
    """
    import sys, pathlib as _p
    sys.path.insert(0, str(_p.Path(__file__).parent.parent / "backend"))
    from model_api_runtime.v2 import worker
    import provider_attempt_ledger

    class _Sink:
        user_id = "usr_route"
        job_id = 99

        async def record(self, kind, payload):
            return None

    sink = _Sink()
    seen = []
    import unittest.mock as _mock
    with _mock.patch.object(
        provider_attempt_ledger, "record_runtime_attempt",
        lambda uid, **kw: seen.append(kw) or True,
    ):
        # Turn start announces the route…
        asyncio.run(worker._mirror_provider_attempt(
            sink, "provider_config_resolved",
            {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        ))
        assert seen == []  # the announcement itself is not a ledger row
        # …and every later provider outcome inherits it.
        asyncio.run(worker._mirror_provider_attempt(
            sink, "provider_response",
            {"response": {"usage": {"prompt_tokens": 5059, "completion_tokens": 15}}},
        ))

    assert len(seen) == 1
    assert seen[0]["provider"] == "anthropic"
    assert seen[0]["model"] == "claude-sonnet-4-6"
    assert seen[0]["input_tokens"] == 5059


# --- thin batches: fold without a provider call -----------------------------
#
# usr_7f30 on prod sat at a frozen watermark 2026-07-24 → 07-27 because its
# oldest pending batch was 60 rows of near-contentless chat (27 user rows of
# 3-24 chars, 30 assistant rows of 4-71 chars, ~1.3 KB total). Any honest
# summary of that is a handful of near-identical lines, so the all-or-nothing
# bullet validator rejected the fold; the retry re-read the identical batch and
# rejected it again, and shrinking the batch only made it MORE homogeneous.
# A batch this small does not need to be summarized at all — keeping it verbatim
# costs less than the summary would and cannot be refused.


def _thin_prod_shaped_batch():
    """usr_7f30's actual wedge shape: many rows, almost no content."""
    rows = []
    for i in range(27):
        rows.append({"role": "user", "content": ["嗯", "好", "哈哈", "在的"][i % 4]})
        rows.append({"role": "assistant", "content": ["好的～", "收到", "嗯嗯"][i % 3]})
    return rows


def test_thin_batch_folds_without_calling_the_provider():
    calls = []

    async def _llm(cfg, messages, **kw):
        calls.append(messages)
        return {"reply": "- unreachable"}

    out = asyncio.run(compaction.compact_segment(
        provider_config=object(),
        old_messages=_thin_prod_shaped_batch(),
        llm=_llm,
    ))
    assert calls == [], "a thin batch must never reach the provider"
    assert out, "a thin batch must still produce a leaf so coverage advances"


def test_thin_batch_leaf_passes_the_same_validator_as_a_model_reply():
    # The verbatim leaf is stored exactly where a validated model reply would
    # be, so it has to satisfy every rule that gates a model reply — otherwise
    # we have merely moved the rejection downstream.
    out = asyncio.run(compaction.compact_segment(
        provider_config=object(),
        old_messages=_thin_prod_shaped_batch(),
        llm=_fake_llm_returning("- unreachable"),
    ))
    rendered, reject = compaction._validate_new_bullets(out, current_summary="")
    assert reject == "", f"verbatim leaf rejected by its own validator: {reject}"
    assert rendered == out


def test_thin_batch_keeps_every_message_verbatim():
    rows = [
        {"role": "user", "content": "周五那家店"},
        {"role": "assistant", "content": "订好了"},
        {"role": "user", "content": "几点"},
    ]
    out = asyncio.run(compaction.compact_segment(
        provider_config=object(),
        old_messages=rows,
        llm=_fake_llm_returning("- unreachable"),
    ))
    for row in rows:
        assert row["content"] in out, f"verbatim fold dropped {row['content']!r}"


def test_homogeneous_thin_batch_does_not_collide_with_itself():
    # 40 byte-identical rows: the failure mode that makes a MODEL summary trip
    # duplicate_within_batch. The verbatim leaf must not reproduce it.
    rows = [{"role": "user", "content": "嗯"} for _ in range(40)]
    out = asyncio.run(compaction.compact_segment(
        provider_config=object(),
        old_messages=rows,
        llm=_fake_llm_returning("- unreachable"),
    ))
    _, reject = compaction._validate_new_bullets(out, current_summary="")
    assert reject == "", f"verbatim leaf self-collided: {reject}"


def test_batch_above_the_threshold_still_goes_to_the_provider():
    calls = []

    async def _llm(cfg, messages, **kw):
        calls.append(messages)
        return {"reply": "- summarized"}

    fat = [
        {"role": "user", "content": "x" * 400}
        for _ in range(compaction._VERBATIM_FOLD_MAX_CHARS // 400 + 2)
    ]
    out = asyncio.run(compaction.compact_segment(
        provider_config=object(), old_messages=fat, llm=_llm,
    ))
    assert len(calls) == 1, "a normal batch must still be summarized by the model"
    assert out == "- summarized"


def test_attachment_only_batch_folds_verbatim():
    """Attachment rows reach compaction as short placeholders, never as bytes.

    ``serve_worker._image_row`` / ``_file_row`` replace a multi-hundred-KB
    payload with the caption or ``[image]``/``[file: name]``, so a batch of
    attachments is exactly the thin, homogeneous shape a model summary cannot
    express without repeating itself. Preserving it costs a few hundred chars
    and no provider call at all.
    """
    calls = []

    async def _llm(cfg, messages, **kw):
        calls.append(messages)
        return {"reply": "- unreachable"}

    rows = [{"role": "user", "content": "[image]"} for _ in range(50)]
    out = asyncio.run(compaction.compact_segment(
        provider_config=object(), old_messages=rows, llm=_llm,
    ))
    assert calls == []
    _, reject = compaction._validate_new_bullets(out, current_summary="")
    assert reject == ""
    assert "[image]" in out


def test_empty_batch_is_not_rescued_by_the_verbatim_path():
    # Contentless rows must keep their existing outcome (no leaf), so the
    # caller's quarantine path still owns them.
    out = asyncio.run(compaction.compact_segment(
        provider_config=object(),
        old_messages=[{"role": "user", "content": "  "}],
        llm=_fake_llm_returning(""),
    ))
    assert out is None
