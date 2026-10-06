"""Shared encrypted checkpoint helpers for garden voice and identity updates.

The garden import session owns memory-card progress. These helpers retain the
per-window voice/direct-reduce task state and resume semantics used by plaintext
imports. Persistence and encryption stay in service.py. The phase vocabulary is
also used to recognize stored checkpoints and bound legacy-reset trace metadata.
"""
from __future__ import annotations

import time
from typing import Any, Mapping


# Normalization remains shared by the encrypted worker fact reduction path.
_FACT_NORM_MAX = 280


def normalize_fact_text(text: str, *, max_len: int = _FACT_NORM_MAX) -> str:
    """Stable normalization for hashing: trim + lower + collapse whitespace +
    truncate. Same fact phrased with different spacing/case → same string."""
    return " ".join(str(text or "").split()).strip().lower()[:max_len]


# --- job phase state machine -------------------------------------------------
PHASE_FOREGROUND_PROCESSING = "foreground_processing"
PHASE_FOREGROUND_READY = "foreground_ready"               # ← greeting may fire from here
PHASE_BACKGROUND_PROCESSING = "background_processing"
PHASE_PROVIDER_CONFIG_BLOCKED = "provider_config_blocked"  # user must fix key/credits/config
PHASE_TRANSIENT_FAILED_RETRYABLE = "transient_failed_retryable"
PHASE_DONE = "done"
PHASE_FAILED_TERMINAL = "failed_terminal"

PHASES = frozenset({
    PHASE_FOREGROUND_PROCESSING, PHASE_FOREGROUND_READY, PHASE_BACKGROUND_PROCESSING,
    PHASE_PROVIDER_CONFIG_BLOCKED, PHASE_TRANSIENT_FAILED_RETRYABLE,
    PHASE_DONE, PHASE_FAILED_TERMINAL,
})

# --- per-task checkpoint -----------------------------------------------------
# One entry per voice or direct-reduce source window.
TASK_PENDING = "pending"
TASK_DONE = "done"
TASK_TRANSIENT_FAILED = "transient_failed"
TASK_PROVIDER_CONFIG_BLOCKED = "provider_config_blocked"


def task_key(task_id: str, chunk_id: Any) -> str:
    return f"{str(task_id)}::{str(chunk_id)}"


def new_checkpoint(*, now: float | None = None) -> dict:
    ts = float(now if now is not None else time.time())
    return {"v": 1, "phase": PHASE_FOREGROUND_PROCESSING, "tasks": {},
            "created_at": ts, "updated_at": ts}


def _tasks(cp: Mapping[str, Any] | None) -> dict[str, dict]:
    raw = (cp or {}).get("tasks") if isinstance(cp, Mapping) else None
    return dict(raw) if isinstance(raw, Mapping) else {}


def get_task(cp: Mapping[str, Any] | None, task_id: str, chunk_id: Any) -> dict | None:
    t = _tasks(cp).get(task_key(task_id, chunk_id))
    return dict(t) if isinstance(t, Mapping) else None


def is_task_done(cp: Mapping[str, Any] | None, task_id: str, chunk_id: Any) -> bool:
    t = get_task(cp, task_id, chunk_id)
    return bool(t) and str(t.get("status") or "") == TASK_DONE


def upsert_task(
    cp: Mapping[str, Any] | None,
    *,
    task_id: str,
    chunk_id: Any,
    status: str,
    source_pass: str = "",
    output_ref: str = "",
    output_summary: str = "",
    error_class: str = "",
    error_type: str = "",
    error_message: str = "",
    provider_status_code: int | None = None,
    bump_attempts: bool = False,
    now: float | None = None,
) -> dict:
    """Return a copy of `cp` with one task entry upserted. `bump_attempts=True`
    increments the per-task retry counter. Re-writing the same (task_id, chunk_id)
    updates one task entry; memory-card deduplication belongs to the garden session."""
    base = dict(cp) if isinstance(cp, Mapping) else new_checkpoint(now=now)
    tasks = _tasks(base)
    k = task_key(task_id, chunk_id)
    prev = tasks.get(k) if isinstance(tasks.get(k), Mapping) else {}
    attempts = int(prev.get("attempts") or 0) + (1 if bump_attempts else 0)
    tasks[k] = {
        "task_id": str(task_id),
        "chunk_id": str(chunk_id),
        "source_pass": str(source_pass or prev.get("source_pass") or ""),
        "status": str(status),
        "output_ref": str(output_ref or prev.get("output_ref") or ""),
        "output_summary": str(output_summary or prev.get("output_summary") or "")[:240],
        "attempts": attempts,
        "error_class": str(error_class or ""),
        # Codex review: keep the ORIGINAL error so ops can tell 402 vs ReadTimeout vs
        # no-usable-reply vs invalid_json_after_repair — not just transient/provider_config.
        "error_type": str(error_type or ""),
        "error_message": str(error_message or "")[:240],
        "provider_status_code": (int(provider_status_code) if isinstance(provider_status_code, int) else None),
    }
    base["tasks"] = tasks
    base["v"] = 1
    base["updated_at"] = float(now if now is not None else time.time())
    return base


def resume(cp: Mapping[str, Any] | None, *, now: float | None = None) -> dict:
    """Contract #4 — provider fixed → continue from checkpoint. Keeps DONE tasks,
    flips non-done back to pending so they re-run; phase → background_processing."""
    base = dict(cp) if isinstance(cp, Mapping) else new_checkpoint(now=now)
    tasks = _tasks(base)
    for k, t in list(tasks.items()):
        if isinstance(t, Mapping) and str(t.get("status") or "") != TASK_DONE:
            nt = dict(t)
            nt["status"] = TASK_PENDING
            nt["error_class"] = ""
            tasks[k] = nt
    base["tasks"] = tasks
    base["phase"] = PHASE_BACKGROUND_PROCESSING
    base["resumable"] = False
    base.pop("blocked_reason", None)
    base["updated_at"] = float(now if now is not None else time.time())
    return base
