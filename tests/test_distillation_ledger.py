from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import distillation_ledger as ledger  # noqa: E402


def test_five_genesis_artifacts_keep_producer_outcome_and_terminal_result(monkeypatch):
    starts = []
    finishes = []
    monkeypatch.setattr(
        ledger.db,
        "genesis_get_job",
        lambda *_args: {
            "source_kind": "onboarding",
            "metadata": {"ingest": "plaintext", "mode": "onboarding"},
            "output": {"stage": "genesis_v2_foreground"},
        },
    )
    monkeypatch.setattr(
        ledger.db,
        "distillation_start_artifact_attempt",
        lambda **kwargs: starts.append(kwargs) or kwargs,
    )
    monkeypatch.setattr(
        ledger.db,
        "distillation_finish_artifact_attempt",
        lambda attempt_id, **kwargs: finishes.append({"attempt_id": attempt_id, **kwargs}) or kwargs,
    )
    store = SimpleNamespace(user_id="usr_ledger")
    outcomes = {
        "memory": "written",
        "identity": "locked",
        "persona": "preserved",
        "voice": "not_provided",
        "profile": "superseded",
    }

    for artifact, outcome in outcomes.items():
        with ledger.ArtifactAttempt(store, "job_1", artifact) as attempt:
            attempt.finish(outcome)

    assert {row["artifact"] for row in starts} == set(outcomes)
    assert all(row["access_path"] == "apikey_v2" for row in starts)
    assert {row["outcome"] for row in finishes} == set(outcomes.values())
    terminal = {row["outcome"]: row["terminal_result"] for row in finishes}
    assert terminal == {
        "written": "succeeded",
        "locked": "no_write",
        "preserved": "no_write",
        "not_provided": "no_write",
        "superseded": "no_write",
    }
    assert ledger.terminal_result_for("partial") == "succeeded"


def test_retry_attempts_are_append_only_and_exception_closes_failure(monkeypatch):
    starts = []
    finishes = []
    monkeypatch.setattr(
        ledger.db,
        "genesis_get_job",
        lambda *_args: {"source_kind": "resident_redistill", "metadata": {}},
    )
    monkeypatch.setattr(
        ledger.db,
        "distillation_start_artifact_attempt",
        lambda **kwargs: starts.append(kwargs) or kwargs,
    )
    monkeypatch.setattr(
        ledger.db,
        "distillation_finish_artifact_attempt",
        lambda attempt_id, **kwargs: finishes.append({"attempt_id": attempt_id, **kwargs}) or kwargs,
    )
    store = SimpleNamespace(user_id="usr_retry")

    with pytest.raises(RuntimeError, match="write exploded"):
        with ledger.ArtifactAttempt(store, "job_retry", "memory"):
            raise RuntimeError("write exploded")
    with ledger.ArtifactAttempt(store, "job_retry", "memory") as attempt:
        attempt.finish("written")
        with pytest.raises(
            RuntimeError, match="distillation artifact attempt already finished"
        ):
            attempt.finish("partial")

    assert len({row["attempt_id"] for row in starts}) == 2
    assert [row["outcome"] for row in finishes] == ["write_failed", "written"]
    assert [row["terminal_result"] for row in finishes] == ["failed", "succeeded"]
    assert all(row["distill_kind"] == "redistill" for row in starts)
    assert all(row["access_path"] == "self_hosted" for row in starts)


def test_unknown_outcome_is_rejected_before_it_can_enter_the_ledger(monkeypatch):
    monkeypatch.setattr(ledger.db, "genesis_get_job", lambda *_args: {})
    monkeypatch.setattr(
        ledger.db,
        "distillation_start_artifact_attempt",
        lambda **kwargs: kwargs,
    )
    store = SimpleNamespace(user_id="usr_bad")
    with ledger.ArtifactAttempt(store, "job_bad", "persona") as attempt:
        with pytest.raises(ValueError, match="invalid persona producer outcome"):
            attempt.finish("looks_good_to_me")


def test_missing_job_dimensions_skip_the_attempt_instead_of_inventing_a_path(
    monkeypatch,
):
    starts = []
    monkeypatch.setattr(ledger.db, "genesis_get_job", lambda *_args: None)
    monkeypatch.setattr(
        ledger.db,
        "distillation_start_artifact_attempt",
        lambda **kwargs: starts.append(kwargs),
    )
    store = SimpleNamespace(user_id="usr_missing_job")

    with ledger.ArtifactAttempt(store, "job_missing", "voice") as attempt:
        attempt.finish("not_provided")

    assert starts == []


def test_plaintext_redistill_keeps_v1_access_path_and_redistill_kind(monkeypatch):
    starts = []
    monkeypatch.setattr(
        ledger.db,
        "genesis_get_job",
        lambda *_args: {
            "source_kind": "add_memory",
            "metadata": {"ingest": "plaintext", "mode": "add_memory"},
            "output": {"stage": "plaintext_add_memory"},
        },
    )
    monkeypatch.setattr(
        ledger.db,
        "distillation_start_artifact_attempt",
        lambda **kwargs: starts.append(kwargs) or kwargs,
    )
    monkeypatch.setattr(
        ledger.db,
        "distillation_finish_artifact_attempt",
        lambda *_args, **_kwargs: {},
    )
    store = SimpleNamespace(user_id="usr_v1_redistill")

    with ledger.ArtifactAttempt(store, "job_v1", "memory") as attempt:
        attempt.finish("written")

    assert starts[0]["distill_kind"] == "redistill"
    assert starts[0]["access_path"] == "apikey_v1"
