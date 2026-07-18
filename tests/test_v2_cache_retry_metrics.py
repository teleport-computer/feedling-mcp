"""Prompt-cache compatibility retries remain visible at the turn boundary."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import jobs_store, worker  # noqa: E402


def test_turn_metrics_persist_provider_http_retries(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        jobs_store,
        "record_whole_turn_metric",
        lambda *args, **kwargs: captured.update(args=args, kwargs=kwargs),
    )
    tm = worker.TurnMetrics(job_id=123, user_id="u", lane="chat")

    tm.add_call({
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "provider_retry_count": 2,
    })
    # Booleans and absurd values are provider-controlled input, not counters.
    tm.add_call({"provider_retry_count": True})
    tm.add_call({"provider_retry_count": 10_000})
    tm.flush(failed=False, status="ok")

    assert captured["kwargs"]["model_calls"] == 3
    assert captured["kwargs"]["retries"] == 1002


def test_cache_turn_details_select_persisted_retry_count(monkeypatch) -> None:
    captured: dict = {}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            captured["query"] = query
            captured["params"] = params

        def fetchone(self):
            return (0, 0, 0, 0, None, None, None, None, 0, 0, None, [])

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

    class Pool:
        def connection(self):
            return Connection()

    monkeypatch.setattr(jobs_store, "_pool", lambda: Pool())

    stats = jobs_store.recent_prompt_cache_stats(include_turns=True)

    assert "SELECT id, job_id, created_at, model_calls, retries" in captured["query"]
    assert "'retries', retries" in captured["query"]
    assert stats["turns"] == []
