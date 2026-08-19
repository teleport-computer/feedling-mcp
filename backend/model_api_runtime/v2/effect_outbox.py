"""Ownership/generation-fenced effect applier (spec A4). Pure of sinks: dispatch
is injected so this module never imports hosted/capabilities. Each pending row
is handled in its own transaction that locks the user's v2_runtime_state row
FOR UPDATE, so a concurrent cutover cannot slip between the authoritative
``state == v2`` + generation check and the apply.
"""
from __future__ import annotations
from dataclasses import dataclass
import json
import logging
import math
import os
from typing import Callable
import db
from model_api_runtime.v2 import effect_id as _effect_id


log = logging.getLogger("feedling.v2.effect_outbox")

_LEGACY_SENSITIVE_EFFECT_TYPES = frozenset({"memory", "identity", "schedule"})
_SCRUBBED_LEGACY_PAYLOAD = json.dumps({"legacy_payload_scrubbed": True})
_PENDING_EFFECT_STATUSES = frozenset({"pending", "pending_fenced_v1"})
# Non-sensitive routing metadata beside the encrypted final-reply envelope.
# It is removed before dispatch, so reply sinks keep their narrow payload
# contract and never need to know about the agent-job admission protocol.
FINAL_REPLY_FENCE_KEY = "_final_reply_fence"
REPLY_SOURCE_FENCE_KEY = "_reply_source_fence"
PROMOTED_INTERMEDIATE_EFFECT_ID_KEY = "_promoted_intermediate_effect_id"
PERCEPTION_GLANCE_FINGERPRINT_KEY = "_perception_glance_fingerprint"
FINAL_REPLY_EFFECT_TYPE = "reply_final_fenced_v1"
TERMINAL_REPLY_EFFECT_TYPE = "reply_terminal_fenced_v1"
INTERMEDIATE_REPLY_EFFECT_TYPE = "reply_intermediate_fenced_v1"
# 「这次尝试有没有产出用户可见的气泡」的**唯一**判据来源。
#
# ⚠️ 与「最终送达」是两套谓词,不可互换:jobs_store 的 chat 送达率只认
# {reply_final_fenced_v1, reply_terminal_fenced_v1}(裸 'reply' 是旧词汇,
# 可能只是个中间气泡,混进去会让送达率超过 100%)。本集合刻意更宽——中间
# 气泡对用户同样是「它说话了」,说话率要的正是这个宽口径。
REPLY_EFFECT_TYPES = frozenset(
    {
        "reply",
        FINAL_REPLY_EFFECT_TYPE,
        TERMINAL_REPLY_EFFECT_TYPE,
        INTERMEDIATE_REPLY_EFFECT_TYPE,
    }
)
_REPLY_EFFECT_TYPES = REPLY_EFFECT_TYPES
_REPLY_SOURCE_LANES = frozenset(
    {"chat", "heartbeat", "scheduled", "manual_wake", "screen_watch"}
)
FINAL_REPLY_SUPERSEDED_EFFECT_IDS_KEY = "final_reply_superseded_effect_ids"
FINALIZED_JOB_IDS_KEY = "finalized_job_ids"
FINAL_REPLY_INPUT_ADVANCED = "input_generation_advanced"
FINAL_REPLY_INVALID_FENCE = "invalid_final_reply_fence"
FINAL_REPLY_SOURCE_JOB_INACTIVE = "source_job_not_active"
FINAL_REPLY_VOICE_CALL_ENDED = "voice_call_ended"
WAKE_REPLY_CHAT_COLLISION = "wake_chat_collision"
EFFECT_RUNTIME_STATE_CHANGED = "runtime_state_not_v2"
EFFECT_RUNTIME_GENERATION_CHANGED = "runtime_generation_advanced"
APPLIED_RESULT_PAYLOAD_KEY = "_applied_result_v1"
APPLIED_WITH_RESULTS_STATUS = "applied_with_results"
# 送达成立的两个终态。``applied_with_results`` 是复合 sink 带确定性逐项拒绝时的
# 父状态——投递**已经完成**(见 WorkspaceBatchAppliedResult 的说明),漏掉它会把
# 已经说过话的回合算成沉默。
DELIVERED_EFFECT_STATUSES = frozenset({"applied", APPLIED_WITH_RESULTS_STATUS})
WORKSPACE_BATCH_RESULT_KIND = "workspace_batch_v1"
WORKSPACE_BATCH_RESULT_MAX_ITEMS = 24
SCHEDULE_RESULT_KIND = "schedule_v1"
WORKSPACE_BATCH_TERMINAL_ERRORS = frozenset(
    {
        "workspace_write_failed",
        "workspace_delete_failed",
        "workspace_revision_conflict",
    }
)
_APPLIED_RESULT_MAX_BYTES = 16_384
_SAFE_EFFECT_ID_PUNCTUATION = frozenset(":_-.")
_COLLISION_WAKE_KINDS = frozenset(
    {"heartbeat", "manual_wake", "screen_watch"}
)
_COLLISION_PROACTIVE_SOURCE = "agent_initiated_proactive"
_COLLISION_CLOCK_SKEW_SEC = 5.0


def _collision_window_sec() -> float:
    raw = os.environ.get("PROACTIVE_CHAT_COLLISION_WINDOW_SEC", "90")
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "PROACTIVE_CHAT_COLLISION_WINDOW_SEC must be finite"
        ) from exc
    if not math.isfinite(value):
        raise RuntimeError(
            "PROACTIVE_CHAT_COLLISION_WINDOW_SEC must be finite"
        )
    return max(0.0, value)


def _wake_reply_chat_collision(cur, *, user_id: str, payload: dict) -> bool:
    """Check the V1 post-time collision rule on the publication transaction.

    Scheduled reminders remain exempt: they are user-requested deliveries and
    V1 deliberately posts them even during an active conversation. Existing V2
    wake rows are also excluded so the gate does not become a wake-spacing
    mechanism; self-wake and delivery guards own that policy.
    """
    wake_kind = str(payload.get("wake_kind") or "").strip().lower()
    if wake_kind not in _COLLISION_WAKE_KINDS:
        return False
    window = _collision_window_sec()
    if window <= 0:
        return False
    cur.execute(
        "SELECT 1 FROM chat_messages "
        "WHERE user_id=%s AND ts>=(EXTRACT(EPOCH FROM clock_timestamp())-%s) "
        "AND ts<=(EXTRACT(EPOCH FROM clock_timestamp())+%s) "
        "AND COALESCE(doc->>'source','') "
        "NOT IN ('verify_ping','resident_maintenance') "
        "AND ("
        "  doc->>'role' IN ('user','human') "
        "  OR ("
        "    doc->>'role' IN ('openclaw','assistant','agent','model') "
        "    AND COALESCE(doc->>'source','')<>%s "
        "    AND COALESCE(doc->>'wake_kind','')=''"
        "  )"
        ") LIMIT 1",
        (
            str(user_id),
            window,
            _COLLISION_CLOCK_SKEW_SEC,
            _COLLISION_PROACTIVE_SOURCE,
        ),
    )
    return cur.fetchone() is not None


@dataclass(frozen=True)
class WorkspaceBatchAppliedResult:
    """A successful sink outcome that carries non-sensitive structured results.

    Delivery is complete even when a composite sink reports deterministic
    per-item rejections. Such a parent uses ``applied_with_results`` so an older
    producer fails visibly instead of mistaking partial success for all-success.
    The result replaces the encrypted request payload, letting a current
    producer recover exact terminal truth without retaining plaintext.
    """

    result: dict
    status: str = "applied"


@dataclass(frozen=True)
class ScheduleAppliedResult:
    """Bounded, non-sensitive truth returned by the scheduled-wake sink."""

    result: dict
    status: str = "applied"


def _validate_applied_result(value: WorkspaceBatchAppliedResult) -> None:
    result = value.result
    if not isinstance(result, dict) or set(result) != {"kind", "items"}:
        raise RuntimeError("applied effect result shape is invalid")
    if result.get("kind") != WORKSPACE_BATCH_RESULT_KIND:
        raise RuntimeError("applied effect result kind is invalid")
    items = result.get("items")
    if (
        not isinstance(items, list)
        or not 1 <= len(items) <= WORKSPACE_BATCH_RESULT_MAX_ITEMS
    ):
        raise RuntimeError("applied effect result items are invalid")
    has_discarded = False
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("applied effect child result is invalid")
        effect_id = item.get("effect_id")
        if (
            not isinstance(effect_id, str)
            or not 1 <= len(effect_id) <= 256
            or not all(
                char.isascii()
                and (char.isalnum() or char in _SAFE_EFFECT_ID_PUNCTUATION)
                for char in effect_id
            )
        ):
            raise RuntimeError("applied effect child identity is invalid")
        status = item.get("status")
        if status == "applied":
            keys = set(item)
            if keys == {"effect_id", "status"}:
                continue
            revision = item.get("revision")
            if (
                keys == {"effect_id", "status", "revision"}
                and type(revision) is int
                and 1 <= revision <= 9_223_372_036_854_775_807
            ):
                continue
        if (
            status == "discarded"
            and set(item) == {"effect_id", "status", "error"}
            and item.get("error") in WORKSPACE_BATCH_TERMINAL_ERRORS
        ):
            has_discarded = True
            continue
        raise RuntimeError("applied effect child status is invalid")
    expected_status = APPLIED_WITH_RESULTS_STATUS if has_discarded else "applied"
    if value.status != expected_status:
        raise RuntimeError("applied effect result status is inconsistent")


def _validate_schedule_applied_result(value: ScheduleAppliedResult) -> None:
    result = value.result
    allowed = {
        "kind",
        "operation",
        "task_id",
        "status",
        "next_trigger_at",
        "timezone",
    }
    if not isinstance(result, dict) or set(result) - allowed:
        raise RuntimeError("applied schedule result shape is invalid")
    if result.get("kind") != SCHEDULE_RESULT_KIND:
        raise RuntimeError("applied schedule result kind is invalid")
    if result.get("operation") not in {"schedule_wake", "cancel_wake"}:
        raise RuntimeError("applied schedule operation is invalid")
    task_id = result.get("task_id")
    if task_id is not None and (
        not isinstance(task_id, str)
        or len(task_id) > 200
        or any(
            not char.isascii()
            or not (char.isalnum() or char in "_.:-")
            for char in task_id
        )
    ):
        raise RuntimeError("applied schedule task id is invalid")
    if result.get("status") not in {
        "scheduled",
        "canceled",
        "not_found",
        "invalid",
        "rejected",
    }:
        raise RuntimeError("applied schedule status is invalid")
    next_trigger = result.get("next_trigger_at")
    if next_trigger is not None and (
        not isinstance(next_trigger, str)
        or not 1 <= len(next_trigger) <= 80
        or any(char not in "0123456789-:T.+ " for char in next_trigger)
    ):
        raise RuntimeError("applied schedule next trigger is invalid")
    timezone_name = result.get("timezone")
    if timezone_name is not None and (
        not isinstance(timezone_name, str)
        or not 1 <= len(timezone_name) <= 80
        or any(
            not char.isascii()
            or not (char.isalnum() or char in "_+./-")
            for char in timezone_name
        )
    ):
        raise RuntimeError("applied schedule timezone is invalid")
    if value.status != "applied":
        raise RuntimeError("applied schedule result status is inconsistent")


def _serialized_applied_result(value: object) -> tuple[str, str] | None:
    if not isinstance(value, (WorkspaceBatchAppliedResult, ScheduleAppliedResult)):
        # Dispatch was historically a void callback. Preserve compatibility
        # with adapters that happen to return an incidental value; only the
        # explicit wrapper opts into durable structured results.
        return None
    if isinstance(value, WorkspaceBatchAppliedResult):
        _validate_applied_result(value)
    else:
        _validate_schedule_applied_result(value)
    rendered = json.dumps(
        {APPLIED_RESULT_PAYLOAD_KEY: value.result},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(rendered.encode("utf-8")) > _APPLIED_RESULT_MAX_BYTES:
        raise RuntimeError("applied effect result exceeds the durable size limit")
    return rendered, value.status


def _sanitized_dispatch_error(exc: Exception) -> str:
    """Return an operator-useful error that cannot contain payload secrets."""
    if isinstance(exc, db.EffectDeliveryUncertainError):
        return "delivery_uncertain: unresolved sink claim requires reconciliation"
    return f"dispatch_failed:{type(exc).__name__}"


def enqueue_effect(
    *,
    job_id,
    user_id,
    effect_type,
    ordinal,
    expected_generation,
    payload,
    input_frontier_seq: int | None = None,
) -> str:
    """Producer-side entry to the generation-fenced outbox (spec C5 / PR A A4). Derives the
    deterministic effect_id, enqueues (ON CONFLICT DO NOTHING = retry-idempotent), returns the id.
    PR C's tool loop is the first caller; the already-wired apply_pending_effects drains it."""
    eid = _effect_id.derive(job_id=job_id, effect_type=effect_type, ordinal=ordinal)
    db.effect_enqueue(
        eid,
        user_id,
        job_id,
        effect_type,
        expected_generation,
        payload,
        input_frontier_seq=input_frontier_seq,
    )
    return eid


def enqueue_reply_promotion(
    *,
    intermediate_effect_id: str,
    job_id,
    user_id,
    expected_generation,
    payload,
) -> str:
    """Enqueue the deterministic terminal half of an intermediate reply.

    The source intermediate effect, rather than an in-process ordinal, is the
    durable idempotency key.  This keeps promotion stable across lease recovery.
    """
    eid = _effect_id.derive_reply_promotion(
        intermediate_effect_id=intermediate_effect_id,
    )
    db.effect_enqueue(
        eid,
        user_id,
        job_id,
        FINAL_REPLY_EFFECT_TYPE,
        expected_generation,
        payload,
    )
    return eid


def _exact_nonnegative_json_int(value) -> int | None:
    """Accept only a JSON integer, never a coercible bool/float/string."""
    if type(value) is not int or value < 0:
        return None
    return value


_MISSING = object()


def _pop_perception_glance_fingerprint(
    payload: dict,
) -> tuple[str | None, bool]:
    """Remove internal terminal metadata and validate exact SHA-256 hex."""
    value = payload.pop(PERCEPTION_GLANCE_FINGERPRINT_KEY, _MISSING)
    if value is _MISSING:
        return None, True
    if (
        type(value) is not str
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        return None, False
    return value, True


def _lock_active_reply_source_job(
    cur,
    *,
    user_id: str,
    job_id,
    expected_claimed_by: str | None,
):
    if job_id is None:
        return None
    cur.execute(
        "SELECT user_id,input_generation,status,claimed_by,lease_expires_at,"
        "lane,priority FROM agent_jobs WHERE id=%s FOR UPDATE",
        (job_id,),
    )
    job = cur.fetchone()
    lease_valid = False
    if job is not None and job[4] is not None:
        # Evaluate only after FOR UPDATE returns; transaction-stable now() can
        # otherwise bless a lease that expired while this statement waited.
        cur.execute(
            "SELECT %s::timestamptz > clock_timestamp()",
            (job[4],),
        )
        lease_valid = bool(cur.fetchone()[0])
    if (
        job is None
        or str(job[0]) != str(user_id)
        or str(job[2]) != "running"
        or str(job[5]) not in _REPLY_SOURCE_LANES
        or not str(job[3] or "")
        or (
            expected_claimed_by is not None
            and str(job[3]) != expected_claimed_by
        )
        or not lease_valid
    ):
        return None
    return job


def _reply_fence_matches(
    cur,
    *,
    user_id: str,
    job_id,
    effect_type: str,
    payload: dict,
) -> tuple[str | None, str | None, int | None, str]:
    """Pop and atomically validate a versioned reply's source/frontier.

    The V2 send transaction persists the user row and increments the active
    chat job's ``input_generation`` together.  Holding that job row lock until
    after dispatch gives the final reply a real linearization point against a
    concurrent send:

    * send commits first -> this check observes the newer generation/seq and
      the stale reply is discarded without reaching its sink;
    * this check locks first -> the send waits until dispatch has durably
      published the reply, so the new message is ordered after it.

    ``through_seq`` is defense in depth and also proves that the exact message
    frontier in the prompt did not lag the admission counter.  The metadata is
    plaintext by design (owner id plus two integers only); reply content remains
    encrypted.
    """
    has_final_frontier = "reply_through_seq" in payload
    final_fence = payload.pop(FINAL_REPLY_FENCE_KEY, None)
    source_fence = payload.pop(REPLY_SOURCE_FENCE_KEY, None)
    if effect_type not in _REPLY_EFFECT_TYPES:
        if final_fence is not None or source_fence is not None:
            return FINAL_REPLY_INVALID_FENCE, None, None, "ordinary"
        return None, None, None, "ordinary"

    if effect_type == INTERMEDIATE_REPLY_EFFECT_TYPE:
        if (
            has_final_frontier
            or final_fence is not None
            or not isinstance(source_fence, dict)
            or set(source_fence) != {"claimed_by"}
            or type(source_fence.get("claimed_by")) is not str
            or not source_fence["claimed_by"]
        ):
            return FINAL_REPLY_INVALID_FENCE, None, None, "transactional"
        job = _lock_active_reply_source_job(
            cur,
            user_id=user_id,
            job_id=job_id,
            expected_claimed_by=source_fence["claimed_by"],
        )
        if job is None:
            return FINAL_REPLY_SOURCE_JOB_INACTIVE, None, None, "transactional"
        if _wake_reply_chat_collision(cur, user_id=user_id, payload=payload):
            return WAKE_REPLY_CHAT_COLLISION, str(job[3]), int(job[1] or 0), (
                "transactional"
            )
        return None, None, None, "transactional"

    if effect_type == TERMINAL_REPLY_EFFECT_TYPE:
        if (
            has_final_frontier
            or source_fence is not None
            or not isinstance(final_fence, dict)
            or set(final_fence)
            != {"claimed_by", "input_generation", "observed_user_seq"}
            or type(final_fence.get("claimed_by")) is not str
            or not final_fence["claimed_by"]
            or _exact_nonnegative_json_int(
                final_fence.get("input_generation")
            ) is None
            or _exact_nonnegative_json_int(
                final_fence.get("observed_user_seq")
            ) is None
        ):
            return FINAL_REPLY_INVALID_FENCE, None, None, "terminal"
        job = _lock_active_reply_source_job(
            cur,
            user_id=user_id,
            job_id=job_id,
            expected_claimed_by=final_fence["claimed_by"],
        )
        if job is None:
            return FINAL_REPLY_SOURCE_JOB_INACTIVE, None, None, "terminal"
        if _wake_reply_chat_collision(cur, user_id=user_id, payload=payload):
            return (
                WAKE_REPLY_CHAT_COLLISION,
                str(job[3]),
                int(job[1] or 0),
                "terminal",
            )
        expected_input_generation = int(final_fence["input_generation"])
        if int(job[1] or 0) != expected_input_generation:
            return (
                FINAL_REPLY_INPUT_ADVANCED,
                str(job[3]),
                int(job[1] or 0),
                "terminal",
            )
        observed_user_seq = int(final_fence["observed_user_seq"])
        cur.execute(
            "SELECT 1 FROM chat_messages WHERE user_id=%s AND seq>%s "
            "AND doc->>'role' IN ('user','human') LIMIT 1",
            (user_id, observed_user_seq),
        )
        if cur.fetchone() is not None:
            return (
                FINAL_REPLY_INPUT_ADVANCED,
                str(job[3]),
                int(job[1] or 0),
                "terminal",
            )
        return (
            None,
            str(job[3]),
            expected_input_generation,
            "terminal",
        )

    is_legacy_final = (
        effect_type == "reply"
        and has_final_frontier
        and final_fence is None
        and isinstance(payload.get("envelope"), dict)
    )
    if effect_type == "reply" and not has_final_frontier:
        # Plaintext rows predate source fencing and remain on the old idempotent
        # compatibility sink. Current/pre encrypted rows are fenced against a
        # failed source job and use the one-connection reply path.
        if final_fence is not None or source_fence is not None:
            return FINAL_REPLY_INVALID_FENCE, None, None, "ordinary"
        if not isinstance(payload.get("envelope"), dict):
            return None, None, None, "ordinary"
        job = _lock_active_reply_source_job(
            cur, user_id=user_id, job_id=job_id, expected_claimed_by=None)
        if job is None:
            return FINAL_REPLY_SOURCE_JOB_INACTIVE, None, None, "transactional"
        if _wake_reply_chat_collision(cur, user_id=user_id, payload=payload):
            return WAKE_REPLY_CHAT_COLLISION, str(job[3]), int(job[1] or 0), (
                "transactional"
            )
        return None, None, None, "transactional"

    if effect_type == FINAL_REPLY_EFFECT_TYPE or (
        effect_type == "reply"
        and has_final_frontier
        and isinstance(final_fence, dict)
    ):
        if source_fence is not None or not isinstance(final_fence, dict):
            return FINAL_REPLY_INVALID_FENCE, None, None, "final"
        fence = final_fence
    elif is_legacy_final:
        # origin/pre already emitted encrypted reply+frontier rows without a
        # fence. Adopt only a still-live source job and the exact old frontier;
        # any newer input rejects the candidate and hands work to a successor.
        if final_fence is not None or source_fence is not None:
            return FINAL_REPLY_INVALID_FENCE, None, None, "legacy_final"
        fence = None
    else:
        return FINAL_REPLY_INVALID_FENCE, None, None, "ordinary"

    preserve_queued_input = False
    if fence is None:
        expected_claimed_by = None
        expected_input_generation = None
        through_seq = _exact_nonnegative_json_int(
            payload.get("reply_through_seq"))
    else:
        fence_keys = set(fence)
        preserve_queued_input = fence.get("preserve_queued_input") is True
        if fence_keys not in (
            {"claimed_by", "input_generation", "through_seq"},
            {
                "claimed_by",
                "input_generation",
                "through_seq",
                "preserve_queued_input",
            },
        ) or ("preserve_queued_input" in fence and not preserve_queued_input):
            return FINAL_REPLY_INVALID_FENCE, None, None, "final"
        expected_claimed_by = fence.get("claimed_by")
        expected_input_generation = _exact_nonnegative_json_int(
            fence.get("input_generation"))
        through_seq = _exact_nonnegative_json_int(fence.get("through_seq"))
    payload_through_seq = _exact_nonnegative_json_int(
        payload.get("reply_through_seq"))
    if (
        through_seq is None
        or payload_through_seq is None
        or through_seq != payload_through_seq
        or (
            fence is not None
            and (
                type(expected_claimed_by) is not str
                or not expected_claimed_by
                or expected_input_generation is None
            )
        )
    ):
        return FINAL_REPLY_INVALID_FENCE, None, None, (
            "legacy_final" if is_legacy_final else "final")
    job = _lock_active_reply_source_job(
        cur,
        user_id=user_id,
        job_id=job_id,
        expected_claimed_by=expected_claimed_by,
    )
    mode = "legacy_final" if is_legacy_final else "final"
    if job is None:
        return FINAL_REPLY_SOURCE_JOB_INACTIVE, None, None, mode
    if _wake_reply_chat_collision(cur, user_id=user_id, payload=payload):
        return (
            WAKE_REPLY_CHAT_COLLISION,
            str(job[3]),
            int(job[1] or 0),
            mode,
        )
    if (
        expected_input_generation is not None
        and int(job[1] or 0) != expected_input_generation
        and not preserve_queued_input
    ):
        return (
            FINAL_REPLY_INPUT_ADVANCED,
            str(job[3]),
            int(job[1] or 0),
            mode,
        )

    # A corrupt frontier beyond the newest actual user input would let the
    # compound reply sink advance the durable cursor past future messages. The
    # running chat job guarantees at least its triggering input is still in the
    # retained tail; zero remains valid only for compatibility/test fixtures.
    cur.execute(
        "SELECT COALESCE(MAX(seq),0) FROM chat_messages "
        "WHERE user_id=%s AND doc->>'role' IN ('user','human')",
        (user_id,),
    )
    newest_input_seq = int(cur.fetchone()[0] or 0)
    if through_seq > newest_input_seq:
        return FINAL_REPLY_INVALID_FENCE, None, None, mode

    # A user row beyond the exact prompt frontier must independently veto the
    # reply. This catches any future producer that persists input correctly but
    # forgets to bump the admission counter.
    cur.execute(
        "SELECT 1 FROM chat_messages "
        "WHERE user_id=%s AND seq>%s "
        "AND doc->>'role' IN ('user','human') LIMIT 1",
        (user_id, through_seq),
    )
    if cur.fetchone() is not None and not preserve_queued_input:
        return (
            FINAL_REPLY_INPUT_ADVANCED,
            str(job[3]),
            int(job[1] or 0),
            mode,
        )
    return None, str(job[3]), int(job[1] or 0), mode


def _complete_final_reply_job_on_cursor(
    cur,
    *,
    user_id: str,
    job_id,
    claimed_by: str,
    input_generation: int,
    perception_glance_fingerprint: str | None = None,
) -> int:
    """Consume the exact source job as part of final-reply publication.

    The fence validator already owns this job row. Repeating every identity and
    ownership predicate here makes a future refactor fail closed rather than
    turning a reply from the wrong job/owner into a successful completion.
    """
    cur.execute(
        "UPDATE agent_jobs SET status='completed',finished_at=now() "
        "WHERE id=%s AND user_id=%s "
        "AND lane IN ('chat','heartbeat','scheduled','manual_wake','screen_watch') "
        "AND status='running' "
        "AND claimed_by=%s AND input_generation=%s RETURNING id,lane",
        (
            job_id,
            user_id,
            claimed_by,
            int(input_generation),
        ),
    )
    row = cur.fetchone()
    if row is None or cur.rowcount != 1:
        raise RuntimeError("final reply source job completion lost ownership")
    if perception_glance_fingerprint is not None:
        if str(row[1]) != "heartbeat":
            raise RuntimeError(
                "perception glance fingerprint source is not a heartbeat"
            )
        from model_api_runtime.v2 import jobs_store

        jobs_store._merge_completed_perception_glance_on_cursor(
            cur,
            user_id=str(user_id),
            source_job_id=int(job_id),
            fingerprint=perception_glance_fingerprint,
        )
    # This is the same normal-success clear previously performed by
    # jobs_store.finish_chat_job. A proactive/wake success must not erase an
    # unresolved failed-chat chip, so only chat-source publication owns it.
    if str(row[1]) == "chat":
        cur.execute(
            "UPDATE model_api_routes SET last_runtime_error='',"
            "last_runtime_error_class='',updated_at=now() "
            "WHERE user_id=%s AND is_active",
            (user_id,),
        )
    return int(row[0])


def _handoff_legacy_chat_final_on_cursor(
    cur,
    *,
    user_id: str,
    job_id,
    claimed_by: str | None,
    runtime_generation: int,
) -> int | None:
    """Regenerate an unfenced old-pre final that lost to newer user input.

    Old producers do not understand the superseded disposition. Completing
    their still-owned chat row and inserting one successor in this transaction
    prevents them from reporting success with no reply while preserving the
    unconsumed cursor for the successor. Wake jobs already have a send-created
    chat job and need no lifecycle handoff here.
    """
    from model_api_runtime.v2 import jobs_store

    if not claimed_by:
        return None
    cur.execute(
        "UPDATE agent_jobs SET status='completed',finished_at=now(),"
        "last_error='legacy_final_regeneration' "
        "WHERE id=%s AND user_id=%s AND lane='chat' AND status='running' "
        "AND claimed_by=%s RETURNING priority",
        (job_id, user_id, claimed_by),
    )
    row = cur.fetchone()
    if row is None:
        return None
    cur.execute(
        "INSERT INTO agent_jobs "
        "(user_id,lane,status,reason,priority,queue_deadline_at,"
        " expected_runtime_generation) "
        "VALUES (%s,'chat','pending','legacy_final_regeneration',%s,"
        "now() + make_interval(secs => %s),%s) RETURNING id",
        (
            user_id,
            int(row[0]),
            float(jobs_store.PENDING_CHAT_TTL_SEC),
            int(runtime_generation),
        ),
    )
    return int(cur.fetchone()[0])


def get_effect_disposition(
    effect_id: str,
    *,
    user_id: str,
    job_id,
    effect_type: str = "reply",
) -> dict | None:
    """Return the authoritative persisted disposition of one exact effect.

    An independent sweeper may win the row before the producing worker's drain.
    A per-call list of rows changed by that drain is therefore not a delivery
    acknowledgement.  The producer uses this identity-checked read after the
    drain and accepts only the effect type's explicit applied status as
    successful publication.
    """
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT user_id,job_id,effect_type,status,last_error,payload "
            "FROM v2_effect_outbox WHERE effect_id=%s FOR UPDATE",
            (str(effect_id),),
        ).fetchone()
    if row is None:
        return None
    if (
        str(row[0]) != str(user_id)
        or row[1] != job_id
        or str(row[2]) != str(effect_type)
    ):
        raise RuntimeError("effect identity mismatch")
    disposition = {
        "status": str(row[3]),
        "last_error": str(row[4] or ""),
    }
    stored_payload = row[5]
    if isinstance(stored_payload, str):
        stored_payload = json.loads(stored_payload)
    if (
        disposition["status"] in {"applied", APPLIED_WITH_RESULTS_STATUS}
        and isinstance(stored_payload, dict)
        and set(stored_payload) == {APPLIED_RESULT_PAYLOAD_KEY}
    ):
        result = stored_payload[APPLIED_RESULT_PAYLOAD_KEY]
        if not isinstance(result, dict):
            raise RuntimeError("persisted applied effect result is invalid")
        disposition["result"] = result
    return disposition


def last_promotable_intermediate_reply(
    *,
    user_id: str,
    job_id,
) -> dict | None:
    """Return the newest durable text bubble not terminally rejected for promotion.

    The outbox row is the recovery record: process-local state may disappear
    after the bubble is visible but before the provider closes the turn.  File
    and image cards carry ``message_extra`` and are intentionally ineligible;
    this promotion contract is for the chat ``reply`` text tool only.
    """
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT source.effect_id,source.payload "
            "FROM v2_effect_outbox source "
            "WHERE source.user_id=%s AND source.job_id=%s "
            "AND source.effect_type=%s AND source.status='applied' "
            "AND source.payload ? 'envelope' "
            "AND NOT (source.payload ? 'message_extra') "
            "AND NOT EXISTS ("
            " SELECT 1 FROM v2_effect_outbox promotion "
            " WHERE promotion.user_id=source.user_id "
            " AND promotion.job_id=source.job_id "
            " AND promotion.effect_type=%s "
            " AND promotion.status='discarded' "
            " AND promotion.payload->>%s=source.effect_id"
            ") ORDER BY source.enqueue_seq DESC LIMIT 1",
            (
                str(user_id),
                job_id,
                INTERMEDIATE_REPLY_EFFECT_TYPE,
                FINAL_REPLY_EFFECT_TYPE,
                PROMOTED_INTERMEDIATE_EFFECT_ID_KEY,
            ),
        ).fetchone()
    if row is None:
        return None
    effect_id = str(row[0] or "").strip()
    payload = row[1]
    if isinstance(payload, str):
        payload = json.loads(payload)
    if (
        not effect_id
        or not isinstance(payload, dict)
        or not isinstance(payload.get("envelope"), dict)
        or "reply_through_seq" in payload
        or FINAL_REPLY_FENCE_KEY in payload
    ):
        raise RuntimeError("invalid promotable intermediate reply")
    return {"effect_id": effect_id, "payload": payload}


def apply_pending_effects(
    user_id: str,
    *,
    dispatch: Callable[[str, dict], object],
    dispatch_reply_in_transaction: Callable | None = None,
) -> dict:
    applied = discarded = 0
    superseded_final_effect_ids: list[str] = []
    finalized_job_ids: list[int] = []
    for row in db.effect_pending(user_id, due_prefix_only=True):
        eid = row["effect_id"]
        dispatch_failed = False
        dispatch_error_committed = False
        deferred_dispatch_error: Exception | None = None
        terminal_discard = False
        serialized_applied_result: tuple[str, str] | None = None
        finalized_job_id_in_transaction: int | None = None
        post_commit_reply: Callable[[], None] | None = None
        try:
            with db.get_pool().connection() as conn:
                # Pool connections are autocommit=True. Without this explicit
                # transaction the FOR UPDATE lock was released immediately after the
                # SELECT, allowing cutover to race between generation validation and
                # dispatch.
                error_staged_in_transaction = False
                with conn.transaction():
                    with conn.cursor() as cur:
                        # Shared across ordinary operations and nested sink
                        # connections; account deletion takes the exclusive form.
                        # Taking it before runtime-state closes the delete-vs-
                        # outer-state-vs-inner-lifecycle deadlock cycle.
                        db._lock_chat_user_fence_on_cursor(cur, user_id)
                        # Be robust to an effect enqueued before any runtime
                        # state read initialized this user.  Initializing and
                        # locking in the same transaction closes that first-
                        # cutover edge as well.
                        cur.execute(
                            "INSERT INTO v2_runtime_state (user_id) "
                            "SELECT %s WHERE EXISTS ("
                            "  SELECT 1 FROM users u WHERE u.user_id=%s"
                            ") ON CONFLICT (user_id) DO NOTHING",
                            (user_id, user_id),
                        )
                        cur.execute(
                            "SELECT hosted_runtime_state, runtime_generation "
                            "FROM v2_runtime_state "
                            "WHERE user_id=%s FOR UPDATE", (user_id,))
                        control = cur.fetchone()
                        current_state = str(control[0]) if control else "resident"
                        current = int(control[1]) if control else 0
                        # effect_pending() is only an ordered work-list hint.
                        # Re-read all dispatch fields while locking the row so
                        # a concurrent applier cannot leave us using a stale
                        # snapshot after it terminalized this effect.
                        cur.execute(
                            "SELECT effect_type, expected_generation, payload, status, "
                            "       job_id, "
                            "       next_attempt_at <= now() AS is_due "
                            "FROM v2_effect_outbox WHERE effect_id=%s FOR UPDATE",
                            (eid,))
                        effect = cur.fetchone()
                        if not effect or str(effect[3]) not in _PENDING_EFFECT_STATUSES:
                            continue
                        if not bool(effect[5]):
                            # Another applier may have failed this ordered head
                            # after our work-list snapshot. Do not bypass its
                            # retry delay or any effect queued behind it.
                            break
                        (
                            effect_type,
                            expected_generation,
                            payload,
                            _status,
                            job_id,
                            _is_due,
                        ) = effect
                        # Holding this row lock across dispatch makes an
                        # in-flight V2 sink finish before cutover can commit;
                        # cutover bumps the generation, so late effects are
                        # discarded without touching a sink.
                        if (
                            current_state == "v2"
                            and int(expected_generation) == current
                        ):
                            try:
                                if isinstance(payload, str):
                                    payload = json.loads(payload)
                                if not isinstance(payload, dict):
                                    raise RuntimeError(
                                        "effect payload must be an object"
                                    )
                                # Never mutate the stored dict in place.
                                payload = dict(payload)
                            except RuntimeError as exc:
                                deferred_dispatch_error = exc
                            except Exception:
                                deferred_dispatch_error = RuntimeError(
                                    "invalid effect payload"
                                )
                            final_reply_discard_reason = None
                            final_reply_claimed_by = None
                            final_reply_input_generation = None
                            perception_glance_fingerprint = None
                            perception_glance_fingerprint_valid = True
                            reply_mode = "ordinary"
                            if deferred_dispatch_error is None:
                                (
                                    perception_glance_fingerprint,
                                    perception_glance_fingerprint_valid,
                                ) = _pop_perception_glance_fingerprint(payload)
                                try:
                                    (
                                        final_reply_discard_reason,
                                        final_reply_claimed_by,
                                        final_reply_input_generation,
                                        reply_mode,
                                    ) = (
                                        _reply_fence_matches(
                                            cur,
                                            user_id=user_id,
                                            job_id=job_id,
                                            effect_type=str(effect_type),
                                            payload=payload,
                                        )
                                    )
                                except Exception as exc:  # trusted metadata, fail closed
                                    deferred_dispatch_error = exc
                            if (
                                deferred_dispatch_error is None
                                and final_reply_discard_reason is None
                                and str(effect_type) in _REPLY_EFFECT_TYPES
                            ):
                                voice_call_id = str(
                                    payload.get("voice_call_id") or ""
                                ).strip()
                                if voice_call_id and db.voice_call_status_on_cursor(
                                    cur, user_id, voice_call_id, lock=True
                                ) in {"finalizing", "cancelled", "finalized"}:
                                    final_reply_discard_reason = (
                                        FINAL_REPLY_VOICE_CALL_ENDED
                                    )
                            if (
                                deferred_dispatch_error is None
                                and final_reply_discard_reason is None
                                and not perception_glance_fingerprint_valid
                            ):
                                final_reply_discard_reason = (
                                    FINAL_REPLY_INVALID_FENCE
                                )
                            if (
                                deferred_dispatch_error is None
                                and final_reply_discard_reason is None
                                and perception_glance_fingerprint is not None
                            ):
                                if (
                                    reply_mode not in {"final", "terminal"}
                                    or str(effect_type)
                                    not in {
                                        FINAL_REPLY_EFFECT_TYPE,
                                        TERMINAL_REPLY_EFFECT_TYPE,
                                    }
                                ):
                                    final_reply_discard_reason = (
                                        FINAL_REPLY_INVALID_FENCE
                                    )
                                else:
                                    cur.execute(
                                        "SELECT lane FROM agent_jobs "
                                        "WHERE id=%s AND user_id=%s "
                                        "AND claimed_by=%s",
                                        (
                                            job_id,
                                            user_id,
                                            final_reply_claimed_by,
                                        ),
                                    )
                                    glance_source = cur.fetchone()
                                    if (
                                        glance_source is None
                                        or str(glance_source[0]) != "heartbeat"
                                    ):
                                        final_reply_discard_reason = (
                                            FINAL_REPLY_INVALID_FENCE
                                        )
                            if (
                                deferred_dispatch_error is None
                                and final_reply_discard_reason is not None
                            ):
                                if (
                                    final_reply_discard_reason
                                    == FINAL_REPLY_VOICE_CALL_ENDED
                                    and reply_mode in {"final", "legacy_final"}
                                ):
                                    candidate_job_id = (
                                        _complete_final_reply_job_on_cursor(
                                            cur,
                                            user_id=user_id,
                                            job_id=job_id,
                                            claimed_by=final_reply_claimed_by,
                                            input_generation=(
                                                final_reply_input_generation
                                            ),
                                            perception_glance_fingerprint=(
                                                perception_glance_fingerprint
                                            ),
                                        )
                                    )
                                    finalized_job_id_in_transaction = (
                                        candidate_job_id
                                    )
                                    persisted_runtime_doc = (
                                        db._advance_blob_int_on_cursor(
                                            cur,
                                            user_id,
                                            "model_api_runtime",
                                            "v2_reply_cursor_seq",
                                            int(
                                                payload.get(
                                                    "reply_through_seq"
                                                )
                                                or 0
                                            ),
                                        )
                                    )
                                    post_commit_reply = lambda doc=(
                                        persisted_runtime_doc
                                    ): db._mirror_persisted_blob(
                                        user_id,
                                        "model_api_runtime",
                                        doc,
                                    )
                                if (
                                    reply_mode == "legacy_final"
                                    and final_reply_discard_reason
                                    == FINAL_REPLY_INPUT_ADVANCED
                                ):
                                    _handoff_legacy_chat_final_on_cursor(
                                        cur,
                                        user_id=user_id,
                                        job_id=job_id,
                                        claimed_by=final_reply_claimed_by,
                                        runtime_generation=current,
                                    )
                                cur.execute(
                                    "UPDATE v2_effect_outbox SET status='discarded', "
                                    "last_error=%s,last_attempt_at=now() "
                                    "WHERE effect_id=%s AND status IN "
                                    "('pending','pending_fenced_v1')",
                                    (final_reply_discard_reason, eid),
                                )
                                discarded += 1
                                superseded_final_effect_ids.append(eid)
                                terminal_discard = True
                            if (
                                deferred_dispatch_error is None
                                and final_reply_discard_reason is None
                            ):
                                payload["effect_id"] = eid
                                try:
                                    logical_effect_type = (
                                        "reply"
                                        if str(effect_type) in _REPLY_EFFECT_TYPES
                                        else str(effect_type)
                                    )
                                    with conn.transaction():
                                        if reply_mode in {
                                            "final",
                                            "terminal",
                                            "legacy_final",
                                        }:
                                            candidate_job_id = (
                                                _complete_final_reply_job_on_cursor(
                                                    cur,
                                                    user_id=user_id,
                                                    job_id=job_id,
                                                    claimed_by=final_reply_claimed_by,
                                                    input_generation=(
                                                        final_reply_input_generation
                                                    ),
                                                    perception_glance_fingerprint=(
                                                        perception_glance_fingerprint
                                                    ),
                                                )
                                            )
                                            finalized_job_id_in_transaction = (
                                                candidate_job_id
                                            )
                                        if reply_mode in {
                                            "transactional",
                                            "final",
                                            "terminal",
                                            "legacy_final",
                                        }:
                                            if dispatch_reply_in_transaction is None:
                                                if isinstance(
                                                    payload.get("envelope"), dict
                                                ):
                                                    raise RuntimeError(
                                                        "transactional reply sink is not wired"
                                                    )
                                                # Test/ancient plaintext adapter.
                                                # Current encrypted producers
                                                # always fail closed above.
                                                dispatch(
                                                    logical_effect_type,
                                                    payload,
                                                )
                                                post_commit_candidate = None
                                            else:
                                                post_commit_candidate = (
                                                    dispatch_reply_in_transaction(
                                                        logical_effect_type,
                                                        payload,
                                                        conn,
                                                    )
                                                )
                                            if (
                                                post_commit_candidate is not None
                                                and not callable(post_commit_candidate)
                                            ):
                                                raise RuntimeError(
                                                    "transactional reply sink returned "
                                                    "invalid post-commit hook"
                                                )
                                        else:
                                            with db._chat_user_fence_held_by_outer_transaction(
                                                user_id
                                            ):
                                                dispatch_result = dispatch(
                                                    logical_effect_type,
                                                    payload,
                                                )
                                                serialized_applied_result = (
                                                    _serialized_applied_result(
                                                        dispatch_result
                                                    )
                                                )
                                            post_commit_candidate = None

                                        post_commit_reply = post_commit_candidate
                                except Exception as exc:
                                    deferred_dispatch_error = exc
                                    finalized_job_id_in_transaction = None
                                    post_commit_reply = None
                            if deferred_dispatch_error is not None:
                                dispatch_failed = True
                                # Persist retry/manual-reconciliation state in
                                # the SAME transaction that still owns the
                                # effect row lock. Releasing the lock first
                                # would let a concurrent sweeper replay an
                                # ambiguously delivered effect before its
                                # backoff/terminal marker became visible.
                                if isinstance(
                                    deferred_dispatch_error, db.EffectTerminalError
                                ):
                                    # Deterministic, non-retryable failure: the
                                    # write provably did not land, so retrying it
                                    # forever only wedges the conversation. Mark it
                                    # discarded (terminal) and DON'T re-raise — this
                                    # is a handled outcome, not a delivery failure.
                                    cur.execute(
                                        "UPDATE v2_effect_outbox SET status='discarded', "
                                        "last_error=%s, last_attempt_at=now() "
                                        "WHERE effect_id=%s AND status IN "
                                        "('pending','pending_fenced_v1')",
                                        (
                                            _sanitized_dispatch_error(
                                                deferred_dispatch_error
                                            ),
                                            eid,
                                        ),
                                    )
                                    discarded += 1
                                    terminal_discard = True
                                else:
                                    db._effect_record_error_on_cursor(
                                        cur,
                                        eid,
                                        _sanitized_dispatch_error(
                                            deferred_dispatch_error
                                        ),
                                        reconciliation_required=isinstance(
                                            deferred_dispatch_error,
                                            db.EffectDeliveryUncertainError,
                                        ),
                                    )
                                error_staged_in_transaction = True
                            if (
                                deferred_dispatch_error is None
                                and final_reply_discard_reason is None
                            ):
                                if serialized_applied_result is not None:
                                    result_payload, result_status = (
                                        serialized_applied_result
                                    )
                                    cur.execute(
                                        "UPDATE v2_effect_outbox SET status=%s, "
                                        "applied_at=now(), last_error='', payload=%s::jsonb "
                                        "WHERE effect_id=%s",
                                        (result_status, result_payload, eid),
                                    )
                                elif effect_type in _LEGACY_SENSITIVE_EFFECT_TYPES:
                                    cur.execute(
                                        "UPDATE v2_effect_outbox SET status='applied', "
                                        "applied_at=now(), last_error='', payload=%s::jsonb "
                                        "WHERE effect_id=%s",
                                        (_SCRUBBED_LEGACY_PAYLOAD, eid),
                                    )
                                else:
                                    cur.execute(
                                        "UPDATE v2_effect_outbox SET status='applied', applied_at=now(), "
                                        "last_error='' WHERE effect_id=%s", (eid,))
                                applied += 1
                        else:
                            discard_reason = (
                                EFFECT_RUNTIME_STATE_CHANGED
                                if current_state != "v2"
                                else EFFECT_RUNTIME_GENERATION_CHANGED
                            )
                            if effect_type in _LEGACY_SENSITIVE_EFFECT_TYPES:
                                cur.execute(
                                    "UPDATE v2_effect_outbox SET status='discarded', "
                                    "last_error=%s,last_attempt_at=now(), "
                                    "payload=%s::jsonb WHERE effect_id=%s",
                                    (
                                        discard_reason,
                                        _SCRUBBED_LEGACY_PAYLOAD,
                                        eid,
                                    ),
                                )
                            else:
                                cur.execute(
                                    "UPDATE v2_effect_outbox SET status='discarded', "
                                    "last_error=%s,last_attempt_at=now() "
                                    "WHERE effect_id=%s",
                                    (discard_reason, eid),
                                )
                            discarded += 1
                # Reaching here means the transaction commit succeeded.
                if finalized_job_id_in_transaction is not None:
                    finalized_job_ids.append(finalized_job_id_in_transaction)
                if post_commit_reply is not None:
                    try:
                        post_commit_reply()
                    except Exception:  # committed primary outcome is authoritative
                        log.exception(
                            "[v2.effect_outbox] reply post-commit hook failed "
                            "effect=%s",
                            eid,
                        )
                dispatch_error_committed = error_staged_in_transaction
                if deferred_dispatch_error is not None and not terminal_discard:
                    # A terminal discard is a handled outcome (row already marked
                    # 'discarded' above); only a retryable/uncertain failure
                    # propagates so the sweeper keeps the row visible.
                    raise deferred_dispatch_error
        except Exception as exc:
            if dispatch_failed and not dispatch_error_committed:
                try:
                    db.effect_record_error(
                        eid,
                        _sanitized_dispatch_error(exc),
                        reconciliation_required=isinstance(
                            exc, db.EffectDeliveryUncertainError),
                    )
                except Exception:  # preserve the original delivery failure
                    log.exception(
                        "[v2.effect_outbox] failed to persist dispatch error effect=%s",
                        eid,
                    )
            raise
    result = {"applied": applied, "discarded": discarded}
    if superseded_final_effect_ids:
        # Additive only on the exceptional late-input path, preserving the
        # exact two-key result used by existing callers/tests in the common
        # case while letting the producing loop identify its own rejected id.
        result[FINAL_REPLY_SUPERSEDED_EFFECT_IDS_KEY] = (
            superseded_final_effect_ids)
    if finalized_job_ids:
        result[FINALIZED_JOB_IDS_KEY] = finalized_job_ids
    return result
