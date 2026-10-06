"""Shared per-window task recovery for voice and identity updates."""
from copy import deepcopy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from genesis import checkpoint as cp  # noqa: E402


def test_new_checkpoint_starts_foreground():
    c = cp.new_checkpoint(now=1000.0)
    assert c["phase"] == cp.PHASE_FOREGROUND_PROCESSING
    assert c["tasks"] == {} and c["created_at"] == 1000.0


def test_upsert_done_and_pending_for_resume():
    c = cp.new_checkpoint(now=0.0)
    c = cp.upsert_task(c, task_id="fact_map", chunk_id=0, status=cp.TASK_DONE, now=1.0)
    c = cp.upsert_task(c, task_id="fact_map", chunk_id=1, status=cp.TASK_PENDING, now=2.0)
    assert cp.is_task_done(c, "fact_map", 0)
    assert not cp.is_task_done(c, "fact_map", 1)


def test_upsert_idempotent_preserves_attempts():
    c = cp.new_checkpoint(now=0.0)
    c = cp.upsert_task(c, task_id="fw", chunk_id=0, status=cp.TASK_TRANSIENT_FAILED,
                       bump_attempts=True, now=1.0)
    c = cp.upsert_task(c, task_id="fw", chunk_id=0, status=cp.TASK_DONE,
                       bump_attempts=True, now=2.0)
    t = cp.get_task(c, "fw", 0)
    assert t["status"] == cp.TASK_DONE and t["attempts"] == 2
    assert len(c["tasks"]) == 1                       # same key → one entry, no dup


def test_provider_config_blocked_then_resume_keeps_done():
    # contract #4 — blocked → user fixes → resume from checkpoint (not re-upload)
    c = cp.new_checkpoint(now=0.0)
    c = cp.upsert_task(c, task_id="t", chunk_id=0, status=cp.TASK_DONE)
    c = cp.upsert_task(c, task_id="t", chunk_id=1, status=cp.TASK_TRANSIENT_FAILED,
                       error_class="provider_config")
    # Stored checkpoints from the old pipeline can still carry blocked metadata.
    c.update(phase=cp.PHASE_PROVIDER_CONFIG_BLOCKED, resumable=True,
             blocked_reason="402 out of credits")
    assert c["phase"] == cp.PHASE_PROVIDER_CONFIG_BLOCKED
    assert c["resumable"] is True and "402" in c["blocked_reason"]
    c = cp.resume(c, now=6.0)
    assert c["phase"] == cp.PHASE_BACKGROUND_PROCESSING
    assert c.get("resumable") is False and "blocked_reason" not in c
    assert cp.is_task_done(c, "t", 0)                               # done kept
    assert cp.get_task(c, "t", 1)["status"] == cp.TASK_PENDING       # failed re-runs
    assert cp.get_task(c, "t", 1)["error_class"] == ""


def test_upsert_keeps_original_error_for_ops():
    # Codex point 1 — keep 402 vs ReadTimeout vs invalid_json, not just the class
    c = cp.new_checkpoint(now=0.0)
    c = cp.upsert_task(c, task_id="t", chunk_id=0, status=cp.TASK_TRANSIENT_FAILED,
                       error_class="transient_exhausted", error_type="ProviderError",
                       error_message="ReadTimeout on fucheers.top", provider_status_code=None)
    t = cp.get_task(c, "t", 0)
    assert t["error_type"] == "ProviderError"
    assert "ReadTimeout" in t["error_message"]
    assert t["error_class"] == "transient_exhausted"


def test_resume_preserves_stored_legacy_task_fields_without_using_them():
    legacy_fields = {
        "source_ref": "genesis:old:fact:0:abc",
        "candidate_id": "cand_old",
        "written_memory_ids": ["memory_old"],
        "foreground_written": True,
    }
    done = {**legacy_fields, "status": cp.TASK_DONE, "output_summary": "saved"}
    failed = {**legacy_fields, "status": cp.TASK_TRANSIENT_FAILED,
              "error_class": "provider_config", "error_type": "ProviderError",
              "attempts": 2, "output_summary": "retry"}
    stored = {"v": 1, "phase": cp.PHASE_PROVIDER_CONFIG_BLOCKED,
              "tasks": {"old::0": done, "old::1": failed},
              "voice_outputs": {"voice::0": {"behavior_notes_candidates": ["note"]}}}
    original = deepcopy(stored)

    resumed = cp.resume(stored, now=10.0)

    assert resumed["tasks"]["old::0"] == done
    assert resumed["tasks"]["old::1"] == {
        **failed, "status": cp.TASK_PENDING, "error_class": "",
    }
    assert resumed["voice_outputs"] == original["voice_outputs"]
    assert resumed["phase"] == cp.PHASE_BACKGROUND_PROCESSING
    assert stored == original
