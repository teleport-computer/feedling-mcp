"""Content-free action receipts committed with the user's memory mutation.

Uses existing user_blobs and the memory transaction/deletion fence. No raw
request, plaintext, or envelope is retained here. Account deletion removes
these user-scoped rows; deleting a card does not erase its replay identity.
"""
import hashlib
import json
from typing import Callable

from psycopg.types.json import Jsonb

import db
from core.store import UserStore
from memory import service

MAX_IDEMPOTENCY_KEY_CHARS = 160
IDEMPOTENCY_CONFLICT = "memory_idempotency_conflict"


def execute(
    store: UserStore, action: dict, dispatch: Callable[[], tuple[dict, list[dict], int]],
) -> tuple[dict, list[dict], int]:
    key = action.get("idempotency_key")
    if "idempotency_key" not in action:
        return dispatch()
    if not isinstance(key, str) or not key.strip() or len(key) > MAX_IDEMPOTENCY_KEY_CHARS:
        return {"status": "error", "error": "memory_idempotency_key_invalid"}, [], 400
    request = {k: v for k, v in action.items() if k != "idempotency_key"}
    try:
        encoded = json.dumps(request, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError):
        return {"status": "error", "error": "memory_action_invalid"}, [], 400
    digest = hashlib.sha256(encoded).hexdigest()
    kind = "memory_action_receipt:" + hashlib.sha256(key.encode()).hexdigest()
    with service.mutation_lock(store):
        with db.memory_user_mutation_fence(store.user_id) as conn:
            row = conn.execute("SELECT doc FROM user_blobs WHERE user_id=%s AND kind=%s",
                               (store.user_id, kind)).fetchone()
            if row is not None:
                saved = row[0]
                if saved["digest"] != digest:
                    return {"status": "error", "error": IDEMPOTENCY_CONFLICT}, [], 409
                return {**saved["receipt"], "replayed": True}, [], 200
            result, effects, status = dispatch()
            if status < 400:
                # An action's change/reason can contain user text: never journal
                # that response wholesale. The replay receipt only identifies it.
                memory = result.get("memory") or {}
                receipt = {"status": "ok", "action": result.get("action", ""),
                           "memory": {k: memory[k] for k in ("id", "type", "status", "occurred_at")
                                      if k in memory}}
                for field in ("noop", "skipped", "superseded_ids"):
                    if field in result:
                        receipt[field] = result[field]
                sql = "INSERT INTO user_blobs(user_id, kind, doc) VALUES (%s,%s,%s)"
                params = (store.user_id, kind, Jsonb({"digest": digest, "receipt": receipt}))
                conn.execute(sql, params)
                from tee_shadow import mirror
                db._defer_memory_post_commit(store.user_id, lambda: mirror.execute(
                    sql + " ON CONFLICT (user_id, kind) DO NOTHING", params))
            return result, effects, status
