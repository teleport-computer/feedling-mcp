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
import time
import uuid
from pathlib import Path

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


def test_offload_stores_pointer_and_chat_load_is_lazy(backend_env, monkeypatch):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid); mid = uuid.uuid4().hex
    body = b"PK\x03\x04the-real-file-bytes"
    db.chat_append(uid, mid, 1.0, _file_doc(uid, mid, body), 100)

    # Stored row = slim pointer: body_key + body_ct_len, body_ct gone.
    raw = _raw_doc(uid, mid)
    assert raw["body_key"] == f"chatfiles/{uid}/{mid}"
    assert raw["body_ct_len"] == len(base64.b64encode(body).decode())
    assert "body_ct" not in raw
    # Ciphertext lives in R2.
    assert client.store[(_BUCKET, f"chatfiles/{uid}/{mid}")] == body
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


def test_chat_clear_and_delete_purge_r2(backend_env, monkeypatch):
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid)
    m1, m2 = uuid.uuid4().hex, uuid.uuid4().hex
    db.chat_append(uid, m1, 1.0, _file_doc(uid, m1), 100)
    db.chat_append(uid, m2, 2.0, _file_doc(uid, m2), 100)
    assert len(client.store) == 2
    assert db.chat_delete(uid, m1) is True
    assert (_BUCKET, f"chatfiles/{uid}/{m1}") not in client.store
    db.chat_clear(uid)
    assert not client.store


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
    assert raw["body_key"] == f"chatimages/{uid}/{mid}"      # row is a slim pointer
    assert "body_ct" not in raw
    assert raw["body_ct_len"] == len(base64.b64encode(body).decode())
    assert client.store[(_BUCKET, f"chatimages/{uid}/{mid}")] == body


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


def test_trimmed_image_reclaims_its_r2_object(backend_env, monkeypatch):
    # The ring-buffer trim must reclaim an evicted image's object, or R2 fills up
    # with orphans no row points at.
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid)
    m1, m2 = uuid.uuid4().hex, uuid.uuid4().hex
    db.chat_append(uid, m1, 1.0, _image_doc(uid, m1), 100)
    db.chat_append(uid, m2, 2.0, _image_doc(uid, m2), 1)      # max_messages=1 → m1 evicted
    assert (_BUCKET, f"chatimages/{uid}/{m1}") not in client.store
    assert (_BUCKET, f"chatimages/{uid}/{m2}") in client.store


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


def test_reswapped_image_reoffloads_and_keeps_the_new_ciphertext(backend_env, monkeypatch):
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
    swapped["body_ct"] = base64.b64encode(b"new-ciphertext").decode()   # ← swap re-adds it
    db.chat_append(uid, mid, 1.0, swapped, 100)

    raw = _raw_doc(uid, mid)
    assert raw.get("body_key") and "body_ct" not in raw       # re-offloaded, still slim
    assert client.store[(_BUCKET, f"chatimages/{uid}/{mid}")] == b"new-ciphertext"
    assert base64.b64decode(db.hydrate_chat_file_body(uid, raw)["body_ct"]) == b"new-ciphertext"


def test_account_purge_clears_both_prefixes(backend_env, monkeypatch):
    # Images and files now live under DIFFERENT prefixes. A reset/clear that swept
    # only chatfiles/ would leave a deleted account's photo ciphertext in R2.
    client = _FakeS3()
    _enable_r2(monkeypatch, client)
    uid = _uid(); seed_user(uid)
    f_mid, i_mid = uuid.uuid4().hex, uuid.uuid4().hex
    db.chat_append(uid, f_mid, 1.0, _file_doc(uid, f_mid), 100)
    db.chat_append(uid, i_mid, 2.0, _image_doc(uid, i_mid), 100)
    assert (_BUCKET, f"chatfiles/{uid}/{f_mid}") in client.store
    assert (_BUCKET, f"chatimages/{uid}/{i_mid}") in client.store

    db.chat_clear(uid)
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
    victim_key = f"chatimages/{victim}/{v_mid}"
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
    client.store.pop((_BUCKET, f"chatimages/{uid}/{gone}"))   # object lost

    out = chat_service.hydrate_history_page(db.chat_load(uid), include_image_body=True)
    by_id = {m["id"]: m for m in out}
    assert base64.b64decode(by_id[good]["body_ct"]) == b"i-am-here"
    assert by_id[gone].get("body_ct") is None                 # degraded, not raised
