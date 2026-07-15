from __future__ import annotations

import json
from pathlib import Path

from qa import finalize_persona_review as review


def _args(tmp_path: Path) -> list[str]:
    fixture = tmp_path / "fixture.json"
    fixture.write_text("{}\n", encoding="utf-8")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    return [
        "--fixture",
        str(fixture),
        "--private-evidence",
        str(tmp_path / "evidence.json"),
        "--semantic-judgment",
        str(tmp_path / "judgment.json"),
        "--artifact-dir",
        str(artifacts),
    ]


def _report(*, ok: bool) -> dict:
    return {
        "ok": ok,
        "checks": {"persona_consistent": ok},
        "transport": {"archive_upload_count": 4},
        "privacy": {"violation_count": 0},
        "evidence": {
            "sha256": "a" * 64,
            "semantic_judgment_bound": True,
            "private_evidence_deleted": True,
        },
    }


def test_schema_valid_negative_verdict_is_completed_evidence(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.setattr(
        review.genesis_e2e,
        "finalize_existing_session_distill_acceptance",
        lambda **_kwargs: _report(ok=False),
    )

    assert review.main(_args(tmp_path)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False


def test_operational_finalizer_failure_is_nonzero(tmp_path: Path, monkeypatch, capsys):
    def fail(**_kwargs):
        raise review.genesis_e2e.ExistingSessionDistillError(
            "semantic", "semantic_judgment_invalid"
        )

    monkeypatch.setattr(
        review.genesis_e2e,
        "finalize_existing_session_distill_acceptance",
        fail,
    )

    assert review.main(_args(tmp_path)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "code": "semantic_judgment_invalid",
        "ok": False,
        "stage": "semantic",
    }


def test_malformed_finalizer_report_is_nonzero(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(
        review.genesis_e2e,
        "finalize_existing_session_distill_acceptance",
        lambda **_kwargs: {"ok": False},
    )

    assert review.main(_args(tmp_path)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "persona_review_report_invalid"
