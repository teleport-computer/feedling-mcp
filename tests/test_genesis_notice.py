"""genesis 失败/部分/恢复 → user_notices（spec Phase C / C1）。

Run:  python -m pytest tests/test_genesis_notice.py -q
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from conftest import seed_user  # noqa: E402
from core.store import get_store  # noqa: E402
from genesis import plaintext, service  # noqa: E402
from notices import core as notices_core  # noqa: E402


def _uid():
    return "usr_" + uuid.uuid4().hex[:12]


def _notices(uid):
    return {r["dedupe_key"]: r for r in db.log_read_all(uid, notices_core.NOTICES_STREAM)}


def test_mark_failed_emits_genesis_notice_with_upstream_class():
    uid = _uid(); seed_user(uid); store = get_store(uid)
    db.genesis_create_job(uid, {"job_id": "job_ab12", "status": "created"})
    service.mark_failed(store, "job_ab12", "insufficient_quota: credit balance too low")
    n = _notices(uid)["genesis:job_ab12"]
    assert n["source"] == "genesis" and n["severity"] == "error"
    assert n["error_class"] == "quota_insufficient"   # classify_upstream 命中上游类
    assert n["blame"] == "user_provider"


def test_mark_failed_unmatched_falls_back_to_genesis_failed():
    uid = _uid(); seed_user(uid); store = get_store(uid)
    db.genesis_create_job(uid, {"job_id": "job_xy", "status": "created"})
    service.mark_failed(store, "job_xy", "worker_failed:RuntimeError:apply_outputs_failed")
    n = _notices(uid)["genesis:job_xy"]
    assert n["error_class"] == "genesis_failed" and n["blame"] == "system"


def test_mark_failed_emits_even_when_job_row_missing():
    """emit 只需要 store + job_id + error，不依赖 job 行是否存在
    （write_genesis_state 在 job 不存在时走空，但 emit 仍应发）。"""
    uid = _uid(); seed_user(uid); store = get_store(uid)
    result = service.mark_failed(store, "job_ghost", "connection refused")
    assert result is None
    n = _notices(uid)["genesis:job_ghost"]
    assert n["error_class"] == "upstream_unavailable"
    assert n["blame"] == "provider_transient"


def test_apply_reducer_output_completion_resolves_prior_failure(monkeypatch):
    uid = _uid(); seed_user(uid); store = get_store(uid)
    db.genesis_create_job(uid, {"job_id": "job_ok", "status": "created"})

    # seed a prior failure notice for this job
    service.mark_failed(store, "job_ok", "connection refused")
    assert _notices(uid)["genesis:job_ok"]["resolved"] is False

    monkeypatch.setattr(service.memory_actions, "_execute_memory_actions",
                        lambda _store, _api_key, actions: ({"status": "ok", "results": []}, 200))
    monkeypatch.setattr(service, "init_identity_if_absent", lambda *_a, **_k: "initialized")
    monkeypatch.setattr(service, "write_persona_artifact", lambda *_a, **_k: ("", ""))
    monkeypatch.setattr(service, "write_voice_artifact", lambda *_a, **_k: ("", ""))

    service.apply_reducer_output(store, "api_key", "job_ok", {})

    n = _notices(uid)["genesis:job_ok"]
    assert n["resolved"] is True


def test_apply_reducer_output_dropped_memory_cards_emits_partial(monkeypatch):
    uid = _uid(); seed_user(uid); store = get_store(uid)
    db.genesis_create_job(uid, {"job_id": "job_partial", "status": "created"})

    monkeypatch.setattr(service.memory_actions, "_execute_memory_actions",
                        lambda _store, _api_key, actions: (
                            {"status": "ok", "results": [{"memory": {"id": "m1"}}]}, 200))
    monkeypatch.setattr(service, "init_identity_if_absent", lambda *_a, **_k: "initialized")
    monkeypatch.setattr(service, "write_persona_artifact", lambda *_a, **_k: ("", ""))
    monkeypatch.setattr(service, "write_voice_artifact", lambda *_a, **_k: ("", ""))

    output = {
        "memories": [
            {"type": "fact", "summary": "kept", "content": "kept content"},
            {"type": "fact", "content": "missing summary -> dropped by _memory_action_from_output"},
        ]
    }
    service.apply_reducer_output(store, "api_key", "job_partial", output)

    n = _notices(uid)["genesis:job_partial:partial"]
    assert n["error_class"] == "genesis_partial"
    assert n["blame"] == "system" and n["severity"] == "warning"
    # the completion-time resolve at the end of apply_reducer_output must not
    # clobber the partial notice emitted mid-run by this same call (dedupe_key
    # "genesis:job_partial:partial" also matches the "genesis:" prefix).
    assert n["resolved"] is False


def test_run_plaintext_add_memory_job_resolves_prior_failure(monkeypatch):
    """_run_plaintext_add_memory_job bypasses apply_reducer_output entirely (it's a
    third completion path alongside the two identity-first/background paths that
    already call notices_core.resolve directly) -> a successful add_memory run must
    still clear a prior genesis failure notice for this user."""
    uid = _uid(); seed_user(uid); store = get_store(uid)
    db.genesis_create_job(uid, {"job_id": "job_am1", "status": "created"})

    service.mark_failed(store, "job_am1", "connection refused")
    assert _notices(uid)["genesis:job_am1"]["resolved"] is False

    monkeypatch.setattr(
        plaintext.worker, "build_foreground_output_from_texts",
        lambda **_k: {"all_fact_candidates": [{"type": "fact", "summary": "s", "content": "c"}]},
    )
    monkeypatch.setattr(
        plaintext.worker, "build_memory_output_from_fact_candidates",
        lambda **_k: {"memories": [{"type": "fact", "summary": "s", "content": "c"}]},
    )
    monkeypatch.setattr(
        plaintext, "_plaintext_merge_reducer_outputs",
        lambda outputs, **_k: dict(outputs[0]),
    )
    monkeypatch.setattr(
        service, "apply_memory_outputs",
        lambda *_a, **_k: (1, [{"memory": {"id": "m1"}}]),
    )

    plaintext._run_plaintext_add_memory_job(
        store, "api_key", "job_am1",
        runtime=object(),
        source_groups=[{"source_kind": "chat_export", "chunk_texts": ["hi"]}],
    )

    n = _notices(uid)["genesis:job_am1"]
    assert n["resolved"] is True
    job = db.genesis_get_job(uid, "job_am1")
    assert job["status"] == "done"
    assert not any(key.startswith("profile_") for key in job["output"])


def test_run_plaintext_update_identity_job_resolves_prior_failure(monkeypatch):
    """_run_plaintext_update_identity_job (角色卡蒸馏 update_identity retry path) never
    goes through apply_reducer_output -> before this fix its 6 mark_failed exits emitted
    failure notices but the success completion never resolved them, so a failed attempt
    followed by a successful retry left a stale genesis_failed notice forever. Its
    success branch must resolve; this only proves the resolve fires and, symmetrically
    with test_apply_reducer_output_dropped_memory_cards_emits_partial's clobber check,
    guards the "don't emit partial here" invariant noted in the fix comment (this
    function never emits a genesis:...:partial notice, so nothing to clobber)."""
    uid = _uid(); seed_user(uid); store = get_store(uid)
    db.genesis_create_job(uid, {"job_id": "job_ui1", "status": "created"})

    service.mark_failed(store, "job_ui1", "connection refused")
    assert _notices(uid)["genesis:job_ui1"]["resolved"] is False

    monkeypatch.setattr(plaintext.identity_service, "_load_identity", lambda _store: {"agent_name": "x"})
    monkeypatch.setattr(plaintext.history_import, "_import_language_for_store", lambda *_a, **_k: "en")
    monkeypatch.setattr(
        plaintext.history_import, "_derive_identity_with_provider",
        lambda *_a, **_k: ({"agent_name": "New Name", "dimensions": [{"k": "v"}]}, []),
    )
    monkeypatch.setattr(plaintext, "_plaintext_persona_material_from_messages", lambda *_a, **_k: "persona material")
    monkeypatch.setattr(plaintext, "_plaintext_existing_voice_workset_for_update", lambda *_a, **_k: {})
    monkeypatch.setattr(
        plaintext.worker, "build_persona_output_from_material",
        lambda **_k: {"persona": {"content": "p"}},
    )
    monkeypatch.setattr(service, "replace_identity_preserving_anchor", lambda *_a, **_k: "updated")
    monkeypatch.setattr(service, "write_persona_artifact", lambda *_a, **_k: ("ref", "sha"))
    monkeypatch.setattr(
        plaintext,
        "_attach_plaintext_profile",
        lambda _store, _api_key, _job_id, *, output, **_kwargs: output.update({
            "profile": {
                "memory": "",
                "style": "style",
                "memory_touched": False,
                "style_touched": True,
                "provider_calls": 1,
            }
        }) or output,
    )
    monkeypatch.setattr(
        service,
        "write_profile_artifact",
        lambda *_a, **_k: ("profile-ref", "profile-sha", "written"),
    )

    plaintext._run_plaintext_update_identity_job(
        store, "api_key", "job_ui1",
        runtime=object(),
        analysis_messages=[{"role": "user", "content": "hi"}],
    )

    n = _notices(uid)["genesis:job_ui1"]
    assert n["resolved"] is True
    job = db.genesis_get_job(uid, "job_ui1")
    assert job["status"] == "done"
    assert job["output"]["profile_status"] == "written"
