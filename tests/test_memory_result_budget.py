"""T511: real memory core -> capability -> executor -> batch -> provider wire.

Only persistence, trace sink, and provider transport are replaced. No mocks of
our new projection, policy, metadata, dispatch, or normalization producers.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from capabilities import memory, memory_results, result_budget
from memory import memory_core, service
from model_api_runtime.v2 import executor, tool_loop
import provider_client
from provider_types import ToolCall, ToolResult


@pytest.fixture
def garden(monkeypatch):
    rows = [{
        "id": f"{i:032x}", "owner_user_id": "budget-user", "status": "active",
        "importance": 0.7, "pulse": 0.3, "created_at": "2026-09-07T14:59:13Z",
        "occurred_at": "2026-09-07T14:59:13Z", "updated_at": "2026-09-07T14:59:13Z",
        "body": json.dumps({"summary": f"生活物品第{i}件的小档案。第二句话。",
                            "content": "正文" * 2500, "bucket": "生活", "threads": ["生活物品"]}, ensure_ascii=False),
    } for i in range(94)]
    monkeypatch.setattr(service.db, "memory_load_strict", lambda _user: list(rows))
    monkeypatch.setattr(service.db, "memory_replace_all", lambda _user, updated: None)
    events = []
    monkeypatch.setattr(memory_core.debug_trace, "trace_event", lambda _store, **event: events.append(event))
    return SimpleNamespace(user_id="budget-user"), rows, events


async def _dispatch(store, calls):
    return await executor.dispatch_tool_calls(
        calls, store=store, api_key=None, runtime_token=None,
        enclave_sem=asyncio.Semaphore(1), turn_authorization=False,
        enqueue_write_effect=lambda _call: None,
    )


@pytest.mark.parametrize("name", result_budget.MEMORY_TOOL_NAMES)
def test_memory_policy_is_atomic_and_reserves_batch_room(name):
    policy = result_budget.for_tool(name)
    assert policy is not None and policy.atomic_json is True
    assert policy.result_cap >= (5000 if name == "memory_fetch" else 2000)
    assert policy.extra_batch_budget == policy.result_cap
    result_budget.validate_result_caps(batch_cap=8000)


@pytest.mark.parametrize("name", ["memory_index", "memory_search"])
@pytest.mark.parametrize("summary,expected", [
    ("球拍穿线 23.5 磅。下次换 24。", "球拍穿线 23.5 磅。"),
    ("v2.0 版本上线了。", "v2.0 版本上线了。"),
    ("3.5 mm 的插头", "3.5 mm 的插头"),
    ("Everything with you... 绣在肩带", "Everything with you... 绣在肩带"),
    ("First sentence. Next sentence.", "First sentence."),
    ("English sentence.下一句。", "English sentence."),
    ("找到了！下一句。", "找到了！"),
    ("第一行\n第二行", "第一行"),
])
def test_index_first_sentence_preserves_numeric_facts_and_ellipsis(name, summary, expected):
    payload = memory_results.index_payload(
        {"items": [{"id": "sentence-card", "summary": summary}]}, tool_name=name,
    )
    # Verify the model-facing JSON, not an independent copy of the split rule.
    rendered = executor._summarize_capability_result({"ok": True, "data": payload}, tool_name=name)
    assert json.loads(rendered)["items"][0]["summary"] == expected


@pytest.mark.parametrize("name,args", [("memory_index", {}), ("memory_search", {"query": "档案"})])
def test_94_cards_survive_real_pipeline_with_seven_siblings(garden, name, args):
    store, rows, events = garden
    (result,) = asyncio.run(_dispatch(store, [ToolCall("m", name, args)]))
    assert result.metadata[result_budget.RESULT_KIND_METADATA_KEY] == name
    siblings = [ToolResult(f"s{i}", "x" * 2000) for i in range(7)]
    batch = tool_loop._normalize_tool_results([result, *siblings], per_result_cap=2000, batch_cap=8000)
    payload = json.loads(batch[0].content)
    assert batch[0].content == result.content
    assert payload["matched"] == payload["total"] == 94
    assert 20 <= payload["returned"] == len(payload["items"]) < 94
    assert payload["returned"] + payload["omitted"] == 94
    assert payload["truncated"] is True
    assert all(set(item) == {"id", "date", "bucket", "summary"} for item in payload["items"])
    assert all(item["summary"].endswith("。") and "第二" not in item["summary"] for item in payload["items"])
    assert [item["id"] for item in payload["items"]] == sorted((row["id"] for row in rows), reverse=True)[:payload["returned"]]
    assert sum(len(item.content) for item in batch) <= 14000
    assert all(len(item.content) >= 1100 for item in batch[1:])
    # The HTTP/core contract is still the full index, including all metadata.
    http, status = memory_core.index(store, None, {}, post_enclave=None)
    assert status == 200 and len(http["items"]) == 94
    assert "score" in http["items"][0] and "threads" in http["items"][0]
    if name == "memory_search":
        search_event = next(e for e in events if e["type"] == "memory.search.called")
        assert len(search_event["detail"]["ids"]) == 20
        assert search_event["detail"]["ids_omitted"] == 74
        assert "档案" not in json.dumps(search_event, ensure_ascii=False)


def test_full_5000_char_fetch_and_multiple_atomic_results_survive(garden):
    store, rows, _events = garden
    calls = [ToolCall(f"f{i}", "memory_fetch", {"ids": [rows[i]["id"]]}) for i in range(7)]
    results = asyncio.run(_dispatch(store, calls))
    sibling = ToolResult("generic", "s" * 2000)
    batch = tool_loop._normalize_tool_results([*results, sibling], per_result_cap=2000, batch_cap=8000)
    assert batch[-1].content == sibling.content
    for result in batch[:-1]:
        card = json.loads(result.content)["items"][0]
        assert card["content"] == "正文" * 2500
    assert sum(len(item.content) for item in batch) <= 8000 + 7 * 12000


def test_fetch_drops_whole_cards_not_body_prefixes(garden):
    store, rows, _events = garden
    result = memory.fetch(store, params={"ids": [row["id"] for row in rows[:8]]})
    payload = result.data
    assert result.ok and payload["truncated"]
    assert 1 <= payload["returned"] < 8
    assert payload["returned"] + payload["omitted"] == 8
    assert all(item["content"] == "正文" * 2500 for item in payload["items"])
    assert len(json.dumps(payload, ensure_ascii=False)) <= 12000


def test_oversize_card_is_explicitly_omitted_without_invalid_json():
    item = {"id": "large", "content": "\x00" * 5000}
    payload = memory_results.fetch_payload({"items": [item]})
    assert payload["items"] == [] and payload["omitted"] == 1 and payload["truncated"]
    assert json.loads(executor._summarize_capability_result({"ok": True, "data": payload}, tool_name="memory_fetch")) == payload


def test_actual_provider_receives_intact_index_and_recall_summary(garden, monkeypatch):
    store, rows, _events = garden
    sent = []
    summaries = []
    answers = [
        {"reply": "", "tool_calls": [
            {"id": "index", "name": "memory_index", "args": {}},
            {"id": "empty", "name": "memory_search", "args": {"query": "不存在的词"}},
            {"id": "fetch", "name": "memory_fetch", "args": {"ids": [rows[0]["id"]]}},
        ], "usage": {}},
        {"reply": "", "tool_calls": [{"id": "again", "name": "memory_index", "args": {}}], "usage": {}},
        {"reply": "查到了", "tool_calls": [], "usage": {}},
    ]

    async def provider(_config, messages, **_kwargs):
        sent.append(messages)
        return answers.pop(0)

    async def reply(*_args, **_kwargs):
        pass

    async def fold():
        return []

    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    asyncio.run(tool_loop.run_tool_loop(
        provider_config=provider_client.ProviderConfig(provider="anthropic", model="claude-sonnet-4-test", api_key="test"),
        build_messages=lambda transcript: [{"role": "user", "content": "请查档案"}, *transcript],
        dispatch_tools=lambda calls: _dispatch(store, calls), on_reply=reply,
        fold_new_messages=fold, add_usage=lambda *_args, **_kwargs: None,
        max_calls=3, on_memory_recall_completed=summaries.append,
    ))
    exchange = next(m for m in sent[1] if hasattr(m, "results"))
    indexed = json.loads(next(r.content for r in exchange.results if r.call_id == "index"))
    assert indexed["returned"] >= 20
    assert len(summaries) == 1
    assert summaries[0]["counts"] == {"injected": 0, "selected": None, "index_calls": 1,
                                      "search_calls": 1, "empty_searches": 1, "fetch_cards": 1}
    assert summaries[0]["unknown"] == ["selected", "quoted_memories"]
    assert "正文" not in json.dumps(summaries, ensure_ascii=False)


def test_trace_emits_zero_read_turn_and_survives_callback_failure(monkeypatch):
    from model_api_runtime.v2 import memory_recall
    events = []

    @memory_recall.traced
    async def zero(**kwargs):
        return SimpleNamespace(stop_reason="final_text")

    outcome = asyncio.run(zero(dispatch_tools=None, on_memory_recall_completed=events.append))
    assert outcome.stop_reason == "final_text" and events[0]["counts"]["index_calls"] == 0
    def broken(_detail):
        raise RuntimeError("not emitted")
    assert asyncio.run(zero(dispatch_tools=None, on_memory_recall_completed=broken)).stop_reason == "final_text"


def test_cancelled_read_emits_unknown_not_false_empty():
    from model_api_runtime.v2 import memory_recall
    events = []

    @memory_recall.traced
    async def cancelled(*, dispatch_tools):
        await dispatch_tools([ToolCall("s", "memory_search", {"query": "private"})])

    async def dispatch(_calls):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled(dispatch_tools=dispatch, on_memory_recall_completed=events.append))
    assert len(events) == 1
    assert events[0]["counts"]["search_calls"] == 1
    assert events[0]["counts"]["empty_searches"] is None
    assert "private" not in json.dumps(events)


def test_worker_bridge_emits_stable_coordinates_and_memory_subsystem(monkeypatch):
    from model_api_runtime.v2 import memory_recall, worker, serve_worker
    from diagnostics import diagnostics_core
    events = []
    monkeypatch.setattr(diagnostics_core, "emit_trace_event_payload", lambda _store, payload: events.append(payload["event"]))

    def sink(_user, event_type, **kwargs):
        serve_worker._emit_v2_debug_trace("store", event_type, **kwargs)

    callback = worker._memory_recall_callback(
        SimpleNamespace(emit_debug_trace=sink), "user",
        {"id": 17, "trace_id": "turn-1", "attempt_count": 2}, "chat",
    )

    @memory_recall.traced
    async def complete(**_kwargs):
        return SimpleNamespace(stop_reason="final_text")

    asyncio.run(complete(dispatch_tools=None, on_memory_recall_completed=callback))
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "memory.recall.completed"
    assert event["subsystem"] == "memory"
    assert (event["trace_id"], event["turn_id"], event["job_id"]) == ("turn-1", "turn-1", "17")
    assert event["detail"]["counts"]["index_calls"] == 0
    assert event["detail"]["counts"]["selected"] is None
    assert event["detail"]["attempt"] == 2
    assert "已选?" in event["summary"]
