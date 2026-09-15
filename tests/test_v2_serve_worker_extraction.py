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


def test_memory_context_uses_plaintext_identity_names(monkeypatch):
    serve_worker.wire_assembly()
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "token")
    monkeypatch.setattr(
        serve_worker,
        "_load_identity_card_view",
        lambda _store, *, runtime_token: {
            "agent_name": "pre c",
            "user_preferred_name": "Seven",
        },
    )
    monkeypatch.setattr("memory.memory_core.buckets", lambda *a, **k: ({"buckets": []}, 200))
    monkeypatch.setattr("memory.memory_core.threads", lambda *a, **k: ({"threads": []}, 200))
    monkeypatch.setattr("memory.memory_core.index", lambda *a, **k: ({"items": []}, 200))

    ctx = serve_worker._read_memory_context("u-identity")

    assert ctx["ai_name"] == "pre c"
    assert ctx["user_name"] == "Seven"
    assert '"agent_name":"pre c"' in ctx["identity"]


def test_dream_context_fetches_full_cards_without_cross_run_cooldown(monkeypatch):
    serve_worker.wire_assembly()
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "token")
    monkeypatch.setattr(serve_worker.time, "time", lambda: 1785542400.0)  # 2026-08-01 UTC
    monkeypatch.setattr(
        "memory.memory_core.buckets", lambda *a, **k: ({"buckets": []}, 200)
    )
    monkeypatch.setattr(
        "memory.memory_core.threads", lambda *a, **k: ({"threads": []}, 200)
    )
    monkeypatch.setattr("identity.identity_core.get_identity", lambda *a, **k: ({}, 200))
    monkeypatch.setattr(
        "memory.memory_core.index",
        lambda *a, **k: ({"items": [{"id": "capture-old"}, {"id": "dream-new"}]}, 200),
    )
    def fetch_cards(_store, _api_key, payload, *, post_enclave):
        assert "include_sensitive" not in payload
        assert "user_explicit_selection" not in payload
        return {"items": [
            {
                "id": "capture-old",
                "summary": "完整摘要",
                "content": "只有 fetch 才返回的完整正文。",
                "source": "memory_capture",
                "occurred_at": "2026-05-01T00:00:00Z",
                "created_at": "2026-07-31T00:00:00Z",
            },
            {
                "id": "dream-new",
                "summary": "新 dream 卡",
                "content": "上一轮 Dream 卡可在后续运行重新参与整理。",
                "source": "memory_dream",
                "occurred_at": "2026-07-01T00:00:00Z",
                "created_at": "2026-07-31T00:00:00Z",
            },
        ]}, 200

    monkeypatch.setattr("memory.memory_core.fetch", fetch_cards)

    ctx = serve_worker._read_dream_memory_context("u_ctx_full")

    # The reader hands the fetched cards over whole; rendering (with bodies)
    # and the prompt budget belong to the Garden component.
    assert "cards" not in ctx
    assert [item["id"] for item in ctx["card_items"]] == ["capture-old", "dream-new"]
    assert ctx["card_items"][0]["content"] == "只有 fetch 才返回的完整正文。"
    assert [item["occurred_at"] for item in ctx["card_items"]] == [
        "2026-05-01T00:00:00Z",
        "2026-07-01T00:00:00Z",
    ]
    assert ctx["_diagnostic_cards_outcome"] == "ready"


def test_dream_context_does_not_pre_budget_cards(monkeypatch):
    """Every fetched card reaches the worker, however long. The component
    applies the Dream prompt budget (and reports what it left out), so a
    second, different budget here would silently drop cards before it."""
    serve_worker.wire_assembly()
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "token")
    assert not hasattr(serve_worker, "_DREAM_CARDS_MAX_CHARS")
    assert not hasattr(serve_worker, "_render_card_line")
    monkeypatch.setattr("memory.memory_core.buckets", lambda *a, **k: ({"buckets": []}, 200))
    monkeypatch.setattr("memory.memory_core.threads", lambda *a, **k: ({"threads": []}, 200))
    monkeypatch.setattr("identity.identity_core.get_identity", lambda *a, **k: ({}, 200))
    monkeypatch.setattr(
        "memory.memory_core.index",
        lambda *a, **k: ({"items": [{"id": "m1"}, {"id": "m2"}]}, 200),
    )
    fetched = [
        {
            "id": memory_id,
            "summary": f"摘要-{memory_id}",
            "content": "正文" * 40_000,
            "source": "memory_capture",
            "occurred_at": "2026-07-01T00:00:00Z",
        }
        for memory_id in ("m1", "m2")
    ]
    monkeypatch.setattr(
        "memory.memory_core.fetch", lambda *a, **k: ({"items": fetched}, 200)
    )

    ctx = serve_worker._read_dream_memory_context("u_ctx_budget")

    assert [item["id"] for item in ctx["card_items"]] == ["m1", "m2"]
    assert ctx["card_items"][1]["content"] == "正文" * 40_000
    assert ctx["_diagnostic_cards_outcome"] == "ready"


def test_capture_context_skips_the_card_read(monkeypatch):
    """Capture's component request carries no card index, so its context must
    not pay an enclave round trip for one."""
    serve_worker.wire_assembly()
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "token")
    monkeypatch.setattr("memory.memory_core.buckets", lambda *a, **k: ({"buckets": []}, 200))
    monkeypatch.setattr("memory.memory_core.threads", lambda *a, **k: ({"threads": []}, 200))

    def _no_index(*_a, **_k):
        raise AssertionError("capture context must not read the card index")

    monkeypatch.setattr("memory.memory_core.index", _no_index)
    monkeypatch.setattr("memory.memory_core.fetch", _no_index)

    ctx = serve_worker._read_memory_context("u_ctx_capture")

    assert ctx["card_items"] == []
    assert "cards" not in ctx
    assert "_diagnostic_cards_outcome" not in ctx


def test_dream_context_distinguishes_empty_index_from_failed_full_card_read(
    monkeypatch,
):
    serve_worker.wire_assembly()
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "token")
    monkeypatch.setattr(
        "memory.memory_core.buckets", lambda *a, **k: ({"buckets": []}, 200)
    )
    monkeypatch.setattr(
        "memory.memory_core.threads", lambda *a, **k: ({"threads": []}, 200)
    )
    monkeypatch.setattr(
        "identity.identity_core.get_identity", lambda *a, **k: ({}, 200)
    )
    monkeypatch.setattr(
        "memory.memory_core.index",
        lambda *a, **k: ({"items": [], "user_card_count": 0}, 200),
    )
    empty = serve_worker._read_dream_memory_context("u_ctx_empty")
    assert empty["_diagnostic_cards_outcome"] == "empty"

    # 200 + no items is only an empty garden when the live card count says so:
    # the readside drops every card it cannot decrypt and still answers 200.
    for unverified in ({"items": []}, {"items": [], "user_card_count": 3}):
        monkeypatch.setattr(
            "memory.memory_core.index", lambda *a, _b=unverified, **k: (_b, 200)
        )
        unreadable = serve_worker._read_dream_memory_context("u_ctx_unreadable")
        assert unreadable["_diagnostic_cards_outcome"] == "unavailable"

    monkeypatch.setattr(
        "memory.memory_core.index",
        lambda *a, **k: ({"items": [{"id": "m1"}]}, 200),
    )
    monkeypatch.setattr(
        "memory.memory_core.fetch",
        lambda *a, **k: ({"error": "unavailable"}, 503),
    )
    failed = serve_worker._read_dream_memory_context("u_ctx_failed")
    assert failed["card_items"] == []
    assert failed["_diagnostic_cards_outcome"] == "unavailable"


def test_capture_submit_enqueues_a_capture_agent_job(monkeypatch):
    from model_api_runtime.v2 import jobs_store
    serve_worker.wire_assembly()
    calls = []
    monkeypatch.setattr(jobs_store, "enqueue_capture",
                        lambda u, **kw: calls.append((u, "capture")) or
                        jobs_store.CaptureEnqueueResult(1, "created"))
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
