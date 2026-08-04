"""Genesis v2 Step 1 — shared retry wrapper + failure classification.

Covers `provider_client.classify_provider_error` and `reliable_chat_completion`:
transient failures (timeout / 429 / 5xx / empty) retry with backoff; user-config
failures (402 / bad key / 4xx config) never retry; the raised exception is labelled
so the genesis job can record `transient_exhausted` vs `provider_config`.
"""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import provider_client as pc  # noqa: E402
from provider_client import (  # noqa: E402
    ProviderError,
    classify_provider_error,
    reliable_chat_completion,
)


def test_classify_transient_vs_provider_config():
    # transient → retry
    assert classify_provider_error(ProviderError("x", status_code=429)) == "transient"
    assert classify_provider_error(ProviderError("x", status_code=503)) == "transient"
    assert classify_provider_error(ProviderError("x", status_code=408)) == "transient"
    assert classify_provider_error(ProviderError("no usable reply text")) == "transient"  # status None
    assert classify_provider_error(httpx.TimeoutException("t")) == "transient"
    assert classify_provider_error(httpx.ConnectError("c")) == "transient"
    # provider_config → never retry
    assert classify_provider_error(ProviderError("out of credits", status_code=402)) == "provider_config"
    assert classify_provider_error(ProviderError("bad key", status_code=401)) == "provider_config"
    assert classify_provider_error(ProviderError("forbidden", status_code=403)) == "provider_config"
    assert classify_provider_error(ProviderError("bad request", status_code=400)) == "provider_config"
    # unrecognised → unknown (treated as transient but capped)
    assert classify_provider_error(ValueError("?")) == "unknown"
    assert classify_provider_error(ProviderError("teapot", status_code=418)) == "unknown"


def _fake(seq):
    """A fake chat_completion: walks `seq`; Exception items are raised, others returned.
    The last item repeats once exhausted (so a single-element seq = 'always raises this')."""
    calls = {"n": 0}

    def fn(*args, **kwargs):
        item = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        if isinstance(item, BaseException):
            raise item
        return item

    return fn, calls


def test_reliable_retries_transient_then_succeeds(monkeypatch):
    fn, calls = _fake([ProviderError("e", status_code=503),
                       ProviderError("e", status_code=503), "ok"])
    monkeypatch.setattr(pc, "chat_completion", fn)
    out = reliable_chat_completion(max_attempts=3, base_delay_sec=0.0)
    assert out == "ok"
    assert calls["n"] == 3  # two failures + one success


def test_reliable_does_not_retry_provider_config(monkeypatch):
    fn, calls = _fake([ProviderError("402 out of credits", status_code=402), "ok"])
    monkeypatch.setattr(pc, "chat_completion", fn)
    with pytest.raises(ProviderError) as ei:
        reliable_chat_completion(max_attempts=3, base_delay_sec=0.0)
    assert ei.value.feedling_error_class == "provider_config"
    assert calls["n"] == 1  # NOT retried


def test_reliable_exhausts_persistent_transient(monkeypatch):
    fn, calls = _fake([ProviderError("e", status_code=500)])
    monkeypatch.setattr(pc, "chat_completion", fn)
    with pytest.raises(ProviderError) as ei:
        reliable_chat_completion(max_attempts=3, base_delay_sec=0.0)
    assert ei.value.feedling_error_class == "transient_exhausted"
    assert calls["n"] == 3  # tried max_attempts times


def test_reliable_caps_timeout_attempts_without_changing_429_budget(monkeypatch):
    timeout_fn, timeout_calls = _fake([httpx.ReadTimeout("slow")])
    monkeypatch.setattr(pc, "chat_completion", timeout_fn)
    with pytest.raises(httpx.ReadTimeout):
        reliable_chat_completion(
            max_attempts=3,
            max_timeout_attempts=2,
            base_delay_sec=0.0,
        )
    assert timeout_calls["n"] == 2

    rate_fn, rate_calls = _fake([ProviderError("rate", status_code=429),
                                 ProviderError("rate", status_code=429), "ok"])
    monkeypatch.setattr(pc, "chat_completion", rate_fn)
    assert reliable_chat_completion(
        max_attempts=3,
        max_timeout_attempts=2,
        base_delay_sec=0.0,
    ) == "ok"
    assert rate_calls["n"] == 3


def test_reliable_caps_adapter_wrapped_read_timeout(monkeypatch):
    wrapped = ProviderError("provider network error: ReadTimeout")
    wrapped.__cause__ = httpx.ReadTimeout("slow")
    fn, calls = _fake([wrapped])
    monkeypatch.setattr(pc, "chat_completion", fn)

    with pytest.raises(ProviderError):
        reliable_chat_completion(
            max_attempts=3,
            max_timeout_attempts=2,
            base_delay_sec=0.0,
        )

    assert calls["n"] == 2


def test_reliable_passes_through_args(monkeypatch):
    seen = {}

    def fn(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return "ok"

    monkeypatch.setattr(pc, "chat_completion", fn)
    out = reliable_chat_completion("p", model="m", timeout=90, base_delay_sec=0.0)
    assert out == "ok"
    assert seen["args"] == ("p",)
    assert seen["kwargs"] == {"model": "m", "timeout": 90}  # retry kwargs not leaked through
