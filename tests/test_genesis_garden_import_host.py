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


# --------------------------------------------------------------------------- #
# 旧上传入口 _process_history_import_sync：切换前的保护，在新引擎上恢复
# --------------------------------------------------------------------------- #

def _upload_env(monkeypatch, *, reply=None, rows=None, user_name: str = "TA") -> dict:
    """真 ``_process_history_import_sync`` + 真引擎 + 真 memory action 映射；替身：模型、
    执行器（落库）、已有卡读取、身份推导/问候（provider）、job 进度写库、台账。"""
    import distillation_ledger
    import genesis.llm_client as llm_client

    monkeypatch.setenv(garden_import.STRATEGY_ENV, "single_pass")
    calls: dict = {"actions": [], "prompts": [], "identity": [], "phases": []}

    class FakeLLM:
        def __init__(self, *_a, **_k):
            pass

        def complete(self, **kwargs):
            prompt = kwargs["messages"][0]["content"]
            calls["prompts"].append(prompt)
            if isinstance(reply, Exception):
                raise reply
            return types.SimpleNamespace(text=reply, stop_reason="stop")

    def execute(_store, _api_key, actions, *, runtime_token=""):
        start = len(calls["actions"])
        calls["actions"].extend(actions)
        out = rows(actions) if rows else [
            _ok(f"mom_{start + i + 1}") for i in range(len(actions))]
        return {"results": out}, 200

    class Attempt:
        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            return self

        def finish(self, _outcome):
            pass

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(llm_client, "GenesisLLMClient", FakeLLM)
    monkeypatch.setattr(memory_actions, "_execute_memory_actions", execute)
    monkeypatch.setattr(import_engine, "existing_cards", lambda *_a, **_k: [])
    monkeypatch.setattr(distillation_ledger, "history_attempt", Attempt)
    monkeypatch.setattr(hi, "_update_history_job_phase",
                        lambda _s, job, phase, **kw: calls["phases"].append(phase) or job)
    monkeypatch.setattr(hi.hosted_config_store, "_load_runtime_provider_config",
                        lambda *_a: types.SimpleNamespace(provider="p", model="m", base_url=""))
    monkeypatch.setattr(hi, "_resolve_import_user_name", lambda *_a: (user_name, []))
    monkeypatch.setattr(hi, "_import_language_for_store", lambda *_a: "zh-Hans")
    monkeypatch.setattr(hi, "_derive_identity_with_provider",
                        lambda _rt, _msgs, cards, *_a: calls["identity"].append(cards) or (
                            {"agent_name": "小满", "dimensions": []}, []))
    monkeypatch.setattr(hi, "_store_identity_payload", lambda *_a, **_k: {"id": "idn"})
    monkeypatch.setattr(hi, "_generate_model_api_onboarding_greeting", lambda *_a: ("你好", []))
    monkeypatch.setattr(hi, "_append_model_api_onboarding_greeting", lambda *_a: {"id": "msg_1"})
    monkeypatch.setattr(hi.notices, "resolve", lambda *_a, **_k: None)
    return calls


_CHAT = ("[2025-03-01 10:00] 小雨: 我每周六早上都去西湖边骑车，已经坚持三年了。\n"
         "[2025-03-01 10:01] 小满: 好厉害，下次带上我。\n")


def _cards_reply(*cards: dict) -> str:
    return json.dumps({"cards": [
        {"action": "add", "type": "fact", "bucket": "爱好", "threads": [],
         "importance": 0.6, "pulse": 0.3, **c} for c in cards]}, ensure_ascii=False)


def _upload(payload: dict) -> dict:
    return hi._process_history_import_sync(
        types.SimpleNamespace(user_id="usr_upload"), "k", {"job_id": "hi_up"}, payload)


def test_upload_model_failure_completes_job_like_before_instead_of_failing(monkeypatch):
    """之前（切换前本入口）：每个窗口的抽取错误被吞掉，任务照常完成（身份卡、问候写了，0 张卡）。
    切换后一度变成：模型一报错整单失败。恢复前者；写库本身坏了仍然失败（见下一条）。"""
    calls = _upload_env(monkeypatch, reply=RuntimeError("provider_timeout_simulated"))
    job = _upload({"content": _CHAT, "format": "plaintext"})
    assert calls["prompts"], "确实问过模型"
    assert job["status"] == "completed"
    assert job["identity_written"] is True and job["onboarding_greeting_written"] is True
    assert job["memories_created"] == 0 and calls["actions"] == []
    assert any(w.startswith("provider_memory_import_failed:initial:history:RuntimeError")
               for w in job["warnings"])
    assert all("西湖" not in w for w in job["warnings"]), "warning 里不带材料内容"


def test_upload_storage_failure_still_fails_the_job(monkeypatch):
    _upload_env(monkeypatch, reply=_cards_reply({"summary": "每周六去西湖边骑车",
                                                 "content": "每周六早上去西湖边骑车，坚持三年。"}),
                rows=lambda actions: [_err("enclave_unavailable", 409) for _ in actions])
    with pytest.raises(RuntimeError, match="memory_actions_failed:enclave_unavailable"):
        _upload({"content": _CHAT, "format": "plaintext"})


def test_upload_rewrites_user_placeholder_to_the_name_before_writing(monkeypatch):
    """之前：模型写出「用户喜欢…」→ 写库前确定性改成「小雨喜欢…」（d72e74c4 / 67bf4b96）；
    「用户增长」这类产品词不动。"""
    calls = _upload_env(monkeypatch, user_name="小雨", reply=_cards_reply({
        "summary": "用户喜欢周六早上去西湖边骑车",
        "content": "用户喜欢清晨骑车，用户增长这个词不是在说人。",
        "bucket": "关于用户", "threads": ["用户的周末"]}))
    job = _upload({"content": _CHAT, "format": "plaintext"})
    assert job["status"] == "completed"
    mem = calls["actions"][0]["memory"]
    assert mem["summary"] == "小雨喜欢周六早上去西湖边骑车"
    assert mem["content"] == "小雨喜欢清晨骑车，用户增长这个词不是在说人。"
    assert mem["bucket"] == "关于小雨" and mem["threads"] == ["小雨的周末"]
    assert calls["identity"][0][0]["summary"] == "小雨喜欢周六早上去西湖边骑车"


def test_upload_undated_archive_card_stays_undated(monkeypatch):
    """之前（0831f3b0）：本入口材料里没写日期的卡 occurred_at 留空，不拿「认识那天」硬填。"""
    calls = _upload_env(monkeypatch, reply=_cards_reply(
        {"summary": "最喜欢的书是《小王子》", "content": "档案里写着最喜欢的书是《小王子》。"},
        {"summary": "2019 年搬到杭州", "content": "2019-08-01 搬到杭州。", "occurred_at": "2019-08-01"}))
    job = _upload({"memory_summary_content": "- 最喜欢的书是《小王子》\n- 2019-08-01 搬到杭州\n",
                   "relationship_started_at": "2025-01-01"})
    assert job["status"] == "completed"
    assert any("[The entries they wrote]" in p for p in calls["prompts"])   # 档案走 curated_archive
    dates = {a["memory"]["summary"]: a["memory"]["occurred_at"] for a in calls["actions"]}
    assert dates == {"最喜欢的书是《小王子》": "", "2019 年搬到杭州": "2019-08-01"}


def test_upload_region_tagged_archive_language_imports_instead_of_crashing(monkeypatch):
    """C1 之前：iOS 存的档案语言 ``en-US`` 经 ``import_language_with_archive`` 原样返回、
    进 ImportRequest → memgarden UnknownBucketLocaleError。旧上传入口吞模型侧错误，
    表现为「完成、0 张卡」+ 一条 warning。之后：导入引擎入口归一，卡照常写进去。"""
    assert hi.import_language_with_archive([{"content": "hello there"}], "en-US") == "en-US"
    calls = _upload_env(monkeypatch, reply=_cards_reply({
        "summary": "Rides a bike around West Lake every Saturday",
        "content": "Every Saturday morning they ride a bike around West Lake."}))
    monkeypatch.setattr(hi, "_import_language_for_store", lambda *_a: "en-US")
    job = _upload({"content": _CHAT, "format": "plaintext"})
    assert job["status"] == "completed"
    assert job["memories_created"] == 1 and len(calls["actions"]) == 1
    assert not any("provider_memory_import_failed" in w for w in job["warnings"])
    assert "Health" in calls["prompts"][0]
