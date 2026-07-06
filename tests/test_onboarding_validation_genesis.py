from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from hosted import onboarding_validation as validation  # noqa: E402


def _store(user_id: str = "usr_genesis_validate"):
    return types.SimpleNamespace(user_id=user_id)


def _install_model_api_harness(monkeypatch, *, genesis_jobs: list[dict], identity: dict | None = None,
                               memory_count: int = 0) -> None:
    monkeypatch.setattr(
        validation.boot_gates,
        "_bootstrap_state",
        lambda _store: {
            "memory_count": memory_count,
            "counts": {"story": 0, "about_me": 0, "ta_thinking": 0},
            "floors": {"story": 1, "about_me": 1},
            "missing_tabs": ["story", "about_me"],
        },
    )
    monkeypatch.setattr(validation.identity_service, "_load_identity", lambda _store: identity)
    monkeypatch.setattr(validation.identity_service, "_live_days_with_user", lambda *_args, **_kwargs: 28)
    monkeypatch.setattr(
        validation.hosted_config_store,
        "_load_model_api_config",
        lambda _store: {"provider": "openrouter", "model": "openai/gpt-4.1-mini", "test_status": "ok"},
    )
    monkeypatch.setattr(
        validation.hosted_config_store,
        "_ensure_model_api_runtime_profile",
        lambda _store, _config: {
            "runtime_mode": validation.hosted_config_store.MODEL_API_RUNTIME_MODE,
            "runtime_version": validation.hosted_config_store.MODEL_API_RUNTIME_VERSION,
            "tool_action_enabled": True,
        },
    )
    monkeypatch.setattr(validation, "_latest_history_import_job", lambda _store: None)
    monkeypatch.setattr(validation, "_model_api_hosted_chat_verified", lambda _store: False)
    monkeypatch.setattr(validation.db, "genesis_list_jobs", lambda _user_id, limit=20: genesis_jobs)


def test_model_api_validate_uses_processing_genesis_job_for_onboarding_steps(monkeypatch):
    _install_model_api_harness(
        monkeypatch,
        genesis_jobs=[
            {
                "job_id": "genesis_1",
                "status": "processing",
                "source_kind": "history_import",
                "output": {"stage": "plaintext_reducer"},
                "metadata": {
                    "ingest": "plaintext",
                    "history_count": 120,
                    "window_count": 3,
                    "history_tier": "small",
                    "timeline_span_days": 9,
                },
            }
        ],
    )

    body = validation._model_api_onboarding_validation_payload(_store())
    steps = {step["id"]: step for step in body["steps"]}

    assert body["route"] == "model_api"
    assert body["stage"] == "history_import"
    assert steps["history_import"]["genesis"] is True
    assert steps["history_import"]["passing"] is False
    assert steps["history_import"]["job_status"] == "processing"
    assert steps["history_import"]["messages_parsed"] == 120
    assert steps["history_import"]["timeline_span_days"] == 9
    assert steps["memory_garden"]["passing"] is False
    assert steps["identity_card"]["passing"] is False
    assert steps["relationship_anchor"]["passing"] is False
    assert steps["hosted_chat"]["passing"] is False


def test_model_api_validate_ticks_steps_incrementally_before_done(monkeypatch):
    # the checklist must light up per-artifact (identity/relationship) as each lands,
    # NOT wait for the single job `done` — restores the legacy/base behavior. Here the
    # job is still processing but identity + relationship are already written.
    _install_model_api_harness(
        monkeypatch,
        identity={
            "agent_name": "小柒",
            "relationship_started_at": "2026-06-01",
            "relationship_anchor_source": "genesis_import",
            "relationship_anchor_evidence": "Imported chat history.",
        },
        genesis_jobs=[
            {
                "job_id": "genesis_1",
                "status": "processing",   # NOT done yet
                "source_kind": "history_import",
                "identity_status": "initialized",
                "output": {"stage": "genesis_v2_foreground"},
                "metadata": {"ingest": "plaintext", "history_count": 2, "timeline_span_days": 1},
            }
        ],
    )

    steps = {s["id"]: s for s in validation._model_api_onboarding_validation_payload(_store())["steps"]}

    # identity + relationship already written -> their steps pass even though not done
    assert steps["identity_card"]["passing"] is True
    assert steps["relationship_anchor"]["passing"] is True
    # history_import stays the overall anchor -> still waiting until the job is done
    assert steps["history_import"]["passing"] is False


def test_model_api_validate_failed_job_keeps_artifacts_lit_but_overall_fails(monkeypatch):
    # Codex boundary: already-landed artifacts stay LIT even when the overall job fails
    # (UI shows 'identity done, but processing failed', not re-extinguished). But the
    # overall onboarding still fails, anchored on history_import.
    _install_model_api_harness(
        monkeypatch,
        identity={
            "agent_name": "小柒",
            "relationship_started_at": "2026-06-01",
            "relationship_anchor_source": "genesis_import",
            "relationship_anchor_evidence": "Imported chat history.",
        },
        memory_count=3,
        genesis_jobs=[
            {
                "job_id": "genesis_1",
                "status": "failed",
                "source_kind": "history_import",
                "identity_status": "initialized",
                "output": {"stage": "genesis_v2_background_deferred"},
                "metadata": {"ingest": "plaintext", "history_count": 2, "timeline_span_days": 1},
            }
        ],
    )

    body = validation._model_api_onboarding_validation_payload(_store())
    steps = {s["id"]: s for s in body["steps"]}

    assert steps["identity_card"]["passing"] is True          # landed artifact stays lit
    assert steps["relationship_anchor"]["passing"] is True
    assert steps["memory_garden"]["passing"] is True
    assert steps["history_import"]["passing"] is False         # overall anchor fails
    assert body["passing"] is False                            # onboarding still fails
    assert body["stage"] == "history_import"
    assert "failed" in steps["history_import"]["required"].lower()


def test_model_api_validate_marks_genesis_done_steps_complete(monkeypatch):
    _install_model_api_harness(
        monkeypatch,
        identity={
            "agent_name": "IO",
            "relationship_started_at": "2026-06-01",
            "relationship_anchor_source": "genesis",
            "relationship_anchor_evidence": "Imported chat history.",
        },
        genesis_jobs=[
            {
                "job_id": "genesis_1",
                "status": "done",
                "source_kind": "history_import",
                "memory_action_count": 2,
                "identity_status": "initialized",
                "persona_ref": "user_blob:genesis_persona",
                "output": {"stage": "complete"},
                "metadata": {"ingest": "plaintext", "history_count": 2, "timeline_span_days": 1},
            }
        ],
    )

    body = validation._model_api_onboarding_validation_payload(_store())
    steps = {step["id"]: step for step in body["steps"]}

    assert body["route"] == "model_api"
    assert body["stage"] == "complete"
    assert body["passing"] is True
    assert steps["history_import"]["passing"] is True
    assert steps["memory_garden"]["passing"] is True
    assert steps["memory_garden"]["memory_action_count"] == 2
    assert steps["identity_card"]["passing"] is True
    assert steps["relationship_anchor"]["passing"] is True
    assert steps["hosted_chat"]["passing"] is True


def test_model_api_validate_rejects_done_genesis_with_empty_identity_card(monkeypatch):
    _install_model_api_harness(
        monkeypatch,
        identity={
            "relationship_started_at": "2026-06-01",
            "relationship_anchor_source": "genesis_import",
            "relationship_anchor_evidence": "Imported chat history.",
        },
        genesis_jobs=[
            {
                "job_id": "genesis_1",
                "status": "done",
                "source_kind": "history_import",
                "memory_action_count": 2,
                "identity_status": "updated",
                "output": {"stage": "complete"},
                "metadata": {"ingest": "plaintext", "history_count": 2, "timeline_span_days": 1},
            }
        ],
    )

    body = validation._model_api_onboarding_validation_payload(_store())
    steps = {step["id"]: step for step in body["steps"]}

    assert body["passing"] is False
    assert body["stage"] == "identity_card"
    assert steps["identity_card"]["written"] is True
    assert steps["identity_card"]["complete"] is False
    assert steps["identity_card"]["passing"] is False


def test_genesis_stage_mapped_to_legacy_phase_for_old_ios(monkeypatch):
    # v2-internal stage must be reported to the client as the LEGACY phase the shipped
    # iOS already maps (so old apps don't show raw 'genesis_v2_foreground').
    from genesis import service as genesis_service
    assert genesis_service.public_stage("genesis_v2_foreground") == "chat_history_importing"
    assert genesis_service.public_stage("genesis_v2_background") == "background_importing"
    assert genesis_service.public_stage("genesis_v2_background_deferred") == "background_importing"
    assert genesis_service.public_stage("genesis_v2_done") == "completed"
    assert genesis_service.public_stage("chat_history_importing") == "chat_history_importing"  # passthru

    _install_model_api_harness(
        monkeypatch,
        identity={"agent_name": "小柒", "relationship_started_at": "2026-06-01",
                  "relationship_anchor_source": "genesis_import"},
        genesis_jobs=[{
            "job_id": "genesis_1", "status": "processing", "source_kind": "history_import",
            "output": {"stage": "genesis_v2_foreground"},
            "metadata": {"ingest": "plaintext", "history_count": 2, "timeline_span_days": 1},
        }],
    )
    steps = {s["id"]: s for s in validation._model_api_onboarding_validation_payload(_store())["steps"]}
    # the history_import step's phase is the mapped legacy phase, NOT the raw v2 stage
    assert steps["history_import"]["phase"] == "chat_history_importing"


def test_validate_surfaces_history_windows_failed_degraded(monkeypatch):
    # Task 2 already computes history_windows_total/history_windows_failed during
    # extraction; this test asserts they're surfaced through to onboarding validate's
    # history_import step, along with a derived `degraded` flag and
    # `support_inputs_present`. The genesis job (not the legacy history_import job,
    # which the harness stubs to None) is the source of truth here.
    _install_model_api_harness(monkeypatch, genesis_jobs=[{
        "job_id": "g1", "status": "done", "source_kind": "history_import",
        "identity_status": "initialized", "memory_action_count": 20,
        "output": {"stage": "genesis_v2_done",
                   "history_windows_total": 8, "history_windows_failed": 3},
        "metadata": {"ingest": "plaintext", "history_count": 68, "timeline_span_days": 1,
                     "history_windows_total": 8, "history_windows_failed": 3,
                     "support_count": 2},
    }])
    body = validation._model_api_onboarding_validation_payload(_store())
    step = next(s for s in body["steps"] if s["id"] == "history_import")
    assert step["history_windows_total"] == 8
    assert step["history_windows_failed"] == 3
    assert step["degraded"] is True
    assert step["support_inputs_present"] is True
