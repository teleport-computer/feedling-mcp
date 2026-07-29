"""Durable tool activity emitted by V1 resident runtimes.

Only fixed identifiers, status, timing, and pre-projected result metadata are
accepted. Tool arguments, result bodies, and model prose never enter this
table.
"""
from __future__ import annotations

from typing import Any, Mapping

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import db
from core import wake_bus


def append_resident_tool_event(
    user_id: str,
    turn_id: str,
    *,
    activity_id: str,
    tool_name: str,
    state: str,
    call_id: str,
    detail: Mapping[str, Any],
) -> tuple[int, bool]:
    """Append one idempotent V1 invocation transition after ownership checks."""
    with db.get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT doc FROM chat_messages WHERE user_id=%s AND msg_id=%s "
                    "FOR SHARE",
                    (str(user_id), str(turn_id)),
                )
                parent = cur.fetchone()
                if parent is None or not isinstance(parent["doc"], dict) \
                        or parent["doc"].get("role") != "user":
                    raise ValueError("activity turn is not a user message")
                cur.execute(
                    "SELECT hosted_runtime_state FROM v2_runtime_state "
                    "WHERE user_id=%s",
                    (str(user_id),),
                )
                control = cur.fetchone()
                if control is not None and str(control["hosted_runtime_state"]) == "v2":
                    raise ValueError("resident activity rejected for V2-owned user")
                cur.execute(
                    "SELECT tool_name FROM chat_turn_activity_events "
                    "WHERE user_id=%s AND turn_id=%s AND activity_id=%s "
                    "LIMIT 1 FOR UPDATE",
                    (str(user_id), str(turn_id), str(activity_id)),
                )
                prior = cur.fetchone()
                if prior is not None and str(prior["tool_name"]) != str(tool_name):
                    raise ValueError("activity invocation tool mismatch")
                cur.execute(
                    "INSERT INTO chat_turn_activity_events "
                    "(user_id,turn_id,activity_id,tool_name,state,call_id,detail_json) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (user_id,turn_id,activity_id,state) DO NOTHING "
                    "RETURNING id",
                    (
                        str(user_id),
                        str(turn_id),
                        str(activity_id),
                        str(tool_name),
                        str(state),
                        str(call_id or ""),
                        Jsonb(dict(detail)),
                    ),
                )
                inserted = cur.fetchone()
                if inserted is not None:
                    event_id = int(inserted["id"])
                else:
                    cur.execute(
                        "SELECT id FROM chat_turn_activity_events "
                        "WHERE user_id=%s AND turn_id=%s AND activity_id=%s "
                        "AND state=%s",
                        (str(user_id), str(turn_id), str(activity_id), str(state)),
                    )
                    event_id = int(cur.fetchone()["id"])
    if inserted is not None:
        try:
            wake_bus.notify("chat", str(user_id))
        except Exception:  # observability must never affect the tool
            pass
    return event_id, inserted is not None


def resident_turn_rows(user_id: str, turn_id: str) -> tuple[dict | None, list[dict]]:
    """Return a V1 user-turn state plus its bounded activity rows."""
    with db.get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT doc FROM chat_messages WHERE user_id=%s AND msg_id=%s",
                (str(user_id), str(turn_id)),
            )
            parent = cur.fetchone()
            if parent is None or not isinstance(parent["doc"], dict) \
                    or parent["doc"].get("role") != "user":
                return None, []
            parent_doc = dict(parent["doc"])
            cur.execute(
                "SELECT id,NULL::bigint AS job_id,user_id,'tool_activity' AS kind,"
                "tool_name AS label,detail_json,0 AS seq,"
                "extract(epoch FROM created_at)::float8 AS created_at "
                "FROM chat_turn_activity_events WHERE user_id=%s AND turn_id=%s "
                "ORDER BY id ASC LIMIT 500",
                (str(user_id), str(turn_id)),
            )
            rows = [dict(row) for row in cur.fetchall()]
    return parent_doc, rows


def resident_activity_rows(user_id: str, turn_id: str) -> list[dict]:
    """Return only V1 activity rows for final reply metadata projection."""
    _parent, rows = resident_turn_rows(user_id, turn_id)
    return rows
