"""Fail-closed redaction of proactive job ``status_reason`` for display surfaces.

``status_reason`` is externally supplied free text: ``proactive_core``'s
``_job_status_patch`` stores ``payload["reason"][:500]`` from the request body,
and ``db.py`` documents the column as free text. No shape test can separate a
producer code from an opaque identifier — an API key id and a reason code are
made of the same characters — so membership in the producer-owned vocabulary is
the only sound test. Anything not declared by a producer is redacted whole.

Display boundary only: classification upstream still reads the raw reason.
Sanitizing before ``v1_proactive_outcome_class`` would silently reclassify every
reason it matches exactly.
"""
from __future__ import annotations

from notices import catalog, error_contract

REDACTED = "<redacted>"

# Reasons our own code writes as literals, never assembled from upstream text.
# Producers (kept in sync by tests/test_status_reason_redaction.py):
#   proactive/poll_core.py:73,165,214,298   lifecycle terminals
#   proactive/poll_core.py:312              evaluate_wake_control_v2 rejections
#   proactive/proactive_core.py:408         gate.HEARTBEAT_THROTTLED_REASON
#   proactive/capture_jobs.py:215           migrate terminal read back by name
#   db.py content_free_failure_code         collapsed-failure placeholder
PROACTIVE_LIFECYCLE_REASONS = frozenset({
    "agent_greeted",
    "ambient_disabled",
    "arrival_wake_disabled",
    "heartbeat_throttled",
    "migrate_no_legacy",
    "photo_wake_disabled",
    "resident_stale_claim_recovered",
    "runtime_failed",
    "scheduled_disabled",
    "screen_watch_disabled",
    "stale_wake_expired",
    "superseded_by_newer",
    "unlock_wake_disabled",
})

# The resident consumer is the other large producer for this column: it writes
# through ``/v1/proactive/jobs/{id}/status``, the same endpoint that stores
# ``payload["reason"]`` raw, so nothing downstream can tell it apart from any
# other caller. Only the leading segment is declared here — these producers
# append exception text to it (``_agent_call_failed_reason`` deliberately
# appends ``detail[:400]``, which is how a provider error body reaches this
# column at all), so the tail is exactly what has to go. The bucket survives.
# Producers (kept in sync by tests/test_status_reason_redaction.py):
#   tools/chat_resident_consumer.py  update_proactive_job_status(..., <reason>)
RESIDENT_CONSUMER_REASONS = frozenset({
    "agent_call_failed",
    "agent_reply_parse_failed",
    "agent_request_broadcast",
    "agent_scheduled_wake_actions",
    "agent_sleep",
    "capture_agent_call_failed",
    "capture_invalid_memory_action",
    "capture_memory_actions_applied",
    "capture_memory_actions_failed",
    "capture_memory_write_failed",
    "capture_window_build_failed",
    "capture_window_unavailable",
    "chat_collision",
    "coalesced_into",
    "degenerate_reply_suppressed",
    "dream_agent_call_failed",
    "dream_invalid_memory_action",
    "dream_memory_actions_applied",
    "dream_memory_actions_failed",
    "dream_memory_actions_partial",
    "dream_memory_write_failed",
    "dream_no_cards_available",
    "dream_nothing_to_consolidate",
    "empty_agent_reply",
    "introduction_identity_action_failed",
    "legacy_batch_unavailable",
    "memory_identity_action_failed",
    "migrate_agent_call_failed",
    "migrate_batch_done",
    "nothing_worth_keeping",
    "proactive_backoff",
    "provider_payment_required",
    "scheduled_wake_actions_failed",
    "thinking_only_silence",
})

# Returned as the parser ``err`` by ``memgarden.prompts.{capture,dream,migrate}``
# and written straight into this column by the consumer's capture/dream/migrate
# lanes. These live in a pinned wheel, not in this repo, so the drift guard
# cannot watch them the way it watches the sets above: a memgarden bump may add
# a parser error that renders as ``<redacted>`` with nothing going red first.
MEMORY_PARSER_REASONS = frozenset({
    "json_decode_error",
    "missing_cards_list",
    "missing_consolidations_list",
    "missing_upgrades_list",
    "no_json_object",
    "not_an_object",
    "too_many_cards",
})

# Derived from the registries that already own this keyspace, so a newly
# registered error class is displayable without editing this module. Runtime
# V2's PUBLIC_FAILURE_CODES is deliberately absent: ``status_reason`` is the
# resident/V1 keyspace and jobs_store.py forbids merging the two. A V2-shaped
# reason arriving here collapses to the redacted bucket with its count intact.
SANCTIONED_REASON_SEGMENTS = frozenset(
    {spec.code for spec in error_contract.all_specs()}
    | {
        segment
        for reason in (
            catalog.USER_UNAVAILABLE_V1_REASONS
            | catalog.USER_UNAVAILABLE_V2_OUTCOME_CODES
        )
        for segment in reason.split(":")
    }
    | PROACTIVE_LIFECYCLE_REASONS
    | RESIDENT_CONSUMER_REASONS
    | MEMORY_PARSER_REASONS
)


def sanitize_status_reason(raw: object) -> str:
    """Keep the sanctioned prefix of a ``:``-joined reason chain, redact the rest.

    Keeping the sanctioned prefix rather than dropping the whole value is what
    makes this redaction instead of deletion: ``auth_invalid:<redacted>`` still
    buckets with its siblings, while the tail that carried a provider error body
    is gone. An unsanctioned first segment yields ``<redacted>`` alone.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    kept: list[str] = []
    for segment in text.split(":"):
        token = segment.strip()
        if token not in SANCTIONED_REASON_SEGMENTS:
            kept.append(REDACTED)
            break
        kept.append(token)
    return ":".join(kept)


def sanitize_reason_counts(counts: dict) -> dict[str, int]:
    """Sanitize reason keys, summing the raw keys that collapse into one.

    Summing is the point, not a detail: an embedded error body made every
    occurrence its own bucket, so one failure appeared as N buckets of 1 and the
    aggregation could not be read at all.
    """
    merged: dict[str, int] = {}
    for reason, count in (counts or {}).items():
        key = sanitize_status_reason(reason) or "unknown"
        merged[key] = merged.get(key, 0) + int(count or 0)
    return merged
