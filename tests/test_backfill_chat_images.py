"""Offline backfill script: inline chat image rows → R2 (backend/backfill_chat_images_to_r2.py).

Legacy rows are produced the honest way — chat_append with R2 disabled, which is
exactly how they got into prod — then the backfill runs against a fake S3. Every
assertion is scoped with --user so the shared session DB can't perturb it.
"""

import base64
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import backfill_chat_images_to_r2 as backfill  # noqa: E402
import db  # noqa: E402
import object_storage  # noqa: E402

from conftest import seed_user  # noqa: E402
from test_chat_file_r2 import (  # noqa: E402
    _BUCKET,
    _assert_versioned_key,
    _enable_r2,
    _image_doc,
    _raw_doc,
)
from test_frame_r2 import _FakeS3  # noqa: E402


def _uid() -> str:
    return f"bfi_{uuid.uuid4().hex[:8]}"


def _write_inline_image(monkeypatch, uid: str, mid: str, body: bytes, ts: float = 1.0) -> None:
    """Append an image the pre-offload way: R2 off → body_ct stays in the row."""
    with monkeypatch.context() as m:
        m.delenv("R2_CHAT_FILES_BUCKET", raising=False)
        db.chat_append(uid, mid, ts, _image_doc(uid, mid, body), 100)
    assert "body_ct" in _raw_doc(uid, mid)  # precondition: it really is legacy


def test_dry_run_changes_nothing(backend_env, monkeypatch):
    client = _FakeS3()
    uid = _uid(); seed_user(uid)
    mids = [uuid.uuid4().hex for _ in range(3)]
    for i, mid in enumerate(mids):
        _write_inline_image(monkeypatch, uid, mid, f"px{i}".encode(), float(i + 1))

    _enable_r2(monkeypatch, client)
    assert backfill.run(batch_size=2, dry_run=True, only_user=uid) == 0

    assert not client.store                                   # nothing uploaded
    for mid in mids:
        assert "body_ct" in _raw_doc(uid, mid)                # rows untouched


def test_backfill_moves_bodies_and_round_trips(backend_env, monkeypatch):
    client = _FakeS3()
    uid = _uid(); seed_user(uid)
    bodies = {}
    for i in range(3):
        mid = uuid.uuid4().hex
        bodies[mid] = f"\xff\xd8\xff-photo-{i}".encode()
        _write_inline_image(monkeypatch, uid, mid, bodies[mid], float(i + 1))

    _enable_r2(monkeypatch, client)
    assert backfill.run(batch_size=2, dry_run=False, only_user=uid) == 0

    for mid, body in bodies.items():
        raw = _raw_doc(uid, mid)
        key = str(raw["body_key"])
        _assert_versioned_key(key, uid, mid, "image")          # row is now a pointer
        assert "body_ct" not in raw
        assert client.store[(_BUCKET, key)] == body
        # The whole point: the read path reconstitutes the ORIGINAL ciphertext.
        assert base64.b64decode(db.hydrate_chat_file_body(uid, raw)["body_ct"]) == body


def test_rerun_is_a_noop(backend_env, monkeypatch):
    client = _FakeS3()
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    _write_inline_image(monkeypatch, uid, mid, b"once")

    _enable_r2(monkeypatch, client)
    backfill.run(batch_size=10, dry_run=False, only_user=uid)
    first = _raw_doc(uid, mid)

    backfill.run(batch_size=10, dry_run=False, only_user=uid)  # already-migrated rows are excluded
    assert _raw_doc(uid, mid) == first


def test_leaves_text_and_already_migrated_rows_alone(backend_env, monkeypatch):
    client = _FakeS3()
    uid = _uid(); seed_user(uid)
    text_mid, img_mid = uuid.uuid4().hex, uuid.uuid4().hex

    with monkeypatch.context() as m:
        m.delenv("R2_CHAT_FILES_BUCKET", raising=False)
        text_doc = _image_doc(uid, text_mid, b"just words")
        text_doc["content_type"] = "text"
        db.chat_append(uid, text_mid, 1.0, text_doc, 100)

    # This one is written WITH R2 on — already a pointer before the backfill runs.
    _enable_r2(monkeypatch, client)
    db.chat_append(uid, img_mid, 2.0, _image_doc(uid, img_mid, b"already-there"), 100)
    already = _raw_doc(uid, img_mid)

    backfill.run(batch_size=10, dry_run=False, only_user=uid)

    assert "body_ct" in _raw_doc(uid, text_mid)               # text stays inline
    assert "body_key" not in _raw_doc(uid, text_mid)
    assert _raw_doc(uid, img_mid) == already                  # pointer row unchanged


def test_upload_failure_leaves_the_row_readable(backend_env, monkeypatch):
    # Crash-safety contract: object first, flip second. A failed upload must never
    # leave a pointer to a missing object — the row stays inline and readable.
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    _write_inline_image(monkeypatch, uid, mid, b"unlucky")

    _enable_r2(monkeypatch, _FakeS3(fail_put=True))
    assert backfill.run(batch_size=10, dry_run=False, only_user=uid) == 1   # non-zero exit

    raw = _raw_doc(uid, mid)
    assert "body_ct" in raw and "body_key" not in raw


def test_flip_preserves_metadata_written_during_upload(backend_env, monkeypatch):
    # A consumer can claim/reply to the message while the backfill is uploading it.
    # The flip is `doc - 'body_ct' || pointer` on the CURRENT row, so that write
    # must survive rather than be clobbered by a stale snapshot.
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    _write_inline_image(monkeypatch, uid, mid, b"racy")

    class _RacingS3(_FakeS3):
        def put_object(self, Bucket, Key, Body, **kw):
            db.chat_update_metadata(uid, mid, {"reply_status": "replied", "reply_message_id": "r1"})
            return super().put_object(Bucket, Key, Body, **kw)

    _enable_r2(monkeypatch, _RacingS3())
    backfill.run(batch_size=10, dry_run=False, only_user=uid)

    raw = _raw_doc(uid, mid)
    assert raw["reply_status"] == "replied"                   # concurrent write survived
    assert raw["reply_message_id"] == "r1"
    assert raw.get("body_key") and "body_ct" not in raw       # and the flip still happened


def test_clear_during_backfill_cannot_promote_or_orphan_old_generation(
    backend_env, monkeypatch,
):
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    _write_inline_image(monkeypatch, uid, mid, b"clear-race")

    class _ClearingS3(_FakeS3):
        def put_object(self, Bucket, Key, Body, **kw):
            assert db.chat_clear(uid) == 1
            return super().put_object(Bucket, Key, Body, **kw)

    client = _ClearingS3()
    _enable_r2(monkeypatch, client)
    assert backfill.run(batch_size=10, dry_run=False, only_user=uid) == 0
    for _ in range(3):
        db.reconcile_chat_r2_cleanup(uid, limit=1000, inventory_limit=10)

    assert _raw_doc(uid, mid) is None
    assert client.store == {}
