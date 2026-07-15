"""Batch 2 A1 consumer 侧:蒸馏走共享模板、全字段、坏 JSON 重试一次、不静默。"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FEEDLING_API_URL", "http://fake.local")
os.environ.setdefault("FEEDLING_API_KEY", "test-key")
os.environ.setdefault("FEEDLING_DATA_DIR", tempfile.mkdtemp(prefix="feedling-rid-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import chat_resident_consumer as crc


GOOD = json.dumps({
    "agent_name": "小明", "self_introduction": "我是小明。", "category": "锐 · 实",
    "signature": ["有事直说", "别客套"], "tone_style": "短句、直接",
    "agent_role": "同事", "do_not_say": ["宝贝"], "boundaries": ["不聊政治"],
    "dimensions": [{"name": "直接", "value": 90, "description": "从不绕"}],
}, ensure_ascii=False)


def _patch(monkeypatch, replies):
    calls = {"prompts": []}
    def fake_call_agent(prompt, raw_text=True, trace_id=""):
        calls["prompts"].append(prompt)
        return replies[min(len(calls["prompts"]) - 1, len(replies) - 1)]
    monkeypatch.setattr(crc, "call_agent", fake_call_agent)
    monkeypatch.setattr(crc, "_capture_agent_reply_text", lambda x: x)
    monkeypatch.setattr(crc, "_resident_existing_identity", lambda: {})
    return calls


def test_derive_returns_full_persona_fields(monkeypatch):
    _patch(monkeypatch, [GOOD])
    out = crc._resident_derive_identity("人设材料", "job1")
    assert out["tone_style"] == "短句、直接"
    assert out["agent_role"] == "同事"
    assert out["do_not_say"] == ["宝贝"]
    assert out["boundaries"] == ["不聊政治"]
    assert out["signature"] == ["有事直说", "别客套"]


def test_prompt_comes_from_shared_template(monkeypatch):
    calls = _patch(monkeypatch, [GOOD])
    crc._resident_derive_identity("独特材料XYZ", "job2")
    p = calls["prompts"][0]
    assert "tone_style" in p and "do_not_say" in p and "boundaries" in p
    assert "独特材料XYZ" in p


def test_bad_json_retries_once_then_succeeds(monkeypatch):
    calls = _patch(monkeypatch, ["这不是 JSON", GOOD])
    out = crc._resident_derive_identity("材料", "job3")
    assert out is not None
    assert len(calls["prompts"]) == 2
    assert "ONLY the JSON" in calls["prompts"][1]  # 重试带纠偏提示


def test_bad_json_twice_returns_none(monkeypatch):
    calls = _patch(monkeypatch, ["垃圾", "还是垃圾"])
    assert crc._resident_derive_identity("材料", "job4") is None
    assert len(calls["prompts"]) == 2  # 只重试一次,不无限


def test_existing_identity_flows_into_merge_prompt(monkeypatch):
    calls = _patch(monkeypatch, [GOOD])
    monkeypatch.setattr(crc, "_resident_existing_identity",
                        lambda: {"agent_name": "老c", "tone_style": "锐"})
    crc._resident_derive_identity("材料", "job5")
    assert "EXISTING identity card" in calls["prompts"][0]
    assert "老c" in calls["prompts"][0]


def test_floor_note_below_floor(monkeypatch):
    monkeypatch.setattr(crc, "_capture_get_json",
                        lambda path, **kw: {"memory_floor": 38, "memories_count": 2})
    note = crc._resident_floor_note()
    assert "2" in note and "38" in note
    assert "绝不编造" in note


def test_floor_note_between_floor_and_aspiration_still_guides(monkeypatch):
    # two-tier (Xiaoting 763b0b03): floor is only the backstop; guidance keeps
    # encouraging real facts up to the aspiration (~2.3x floor when backend
    # doesn't expose one). 40 >= floor 38 but < asp 87 -> note still present.
    monkeypatch.setattr(crc, "_capture_get_json",
                        lambda path, **kw: {"memory_floor": 38, "memories_count": 40})
    note = crc._resident_floor_note()
    assert "38" in note and "87" in note
    assert "绝不编造" in note


def test_floor_note_empty_at_or_above_aspiration(monkeypatch):
    monkeypatch.setattr(crc, "_capture_get_json",
                        lambda path, **kw: {"memory_floor": 38, "memories_count": 90})
    assert crc._resident_floor_note() == ""


def test_floor_note_empty_on_error(monkeypatch):
    def boom(path, **kw):
        raise RuntimeError("api down")
    monkeypatch.setattr(crc, "_capture_get_json", boom)
    assert crc._resident_floor_note() == ""


def test_memory_snapshot_composes_terms_and_known(monkeypatch):
    def fake_get(path, **kw):
        if path == "/v1/memory/buckets":
            return {"buckets": [{"name": "工作", "count": 3}, {"name": "协作方式", "count": 2}]}
        if path == "/v1/memory/threads":
            return {"threads": [{"name": "查证不猜"}]}
        return {}
    monkeypatch.setattr(crc, "_capture_get_json", fake_get)
    monkeypatch.setattr(crc, "_resident_memory_index_summaries",
                        lambda: ["hx 是 Teleport 前端", "hx 的红线:优先成功率"])
    terms, known = crc._resident_memory_snapshot()
    assert "工作" in terms and "协作方式" in terms and "查证不猜" in terms
    assert "复用" in terms          # 引导语:先复用,别造近义/中英重复桶
    assert known == ["hx 是 Teleport 前端", "hx 的红线:优先成功率"]


def test_memory_snapshot_empty_garden_returns_empty(monkeypatch):
    monkeypatch.setattr(crc, "_capture_get_json", lambda path, **kw: {})
    monkeypatch.setattr(crc, "_resident_memory_index_summaries", lambda: [])
    terms, known = crc._resident_memory_snapshot()
    assert terms == "" and known == []


def test_memory_snapshot_error_returns_empty(monkeypatch):
    def boom(path, **kw):
        raise RuntimeError("api down")
    monkeypatch.setattr(crc, "_capture_get_json", boom)
    monkeypatch.setattr(crc, "_resident_memory_index_summaries", lambda: [])
    terms, known = crc._resident_memory_snapshot()
    assert terms == "" and known == []


# --------------------------------------------------------------------------- #
# P5 (Task 5): update_identity path — base_identity_replaced_at forwarding +
# conflict-aware re-derive on identity_base_stale.
# --------------------------------------------------------------------------- #

def _resident_job(job_id="job1", base_identity_replaced_at="2026-07-01T00:00:00"):
    return {
        "job_id": job_id,
        "mode": "update_identity",
        "material_kind": "",
        "base_identity_replaced_at": base_identity_replaced_at,
        "sealed": {"envelope": {"body_ct": "x"}},
    }


def _patch_distill_pipeline(monkeypatch, job, derive_results, execute_side_effects,
                             refreshed_baseline="2026-07-02T00:00:00"):
    monkeypatch.setattr(crc, "genesis_resident_pending", lambda: [job])
    monkeypatch.setattr(crc, "genesis_resident_heartbeat", lambda job_id: None)
    monkeypatch.setattr(crc, "_decrypt_sealed_material", lambda env: b"persona document")
    monkeypatch.setattr(crc, "_resident_current_replaced_at", lambda: refreshed_baseline)

    completed: dict = {}

    def fake_complete(job_id, *, memory_action_count, identity_status):
        completed["job_id"] = job_id
        completed["memory_action_count"] = memory_action_count
        completed["identity_status"] = identity_status

    monkeypatch.setattr(crc, "genesis_resident_complete", fake_complete)

    derive_calls = {"n": 0}

    def fake_derive(document, job_id):
        derive_calls["n"] += 1
        idx = min(derive_calls["n"] - 1, len(derive_results) - 1)
        return derive_results[idx]

    monkeypatch.setattr(crc, "_resident_derive_identity", fake_derive)

    execute_calls = {"n": 0, "baselines": []}

    def fake_execute(actions):
        execute_calls["n"] += 1
        execute_calls["baselines"].append(actions[0].get("base_identity_replaced_at"))
        idx = execute_calls["n"] - 1
        effect = execute_side_effects[min(idx, len(execute_side_effects) - 1)]
        if isinstance(effect, Exception):
            raise effect
        return effect

    monkeypatch.setattr(crc, "execute_identity_actions", fake_execute)
    return completed, derive_calls, execute_calls


def test_update_identity_forwards_job_baseline_on_first_attempt(monkeypatch):
    job = _resident_job()
    completed, derive_calls, execute_calls = _patch_distill_pipeline(
        monkeypatch, job,
        derive_results=[{"agent_name": "A"}],
        execute_side_effects=[{"status": "ok"}],
    )
    crc._process_resident_distill_once()
    assert derive_calls["n"] == 1
    assert execute_calls["n"] == 1
    assert execute_calls["baselines"] == ["2026-07-01T00:00:00"]
    assert completed["identity_status"] == "replaced"


def test_update_identity_conflict_once_then_succeeds(monkeypatch):
    job = _resident_job()
    completed, derive_calls, execute_calls = _patch_distill_pipeline(
        monkeypatch, job,
        derive_results=[{"agent_name": "A"}, {"agent_name": "B"}],
        execute_side_effects=[
            RuntimeError('identity_actions_http_409:{"error": "identity_base_stale"}'),
            {"status": "ok"},
        ],
    )
    crc._process_resident_distill_once()
    # derive ran twice (initial + one re-derive after the conflict).
    assert derive_calls["n"] == 2
    # execute ran twice; the retry carried the REFRESHED baseline, not the stale job one.
    assert execute_calls["n"] == 2
    assert execute_calls["baselines"] == ["2026-07-01T00:00:00", "2026-07-02T00:00:00"]
    assert completed["identity_status"] == "replaced"


def test_update_identity_conflict_twice_gives_up_no_third_attempt(monkeypatch):
    job = _resident_job()
    completed, derive_calls, execute_calls = _patch_distill_pipeline(
        monkeypatch, job,
        derive_results=[{"agent_name": "A"}, {"agent_name": "B"}],
        execute_side_effects=[
            RuntimeError('identity_actions_http_409:{"error": "identity_base_stale"}'),
            RuntimeError('identity_actions_http_409:{"error": "identity_base_stale"}'),
        ],
    )
    crc._process_resident_distill_once()
    # Exactly one re-derive, exactly one retry — never a third attempt (no infinite loop).
    assert derive_calls["n"] == 2
    assert execute_calls["n"] == 2
    assert completed["identity_status"] == "skipped_conflict"
    # job still completes (not left hanging for the reaper) despite the conflict.
    assert completed["job_id"] == "job1"


def test_update_identity_non_conflict_error_propagates_and_job_not_completed(monkeypatch):
    # A non-identity_base_stale error must NOT be swallowed as a conflict — it should
    # propagate up to the outer per-job try/except (leaving the job for the stale reaper),
    # same as before this change.
    job = _resident_job()
    completed, derive_calls, execute_calls = _patch_distill_pipeline(
        monkeypatch, job,
        derive_results=[{"agent_name": "A"}],
        execute_side_effects=[RuntimeError("identity_actions_http_500:boom")],
    )
    crc._process_resident_distill_once()  # swallowed by the outer except + logged, not raised
    assert derive_calls["n"] == 1
    assert execute_calls["n"] == 1
    assert completed == {}  # genesis_resident_complete never called
