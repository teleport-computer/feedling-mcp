"""Explicit refusal policy is lane-scoped and independent of telemetry."""
import asyncio
import inspect
import json

import httpx
import pytest

import provider_client as pc
import provider_attempt_metadata
import provider_refusal
from admin import data_track
from memory import extraction_trace
from model_api_runtime.v2 import extraction


PRIVATE = "PRIVATE_PROMPT_OR_PROVIDER_EXPLANATION"
DEFAULT_ATTEMPTS = inspect.signature(pc.reliable_chat_completion_async).parameters[
    "max_attempts"
].default


def body(category="reasoning_extraction", *, stop="refusal", text=""):
    return {
        "stop_reason": stop,
        "stop_details": {"category": category, "explanation": PRIVATE},
        "content": [{"type": "text", "text": text}] if text else [],
        "usage": {"input_tokens": 5, "output_tokens": 0},
    }


@pytest.mark.parametrize("category", sorted(provider_refusal.CATEGORIES))
@pytest.mark.parametrize("require_reply", [False, True])
def test_anthropic_refusal_survives_real_parser_without_content(category, require_reply):
    raw = body(category)
    if require_reply:
        with pytest.raises(pc.ProviderError) as e:
            pc._parse_anthropic_body(raw, model="test", require_reply=True)
        result = e.value.provider_refusal
    else:
        result = pc._parse_anthropic_body(raw, model="test", require_reply=False)["provider_refusal"]
    assert result == {"stop_reason": "refusal", "refusal_category": category}
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("category", [None, PRIVATE, {}, [], 3, True, " reasoning_extraction"])
def test_unknown_or_malformed_category_is_unclassified(category):
    assert provider_refusal.from_anthropic_body(body(category)) == {
        "stop_reason": "refusal", "refusal_category": "unclassified",
    }


@pytest.mark.parametrize("stop", [None, "end_turn", "content_filtered", "length", PRIVATE])
def test_text_and_category_never_trigger_refusal_without_exact_marker(stop):
    raw = body(stop=stop, text="refusal safeguards_refusal reasoning_extraction")
    assert provider_refusal.from_anthropic_body(raw) is None
    assert "provider_refusal" not in pc._parse_anthropic_body(raw, model="test", require_reply=False)


def test_missing_stop_details_is_unclassified_and_refusal_shape_not_other():
    assert provider_refusal.from_anthropic_body({"stop_reason": "refusal"}) == {
        "stop_reason": "refusal", "refusal_category": "unclassified",
    }
    assert extraction._response_shape("refusal", {}, 100)["stop_reason"] == "refusal"
    assert extraction._response_shape("end_turn", {}, 100)["stop_reason"] == "other"
    assert extraction._response_shape("max_tokens", {}, 100)["stop_reason"] == "length"


@pytest.mark.parametrize("recover", [False, True])
def test_reliable_observer_does_not_change_retry_count_or_result(monkeypatch, recover):
    async def run(callback):
        calls = []
        async def wire(*args, **kwargs):
            # Callback must be consumed by reliable, not forwarded to provider.
            assert "refusal_out" not in kwargs
            calls.append(1)
            raw = body()
            if recover and len(calls) == DEFAULT_ATTEMPTS:
                raw = body(stop="end_turn", text="ok")
            return pc._parse_anthropic_body(raw, model="test", require_reply=True)
        monkeypatch.setattr(pc, "chat_completion_async", wire)
        try:
            result = await pc.reliable_chat_completion_async(
                refusal_out=callback, base_delay_sec=0.0,
            )
            outcome = ("success", result)
        except pc.ProviderError as e:
            outcome = (type(e).__name__, str(e), e.feedling_error_class)
        return outcome, len(calls)
    seen = []
    async def observe(detail):
        seen.append(detail)
    async def broken(detail):
        raise RuntimeError(PRIVATE)
    baseline = asyncio.run(run(None))
    assert asyncio.run(run(observe)) == baseline
    assert asyncio.run(run(broken)) == baseline
    assert baseline[1] == DEFAULT_ATTEMPTS
    assert len(seen) == DEFAULT_ATTEMPTS - int(recover)
    assert [d["attempt"] for d in seen] == list(range(1, len(seen) + 1))
    assert all(d["refusal_category"] == "reasoning_extraction" for d in seen)


@pytest.mark.parametrize("lane", sorted(extraction_trace.LANES))
def test_real_parser_reliable_extraction_to_trace_and_admin(monkeypatch, lane):
    traces = []
    def emit(uid, event_type, **kwargs):
        traces.append({"type": event_type, **kwargs})
    observer = extraction_trace.refusal_observer(
        emit, "u", lane=lane, job_id="job", trace_id="trace",
    )
    # Partial refused text must never become memory content.
    async def wire(*args, **kwargs):
        return pc._parse_anthropic_body(body(text="partial"), model="test", require_reply=True)
    monkeypatch.setattr(pc, "chat_completion_async", wire)
    result = asyncio.run(extraction.extract(
        provider_config=object(), prompt=PRIVATE, parse=lambda reply: ([reply], None),
        refusal_out=observer,
    ))
    assert result == (None, "provider_call_failed:content_filtered")
    assert len(traces) == 1
    detail = traces[0]["detail"]
    assert detail == {
        "runtime": "hosted_v2", "lane": lane, "stop_reason": "refusal",
        "refusal_category": "reasoning_extraction", "attempt": 1,
    }
    assert extraction_trace.valid_refusal_detail(detail)
    projected = data_track._debug_event_public_json(traces[0])
    assert projected["detail"] == detail
    assert PRIVATE not in json.dumps(projected)
    assert "partial" not in json.dumps(projected)
    ordinary = data_track._debug_event_public_json({
        "type": "agent.model.call.error", "detail": {"error_class": "content_filtered"},
    })
    assert ordinary["type"] != projected["type"]
    assert "refusal_category" not in ordinary["detail"]
    assert "error_class" not in projected["detail"]


@pytest.mark.parametrize("field,value", [
    ("explanation", PRIVATE), ("refusal_category", PRIVATE), ("lane", []),
    ("attempt", True), ("stop_reason", "other"), ("runtime", "resident_v1"),
    ("outcome_class_provenance", PRIVATE), ("outcome_class_provenance", None),
])
def test_admin_refusal_projection_rejects_noncontract_detail(field, value):
    detail = extraction_trace.refusal_detail({
        "stop_reason": "refusal", "refusal_category": "reasoning_extraction", "attempt": 1,
    }, "dream")
    detail[field] = value
    assert data_track._debug_event_public_json({
        "type": extraction_trace.REFUSAL_TRACE_TYPE, "detail": detail,
    })["detail"] == {}


def test_trace_sink_failure_does_not_change_extraction_result(monkeypatch):
    async def wire(*args, **kwargs):
        return pc._parse_anthropic_body(body(text="ok"), model="test", require_reply=True)
    monkeypatch.setattr(pc, "chat_completion_async", wire)
    def broken(*args, **kwargs):
        raise RuntimeError(PRIVATE)
    kwargs = dict(provider_config=object(), prompt="p", parse=lambda reply: ([reply], None))
    baseline = asyncio.run(extraction.extract(**kwargs))
    assert baseline == (None, "provider_call_failed:content_filtered")
    assert asyncio.run(extraction.extract(**kwargs, refusal_out=extraction_trace.refusal_observer(
        broken, "u", lane="capture", job_id="job", trace_id="trace",
    ))) == baseline


@pytest.mark.parametrize("lane", sorted(extraction_trace.LANES))
def test_refusal_survives_production_trace_storage_and_admin(monkeypatch, lane):
    import conftest
    import debug_trace
    from types import SimpleNamespace
    from model_api_runtime.v2 import serve_worker

    uid = f"u_refusal_persist_{lane}"
    conftest.seed_user(uid)
    store = SimpleNamespace(user_id=uid)
    debug_trace.set_enabled(store, True)
    def emit(user_id, event_type, **kwargs):
        assert user_id == uid
        serve_worker._emit_v2_debug_trace(store, event_type, **kwargs)
    async def wire(*args, **kwargs):
        return pc._parse_anthropic_body(body(text="partial"), model="test", require_reply=True)
    monkeypatch.setattr(pc, "chat_completion_async", wire)
    assert asyncio.run(extraction.extract(
        provider_config=object(), prompt=PRIVATE, parse=lambda reply: ([reply], None),
        refusal_out=extraction_trace.refusal_observer(
            emit, uid, lane=lane, job_id="job", trace_id="trace",
        ),
    )) == (None, "provider_call_failed:content_filtered")
    rows = [r for r in debug_trace.read_trace(store) if r["type"] == extraction_trace.REFUSAL_TRACE_TYPE]
    assert len(rows) == 1
    assert rows[0]["detail"]["refusal_category"] == "reasoning_extraction"
    public = data_track._debug_event_public_json(rows[0])
    assert public["detail"]["refusal_category"] == "reasoning_extraction"
    assert public["detail"]["lane"] == lane
    assert PRIVATE not in json.dumps(public)


@pytest.mark.parametrize("value", [[], "private", 7, True])
def test_refusal_admin_rejects_nonobject_detail(value):
    assert data_track._debug_event_public_json({
        "type": extraction_trace.REFUSAL_TRACE_TYPE, "detail": value,
    })["detail"] == {}


@pytest.mark.parametrize("stop", ["refusal_pending", "no_refusal", "soft-refusal", "REFUSAL"])
@pytest.mark.parametrize("boundary", ["anthropic", "project"])
def test_refusal_stop_marker_requires_exact_case_sensitive_match(stop, boundary):
    # Provider protocol markers are exact and case-sensitive, not free text.
    if boundary == "anthropic":
        raw = body(stop=stop, text="normal reply")
        assert provider_refusal.from_anthropic_body(raw) is None
        assert "provider_refusal" not in pc._parse_anthropic_body(
            raw, model="test", require_reply=True,
        )
    else:
        assert provider_refusal.project({
            "stop_reason": stop, "refusal_category": "reasoning_extraction",
        }) is None


@pytest.mark.parametrize("text", ["", "partial"])
@pytest.mark.parametrize("observer", ["absent", "working", "broken"])
def test_opt_out_refusal_stops_one_real_wire_and_preserves_trace(monkeypatch, text, observer):
    calls, seen = [], []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=body(text=text))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(pc, "_shared_async_client", client)
    cfg = pc.ProviderConfig(
        provider="anthropic", model="test", api_key="private-key",
        base_url="https://relay.example/v1", capture_attempt_trace=True,
    )

    async def callback(detail):
        seen.append(detail)
        if observer == "broken":
            raise RuntimeError(PRIVATE)

    with pytest.raises(pc.ProviderError) as raised:
        asyncio.run(pc.reliable_chat_completion_async(
            cfg, [{"role": "user", "content": PRIVATE}], retry_refusal=False,
            refusal_out=None if observer == "absent" else callback, base_delay_sec=0,
        ))
    assert len(calls) == 1
    assert raised.value.feedling_error_class == "content_filtered"
    assert raised.value.provider_refusal == provider_refusal.from_anthropic_body(body())
    assert len(seen) == (0 if observer == "absent" else 1)
    attempts = pc.runtime_provider_attempt_trace(raised.value)["attempts"]
    assert [a["kind"] for a in attempts] == ["http_attempt", "outer_attempt"]
    assert [a["status"] for a in attempts] == [200, 200]
    assert attempts[-1]["outcome"] == "terminal_error"
    assert attempts[-1]["wire"]["ordinals"] == [1]
    # This private runtime envelope deliberately retains the request; public
    # refusal detail is independently content-free.
    assert PRIVATE not in json.dumps(seen)
    public = provider_attempt_metadata.project(pc.runtime_provider_attempt_trace(raised.value))
    assert public["wire_attempt_count"] == public["outer_attempt_count"] == 1
    assert PRIVATE not in json.dumps(public)


@pytest.mark.parametrize("stop", ["end_turn", "refusal_pending", "no_refusal", "soft-refusal", "REFUSAL"])
def test_opt_out_still_retries_nonrefusal_empty_replies(monkeypatch, stop):
    calls = []

    async def wire(*args, **kwargs):
        assert "retry_refusal" not in kwargs
        calls.append(1)
        return pc._parse_anthropic_body(body(stop=stop), model="test", require_reply=True)

    monkeypatch.setattr(pc, "chat_completion_async", wire)
    monkeypatch.setattr(pc, "_reliable_retry_delay_sec", lambda *a, **k: 0)
    assert asyncio.run(extraction.extract(
        provider_config=object(), prompt="p", parse=lambda r: pytest.fail("empty reply parsed"),
    )) == (None, "provider_call_failed:upstream_unavailable")
    assert len(calls) == DEFAULT_ATTEMPTS


@pytest.mark.parametrize("session_mode", [False, True])
@pytest.mark.parametrize("text", ["", "partial"])
def test_refusal_never_reaches_parser_or_component_session(monkeypatch, session_mode, text):
    calls = []

    async def wire(*args, **kwargs):
        calls.append(1)
        return pc._parse_anthropic_body(body(text=text), model="test", require_reply=True)

    class Session:
        def next_prompt(self):
            return "p"

        def feed(self, *args, **kwargs):
            pytest.fail("refused text reached component parser")

        def result(self):
            pytest.fail("refusal must return before component result")

    monkeypatch.setattr(pc, "chat_completion_async", wire)
    monkeypatch.setattr(pc, "_reliable_retry_delay_sec", lambda *a, **k: 0)
    assert asyncio.run(extraction.extract(
        provider_config=object(), prompt="p", parse=lambda r: pytest.fail("refused text parsed"),
        session=Session() if session_mode else None,
    )) == (None, "provider_call_failed:content_filtered")
    assert len(calls) == 1


def test_default_async_partial_refusal_still_returns_reply(monkeypatch):
    async def wire(*args, **kwargs):
        return pc._parse_anthropic_body(body(text="partial"), model="test", require_reply=True)
    monkeypatch.setattr(pc, "chat_completion_async", wire)
    result = asyncio.run(pc.reliable_chat_completion_async())
    assert result["reply"] == "partial"
    assert result["provider_refusal"]["stop_reason"] == "refusal"


def test_sync_refusal_keeps_default_retry_policy(monkeypatch):
    calls = []
    def wire(*args, **kwargs):
        calls.append(1)
        return pc._parse_anthropic_body(body(), model="test", require_reply=True)
    monkeypatch.setattr(pc, "chat_completion", wire)
    with pytest.raises(pc.ProviderError) as raised:
        pc.reliable_chat_completion(base_delay_sec=0)
    assert len(calls) == inspect.signature(pc.reliable_chat_completion).parameters["max_attempts"].default
    assert raised.value.feedling_error_class == "transient_exhausted"
