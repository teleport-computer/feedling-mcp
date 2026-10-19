"""Recall-only candidate read: dedicated transaction, no UserStore hydration.

Read the same complete SQL universe as memory_load_strict, validate every raw
owner before archive filtering/normalization, then retain memory/list ordering
and its first-page limit. Never borrow the memory mutation transaction.
"""
from __future__ import annotations

import time

import db
from psycopg_pool import PoolTimeout
from memory import memory_core, recall_contract, service


def read_owned(user_id: str, deadline: float) -> recall_contract.OwnerValidatedCandidates:
    if not isinstance(user_id, str) or not user_id:
        raise recall_contract.RecallFailure("account_unconfirmed", category="binding_error")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise recall_contract.RecallFailure("input_read_unconfirmed")
    # Only the acquisition has a PoolTimeout downgrade. A similarly named
    # exception from a later query or validator is not treated as acquisition.
    manager = db.get_pool().connection(timeout=remaining)
    try:
        conn = manager.__enter__()
    except PoolTimeout as exc:
        raise recall_contract.RecallUnavailable("candidate_read_unavailable") from exc
    try:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise recall_contract.RecallFailure("input_read_unconfirmed")
            conn.execute("SELECT set_config('statement_timeout', %s, true)",
                         (str(max(1, int(remaining * 1000))),))
            try:
                rows = conn.execute(
                    "SELECT moment_id, doc FROM memory_moments WHERE user_id = %s "
                    "ORDER BY occurred_at, moment_id", (user_id,),
                ).fetchall()
            except Exception as exc:
                # This code is attached only to this dedicated candidate query.
                if getattr(exc, "sqlstate", None) == "57014":
                    raise recall_contract.RecallUnavailable("candidate_read_unavailable") from exc
                raise recall_contract.RecallFailure("candidate_read_unconfirmed") from exc
        # Full owner pass precedes normalization and every exclusion/limit.
        for moment_id, doc in rows:
            if not isinstance(doc, dict) or doc.get("id") != moment_id:
                raise recall_contract.RecallFailure("candidate_identity_invalid", category="binding_error")
        return recall_contract.candidate_batch([row[1] for row in rows], user_id)
    except (recall_contract.RecallFailure, recall_contract.RecallUnavailable):
        raise
    except Exception as exc:
        raise recall_contract.RecallFailure("candidate_read_unconfirmed") from exc
    finally:
        # transaction() already committed/rolled back. Return the connection
        # even on a typed failure, without suppressing the original exception.
        manager.__exit__(None, None, None)


def first_page(batch: recall_contract.OwnerValidatedCandidates, limit: int) -> list:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise recall_contract.RecallFailure("candidate_limit_invalid")
    try:
        normalized = [service.to_v1_card(row) for row in batch.rows]
        active = service._active_memory_moments(normalized)
        return memory_core._sort_memory_list(active)[:limit]
    except Exception as exc:
        raise recall_contract.RecallUnavailable("candidate_payload_invalid") from exc


def vectors_for_snapshot(batch_rows, user_id, model_id, ids):
    """Vectors are optional derived data; never reload candidate bodies in S."""
    import base64
    import struct
    from memory.embedding import sweep, recall_policy

    eligible, _ = sweep._eligible(batch_rows, user_id)
    if any(mid not in {row['id'] for row in batch_rows} for mid in ids):
        raise recall_contract.RecallFailure("vector_owner_conflict", category="binding_error")
    try:
        stored = db.memory_vectors_load(user_id, model_id)
    except Exception as exc:
        if getattr(exc, 'sqlstate', None) == '57014':
            raise recall_policy.Fallback('vectors_unavailable') from exc
        raise recall_contract.RecallFailure('vector_read_unconfirmed') from exc
    output = []
    dim = None
    for mid in sorted(set(ids)):
        current, hit = eligible.get(mid), stored.get(mid)
        if current is None or hit is None or hit[0] != current[0]:
            continue
        digest, vector = hit
        if dim is None:
            dim = len(vector)
        if len(vector) != dim:
            continue
        output.append({'id':mid, 'projection_hash':digest,
                       'vector_b64':base64.b64encode(struct.pack(f'<{len(vector)}f', *vector)).decode()})
    return {'model_id':model_id, 'dim':dim or 0, 'vectors':output, 'count':len(output)}
