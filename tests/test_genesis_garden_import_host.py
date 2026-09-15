"""托管侧接导入引擎的那一层（``genesis/import_engine.py``）+ 旧上传入口的记忆卡一步。

  · 写卡指令 → io 明文 memory action：来源、supersede、检索线索、日期规整
  · 检索线索真的进了卡的加密正文（``_memory_inner_from_action``），没给时正文与之前一致
  · 执行器行为：卡本身不合格只丢这一张；supersede 目标不在了改成新增；写库坏了整段抛
  · 读已有卡失败时降级为空索引（并留轨迹），不让导入失败
  · 旧 ``/v1/history_import/upload`` 的记忆卡走同一个引擎，分层张数上限生效

全部是合成材料，不含真实用户数据。
"""
from __future__ import annotations

import json
import sys
import types
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from genesis import import_engine  # noqa: E402
from hosted import history_import as hi  # noqa: E402
from memory import actions as memory_actions  # noqa: E402
from memory import garden_import  # noqa: E402


def _mutation(summary: str, *, op: str = "add", target: str = "", **card) -> dict:
    out = {"op": op, "card": {"summary": summary, "content": f"{summary}，材料里说过。",
                              "bucket": "爱好", "threads": ["周末"], "type": "event",
                              "importance": 0.8, "pulse": 0.4, **card}}
    if op == "supersede":
        out["target_id"] = target
    return out


def test_memory_action_maps_card_fields_source_and_supersede():
    add = import_engine.memory_action(_mutation(
        "第一次跑完半马", occurred_at="2024-05-20", retrieval_cues=["半马", "马拉松"]))
    assert add["type"] == "memory.add"
    assert add["capture_mode"] == "genesis_import"
    mem = add["memory"]
    assert (mem["summary"], mem["type"], mem["bucket"], mem["threads"]) == (
        "第一次跑完半马", "event", "爱好", ["周末"])
    assert mem["occurred_at"] == "2024-05-20"
    assert mem["source"] == "genesis_import"
    assert mem["retrieval_cues"] == ["半马", "马拉松"]
    assert mem["importance"] == 0.8

    sup = import_engine.memory_action(_mutation("半马成绩两小时", op="supersede", target="mom_1"),
                                      source="history_import")
    assert sup["type"] == "memory.supersede" and sup["supersedes"] == "mom_1"
    assert sup["memory"]["source"] == "history_import"
    assert import_engine.memory_action({"op": "add", "card": {"summary": ""}}) is None


def test_retrieval_cues_reach_the_sealed_body_only_when_given():
    with_cues = memory_actions._memory_inner_from_action(
        {"summary": "s", "content": "c", "bucket": "爱好", "retrieval_cues": ["半马", " 半马 ", "x" * 200]})
    assert with_cues["retrieval_cues"] == ["半马", "x" * 120]
    without = memory_actions._memory_inner_from_action({"summary": "s", "content": "c", "bucket": "爱好"})
    assert set(without) == {"summary", "content", "bucket", "threads"}


def _executor(rows_by_call: list[list[dict]], seen: list[list[dict]]):
    def execute(_store, _api_key, actions, *, runtime_token=""):
        seen.append(actions)
        return {"results": rows_by_call[len(seen) - 1]}, 200
    return execute


def _ok(mid: str) -> dict:
    return {"status": "ok", "http_status": 201, "memory": {"id": mid}}


def _err(error: str, status: int = 400) -> dict:
    return {"status": "error", "error": error, "http_status": status}


def test_store_writer_drops_bad_cards_and_turns_stale_supersede_into_add():
    seen: list[list[dict]] = []
    write = import_engine.store_writer(object(), "k", execute=_executor([
        [_ok("mom_1"), _err("memory_card_polluted"), _err("supersede_targets_unavailable", 409)],
        [_ok("mom_3")],
    ], seen))
    ids = write([_mutation("一"), _mutation("二"), _mutation("三", op="supersede", target="gone")], "k1")
    assert ids == ["mom_1", "", "mom_3"]
    assert seen[1][0]["type"] == "memory.add" and "supersedes" not in seen[1][0]


def test_store_writer_raises_when_writing_itself_is_broken():
    write = import_engine.store_writer(object(), "k", execute=_executor(
        [[_err("enclave_unavailable", 409), _err("enclave_unavailable", 409)]], []))
    with pytest.raises(RuntimeError, match="memory_actions_failed:enclave_unavailable"):
        write([_mutation("一"), _mutation("二")], "k1")


def test_store_writer_partial_hard_failure_keeps_written_ids():
    # 一张写进去了、另一张是存储错误：已写的 id 必须交回（否则会话登记不到、续跑重复写）
    write = import_engine.store_writer(object(), "k", execute=_executor(
        [[_ok("mom_1"), _err("envelope_failed", 409)]], []))
    assert write([_mutation("一"), _mutation("二")], "k1") == ["mom_1", ""]


def test_existing_cards_degrades_to_empty_index_with_trace(monkeypatch):
    import memory_readside_core

    events = []
    monkeypatch.setattr(memory_readside_core, "memory_index_core",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("enclave_unavailable")))
    monkeypatch.setattr(import_engine.debug_trace, "trace_event",
                        lambda *_a, **kw: events.append(kw))
    assert import_engine.existing_cards(object(), "k", job_id="j") == []
    assert events[0]["type"] == "genesis.garden_import.index_unavailable"
    assert "enclave" not in json.dumps(events[0]["detail"])  # 只记异常类名，不记内容


def test_existing_cards_keeps_only_visible_cards(monkeypatch):
    import memory_readside_core

    monkeypatch.setattr(memory_readside_core, "memory_index_core", lambda *_a, **_k: {"items": [
        {"id": "a", "summary": "可见", "status": "active", "bucket": "b"},
        {"id": "b", "summary": "归档了", "status": "archived"},
        {"id": "", "summary": "没 id"},
    ]})
    assert [c["id"] for c in import_engine.existing_cards(object(), "k")] == ["a"]


def test_legacy_upload_memory_step_uses_engine_with_tier_cap(monkeypatch):
    monkeypatch.setenv(garden_import.STRATEGY_ENV, "single_pass")
    prompts: list[str] = []

    class FakeLLM:
        def __init__(self, *_a, **_k):
            pass

        def complete(self, **kwargs):
            prompts.append(kwargs["messages"][0]["content"])
            cards = [{"action": "add", "type": "fact", "bucket": "爱好", "threads": [],
                      "summary": f"第{i}件具体的事情", "content": f"第{i}件具体的事情，材料里说过。",
                      "importance": 0.5, "pulse": 0.3} for i in range(1, 4)]
            return types.SimpleNamespace(text=json.dumps({"cards": cards}, ensure_ascii=False),
                                         stop_reason="stop")

    import genesis.llm_client as llm_client

    monkeypatch.setattr(llm_client, "GenesisLLMClient", FakeLLM)
    written: list[dict] = []
    monkeypatch.setattr(import_engine, "existing_cards", lambda *_a, **_k: [])
    monkeypatch.setattr(import_engine, "store_writer", lambda _s, _k, *, source="", **_kw: (
        lambda muts, _key: [written.append({**m, "source": source}) or f"mom_{len(written)}" for m in muts]))
    monkeypatch.setattr(hi, "_update_history_job_phase", lambda *_a, **_k: None)
    messages = [{"role": "user", "content": f"聊天第{i}句，内容足够长的一段话", "ts": 1_740_000_000 + i,
                 "source": "history_import"} for i in range(6)]
    run = hi._GardenMemoryImport(
        types.SimpleNamespace(user_id="usr_x"), "k", {"job_id": "hi_1"}, object(),
        analysis_messages=messages, fresh_start=False, window_limit=8, initial_windows=8,
        background=False, relationship_start=date(2025, 1, 1), language="zh-Hans",
        user_name="TA", max_cards=2)
    out = run.run("initial")
    assert out == {"written": 2, "dropped": 0}, "分层配额（张数上限）必须生效"
    assert {w["source"] for w in written} == {"history_import"}
    assert len(prompts) == 1
    assert [c["summary"] for c in run.cards()] == ["第1件具体的事情", "第2件具体的事情"]
    assert run.run("background") == {"written": 0, "dropped": 0}


def test_legacy_upload_fresh_start_asks_no_model(monkeypatch):
    monkeypatch.setattr(import_engine, "existing_cards",
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no index read")))
    run = hi._GardenMemoryImport(
        types.SimpleNamespace(user_id="usr_x"), "k", {"job_id": "hi_2"}, object(),
        analysis_messages=[{"role": "user", "content": "Fresh start.", "source": "fresh_start"}],
        fresh_start=True, window_limit=8, initial_windows=8, background=False,
        relationship_start=date(2025, 1, 1), language="zh-Hans", user_name="TA", max_cards=12)
    assert run.run("initial") == {"written": 0, "dropped": 0}
    assert run.windows_total == 0


def _apply_env(monkeypatch) -> dict:
    from genesis import service

    calls: dict = {"attempts": [], "completed": None, "notices": []}

    class Attempt:
        def __init__(self, _store, _job_id, artifact):
            self.artifact = artifact

        def __enter__(self):
            return self

        def finish(self, outcome):
            calls["attempts"].append((self.artifact, outcome))

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(service.distillation_ledger, "ArtifactAttempt", Attempt)
    monkeypatch.setattr(service.db, "genesis_get_job", lambda *_a: {"job_id": "job_1", "status": "processing"})
    monkeypatch.setattr(service.db, "genesis_set_job_status", lambda *_a, **_k: None)
    monkeypatch.setattr(service.db, "genesis_upsert_output", lambda *_a, **_k: None)
    monkeypatch.setattr(service.db, "genesis_complete_job",
                        lambda *_a, **kw: calls.__setitem__("completed", kw) or None)
    monkeypatch.setattr(service, "write_genesis_state", lambda *_a, **_k: None)
    monkeypatch.setattr(service.notices, "resolve", lambda *_a, **_k: None)
    monkeypatch.setattr(service.notices, "emit", lambda *_a, **kw: calls["notices"].append(kw))
    monkeypatch.setattr(service, "apply_memory_outputs",
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("cards already written")))
    monkeypatch.setattr(service, "init_identity_if_absent", lambda *_a: "initialized")
    monkeypatch.setattr(service, "write_persona_artifact", lambda *_a, **_k: ("", ""))
    monkeypatch.setattr(service, "write_voice_artifact", lambda *_a, **_k: ("", ""))
    monkeypatch.setattr(service, "write_profile_artifact", lambda *_a, **_k: ("", "", "skipped"))
    return calls


def test_apply_route_counts_engine_written_cards_without_writing_again(monkeypatch):
    from genesis import service

    calls = _apply_env(monkeypatch)
    result = service.apply_reducer_output(
        types.SimpleNamespace(user_id="usr_x"), None, "job_1",
        {"memories": [], "source_family": "history",
         "garden_import": {"cards_written": 5, "dropped": 1}})
    assert result["memory_action_count"] == 5
    assert calls["completed"]["memory_action_count"] == 5
    assert [a for a in calls["attempts"] if a[0] == "memory"] == []  # 台账在 worker 那边记过了
    assert calls["notices"][0]["error_class"] == "genesis_partial"


def test_apply_route_rejects_engine_output_that_still_carries_cards(monkeypatch):
    from genesis import service

    _apply_env(monkeypatch)
    with pytest.raises(ValueError, match="must_not_carry_memories"):
        service.apply_reducer_output(
            types.SimpleNamespace(user_id="usr_x"), None, "job_1",
            {"memories": [{"summary": "x"}], "garden_import": {"cards_written": 1}})
