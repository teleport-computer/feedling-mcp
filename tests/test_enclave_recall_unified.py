"""Automatic recall (/v1/chat/history context_memories) on memgarden.retrieval.select_context.

Pure unit: drives the real ``_build_context_memories`` with decrypted cards
monkeypatched in (no DB, no crypto), and feeds its output to the real V2 renderer.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import memory_search_contract as contract  # noqa: E402
from enclave.routes import chat  # noqa: E402
from memgarden import observability  # noqa: E402
from model_api_runtime.v2 import memory_context  # noqa: E402

ARGS = {"context_mode": "", "want_trace": True, "authorized_user_id": "u", "content_sk": None}

WINDOW = [
    {"role": "user", "content": "今天好累啊，刚下班到家"},
    {"role": "assistant", "content": "辛苦啦，晚饭吃了吗？要不要先歇会儿"},
    {"role": "user", "content": "还行，就是担心家里猫咪最近不吃饭"},
    {"role": "assistant", "content": "是咪咪吗？吃饭情况持续几天了？"},
]
# T684: the latest two user messages, newest first; no assistant anchors.
USER_QUERY = "还行，就是担心家里猫咪最近不吃饭\n今天好累啊，刚下班到家"


def _cards(*extra):
    return [
        {"id": "cat", "title": "猫咪照顾", "linked_dimension": "猫咪", "status": "active",
         "description": "用户聊猫咪健康问题时，先需要被安抚，再给观察饮水和精神状态的建议。"},
        {"id": "kidney", "summary": "咪咪是只三岁的橘猫，去年体检肾指标偏高",
         "content": "医生让每半年复查一次", "status": "active"},
        {"id": "archived_cat", "summary": "猫咪 咪咪 吃饭 旧卡", "archived_at": "2026-01-01",
         "status": "active"},
        *extra,
    ]


def _run(monkeypatch, cards, rows=WINDOW, **args):
    monkeypatch.setattr(chat.readside, "moments_to_cards", lambda *a: cards)
    return chat._build_context_memories([], rows, {**ARGS, **args})


def test_default_unified_ranker_records_version_and_latest_two_user_query(monkeypatch):
    monkeypatch.delenv(chat.RECALL_RANKER_ENV, raising=False)
    legacy = []
    monkeypatch.setattr(chat.memory_relevance, "select_relevant_context_memories_with_trace",
                        lambda *a, **k: legacy.append(1))
    filler = [{"id": f"f{i}", "summary": f"第{i}次整理工作周报和会议纪要", "status": "active"}
              for i in range(30)]
    picked, trace, log = _run(monkeypatch, _cards(*filler))
    assert not legacy
    assert log["query_fingerprint"] == observability.query_fingerprint(USER_QUERY)
    # T684: “咪咪” occurs only in the assistant reply, so kidney loses its anchor.
    assert {c["id"] for c in picked} == {"cat"}
    # The model gets the original io cards, not the kernel translation.
    assert next(c for c in picked if c["id"] == "cat")["title"] == "猫咪照顾"
    assert "search_text" not in json.dumps(picked, ensure_ascii=False)
    assert trace["version"] == contract.RECALL_VERSION + "+host:latest-first-v1"
    assert trace["kernel_version"] == contract.RECALL_VERSION
    assert contract.RECALL_VERSION.startswith(contract.VERSION + "+cfg:")
    assert log["mode"] == f"relevant:unified:{contract.RECALL_VERSION}+host:latest-first-v1"
    assert log["injected_ids"] == [c["id"] for c in picked]
    by_id = {item["id"]: item for item in trace["selected"]}
    assert by_id["cat"]["bucket"] in {"query", "recent"} and by_id["cat"]["reason"] == "bm25_match"
    assert by_id["cat"]["matched_phrases"][0] == "猫咪"
    assert [c["id"] for c in picked] == [item["id"] for item in trace["selected"]]


def test_lifecycle_filter_runs_before_translation(monkeypatch):
    picked, trace, _ = _run(monkeypatch, _cards())
    assert "archived_cat" not in {c["id"] for c in picked}
    assert trace["index_count"] == 2  # the archived card never reached the ranker


def test_one_anchor_inside_everyday_chat_is_recalled_even_in_a_tiny_garden(monkeypatch):
    # With memory_search's gate (strong_evidence 1.25) this window recalled nothing:
    # every chit-chat word the garden never saw counted against the cat card.
    for size in (0, 8, 30):
        filler = [{"id": f"f{i}", "summary": f"第{i}次整理工作周报和会议纪要", "status": "active"}
                  for i in range(size)]
        picked, _, _ = _run(monkeypatch, _cards(*filler))
        assert "cat" in {c["id"] for c in picked}, size


def test_stopword_only_window_injects_nothing(monkeypatch):
    rows = [{"role": "user", "content": "嗯"}, {"role": "assistant", "content": "我在呢"},
            {"role": "user", "content": "那就这样吧"}, {"role": "assistant", "content": "好的呀"}]
    picked, trace, log = _run(monkeypatch, _cards(), rows=rows)
    assert picked == [] and trace["selected"] == []
    assert log["counts"]["injected"] == 0


def test_kill_switch_off_restores_previous_selector_with_latest_two_user_query(monkeypatch):
    calls = []
    original = chat.memory_relevance.select_relevant_context_memories_with_trace

    def spy(cards, query):
        calls.append(query)
        return original(cards, query)

    monkeypatch.setattr(chat.memory_relevance, "select_relevant_context_memories_with_trace", spy)
    for value in ("0", "false", "off", "NO"):
        monkeypatch.setenv(chat.RECALL_RANKER_ENV, value)
        picked, trace, log = _run(monkeypatch, _cards())
        assert log["mode"] == "relevant:unified:legacy-relevance+host:latest-first-v1"
        assert trace["mode"] == "latest_first"
        assert all(p["trace"]["mode"] == "relevant" for p in trace["passes"])
    # T684 changes query construction for both selectors, not the kill switch.
    assert calls == [WINDOW[2]["content"], USER_QUERY] * 4
    for value in ("1", "true", "", "anything"):
        monkeypatch.setenv(chat.RECALL_RANKER_ENV, value)
        assert _run(monkeypatch, _cards())[2]["mode"].startswith("relevant:unified:memgarden-bm25-v2")
    assert len(calls) == 8


def test_record_stays_content_free(monkeypatch):
    _, trace, log = _run(monkeypatch, _cards(), context_recent=True)
    observability.assert_content_free(log)
    dumped = json.dumps(log, ensure_ascii=False)
    for text in ("猫咪", "咪咪", "吃饭", "下班"):
        assert text not in dumped
    # matched_phrases only travel in the response trace, never the stored record.
    assert any(item.get("matched_phrases") for item in trace["selected"])


def test_fresh_recent_cards_stay_ahead_of_bm25_scores_in_the_v2_block(monkeypatch):
    from datetime import datetime, timedelta, timezone
    created = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    fresh = {"id": "fresh", "summary": "新写的一张无关卡", "created_at": created, "status": "active"}
    picked, trace, log = _run(monkeypatch, _cards(fresh), context_recent=True)
    assert [c["id"] for c in picked][0] == "fresh"
    scores = {item["id"]: item["score"] for item in trace["selected"]}
    assert scores["fresh"] > max(v for k, v in scores.items() if k != "fresh") > 1.0
    assert log["mode"].endswith(":recent7d")
    view = memory_context.render({"context_memories": picked, "context_memory_trace": trace,
                                  "context_memory_log": log})
    assert view["ids"][0] == "fresh"
    assert "最近7天新卡" in view["block"]
    assert json.loads(view["block"].splitlines()[3])["id"] == "cat"


def test_cap_is_eight_with_user_anchors_and_query_reasons_render_matched_terms(monkeypatch):
    anchors = ["猫咪", "咪咪", "吃饭", "下班", "晚饭", "歇会儿", "持续", "几天", "担心", "家里"]
    many = [{"id": f"c{i:02d}", "summary": f"{word}相关的一件事", "status": "active"}
            for i, word in enumerate(anchors)]
    filler = [{"id": f"f{i}", "summary": f"第{i}次整理工作周报和会议纪要", "status": "active"}
              for i in range(40)]
    # T684: >8 anchors must occur in user text to exercise the unchanged cap;
    # the original WINDOW now supplies only 5, which cannot test truncation.
    latest = "我担心家里猫咪咪咪吃饭的情况持续几天了，晚饭后下班到家想歇会儿。"
    rows = [WINDOW[0], WINDOW[1], {"role": "user", "content": latest}, WINDOW[3]]
    picked, trace, log = _run(monkeypatch, many + filler, rows=rows)
    assert log["query_fingerprint"] == observability.query_fingerprint(
        latest + "\n今天好累啊，刚下班到家")
    assert len(picked) == 8 and log["counts"]["cap"] == 8
    view = memory_context.render({"context_memories": picked, "context_memory_trace": trace,
                                  "context_memory_log": log})
    assert "与这句相关：匹配「" in view["block"]
