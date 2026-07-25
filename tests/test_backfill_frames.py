"""Offline backfill script: legacy frame rows → R2 (backend/backfill_frames_to_r2.py).

Inserts legacy-shaped rows (inline doc, no pointer) via raw SQL, then drives the
backfill against a fake S3 client. Assertions are scoped to a unique user_id so
the shared session DB / other tests don't perturb counts.
"""

import sys
import uuid
from pathlib import Path

from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
import object_storage  # noqa: E402
import backfill_frames_to_r2 as backfill  # noqa: E402

from test_frame_r2 import _FakeS3, _enable_r2, _env  # noqa: E402

from conftest import seed_user  # noqa: E402


def _uid() -> str:
    return f"bf_{uuid.uuid4().hex[:10]}"


def _insert_legacy(uid: str, fid: str, ts: float) -> dict:
    seed_user(uid)
    env = _env(uid, fid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO frame_envelopes (user_id, frame_id, ts, doc) VALUES (%s, %s, %s, %s)",
            (uid, fid, ts, Jsonb(env)),
        )
    return env


def _pending(uid: str) -> int:
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT count(*) FROM frame_envelopes "
            "WHERE user_id = %s AND body_key IS NULL AND doc IS NOT NULL",
            (uid,),
        ).fetchone()[0]


def test_dry_run_changes_nothing(monkeypatch):
    fake = _FakeS3()
    _enable_r2(monkeypatch, fake)
    uid = _uid()
    for i in range(3):
        _insert_legacy(uid, f"f{i}", float(i))
    backfill.run(batch_size=2, dry_run=True)
    assert _pending(uid) == 3            # rows untouched
    assert not [k for (b, k) in fake.store if k.startswith(f"frames/{uid}/")]


def test_backfill_moves_bodies_and_is_idempotent(monkeypatch):
    fake = _FakeS3()
    _enable_r2(monkeypatch, fake)
    uid = _uid()
    envs = {f"f{i}": _insert_legacy(uid, f"f{i}", float(i)) for i in range(3)}

    backfill.run(batch_size=2, dry_run=False)

    assert _pending(uid) == 0
    for fid, env in envs.items():
        # ciphertext now in R2, row reconstructs to the original envelope
        assert (("io-image-frames", f"frames/{uid}/{fid}")) in fake.store
        assert db.frame_get(uid, fid) == env

    # second pass is a no-op (nothing left pending)
    backfill.run(batch_size=2, dry_run=False)
    assert _pending(uid) == 0


def test_skips_row_without_body_ct(monkeypatch):
    fake = _FakeS3()
    _enable_r2(monkeypatch, fake)
    uid = _uid()
    seed_user(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO frame_envelopes (user_id, frame_id, ts, doc) VALUES (%s, %s, %s, %s)",
            (uid, "nobody", 1.0, Jsonb({"id": "nobody", "v": 1})),
        )
    backfill.run(batch_size=10, dry_run=False)
    # malformed row is left as-is (still legacy), not crashed on
    with db.get_pool().connection() as conn:
        doc, body_key = conn.execute(
            "SELECT doc, body_key FROM frame_envelopes WHERE user_id = %s AND frame_id = %s",
            (uid, "nobody"),
        ).fetchone()
    assert body_key is None
    assert doc is not None
