"""Generation-fenced effect applier (spec A4). Pure of sinks: dispatch is injected
so this module never imports hosted/capabilities. Each pending row is handled in
its own transaction that locks the user's v2_runtime_state row FOR UPDATE, so a
concurrent cutover cannot slip between the generation read and the apply.
"""
from __future__ import annotations
import json
from typing import Callable
import db


def apply_pending_effects(user_id: str, *, dispatch: Callable[[str, dict], None]) -> dict:
    applied = discarded = 0
    for row in db.effect_pending(user_id):
        eid = row["effect_id"]
        with db.get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT runtime_generation FROM v2_runtime_state "
                    "WHERE user_id=%s FOR UPDATE", (user_id,))
                gr = cur.fetchone()
                current = int(gr[0]) if gr else 0
                # re-check the row is still pending under the lock (rerun safety)
                cur.execute(
                    "SELECT status FROM v2_effect_outbox WHERE effect_id=%s", (eid,))
                st = cur.fetchone()
                if not st or st[0] != "pending":
                    continue
                if int(row["expected_generation"]) == current:
                    payload = row["payload"]
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    payload = dict(payload)      # never mutate the stored dict in place
                    payload["effect_id"] = eid   # sinks claim via db.effect_sink_claim(payload["effect_id"])
                    dispatch(row["effect_type"], payload)  # sink is effect_id-unique -> replay-safe
                    cur.execute(
                        "UPDATE v2_effect_outbox SET status='applied', applied_at=now() "
                        "WHERE effect_id=%s", (eid,))
                    applied += 1
                else:
                    cur.execute(
                        "UPDATE v2_effect_outbox SET status='discarded' WHERE effect_id=%s",
                        (eid,))
                    discarded += 1
    return {"applied": applied, "discarded": discarded}
