"""serve_worker：capture/dream 抽取 lane 的生产装配（Task 4）——记忆上下文读取的逐项降级、
capture/dream submitter 把 job 塞进 agent_jobs 的接线。不起真 worker/真 enclave/真 provider。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import serve_worker
from memory.dream_prompt_v1 import build_dream_prompt


def test_memory_context_degrades_each_field_independently(monkeypatch):
    """One failing sub-fetch must not blank the others, and must not raise."""
    serve_worker.wire_assembly()
    monkeypatch.setattr("memory.memory_core.buckets",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("memory.memory_core.threads", lambda *a, **k: ({"threads": ["t1"]}, 200))
    ctx = serve_worker._read_memory_context("u_ctx_degrade")
    assert ctx["buckets"] == ""
    assert isinstance(ctx["threads"], str)


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
    monkeypatch.setattr(
        "memory.memory_core.fetch",
        lambda *a, **k: ({"items": [
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
        ]}, 200),
    )

    ctx = serve_worker._read_dream_memory_context("u_ctx_full")

    assert "完整摘要" in ctx["cards"]
    assert "只有 fetch 才返回的完整正文。" in ctx["cards"]
    assert "dream-new" in ctx["cards"]
    assert "上一轮 Dream 卡可在后续运行重新参与整理。" in ctx["cards"]
    assert [item["id"] for item in ctx["card_items"]] == ["capture-old", "dream-new"]
    assert [item["occurred_at"] for item in ctx["card_items"]] == [
        "2026-05-01T00:00:00Z",
        "2026-07-01T00:00:00Z",
    ]


def test_dream_context_budget_keeps_only_whole_cards(monkeypatch):
    serve_worker.wire_assembly()
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "token")
    monkeypatch.setattr(serve_worker, "_DREAM_CARDS_MAX_CHARS", 250)
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
            "content": "正文" * 40,
            "source": "memory_capture",
            "occurred_at": "2026-07-01T00:00:00Z",
            "created_at": "2026-07-01T00:00:00Z",
        }
        for memory_id in ("m1", "m2")
    ]
    monkeypatch.setattr(
        "memory.memory_core.fetch", lambda *a, **k: ({"items": fetched}, 200)
    )

    ctx = serve_worker._read_dream_memory_context("u_ctx_budget")

    assert [item["id"] for item in ctx["card_items"]] == ["m1"]
    assert "id=m1" in ctx["cards"]
    assert "id=m2" not in ctx["cards"]
    assert ctx["card_items"][0]["content"] == "正文" * 40


def test_dream_context_budget_rejects_oversized_first_card_from_final_prompt(
    monkeypatch, caplog,
):
    serve_worker.wire_assembly()
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "token")
    monkeypatch.setattr(serve_worker, "_DREAM_CARDS_MAX_CHARS", 150)
    monkeypatch.setattr("memory.memory_core.buckets", lambda *a, **k: ({"buckets": []}, 200))
    monkeypatch.setattr("memory.memory_core.threads", lambda *a, **k: ({"threads": []}, 200))
    monkeypatch.setattr("identity.identity_core.get_identity", lambda *a, **k: ({}, 200))
    monkeypatch.setattr(
        "memory.memory_core.index",
        lambda *a, **k: ({"items": [{"id": "oversized-first"}]}, 200),
    )
    monkeypatch.setattr(
        "memory.memory_core.fetch",
        lambda *a, **k: (
            {
                "items": [
                    {
                        "id": "oversized-first",
                        "summary": "超限首卡",
                        "content": "正文" * 100,
                        "source": "memory_capture",
                        "created_at": "2026-07-01T00:00:00Z",
                    }
                ]
            },
            200,
        ),
    )

    with caplog.at_level("WARNING", logger="feedling.runtime_v2.serve_worker"):
        ctx = serve_worker._read_dream_memory_context("u_ctx_oversized_first")
    prompt = build_dream_prompt(
        ai_name=ctx["ai_name"],
        user_name=ctx["user_name"],
        cards=ctx["cards"],
        recent_conversations="",
    )
    empty_prompt = build_dream_prompt(
        ai_name=ctx["ai_name"],
        user_name=ctx["user_name"],
        cards="",
        recent_conversations="",
    )

    assert ctx["card_items"] == []
    assert len(prompt) == len(empty_prompt)
    assert "oversized-first" not in prompt
    budget_logs = [
        record.getMessage()
        for record in caplog.records
        if "dream cards truncated" in record.getMessage()
    ]
    assert len(budget_logs) == 1
    assert "kept=0/1" in budget_logs[0]
    assert "empty_context=True" in budget_logs[0]
    assert "oversized-first" not in budget_logs[0]


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
