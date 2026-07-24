"""One-off offline backfill: move existing chat IMAGE body_ct into R2.

``db.chat_append`` offloads heavy bodies (``_R2_OFFLOAD_CONTENT_TYPES``) to R2
going forward, but rows written before images joined that list still carry a
1-2MB base64 ciphertext inline in ``chat_messages.doc``. This walks them and
rewrites each to the pointer shape ``db._is_chat_file_pointer`` expects:

    doc = <doc minus body_ct> + {body_key: <generation key>, body_ct_len: N}

The backfill delegates to the same generation-fenced, tombstone-guarded offload
protocol as live appends. A crash leaves the row inline and readable plus a
durable cleanup intent; clear and same-id replacement cannot promote a stale
upload. Operational metadata merged during upload is preserved by the same
crypto-envelope CAS as the live path.

Read side needs no migration: every delivery exit already hydrates a pointer
(history page, poll peek, claim, single-body fetch) and is content-type agnostic.

Usage (from the backend dir, with the target DATABASE_URL + R2_* env loaded):

    python backfill_chat_images_to_r2.py --dry-run     # count rows + bytes
    python backfill_chat_images_to_r2.py               # migrate
    python backfill_chat_images_to_r2.py --user usr_x  # one user only
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

# Make `import db` / `import object_storage` work regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import object_storage  # noqa: E402

# Inline image rows only: content_type=image AND a body_ct still in the doc.
# `doc ? 'body_key'` rows are already migrated; keyset-paginate on (user_id, msg_id).
_SELECT = (
    "SELECT user_id, msg_id, doc FROM chat_messages "
    "WHERE (user_id, msg_id) > (%s, %s) "
    "  AND doc->>'content_type' = 'image' "
    "  AND doc ? 'body_ct' "
    "  AND NOT (doc ? 'body_key') "
    "ORDER BY user_id, msg_id LIMIT %s"
)

_SELECT_ONE_USER = (
    "SELECT user_id, msg_id, doc FROM chat_messages "
    "WHERE user_id = %s AND (user_id, msg_id) > (%s, %s) "
    "  AND doc->>'content_type' = 'image' "
    "  AND doc ? 'body_ct' "
    "  AND NOT (doc ? 'body_key') "
    "ORDER BY user_id, msg_id LIMIT %s"
)


def _migrate_row(user_id: str, msg_id: str, doc: dict, dry_run: bool) -> tuple[str, int]:
    """Return (status, raw_bytes). status in {'migrated','skipped','dry','failed'}."""
    body_ct = (doc or {}).get("body_ct")
    if not isinstance(body_ct, str) or not body_ct:
        return ("skipped", 0)
    raw_len = len(base64.b64decode(body_ct))
    if dry_run:
        return ("dry", raw_len)

    try:
        with db.get_pool().connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    generation = db._lock_chat_r2_lifecycle_on_cursor(cur, user_id)
                    cur.execute(
                        "SELECT storage_generation FROM chat_messages "
                        "WHERE user_id=%s AND msg_id=%s FOR UPDATE",
                        (user_id, msg_id),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return ("skipped", 0)
                    if int(row[0]) != generation:
                        return ("failed", 0)
        db._offload_chat_body_after_commit(user_id, msg_id, doc, generation)
        with db.get_pool().connection() as conn:
            persisted = conn.execute(
                "SELECT doc FROM chat_messages WHERE user_id=%s AND msg_id=%s",
                (user_id, msg_id),
            ).fetchone()
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL {user_id}/{msg_id} — offload: {e}", file=sys.stderr)
        return ("failed", 0)
    if persisted is None:
        return ("skipped", 0)
    persisted_doc = persisted[0] if isinstance(persisted[0], dict) else {}
    if db._is_chat_file_pointer(persisted_doc):
        return ("migrated", raw_len)
    return ("failed", 0)


def run(batch_size: int, dry_run: bool, only_user: str = "") -> int:
    last_user, last_msg = "", ""
    totals = {"migrated": 0, "skipped": 0, "dry": 0, "failed": 0}
    total_bytes = 0

    while True:
        sql = _SELECT_ONE_USER if only_user else _SELECT
        params = (
            (only_user, only_user, last_msg, batch_size) if only_user
            else (last_user, last_msg, batch_size)
        )
        with db.get_pool().connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        if not rows:
            break
        for user_id, msg_id, doc in rows:
            status, raw_len = _migrate_row(user_id, msg_id, doc, dry_run)
            totals[status] += 1
            total_bytes += raw_len
            last_user, last_msg = user_id, msg_id

    verb = "would migrate" if dry_run else "migrated"
    n = totals["dry"] if dry_run else totals["migrated"]
    print(
        f"{verb} {n} image rows ({total_bytes / 1_048_576:.1f} MB of ciphertext), "
        f"skipped {totals['skipped']}, failed {totals['failed']}"
    )
    return 1 if totals["failed"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill chat image body_ct into R2")
    parser.add_argument("--dry-run", action="store_true",
                        help="Count rows + bytes; do not upload or rewrite")
    parser.add_argument("--batch-size", type=int, default=50,
                        help="Rows per keyset page (default 50; images are heavy)")
    parser.add_argument("--user", default="",
                        help="Migrate only this user_id")
    args = parser.parse_args()

    if not object_storage.chat_files_enabled():
        print("ERROR: chat-file R2 is not configured (set R2_ENDPOINT / "
              "R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_CHAT_FILES_BUCKET).",
              file=sys.stderr)
        return 2
    return run(args.batch_size, args.dry_run, args.user)


if __name__ == "__main__":
    raise SystemExit(main())
