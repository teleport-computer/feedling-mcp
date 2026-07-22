"""Shared one-shot post-spawn introduction machinery.

Extracted so BOTH paths that can open a fresh resident's first greeting enqueue
the SAME introduction through ONE atomic enqueue-once entry — never a parallel
field or a second state machine:

- ``agent_runtime.supervisor`` — spawn / autoverify recovery path, gated on
  ``proactive_activation_ready()`` (a user who has had a real conversation).
- ``chat.chat_core`` — the resident ``chat_loop_verified`` fast-path, which is
  what actually breaks the fresh-resident deadlock: a brand-new resident user
  cannot send a real message until the greeting opens Chat, and ``verify_loop``
  deliberately does NOT count as the user's First message, so gating the
  greeting on ``first_chat_ok_at`` wedges it forever.

The durable dedup marker stays ``store.introduced_at`` / ``introduction_done()``
(unchanged from the existing system — no ``introduction_enqueued_at`` parallel
field). This module only adds the atomic claim + rollback so the two paths never
double-send and the marker never says "introduced" without an actual job.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from core import util

log = logging.getLogger("feedling.introduction")

INTRODUCTION_JOB_KIND = "introduction"
INTRODUCTION_TRIGGER = "post_spawn_genesis"
INTRODUCTION_INTENT_LABEL = "post_respawn_introduction"
_INTRODUCTION_ACTIVE_STATUSES = {"pending", "claimed", "realizing"}


def _build_introduction_job(*, now: float) -> dict:
    job_id = util._new_public_id("pj")
    return {
        "job_id": job_id,
        "schema_version": 2,
        "ts": float(now),
        "created_at": datetime.fromtimestamp(float(now)).isoformat(),
        "wake_id": job_id,
        "source": "agent_initiated_proactive",
        "status": "pending",
        "intent_label": INTRODUCTION_INTENT_LABEL,
        "context_hint": "",
        "connections": [],
        "connection": {},
        "frame_ids": [],
        "device_event_ids": [],
        "current_app": "",
        "trigger": INTRODUCTION_TRIGGER,
        "job_kind": INTRODUCTION_JOB_KIND,
        "manual": False,
        "forced": False,
        "user_state": "",
        "ai_state": "",
        "broadcast_state": "",
        "wake_kind": "introduction",
        "screen_context_available": False,
        "agent_action": "",
        "agent_action_status": "",
    }


def _has_active_introduction_job(store) -> bool:
    try:
        jobs = store.list_proactive_jobs(since_epoch=0, limit=0)
    except Exception as e:  # noqa: BLE001
        log.warning("introduction active-job scan failed for %s: %s", getattr(store, "user_id", ""), e)
        return True
    for job in jobs or []:
        if str((job or {}).get("job_kind") or "").strip() != INTRODUCTION_JOB_KIND:
            continue
        status = str((job or {}).get("status") or "pending").strip()
        if status in _INTRODUCTION_ACTIVE_STATUSES:
            return True
    return False


def enqueue_introduction_once(store, *, now=None) -> dict | None:
    """Enqueue EXACTLY ONE introduction job for this user.

    Trigger-agnostic: the CALLER decides *when* (supervisor: a recovered
    activation; chat_core: ``chat_loop_verified``). Exactly-once across both
    triggers AND across processes is enforced authoritatively by the SINGLE DB
    transaction in ``store.claim_and_enqueue_introduction`` (guarded marker
    UPSERT + job INSERT together — see Codex P1); a lost race or a rolled-back
    job write simply returns ``None`` and leaves no orphan marker.

    The two checks below are only cheap, non-authoritative fast-path skips so we
    don't build a job or hit the DB when the user is already, visibly done. Two
    callers that both pass them still yield exactly one job.

    Returns the enqueued job dict, or ``None`` when nothing was enqueued
    (already introduced, one already in flight, or this caller lost the claim).
    """
    if store.introduction_done():
        return None
    if _has_active_introduction_job(store):
        return None
    clock = (now() if callable(now) else float(now)) if now is not None else time.time()
    return store.claim_and_enqueue_introduction(_build_introduction_job(now=clock))
