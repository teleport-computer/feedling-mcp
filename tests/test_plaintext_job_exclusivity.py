from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from conftest import seed_user  # noqa: E402


def _job(job_id: str) -> dict:
    return {
        "job_id": job_id,
        "status": "processing",
        "source_kind": "history_import",
        "metadata": {"ingest": "plaintext"},
    }


def test_plaintext_processing_slot_is_database_exclusive_across_workers():
    user_id = "usr_plaintext_exclusive"
    seed_user(user_id)
    db.genesis_create_job(user_id, _job("plaintext_a"))

    with pytest.raises(db.GenesisPlaintextJobActive) as exc:
        db.genesis_create_job(user_id, _job("plaintext_b"))

    assert exc.value.active_job_id == "plaintext_a"

    db.genesis_set_job_status(user_id, "plaintext_a", status="failed", error="test")
    assert db.genesis_create_job(user_id, _job("plaintext_b"))["job_id"] == "plaintext_b"
