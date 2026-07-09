"""Async mirror of `test_provider_retry.py` for `reliable_chat_completion_async`
(hosted-runtime-v2 concurrency fix): the V2 worker's provider call was bridged onto
a thread pool via `asyncio.to_thread(reliable_chat_completion, ...)`, which silently
caps concurrency at the thread pool size (~32). This wrapper is a natively async
mirror of the sync retry loop — same classification/backoff/terminal-labelling
semantics, but `await`s `chat_completion_async` and sleeps via `asyncio.sleep`
instead of blocking `time.sleep`, so the worker pool's own concurrency dial is real.

No real network: `chat_completion_async` is monkeypatched with a fake async
callable. Sleeps kept fast via `base_delay_sec=0.0`.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import provider_client as pc  # noqa: E402
from provider_client import ProviderError, reliable_chat_completion_async  # noqa: E402


def _fake_async(seq):
    """Async analogue of test_provider_retry.py's `_fake`: walks `seq`; Exception
    items are raised, others returned. Last item repeats once exhausted."""
    calls = {"n": 0}

    async def fn(*args, **kwargs):
        item = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        if isinstance(item, BaseException):
            raise item
        return item

    return fn, calls


def test_reliable_async_retries_transient_then_succeeds(monkeypatch):
    fn, calls = _fake_async([ProviderError("e", status_code=503),
                             ProviderError("e", status_code=503), "ok"])
    monkeypatch.setattr(pc, "chat_completion_async", fn)
    out = asyncio.run(reliable_chat_completion_async(max_attempts=3, base_delay_sec=0.0))
    assert out == "ok"
    assert calls["n"] == 3  # two failures + one success


def test_reliable_async_exhausts_persistent_transient(monkeypatch):
    fn, calls = _fake_async([ProviderError("e", status_code=500)])
    monkeypatch.setattr(pc, "chat_completion_async", fn)
    with pytest.raises(ProviderError) as ei:
        asyncio.run(reliable_chat_completion_async(max_attempts=3, base_delay_sec=0.0))
    assert ei.value.feedling_error_class == "transient_exhausted"
    assert calls["n"] == 3  # tried max_attempts times


def test_reliable_async_does_not_retry_provider_config(monkeypatch):
    fn, calls = _fake_async([ProviderError("402 out of credits", status_code=402), "ok"])
    monkeypatch.setattr(pc, "chat_completion_async", fn)
    with pytest.raises(ProviderError) as ei:
        asyncio.run(reliable_chat_completion_async(max_attempts=3, base_delay_sec=0.0))
    assert ei.value.feedling_error_class == "provider_config"
    assert calls["n"] == 1  # NOT retried


def test_reliable_async_passes_through_args(monkeypatch):
    seen = {}

    async def fn(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return "ok"

    monkeypatch.setattr(pc, "chat_completion_async", fn)
    out = asyncio.run(reliable_chat_completion_async("p", model="m", timeout=90, base_delay_sec=0.0))
    assert out == "ok"
    assert seen["args"] == ("p",)
    assert seen["kwargs"] == {"model": "m", "timeout": 90}  # retry kwargs not leaked through


def test_reliable_async_honours_retry_after_and_uses_asyncio_sleep(monkeypatch):
    """429 with a Retry-After should stretch the delay to at least that many
    seconds, and the sleep must go through `asyncio.sleep` (not blocking
    `time.sleep`) — that's the whole point of the async wrapper."""
    exc = ProviderError("slow down", status_code=429)
    exc.retry_after = 5.0
    fn, calls = _fake_async([exc, "ok"])
    monkeypatch.setattr(pc, "chat_completion_async", fn)

    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    out = asyncio.run(reliable_chat_completion_async(
        max_attempts=3, base_delay_sec=1.0, max_delay_sec=30.0))
    assert out == "ok"
    assert calls["n"] == 2
    assert len(sleeps) == 1
    assert sleeps[0] >= 5.0
