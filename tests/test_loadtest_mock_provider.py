"""Smoke tests for the load-test mock LLM provider (scripts/loadtest/mock_provider.py).

The mock provider is a minimal stdlib-only HTTP server that impersonates the
OpenAI/Anthropic chat wire so V2 load tests can drive real turns without
burning real BYOK credit or real tokens. It must return a deterministic reply
with configurable, fixed `usage` token counts and optional latency.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.loadtest.mock_provider import MockProvider


def _post(base_url: str, path: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base_url + path,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # The mock is always loopback. Bypass macOS/system proxy settings so a
    # stopped ephemeral port produces the real connection-refused signal
    # instead of a proxy-generated empty 502 response.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as resp:
            status = resp.status
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        status = exc.code
        data = json.loads(exc.read().decode("utf-8"))
    return status, data


def test_default_reply_and_usage_shape():
    provider = MockProvider()
    provider.start()
    try:
        status, data = _post(
            provider.base_url,
            "/v1/chat/completions",
            {"model": "mock", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert status == 200
        assert data["object"] == "chat.completion"
        choice = data["choices"][0]
        assert choice["message"]["role"] == "assistant"
        assert choice["message"]["content"] == provider.reply
        assert choice["finish_reason"] == "stop"
        usage = data["usage"]
        assert usage["prompt_tokens"] == 100
        assert usage["completion_tokens"] == 20
        assert usage["total_tokens"] == 120
    finally:
        provider.stop()


def test_accepts_anthropic_style_path():
    provider = MockProvider(reply="canned anthropic reply")
    provider.start()
    try:
        status, data = _post(
            provider.base_url,
            "/v1/messages",
            {"model": "mock", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert status == 200
        assert data["choices"][0]["message"]["content"] == "canned anthropic reply"
    finally:
        provider.stop()


def test_configurable_token_counts_reflected():
    provider = MockProvider(prompt_tokens=7, completion_tokens=3)
    provider.start()
    try:
        _status, data = _post(provider.base_url, "/chat/completions", {"messages": []})
        usage = data["usage"]
        assert usage["prompt_tokens"] == 7
        assert usage["completion_tokens"] == 3
        assert usage["total_tokens"] == 10
    finally:
        provider.stop()


def test_configurable_reply_text():
    provider = MockProvider(reply="a custom canned reply")
    provider.start()
    try:
        _status, data = _post(provider.base_url, "/v1/chat/completions", {"messages": []})
        assert data["choices"][0]["message"]["content"] == "a custom canned reply"
    finally:
        provider.stop()


def test_latency_ms_is_honored():
    provider = MockProvider(latency_ms=50)
    provider.start()
    try:
        start = time.monotonic()
        _post(provider.base_url, "/v1/chat/completions", {"messages": []})
        elapsed_ms = (time.monotonic() - start) * 1000
        # Loose lower bound to avoid flakiness while still proving the sleep
        # actually happened (well below the configured 50ms would mean the
        # server ignored latency_ms entirely).
        assert elapsed_ms >= 40
    finally:
        provider.stop()


def test_zero_latency_by_default_is_fast():
    provider = MockProvider()
    provider.start()
    try:
        start = time.monotonic()
        _post(provider.base_url, "/v1/chat/completions", {"messages": []})
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 1000
    finally:
        provider.stop()


def test_stop_is_clean_and_does_not_hang():
    provider = MockProvider()
    provider.start()
    url = provider.base_url
    _post(url, "/v1/chat/completions", {"messages": []})

    stop_start = time.monotonic()
    provider.stop()
    stop_elapsed = time.monotonic() - stop_start
    assert stop_elapsed < 5

    # server should no longer be reachable after stop (captured base_url,
    # since the port is no longer available from the stopped provider)
    try:
        _post(url, "/v1/chat/completions", {"messages": []})
        reachable = True
    except (urllib.error.URLError, ConnectionError, OSError):
        reachable = False
    assert not reachable


def test_context_manager_start_stop():
    with MockProvider(reply="ctx reply") as provider:
        status, data = _post(provider.base_url, "/v1/chat/completions", {"messages": []})
        assert status == 200
        assert data["choices"][0]["message"]["content"] == "ctx reply"


def test_base_url_uses_ephemeral_port():
    provider = MockProvider()
    provider.start()
    try:
        assert provider.base_url.startswith("http://127.0.0.1:")
        port = int(provider.base_url.rsplit(":", 1)[1])
        assert port > 0
    finally:
        provider.stop()
