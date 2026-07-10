import threading

import pytest

from model_api_runtime.v2 import serve_worker


def test_genesis_thread_dormant_without_env(monkeypatch):
    monkeypatch.delenv("FEEDLING_GENESIS_WORKER_ENABLED", raising=False)
    assert serve_worker._start_genesis_thread("w1") is None


def test_genesis_thread_dormant_when_prereqs_missing(monkeypatch):
    monkeypatch.setenv("FEEDLING_GENESIS_WORKER_ENABLED", "1")
    monkeypatch.delenv("FEEDLING_RUNTIME_TOKEN_SECRET", raising=False)
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave")
    assert serve_worker._start_genesis_thread("w1") is None


def test_genesis_thread_starts_and_stops(monkeypatch):
    monkeypatch.setenv("FEEDLING_GENESIS_WORKER_ENABLED", "1")
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", "s3cret")
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave")
    monkeypatch.setenv("FEEDLING_API_URL", "https://api")
    monkeypatch.setenv("FEEDLING_GENESIS_WORKER_INTERVAL_SEC", "0.01")

    ran = threading.Event()
    beats: list[tuple] = []

    def _fake_run_loop(*, api_url, enclave_url, mint_genesis, interval, stop_event, on_beat=None):
        assert api_url == "https://api"
        assert enclave_url == "https://enclave"
        if on_beat:
            on_beat()
        ran.set()
        stop_event.wait(5)

    monkeypatch.setattr(serve_worker.genesis_daemon, "run_loop", _fake_run_loop)
    monkeypatch.setattr(serve_worker.jobs_store, "record_worker_heartbeat",
                        lambda wid, **kw: beats.append((wid, kw.get("kind"))))

    started = serve_worker._start_genesis_thread("w1")
    assert started is not None
    thread, stop = started
    assert ran.wait(5)
    assert beats == [("w1:genesis", "genesis")]

    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_genesis_token_scope_is_wider_than_chat_scope():
    """The chat path must NOT get the genesis scope; genesis must have both."""
    assert serve_worker._RUNTIME_TOKEN_SCOPE == ["envelope_decrypt"]
    assert set(serve_worker._GENESIS_TOKEN_SCOPE) == {"envelope_decrypt", "genesis"}


def test_mint_genesis_token_carries_both_scopes(monkeypatch):
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", "s3cret")
    from core import runtime_token
    token = serve_worker._mint_genesis_token("usr_1")
    claims = runtime_token.verify(b"s3cret", token)
    assert claims["user_id"] == "usr_1"
    assert set(claims["scope"]) == {"envelope_decrypt", "genesis"}
