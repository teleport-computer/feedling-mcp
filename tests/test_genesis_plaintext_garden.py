"""托管 plaintext genesis 换到 memgarden 导入会话之后的端到端行为（真 PostgreSQL、真记忆
执行器；模型、加密信封和 checkpoint 的加密层是替身 —— 加密契约不在这里验，也没被改）。

守的是：
  · 新 job 走导入会话；checkpoint 里存导入进度，重试从下一批接着跑，已提交的批次不再问
    模型、卡不重复
  · 升级前就开始、checkpoint 里已有旧流水线进度的 job 在旧流水线上跑完
  · 写卡语言跟 io 的导入语言判定走（英文材料 → 英文提示词）
  · onboarding：前台只读采样窗口 → 身份卡另走一次推导（拿的是真写进去的卡）→ 问候 →
    后台补剩下的窗口；不再调用 fact_map / fact_write
  · 全新开始（没有材料）不从占位文本里蒸卡，照样完成
  · 同一份材料再导一次：已有卡进「已有记忆索引」

全部是合成材料，不含真实用户数据。
"""
from __future__ import annotations

import json
import sys
import types
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from conftest import seed_user  # noqa: E402
from core.store import get_store  # noqa: E402
from genesis import foreground_identity, import_engine, plaintext, plaintext_garden, service, worker  # noqa: E402
from memory import actions as memory_actions  # noqa: E402
from memory import garden_import  # noqa: E402


def _material(prompt: str) -> str:
    end = prompt.find("[Output]")
    start = max(prompt.rfind("[The material", 0, end), prompt.rfind("[The entries", 0, end))
    return prompt[start:end] if start >= 0 else ""


def _card(summary: str, *, action: str = "add", target: str | None = None) -> dict:
    return {"action": action, "type": "fact", "target_id": target, "bucket": "爱好",
            "threads": [], "summary": summary,
            "content": f"{summary}，这是材料里明确说过的事情。", "importance": 0.6, "pulse": 0.3,
            "occurred_at": None}


class FakeModel:
    """按材料里的窗口标记回卡；``fail_on`` 里的标记第一次出现时模拟 provider 故障。"""

    def __init__(self, cards_by_marker: dict[str, list[dict]], *, fail_on: set[str] | None = None):
        self.cards_by_marker = cards_by_marker
        self.fail_on = set(fail_on or ())
        self.prompts: list[str] = []

    def __call__(self, **kwargs) -> types.SimpleNamespace:
        prompt = kwargs["messages"][0]["content"]
        self.prompts.append(prompt)
        material = _material(prompt)
        for marker in list(self.fail_on):
            if marker in material:
                self.fail_on.discard(marker)
                raise RuntimeError("provider_timeout_simulated")
        cards = [c for marker, cs in self.cards_by_marker.items() if marker in material for c in cs]
        return types.SimpleNamespace(text=json.dumps({"cards": cards}, ensure_ascii=False),
                                     stop_reason="stop")

    def windows_asked(self) -> list[str]:
        return [m for p in self.prompts for m in self.cards_by_marker if m in _material(p)]


@pytest.fixture
def env(monkeypatch):
    """真用户 + 真 genesis job 行 + 真记忆执行器；替身：模型、信封、checkpoint 加密层。"""
    monkeypatch.setenv("FEEDLING_GARDEN_IMPORT_STRATEGY", "single_pass")
    user_id = f"usr_{uuid.uuid4().hex[:12]}"
    seed_user(user_id)
    store = get_store(user_id)
    checkpoints: dict[str, dict] = {}
    saved_docs: list[dict] = []

    def write_checkpoint(_store, job_id, doc):
        checkpoints[job_id] = json.loads(json.dumps(doc, ensure_ascii=False))
        saved_docs.append(checkpoints[job_id])

    monkeypatch.setattr(service, "load_genesis_checkpoint",
                        lambda _s, _k, job_id, **_kw: json.loads(json.dumps(checkpoints[job_id]))
                        if job_id in checkpoints else None)
    monkeypatch.setattr(service, "write_genesis_checkpoint", write_checkpoint)
    monkeypatch.setattr(service, "delete_genesis_checkpoint", lambda *_a: None)
    monkeypatch.setattr(service, "write_genesis_state", lambda *_a, **_k: None)
    monkeypatch.setattr(plaintext.hosted_config_store, "_load_runtime_provider_config",
                        lambda *_a: types.SimpleNamespace(provider="p", model="m", base_url=""))
    monkeypatch.setattr(plaintext, "_resolve_plaintext_user_name", lambda *_a: "TA")
    monkeypatch.setattr(plaintext, "_write_back_plaintext_user_name", lambda *_a, **_k: None)
    monkeypatch.setattr(plaintext, "_attach_plaintext_profile",
                        lambda _s, _k, _j, *, output, **_kw: output)
    monkeypatch.setattr(service, "write_profile_artifact", lambda *_a, **_k: ("", "", "skipped"))
    counter = {"n": 0}

    def fake_envelope(actual_store, inner, *, item_id=None):
        counter["n"] += 1
        mid = item_id or f"mom_imp_{counter['n']}"
        return ({"id": mid, "body_ct": json.dumps(inner, ensure_ascii=False), "nonce": "n",
                 "K_user": "ku", "K_enclave": "ke", "enclave_pk_fpr": "fpr",
                 "visibility": "shared", "owner_user_id": actual_store.user_id}, "")

    monkeypatch.setattr(memory_actions, "_build_memory_envelope_for_store", fake_envelope)
    monkeypatch.setattr(memory_actions.boot_gates, "_log_bootstrap_event", lambda *_a, **_k: None)
    # supersede 要解开旧卡继承桶/线索：替身信封的正文就是明文 JSON。
    monkeypatch.setattr(memory_actions, "_memory_plain_from_envelope",
                        lambda _uid, moment, _key, runtime_token="": (json.loads(moment["body_ct"]), ""))
    # 读侧索引要 enclave 解密：这里直接用库里的卡（明文替身信封）当「已有记忆索引」。
    index_reads: list[list[dict]] = []

    def existing(_store, _api_key, **_kw):
        cards = []
        for m in db.memory_load(user_id):
            if str(m.get("status") or "active") != "active":
                continue
            body = json.loads(m["body_ct"])
            cards.append({"id": m["id"], "summary": body["summary"], "bucket": body.get("bucket", "")})
        index_reads.append(cards)
        return cards

    monkeypatch.setattr(import_engine, "existing_cards", existing)
    return types.SimpleNamespace(user_id=user_id, store=store, checkpoints=checkpoints,
                                 saved_docs=saved_docs, index_reads=index_reads)


def _job(env, job_id: str, *, mode: str) -> None:
    db.genesis_create_job(env.user_id, {
        "job_id": job_id, "status": "processing", "source_kind": "history_import",
        "metadata": {"ingest": "plaintext", "mode": mode},
    })


def _use_model(monkeypatch, model: FakeModel) -> None:
    class _LLM:
        def __init__(self, *_a, **_k):
            pass

        def complete(self, **kwargs):
            return model(**kwargs)

    monkeypatch.setattr(plaintext, "GenesisLLMClient", _LLM)


def _live_cards(user_id: str) -> list[str]:
    return sorted(json.loads(m["body_ct"])["summary"] for m in db.memory_load(user_id)
                  if str(m.get("status") or "active") == "active")


def _history_groups(*markers: str) -> list[dict]:
    return [{"source_kind": "history_import", "source_family": "history",
             "chunk_texts": [f"2025-03-0{i + 1}T10:00:00 The person: {m} 这一段聊到的事\n"
                             for i, m in enumerate(markers)]}]


def test_add_memory_resumes_from_checkpoint_without_rejudging_or_duplicating(env, monkeypatch):
    job_id = f"job_{uuid.uuid4().hex[:10]}"
    _job(env, job_id, mode="add_memory")
    model = FakeModel({"〔窗A〕": [_card("周末常去西湖边骑车")],
                       "〔窗B〕": [_card("每天早上喝一杯冰美式")]}, fail_on={"〔窗B〕"})
    _use_model(monkeypatch, model)
    failures: list[str] = []
    real_mark_failed = service.mark_failed
    monkeypatch.setattr(service, "mark_failed",
                        lambda s, j, err, **kw: failures.append(err) or real_mark_failed(s, j, err, **kw))
    groups = _history_groups("〔窗A〕", "〔窗B〕")

    plaintext._run_plaintext_genesis_job(env.store, "api_key", job_id, mode="add_memory",
                                         source_groups=groups)
    assert failures and "provider_timeout_simulated" in failures[0]
    assert _live_cards(env.user_id) == ["周末常去西湖边骑车"]
    doc = env.checkpoints[job_id]
    assert doc["import_engine"] == garden_import.ENGINE
    state = doc["garden_import"]
    assert state["sessions"]["am:1:history"]["cards_written"] == 1
    assert state["pending"] is None
    # 进度（含卡的摘要）只经由 genesis checkpoint 那一个出口保存 —— 生产里它是加密信封
    assert all("garden_import" in d for d in env.saved_docs[1:])

    model.prompts.clear()
    db.genesis_set_job_status(env.user_id, job_id, status="processing")
    plaintext._run_plaintext_genesis_job(env.store, "api_key", job_id, mode="add_memory",
                                         source_groups=groups)
    assert model.windows_asked() == ["〔窗B〕"], "已提交的窗A不能再问模型"
    assert _live_cards(env.user_id) == ["周末常去西湖边骑车", "每天早上喝一杯冰美式"]
    job = db.genesis_get_job(env.user_id, job_id)
    assert job["status"] == "done"
    assert job["memory_action_count"] == 2  # 整个导入写进去的张数（跨两次运行累计，不重复计）
    materials = job["output"].get("materials") or []
    assert materials == [] or materials[0]["windows_done"] == materials[0]["windows_total"] == 2


def test_in_flight_legacy_checkpoint_finishes_on_old_pipeline(env, monkeypatch):
    job_id = f"job_{uuid.uuid4().hex[:10]}"
    _job(env, job_id, mode="add_memory")
    env.checkpoints[job_id] = {
        "v": 1, "phase": "foreground_processing",
        "tasks": {"plaintext-map:1:history::0": {"status": "done", "task_id": "plaintext-map:1:history",
                                                  "chunk_id": 0}},
        "map_outputs": {"plaintext-map:1:history::0": {"fact_candidates": [{"summary": "养了一条狗"}]}},
    }
    monkeypatch.setattr(garden_import, "run_import",
                        lambda **_k: (_ for _ in ()).throw(AssertionError("must stay on old path")))
    seen = {}

    def old_map(**kwargs):
        seen["resume"] = kwargs.get("resume_map_outputs")
        return {"all_fact_candidates": [{"summary": "养了一条狗"}]}

    monkeypatch.setattr(worker, "build_foreground_output_from_texts", old_map)
    monkeypatch.setattr(worker, "build_memory_output_from_fact_candidates",
                        lambda **_k: {"memories": [{"type": "fact", "summary": "养了一条狗",
                                                    "content": "家里养了一条狗。"}]})
    plaintext._run_plaintext_genesis_job(env.store, "api_key", job_id, mode="add_memory",
                                         source_groups=_history_groups("〔窗A〕"))
    assert seen["resume"] == {0: {"fact_candidates": [{"summary": "养了一条狗"}]}}
    assert "import_engine" not in env.checkpoints[job_id]
    assert db.genesis_get_job(env.user_id, job_id)["status"] == "done"
    assert _live_cards(env.user_id) == ["养了一条狗"]


def test_locale_comes_from_io_import_language_detection(env, monkeypatch):
    job_id = f"job_{uuid.uuid4().hex[:10]}"
    _job(env, job_id, mode="add_memory")
    model = FakeModel({"W1": [_card("Runs the Chicago marathon in October")]})
    _use_model(monkeypatch, model)
    groups = [{"source_kind": "history_import", "source_family": "history",
               "chunk_texts": ["2025-06-01T20:30:00 The person: W1 signed up for the Chicago marathon\n"]}]
    msgs = [{"role": "user", "content": "signed up for the Chicago marathon in October, first one ever"}]
    plaintext._run_plaintext_genesis_job(env.store, "api_key", job_id, mode="add_memory",
                                         source_groups=groups, analysis_messages=msgs)
    assert env.checkpoints[job_id]["garden_import"]["params"]["locale"] == "en"
    assert "English" in model.prompts[0]
    assert "简体中文" not in model.prompts[0]


def test_reimport_puts_existing_cards_into_the_index(env, monkeypatch):
    groups = _history_groups("〔窗A〕")
    first_id = ""
    for round_no in (1, 2):
        job_id = f"job_{uuid.uuid4().hex[:10]}"
        _job(env, job_id, mode="add_memory")
        if round_no == 2:
            first_id = _existing_id(env.user_id)
        cards = ([_card("周末常去西湖边骑车")] if round_no == 1 else
                 [_card("周末常去西湖边骑车，喜欢清晨出发", action="merge", target=first_id)])
        model = FakeModel({"〔窗A〕": cards})
        _use_model(monkeypatch, model)
        plaintext._run_plaintext_genesis_job(env.store, "api_key", job_id, mode="add_memory",
                                             source_groups=groups)
    assert first_id and first_id in model.prompts[0], "第二次导入的已有记忆索引里必须有第一次的卡"
    assert _live_cards(env.user_id) == ["周末常去西湖边骑车，喜欢清晨出发"], "再导一次是合并，不是多一张"


def _existing_id(user_id: str) -> str:
    live = [m for m in db.memory_load(user_id) if str(m.get("status") or "active") == "active"]
    return live[0]["id"] if live else ""


def test_onboarding_foreground_identity_greeting_then_background(env, monkeypatch):
    monkeypatch.setattr(worker, "genesis_v2_enabled", lambda: True)
    monkeypatch.setattr(worker, "genesis_combined_map_enabled", lambda: False)
    monkeypatch.setenv("FEEDLING_GENESIS_FG_HISTORY_CAP", "2")
    monkeypatch.setattr(worker, "_fact_write", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("fact_write must not run for new jobs")))
    job_id = f"job_{uuid.uuid4().hex[:10]}"
    _job(env, job_id, mode="onboarding")
    markers = ["〔窗1〕", "〔窗2〕", "〔窗3〕"]
    model = FakeModel({m: [_card(f"第{i + 1}段里说到的一件具体的事")] for i, m in enumerate(markers)})
    _use_model(monkeypatch, model)
    timeline: list[str] = []
    identity_cards: list[list[str]] = []

    def derive(**kwargs):
        timeline.append("identity")
        identity_cards.append([c["summary"] for c in kwargs["core_memories"]])
        return {"agent_name": "小满", "dimensions": [
            {"name": "温柔", "value": 80, "description": "说话温和"}]}, []

    monkeypatch.setattr(foreground_identity, "derive_foreground_identity", derive)
    monkeypatch.setattr(plaintext.history_import, "_store_identity_payload",
                        lambda *_a, **_k: timeline.append("identity_write") or {"id": "idn"})
    monkeypatch.setattr(plaintext, "_append_plaintext_onboarding_greeting",
                        lambda *_a, **_k: timeline.append("greeting") or "hi")
    persona_calls = []
    monkeypatch.setattr(worker, "build_reducer_output_from_texts",
                        lambda **kw: persona_calls.append(kw) or {
                            "source_family": "history", "memories": [],
                            "persona": {"content": "## 你是谁", "source_family": "history"},
                            "voice_workset": {"behavior_notes": ["短句"], "exemplars": []}})
    monkeypatch.setattr(service, "write_persona_artifact", lambda *_a, **_k: ("ref", "sha"))
    monkeypatch.setattr(service, "write_voice_artifact", lambda *_a, **_k: ("", ""))
    real_asked = model.__call__

    def tracking(**kwargs):
        out = real_asked(**kwargs)
        timeline.append("model:" + ",".join(m for m in markers if m in _material(kwargs["messages"][0]["content"])))
        return out

    monkeypatch.setattr(plaintext, "GenesisLLMClient", lambda *_a, **_k: types.SimpleNamespace(complete=tracking))

    plaintext._run_plaintext_genesis_job(
        env.store, "api_key", job_id, mode="onboarding",
        source_groups=_history_groups(*markers),
        analysis_messages=[{"role": "user", "content": "聊天记录", "source": "history_import"}])

    first_identity = timeline.index("identity")
    assert set(t for t in timeline[:first_identity] if t.startswith("model:")) == {"model:〔窗1〕", "model:〔窗3〕"}
    assert timeline.index("greeting") < timeline.index("model:〔窗2〕"), "问候在后台窗口之前"
    assert identity_cards[0] == ["第1段里说到的一件具体的事", "第3段里说到的一件具体的事"]
    assert persona_calls and all(kw["include_memory"] is False for kw in persona_calls)
    job = db.genesis_get_job(env.user_id, job_id)
    assert job["status"] == "done"
    assert job["output"]["stage"] == "genesis_v2_done"
    assert [m["windows_done"] for m in job["output"]["materials"]] == [3]
    assert len(_live_cards(env.user_id)) == 3


def test_onboarding_fresh_start_writes_no_cards_and_completes(env, monkeypatch):
    monkeypatch.setattr(worker, "genesis_v2_enabled", lambda: True)
    job_id = f"job_{uuid.uuid4().hex[:10]}"
    _job(env, job_id, mode="onboarding")
    model = FakeModel({})
    _use_model(monkeypatch, model)
    greetings = []
    monkeypatch.setattr(plaintext, "_append_plaintext_onboarding_greeting",
                        lambda *_a, **kw: greetings.append(kw.get("fresh_start")) or "hi")
    sentinel = plaintext._plaintext_fresh_start_message()
    plaintext._run_plaintext_genesis_job(
        env.store, "api_key", job_id, mode="onboarding",
        source_groups=[{"source_kind": "history_import", "source_family": "history",
                        "chunk_texts": [sentinel["content"]]}],
        analysis_messages=[sentinel])
    assert model.prompts == []
    assert greetings == [True]
    assert db.genesis_get_job(env.user_id, job_id)["status"] == "done"
    assert _live_cards(env.user_id) == []


def test_onboarding_with_empty_foreground_falls_back_to_full_path(env, monkeypatch):
    monkeypatch.setattr(worker, "genesis_v2_enabled", lambda: True)
    job_id = f"job_{uuid.uuid4().hex[:10]}"
    _job(env, job_id, mode="onboarding")
    _use_model(monkeypatch, FakeModel({}))  # 什么都没得记
    monkeypatch.setattr(worker, "build_reducer_output_from_texts",
                        lambda **kw: {"source_family": "history", "memories": []})
    greetings = []
    monkeypatch.setattr(plaintext, "_append_plaintext_onboarding_greeting",
                        lambda *_a, **_k: greetings.append(1) or "hi")
    monkeypatch.setattr(foreground_identity, "derive_foreground_identity",
                        lambda **_k: ({"agent_name": "", "dimensions": []}, []))
    plaintext._run_plaintext_genesis_job(
        env.store, "api_key", job_id, mode="onboarding", source_groups=_history_groups("〔窗1〕"),
        analysis_messages=[{"role": "user", "content": "嗯", "source": "history_import"}])
    job = db.genesis_get_job(env.user_id, job_id)
    assert job["status"] == "done"
    assert greetings == []                       # 一次做完那条路不问候（同切换前）
    assert job["memory_action_count"] == 0


# --------------------------------------------------------------------------- #
# 切换前的保护，在新引擎上恢复
# --------------------------------------------------------------------------- #

def test_add_memory_rewrites_user_placeholder_to_the_name(env, monkeypatch):
    """之前（d72e74c4 / 67bf4b96）：导入卡写库前「用户喜欢…」→「小雨喜欢…」。"""
    monkeypatch.setattr(plaintext, "_resolve_plaintext_user_name", lambda *_a: "小雨")
    job_id = f"job_{uuid.uuid4().hex[:10]}"
    _job(env, job_id, mode="add_memory")
    _use_model(monkeypatch, FakeModel({"〔窗A〕": [_card("用户喜欢周末去西湖边骑车")]}))
    plaintext._run_plaintext_genesis_job(env.store, "api_key", job_id, mode="add_memory",
                                         source_groups=_history_groups("〔窗A〕"))
    assert db.genesis_get_job(env.user_id, job_id)["status"] == "done"
    assert _live_cards(env.user_id) == ["小雨喜欢周末去西湖边骑车"]


def test_add_memory_with_every_card_rejected_fails_instead_of_done_with_zero(env, monkeypatch):
    """之前（6972427d）：整段写卡全被判不合格 → 任务失败（用户可重试），不是「完成、0 张卡」。"""
    job_id = f"job_{uuid.uuid4().hex[:10]}"
    _job(env, job_id, mode="add_memory")
    _use_model(monkeypatch, FakeModel({"〔窗A〕": [_card("周末常去西湖边骑车"), _card("每天一杯冰美式")]}))
    monkeypatch.setattr(import_engine.memory_actions, "_execute_memory_actions",
                        lambda _s, _k, actions, **_kw: ({"results": [
                            {"status": "error", "error": "memory_card_polluted", "http_status": 422}
                            for _ in actions]}, 200))
    failures: list[str] = []
    real_mark_failed = service.mark_failed
    monkeypatch.setattr(service, "mark_failed",
                        lambda s, j, err, **kw: failures.append(err) or real_mark_failed(s, j, err, **kw))
    plaintext._run_plaintext_genesis_job(env.store, "api_key", job_id, mode="add_memory",
                                         source_groups=_history_groups("〔窗A〕"))
    job = db.genesis_get_job(env.user_id, job_id)
    assert job["status"] != "done"
    assert failures and "GardenImportCardsRejected" in failures[0]
    assert env.checkpoints[job_id]["garden_import"]["pending"] is None, "重试要重新问模型"


def test_archive_only_onboarding_still_brings_the_ai_name_from_the_archive(env, monkeypatch):
    """之前（5965e943 / 3fcfc2fc）：只上传长期记忆档案，TA 的名字 / 认识天数 / 关系锚点
    从档案那次 fact_write 里带出来写进身份卡。切换后那次调用没了，身份卡拿不到名字。"""
    monkeypatch.setattr(worker, "genesis_v2_enabled", lambda: False)
    job_id = f"job_{uuid.uuid4().hex[:10]}"
    _job(env, job_id, mode="onboarding")
    _use_model(monkeypatch, FakeModel({"〔档案〕": [_card("最喜欢的书是小王子")]}))
    fact_writes: list[dict] = []

    def fake_fact_write(_llm, **kwargs):
        fact_writes.append(kwargs)
        return {"memories": [{"summary": "不该写进去的卡", "content": "x"}],
                "identity": {"agent_name": "阿樟", "dimensions": [{"name": "Wrong", "value": 1}]},
                "days_with_user": 321, "relationship_anchor_evidence": "2024-08 开始"}

    monkeypatch.setattr(worker, "_fact_write", fake_fact_write)
    monkeypatch.setattr(foreground_identity, "derive_foreground_identity",
                        lambda **_k: ({"agent_name": "", "dimensions": []}, []))
    identity_outputs: list[dict] = []
    monkeypatch.setattr(service, "init_identity_if_absent",
                        lambda _s, output, _k=None, **_kw: identity_outputs.append(output) or "initialized")
    monkeypatch.setattr(service, "write_persona_artifact", lambda *_a, **_k: ("", ""))
    monkeypatch.setattr(service, "write_voice_artifact", lambda *_a, **_k: ("", ""))
    groups = [{"source_kind": "memory_summary_import", "source_family": "memory_summary",
               "chunk_texts": ["〔档案〕- 最喜欢的书是小王子\n- 叫我阿樟就好\n"]}]
    plaintext._run_plaintext_genesis_job(
        env.store, "api_key", job_id, mode="onboarding", source_groups=groups,
        analysis_messages=[{"role": "user", "content": "- 最喜欢的书是小王子",
                            "source": "memory_summary_import"}])
    assert db.genesis_get_job(env.user_id, job_id)["status"] == "done"
    assert _live_cards(env.user_id) == ["最喜欢的书是小王子"], "卡只来自导入会话"
    assert len(fact_writes) == 1 and "小王子" in fact_writes[0]["memory_summary"]
    merged = identity_outputs[0]
    assert merged["identity"]["agent_name"] == "阿樟"
    assert merged["identity"]["dimensions"] == [], "档案不推性格维度（同切换前）"
    assert merged["days_with_user"] == 321
    assert merged["relationship_anchor_evidence"] == "2024-08 开始"
