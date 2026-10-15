"""Typed operation boundaries for plaintext automatic recall (T820).

These values contain no credentials. Ownership errors must never be converted
into an optional recall failure by a caller's broad exception handler.
"""
from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass


class RecallFailure(RuntimeError):
    """Required content, authorization or provenance could not be verified."""

    def __init__(self, reason: str, *, category: str = "content_read_failed"):
        super().__init__(reason)
        self.reason = reason
        self.category = category


class RecallUnavailable(RuntimeError):
    """An isolated optional dependency failed after the required proofs."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def exact_mode(value, *, missing: bool = False) -> str:
    if missing:
        return "unknown"
    if not isinstance(value, str):
        raise RecallFailure("account_mode_invalid", category="binding_error")
    return value if value in {"off", "on"} else "unknown"


def owner(row, user_id: str) -> None:
    if (not isinstance(row, dict) or not isinstance(user_id, str) or not user_id
            or not isinstance(row.get("owner_user_id"), str)
            or row["owner_user_id"] != user_id):
        raise RecallFailure("owner_unconfirmed", category="binding_error")


@dataclass(frozen=True)
class ValidatedTurnContent:
    user_id: str
    through_seq: int
    message_id: str
    original_json: str
    projected_json: str
    attachment_hashes_json: str = "[]"
    required_references_json: str = "[]"

    @property
    def original(self) -> list:
        return json.loads(self.original_json)

    @property
    def projected(self) -> list:
        return json.loads(self.projected_json)


@dataclass(frozen=True)
class OwnerValidatedCandidates:
    user_id: str
    rows_json: str

    @property
    def rows(self) -> list:
        return json.loads(self.rows_json)


def candidate_batch(rows, user_id: str) -> OwnerValidatedCandidates:
    if not isinstance(rows, list):
        raise RecallFailure("candidate_batch_invalid", category="binding_error")
    ids = set()
    for row in rows:
        owner(row, user_id)
        mid = row.get("id")
        if not isinstance(mid, str) or not mid or mid in ids:
            raise RecallFailure("candidate_identity_invalid", category="binding_error")
        ids.add(mid)
    try:
        serialized = json.dumps(rows, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RecallFailure("candidate_metadata_invalid", category="binding_error") from exc
    return OwnerValidatedCandidates(user_id, serialized)


def unavailable_payload(reason: str, turn: ValidatedTurnContent) -> dict:
    return {
        "error": "recall_unavailable", "category": "recall_unavailable",
        "detail": reason, "user_id": turn.user_id,
        "seq": turn.through_seq, "message_id": turn.message_id,
        "required_attachments": json.loads(turn.attachment_hashes_json),
        "required_references": json.loads(turn.required_references_json),
        "context_memories": [],
        "context_memory_log": {"mode": "unavailable", "reason": reason,
                               "counts": {"selected": None, "candidate_pool": None,
                                          "injected": 0}},
    }


@dataclass(frozen=True)
class TurnRowProof:
    user_id: str
    message_id: str
    seq: int
    content: str
    excluded: bool = False


def validate_prompt_rows(rows, user_id, through_seq, *, required_ids=()):
    """Accept only reader-created proofs, not a nonempty prompt or wire flag."""
    required = set(required_ids)
    seen = set()
    sources = {}
    for row in rows:
        proof = row.get("_recall_row_proof") if isinstance(row, dict) else None
        if (not isinstance(proof, TurnRowProof) or proof.user_id != user_id
                or proof.message_id != row.get("id")
                or proof.seq != row.get("seq") or proof.seq > through_seq
                or str(row.get("content") or "").strip() != proof.content.strip()):
            raise RecallFailure("turn_content_unconfirmed", category="binding_error")
        if proof.excluded and proof.message_id in required:
            raise RecallFailure("unreadable_current_message")
        if row.get("unreadable") and proof.message_id in required:
            raise RecallFailure("unreadable_current_message")
        source = row.get("_recall_source_json")
        if proof.message_id in sources and sources[proof.message_id] != source:
            raise RecallFailure("turn_version_changed", category="binding_error")
        sources[proof.message_id] = source
        seen.add(proof.message_id)
    if not required.issubset(seen):
        raise RecallFailure("turn_content_unconfirmed", category="binding_error")


# Context-local operation mode is copied into asyncio.to_thread readers. It is
# never a process cache and is reset when the owned job finishes.
_OPERATION_MODE = ContextVar("recall_operation_mode", default=None)


def bind_operation_mode(user_id, value):
    return _OPERATION_MODE.set((user_id, exact_mode(value)))


def reset_operation_mode(token):
    _OPERATION_MODE.reset(token)


def checked_operation_mode(user_id, value):
    mode = exact_mode(value)
    bound = _OPERATION_MODE.get()
    if bound is not None and bound != (user_id, mode):
        raise RecallFailure("account_mode_changed", category="binding_error")
    return mode


def operation_mode(user_id):
    bound = _OPERATION_MODE.get()
    if bound is None:
        return None
    if bound[0] != user_id:
        raise RecallFailure("account_subject_changed", category="binding_error")
    return bound[1]
