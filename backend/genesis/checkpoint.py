"""Genesis v2 Step 2 — job phase state machine + per-task checkpoint (the contract).

⚠️ LIVE-USE STATUS (2026-07-01, self-audit + Codex): the live plaintext orchestration
in routes.py ONLY uses the dedup anchors here — `normalize_fact_text` /
`make_candidate_id` / `make_source_ref`. The phase/task/resume STATE MACHINE
(`new_checkpoint` / `upsert_task` / `runnable_tasks` / `set_phase` /
`mark_provider_config_blocked` / `resume`) is NOT wired into production yet — it is the
foundation for Step 4 (resumable background continuation) and currently lives only in
its own tests + the e2e reference impl. Kept on purpose (don't rebuild it later); just
don't mistake it for a shipped capability.

PURE, side-effect-free helpers (no DB / LLM) so they unit-test in isolation. The
worker/routes own persistence — store this dict in the genesis job blob
`genesis_checkpoint:{job_id}`. Step 3 (foreground reducer) and Step 4 (background
continuation) drive it through these helpers; they must NOT reach past this contract.

Bakes in the 4 v2 safety contracts (CC + Codex, 2026-06-30) so Step 3/4 can't
"forget" them:

  1. **fg/bg + live-capture 双写** → append + dedup, never replace-all. Background
     continuation only ever `memory.add` / `memory.supersede`; every genesis card
     carries a stable `source_ref` / `candidate_id`; resolve/dedup before writing,
     **including cards the live capture lane just wrote.**
  2. **前台 core vs 后台 full 去重** → foreground-written refs/ids are recorded;
     background full extraction skips/supersedes them instead of re-adding.
  3. **greeting 挂 FOREGROUND_READY,不等 DONE** → `greeting_allowed(phase)`.
  4. **provider_config_blocked 可 resume** → `resume()` keeps done tasks, redoes the
     rest; never re-uploads the whole import.
"""
from __future__ import annotations

import hashlib
import time
from typing import Any, Iterable, Mapping

CHECKPOINT_BLOB_PREFIX = "genesis_checkpoint"  # blob key = f"{prefix}:{job_id}"


# --- stable candidate_id / source_ref (Codex rule #2: the dedup anchor) -------
# THE critical dedup rule: foreground and background must derive the SAME id for
# the SAME fact, or contract #1/#2 dedup drifts and a card gets written twice.
# So the id is a pure hash of stable inputs and lives HERE (one place), never
# generated ad-hoc in foreground/background. Deliberately EXCLUDES bucket / thread
# / importance / pulse — those drift between passes; the fact text does not.
_FACT_NORM_MAX = 280


def normalize_fact_text(text: str, *, max_len: int = _FACT_NORM_MAX) -> str:
    """Stable normalization for hashing: trim + lower + collapse whitespace +
    truncate. Same fact phrased with different spacing/case → same string."""
    return " ".join(str(text or "").split()).strip().lower()[:max_len]


def make_candidate_id(
    *, user_id: str, job_id: str, source_family: str, source_pass: str,
    chunk_index: Any, fact_text: str,
) -> str:
    """Deterministic candidate id. Same (user, job, family, pass, chunk, fact) →
    same id, whether foreground writes it first or background re-scans it."""
    raw = "\x1f".join([
        str(user_id), str(job_id), str(source_family), str(source_pass),
        str(chunk_index), normalize_fact_text(fact_text),
    ])
    return "cand_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def make_source_ref(*, job_id: str, source_pass: str, chunk_index: Any, candidate_id: str) -> str:
    """Human/ops-readable locator carrying the candidate hash; pairs with candidate_id."""
    cand_hash = str(candidate_id or "").split("_")[-1][:16]
    return f"genesis:{job_id}:{source_pass}:{chunk_index}:{cand_hash}"


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

# Once foreground is ready the user can enter + be greeted; later phases keep that.
_GREETING_OK = frozenset({
    PHASE_FOREGROUND_READY, PHASE_BACKGROUND_PROCESSING,
    PHASE_PROVIDER_CONFIG_BLOCKED, PHASE_DONE,
})


def greeting_allowed(phase: str | None) -> bool:
    """Contract #3 — greeting fires once foreground baseline exists, not at DONE."""
    return str(phase or "") in _GREETING_OK


# --- per-task checkpoint -----------------------------------------------------
# One entry per genesis task × chunk: voice_map / fact_map / fact_write / persona_build.
TASK_PENDING = "pending"
TASK_PROCESSING = "processing"
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
    source_ref: str = "",
    candidate_id: str = "",
    written_memory_ids: Iterable[str] = (),
    foreground_written: bool = False,
    bump_attempts: bool = False,
    now: float | None = None,
) -> dict:
    """Return a copy of `cp` with one task entry upserted. `bump_attempts=True`
    increments the per-task retry counter (for the transient cap). Idempotent on
    re-write of the SAME (task_id, chunk_id) — does NOT create duplicate cards;
    the caller dedups card *content* via the *_refs helpers before writing."""
    base = dict(cp) if isinstance(cp, Mapping) else new_checkpoint(now=now)
    tasks = _tasks(base)
    k = task_key(task_id, chunk_id)
    prev = tasks.get(k) if isinstance(tasks.get(k), Mapping) else {}
    attempts = int(prev.get("attempts") or 0) + (1 if bump_attempts else 0)
    ids = sorted({str(i) for i in (list(prev.get("written_memory_ids") or []) + list(written_memory_ids)) if str(i).strip()})
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
        "source_ref": str(source_ref or prev.get("source_ref") or ""),
        "candidate_id": str(candidate_id or prev.get("candidate_id") or ""),
        "written_memory_ids": ids,
        "foreground_written": bool(foreground_written or prev.get("foreground_written") or False),
    }
    base["tasks"] = tasks
    base["v"] = 1
    base["updated_at"] = float(now if now is not None else time.time())
    return base


def pending_tasks(cp: Mapping[str, Any] | None) -> list[dict]:
    """Tasks NOT done → what a resume re-runs (contract #4). Done tasks are skipped
    so a single failure never re-runs the whole import."""
    return [dict(t) for t in _tasks(cp).values()
            if isinstance(t, Mapping) and str(t.get("status") or "") != TASK_DONE]


# Phases in which the worker may actually run pending tasks. Critically NOT
# provider_config_blocked: pending_tasks() lists blocked tasks too (so resume can
# pick them up), but the worker loop must NOT auto-run them — it has to wait for
# resume() to flip the phase back. (Codex review point 2.)
_RUNNABLE_PHASES = frozenset({PHASE_FOREGROUND_PROCESSING, PHASE_BACKGROUND_PROCESSING})


def runnable_tasks(cp: Mapping[str, Any] | None) -> list[dict]:
    """pending_tasks(), but only when the phase permits running. Returns [] while
    provider_config_blocked / done / failed_terminal, so the worker can't process
    pending work on a blocked job — it must call resume() first."""
    phase = str((cp or {}).get("phase") or "") if isinstance(cp, Mapping) else ""
    if phase not in _RUNNABLE_PHASES:
        return []
    return pending_tasks(cp)


# --- dedup refs (contracts #1 / #2) ------------------------------------------

def written_refs(cp: Mapping[str, Any] | None) -> set[str]:
    """All source_ref/candidate_id already written (any task) — strong-dedup key set."""
    out: set[str] = set()
    for t in _tasks(cp).values():
        # Only a task that actually finished writing counts as "already written" —
        # a pending/failed task (incl. one flipped back to pending by resume()) must
        # NOT block re-processing its candidate.
        if not isinstance(t, Mapping) or str(t.get("status") or "") != TASK_DONE:
            continue
        for key in ("source_ref", "candidate_id"):
            v = str(t.get(key) or "").strip()
            if v:
                out.add(v)
    return out


def foreground_written_refs(cp: Mapping[str, Any] | None) -> set[str]:
    """Refs the FOREGROUND already wrote — background full extraction must skip these
    (contract #2: don't re-add the 3-5 core cards)."""
    out: set[str] = set()
    for t in _tasks(cp).values():
        if (not isinstance(t, Mapping) or not t.get("foreground_written")
                or str(t.get("status") or "") != TASK_DONE):
            continue
        for key in ("source_ref", "candidate_id"):
            v = str(t.get(key) or "").strip()
            if v:
                out.add(v)
    return out


def all_written_memory_ids(cp: Mapping[str, Any] | None) -> set[str]:
    out: set[str] = set()
    for t in _tasks(cp).values():
        if isinstance(t, Mapping):
            out.update(str(i) for i in (t.get("written_memory_ids") or []) if str(i).strip())
    return out


def should_skip_candidate(cp: Mapping[str, Any] | None, *, source_ref: str = "", candidate_id: str = "") -> bool:
    """Strong dedup: True if this candidate was already written (by foreground OR an
    earlier background task). Lexical/semantic weak-dedup is layered on by the caller."""
    refs = written_refs(cp)
    return (str(source_ref or "").strip() in refs and bool(str(source_ref or "").strip())) or \
           (str(candidate_id or "").strip() in refs and bool(str(candidate_id or "").strip()))


# --- phase transitions -------------------------------------------------------

def set_phase(cp: Mapping[str, Any] | None, phase: str, *, now: float | None = None) -> dict:
    if phase not in PHASES:
        raise ValueError(f"unknown genesis phase: {phase!r}")
    base = dict(cp) if isinstance(cp, Mapping) else new_checkpoint(now=now)
    base["phase"] = phase
    base["updated_at"] = float(now if now is not None else time.time())
    return base


def mark_provider_config_blocked(cp: Mapping[str, Any] | None, *, reason: str = "", now: float | None = None) -> dict:
    """Contract #4 — user must fix provider; job is resumable, not failed_terminal."""
    base = set_phase(cp, PHASE_PROVIDER_CONFIG_BLOCKED, now=now)
    base["resumable"] = True
    if reason:
        base["blocked_reason"] = str(reason)[:240]
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
