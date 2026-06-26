"""One-off offline backfill: move existing frame_envelopes body_ct into R2.

Run locally, pointed at the production Postgres + R2 (S3-compatible) creds. For
each legacy row (``body_key IS NULL AND doc IS NOT NULL``) it uploads the
ciphertext ``doc.body_ct`` to R2 and rewrites the row to the offloaded shape:

    env_meta = <doc minus body_ct>,  body_key = frames/<user>/<frame>,  doc = NULL

Idempotent + resumable: already-migrated rows have ``body_key`` set and are
skipped; re-running converges. Use ``--dry-run`` to count + size first.

Usage::

    DATABASE_URL=postgresql://...?sslmode=require \\
    FEEDLING_R2_ENDPOINT=https://<acct>.r2.cloudflarestorage.com \\
    FEEDLING_R2_ACCESS_KEY=... FEEDLING_R2_SECRET_KEY=... \\
    FEEDLING_R2_BUCKET=feedling-frames \\
    python backend/backfill_frames_to_r2.py --dry-run
    # then drop --dry-run to perform the move

Prereq: schema is at >= 0006 (env_meta / body_key columns, doc nullable).
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

from psycopg.types.json import Jsonb

# Make `import db` / `import object_storage` work regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import object_storage  # noqa: E402

_SELECT = (
    "SELECT user_id, frame_id, doc FROM frame_envelopes "
    "WHERE (user_id, frame_id) > (%s, %s) AND body_key IS NULL AND doc IS NOT NULL "
    "ORDER BY user_id, frame_id LIMIT %s"
)


def _migrate_row(user_id: str, frame_id: str, doc: dict, dry_run: bool) -> tuple[str, int]:
    """Return (status, bytes). status in {'migrated','skipped','dry'}."""
    body_ct = (doc or {}).get("body_ct")
    if not isinstance(body_ct, str) or not body_ct:
        return ("skipped", 0)
    raw_len = len(base64.b64decode(body_ct))
    if dry_run:
        return ("dry", raw_len)
    key = object_storage.put_frame_body(user_id, frame_id, body_ct)
    env_meta = {k: v for k, v in doc.items() if k != "body_ct"}
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE frame_envelopes SET env_meta = %s, body_key = %s, doc = NULL "
            "WHERE user_id = %s AND frame_id = %s",
            (Jsonb(env_meta), key, user_id, frame_id),
        )
    return ("migrated", raw_len)


def run(batch_size: int, dry_run: bool) -> int:
    last_user, last_frame = "", ""
    migrated = skipped = total_rows = 0
    total_bytes = 0
    while True:
        with db.get_pool().connection() as conn:
            rows = conn.execute(_SELECT, (last_user, last_frame, batch_size)).fetchall()
        if not rows:
            break
        for user_id, frame_id, doc in rows:
            total_rows += 1
            status, nbytes = _migrate_row(user_id, frame_id, doc, dry_run)
            total_bytes += nbytes
            if status == "skipped":
                skipped += 1
                print(f"  SKIP {user_id}/{frame_id} — no body_ct")
            else:
                migrated += 1
        last_user, last_frame = rows[-1][0], rows[-1][1]
        print(f"… scanned {total_rows} (moved {migrated}, skipped {skipped}, "
              f"{total_bytes / 1e6:.1f} MB)")
    verb = "would move" if dry_run else "moved"
    print(f"\nDone. scanned={total_rows} {verb}={migrated} skipped={skipped} "
          f"bytes={total_bytes / 1e6:.1f} MB")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill frame body_ct into R2")
    parser.add_argument("--dry-run", action="store_true",
                        help="Count rows + bytes; do not upload or rewrite")
    parser.add_argument("--batch-size", type=int, default=200,
                        help="Rows per keyset page (default 200)")
    args = parser.parse_args()

    if not object_storage.enabled():
        print("ERROR: R2 is not configured (set FEEDLING_R2_ENDPOINT / "
              "FEEDLING_R2_ACCESS_KEY / FEEDLING_R2_SECRET_KEY / FEEDLING_R2_BUCKET).",
              file=sys.stderr)
        return 2
    return run(args.batch_size, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
