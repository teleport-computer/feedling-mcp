#!/usr/bin/env python3
"""Repair V2 users whose summary frontier is permanently corrupt.

Before commit ed3a275f the compaction fold could fold a GC-able synthetic row
(``verify_ping``, deleted once /v1/chat/verify_loop completes) into an
IMMUTABLE level-0 summary leaf. The leaf's frozen ``source_message_count`` then
outlives the row: once verify_loop deletes it, the canonical witness (a live
``COUNT``/``MIN`` over ``chat_messages``) no longer matches the leaf, and
``validate_canonical_frontier`` raises ``v2_summary_frontier_integrity_error``
on EVERY later turn — a permanent multi-turn brick (the user only ever sees the
"刚刚没接上" fallback).

The exclusion fix stops NEW corruption but cannot rewrite an already-frozen
immutable leaf, so users bricked before it need this one-shot reset.

What it does: raw ``chat_messages`` are the durable source ledger and are NEVER
touched. Only the encrypted summary frontier (``v2_conversation_summary`` +
``v2_conversation_summary_segments``) is deleted, resetting the watermark to 0.
The user's next turn re-folds from scratch through the now-fixed path, losing no
content. Cost note: a long history re-folds in bounded batches on the user's own
BYOK key across their next turns — expected, not a leak.

Detection replays the EXACT production gate
(``serve_worker._summary_metadata_frontier`` →
``summary_frontier.validate_canonical_frontier``) against the post-fix
frontier state, so it flags precisely the frontiers that do (or will, once the
synthetic row is GC'd) fail a real turn.

Read-only by default — lists bricked users and the failing invariant:

    python scripts/repair_v2_bricked_summary_frontier.py --env test
    # or point at any DB directly:
    DATABASE_URL='postgres://…?sslmode=require' \
        python scripts/repair_v2_bricked_summary_frontier.py

Apply the reset (per-user, each in its own fenced transaction):

    python scripts/repair_v2_bricked_summary_frontier.py --env test --apply

Scope to one user (works for both dry-run and --apply):

    python scripts/repair_v2_bricked_summary_frontier.py --env test --user usr_abc --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# .env variable name per environment; resolved into DATABASE_URL before the pool
# is first opened (db.get_pool reads DATABASE_URL lazily, so setting it here is
# enough). Matches the feedling-ops-recon convention.
_ENV_DB_VAR = {
    "test": "TEST_DATABASE_URL",
    "pre": "PRE_DATABASE_URL",
    "prod": "PROD_DATABASE_URL",
}


def _resolve_database_url(env: str | None) -> None:
    if env is None:
        if not os.environ.get("DATABASE_URL", "").strip():
            raise SystemExit(
                "Set DATABASE_URL, or pass --env {test|pre|prod} to resolve it from .env"
            )
        return
    var = _ENV_DB_VAR[env]
    dsn = ""
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith(var + "="):
                dsn = line.split("=", 1)[1].strip()
                break
    if not dsn:
        raise SystemExit(f"{var} not found in {env_file}")
    os.environ["DATABASE_URL"] = dsn


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--env",
        choices=sorted(_ENV_DB_VAR),
        help="resolve DATABASE_URL from .env for this environment",
    )
    parser.add_argument(
        "--user",
        default=None,
        help="restrict to a single user_id (default: scan every user with a summary)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete the corrupt summary+segments (no writes without this)",
    )
    return parser.parse_args(argv)


def _validate_frontier(state: dict) -> None:
    """Replay the exact production gate a live turn passes through.

    Mirrors ``serve_worker._summary_metadata_frontier`` without importing the
    worker (keeps this an enclave-free, DB-only tool). Raises
    ``SummaryFrontierIntegrityError`` on a corrupt frontier.
    """
    from model_api_runtime.v2 import summary_frontier

    opened = [
        summary_frontier.SummarySegment(
            segment_id=int(row["segment_id"]),
            coverage_kind=str(row["coverage_kind"]),
            level=int(row["level"]),
            start_seq=int(row["start_seq"]),
            end_seq=int(row["end_seq"]),
            source_message_count=int(row["source_message_count"]),
            legacy_opaque_through_seq=int(row.get("legacy_opaque_through_seq") or 0),
            child_segment_ids=tuple(
                int(value) for value in (row.get("child_segment_ids") or [])
            ),
            # Same content-free sentinel the production validator uses: coverage
            # provenance is checked without decrypting any segment.
            text="<encrypted-summary-segment>",
        )
        for row in list(state.get("segments") or [])
    ]
    validated = summary_frontier.validate_canonical_frontier(
        opened,
        watermark_seq=int(state.get("watermark_seq") or 0),
        first_source_seq=int(state.get("first_source_seq") or 0),
        covered_source_count=int(state.get("covered_source_count") or 0),
    )
    canonical_ids = tuple(item.segment_id for item in validated)
    materialized_ids = tuple(
        int(value) for value in (state.get("materialized_segment_ids") or [])
    )
    if canonical_ids != materialized_ids:
        raise summary_frontier.SummaryFrontierIntegrityError(
            "materialized_head_provenance_mismatch"
        )


def _bricked_detail(uid: str) -> str | None:
    """Return the failing invariant's detail for a corrupt frontier, else None."""
    from model_api_runtime.v2 import jobs_store, summary_frontier

    try:
        state = jobs_store.get_summary_frontier_state(uid)
    except summary_frontier.SummaryFrontierIntegrityError as exc:
        return getattr(exc, "detail", str(exc))
    if state is None:
        return None  # no summary yet — nothing to validate
    try:
        _validate_frontier(state)
    except summary_frontier.SummaryFrontierIntegrityError as exc:
        return getattr(exc, "detail", str(exc))
    return None


def _reset_frontier(uid: str) -> tuple[int, int]:
    """Delete this user's summary head + all segments under the chat fence.

    The exclusive per-user chat fence is the same linearization point
    append_summary_leaf_cas/chat_clear take, so a racing compaction cannot
    observe or write a half-reset frontier: its version CAS simply misses and it
    re-reads the empty frontier. Returns (segments_deleted, heads_deleted)."""
    import db

    with db.get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                db._lock_chat_user_fence_on_cursor(cur, str(uid), exclusive=True)
                cur.execute(
                    "DELETE FROM v2_conversation_summary_segments WHERE user_id=%s",
                    (uid,),
                )
                segments = cur.rowcount
                cur.execute(
                    "DELETE FROM v2_conversation_summary WHERE user_id=%s",
                    (uid,),
                )
                heads = cur.rowcount
    return segments, heads


def _candidate_user_ids(single_user: str | None) -> list[str]:
    import db

    if single_user:
        return [single_user]
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT user_id FROM v2_conversation_summary ORDER BY user_id"
        ).fetchall()
    return [str(row[0]) for row in rows]


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _resolve_database_url(args.env)

    candidates = _candidate_user_ids(args.user)
    bricked: list[tuple[str, str]] = []
    for uid in candidates:
        detail = _bricked_detail(uid)
        if detail is not None:
            bricked.append((uid, detail))

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] scanned {len(candidates)} user(s) with a summary; "
          f"{len(bricked)} bricked frontier(s) found", flush=True)
    for uid, detail in bricked:
        print(f"  - {uid}: {detail}", flush=True)

    if not bricked:
        print("Nothing to repair.", flush=True)
        return 0
    if not args.apply:
        print("\nRe-run with --apply to reset the summary frontier for the users "
              "above (raw chat_messages are never touched; the next turn re-folds "
              "cleanly).", flush=True)
        return 0

    repaired = 0
    for uid, detail in bricked:
        segments, heads = _reset_frontier(uid)
        # Confirm the reset actually cleared the corruption.
        residual = _bricked_detail(uid)
        status = "ok" if residual is None else f"STILL BRICKED: {residual}"
        print(f"  reset {uid}: -{segments} segment(s), -{heads} head; {status}",
              flush=True)
        if residual is None:
            repaired += 1
    print(f"\nRepaired {repaired}/{len(bricked)} bricked frontier(s).", flush=True)
    return 0 if repaired == len(bricked) else 1


if __name__ == "__main__":
    raise SystemExit(main())
