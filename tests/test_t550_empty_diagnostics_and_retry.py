"""T550 — content-free empty-response root-cause diagnostics (A①) + per-call
transport retry count (A②), verified across the FULL handoff chain.

codex2's audit note: helper-only tests miss a severed handoff, so A① runs the
real chain raw Gemini body -> ``_parse_gemini_body`` -> ``_empty_response_shape``
-> ``_empty_response_trace_detail`` -> public ``_safe_detail``, and A② runs the
real ``_ProviderRoundtripTrace._safe_model_call_detail`` whitelist. Every value
asserted is an enum/count/bool (no content). Removing any projected field turns
the matching assertion red — these pins ARE the mutation guard.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import provider_client as pc  # noqa: E402
from model_api_runtime.v2 import tool_loop as tl  # noqa: E402
from model_api_runtime.v2 import worker  # noqa: E402
from debug_trace import _safe_detail  # noqa: E402


class _FakeUsage:
    def __init__(self, completion_tokens: int = 0) -> None:
        self.completion_tokens = completion_tokens


class _FakePR:
    """Thin ProviderResponse double: only .raw/.text/.tool_calls/.usage matter,
    and .raw is the REAL _parse_gemini_body output (the actual handoff under test)."""

    def __init__(self, raw: dict, text: str = "", tool_calls=None, ct: int = 0) -> None:
        self.raw = raw
        self.text = text
        self.tool_calls = tool_calls or []
        self.usage = _FakeUsage(ct)


def _full_chain(body: dict) -> dict:
    normalized = pc._parse_gemini_body(
        body, model="gemini-3.6-flash", require_reply=False
    )
    shape = tl._empty_response_shape(_FakePR(raw=normalized))
    detail = worker._empty_response_trace_detail(shape, "chat")
    return _safe_detail(detail)


# ---------------------------------------------------------------- A① four classes
def test_a1_safety_block_class():
    body = {
        "candidates": [{
            "finishReason": "SAFETY",
            "content": {"parts": []},
            "safetyRatings": [
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                 "probability": "HIGH", "blocked": True}
            ],
        }],
        "usageMetadata": {"promptTokenCount": 50},
    }
    d = _full_chain(body)
    assert d["provider_finish_reason"] == "SAFETY"
    assert d["provider_safety_blocked"] is True
    assert d["provider_safety_blocked_categories"] == ["HARM_CATEGORY_DANGEROUS_CONTENT"]
    assert d["provider_safety_max_probability"] == "HIGH"


def test_a1_budget_eaten_by_thinking_class():
    body = {
        "candidates": [{
            "finishReason": "MAX_TOKENS",
            "content": {"parts": [{"thought": True, "text": "long reasoning"}]},
        }],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 0,
                          "thoughtsTokenCount": 900},
    }
    d = _full_chain(body)
    assert d["provider_finish_reason"] == "MAX_TOKENS"
    assert d["provider_thoughts_token_count"] == 900
    assert d["provider_candidates_token_count"] == 0


def test_a1_only_thought_parts_class():
    body = {
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"thought": True, "text": "reasoning only"}]},
        }],
        "usageMetadata": {},
    }
    d = _full_chain(body)
    assert d["provider_only_thought_parts"] is True
    assert d["provider_visible_text_part_count"] == 0
    assert d["provider_thought_part_count"] == 1


def test_a1_other_class():
    body = {"candidates": [{"finishReason": "OTHER", "content": {"parts": []}}],
            "usageMetadata": {}}
    d = _full_chain(body)
    assert d["provider_finish_reason"] == "OTHER"
    assert d["provider_safety_blocked"] is False
    assert d["provider_only_thought_parts"] is False


def test_a1_scalars_survive_safe_detail_not_stringified():
    body = {
        "candidates": [{
            "finishReason": "SAFETY",
            "content": {"parts": [{"thought": True, "text": "x"}]},
            "safetyRatings": [
                {"category": "HARM_CATEGORY_HATE_SPEECH",
                 "probability": "MEDIUM", "blocked": True}
            ],
        }],
        "usageMetadata": {"promptTokenCount": 10, "thoughtsTokenCount": 5},
    }
    d = _full_chain(body)
    assert d["provider_safety_blocked"] is True          # bool, not "True"
    assert isinstance(d["provider_thoughts_token_count"], int)
    assert isinstance(d["provider_candidates_count"], int)
    assert isinstance(d["provider_safety_blocked_categories"], list)


def test_a1_non_gemini_adds_no_provider_keys():
    shape = {"stop_reason": "other", "has_visible_text": False,
             "reasoning_present": False, "tool_call_count": 0, "completion_tokens": 0}
    d = _safe_detail(worker._empty_response_trace_detail(shape, "chat"))
    assert not any(k.startswith("provider_") for k in d)


def test_a1_unknown_safety_category_counted_not_named():
    body = {
        "candidates": [{
            "finishReason": "SAFETY", "content": {"parts": []},
            "safetyRatings": [
                {"category": "HARM_CATEGORY_FUTURE_UNKNOWN",
                 "probability": "HIGH", "blocked": True}
            ],
        }],
        "usageMetadata": {},
    }
    d = _full_chain(body)
    assert d["provider_safety_blocked"] is True
    assert d["provider_safety_blocked_categories"] == []       # unknown NOT named
    assert d["provider_safety_blocked_unknown_count"] == 1     # but counted


# ---------------------------------------------------------------- A② retry count
def _safe_call_detail(detail: dict) -> dict:
    trace = worker._ProviderRoundtripTrace(deps=None, user_id="u", lane="chat")
    return trace._safe_model_call_detail(detail)


def test_a2_usage_helper_oneshot_zero_and_retried():
    assert tl._transport_retry_count_from_usage({"provider_retry_count": 0}) == 0
    assert tl._transport_retry_count_from_usage({"provider_retry_count": 3}) == 3
    assert tl._transport_retry_count_from_usage({}) is None          # unknown, never 0
    assert tl._transport_retry_count_from_usage(None) is None


def test_a2_error_helper_from_exception_envelope():
    class _E(Exception):
        pass
    exc = _E()
    # Real envelope shape interleaves http_attempt / outer_attempt records; only
    # http attempts are real transport requests.
    exc.feedling_provider_attempt_trace = {"attempts": [
        {"kind": "http_attempt"}, {"kind": "outer_attempt"},
        {"kind": "http_attempt"}, {"kind": "outer_attempt"},
    ]}
    assert tl._transport_retry_count_from_error(exc) == 1           # 2 http -> 1 retry
    assert tl._transport_retry_count_from_error(_E()) is None       # no envelope, never 0
    # a bare non-kind list (no http_attempt markers) is not a real envelope -> None
    bad = _E(); bad.feedling_provider_attempt_trace = {"attempts": [1, 2, 3]}
    assert tl._transport_retry_count_from_error(bad) is None


def test_a2_safe_detail_forwards_zero_on_oneshot():
    d = _safe_call_detail({"round": 1, "provider": "gemini", "model": "m",
                           "transport_retry_count": 0, "dur_ms": 5})
    assert d["transport_retry_count"] == 0


def test_a2_safe_detail_forwards_retried_count():
    d = _safe_call_detail({"round": 1, "provider": "gemini", "model": "m",
                           "transport_retry_count": 2, "dur_ms": 5})
    assert d["transport_retry_count"] == 2


def test_a2_safe_detail_omits_unknown_never_coerces_zero():
    d = _safe_call_detail({"round": 1, "provider": "gemini", "model": "m",
                           "transport_retry_count": None, "dur_ms": 5})
    assert "transport_retry_count" not in d


def test_a2_no_retry_mirror_holds_for_all_providers():
    for provider in ("openai", "anthropic", "deepseek", "openrouter", "openai_compatible"):
        d = _safe_call_detail({"round": 1, "provider": provider, "model": "m",
                               "transport_retry_count": 0, "dur_ms": 1})
        assert d["transport_retry_count"] == 0, provider


# ---------------------------------------------------------------- real emission
# Drive the ACTUAL run_tool_loop through the REAL Gemini async wire (MockTransport)
# and the real reliable wrapper, so: one-shot transport_retry_count=0 is produced
# by the real path (not a test-injected provider_retry_count that masks the gap),
# and removing the tool_loop done/error event injection turns these red.
import asyncio  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
from model_api_runtime.v2 import tool_loop as _tl  # noqa: E402

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


def _run_loop_via_transport(monkeypatch, handler, *, require_reply=False, max_calls=1):
    # Sandbox DNS is fake-ip for the real Gemini host; egress validation is
    # exercised by its own tests, so neutralize it here to reach MockTransport.
    monkeypatch.setattr(pc, "_validate_egress_url", lambda *_a, **_k: None)
    monkeypatch.setattr(
        pc, "_shared_async_client",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    events, empties = [], []

    async def on_pce(kind, detail):
        events.append((kind, dict(detail)))

    async def on_empty(shape):
        empties.append(dict(shape))

    async def on_reply(_text, *, final, reasoning=""):
        return None

    async def dispatch(_calls):
        return []

    async def fold():
        return []

    try:
        asyncio.run(
            _tl.run_tool_loop(
                provider_config=pc.ProviderConfig(
                    provider="gemini", model="gemini-3.6-flash",
                    api_key="secret-key", base_url=_GEMINI_BASE,
                    # V2 production (serve_worker.py) captures the attempt trace;
                    # the error event's transport_retry_count derives from it.
                    capture_attempt_trace=True,
                ),
                build_messages=lambda t: [{"role": "user", "content": "hi"}, *t],
                dispatch_tools=dispatch,
                on_reply=on_reply,
                fold_new_messages=fold,
                add_usage=lambda _u: None,
                max_calls=max_calls,
                on_provider_call_event=on_pce,
                on_empty_provider_response=on_empty,
                require_reply=require_reply,
            )
        )
    except Exception:
        pass
    assert all("secret-key" not in str(p) for _k, p in events)
    return events, empties


def test_empty_gemini_done_event_real_oneshot_via_run_tool_loop(monkeypatch):
    """Real Gemini one-shot empty: done event carries empty=True AND a real
    transport_retry_count=0 (produced by the wire, not test-injected); full
    diagnostics ride the empty callback (provider.empty_response)."""
    def handler(_request):
        return httpx.Response(200, json={
            "candidates": [{
                "finishReason": "SAFETY", "content": {"parts": []},
                "safetyRatings": [{"category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                                   "probability": "HIGH", "blocked": True}],
            }],
            "usageMetadata": {"promptTokenCount": 50, "thoughtsTokenCount": 40},
        })

    events, empties = _run_loop_via_transport(monkeypatch, handler)
    done = next(d for k, d in events if k == "done")
    assert done["empty"] is True
    assert done["transport_retry_count"] == 0      # real wire, not injected
    assert "provider_diagnostics" not in done
    assert empties, "on_empty_provider_response never fired"
    diag = empties[-1]["provider_diagnostics"]
    assert diag["finish_reason"] == "SAFETY"
    assert diag["safety_blocked"] is True
    assert diag["thoughts_token_count"] == 40


def test_nonempty_gemini_done_event_real_oneshot(monkeypatch):
    def handler(_request):
        return httpx.Response(200, json={
            "candidates": [{"finishReason": "STOP",
                            "content": {"parts": [{"text": "hello"}]}}],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2},
        })

    events, empties = _run_loop_via_transport(monkeypatch, handler, require_reply=True)
    done = next(d for k, d in events if k == "done")
    assert done["empty"] is False
    assert done["transport_retry_count"] == 0
    assert not empties


def test_error_event_real_transport_failure_via_run_tool_loop(monkeypatch):
    """Real transport failure through the reliable wrapper: the ERROR event must
    carry transport_retry_count from the genuine attempt envelope. Deleting the
    tool_loop error-event injection turns this red (a helper-only test would not)."""
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    events, _ = _run_loop_via_transport(monkeypatch, handler, require_reply=True)
    err = next((d for k, d in events if k == "error"), None)
    assert err is not None, "no error event emitted"
    # Foreground provider call allows 2 attempts -> 2 http_attempts -> 1 retry.
    assert err["transport_retry_count"] == 1


def test_transport_retry_count_helper_on_real_reliable_envelope(monkeypatch):
    """codex2's 'real envelope' pin for the helper itself."""
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    monkeypatch.setattr(pc, "_validate_egress_url", lambda *_a, **_k: None)
    monkeypatch.setattr(
        pc, "_shared_async_client",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    cfg = pc.ProviderConfig(provider="gemini", model="gemini-3.6-flash",
                            api_key="k", base_url=_GEMINI_BASE,
                            capture_attempt_trace=True)
    with pytest.raises(pc.ProviderError) as raised:
        asyncio.run(pc.reliable_chat_completion_async(
            cfg, [{"role": "user", "content": "hi"}],
            max_attempts=2, base_delay_sec=0.0, max_delay_sec=0.0))
    exc = raised.value
    http_attempts = sum(1 for a in exc.feedling_provider_attempt_trace["attempts"]
                        if a.get("kind") == "http_attempt")
    assert http_attempts == 2
    assert _tl._transport_retry_count_from_error(exc) == 1


def test_worker_done_event_reaches_real_trace_with_empty_and_retry(monkeypatch):
    emitted = []

    class _Deps:
        def emit_debug_trace(self, user_id, event_type, **kw):
            emitted.append((event_type, kw.get("detail")))

    trace = worker._ProviderRoundtripTrace(deps=_Deps(), user_id="u", lane="chat")
    detail = {"round": 1, "provider": "gemini", "model": "m", "finish_reason": "other",
              "transport_retry_count": 0, "empty": True, "dur_ms": 5}
    asyncio.run(trace.record_model_call("done", detail))
    from debug_trace import _safe_detail
    kind, out = emitted[-1]
    out = _safe_detail(out)
    assert kind == "agent.model.call.done"
    assert out["empty"] is True
    assert out["transport_retry_count"] == 0


# --- round3 fix #3: malformed provider token counts must not fake or raise ---
def test_malformed_gemini_token_counts_become_none_never_faked_or_raised():
    for bad in (1.5, -1, float("nan"), float("inf"), "x", None, True):
        body = {"candidates": [{"finishReason": "STOP", "content": {"parts": []}}],
                "usageMetadata": {"thoughtsTokenCount": bad,
                                  "promptTokenCount": bad,
                                  "candidatesTokenCount": bad}}
        d = pc._gemini_empty_diagnostics(body)  # must not raise
        assert d["thoughts_token_count"] is None, bad
        assert d["prompt_token_count"] is None, bad
        assert d["candidates_token_count"] is None, bad


def test_valid_float_gemini_token_count_is_kept_as_int():
    body = {"candidates": [{"finishReason": "STOP", "content": {"parts": []}}],
            "usageMetadata": {"thoughtsTokenCount": 40.0, "promptTokenCount": 12}}
    d = pc._gemini_empty_diagnostics(body)
    assert d["thoughts_token_count"] == 40 and isinstance(d["thoughts_token_count"], int)
    assert d["prompt_token_count"] == 12


# --- round6 fix: reliable-exit guarantees provider_retry_count for EVERY wire,
# including the dedicated image wire whose parse path skips _with_request_diagnostics.
import base64 as _b64  # noqa: E402


def test_dedicated_image_oneshot_reports_zero_retry_via_reliable_exit(monkeypatch):
    image_b64 = _b64.b64encode(b"\x89PNG\r\n\x1a\n fake").decode()

    def handler(request):
        assert request.url.path.endswith("/images"), request.url.path
        return httpx.Response(200, json={"data": [{"b64_json": image_b64}]})

    monkeypatch.setattr(pc, "_validate_egress_url", lambda *_a, **_k: None)
    monkeypatch.setattr(
        pc, "_shared_async_client",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    cfg = pc.ProviderConfig(provider="openrouter",
                            model="google/gemini-2.5-flash-image",
                            api_key="k", base_url="https://openrouter.ai/api/v1")
    out = asyncio.run(pc.reliable_chat_completion_async(
        cfg, [{"role": "user", "content": "draw a cat"}],
        max_attempts=1, allow_image_output=True, image_generation_probe=True))
    assert out.get("media"), "expected terminal image media"
    # The dedicated image parse path never called _with_request_diagnostics; the
    # reliable-exit guarantee is what supplies the count.
    assert _tl._transport_retry_count_from_usage(out["usage"]) == 0


def test_gemini_oneshot_reports_zero_retry_via_reliable_exit(monkeypatch):
    def handler(_request):
        return httpx.Response(200, json={
            "candidates": [{"finishReason": "STOP",
                            "content": {"parts": [{"text": "hi"}]}}],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1}})

    monkeypatch.setattr(pc, "_validate_egress_url", lambda *_a, **_k: None)
    monkeypatch.setattr(
        pc, "_shared_async_client",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    cfg = pc.ProviderConfig(provider="gemini", model="gemini-3.6-flash", api_key="k",
                            base_url="https://generativelanguage.googleapis.com/v1beta")
    out = asyncio.run(pc.reliable_chat_completion_async(
        cfg, [{"role": "user", "content": "hi"}], max_attempts=1))
    assert _tl._transport_retry_count_from_usage(out["usage"]) == 0
