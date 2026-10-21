"""Bounded effective-off recall stages; no enclave fallback is available here."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace

from core import plaintext_row
from memory import plaintext_recall, recall_attachments, recall_candidates, recall_contract, recall_projection, recall_references, recall_select
from memory.embedding import recall_policy


@dataclass(frozen=True)
class DecodedCandidates:
    user_id: str
    moments_json: str
    cards_json: str
    inner_json: str
    sealed: int


def decode(batch, limit):
    moments = recall_candidates.first_page(batch, limit)
    cards, inner, usable = [], {}, []
    sealed = 0
    for row in moments:
        if row.get("visibility") == "local_only":
            continue
        if row.get("body_ct") or row.get("K_enclave"):
            sealed += 1
            continue
        try:
            raw = plaintext_row.read_plaintext_row(row, batch.user_id, ValueError)
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("card must be an object")
            for field in ("title", "description", "summary", "content", "bucket", "type"):
                if value.get(field) is not None and not isinstance(value[field], str):
                    raise ValueError("invalid card text field")
            if value.get("threads") is not None and not isinstance(value["threads"], (list, str)):
                raise ValueError("invalid card threads field")
            if value.get("roles") is not None and not isinstance(value["roles"], list):
                raise ValueError("invalid card roles field")
            card = recall_select.card_from_inner(value, row)
        except (ValueError, TypeError, UnicodeError, KeyError, AttributeError) as exc:
            raise recall_contract.RecallUnavailable("candidate_payload_invalid") from exc
        cards.append(card)
        inner[row["id"]] = value
        usable.append(row)
    return DecodedCandidates(batch.user_id, json.dumps(usable), json.dumps(cards), json.dumps(inner), sealed)


def rank(turn, candidates, deps, deadline, *, query_args, hybrid):
    cards = json.loads(candidates.cards_json)
    inner = json.loads(candidates.inner_json) if hybrid else None
    history = turn.projected
    args = {**query_args, "authorized_user_id": turn.user_id, "content_sk": None}
    encoder = None
    if hybrid:
        snapshot = json.loads(candidates.moments_json)
        deps = replace(deps, stored_vectors=lambda uid, model, ids:
                       recall_candidates.vectors_for_snapshot(snapshot, uid, model, ids))
        current, combined = recall_select.query_texts(history)
        state, encoder = plaintext_recall._hybrid_state(
            json.loads(candidates.moments_json), turn.user_id, current, combined,
            deps, min(deadline, time.monotonic() + recall_policy.budget_seconds()),
            time.monotonic(), strict=True)
        args["hybrid"] = state
    picks, trace, log = recall_select.select_context_memories(
        cards, history, args, inner=inner, encoder=encoder)
    return {"source": "local", "context_memories": picks, "context_memory_trace": trace,
            "context_memory_log": log, "messages": [
                {k: row.get(k) for k in ("id", "role", "seq")} for row in turn.original],
            "user_id": turn.user_id, "seq": turn.through_seq, "message_id": turn.message_id,
            "required_attachments": json.loads(turn.attachment_hashes_json),
            "required_references": json.loads(turn.required_references_json)}


def _stage(fn, deadline, permit, *, optional=False):
    status, value = plaintext_recall.run_bounded(fn, deadline - time.monotonic(), permit)
    if status == "ok" and time.monotonic() <= deadline:
        return value
    if status == "error":
        if isinstance(value, recall_contract.RecallFailure):
            raise value
        if time.monotonic() > deadline:
            if optional:
                raise recall_contract.RecallUnavailable("selection_unavailable")
            raise recall_contract.RecallFailure("input_read_unconfirmed")
        if isinstance(value, recall_contract.RecallUnavailable):
            raise value
        if optional:
            raise recall_contract.RecallUnavailable("selection_unavailable") from value
        raise recall_contract.RecallFailure("input_read_unconfirmed") from value
    if optional:
        raise recall_contract.RecallUnavailable("selection_unavailable")
    raise recall_contract.RecallFailure("input_read_unconfirmed")


def select_off(user_id, seq, deps, *, deadline, permit, rollout, query_args,
               message_id="", hybrid=False, read_candidates=None, include_references=True,
               attachment_proofs=None, source_snapshots=None):
    read_candidates = read_candidates or recall_candidates.read_owned
    verified_attachments = {}
    def attachment_reader(source):
        mid = source.get("id")
        expected = (source_snapshots or {}).get(mid)
        proof = verified_attachments.get(mid) or (attachment_proofs or {}).get(mid)
        if proof is not None:
            if (not isinstance(proof, recall_attachments.AttachmentProof)
                    or proof.user_id != user_id
                    or not recall_attachments.same_source(proof.storage_source, source, proof.body)):
                raise recall_contract.RecallFailure("attachment_changed", category="binding_error")
            if proof.storage_source != source:
                fresh = recall_attachments.read(source, user_id, deadline)
                if fresh.body != proof.body:
                    raise recall_contract.RecallFailure("attachment_changed", category="binding_error")
                proof = fresh
        else:
            proof = recall_attachments.read(source, user_id, deadline)
        if expected is not None and not recall_attachments.same_source(expected, source, proof.body):
            raise recall_contract.RecallFailure("attachment_changed", category="binding_error")
        verified_attachments[mid] = proof
        return proof

    # Required content is validated even when rollout disables optional recall.
    turn = _stage(lambda: recall_projection.turn(deps.history_page(user_id, seq),
                  user_id, seq, message_id, attachment_reader=attachment_reader,
                  source_snapshots=source_snapshots), deadline, permit)
    current = next((row for row in turn.original if row.get("id") == turn.message_id), {})
    projected_current = next((row for row in turn.projected if row.get("id") == turn.message_id), {})
    requested = [] if not include_references or projected_current.get("body_unavailable_reason") else [
        value.strip() for value in str(current.get("quoted_memory_ids") or "").split(",")[:8] if value.strip()]
    if requested:
        references = _stage(lambda: recall_references.read(user_id, requested, deadline), deadline, permit)
        turn = replace(turn, required_references_json=json.dumps(references, ensure_ascii=True))
    try:
        if rollout != "on":
            raise recall_contract.RecallUnavailable(
                "local_disabled" if rollout == "off" else "shadow_not_supported_for_off")
        # C0 read and C1 full owner validation are opaque to this waiter. If
        # they time out, do not infer the optional substage from mutable flags.
        batch = _stage(lambda: read_candidates(user_id, deadline), deadline, permit)
        candidates = _stage(lambda: decode(batch, recall_select.memory_readside_model_api_limit()),
                            deadline, permit, optional=True)
        return _stage(lambda: rank(turn, candidates, deps, deadline,
                                  query_args=query_args, hybrid=hybrid),
                      deadline, permit, optional=True), 200
    except recall_contract.RecallUnavailable as exc:
        return recall_contract.unavailable_payload(exc.reason, turn), 503
