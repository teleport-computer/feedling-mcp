"""db.py chat heavy-body R2 offload (lazy hydration at delivery exits).

A body_ct whose content_type is in db._R2_OFFLOAD_CONTENT_TYPES ("file", "image")
is offloaded to R2; the chat_messages row keeps a slim pointer (body_key +
body_ct_len). chat_load returns POINTERS — the heavy ciphertext is fetched lazily
only at read exits that deliver a body (poll claim, a history page that includes
the body, single message_body), and a page's pointers are pre-fetched CONCURRENTLY
(chat_service.hydrate_history_page) so N images don't cost N serial round-trips.
Text messages and, when R2 is unconfigured, everything stay inline. A fake S3
stands in for boto3 (reuses test_frame_r2).
"""

import base64
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
import object_storage  # noqa: E402
from chat import service as chat_service  # noqa: E402

from conftest import seed_user  # noqa: E402
from test_frame_r2 import _FakeS3  # noqa: E402  reuse the fake S3


_BUCKET = "io-user-attachments"


class _CountingS3(_FakeS3):
    """_FakeS3 + a tally of body fetches, so a test can prove the omit path
    never touches R2 and that a page of pointers fans out one GET each."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.gets: list[str] = []

    def get_object(self, Bucket, Key, **kw):
        self.gets.append(Key)
        return super().get_object(Bucket, Key, **kw)


def _enable_r2(monkeypatch, client):
    monkeypatch.setenv("R2_ENDPOINT", "https://acct.r2.cloudflarestorage.com")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "sk")
    monkeypatch.setenv("R2_CHAT_FILES_BUCKET", _BUCKET)
    monkeypatch.setattr(object_storage, "_client", lambda: client)


def _uid() -> str:
    return f"u_{uuid.uuid4().hex[:10]}"


def _file_doc(uid: str, mid: str, body: bytes = b"PK\x03\x04docx-bytes") -> dict:
    return {
        "id": mid, "role": "user", "ts": 1.0, "source": "model_api",
        "content_type": "file", "file_name": "报告.docx", "file_mime": "application/octet-stream",
        "body_ct": base64.b64encode(body).decode(),
        "nonce": base64.b64encode(b"123456789012").decode(),
        "K_user": base64.b64encode(b"user-key").decode(),
        "K_enclave": base64.b64encode(b"enc-key").decode(),
        "visibility": "shared", "owner_user_id": uid,
    }


def _raw_doc(uid: str, mid: str) -> dict | None:
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT doc FROM chat_messages WHERE user_id=%s AND msg_id=%s", (uid, mid)
        ).fetchone()
    return row[0] if row else None


def _archived_doc(uid: str, mid: str) -> dict | None:
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT doc FROM chat_message_archive "
            "WHERE user_id=%s AND msg_id=%s ORDER BY source_seq DESC LIMIT 1",
            (uid, mid),
        ).fetchone()
    return row[0] if row else None


def _body_key(uid: str, mid: str) -> str:
    doc = _raw_doc(uid, mid)
    assert isinstance(doc, dict) and doc.get("body_key")
    return str(doc["body_key"])


def _cleanup_keys(uid: str) -> list[str]:
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT body_key FROM chat_r2_cleanup "
            "WHERE user_id=%s ORDER BY body_key",
            (uid,),
        ).fetchall()
    return [str(row[0]) for row in rows]


def _storage_generation(uid: str) -> int:
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT generation FROM chat_r2_lifecycle WHERE user_id=%s",
            (uid,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _inventory_state(uid: str) -> tuple[bool, int, str]:
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT inventory_pending,inventory_attempt_count,"
            "inventory_last_error FROM chat_r2_lifecycle WHERE user_id=%s",
            (uid,),
        ).fetchone()
    assert row is not None
    return bool(row[0]), int(row[1]), str(row[2])


def _drain_r2(uid: str) -> None:
    """Drive the isolated cleanup worker deterministically in unit tests."""
    for _ in range(3):
        db.reconcile_chat_r2_cleanup(
            uid, limit=1000, include_inventory=True, inventory_limit=10,
        )


def _assert_versioned_key(
    key: str, uid: str, mid: str, content_type: str,
) -> None:
    prefix = "chatimages" if content_type == "image" else "chatfiles"
    key_prefix, owner, generation, message_id, version = key.split("/", 4)
    assert (key_prefix, owner, message_id) == (prefix, uid, mid)
    assert generation.startswith("g") and generation[1:].isdigit()
    assert len(version) == 32
    assert all(ch in "0123456789abcdef" for ch in version)


def _set_resident_runtime(uid: str) -> None:
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_runtime_state "
            "(user_id,hosted_runtime_state,runtime_generation) "
            "VALUES (%s,'resident',1) "
            "ON CONFLICT (user_id) DO UPDATE SET hosted_runtime_state='resident'",
            (uid,),
        )


def test_offload_stores_pointer_and_chat_load_is_lazy(backend_env, monkeypatch):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    body = b"PK\x03\x04the-real-file-bytes"
    db.chat_append(uid, mid, 1.0, _file_doc(uid, mid, body), 100)

    # Stored row = slim pointer: body_key + body_ct_len, body_ct gone.
    raw = _raw_doc(uid, mid)
    key = str(raw["body_key"])
    _assert_versioned_key(key, uid, mid, "file")
    assert raw["body_ct_len"] == len(base64.b64encode(body).decode())
    assert "body_ct" not in raw
    # Ciphertext lives in R2.
    assert client.store[(_BUCKET, key)] == body
    # chat_load is LAZY: returns the pointer, does NOT reconstitute.
    loaded = {m["id"]: m for m in db.chat_load(uid)}
    assert loaded[mid].get("body_key") and loaded[mid].get("body_ct") is None


def test_idempotent_retry_offloads_winner_only_once(backend_env, monkeypatch):
    class _PutCountingS3(_FakeS3):
        def __init__(self):
            super().__init__()
            self.puts = 0

        def put_object(self, Bucket, Key, Body, **kw):
            self.puts += 1
            return super().put_object(Bucket, Key, Body, **kw)

    client = _PutCountingS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid)
    key = str(uuid.uuid4())
    first = _file_doc(uid, "idem-file-first")
    first["client_msg_id"] = key
    retry = _file_doc(uid, "idem-file-retry")
    retry["client_msg_id"] = key

    first_winner, first_inserted = db.chat_append_idempotent(
        uid, first["id"], time.time(), first, 100,
        client_msg_id=key, window_sec=600,
    )
    retry_winner, retry_inserted = db.chat_append_idempotent(
        uid, retry["id"], time.time(), retry, 100,
        client_msg_id=key, window_sec=600,
    )

    assert first_inserted is True
    assert retry_inserted is False
    assert first_winner["id"] == retry_winner["id"] == first["id"]
    assert client.puts == 1
    assert len(client.store) == 1


def test_idempotent_append_after_clear_pins_live_storage_generation(
    backend_env, monkeypatch,
):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid()
    seed_user(uid)
    assert db.chat_clear(uid) == 0
    mid = uuid.uuid4().hex
    client_msg_id = str(uuid.uuid4())
    doc = _file_doc(uid, mid, b"post-clear-idempotent")
    doc["client_msg_id"] = client_msg_id

    _winner, inserted = db.chat_append_idempotent(
        uid,
        mid,
        time.time(),
        doc,
        100,
        client_msg_id=client_msg_id,
        window_sec=600,
    )
    assert inserted is True
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT storage_generation,doc FROM chat_messages "
            "WHERE user_id=%s AND msg_id=%s",
            (uid, mid),
        ).fetchone()
    assert row is not None and int(row[0]) == 0
    key = str(row[1]["body_key"])
    assert object_storage.chat_body_storage_generation(key, uid) == 0


def test_idempotent_clear_during_slow_put_keeps_durable_cleanup_guard(
    backend_env, monkeypatch,
):
    """A clear preserves the inline source while a paused offload loses CAS.

    The encrypted row is archived before the upload finishes. The late upload
    cannot promote a pointer into the now-absent live row, so its exact upload
    guard reclaims the unreferenced object while the archived inline ciphertext
    remains available until account deletion.
    """
    upload_started = threading.Event()
    release_upload = threading.Event()

    class _SlowPutS3(_FakeS3):
        def put_object(self, Bucket, Key, Body, **kw):
            upload_started.set()
            assert release_upload.wait(timeout=5)
            return super().put_object(Bucket, Key, Body, **kw)

    client = _SlowPutS3()
    _enable_r2(monkeypatch, client)
    uid = _uid()
    seed_user(uid)
    mid = uuid.uuid4().hex
    client_msg_id = str(uuid.uuid4())
    doc = _file_doc(uid, mid, b"late-idempotent-upload")
    doc["client_msg_id"] = client_msg_id
    outcome: list[object] = []

    def _append() -> None:
        try:
            outcome.append(
                db.chat_append_idempotent(
                    uid,
                    mid,
                    time.time(),
                    doc,
                    100,
                    client_msg_id=client_msg_id,
                    window_sec=600,
                )
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced in test thread
            outcome.append(exc)

    writer = threading.Thread(target=_append, name="slow-idempotent-upload")
    writer.start()
    try:
        assert upload_started.wait(timeout=3)
        assert db.chat_clear(uid) == 1

        assert _storage_generation(uid) == 0
        assert _inventory_state(uid)[0] is False
    finally:
        release_upload.set()
        writer.join(timeout=5)

    assert not writer.is_alive()
    assert len(outcome) == 1 and not isinstance(outcome[0], BaseException)
    assert outcome[0][1] is True
    assert _raw_doc(uid, mid) is None
    archived = _archived_doc(uid, mid)
    assert archived is not None and archived.get("body_ct") is not None

    # Only the per-upload guard can discover and reclaim this late object now.
    _drain_r2(uid)
    assert client.store == {}
    assert _cleanup_keys(uid) == []


def test_hydrate_helper_reconstitutes(backend_env, monkeypatch):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    body = b"hydrate-me"
    db.chat_append(uid, mid, 1.0, _file_doc(uid, mid, body), 100)
    pointer = _raw_doc(uid, mid)
    full = db.hydrate_chat_file_body(uid, pointer)
    assert base64.b64decode(full["body_ct"]) == body
    assert "body_key" not in full


def test_history_item_hydrates_when_body_included(backend_env, monkeypatch):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    body = b"included-body"
    db.chat_append(uid, mid, 1.0, _file_doc(uid, mid, body), 100)
    item = chat_service._chat_history_item(_raw_doc(uid, mid), include_image_body=True)
    assert base64.b64decode(item["body_ct"]) == body
    assert "body_key" not in item
    assert not item.get("body_omitted")


def test_history_item_omits_large_file_without_fetch(backend_env, monkeypatch):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    big = b"x" * 300_000  # > CHAT_HISTORY_INLINE_BODY_CT_MAX (262144)
    db.chat_append(uid, mid, 1.0, _file_doc(uid, mid, big), 100)
    pointer = _raw_doc(uid, mid)
    item = chat_service._chat_history_item(pointer, include_image_body=False)
    # Omitted → reported from the pointer's stored length, no R2 fetch, no body.
    assert item["body_omitted"] is True
    assert item.get("body_ct") is None and "body_key" not in item
    assert item["body_ct_len"] == pointer["body_ct_len"]


def test_phase3_preserves_concurrent_metadata(backend_env, monkeypatch):
    # Simulate another worker writing reply metadata DURING the R2 upload; the
    # atomic pointer flip must not clobber it (P1b regression).
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex

    class _RacingS3(_FakeS3):
        def put_object(self, Bucket, Key, Body, **kw):
            db.chat_update_metadata(uid, mid, {"reply_status": "replied", "reply_message_id": "r1"})
            return super().put_object(Bucket, Key, Body, **kw)

    _enable_r2(monkeypatch, _RacingS3())
    db.chat_append(uid, mid, 1.0, _file_doc(uid, mid), 100)
    raw = _raw_doc(uid, mid)
    assert raw["reply_status"] == "replied"        # concurrent write survived
    assert raw["reply_message_id"] == "r1"
    assert raw.get("body_key") and "body_ct" not in raw  # and the flip still happened


def test_concurrent_same_id_rewrite_cannot_overwrite_winners_object(
    backend_env, monkeypatch,
):
    uid = _uid()
    seed_user(uid)
    mid = uuid.uuid4().hex
    puts: list[str] = []

    class _InterleavingS3(_FakeS3):
        nested = False

        def put_object(self, Bucket, Key, Body, **kw):
            puts.append(Key)
            if not self.nested:
                self.nested = True
                # The newer row and upload win completely while the older
                # attempt is paused before its own object is stored.
                db.chat_append(
                    uid,
                    mid,
                    2.0,
                    _image_doc(uid, mid, b"new-winner"),
                    100,
                )
            return super().put_object(Bucket, Key, Body, **kw)

    client = _InterleavingS3()
    _enable_r2(monkeypatch, client)
    db.chat_append(uid, mid, 1.0, _image_doc(uid, mid, b"stale-loser"), 100)
    _drain_r2(uid)

    assert len(puts) == 2 and puts[0] != puts[1]
    raw = _raw_doc(uid, mid)
    winner_key = str(raw["body_key"])
    assert winner_key == puts[1]
    assert (_BUCKET, puts[0]) not in client.store
    assert client.store == {(_BUCKET, winner_key): b"new-winner"}
    hydrated = db.hydrate_chat_file_body(uid, raw)
    assert base64.b64decode(hydrated["body_ct"]) == b"new-winner"


@pytest.mark.parametrize("purge", ["delete", "clear"])
def test_delete_or_clear_during_upload_reclaims_late_object(
    backend_env, monkeypatch, purge,
):
    uid = _uid()
    seed_user(uid)
    mid = uuid.uuid4().hex

    class _PurgingS3(_FakeS3):
        def put_object(self, Bucket, Key, Body, **kw):
            if purge == "delete":
                assert db.chat_delete(uid, mid) is True
            else:
                assert db.chat_clear(uid) == 1
            # Simulate the upload completing only after the row/prefix purge.
            return super().put_object(Bucket, Key, Body, **kw)

    client = _PurgingS3()
    _enable_r2(monkeypatch, client)
    db.chat_append(uid, mid, 1.0, _image_doc(uid, mid, b"must-not-survive"), 100)
    _drain_r2(uid)

    assert _raw_doc(uid, mid) is None
    archived = _archived_doc(uid, mid)
    if purge == "clear":
        assert archived is not None and archived.get("body_ct") is not None
    else:
        assert archived is None
    assert client.store == {}


def test_clear_waits_for_inflight_pointer_promotion_then_archives_its_key(
    backend_env, monkeypatch,
):
    """Exercise the promotion-wins ordering of the lifecycle fence.

    The pointer transaction holds the per-user lifecycle row before clear
    starts. Clear must wait, then archive and hide the newly promoted row in its
    own transaction. The R2 object remains referenced by that encrypted archive
    until account deletion.
    """
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    promotion_has_fence = threading.Event()
    allow_promotion = threading.Event()
    clear_started = threading.Event()
    clear_done = threading.Event()
    original_lock = db._lock_chat_r2_lifecycle_on_cursor
    promoter_lock_count = 0
    append_errors: list[BaseException] = []
    clear_errors: list[BaseException] = []
    clear_results: list[int | None] = []

    def _pause_fourth_promoter_lock(cur, lock_uid: str) -> int:
        nonlocal promoter_lock_count
        generation = original_lock(cur, lock_uid)
        if threading.current_thread().name == "r2-promoter":
            promoter_lock_count += 1
            # insert, guard reservation, pre-upload validation, promotion
            if promoter_lock_count == 4:
                promotion_has_fence.set()
                assert allow_promotion.wait(5), "test did not release promotion"
        return generation

    monkeypatch.setattr(
        db, "_lock_chat_r2_lifecycle_on_cursor", _pause_fourth_promoter_lock,
    )

    def _append() -> None:
        try:
            db.chat_append_strict(
                uid, mid, 1.0, _image_doc(uid, mid, b"promotion-wins"), 100,
            )
        except BaseException as exc:  # surfaced in the asserting thread
            append_errors.append(exc)

    def _clear() -> None:
        clear_started.set()
        try:
            clear_results.append(db.chat_clear(uid))
        except BaseException as exc:  # surfaced in the asserting thread
            clear_errors.append(exc)
        finally:
            clear_done.set()

    promoter = threading.Thread(target=_append, name="r2-promoter")
    promoter.start()
    assert promotion_has_fence.wait(5), "promotion never acquired lifecycle fence"
    clearer = threading.Thread(target=_clear, name="r2-clearer")
    clearer.start()
    assert clear_started.wait(5)
    assert not clear_done.wait(0.1), "clear bypassed the held lifecycle fence"

    allow_promotion.set()
    promoter.join(5)
    clearer.join(5)
    assert not promoter.is_alive() and not clearer.is_alive()
    assert append_errors == [] and clear_errors == []
    assert clear_results == [1]
    assert promoter_lock_count >= 4
    assert _raw_doc(uid, mid) is None
    assert _storage_generation(uid) == 0

    db.reconcile_chat_r2_cleanup(uid, include_inventory=False)
    archived = _archived_doc(uid, mid)
    assert archived is not None and archived.get("body_key")
    assert client.store == {
        (_BUCKET, archived["body_key"]): b"promotion-wins",
    }
    assert _cleanup_keys(uid) == []


def test_delayed_stale_pointer_replay_cannot_restore_replaced_key(
    backend_env, monkeypatch,
):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex

    db.chat_append_strict(uid, mid, 1.0, _image_doc(uid, mid, b"K1"), 100)
    stale_pointer = dict(_raw_doc(uid, mid))
    stale_key = str(stale_pointer["body_key"])
    db.chat_append_strict(uid, mid, 2.0, _image_doc(uid, mid, b"K2"), 100)
    _drain_r2(uid)
    current_key = _body_key(uid, mid)

    assert current_key != stale_key
    assert (_BUCKET, stale_key) not in client.store
    with pytest.raises(db.ChatPointerReplayConflict):
        db.chat_append_strict(uid, mid, 1.0, stale_pointer, 100)
    _drain_r2(uid)
    _drain_r2(uid)

    assert _body_key(uid, mid) == current_key
    assert client.store == {(_BUCKET, current_key): b"K2"}


def test_pointer_only_replay_after_clear_cannot_recreate_deleted_row(
    backend_env, monkeypatch,
):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    db.chat_append_strict(uid, mid, 1.0, _image_doc(uid, mid, b"retired"), 100)
    stale_pointer = dict(_raw_doc(uid, mid))

    assert db.chat_clear(uid) == 1
    with pytest.raises(db.ChatPointerReplayConflict):
        db.chat_append_strict(uid, mid, 1.0, stale_pointer, 100)
    _drain_r2(uid)

    assert _raw_doc(uid, mid) is None
    assert _archived_doc(uid, mid) == stale_pointer
    assert client.store == {(_BUCKET, stale_pointer["body_key"]): b"retired"}


def test_account_deletion_purges_clear_archived_body(
    backend_env, monkeypatch,
):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    db.chat_append_strict(uid, mid, 1.0, _image_doc(uid, mid, b"until-account-delete"), 100)
    key = _body_key(uid, mid)

    assert db.chat_clear(uid) == 1
    assert _archived_doc(uid, mid)["body_key"] == key
    assert client.store == {(_BUCKET, key): b"until-account-delete"}

    db.delete_user(uid)
    _drain_r2(uid)

    assert _archived_doc(uid, mid) is None
    assert client.store == {}
    assert _cleanup_keys(uid) == []


def test_clear_archive_preserves_old_body_and_post_clear_append(
    backend_env, monkeypatch,
):
    """Clear hides old chat without retiring either old or new ciphertext."""
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid)
    old_mid, new_mid = uuid.uuid4().hex, uuid.uuid4().hex
    db.chat_append_strict(uid, old_mid, 1.0, _image_doc(uid, old_mid, b"old"), 100)
    old_key = _body_key(uid, old_mid)
    assert db.chat_clear(uid) == 1
    db.chat_append_strict(
        uid, new_mid, 2.0, _image_doc(uid, new_mid, b"new"), 100,
    )
    _drain_r2(uid)

    new_key = _body_key(uid, new_mid)
    assert _storage_generation(uid) == 0
    assert object_storage.chat_body_storage_generation(old_key, uid) == 0
    assert object_storage.chat_body_storage_generation(new_key, uid) == 0
    assert _archived_doc(uid, old_mid)["body_key"] == old_key
    assert client.store == {
        (_BUCKET, old_key): b"old",
        (_BUCKET, new_key): b"new",
    }
    assert base64.b64decode(
        db.hydrate_chat_file_body(uid, _raw_doc(uid, new_mid))["body_ct"]
    ) == b"new"


def test_upload_guard_survives_crash_and_periodic_reconcile_reclaims_object(
    backend_env, monkeypatch,
):
    class _StoredButAckLostS3(_FakeS3):
        def put_object(self, Bucket, Key, Body, **kw):
            super().put_object(Bucket, Key, Body, **kw)
            raise RuntimeError("connection lost after R2 accepted PUT")

    client = _StoredButAckLostS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    real_reconcile_one = db._reconcile_one_chat_r2_cleanup
    # Model a hard process death: the request-local finally cannot drain, but
    # the pre-upload DB tombstone must already be committed.
    monkeypatch.setattr(db, "_reconcile_one_chat_r2_cleanup", lambda *_a: False)

    db.chat_append_strict(uid, mid, 1.0, _image_doc(uid, mid, b"orphan"), 100)

    raw = _raw_doc(uid, mid)
    assert raw.get("body_ct") is not None and raw.get("body_key") is None
    assert len(client.store) == 1
    orphan_key = next(iter(client.store))[1]
    assert _cleanup_keys(uid) == [orphan_key]

    monkeypatch.setattr(db, "_reconcile_one_chat_r2_cleanup", real_reconcile_one)
    assert db.reconcile_chat_r2_cleanup(uid, include_inventory=False) == 1
    assert client.store == {}
    assert _cleanup_keys(uid) == []


def test_replacement_tombstone_survives_process_death_before_worker_drain(
    backend_env, monkeypatch,
):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    db.chat_append_strict(uid, mid, 1.0, _image_doc(uid, mid, b"old"), 100)
    old_key = _body_key(uid, mid)
    real_reconcile = db.reconcile_chat_r2_cleanup
    monkeypatch.setattr(db, "reconcile_chat_r2_cleanup", lambda *_a, **_k: 0)

    db.chat_append_strict(uid, mid, 2.0, _image_doc(uid, mid, b"new"), 100)

    new_key = _body_key(uid, mid)
    assert new_key != old_key
    assert (_BUCKET, old_key) in client.store
    assert old_key in _cleanup_keys(uid)

    monkeypatch.setattr(db, "reconcile_chat_r2_cleanup", real_reconcile)
    assert db.reconcile_chat_r2_cleanup(uid, include_inventory=False) >= 1
    assert (_BUCKET, old_key) not in client.store
    assert client.store == {(_BUCKET, new_key): b"new"}


def test_empty_clear_keeps_durable_storage_retention_generation(
    backend_env, monkeypatch,
):
    _enable_r2(monkeypatch, _FakeS3())
    uid = _uid(); seed_user(uid)

    assert db.chat_clear(uid) == 0
    assert _storage_generation(uid) == 0
    assert db.chat_clear(uid) == 0
    assert _storage_generation(uid) == 0


def test_account_cascade_keeps_cleanup_intent_after_user_row_is_gone(
    backend_env, monkeypatch,
):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    db.chat_append_strict(uid, mid, 1.0, _image_doc(uid, mid, b"account"), 100)
    key = _body_key(uid, mid)
    real_reconcile = db.reconcile_chat_r2_cleanup
    monkeypatch.setattr(db, "reconcile_chat_r2_cleanup", lambda *_a, **_k: 0)

    db.delete_user(uid)

    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM users WHERE user_id=%s", (uid,),
        ).fetchone() is None
        assert conn.execute(
            "SELECT generation FROM chat_r2_lifecycle WHERE user_id=%s", (uid,),
        ).fetchone() == (1,)
        fk_count = conn.execute(
            "SELECT COUNT(*) FROM information_schema.table_constraints "
            "WHERE table_name='chat_r2_cleanup' "
            "AND constraint_type='FOREIGN KEY'"
        ).fetchone()[0]
    assert fk_count == 0
    assert _cleanup_keys(uid) == [key]
    assert (_BUCKET, key) in client.store

    monkeypatch.setattr(db, "reconcile_chat_r2_cleanup", real_reconcile)
    assert db.reconcile_chat_r2_cleanup(uid) >= 1
    assert client.store == {}
    assert _cleanup_keys(uid) == []


def test_deleted_account_inventory_failure_is_durable_and_retryable(
    backend_env, monkeypatch,
):
    class _FailFirstListS3(_FakeS3):
        failed = False

        def list_objects_v2(self, Bucket, Prefix, **kw):
            if not self.failed:
                self.failed = True
                raise RuntimeError("temporary list outage")
            return super().list_objects_v2(Bucket, Prefix, **kw)

    client = _FailFirstListS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid)
    legacy_key = f"chatfiles/{uid}/legacy-message"
    client.store[(_BUCKET, legacy_key)] = b"legacy"

    db.delete_user(uid)

    # Account deletion commits and returns without touching R2. The durable
    # marker survives the users-row cascade and owns all future retries.
    assert client.store == {(_BUCKET, legacy_key): b"legacy"}
    assert _storage_generation(uid) == 1
    assert _inventory_state(uid) == (True, 0, "")

    assert db.reconcile_chat_r2_cleanup(uid) == 0
    pending, attempts, error = _inventory_state(uid)
    assert pending is True and attempts == 1
    assert "temporary list outage" in error

    # Make the deterministic test retry immediately instead of waiting for the
    # production backoff window.
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE chat_r2_lifecycle SET inventory_next_attempt_at=now() "
            "WHERE user_id=%s",
            (uid,),
        )
    assert db.reconcile_chat_r2_cleanup(uid) == 1
    assert _inventory_state(uid) == (False, 0, "")
    assert client.store == {}


def test_failed_cleanup_row_backs_off_so_newer_work_is_not_starved(
    backend_env, monkeypatch,
):
    uid = _uid(); seed_user(uid)
    poison_key = f"chatfiles/{uid}/g0/poison/{uuid.uuid4().hex}"
    good_key = f"chatfiles/{uid}/g0/good/{uuid.uuid4().hex}"

    class _PoisonDeleteS3(_FakeS3):
        def delete_object(self, Bucket, Key, **kw):
            if Key == poison_key:
                raise RuntimeError("permanent poison")
            return super().delete_object(Bucket, Key, **kw)

    client = _PoisonDeleteS3()
    _enable_r2(monkeypatch, client)
    client.store[(_BUCKET, poison_key)] = b"poison"
    client.store[(_BUCKET, good_key)] = b"good"
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO chat_r2_cleanup "
            "(body_key,user_id,generation,reason,created_at) VALUES "
            "(%s,%s,0,'test',now()-interval '1 minute'),"
            "(%s,%s,0,'test',now())",
            (poison_key, uid, good_key, uid),
        )

    assert db.reconcile_chat_r2_cleanup(
        uid, limit=1, include_inventory=False,
    ) == 0
    with db.get_pool().connection() as conn:
        retry = conn.execute(
            "SELECT attempt_count,next_attempt_at > now() "
            "FROM chat_r2_cleanup WHERE body_key=%s",
            (poison_key,),
        ).fetchone()
    assert retry == (1, True)

    # The poisoned oldest row is no longer due, so the next pass reaches the
    # newer tombstone instead of retrying the same FIFO head forever.
    assert db.reconcile_chat_r2_cleanup(
        uid, limit=1, include_inventory=False,
    ) == 1
    assert (_BUCKET, good_key) not in client.store
    assert (_BUCKET, poison_key) in client.store


def test_bulk_user_removal_advances_generation_and_schedules_inventory(
    backend_env, monkeypatch,
):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid)
    legacy_key = f"chatimages/{uid}/legacy-image"
    client.store[(_BUCKET, legacy_key)] = b"legacy"
    with db.get_pool().connection() as conn:
        snapshot = [
            {**(row[1] or {}), "user_id": row[0]}
            for row in conn.execute("SELECT user_id,doc FROM users").fetchall()
            if row[0] != uid
        ]

    db.save_all_users(snapshot)

    assert _storage_generation(uid) == 1
    assert _inventory_state(uid) == (True, 0, "")
    assert client.store == {(_BUCKET, legacy_key): b"legacy"}


def test_migration_installs_generation_column_and_retirement_trigger(backend_env):
    with db.get_pool().connection() as conn:
        storage_generation = conn.execute(
            "SELECT is_nullable,column_default FROM information_schema.columns "
            "WHERE table_name='chat_messages' "
            "AND column_name='storage_generation'"
        ).fetchone()
        trigger = conn.execute(
            "SELECT 1 FROM pg_trigger "
            "WHERE tgname='chat_messages_retire_r2_body' AND NOT tgisinternal"
        ).fetchone()
        cleanup_columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='chat_r2_cleanup'"
            ).fetchall()
        }
        lifecycle_columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='chat_r2_lifecycle'"
            ).fetchall()
        }
    assert storage_generation is not None
    assert storage_generation[0] == "NO"
    assert storage_generation[1] is not None and "0" in storage_generation[1]
    assert trigger == (1,)
    assert "next_attempt_at" in cleanup_columns
    assert {
        "inventory_pending", "inventory_next_attempt_at",
        "inventory_attempt_count", "inventory_last_error",
    } <= lifecycle_columns


def test_envelope_cas_miss_deletes_private_upload_and_keeps_new_inline_body(
    backend_env, monkeypatch,
):
    uid = _uid()
    seed_user(uid)
    mid = uuid.uuid4().hex
    newer = _image_doc(uid, mid, b"new-inline-envelope")

    class _EnvelopeRewriteS3(_FakeS3):
        def put_object(self, Bucket, Key, Body, **kw):
            # Change an envelope-defining field while the old ciphertext is in
            # flight. The old attempt must not promote over this exact version.
            db.chat_update_metadata(
                uid,
                mid,
                {"body_ct": newer["body_ct"], "nonce": "new-nonce"},
            )
            return super().put_object(Bucket, Key, Body, **kw)

    client = _EnvelopeRewriteS3()
    _enable_r2(monkeypatch, client)
    db.chat_append(uid, mid, 1.0, _image_doc(uid, mid, b"old-upload"), 100)
    _drain_r2(uid)

    raw = _raw_doc(uid, mid)
    assert raw["body_ct"] == newer["body_ct"]
    assert raw["nonce"] == "new-nonce"
    assert "body_key" not in raw
    assert client.store == {}


def test_text_message_stays_inline(backend_env, monkeypatch):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    doc = _file_doc(uid, mid); doc["content_type"] = "text"
    db.chat_append(uid, mid, 1.0, doc, 100)
    raw = _raw_doc(uid, mid)
    assert "body_ct" in raw and "body_key" not in raw
    assert not client.store


def test_upload_failure_falls_back_to_inline(backend_env, monkeypatch):
    _enable_r2(monkeypatch, _FakeS3(fail_put=True))
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    db.chat_append(uid, mid, 1.0, _file_doc(uid, mid), 100)
    raw = _raw_doc(uid, mid)
    assert "body_ct" in raw and "body_key" not in raw   # stayed inline, readable


def test_disabled_r2_stays_inline(backend_env, monkeypatch):
    monkeypatch.delenv("R2_CHAT_FILES_BUCKET", raising=False)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    db.chat_append(uid, mid, 1.0, _file_doc(uid, mid), 100)
    raw = _raw_doc(uid, mid)
    assert "body_ct" in raw and "body_key" not in raw


def test_chat_delete_purges_but_clear_retains_r2(backend_env, monkeypatch):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid)
    m1, m2 = uuid.uuid4().hex, uuid.uuid4().hex
    db.chat_append(uid, m1, 1.0, _file_doc(uid, m1), 100)
    db.chat_append(uid, m2, 2.0, _file_doc(uid, m2), 100)
    k1 = _body_key(uid, m1)
    assert len(client.store) == 2
    assert db.chat_delete(uid, m1) is True
    _drain_r2(uid)
    assert (_BUCKET, k1) not in client.store
    db.chat_clear(uid)
    _drain_r2(uid)
    archived = _archived_doc(uid, m2)
    assert archived is not None and archived.get("body_key")
    assert client.store == {(_BUCKET, archived["body_key"]): b"PK\x03\x04docx-bytes"}


# --------------------------------------------------------------------------- #
# Images ride the same offload. A photo's ciphertext is 1-2MB, which TOASTs the
# chat_messages row and is then carried through every WAL record, WAL-G backup
# and TEE mirror pass — the exact cost the file offload was built to avoid.
# --------------------------------------------------------------------------- #


def _image_doc(uid: str, mid: str, body: bytes = b"\xff\xd8\xff-jpeg-bytes") -> dict:
    doc = _file_doc(uid, mid, body)
    doc["content_type"] = "image"
    doc["image_mime"] = "image/jpeg"
    doc.pop("file_name", None)
    doc.pop("file_mime", None)
    return doc


def test_image_offloads_to_r2_like_a_file(backend_env, monkeypatch):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    body = b"\xff\xd8\xff" + b"photo-bytes" * 100
    db.chat_append(uid, mid, 1.0, _image_doc(uid, mid, body), 100)

    raw = _raw_doc(uid, mid)
    # Images get their OWN prefix (own lifecycle rule / usage accounting), while
    # files stay under chatfiles/ — same bucket, no collision.
    key = str(raw["body_key"])
    _assert_versioned_key(key, uid, mid, "image")
    assert "body_ct" not in raw
    assert raw["body_ct_len"] == len(base64.b64encode(body).decode())
    assert client.store[(_BUCKET, key)] == body


def test_image_pointer_hydrates_when_body_is_delivered(backend_env, monkeypatch):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    body = b"\xff\xd8\xff-the-actual-pixels"
    db.chat_append(uid, mid, 1.0, _image_doc(uid, mid, body), 100)
    item = chat_service._chat_history_item(_raw_doc(uid, mid), include_image_body=True)
    assert base64.b64decode(item["body_ct"]) == body
    assert "body_key" not in item and not item.get("body_omitted")


def test_omitted_image_never_touches_r2(backend_env, monkeypatch):
    # The resident consumer's text-only reads (include_image_body=false) must stay
    # free: an omitted body is reported from the pointer's stored length, never
    # fetched. Otherwise offloading images would make every text turn pay for them.
    client = _CountingS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    db.chat_append(uid, mid, 1.0, _image_doc(uid, mid), 100)
    pointer = _raw_doc(uid, mid)

    item = chat_service._chat_history_item(pointer, include_image_body=False)
    assert item["body_omitted"] is True
    assert item["body_omitted_reason"] == "image_body"
    assert item.get("body_ct") is None and "body_key" not in item
    assert item["body_ct_len"] == pointer["body_ct_len"]
    assert client.gets == []                                  # zero R2 round-trips


def test_append_limit_preserves_prior_image_and_r2_object(backend_env, monkeypatch):
    # The append limit is a hot-cache hint, not a retention policy. The older
    # durable pointer and its encrypted R2 body must remain readable.
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid)
    m1, m2 = uuid.uuid4().hex, uuid.uuid4().hex
    db.chat_append(uid, m1, 1.0, _image_doc(uid, m1), 100)
    k1 = _body_key(uid, m1)
    db.chat_append(uid, m2, 2.0, _image_doc(uid, m2), 1)
    k2 = _body_key(uid, m2)
    with db.get_pool().connection() as conn:
        queued = conn.execute(
            "SELECT body_key FROM chat_r2_cleanup WHERE user_id=%s",
            (uid,),
        ).fetchall()
    assert queued == [], "ordinary append must not queue any live body for retirement"
    _drain_r2(uid)
    assert (_BUCKET, k1) in client.store
    assert (_BUCKET, k2) in client.store


def test_resident_linked_reply_offloads_and_preserves_prior_body(
    backend_env, monkeypatch,
):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid()
    seed_user(uid)
    _set_resident_runtime(uid)
    evicted, parent, reply = (uuid.uuid4().hex for _ in range(3))
    db.chat_append(uid, evicted, 1.0, _image_doc(uid, evicted), 100)
    evicted_key = _body_key(uid, evicted)
    db.chat_append(
        uid,
        parent,
        2.0,
        {"id": parent, "role": "user", "body_ct": "question"},
        100,
    )
    reply_doc = _image_doc(uid, reply, b"resident-linked-image")
    reply_doc.update({
        "role": "openclaw",
        "source": "chat",
        "resident_delivery_id": reply,
    })

    _seq, inserted, _parent_doc, _persisted = db.chat_append_resident_reply(
        uid,
        reply,
        3.0,
        reply_doc,
        2,
        parent_msg_id=parent,
        replied_by="resident-test",
    )

    assert inserted is True
    _drain_r2(uid)
    assert (_BUCKET, evicted_key) in client.store
    raw = _raw_doc(uid, reply)
    reply_key = str(raw["body_key"])
    _assert_versioned_key(reply_key, uid, reply, "image")
    assert client.store[(_BUCKET, reply_key)] == b"resident-linked-image"
    assert "body_ct" not in raw

    replay_seq, replay_inserted, replay_parent, replay_doc = (
        db.chat_append_resident_reply(
            uid,
            reply,
            3.0,
            reply_doc,
            2,
            parent_msg_id=parent,
            replied_by="resident-test",
        )
    )
    assert replay_seq == _seq
    assert replay_inserted is False
    assert replay_parent["reply_message_id"] == reply
    assert replay_doc["body_key"] == reply_key
    assert set(client.store) == {(_BUCKET, evicted_key), (_BUCKET, reply_key)}


def test_resident_unlinked_message_offloads_and_preserves_prior_body(
    backend_env, monkeypatch,
):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid()
    seed_user(uid)
    _set_resident_runtime(uid)
    evicted, message = uuid.uuid4().hex, uuid.uuid4().hex
    db.chat_append(uid, evicted, 1.0, _image_doc(uid, evicted), 100)
    evicted_key = _body_key(uid, evicted)
    message_doc = _file_doc(uid, message, b"resident-unlinked-file")
    message_doc.update({"role": "openclaw", "source": "proactive_job"})

    _seq, inserted, _persisted = db.chat_append_resident_message(
        uid,
        message,
        2.0,
        message_doc,
        1,
    )

    assert inserted is True
    _drain_r2(uid)
    assert (_BUCKET, evicted_key) in client.store
    raw = _raw_doc(uid, message)
    message_key = str(raw["body_key"])
    _assert_versioned_key(message_key, uid, message, "file")
    assert client.store[(_BUCKET, message_key)] == b"resident-unlinked-file"
    assert "body_ct" not in raw

    replay_seq, replay_inserted, replay_doc = db.chat_append_resident_message(
        uid,
        message,
        2.0,
        message_doc,
        1,
    )
    assert replay_seq == _seq
    assert replay_inserted is False
    assert replay_doc["body_key"] == message_key
    assert set(client.store) == {(_BUCKET, evicted_key), (_BUCKET, message_key)}


def test_v2_atomic_reply_preserves_prior_r2_body(backend_env, monkeypatch):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid()
    seed_user(uid)
    evicted, parent, reply = (uuid.uuid4().hex for _ in range(3))
    db.chat_append(uid, evicted, 1.0, _image_doc(uid, evicted), 100)
    evicted_key = _body_key(uid, evicted)
    db.chat_append(
        uid,
        parent,
        2.0,
        {"id": parent, "role": "user", "body_ct": "question"},
        100,
    )
    parent_seq = db.chat_seq_for_msg_id(uid, parent)
    assert parent_seq is not None

    db.chat_append_effect_with_cursor(
        uid,
        reply,
        3.0,
        {"id": reply, "role": "openclaw", "body_ct": "answer"},
        2,
        parent_seq,
    )

    _drain_r2(uid)
    assert (_BUCKET, evicted_key) in client.store
    assert _raw_doc(uid, evicted) is not None


def test_v2_atomic_send_offloads_and_preserves_prior_body(
    backend_env, monkeypatch,
):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid()
    seed_user(uid)
    evicted, message = uuid.uuid4().hex, uuid.uuid4().hex
    db.chat_append(uid, evicted, 1.0, _image_doc(uid, evicted), 100)
    evicted_key = _body_key(uid, evicted)
    message_doc = _file_doc(uid, message, b"v2-atomic-send-file")

    _seq, job_id = db.chat_append_and_enqueue(
        uid,
        message,
        2.0,
        message_doc,
        1,
        "chat",
    )

    assert job_id > 0
    _drain_r2(uid)
    assert (_BUCKET, evicted_key) in client.store
    raw = _raw_doc(uid, message)
    message_key = str(raw["body_key"])
    _assert_versioned_key(message_key, uid, message, "file")
    assert client.store[(_BUCKET, message_key)] == b"v2-atomic-send-file"
    assert "body_ct" not in raw


def test_v2_atomic_send_replay_and_same_id_replacement_manage_object_versions(
    backend_env, monkeypatch,
):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid()
    seed_user(uid)
    mid = uuid.uuid4().hex

    db.chat_append_and_enqueue(
        uid,
        mid,
        1.0,
        _file_doc(uid, mid, b"atomic-old"),
        100,
        "chat",
    )
    old_pointer = dict(_raw_doc(uid, mid))
    old_key = str(old_pointer["body_key"])

    # A transport replay of the already-persisted pointer must retain its exact
    # object even though the job operation coalesces again.
    db.chat_append_and_enqueue(uid, mid, 1.0, old_pointer, 100, "chat")
    assert _body_key(uid, mid) == old_key
    assert client.store[(_BUCKET, old_key)] == b"atomic-old"

    db.chat_append_and_enqueue(
        uid,
        mid,
        2.0,
        _file_doc(uid, mid, b"atomic-new"),
        100,
        "chat",
    )
    new_pointer = _raw_doc(uid, mid)
    new_key = str(new_pointer["body_key"])
    assert new_key != old_key
    _drain_r2(uid)
    assert (_BUCKET, old_key) not in client.store
    assert client.store == {(_BUCKET, new_key): b"atomic-new"}


def test_history_page_prefetches_every_delivered_body(backend_env, monkeypatch):
    # The point of hydrate_history_page: a page of N images resolves all N bodies
    # (concurrently) rather than N serial GETs inside the render loop.
    client = _CountingS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid)
    bodies = {}
    for i in range(4):
        mid = uuid.uuid4().hex
        bodies[mid] = f"pixels-{i}".encode()
        db.chat_append(uid, mid, float(i + 1), _image_doc(uid, mid, bodies[mid]), 100)

    msgs = db.chat_load(uid)
    assert all(m.get("body_key") and m.get("body_ct") is None for m in msgs)  # all pointers

    hydrated = chat_service.hydrate_history_page(msgs, include_image_body=True)
    assert len(client.gets) == 4                              # one GET per pointer
    for m in hydrated:
        assert base64.b64decode(m["body_ct"]) == bodies[m["id"]]

    # And the render pass adds no further fetches — its lazy hydrate no-ops.
    client.gets.clear()
    out = [chat_service._chat_history_item(m, include_image_body=True) for m in hydrated]
    assert client.gets == []
    assert all(not o.get("body_omitted") for o in out)


def test_history_page_skips_bodies_the_page_omits(backend_env, monkeypatch):
    # include_image_body=false → the page delivers no image bodies, so the
    # pre-fetch must not pull them either.
    client = _CountingS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid)
    for i in range(3):
        mid = uuid.uuid4().hex
        db.chat_append(uid, mid, float(i + 1), _image_doc(uid, mid), 100)

    msgs = db.chat_load(uid)
    out = chat_service.hydrate_history_page(msgs, include_image_body=False)
    assert client.gets == []
    assert all(m.get("body_ct") is None for m in out)


def test_pointer_replay_survives_then_replacement_reclaims_old_object(
    backend_env, monkeypatch,
):
    # /v1/content/swap (visibility toggle / rewrap) writes a client-built envelope
    # back through chat_append. When the row was already offloaded, the doc it hands
    # us carries BOTH a stale body_key and the NEW body_ct. That must converge on a
    # pointer whose R2 object holds the new ciphertext — not resurrect the old one.
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    db.chat_append(uid, mid, 1.0, _image_doc(uid, mid, b"old-ciphertext"), 100)

    swapped = dict(_raw_doc(uid, mid))                        # pointer: body_key, no body_ct
    assert swapped.get("body_key") and swapped.get("body_ct") is None
    old_key = str(swapped["body_key"])
    _assert_versioned_key(old_key, uid, mid, "image")

    # An exact pointer replay is not a replacement and must not reap the object
    # it still references.
    db.chat_append(uid, mid, 1.0, dict(swapped), 100)
    assert _body_key(uid, mid) == old_key
    assert client.store[(_BUCKET, old_key)] == b"old-ciphertext"

    swapped["body_ct"] = base64.b64encode(b"new-ciphertext").decode()   # ← swap re-adds it
    db.chat_append(uid, mid, 1.0, swapped, 100)

    raw = _raw_doc(uid, mid)
    assert raw.get("body_key") and "body_ct" not in raw       # re-offloaded, still slim
    new_key = str(raw["body_key"])
    _assert_versioned_key(new_key, uid, mid, "image")
    assert new_key != old_key
    _drain_r2(uid)
    assert (_BUCKET, old_key) not in client.store
    assert client.store[(_BUCKET, new_key)] == b"new-ciphertext"
    assert base64.b64decode(db.hydrate_chat_file_body(uid, raw)["body_ct"]) == b"new-ciphertext"


def test_account_purge_clears_both_prefixes(backend_env, monkeypatch):
    # Images and files now live under DIFFERENT prefixes. An account reset that swept
    # only chatfiles/ would leave a deleted account's photo ciphertext in R2.
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid)
    f_mid, i_mid = uuid.uuid4().hex, uuid.uuid4().hex
    db.chat_append(uid, f_mid, 1.0, _file_doc(uid, f_mid), 100)
    db.chat_append(uid, i_mid, 2.0, _image_doc(uid, i_mid), 100)
    f_key, i_key = _body_key(uid, f_mid), _body_key(uid, i_mid)
    assert (_BUCKET, f_key) in client.store
    assert (_BUCKET, i_key) in client.store

    db.delete_user(uid)
    _drain_r2(uid)
    assert not client.store                                   # BOTH prefixes swept


def test_a_body_key_from_an_older_layout_still_resolves(backend_env, monkeypatch):
    # Reads/deletes use the key STORED ON THE ROW, never a recomputed one. So a row
    # written before images got their own prefix (body_key under chatfiles/) still
    # hydrates — which is what makes the layout safe to change at all.
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    legacy_key = f"chatfiles/{uid}/{mid}"                     # old layout, image content
    client.store[(_BUCKET, legacy_key)] = b"legacy-pixels"
    legacy_row = {
        "id": mid, "role": "user", "ts": 1.0, "content_type": "image",
        "body_key": legacy_key, "body_ct_len": 20, "owner_user_id": uid,
    }

    full = db.hydrate_chat_file_body(uid, legacy_row)
    assert base64.b64decode(full["body_ct"]) == b"legacy-pixels"

    # And it's reclaimable — the delete follows the same stored key.
    object_storage.delete_chat_body(legacy_key, uid)
    assert (_BUCKET, legacy_key) not in client.store


def test_a_pointer_to_another_users_object_is_refused(backend_env, monkeypatch):
    # body_key is DATA now (that's what buys legacy-layout support), so it can be
    # wrong — a bad migration or hand-repaired row could name someone else's key.
    # Reads must not hand this user another user's ciphertext, and deletes must not
    # destroy another user's object. Enforced in object_storage, so no caller can
    # skip it.
    client = _CountingS3()
    _enable_r2(monkeypatch, client)
    victim, attacker = _uid(), _uid()
    seed_user(victim); seed_user(attacker)
    v_mid = uuid.uuid4().hex
    db.chat_append(victim, v_mid, 1.0, _image_doc(victim, v_mid, b"victim-pixels"), 100)
    victim_key = _body_key(victim, v_mid)
    assert (_BUCKET, victim_key) in client.store

    # A row owned by `attacker` whose body_key points at the victim's object.
    poisoned = {
        "id": uuid.uuid4().hex, "role": "user", "ts": 1.0, "content_type": "image",
        "body_key": victim_key, "body_ct_len": 20, "owner_user_id": attacker,
    }
    out = db.hydrate_chat_file_body(attacker, poisoned)
    assert out.get("body_ct") is None                  # no cross-user read
    assert client.gets == []                           # not even attempted

    object_storage.delete_chat_body(victim_key, attacker)
    assert (_BUCKET, victim_key) in client.store       # victim's object survives

    # The rightful owner is of course still served.
    assert object_storage.get_chat_body(victim_key, victim) is not None


def test_history_page_survives_a_failed_body_fetch(backend_env, monkeypatch):
    # R2 is now in the path of every image read. A missing/failed object must
    # degrade that ONE message (no body) — never take down the whole page.
    client = _CountingS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid)
    good, gone = uuid.uuid4().hex, uuid.uuid4().hex
    db.chat_append(uid, good, 1.0, _image_doc(uid, good, b"i-am-here"), 100)
    db.chat_append(uid, gone, 2.0, _image_doc(uid, gone, b"i-will-vanish"), 100)
    client.store.pop((_BUCKET, _body_key(uid, gone)))          # object lost

    out = chat_service.hydrate_history_page(db.chat_load(uid), include_image_body=True)
    by_id = {m["id"]: m for m in out}
    assert base64.b64decode(by_id[good]["body_ct"]) == b"i-am-here"
    assert by_id[gone].get("body_ct") is None                 # degraded, not raised
