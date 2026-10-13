"""Bounded resident failure *claims*, never independently verified causes.

This module is pure: no storage, provider, authentication or logging calls.
"""
from __future__ import annotations

from dataclasses import dataclass

SOURCE = "agent_initiated_proactive"
ERROR_CLASSES = frozenset({
    "auth_invalid", "quota_insufficient", "model_not_found",
    "provider_account_expired", "rate_limited", "upstream_unavailable",
    "provider_incompatible", "cli_config_invalid", "resident_agent_cli_logged_out",
    "turn_timeout", "content_filtered", "context_overflow", "reply_parse_failed",
    "provider_error_unclassified", "provider_empty_reply", "unknown",
})
STATES = frozenset({"reported", "missing", "invalid", "binding_unavailable", "conflicting_reports"})


def envelope(state: str, error_class: str = "unknown") -> dict:
    return {"v": 1, "reported_error_class": error_class if state == "reported" else "unknown",
            "source": "consumer_reported" if state == "reported" else "none", "state": state}


def normalize_stored(value: object) -> dict:
    if value is None:
        return envelope("missing")
    if (type(value) is not dict or set(value) != {"v", "reported_error_class", "source", "state"}
            or type(value.get("v")) is not int or value["v"] != 1
            or type(value.get("state")) is not str or value["state"] not in STATES
            or type(value.get("reported_error_class")) is not str
            or value["reported_error_class"] not in ERROR_CLASSES):
        return envelope("invalid")
    expected = envelope(value["state"], value["reported_error_class"])
    return expected if value == expected else envelope("invalid")


def _consumer_valid(value: object) -> bool:
    return type(value) is str and 1 <= len(value) <= 160 and value == value.strip()


@dataclass(frozen=True)
class ResidentFailureReportContext:
    candidate_state: str
    candidate_class: str | None
    diagnostic_was_present: bool
    consumer_valid: bool
    consumer_value: str | None


def report_context(payload: dict) -> ResidentFailureReportContext:
    """Inspect original JSON, before the legacy status patch coerces identities."""
    present = "failure_diagnostic" in payload
    value = payload.get("failure_diagnostic")
    valid = (type(value) is dict and set(value) == {"v", "reported_error_class"}
             and type(value.get("v")) is int and value["v"] == 1
             and type(value.get("reported_error_class")) is str
             and value["reported_error_class"] in ERROR_CLASSES)
    consumer = payload.get("consumer_id")
    identity_valid = _consumer_valid(consumer)
    return ResidentFailureReportContext(
        "valid" if valid else "invalid" if present else "missing",
        value["reported_error_class"] if valid else None, present,
        identity_valid, consumer if identity_valid else None,
    )


def sql_inputs(context: object) -> tuple[dict, str | None, bool]:
    """Fail closed on direct store calls, forged mappings, or malformed contexts."""
    if type(context) is not ResidentFailureReportContext:
        return envelope("missing"), None, False
    present = context.diagnostic_was_present is True
    consumer = (context.consumer_value if context.consumer_valid is True
                and _consumer_valid(context.consumer_value) else None)
    if not present:
        candidate = envelope("missing")
    elif (context.candidate_state == "valid" and type(context.candidate_class) is str
          and context.candidate_class in ERROR_CLASSES):
        candidate = envelope("reported", context.candidate_class)
    else:
        candidate = envelope("invalid")
    return candidate, consumer, present


# The CASE runs against the row locked by UPDATE, not job_status's earlier read.
# Every operator tolerates absent/scalar/array legacy JSON. No raw values are cast.
UPDATE_SQL = """
WITH input AS (
    SELECT %s::jsonb AS patch, %s::jsonb AS candidate, %s::text AS consumer,
           %s::boolean AS diagnostic_present, %s::jsonb AS classes
)
UPDATE user_logs AS target
SET doc = target.doc || i.patch || jsonb_build_object('failure_diagnostic', (
    SELECT CASE
      WHEN target.doc->>'source' IS DISTINCT FROM 'agent_initiated_proactive'
        OR (CASE WHEN i.patch ? 'status' THEN i.patch->>'status'
                 ELSE target.doc->>'status' END) IS DISTINCT FROM 'failed' THEN 'null'::jsonb
      WHEN i.patch ? 'consumer_id'
        AND i.patch->'consumer_id' IS DISTINCT FROM target.doc->'consumer_id'
        THEN '{"v":1,"reported_error_class":"unknown","source":"none","state":"binding_unavailable"}'::jsonb
      WHEN NOT (i.patch ? 'status') THEN
        CASE WHEN i.patch ? 'status_reason' OR i.patch ? 'consumer_id' OR i.diagnostic_present
          THEN '{"v":1,"reported_error_class":"unknown","source":"none","state":"missing"}'::jsonb
          ELSE normalized.old_diag END
      WHEN target.doc->>'status' = 'failed' AND normalized.old_diag->>'state' = 'conflicting_reports'
        THEN normalized.old_diag
      WHEN i.candidate->>'state' IN ('missing', 'invalid') THEN i.candidate
      WHEN i.consumer IS NULL OR jsonb_typeof(target.doc->'consumer_id') IS DISTINCT FROM 'string'
        OR length(target.doc->>'consumer_id') NOT BETWEEN 1 AND 160
        OR target.doc->>'consumer_id' IS DISTINCT FROM i.consumer
        OR COALESCE(target.doc->>'status', '') NOT IN ('claimed', 'realizing', 'failed')
        THEN '{"v":1,"reported_error_class":"unknown","source":"none","state":"binding_unavailable"}'::jsonb
      WHEN target.doc->>'status' = 'failed' AND normalized.old_diag->>'state' = 'reported'
        AND normalized.old_diag->>'reported_error_class' IS DISTINCT FROM i.candidate->>'reported_error_class'
        THEN '{"v":1,"reported_error_class":"unknown","source":"none","state":"conflicting_reports"}'::jsonb
      ELSE i.candidate END
    FROM LATERAL (
      SELECT CASE
        WHEN target.doc->'failure_diagnostic' IS NULL OR target.doc->'failure_diagnostic' = 'null'::jsonb
          THEN '{"v":1,"reported_error_class":"unknown","source":"none","state":"missing"}'::jsonb
        WHEN jsonb_typeof(target.doc->'failure_diagnostic') = 'object'
          AND target.doc->'failure_diagnostic'->>'v' = '1'
          AND target.doc->'failure_diagnostic'->'v' = '1'::jsonb
          AND i.classes ? (target.doc->'failure_diagnostic'->>'reported_error_class')
          AND target.doc->'failure_diagnostic' = jsonb_build_object(
            'v', 1,
            'reported_error_class', CASE WHEN target.doc->'failure_diagnostic'->>'state' = 'reported'
              THEN target.doc->'failure_diagnostic'->>'reported_error_class' ELSE 'unknown' END,
            'source', CASE WHEN target.doc->'failure_diagnostic'->>'state' = 'reported'
              THEN 'consumer_reported' ELSE 'none' END,
            'state', target.doc->'failure_diagnostic'->>'state')
          AND target.doc->'failure_diagnostic'->>'state' IN
            ('reported', 'missing', 'invalid', 'binding_unavailable', 'conflicting_reports')
          THEN target.doc->'failure_diagnostic'
        ELSE '{"v":1,"reported_error_class":"unknown","source":"none","state":"invalid"}'::jsonb
      END AS old_diag
    ) AS normalized
))
FROM input AS i
WHERE target.user_id = %s AND target.stream = %s AND target.seq = (
    SELECT seq FROM user_logs WHERE user_id = %s AND stream = %s AND item_key = %s
    ORDER BY seq DESC LIMIT 1
)
"""
