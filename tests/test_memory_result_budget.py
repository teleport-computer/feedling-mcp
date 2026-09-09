"""T511: real memory core -> capability -> executor -> batch -> provider wire.

Only persistence, trace sink, and provider transport are replaced. No mocks of
our new projection, policy, metadata, dispatch, or normalization producers.
"""
from __future__ import annotations

import asyncio
import json
import time
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


def test_cues_flow_from_encrypted_inner_to_search_index_and_garden():
    from enclave import readside
    from memory import card_shape
    from model_api_runtime.v2 import extraction
    card = {"summary": "收好包裹。", "content": "正文不进索引", "threads": [],
            "bucket": "生活", "retrieval_cues": ["取件别名", "什么时候取包裹", "2026-09-01", "取件别名", {}]}
    inner = extraction._inner_from_card(card)
    assert inner["retrieval_cues"] == ["取件别名", "什么时候取包裹", "2026-09-01"]
    index = readside.build_memory_index_item({"id": "one"}, inner)
    assert "content" not in index and "_search_content" not in index
    search = readside.build_memory_search_item({"id": "one"}, inner)
    assert readside.memory_index_filter_items([search], {"query": "取件别名"}) == [search]
    assert "取件别名" in card_shape.to_garden_card(inner)["search_text"]
    wire = memory_results.index_payload({"items": [index]}, tool_name="memory_index")
    assert wire["items"][0]["retrieval_cues"] == inner["retrieval_cues"]
    assert "正文不进索引" not in json.dumps(wire, ensure_ascii=False)
    assert "retrieval_cues" not in extraction._inner_from_card({"summary": "old", "content": "old"})


def test_recall_cues_skip_non_strings_without_stringifying():
    from memory import recall_metadata
    raw = [{"k": "v"}, ["x"], 7, True, " 只留 字符串 "]
    assert recall_metadata.cues(raw) == ["只留 字符串"]


def test_extraction_cues_skip_non_strings_without_stringifying():
    from model_api_runtime.v2 import extraction
    raw = [{"k": "v"}, ["x"], 7, True, " 只留 字符串 "]
    assert extraction._inner_from_card({"retrieval_cues": raw})["retrieval_cues"] == ["只留 字符串"]


def test_garden_match_cues_skip_non_strings_without_stringifying():
    from memory import card_shape
    raw = [{"k": "v"}, ["x"], 7, True, " 只留 字符串 "]
    # Feed raw cues directly: a sanitizing writer must not hide a broken reader.
    card = {"summary": "原摘要", "retrieval_cues": raw}
    assert card_shape.text_for_match(card) == "原摘要 只留 字符串"
    assert card_shape.to_garden_card(card)["search_text"] == "原摘要 只留 字符串"


@pytest.mark.parametrize("status", ["archived", "superseded"])
@pytest.mark.parametrize("relation", ["thread", "anchor", "supersedes"])
def test_one_hop_retired_cards_require_explicit_superseded_link(status, relation):
    from memory import recall_metadata
    source = {"id": "source", "threads": ["t"]}
    if relation != "thread":
        key = "anchor_memory_ids" if relation == "anchor" else "supersedes"
        source[key] = ["retired"]
    # Exercise one_hop directly, without memory_available prefiltering archives.
    candidates = [{"id": "retired", "summary": "历史摘要", "status": status, "threads": ["t"]},
                  {"id": "current", "summary": "当前摘要", "status": "active", "threads": ["t"]}]
    result = recall_metadata.one_hop([source], candidates)
    expected = [{"id": "current", "summary": "当前摘要", "source_id": "source",
                 "relation": "thread", "status": "active"}]
    if status == "superseded" and relation != "thread":
        expected.insert(0, {"id": "retired", "summary": "历史摘要", "source_id": "source",
                            "relation": relation, "status": "superseded"})
    assert result == expected


def test_fetch_one_hop_actual_core_excludes_foreign_private_archived_and_transitive(garden):
    store, rows, _ = garden
    rows[:] = rows[:7]
    for row in rows:
        row["body"] = json.dumps({"summary": row["id"], "content": "full", "threads": [], "bucket": "生活"})
    source = json.loads(rows[0]["body"])
    source.update(threads=["parcel"], anchor_memory_ids=[rows[1]["id"]], supersedes=[rows[2]["id"]])
    rows[0]["body"] = json.dumps(source)
    rows[1]["body"] = json.dumps({"summary": "anchor", "content": "private full", "bucket": "生活", "threads": ["else"], "anchor_memory_ids": [rows[6]["id"]]})
    rows[2]["status"] = "superseded"
    rows[3]["owner_user_id"] = "somebody-else"
    rows[4]["visibility"] = "local_only"
    rows[5]["status"] = "archived"
    for row in rows[2:6]:
        row["body"] = json.dumps({"summary": "neighbor", "content": "never in neighbor", "bucket": "生活", "threads": ["parcel"]})
    result = memory.fetch(store, params={"ids": [rows[0]["id"]]})
    assert result.ok
    related = result.data["related_items"]
    assert {r["id"] for r in related} == {rows[1]["id"], rows[2]["id"]}
    assert {r["relation"] for r in related} == {"anchor", "supersedes"}
    assert {r["status"] for r in related} == {"active", "superseded"}
    assert all("content" not in r and r["source_id"] == rows[0]["id"] for r in related)
    assert result.data["returned"] == 1


def test_one_hop_explicit_link_wins_across_sources_and_cap_is_stable():
    from memory import recall_metadata
    sources = [{"id": "s1", "threads": ["t"]}, {"id": "s2", "anchor_memory_ids": ["z"]}]
    cards = [{"id": mid, "summary": mid, "threads": ["t"]} for mid in "abcdefghz"]
    picked = recall_metadata.one_hop(sources, cards)
    assert len(picked) == 6 and len({item["id"] for item in picked}) == 6
    assert picked[0] == {"id": "z", "summary": "z", "source_id": "s2", "relation": "anchor", "status": "active"}
    assert recall_metadata.one_hop(list(reversed(sources)), list(reversed(cards))) == picked


def test_recent_supplement_is_created_time_bounded_not_updated_time(monkeypatch):
    from datetime import datetime, timezone
    from memory import recall_metadata
    from enclave.routes import chat
    from model_api_runtime.v2 import memory_context
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    cards = [{"id": mid, "summary": mid, "created_at": created, "updated_at": "2026-09-08T00:00:00Z", "status": status}
             for mid, created, status in [("fresh", "2026-09-07T00:00:00Z", "active"),
                 ("old", "2026-08-01T00:00:00Z", "active"), ("retired", "2026-09-07T00:00:00Z", "superseded"),
                 ("future", "2026-09-09T00:00:00Z", "active"), ("unknown", "", "active")]]
    assert [c["id"] for c in recall_metadata.recent_cards(cards, now=now)] == ["fresh"]
    original = recall_metadata.recent_cards
    monkeypatch.setattr(recall_metadata, "recent_cards", lambda cards: original(cards, now=now))
    monkeypatch.setattr(chat.readside, "moments_to_cards", lambda *a: cards)
    monkeypatch.setattr(chat.memory_relevance, "select_context_memories_with_trace", lambda *a: ([], {"selected": []}))
    args = {"authorized_user_id": "u", "content_sk": None, "want_trace": True}
    assert chat._build_context_memories([], [], args)[0] == []
    picked, trace, log = chat._build_context_memories([], [], {**args, "context_recent": True})
    assert [c["id"] for c in picked] == ["fresh"]
    view = memory_context.render({"context_memories": picked, "context_memory_trace": trace, "context_memory_log": log})
    assert view["ids"] == ["fresh"] and "最近7天新卡" in view["block"]
    assert view["chars"] <= 2500 and "不代表与本题相关" in view["block"]
    covered = memory_context.render({"context_memories": picked}, profile="fresh")
    assert covered["ids"] == ["fresh"]   # profile-covered: id kept, summary dropped (T529)
    assert json.loads(covered["block"].splitlines()[2])["summary"] == ""


def test_related_optional_budget_never_evicts_primary(monkeypatch):
    item = {"id": "one", "content": "x" * 4000}
    base = memory_results.fetch_payload({"items": [item]})
    extended = memory_results.fetch_payload({"items": [item], "related_items": [
        {"id": str(i), "source_id": "one", "summary": "n" * 120} for i in range(6)], "related_status": "bounded"})
    assert extended["items"] == base["items"] == [item]
    assert len(json.dumps(extended, ensure_ascii=False)) <= result_budget.for_tool("memory_fetch").result_cap


def test_fetch_related_failure_is_unknown_and_primary_survives(garden, monkeypatch):
    import memory_readside_core
    store, rows, _ = garden
    original = memory_readside_core._memory_index_partition
    def fail(*args, **kwargs):
        raise RuntimeError("enclave unavailable")
    monkeypatch.setattr(memory_readside_core, "_memory_index_partition", fail)
    result = memory.fetch(store, params={"ids": [rows[0]["id"]]})
    assert result.ok and result.data["returned"] == 1
    assert result.data["related_status"] == "unavailable"
    assert result.data["related_items"] == []
    monkeypatch.setattr(memory_readside_core, "_memory_index_partition", original)


def test_index_keeps_bounded_thread_pointers_for_navigation():
    result = memory_results.index_payload({"items": [{"id": "one", "summary": "S", "threads": ["a", "b", "c", "d"]}]}, tool_name="memory_index")
    assert result["items"][0]["threads"] == ["a", "b", "c"]


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
    assert all(set(item) == {"id", "date", "bucket", "summary", "threads"} for item in payload["items"])
    assert all(item["threads"] == ["生活物品"] for item in payload["items"])
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
    measured = {r["call_id"]: r for r in summaries[0]["tool_results"]}
    assert measured["index"]["json_complete"] is True
    assert measured["index"]["returned"] == indexed["returned"]
    assert measured["index"]["omitted"] == indexed["omitted"]
    assert measured["fetch"]["requested_ids"] == [rows[0]["id"]]
    assert measured["fetch"]["returned_ids"] == [rows[0]["id"]]
    assert "again" not in measured  # cached discovery isn't a new dispatch


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
    async def cancelled(*, dispatch_tools, on_trajectory_event=None):
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


def _injection_payload():
    return {
        "context_memories": [
            {"id": "target", "summary": "灯牌编号 NP-4286", "content": "PRIVATE_BODY"},
            {"id": "quoted", "summary": "已经被引用"},
            {"id": "profile", "summary": "已有完整摘要"},
            {"id": "body_only", "content": "PRIVATE_BODY"},
        ],
        "context_memory_trace": {"selected": [
            {"id": "target", "score": 0.9, "bucket": "query", "matched_phrases": ["灯牌"]},
        ]},
        "context_memory_log": {"mode": "bucketed:unified"},
    }


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_turn_memory_block_is_untrusted_summary_only_deduped_and_bounded(role):
    from model_api_runtime.v2 import memory_context, worker
    view = {}
    builder = worker._make_build_messages_fn(
        system_prompt="SYS", summary="", agent_memory="已有完整摘要",
        tail=[{"role": "user", "content": "本轮问灯牌", "_quoted_memory_ids": ["quoted"]}],
        memory_context_payload=_injection_payload(), memory_context_observation=view,
        application_data_role=role,
    )
    messages = builder([])
    assert view["ids"] == ["target", "profile"] and view["selected"] == 4   # covered card keeps its id (T529)
    assert messages[1] == {"role": role, "content": view["block"]}
    assert messages[-1]["content"] == "本轮问灯牌"
    assert "PRIVATE_BODY" not in view["block"]
    assert '匹配「灯牌」' in view["block"] and "memory_fetch <id>" in view["block"]
    assert view["chars"] == len(view["block"]) <= memory_context.MAX_CHARS


def test_injection_budget_drops_whole_low_ranked_cards():
    from model_api_runtime.v2 import memory_context
    payload = {"context_memories": [
        {"id": f"m{i:02}", "summary": "x" * 119 + "\x00" * 100} for i in range(40)
    ], "context_memory_trace": {"selected": [
        {"id": f"m{i:02}", "score": 40 - i} for i in range(40)
    ]}, "context_memory_log": {"mode": "default"}}
    view = memory_context.render(payload)
    entries = [json.loads(line) for line in view["block"].splitlines()[2:-1]]
    assert 0 < len(entries) < 40
    assert len(view["block"]) <= 2500
    assert [e["id"] for e in entries] == view["ids"] == [f"m{i:02}" for i in range(len(entries))]
    assert all("…" not in e["summary"] and len(e["summary"]) <= memory_context.SUMMARY_MAX_CHARS for e in entries)
    assert memory_context.render({})["selected"] is None
    assert memory_context.render({"context_memories": [], "context_memory_log": {"mode": "failed"}})["selected"] is None


@pytest.mark.parametrize("drop_at_boundary", [False, True])
def test_recall_measures_final_provider_request_not_selection(monkeypatch, drop_at_boundary):
    from model_api_runtime.v2 import worker
    view, observed, sent, trajectories = {}, [], [], []
    builder = worker._make_build_messages_fn(
        system_prompt="SYS", summary="", tail=[{"role": "user", "content": "灯牌编号?"}],
        memory_context_payload=_injection_payload(), memory_context_observation=view,
        agent_memory="已有完整摘要",
    )
    def build(transcript):
        messages = builder(transcript)
        return [m for m in messages if m.get("content") != view["block"]] if drop_at_boundary else messages
    async def provider(_config, messages, **kwargs):
        sent.append(messages)
        return {"reply": "不猜具体值", "tool_calls": [], "usage": {}}
    async def noop(*args, **kwargs):
        return []
    async def trajectory(kind, payload):
        trajectories.append((kind, payload))
    async def reply(*args, **kwargs):
        return None
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    asyncio.run(tool_loop.run_tool_loop(
        provider_config=provider_client.ProviderConfig(provider="anthropic", model="claude-sonnet-4-test", api_key="test"),
        build_messages=build, dispatch_tools=noop, on_reply=reply,
        fold_new_messages=noop, add_usage=lambda *a, **k: None, max_calls=1,
        memory_context_observation=view, on_memory_recall_completed=observed.append,
        on_trajectory_event=trajectory,
    ))
    assert len(sent) == 1 and len(observed) == 1
    expected = [] if drop_at_boundary else ["profile", "quoted", "target"]
    assert observed[0]["injected_ids"] == expected
    assert observed[0]["counts"]["injected"] == len(expected)
    assert observed[0]["profile_used"] is True
    assert observed[0]["injected_chars"] == (0 if drop_at_boundary else len(view["block"]))
    request = next(p for k, p in trajectories if k == "provider_request")
    assert request["memory_context"]["injected_ids"] == ([] if drop_at_boundary else view["ids"])
    assert "NP-4286" not in json.dumps(observed, ensure_ascii=False)


def test_enclave_selection_query_is_last_four_conversation_messages():
    from enclave.routes import chat
    from memgarden import observability
    rows = [{"role": "user", "content": "old-secret"},
            {"role": "user", "content": "露营灯"},
            {"role": "assistant", "content": "你问的是保修卡吗"},
            {"role": "user", "content": "对"},
            {"role": "assistant", "content": "需要编号"},
            {"role": "tool", "content": "tool-secret"}]
    _, _, log = chat._build_context_memories([], rows, {
        "context_mode": "", "want_trace": True, "authorized_user_id": "u", "content_sk": None,
    })
    assert log["query_fingerprint"] == observability.query_fingerprint("露营灯\n你问的是保修卡吗\n对\n需要编号")
    assert "old-secret" not in json.dumps(log) and "tool-secret" not in json.dumps(log)


def test_production_selection_reader_is_authenticated_and_frontier_bounded(monkeypatch):
    from model_api_runtime.v2 import serve_worker
    captured = {}
    class Response:
        status_code = 200
        def json(self):
            return {"user_id": "u", **_injection_payload()}
    class Client:
        def get(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return Response()
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.invalid")
    monkeypatch.setattr(serve_worker.core_enclave, "_client", lambda: Client())
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda uid: "test-token:" + uid)
    payload = serve_worker._read_context_memories("u", through_seq=47)
    assert payload == _injection_payload()
    assert captured["params"] == {"before_seq": 48, "limit": 4, "include_image_body": "0", "context_trace": "1", "context_recent": "1"}
    assert captured["headers"] == {"X-Feedling-Runtime-Token": "test-token:u"}
    with pytest.raises(ValueError, match="frontier"):
        serve_worker._read_context_memories("u", through_seq=0)
    with pytest.raises(RuntimeError, match="user_mismatch"):
        serve_worker._read_context_memories("other", through_seq=47)


def test_v2_fact_discipline_retains_past_memory_and_forbids_invention():
    from model_api_runtime.v2 import context
    assert "不要顺着话头猜一个像样的值" in context.chat_system_prompt(None)
    assert "没有这些支撑就先去查" in context.CHAT_SYSTEM_PROMPT
    assert "记忆可能停在过去" in context.AGENT_MEMORY_HEADER
    assert "先查再答" in context.AGENT_MEMORY_HEADER


def test_flat_recall_events_survive_the_real_durable_detail_sanitizer():
    import debug_trace
    from model_api_runtime.v2 import worker
    saved = []
    def sink(_uid, kind, **kwargs):
        saved.append((kind, debug_trace._safe_detail(kwargs["detail"])))
    ids = [f"{i:032x}" for i in range(8)]
    callback = worker._memory_recall_callback(SimpleNamespace(emit_debug_trace=sink), "u",
        {"id": 23, "trace_id": "trace23", "attempt_count": 1}, "chat")
    asyncio.run(callback({"counts": {"injected": 8, "selected": 8},
        "tool_results": [{"call_id": "fetch23", "tool": "memory_fetch", "returned": 8,
                          "omitted": 0, "json_complete": True, "result_chars": 1200,
                          "requested_ids": ids, "returned_ids": ids}],
        "prompt_observations": [{"round": 1, "injected_ids": ids, "injected_chars": 1000,
                                 "profile_used": False, "block_header": "# 相关记忆"}],
        "injected_ids": ids, "injected_chars": 1000, "profile_used": False}))
    rows = dict(saved)
    assert rows["memory.recall.tool_result"]["returned_ids"] == ids
    assert rows["memory.recall.tool_result"]["requested_ids"] == ids
    assert rows["memory.recall.tool_result"]["call_id"] == "fetch23"
    assert rows["memory.context.applied"]["injected_ids"] == ids
    assert rows["memory.recall.completed"]["tool_result_events"] == 1
    assert rows["memory.recall.completed"]["attempt"] == 1


@pytest.mark.parametrize("shape", ["reflow", "parts", "parts_mid_token", "compact_json",
                                   "partial", "id_only", "altered_summary", "system_only"])
def test_recall_arrival_survives_adapter_reflow_and_parts_but_not_missing_lines(monkeypatch, shape):
    """haoxuan (T529): whole-block equality read 'never arrived' the moment an
    adapter re-flowed whitespace or split content into parts; arrival is now
    proven per rendered card line, whitespace-normalized, on string or parts
    content. A line that is really gone is still reported missing, and an id
    without its line does not count."""
    from model_api_runtime.v2 import worker
    view, observed, sent = {}, [], []
    builder = worker._make_build_messages_fn(
        system_prompt="SYS", summary="", tail=[{"role": "user", "content": "灯牌编号?"}],
        memory_context_payload=_injection_payload(), memory_context_observation=view,
        agent_memory="已有完整摘要",
    )
    def mutate(message):
        if message.get("content") != view["block"]:
            return message
        lines = view["block"].splitlines()
        if shape == "reflow":
            return {**message, "content": "  ".join(ln.strip() for ln in lines) + "  "}
        if shape == "parts":
            return {**message, "content": [{"type": "text", "text": ln} for ln in lines]}
        if shape == "parts_mid_token":
            # an adapter may split anywhere, including inside "NP-4286"
            whole = view["block"]
            cut = whole.index("NP-") + 3
            return {**message, "content": [{"type": "text", "text": whole[:cut]},
                                           {"type": "text", "text": whole[cut:]}]}
        if shape == "compact_json":
            out = []
            for ln in lines:
                stripped = ln.strip()
                if stripped.startswith("{"):
                    row = json.loads(stripped)
                    out.append(json.dumps({k: row[k] for k in reversed(list(row))},
                                          ensure_ascii=False, separators=(",", ":")))
                else:
                    out.append(ln)
            return {**message, "content": "\n".join(out)}
        if shape == "altered_summary":
            out = []
            for ln in lines:
                stripped = ln.strip()
                if stripped.startswith("{") and '"id": "target"' in stripped:
                    row = json.loads(stripped)
                    row["summary"] = row["summary"].replace("NP-4286", "NP-9999")
                    out.append(json.dumps(row, ensure_ascii=False))
                else:
                    out.append(ln)
            return {**message, "content": "\n".join(out)}
        if shape == "system_only":
            return {**message, "role": "system"}
        if shape == "partial":
            keep = [ln for ln in lines if '"id": "target"' not in ln]
            return {**message, "content": "\n".join(keep)}
        if shape == "id_only":
            return {**message, "content": "ids: " + ",".join(view["ids"])}
        raise AssertionError(shape)
    def build(transcript):
        return [mutate(m) for m in builder(transcript)]
    async def provider(_config, messages, **kwargs):
        sent.append(messages)
        return {"reply": "不猜具体值", "tool_calls": [], "usage": {}}
    async def noop(*args, **kwargs):
        return []
    async def reply(*args, **kwargs):
        return None
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    asyncio.run(tool_loop.run_tool_loop(
        provider_config=provider_client.ProviderConfig(provider="anthropic", model="claude-sonnet-4-test", api_key="test"),
        build_messages=build, dispatch_tools=noop, on_reply=reply,
        fold_new_messages=noop, add_usage=lambda *a, **k: None, max_calls=1,
        memory_context_observation=view, on_memory_recall_completed=observed.append,
    ))
    assert len(sent) == 1 and len(observed) == 1
    every = ["profile", "quoted", "target"]
    expected = {"reflow": every, "parts": every, "parts_mid_token": every,
                "compact_json": every, "partial": ["profile", "quoted"],
                "id_only": [], "altered_summary": ["profile", "quoted"],
                "system_only": []}[shape]
    assert observed[0]["injected_ids"] == expected
    assert observed[0]["counts"]["injected"] == len(expected)
    if shape in {"reflow", "parts", "parts_mid_token", "compact_json"}:
        assert observed[0]["injected_chars"] == len(view["block"])
    if shape in {"partial", "altered_summary"}:
        assert 0 < observed[0]["injected_chars"] < len(view["block"])
        assert observed[0]["prompt_observations"][0]["missing_ids"] == ["target"]
        assert observed[0]["injected_is_lower_bound"] is False
    if shape in {"id_only", "system_only"}:
        assert observed[0]["injected_chars"] == 0
    assert "NP-4286" not in json.dumps(observed, ensure_ascii=False)


def test_render_never_cuts_a_summary_and_keeps_ids_covered_by_profile():
    """haoxuan (T529): a cut summary is the shape that invites completion, and
    dropping a profile-covered card whole hides its id so the body can never be
    fetched. Over the limit: id + reason only. Covered: id kept, summary empty."""
    from model_api_runtime.v2 import memory_context
    long = "他一开始想辞职，后来跟他妈聊完就打消了。" * 20   # > SUMMARY_MAX_CHARS
    payload = {"context_memories": [
        {"id": "long", "summary": long},
        {"id": "mom", "summary": "他妈妈住在杭州", "content": "上个月摔了一跤，他每周末回去看一次"},
        {"id": "short", "summary": "旅行杯盖维修单号 LK-7319"},
    ], "context_memory_trace": {"selected": [
        {"id": "long", "score": 0.9, "bucket": "query", "matched_phrases": ["辞职"]},
        {"id": "mom", "score": 0.8, "bucket": "query"},
        {"id": "short", "score": 0.7, "bucket": "recent"},
    ]}, "context_memory_log": {"mode": "default"}}
    view = memory_context.render(payload, profile="档案：他妈妈住在杭州，喜欢散步。")
    entries = {json.loads(line)["id"]: json.loads(line) for line in view["block"].splitlines()[2:-1]}
    assert view["ids"] == ["long", "mom", "short"]
    assert entries["long"]["summary"] == "" and "fetch" in entries["long"]["reason"] and "…" not in view["block"]
    assert "他一开始想辞职" not in view["block"]          # whole or nothing: no half sentence
    assert entries["mom"]["summary"] == "" and entries["mom"]["reason"] == memory_context.PROFILE_COVERED_REASON
    assert "上个月摔了一跤" not in view["block"]           # body still never leaks into the block
    assert entries["short"]["summary"] == "旅行杯盖维修单号 LK-7319"


def test_arrival_scan_is_bounded_and_never_aborts_the_turn():
    """codex (T529 round 3): this accounting runs *before* the provider request,
    so hostile-looking text must not make it slow or raise. A brace flood used to
    rescan to the end from every position (quadratic); 1100-level nesting used to
    raise RecursionError out of json.loads and abort the turn."""
    from model_api_runtime.v2 import memory_recall
    entry = {"id": "target", "summary": "灯牌编号 NP-4286", "reason": "与这句相关"}
    block = "\n".join(["# 相关记忆", "以下是记忆资料", json.dumps(entry, ensure_ascii=False),
                       "需要细节用 memory_fetch <id>"])
    started = time.time()
    assert memory_recall.arrival_ids(block, ["target"], [{"role": "user", "content": "{" * 8000}]) == ([], [], ["target"])
    assert time.time() - started < 1.0            # was ~2.5s at 8k unmatched braces
    deep = "{" * 1100 + "}" * 1100                # was RecursionError
    compact = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    assert memory_recall.arrival_ids(
        block, ["target"], [{"role": "user", "content": deep + "\n" + compact}]) == (["target"], [], [])


def test_arrival_matches_any_equal_object_with_the_same_id():
    """codex (T529 round 3): a stale copy of the same id earlier in the text must
    not shadow the correct entry that follows."""
    from model_api_runtime.v2 import memory_recall
    entry = {"id": "target", "summary": "灯牌编号 NP-4286", "reason": "与这句相关"}
    block = "\n".join(["# 相关记忆", "以下是记忆资料", json.dumps(entry, ensure_ascii=False), "尾"])
    stale = json.dumps({**entry, "summary": "旧的摘要"}, ensure_ascii=False)
    correct = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    assert memory_recall.arrival_ids(
        block, ["target"], [{"role": "user", "content": stale + "\n" + correct}]) == (["target"], [], [])
    assert memory_recall.arrival_ids(
        block, ["target"], [{"role": "user", "content": stale}]) == ([], ["target"], [])


def test_arrival_failure_degrades_the_observation_not_the_turn(monkeypatch):
    """An accounting bug must show up as arrival_error, never as a failed turn."""
    from model_api_runtime.v2 import worker
    view, observed, sent = {}, [], []
    builder = worker._make_build_messages_fn(
        system_prompt="SYS", summary="", tail=[{"role": "user", "content": "灯牌编号?"}],
        memory_context_payload=_injection_payload(), memory_context_observation=view,
        agent_memory="已有完整摘要",
    )
    def boom(*args, **kwargs):
        raise RecursionError("simulated")
    monkeypatch.setattr("model_api_runtime.v2.memory_recall.arrival_ids", boom)
    async def provider(_config, messages, **kwargs):
        sent.append(messages)
        return {"reply": "照常回复", "tool_calls": [], "usage": {}}
    async def noop(*args, **kwargs):
        return []
    async def reply(*args, **kwargs):
        return None
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    asyncio.run(tool_loop.run_tool_loop(
        provider_config=provider_client.ProviderConfig(provider="anthropic", model="claude-sonnet-4-test", api_key="test"),
        build_messages=builder, dispatch_tools=noop, on_reply=reply,
        fold_new_messages=noop, add_usage=lambda *a, **k: None, max_calls=1,
        memory_context_observation=view, on_memory_recall_completed=observed.append,
    ))
    assert len(sent) == 1 and len(observed) == 1          # the turn still ran
    obs = observed[0]["prompt_observations"][0]
    assert obs["arrival_error"] == "RecursionError"
    assert obs["injected_ids"] == [] and obs["missing_ids"] == []
    assert obs["unverified_ids"] == view["ids"] and obs["injected_is_lower_bound"] is True
    assert observed[0]["unverified_ids"] == sorted(view["ids"])
    assert observed[0]["injected_is_lower_bound"] is True
    assert observed[0]["arrival_errors"] == ["RecursionError"]


def test_arrival_scan_bounds_are_explicit(monkeypatch):
    """The linear stack scan is what makes a brace flood cheap; these caps are the
    belt-and-braces on top, so pin them: past a bound an entry is simply not
    structurally matched (the verbatim path still applies), never an exception."""
    from model_api_runtime.v2 import memory_recall
    entry = {"id": "target", "summary": "灯牌编号 NP-4286", "reason": "与这句相关"}
    block = "\n".join(["# 相关记忆", "以下是记忆资料", json.dumps(entry, ensure_ascii=False), "尾"])
    compact = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    # Past a bound the answer is "not verified", never "absent" (codex round 5).
    beyond = {"role": "user", "content": "x" * memory_recall._MAX_JSON_SCAN_CHARS + compact}
    assert memory_recall.arrival_ids(block, ["target"], [beyond]) == ([], [], ["target"])
    nested = {"role": "user", "content": "{" * memory_recall._MAX_JSON_DEPTH + compact}
    assert memory_recall.arrival_ids(block, ["target"], [nested]) == ([], [], ["target"])
    monkeypatch.setattr(memory_recall, "_MAX_JSON_SPAN_CHARS", 4)
    assert memory_recall.arrival_ids(block, ["target"], [{"role": "user", "content": compact}]) == ([], [], ["target"])


def test_a_quote_in_ordinary_prose_does_not_hide_a_following_entry():
    """codex (T529 round 5): quote state belongs to a JSON candidate, not to the
    surrounding prose — a lone quotation mark used to swallow the entry after it."""
    from model_api_runtime.v2 import memory_recall
    entry = {"id": "target", "summary": "灯牌编号 NP-4286", "reason": "与这句相关"}
    block = "\n".join(["# 相关记忆", "以下是记忆资料", json.dumps(entry, ensure_ascii=False), "尾"])
    compact = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    # An ODD number of quotes is what poisons a globally-tracked scanner; a
    # balanced pair cancels out and would not discriminate (checked against the
    # mutant before writing this).
    lone = {"role": "user", "content": '屏幕是 27" 的 ' + compact}
    assert memory_recall.arrival_ids(block, ["target"], [lone]) == (["target"], [], [])
    unclosed = {"role": "user", "content": '他说"好啊 ' + compact}
    assert memory_recall.arrival_ids(block, ["target"], [unclosed]) == (["target"], [], [])
    # a real absence is still a real absence
    assert memory_recall.arrival_ids(
        block, ["target"], [{"role": "user", "content": '屏幕是 27" 的'}]) == ([], ["target"], [])


def test_text_ending_inside_an_object_or_string_is_unverified_not_missing():
    """codex (T529 round 7): an unclosed object or string swallows everything
    after it, so the scan proves nothing about what it did not see."""
    from model_api_runtime.v2 import memory_recall
    entry = {"id": "target", "summary": "灯牌编号 NP-4286", "reason": "与这句相关"}
    block = "\n".join(["# 相关记忆", "以下是记忆资料", json.dumps(entry, ensure_ascii=False), "尾"])
    compact = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    swallowed = {"role": "user", "content": '{"draft":"' + compact}
    assert memory_recall.arrival_ids(block, ["target"], [swallowed]) == ([], [], ["target"])
    # an object that closes normally around the entry still verifies it
    nested = {"role": "user", "content": '{"draft": ' + compact + "}"}
    assert memory_recall.arrival_ids(block, ["target"], [nested]) == (["target"], [], [])
    # and a complete scan that simply does not contain the entry is still missing
    assert memory_recall.arrival_ids(
        block, ["target"], [{"role": "user", "content": "没有记忆块"}]) == ([], ["target"], [])
