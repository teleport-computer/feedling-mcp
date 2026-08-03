"""Genesis v2 Step 3b — the live job orchestration (foreground-fast wiring).

Tests the control flow of plaintext._run_plaintext_genesis_v2 with the heavy
collaborators (db / apply / background reduce) mocked: real DB e2e is run on test.
What must hold:
  - greetable foreground -> apply+complete, then background skips ONLY the history core
  - nothing greetable -> return False (caller falls back to the v1 full path), no greet
  - background failure -> job stays done (never fails an already-greetable onboarding)
  - the flag gate is off by default
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import db  # noqa: E402
from genesis import foreground, foreground_identity, plaintext, service, worker  # noqa: E402
from hosted import history_import  # noqa: E402


class _Store:
    user_id = "u1"


@pytest.fixture(autouse=True)
def _stub_full_fact_write(monkeypatch):
    def fake_full_fact_write(**kwargs):
        return {
            "memories": [
                {"summary": str(item.get("summary") or item.get("content") or "memory")}
                for item in (kwargs.get("fact_candidates") or [])
                if isinstance(item, dict)
            ],
            "identity": {"agent_name": "小柒", "dimensions": []},
        }

    monkeypatch.setattr(worker, "build_memory_output_from_fact_candidates", fake_full_fact_write)


def _groups():
    return [
        {"source_kind": "history_import", "source_family": "history", "chunk_texts": ["c1", "c2"]},
        {"source_kind": "ai_persona_import", "source_family": "ai_persona", "chunk_texts": ["p1"]},
    ]


def _greetable_fg(**_):
    return {
        "memories": [{"summary": "x"}],
        "identity": {"agent_name": "老 A"},
        "all_fact_candidates": [
            {"summary": "我家狗叫蛋子"},
            {"summary": "用户住在河南焦作"},
            {"summary": "用户远程做前端开发"},
            {"summary": "用户喜欢健身"},
            {"summary": "用户喜欢唱歌"},
            {"summary": "用户是 INFJ"},
        ],
        "core_fact_candidates": [{"summary": "我家狗叫蛋子"}],
        "source_family": "history",
    }


def test_v2_foreground_completes_then_background_skips_only_history_core(monkeypatch):
    calls = {}
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)
    monkeypatch.setattr(worker, "build_foreground_output_from_texts", _greetable_fg)
    # the foreground-applied merge carries the core memory text -> threaded to background
    monkeypatch.setattr(plaintext, "_plaintext_merge_reducer_outputs",
                        lambda outs, **k: {"memories": [{"summary": "用户养了一只狗叫蛋子"}]})
    monkeypatch.setattr(foreground_identity, "derive_foreground_identity",
                        lambda **k: ({"agent_name": "", "dimensions": []}, []))
    monkeypatch.setattr(service, "apply_reducer_output",
                        lambda *a, **k: calls.__setitem__("fg_applied", a[3]))
    monkeypatch.setattr(plaintext, "_run_plaintext_background_enrichment",
                        lambda *a, **k: calls.update(bg_skip=k["skip_texts"], bg_family=k["skip_family"],
                                                     bg_known=k.get("known_memories")))

    handled = plaintext._run_plaintext_genesis_v2(
        _Store(), "key", "job1", runtime=object(), source_groups=_groups(), relationship_anchor=None)

    assert handled is True
    assert "memories" in calls.get("fg_applied", {})            # foreground completed the job
    assert calls["bg_family"] == "history"                       # only the history group's core is skipped
    assert calls["bg_skip"] == foreground.core_skip_texts([{"summary": "我家狗叫蛋子"}])
    # the foreground core memory text is handed to the background as "already saved"
    assert calls["bg_known"] == ["用户养了一只狗叫蛋子"]


def test_v2_sampled_foreground_checkpoints_full_history_indices(monkeypatch):
    monkeypatch.setenv("FEEDLING_GENESIS_FG_HISTORY_CAP", "8")
    chunks = [f"history-{idx}" for idx in range(12)]
    expected_indices = [0, 2, 3, 5, 6, 8, 9, 11]
    captured = {}

    class Progress:
        def __init__(self):
            self.outputs = {}
            self.published = []

        def resume_outputs(self, source_pass, source_family):
            assert (source_pass, source_family) == (1, "history")
            return dict(self.outputs)

        def record_map(self, source_pass, source_family, chunk_index, output):
            assert (source_pass, source_family) == (1, "history")
            self.outputs[chunk_index] = output

        def publish(self, **kwargs):
            self.published.append(kwargs)

        def mark_identity_ready(self):
            return None

    progress = Progress()
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)

    def fake_foreground(**kwargs):
        assert kwargs["chunk_texts"] == [chunks[idx] for idx in expected_indices]
        resumed = kwargs.get("resume_map_outputs") or {}
        for local_index, text in enumerate(kwargs["chunk_texts"]):
            if local_index not in resumed:
                kwargs["on_map_completed"](
                    local_index,
                    {"fact_candidates": [{"summary": text}]},
                )
        return _greetable_fg()

    monkeypatch.setattr(worker, "build_foreground_output_from_texts", fake_foreground)
    monkeypatch.setattr(
        plaintext,
        "_plaintext_merge_reducer_outputs",
        lambda outs, **k: {"memories": [{"summary": "foreground memory"}]},
    )
    monkeypatch.setattr(
        foreground_identity,
        "derive_foreground_identity",
        lambda **k: ({"agent_name": "", "dimensions": []}, []),
    )
    monkeypatch.setattr(service, "apply_reducer_output", lambda *a, **k: None)

    def fake_background(*_args, **kwargs):
        captured["resumed"] = kwargs["progress"].resume_outputs(1, "history")

    monkeypatch.setattr(plaintext, "_run_plaintext_background_enrichment", fake_background)

    handled = plaintext._run_plaintext_genesis_v2(
        _Store(),
        "key",
        "job-large",
        runtime=object(),
        source_groups=[{
            "source_kind": "history_import",
            "source_family": "history",
            "chunk_texts": chunks,
        }],
        progress=progress,
    )

    assert handled is True
    assert sorted(captured["resumed"]) == expected_indices
    assert {
        index: captured["resumed"][index]["fact_candidates"][0]["summary"]
        for index in expected_indices
    } == {index: chunks[index] for index in expected_indices}
    assert sorted(set(range(len(chunks))) - set(captured["resumed"])) == [1, 4, 7, 10]
    assert progress.published[0]["stage"] == "genesis_v2_foreground"
    assert progress.published[0]["source_pass"] == 1


def test_v2_returns_false_when_nothing_greetable(monkeypatch):
    applied = {"n": 0}
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)
    monkeypatch.setattr(worker, "build_foreground_output_from_texts",
                        lambda **k: {"memories": [], "identity": {"agent_name": ""}, "core_fact_candidates": []})
    monkeypatch.setattr(service, "apply_reducer_output",
                        lambda *a, **k: applied.__setitem__("n", applied["n"] + 1))

    handled = plaintext._run_plaintext_genesis_v2(
        _Store(), "key", "job1", runtime=object(), source_groups=_groups(), relationship_anchor=None)

    assert handled is False and applied["n"] == 0                # never greets/completes on nothing


def test_v2_background_failure_marks_job_failed_for_retry(monkeypatch):
    last = {}
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: last.update(output=k.get("output")))
    monkeypatch.setattr(worker, "build_foreground_output_from_texts", _greetable_fg)
    monkeypatch.setattr(plaintext, "_plaintext_merge_reducer_outputs", lambda outs, **k: {"merged": True})
    monkeypatch.setattr(service, "apply_reducer_output", lambda *a, **k: None)
    monkeypatch.setattr(foreground_identity, "derive_foreground_identity",
                        lambda **k: ({"agent_name": "", "dimensions": []}, []))

    def boom(*a, **k):
        raise RuntimeError("provider 402 out of credits")
    monkeypatch.setattr(plaintext, "_run_plaintext_background_enrichment", boom)
    monkeypatch.setattr(
        service,
        "mark_failed",
        lambda _store, job_id, error, **_kwargs: last.update(
            job_id=job_id, error=error, status="failed"),
    )

    handled = plaintext._run_plaintext_genesis_v2(
        _Store(), "key", "job1", runtime=object(), source_groups=_groups(), relationship_anchor=None)

    assert handled is True
    assert last["status"] == "failed"
    assert last["job_id"] == "job1"
    assert "402" in last["error"]


def test_v2_background_lexical_backstop_drops_near_identical(monkeypatch):
    applied = {}
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)
    # background reduce yields a near-identical twin of the foreground core + a distinct fact
    monkeypatch.setattr(worker, "build_reducer_output_from_texts", lambda **k: {"memories": [
        {"summary": "用户养了一只比熊狗，叫蛋子。"},   # near-identical survivor -> backstop drops
        {"summary": "用户在杭州工作"},                # distinct -> keep
    ], "source_family": "history"})
    monkeypatch.setattr(plaintext, "_plaintext_merge_reducer_outputs",
                        lambda outs, **k: {"memories": outs[0]["memories"]} if outs else {"memories": []})
    monkeypatch.setattr(service, "apply_memory_outputs",
                        lambda store, api_key, merged: applied.update(memories=merged.get("memories")))
    monkeypatch.setattr(service, "init_identity_if_absent",
                        lambda store, merged, api_key=None: applied.update(identity_applied=True))
    monkeypatch.setattr(service, "write_persona_artifact", lambda *a, **k: ("", ""))
    monkeypatch.setattr(service, "write_voice_artifact", lambda *a, **k: ("", ""))

    plaintext._run_plaintext_background_enrichment(
        _Store(), "key", "job1", runtime=object(),
        source_groups=[{"source_kind": "history_import", "source_family": "history", "chunk_texts": ["c"]}],
        relationship_anchor=None, skip_family="history", skip_texts=set(),
        known_memories=["用户养了一只比熊狗叫蛋子"])

    summaries = [m["summary"] for m in applied["memories"]]
    assert "用户在杭州工作" in summaries                 # distinct kept
    assert not any("蛋子" in s for s in summaries)       # near-identical twin dropped by backstop
    assert applied.get("identity_applied") is True       # background writes the real identity


def test_v2_foreground_writes_identity_greeting_before_background_completion(monkeypatch):
    # identity-first contract (restored from legacy chat_ready): when analysis_messages
    # exist and the deriver yields a real identity, the FOREGROUND writes identity +
    # greeting + core, publishes readiness, and the background does NOT re-write identity.
    calls = {"statuses": []}
    monkeypatch.setattr(
        db,
        "genesis_set_job_status",
        lambda *a, **k: calls["statuses"].append(k),
    )
    monkeypatch.setattr(worker, "build_foreground_output_from_texts", _greetable_fg)
    monkeypatch.setattr(plaintext, "_plaintext_merge_reducer_outputs",
                        lambda outs, **k: {"memories": [{"summary": "用户养了一只狗叫蛋子"}]})
    monkeypatch.setattr(history_import, "_import_language_for_store", lambda store, msgs: "zh")
    monkeypatch.setattr(foreground_identity, "derive_foreground_identity",
                        lambda **k: ({"agent_name": "小柒", "dimensions": [{"name": "温柔"}]}, []))
    monkeypatch.setattr(service, "apply_memory_outputs",
                        lambda store, api_key, out: (len(out.get("memories") or []), []))
    monkeypatch.setattr(history_import, "_store_identity_payload",
                        lambda store, payload, **k: calls.update(identity_stored=payload,
                                                                 stored_days=k.get("days_with_user"),
                                                                 stored_started_at=k.get("relationship_started_at")))
    monkeypatch.setattr(history_import, "_generate_model_api_onboarding_greeting",
                        lambda *a, **k: ("小柒: 好久不见呀", []))
    monkeypatch.setattr(history_import, "_append_model_api_onboarding_greeting",
                        lambda store, text: calls.__setitem__("greeting", text))
    monkeypatch.setattr(service, "apply_reducer_output",
                        lambda *a, **k: calls.__setitem__("used_apply_reducer", True))
    monkeypatch.setattr(plaintext, "_run_plaintext_background_enrichment",
                        lambda *a, **k: calls.__setitem__("bg_write_identity", k.get("write_identity")))

    handled = plaintext._run_plaintext_genesis_v2(
        _Store(), "key", "job1", runtime=object(), source_groups=_groups(),
        relationship_anchor={"days_with_user": 144},
        analysis_messages=[{"role": "user", "content": "hi"}])

    assert handled is True
    assert calls["identity_stored"]["agent_name"] == "小柒"     # identity written in foreground
    assert "小柒" in calls["greeting"]                          # greeting written in foreground
    assert calls["statuses"][-1]["status"] == "processing"
    assert calls["statuses"][-1]["output"]["identity_ready"] is True
    assert calls["stored_days"] == 144                           # relationship anchor days -> identity
    assert calls["bg_write_identity"] is False                   # background must NOT re-write identity
    assert "used_apply_reducer" not in calls                     # did NOT take the empty-identity fallback


def test_v2_foreground_writes_full_memory_set_and_feeds_identity_and_greeting(monkeypatch):
    calls = {}
    full_memories = [{"summary": f"full memory {idx}"} for idx in range(6)]
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)
    monkeypatch.setattr(worker, "build_foreground_output_from_texts", _greetable_fg)

    def fake_full_fact_write(**kwargs):
        calls["full_fact_candidates"] = kwargs["fact_candidates"]
        return {"memories": full_memories, "identity": {"agent_name": "小柒"}}

    monkeypatch.setattr(worker, "build_memory_output_from_fact_candidates", fake_full_fact_write)
    monkeypatch.setattr(history_import, "_import_language_for_store", lambda store, msgs: "zh")
    monkeypatch.setattr(foreground_identity, "derive_foreground_identity",
                        lambda **k: calls.update(identity_memories=k["core_memories"]) or
                        ({"agent_name": "小柒", "dimensions": [{"name": "温柔"}]}, []))
    monkeypatch.setattr(service, "apply_memory_outputs",
                        lambda store, api_key, out: calls.update(written_memories=out.get("memories")) or
                        (len(out.get("memories") or []), []))
    monkeypatch.setattr(history_import, "_store_identity_payload", lambda *a, **k: None)
    monkeypatch.setattr(history_import, "_generate_model_api_onboarding_greeting",
                        lambda runtime, msgs, memory_cards, identity_payload, days, language:
                        calls.update(greeting_memories=memory_cards) or ("你好", []))
    monkeypatch.setattr(history_import, "_append_model_api_onboarding_greeting", lambda *a, **k: None)
    monkeypatch.setattr(db, "genesis_complete_job", lambda *a, **k: {"job_id": "job1", "status": "done"})
    monkeypatch.setattr(service, "write_genesis_state", lambda *a, **k: None)
    monkeypatch.setattr(plaintext, "_run_plaintext_background_enrichment", lambda *a, **k: None)

    handled = plaintext._run_plaintext_genesis_v2(
        _Store(), "key", "job1", runtime=object(), source_groups=_groups(),
        relationship_anchor={"days_with_user": 144},
        analysis_messages=[{"role": "user", "content": "hi"}])

    assert handled is True
    assert len(calls["full_fact_candidates"]) == 12
    assert calls["written_memories"] == full_memories
    assert calls["identity_memories"] == full_memories
    assert calls["greeting_memories"] == full_memories


def test_v2_combined_flag_writes_voice_persona_then_completes_remaining_windows(monkeypatch):
    calls = {"order": []}
    monkeypatch.setenv("FEEDLING_GENESIS_COMBINED_MAP", "1")
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)

    def fake_foreground(**kwargs):
        assert kwargs["include_voice_candidates"] is True
        is_history = kwargs["source_kind"] == "history_import"
        return {
            "memories": [{"summary": "history"}],
            "identity": {"agent_name": "小柒"},
            "all_fact_candidates": [{"summary": "用户叫 Z"}],
            "core_fact_candidates": [{"summary": "用户叫 Z"}],
            "voice_candidates": [{"behavior_notes_candidates": ["短句"], "exemplar_candidates": []}] if is_history else [],
            "source_family": "history" if is_history else "ai_persona",
        }

    monkeypatch.setattr(worker, "build_foreground_output_from_texts", fake_foreground)
    monkeypatch.setattr(worker, "build_memory_output_from_fact_candidates",
                        lambda **k: {"memories": [{"summary": "用户叫 Z"}], "identity": {"agent_name": "小柒"}})

    def fake_voice_persona(**kwargs):
        calls["voice_persona_candidates"] = kwargs["voice_candidates"]
        calls["existing_persona"] = kwargs.get("existing_persona")
        return {
            "persona": {"content": "## 你是谁\n\n你叫小柒。"},
            "voice_workset": {"behavior_notes": ["短句"], "exemplars": []},
            "voice": {"behavior_notes_count": 1, "exemplar_count": 0, "founding_exemplar_count": 0},
        }

    monkeypatch.setattr(worker, "build_voice_persona_output_from_candidates", fake_voice_persona, raising=False)
    monkeypatch.setattr(history_import, "_import_language_for_store", lambda store, msgs: "zh")
    monkeypatch.setattr(foreground_identity, "derive_foreground_identity",
                        lambda **k: ({"agent_name": "小柒", "dimensions": [{"name": "直接"}]}, []))
    monkeypatch.setattr(service, "apply_memory_outputs", lambda *a, **k: (1, []))
    monkeypatch.setattr(history_import, "_store_identity_payload", lambda *a, **k: None)
    monkeypatch.setattr(history_import, "_generate_model_api_onboarding_greeting", lambda *a, **k: ("你好", []))
    monkeypatch.setattr(history_import, "_append_model_api_onboarding_greeting", lambda *a, **k: None)
    monkeypatch.setattr(service, "write_persona_artifact",
                        lambda *a, **k: calls["order"].append("persona") or ("persona-ref", "persona-sha"))
    monkeypatch.setattr(service, "write_voice_artifact",
                        lambda *a, **k: calls["order"].append("voice") or ("voice-ref", "voice-sha"))

    monkeypatch.setattr(service, "write_genesis_state", lambda *a, **k: None)

    def fake_background(*_args, **kwargs):
        calls["order"].append("background")
        calls["background"] = kwargs

    monkeypatch.setattr(plaintext, "_run_plaintext_background_enrichment", fake_background)

    handled = plaintext._run_plaintext_genesis_v2(
        _Store(), "key", "job1", runtime=object(), source_groups=_groups(),
        relationship_anchor={"days_with_user": 144},
        analysis_messages=[{"role": "user", "content": "hi"}])

    assert handled is True
    assert calls["voice_persona_candidates"] == [{"behavior_notes_candidates": ["短句"], "exemplar_candidates": []}]
    assert calls["existing_persona"] == {"content": "p1"}
    assert calls["order"] == ["persona", "voice", "background"]
    assert calls["background"]["source_groups"] == _groups()
    assert calls["background"]["include_memory"] is True
    assert calls["background"]["include_persona_voice"] is False
    assert calls["background"]["write_identity"] is False
    assert calls["background"]["completion"]["persona_ref"] == "persona-ref"
    assert calls["background"]["completion"]["persona_sha256"] == "persona-sha"


def test_v2_foreground_full_fact_write_spans_all_source_groups(monkeypatch):
    calls = {"foreground_kinds": []}

    def fake_foreground(**kwargs):
        source_kind = kwargs["source_kind"]
        calls["foreground_kinds"].append(source_kind)
        if source_kind == "history_import":
            return {
                "memories": [{"summary": "history"}],
                "identity": {"agent_name": "小柒"},
                "all_fact_candidates": [{"summary": "用户养了一只狗叫蛋子"}],
                "core_fact_candidates": [{"summary": "用户养了一只狗叫蛋子"}],
                "source_family": "history",
            }
        return {
            "memories": [{"summary": "persona"}],
            "identity": {"agent_name": "小柒"},
            "all_fact_candidates": [{"summary": "乔伊是广告设计师和自媒体创作者"}],
            "core_fact_candidates": [{"summary": "乔伊是广告设计师和自媒体创作者"}],
            "source_family": "ai_persona",
        }

    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)
    monkeypatch.setattr(worker, "build_foreground_output_from_texts", fake_foreground)

    def fake_full_fact_write(**kwargs):
        calls["full_fact_candidates"] = kwargs["fact_candidates"]
        return {"memories": [{"summary": item["summary"]} for item in kwargs["fact_candidates"]]}

    monkeypatch.setattr(worker, "build_memory_output_from_fact_candidates", fake_full_fact_write)
    monkeypatch.setattr(history_import, "_import_language_for_store", lambda store, msgs: "zh")
    monkeypatch.setattr(foreground_identity, "derive_foreground_identity",
                        lambda **k: ({"agent_name": "小柒", "dimensions": [{"name": "温柔"}]}, []))
    monkeypatch.setattr(service, "apply_memory_outputs", lambda *a, **k: (2, []))
    monkeypatch.setattr(history_import, "_store_identity_payload", lambda *a, **k: None)
    monkeypatch.setattr(history_import, "_generate_model_api_onboarding_greeting", lambda *a, **k: ("你好", []))
    monkeypatch.setattr(history_import, "_append_model_api_onboarding_greeting", lambda *a, **k: None)
    monkeypatch.setattr(db, "genesis_complete_job", lambda *a, **k: {"job_id": "job1", "status": "done"})
    monkeypatch.setattr(service, "write_genesis_state", lambda *a, **k: None)
    monkeypatch.setattr(plaintext, "_run_plaintext_background_enrichment", lambda *a, **k: None)

    handled = plaintext._run_plaintext_genesis_v2(
        _Store(), "key", "job1", runtime=object(), source_groups=_groups(),
        relationship_anchor={"days_with_user": 144},
        analysis_messages=[{"role": "user", "content": "hi"}])

    assert handled is True
    assert calls["foreground_kinds"] == ["history_import", "ai_persona_import"]
    assert [item["summary"] for item in calls["full_fact_candidates"]] == [
        "用户养了一只狗叫蛋子",
        "乔伊是广告设计师和自媒体创作者",
    ]


def test_v2_background_can_skip_fact_write_for_voice_persona_only(monkeypatch):
    calls = {}
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)

    def fake_build(**kwargs):
        calls.setdefault("include_memory", []).append(kwargs.get("include_memory"))
        return {
            "memories": [{"summary": "should not be produced"}] if kwargs.get("include_memory", True) else [],
            "persona": {"content": "voice-backed persona"},
            "voice_workset": {
                "behavior_notes": ["短句"],
                "exemplars": [{"turns": [{"role": "ta", "text": "我在"}]}],
            },
            "source_family": "history",
        }

    monkeypatch.setattr(worker, "build_reducer_output_from_texts", fake_build)
    monkeypatch.setattr(plaintext, "_plaintext_merge_reducer_outputs",
                        lambda outs, **k: {
                            "memories": [m for out in outs for m in out.get("memories", [])],
                            "persona": outs[0].get("persona", {}),
                            "voice_workset": outs[0].get("voice_workset", {}),
                        })
    monkeypatch.setattr(service, "apply_memory_outputs",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("background must not write memory")))
    monkeypatch.setattr(service, "init_identity_if_absent",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("background must not write identity")))
    monkeypatch.setattr(service, "write_persona_artifact", lambda *a, **k: calls.update(persona=True) or ("ref", "sha"))
    monkeypatch.setattr(service, "write_voice_artifact", lambda *a, **k: calls.update(voice=True) or ("vref", "vsha"))

    plaintext._run_plaintext_background_enrichment(
        _Store(), "key", "job1", runtime=object(),
        source_groups=[{"source_kind": "history_import", "source_family": "history", "chunk_texts": ["c"]}],
        relationship_anchor=None, skip_family="history", skip_texts=set(),
        known_memories=[], write_identity=False, include_memory=False)

    assert calls["include_memory"] == [False]
    assert calls["persona"] is True
    assert calls["voice"] is True


def test_v2_combined_background_completes_every_material_window(monkeypatch):
    chunks = [f"history-{idx}" for idx in range(12)]
    foreground_indices = {0, 2, 3, 5, 6, 8, 9, 11}
    calls = {}

    class Progress:
        def __init__(self):
            self.outputs = {
                idx: {"fact_candidates": [{"summary": chunks[idx]}]}
                for idx in foreground_indices
            }

        def publish(self, **kwargs):
            calls.setdefault("published", []).append(kwargs)

        def resume_outputs(self, source_pass, source_family):
            assert (source_pass, source_family) == (1, "history")
            return dict(self.outputs)

        def record_map(self, source_pass, source_family, chunk_index, output):
            assert (source_pass, source_family) == (1, "history")
            self.outputs[chunk_index] = output

        def materials(self):
            done = len(self.outputs)
            return [{
                "kind": "chat_history",
                "status": "done" if done == len(chunks) else "processing",
                "windows_done": done,
                "windows_total": len(chunks),
                "cards": done,
            }]

    progress = Progress()

    def fake_build(**kwargs):
        calls["include_memory"] = kwargs["include_memory"]
        calls["include_persona_voice"] = kwargs["include_persona_voice"]
        resumed = kwargs["resume_map_outputs"]
        for idx, text in enumerate(kwargs["chunk_texts"]):
            if idx not in resumed:
                kwargs["on_map_completed"](
                    idx, {"fact_candidates": [{"summary": text}]})
        return {
            "memories": [{"summary": text} for text in kwargs["chunk_texts"]],
            "source_family": "history",
        }

    monkeypatch.setattr(worker, "build_reducer_output_from_texts", fake_build)
    monkeypatch.setattr(
        plaintext,
        "_plaintext_merge_reducer_outputs",
        lambda outputs, **_kwargs: outputs[0],
    )
    monkeypatch.setattr(
        service,
        "apply_memory_outputs",
        lambda *_args: (4, []),
    )
    monkeypatch.setattr(service, "write_persona_artifact", lambda *_args: ("", ""))
    monkeypatch.setattr(service, "write_voice_artifact", lambda *_args: ("", ""))
    monkeypatch.setattr(
        db,
        "genesis_complete_job",
        lambda *_args, **kwargs: calls.update(completed=kwargs) or {
            "job_id": "job-large",
            "status": "done",
            "output": kwargs["output"],
        },
    )
    monkeypatch.setattr(service, "write_genesis_state", lambda *_args, **_kwargs: None)

    plaintext._run_plaintext_background_enrichment(
        _Store(),
        "key",
        "job-large",
        runtime=object(),
        source_groups=[{
            "source_kind": "history_import",
            "source_family": "history",
            "chunk_texts": chunks,
        }],
        relationship_anchor=None,
        skip_family="history",
        skip_texts=set(),
        known_memories=["foreground memory"],
        write_identity=False,
        include_memory=True,
        include_persona_voice=False,
        progress=progress,
        completion={
            "memory_action_count": 8,
            "identity_status": "initialized",
            "persona_ref": "persona-ref",
            "persona_sha256": "persona-sha",
        },
    )

    assert calls["include_memory"] is True
    assert calls["include_persona_voice"] is False
    assert sorted(progress.outputs) == list(range(12))
    assert calls["completed"]["memory_action_count"] == 12
    assert calls["completed"]["output"]["materials"][0]["windows_done"] == 12
    assert calls["completed"]["output"]["materials"][0]["windows_total"] == 12


def test_v2_foreground_salvages_identity_from_support_card(monkeypatch):
    # LLM derive fails/empty, but the upload includes a character-card support message
    # with an explicit name -> the non-LLM lightweight fallback salvages it, and the
    # salvaged identity flows through the normal identity-first write path before
    # background completion and never fails.
    calls = {}
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)
    monkeypatch.setattr(worker, "build_foreground_output_from_texts", _greetable_fg)
    monkeypatch.setattr(plaintext, "_plaintext_merge_reducer_outputs", lambda outs, **k: {"memories": [{"summary": "x"}]})
    monkeypatch.setattr(history_import, "_import_language_for_store", lambda store, msgs: "zh")
    monkeypatch.setattr(foreground_identity, "derive_foreground_identity",
                        lambda **k: ({"agent_name": "", "dimensions": []}, []))
    monkeypatch.setattr(service, "apply_memory_outputs", lambda *a, **k: (1, []))
    monkeypatch.setattr(history_import, "_store_identity_payload",
                        lambda store, payload, **k: calls.update(payload=payload))
    monkeypatch.setattr(history_import, "_generate_model_api_onboarding_greeting", lambda *a, **k: ("", []))
    monkeypatch.setattr(history_import, "_append_model_api_onboarding_greeting", lambda *a, **k: None)
    monkeypatch.setattr(service, "write_genesis_state", lambda *a, **k: None)
    monkeypatch.setattr(
        plaintext,
        "_run_plaintext_background_enrichment",
        lambda *a, **k: calls.update(background=True),
    )
    monkeypatch.setattr(service, "mark_failed",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fail when salvage succeeds")))

    support_card = {
        "role": "user",
        "content": "# 阿樟 · 角色卡\n- 名字：阿樟\n- 性格：温柔但毒舌",
        "source": "ai_persona_import",
        "source_family": "ai_persona_import",
    }
    assert history_import._is_import_support_message(support_card)  # sanity: classified as support

    handled = plaintext._run_plaintext_genesis_v2(
        _Store(), "key", "job1", runtime=object(), source_groups=_groups(),
        relationship_anchor={"days_with_user": 1},
        analysis_messages=[support_card])

    assert handled is True
    assert calls["payload"]["agent_name"] == "阿樟"
    assert calls.get("background") is True


def test_v2_fresh_start_with_empty_core_still_greets(monkeypatch):
    # REAL fresh_start shape: the sentinel message yields ZERO core candidates
    # (there is nothing to extract from the synthetic 113-byte marker), unlike
    # the _greetable_fg stub below. v2 must NOT bail to the v1 full path here —
    # v1 never writes the onboarding greeting, so the apikey fresh user lands
    # on an empty chat with no first message.
    calls = {}
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)
    monkeypatch.setattr(worker, "build_foreground_output_from_texts",
                        lambda **k: {"memories": [], "identity": {"agent_name": ""},
                                     "core_fact_candidates": [], "all_fact_candidates": []})
    monkeypatch.setattr(history_import, "_import_language_for_store", lambda store, msgs: "zh")
    # fresh_start has no material by definition -> deriving identity from the
    # sentinel is meaningless (and a provider hiccup there must not fail the job).
    monkeypatch.setattr(foreground_identity, "derive_foreground_identity",
                        lambda **k: (_ for _ in ()).throw(AssertionError("fresh_start must not call the identity LLM")))
    monkeypatch.setattr(service, "mark_failed",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("fresh_start must not fail")))
    monkeypatch.setattr(service, "apply_reducer_output",
                        lambda *a, **k: calls.__setitem__("used_apply_reducer", True))
    monkeypatch.setattr(history_import, "_generate_model_api_onboarding_greeting",
                        lambda *a, **k: ("", []))
    monkeypatch.setattr(history_import, "_append_model_api_onboarding_greeting",
                        lambda store, text: calls.__setitem__("greeting", text))
    monkeypatch.setattr(plaintext, "_run_plaintext_background_enrichment",
                        lambda *a, **k: calls.__setitem__("bg_write_identity", k.get("write_identity")))

    handled = plaintext._run_plaintext_genesis_v2(
        _Store(), "key", "job1", runtime=object(), source_groups=_groups(),
        relationship_anchor={"days_with_user": 0},
        analysis_messages=[plaintext._plaintext_fresh_start_message()])

    assert handled is True                                       # never v1-fallback (v1 doesn't greet)
    assert calls["greeting"] == "你好，很高兴认识你。我现在还没有名字，你想以后怎么称呼我？"
    assert calls.get("used_apply_reducer") is True               # nameless done
    # No material by definition -> background enrichment must NOT run: it would
    # re-reduce the pure sentinel as history with write_identity=True (prod runs
    # WITHOUT FEEDLING_GENESIS_COMBINED_MAP, so that path is reachable there)
    # and could invent persona/identity from synthetic text.
    assert "bg_write_identity" not in calls


def test_v2_fresh_start_with_combined_map_flag_never_touches_voice_persona(monkeypatch):
    # test compose sets FEEDLING_GENESIS_COMBINED_MAP=1: with the flag on, the
    # foreground loop must not even extract voice candidates from the sentinel
    # (include_voice_candidates stays off) and build_voice_persona_output_...
    # must never run — same "never derive anything from the sentinel" rule as
    # the identity skip. The effective combined_map is disabled for
    # fresh_start_only input, so the flow still ends nameless greeting+done.
    calls = {}
    captured = {}
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)
    monkeypatch.setattr(worker, "genesis_combined_map_enabled", lambda: True)

    def fake_fg(**kwargs):
        captured["include_voice_candidates"] = kwargs.get("include_voice_candidates")
        return {"memories": [], "identity": {"agent_name": ""},
                "core_fact_candidates": [], "all_fact_candidates": []}

    monkeypatch.setattr(worker, "build_foreground_output_from_texts", fake_fg)
    monkeypatch.setattr(worker, "build_voice_persona_output_from_candidates",
                        lambda **k: (_ for _ in ()).throw(AssertionError("sentinel must not reach persona/voice build")))
    monkeypatch.setattr(history_import, "_import_language_for_store", lambda store, msgs: "zh")
    monkeypatch.setattr(foreground_identity, "derive_foreground_identity",
                        lambda **k: (_ for _ in ()).throw(AssertionError("fresh_start must not call the identity LLM")))
    monkeypatch.setattr(service, "mark_failed",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("fresh_start must not fail")))
    monkeypatch.setattr(service, "apply_reducer_output",
                        lambda *a, **k: calls.__setitem__("used_apply_reducer", True))
    monkeypatch.setattr(history_import, "_generate_model_api_onboarding_greeting",
                        lambda *a, **k: ("", []))
    monkeypatch.setattr(history_import, "_append_model_api_onboarding_greeting",
                        lambda store, text: calls.__setitem__("greeting", text))
    monkeypatch.setattr(plaintext, "_run_plaintext_background_enrichment",
                        lambda *a, **k: calls.__setitem__("bg_write_identity", k.get("write_identity")))

    handled = plaintext._run_plaintext_genesis_v2(
        _Store(), "key", "job1", runtime=object(), source_groups=_groups(),
        relationship_anchor={"days_with_user": 0},
        analysis_messages=[plaintext._plaintext_fresh_start_message()])

    assert handled is True
    assert captured["include_voice_candidates"] is False
    assert calls.get("used_apply_reducer") is True
    assert "greeting" in calls
    assert "bg_write_identity" not in calls


def test_v2_fresh_start_predicate_rejects_non_dict_messages(monkeypatch):
    # all(...) with an `if isinstance` filter would vacuously pass on non-dict
    # junk (["bad"] -> all([]) is True) and mis-route it as fresh_start. Junk
    # input must keep falling back to v1.
    applied = {"n": 0}
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)
    monkeypatch.setattr(worker, "build_foreground_output_from_texts",
                        lambda **k: {"memories": [], "identity": {"agent_name": ""},
                                     "core_fact_candidates": [], "all_fact_candidates": []})
    monkeypatch.setattr(service, "apply_reducer_output",
                        lambda *a, **k: applied.__setitem__("n", applied["n"] + 1))

    handled = plaintext._run_plaintext_genesis_v2(
        _Store(), "key", "job1", runtime=object(), source_groups=_groups(),
        relationship_anchor=None,
        analysis_messages=["bad"])

    assert handled is False and applied["n"] == 0


def test_v2_real_material_with_empty_core_still_falls_back_to_v1(monkeypatch):
    # Guard the OTHER side of the fresh_start carve-out: real user material that
    # yields no core must keep falling back to the v1 full path (its different
    # windowing may still extract), exactly as before.
    applied = {"n": 0}
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)
    monkeypatch.setattr(worker, "build_foreground_output_from_texts",
                        lambda **k: {"memories": [], "identity": {"agent_name": ""},
                                     "core_fact_candidates": [], "all_fact_candidates": []})
    monkeypatch.setattr(service, "apply_reducer_output",
                        lambda *a, **k: applied.__setitem__("n", applied["n"] + 1))

    handled = plaintext._run_plaintext_genesis_v2(
        _Store(), "key", "job1", runtime=object(), source_groups=_groups(),
        relationship_anchor=None,
        analysis_messages=[plaintext._plaintext_fresh_start_message(),
                           {"role": "user", "content": "真实历史消息", "source": "history_import"}])

    assert handled is False and applied["n"] == 0


def test_v2_foreground_fresh_start_allows_nameless_done(monkeypatch):
    # truly-empty upload (the real fresh_start sentinel message) with no derivable
    # identity must still complete nameless.
    calls = {}
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)
    monkeypatch.setattr(worker, "build_foreground_output_from_texts", _greetable_fg)
    monkeypatch.setattr(plaintext, "_plaintext_merge_reducer_outputs", lambda outs, **k: {"memories": []})
    monkeypatch.setattr(history_import, "_import_language_for_store", lambda store, msgs: "zh")
    monkeypatch.setattr(foreground_identity, "derive_foreground_identity",
                        lambda **k: ({"agent_name": "", "dimensions": []}, []))
    monkeypatch.setattr(service, "mark_failed",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("fresh_start must not fail")))
    monkeypatch.setattr(service, "apply_reducer_output",
                        lambda *a, **k: calls.__setitem__("used_apply_reducer", True))
    monkeypatch.setattr(history_import, "_generate_model_api_onboarding_greeting",
                        lambda *a, **k: ("", []))
    monkeypatch.setattr(history_import, "_append_model_api_onboarding_greeting",
                        lambda store, text: calls.__setitem__("greeting", text))
    monkeypatch.setattr(plaintext, "_run_plaintext_background_enrichment",
                        lambda *a, **k: calls.__setitem__("bg_write_identity", k.get("write_identity")))

    plaintext._run_plaintext_genesis_v2(
        _Store(), "key", "job1", runtime=object(), source_groups=_groups(),
        relationship_anchor={"days_with_user": 1},
        analysis_messages=[plaintext._plaintext_fresh_start_message()])

    assert calls.get("used_apply_reducer") is True               # nameless-done fallback
    # first-meet copy, not the import ("好久不见") one — a brand-new user has
    # never met the agent before.
    assert calls["greeting"] == "你好，很高兴认识你。我现在还没有名字，你想以后怎么称呼我？"
    assert "bg_write_identity" not in calls                      # sentinel-only: no enrichment


def test_v2_foreground_provider_identity_failure_marks_job_failed(monkeypatch):
    calls = {}
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)
    monkeypatch.setattr(worker, "build_foreground_output_from_texts", _greetable_fg)
    monkeypatch.setattr(worker, "build_memory_output_from_fact_candidates",
                        lambda **k: {"memories": [{"summary": "用户养狗"}]})
    monkeypatch.setattr(history_import, "_import_language_for_store", lambda store, msgs: "zh")
    monkeypatch.setattr(foreground_identity, "derive_foreground_identity",
                        lambda **k: ({"agent_name": "Fallback", "dimensions": [{"name": "泛化"}]},
                                     ["provider_identity_failed:ProviderError:provider_http_402:credits"]))
    monkeypatch.setattr(service, "mark_failed",
                        lambda store, job_id, error: calls.update(job_id=job_id, error=error) or
                        {"job_id": job_id, "status": "failed", "error": error})
    monkeypatch.setattr(service, "apply_memory_outputs",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must fail before writes")))

    handled = plaintext._run_plaintext_genesis_v2(
        _Store(), "key", "job1", runtime=object(), source_groups=_groups(),
        relationship_anchor={"days_with_user": 1},
        analysis_messages=[{"role": "user", "content": "hi"}])

    assert handled is True
    assert calls["job_id"] == "job1"
    assert calls["error"] == "onboarding_no_identity:provider_unstable"


def test_v2_foreground_honors_explicit_relationship_date(monkeypatch):
    # documented priority: if the user typed a relationship date, it wins verbatim —
    # it must NOT be overridden by prefer_memory (which for genesis' today-dated core
    # memories would collapse 相处天数 to 0).
    calls = {}
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)
    monkeypatch.setattr(worker, "build_foreground_output_from_texts", _greetable_fg)
    monkeypatch.setattr(plaintext, "_plaintext_merge_reducer_outputs", lambda outs, **k: {"memories": [{"summary": "x"}]})
    monkeypatch.setattr(history_import, "_import_language_for_store", lambda store, msgs: "zh")
    monkeypatch.setattr(foreground_identity, "derive_foreground_identity",
                        lambda **k: ({"agent_name": "小柒", "dimensions": [{"name": "温柔"}]}, []))
    monkeypatch.setattr(service, "apply_memory_outputs", lambda *a, **k: (1, []))
    monkeypatch.setattr(history_import, "_store_identity_payload",
                        lambda store, payload, **k: calls.update(stored_days=k.get("days_with_user"),
                                                                 stored_started_at=k.get("relationship_started_at")))
    monkeypatch.setattr(history_import, "_generate_model_api_onboarding_greeting", lambda *a, **k: ("", []))
    monkeypatch.setattr(history_import, "_append_model_api_onboarding_greeting", lambda *a, **k: None)
    monkeypatch.setattr(db, "genesis_complete_job", lambda *a, **k: {"job_id": "job1"})
    monkeypatch.setattr(service, "write_genesis_state", lambda *a, **k: None)
    monkeypatch.setattr(plaintext, "_run_plaintext_background_enrichment", lambda *a, **k: None)

    plaintext._run_plaintext_genesis_v2(
        _Store(), "key", "job1", runtime=object(), source_groups=_groups(),
        relationship_anchor={"days_with_user": 200, "relationship_started_at": "2024-06-01"},
        analysis_messages=[{"role": "user", "content": "hi"}])

    assert calls["stored_started_at"] == "2024-06-01"   # user's date passed through verbatim
    assert calls["stored_days"] == 200


def test_v2_foreground_content_without_identity_signal_completes_nameless(monkeypatch):
    # A real upload may contain memories but no identity signal. With no provider
    # failure warning this is a valid nameless result, not a retryable failure.
    calls = {}
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)
    monkeypatch.setattr(worker, "build_foreground_output_from_texts", _greetable_fg)
    monkeypatch.setattr(plaintext, "_plaintext_merge_reducer_outputs", lambda outs, **k: {"memories": []})
    monkeypatch.setattr(history_import, "_import_language_for_store", lambda store, msgs: "zh")
    monkeypatch.setattr(foreground_identity, "derive_foreground_identity",
                        lambda **k: ({"agent_name": "", "dimensions": []}, []))
    monkeypatch.setattr(service, "mark_failed",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("valid nameless import must not fail")))
    monkeypatch.setattr(service, "apply_reducer_output",
                        lambda *a, **k: calls.__setitem__("used_apply_reducer", True))
    monkeypatch.setattr(history_import, "_generate_model_api_onboarding_greeting",
                        lambda *a, **k: ("", []))
    monkeypatch.setattr(history_import, "_append_model_api_onboarding_greeting",
                        lambda store, text: calls.__setitem__("greeting", text))
    monkeypatch.setattr(plaintext, "_run_plaintext_background_enrichment",
                        lambda *a, **k: calls.__setitem__("bg_write_identity", k.get("write_identity")))

    handled = plaintext._run_plaintext_genesis_v2(
        _Store(), "key", "job1", runtime=object(), source_groups=_groups(),
        relationship_anchor={"days_with_user": 1},
        analysis_messages=[{"role": "user", "content": "hi"}])

    assert handled is True
    assert calls.get("used_apply_reducer") is True
    assert calls["greeting"] == "好久不见，很高兴又能和你聊天。"
    assert calls["bg_write_identity"] is True


def test_merged_has_identity_rule():
    assert plaintext._merged_has_identity({"identity": {"agent_name": "小柒", "dimensions": []}})
    assert plaintext._merged_has_identity({"identity": {"agent_name": "", "dimensions": [{"name": "温柔"}]}})
    assert not plaintext._merged_has_identity({"identity": {"agent_name": "", "dimensions": []}})
    assert not plaintext._merged_has_identity({"memories": []})


def test_v2_background_derives_baseline_identity_from_persona(monkeypatch):
    # the real bug: memories + persona generated but identity empty -> not_provided ->
    # onboarding wedges on identity_card. Background must derive a baseline from persona.
    applied = {}
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)
    monkeypatch.setattr(worker, "build_reducer_output_from_texts", lambda **k: {
        "memories": [{"summary": "用户在杭州工作"}],
        "persona": {"content": "你是小柒，温柔细心的陪伴者。"},
        "identity": {"agent_name": "", "dimensions": []},   # reduce produced NO identity
        "source_family": "history"})
    monkeypatch.setattr(plaintext, "_plaintext_merge_reducer_outputs", lambda outs, **k: {
        "memories": outs[0]["memories"], "persona": outs[0]["persona"],
        "identity": {"agent_name": "", "dimensions": []}})
    monkeypatch.setattr(worker, "derive_identity_from_persona", lambda **k: {
        "agent_name": "小柒", "category": "温柔 · 细心",
        "dimensions": [{"name": "温柔", "value": 80, "description": "历史里一贯的语气"}]})
    monkeypatch.setattr(service, "apply_memory_outputs", lambda *a, **k: None)
    monkeypatch.setattr(service, "init_identity_if_absent",
                        lambda store, merged, api_key=None: applied.update(identity=merged.get("identity")))
    monkeypatch.setattr(service, "write_persona_artifact", lambda *a, **k: ("", ""))
    monkeypatch.setattr(service, "write_voice_artifact", lambda *a, **k: ("", ""))

    plaintext._run_plaintext_background_enrichment(
        _Store(), "key", "job1", runtime=object(),
        source_groups=[{"source_kind": "history_import", "source_family": "history", "chunk_texts": ["c"]}],
        relationship_anchor=None, skip_family="history", skip_texts=set(), known_memories=[])

    # baseline derived from persona prose got written as the Identity Card
    assert applied["identity"]["agent_name"] == "小柒"
    assert applied["identity"]["dimensions"]


def test_v2_background_baseline_derivation_merges_not_overwrites_user_layer_fields(monkeypatch):
    # B2 regression guard: _merged_has_identity only counts agent_name/dimensions
    # as "usable identity" (unchanged), so a merged output carrying ONLY a B2
    # user-layer field (custom_persona_prompt here) still triggers the persona
    # baseline derivation below — that derivation must MERGE onto the existing
    # identity dict, not replace it, or the user-layer signal is silently lost
    # the moment a persona baseline also exists.
    applied = {}
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)
    monkeypatch.setattr(worker, "build_reducer_output_from_texts", lambda **k: {
        "memories": [], "persona": {"content": "你是小柒，温柔细心的陪伴者。"},
        "identity": {"agent_name": "", "dimensions": [], "custom_persona_prompt": "永远用第二人称。"},
        "source_family": "history"})
    monkeypatch.setattr(plaintext, "_plaintext_merge_reducer_outputs", lambda outs, **k: {
        "memories": [], "persona": outs[0]["persona"],
        "identity": {"agent_name": "", "dimensions": [], "custom_persona_prompt": "永远用第二人称。"}})
    monkeypatch.setattr(worker, "derive_identity_from_persona", lambda **k: {
        "agent_name": "小柒",
        "dimensions": [{"name": "温柔", "value": 80, "description": "历史里一贯的语气"}]})
    monkeypatch.setattr(service, "apply_memory_outputs", lambda *a, **k: None)
    monkeypatch.setattr(service, "init_identity_if_absent",
                        lambda store, merged, api_key=None: applied.update(identity=merged.get("identity")))
    monkeypatch.setattr(service, "write_persona_artifact", lambda *a, **k: ("", ""))
    monkeypatch.setattr(service, "write_voice_artifact", lambda *a, **k: ("", ""))

    plaintext._run_plaintext_background_enrichment(
        _Store(), "key", "job1", runtime=object(),
        source_groups=[{"source_kind": "history_import", "source_family": "history", "chunk_texts": ["c"]}],
        relationship_anchor=None, skip_family="history", skip_texts=set(), known_memories=[])

    assert applied["identity"]["agent_name"] == "小柒"
    assert applied["identity"]["dimensions"]
    # the user-layer field survived the baseline merge
    assert applied["identity"]["custom_persona_prompt"] == "永远用第二人称。"


def test_genesis_v2_flag_gate_off_by_default(monkeypatch):
    monkeypatch.delenv("FEEDLING_GENESIS_V2_ENABLED", raising=False)
    assert worker.genesis_v2_enabled() is False                 # default off -> v1 path
    monkeypatch.setenv("FEEDLING_GENESIS_V2_ENABLED", "true")
    assert worker.genesis_v2_enabled() is True
    monkeypatch.setenv("FEEDLING_GENESIS_V2_ENABLED", "0")
    assert worker.genesis_v2_enabled() is False
