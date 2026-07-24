"""serve_worker：capture/dream 抽取 lane 的生产装配（Task 4）——记忆上下文读取的逐项降级、
capture/dream submitter 把 job 塞进 agent_jobs 的接线。不起真 worker/真 enclave/真 provider。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import serve_worker


def test_memory_context_degrades_each_field_independently(monkeypatch):
    """One failing sub-fetch must not blank the others, and must not raise."""
    serve_worker.wire_assembly()
    monkeypatch.setattr("memory.memory_core.buckets",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("memory.memory_core.threads", lambda *a, **k: ({"threads": ["t1"]}, 200))
    ctx = serve_worker._read_memory_context("u_ctx_degrade")
    assert ctx["buckets"] == ""
    assert isinstance(ctx["threads"], str)


def test_capture_submit_enqueues_a_capture_agent_job(monkeypatch):
    from model_api_runtime.v2 import jobs_store
    serve_worker.wire_assembly()
    calls = []
    monkeypatch.setattr(jobs_store, "enqueue_job",
                        lambda u, lane, **kw: calls.append((u, lane)) or ("j1", False))
    monkeypatch.setattr("proactive.capture_scheduler.tick_quiet_capture",
                        lambda store, *, now=None, submit=None:
                            submit(
                                store,
                                trigger="quiet_timeout",
                                now=0.0,
                                window={"after_seq": 0, "through_seq": 1},
                                capture_key="capture:test",
                            ))
    assert serve_worker._tick_capture_for_user("u_cap") == 1
    assert calls == [("u_cap", "capture")]


def test_dream_submit_enqueues_a_dream_agent_job(monkeypatch):
    from model_api_runtime.v2 import jobs_store
    serve_worker.wire_assembly()
    calls = []
    monkeypatch.setattr(jobs_store, "enqueue_job",
                        lambda u, lane, **kw: calls.append((u, lane)) or ("j1", False))
    monkeypatch.setattr("proactive.dream_scheduler.tick_memory_dream",
                        lambda store, *, now=None, force=False, submit=None:
                            submit(store, trigger="dream", now=0.0))
    assert serve_worker._tick_dream_for_user("u_dream") == 1
    assert calls == [("u_dream", "dream")]
