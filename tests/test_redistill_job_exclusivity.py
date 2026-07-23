"""DB-level exclusivity for resident_redistill jobs (0023_redistill_job_exclusivity.py).

Terminal ``identity-redistill`` (io_cli → consumer IPC, T11) reuses the existing resident
sealed lane (``genesis_core._resident_sealed_import``) tagged with
``job_kind: "resident_redistill"``. A partial unique index on
``genesis_import_jobs (user_id) WHERE source_kind='resident_redistill' AND status IN
('awaiting_resident','processing')`` guarantees at most one such job is ever "in flight"
per user at the DB layer — no in-process lock needed, correct even across worker processes.

These tests exercise the real Postgres constraint (not a mock), so a double-submit race
(two requests hitting different app workers at once) is provably caught, not just "usually
caught by app-level bookkeeping".
"""
import base64
import hashlib
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent))

from genesis import genesis_core  # noqa: E402
import db  # noqa: E402
from conftest import seed_user  # noqa: E402


def _ns(uid):
    return types.SimpleNamespace(user_id=uid)


def _sealed_body(uid: str, ct: bytes, *, client_job_id="cj-1", mode="add_memory", job_kind=None):
    body = {
        "format": "sealed_v1", "client_job_id": client_job_id, "mode": mode,
        "envelope": {
            "v": 1, "id": hashlib.sha256(ct).hexdigest()[:16],
            "body_ct": base64.b64encode(ct).decode("ascii"),
            "nonce": base64.b64encode(b"nonce").decode(),
            "K_user": base64.b64encode(b"ku").decode(),
            "K_enclave": base64.b64encode(b"ke").decode(),
            "owner_user_id": uid, "visibility": "shared", "enclave_pk_fpr": "fpr",
        },
    }
    if job_kind is not None:
        body["job_kind"] = job_kind
    return body


def _import(uid, payload):
    return genesis_core._resident_sealed_import(_ns(uid), payload)


def test_second_concurrent_redistill_job_409s_with_active_job_id():
    uid = "usr_redistill_excl_1"
    seed_user(uid)
    b1, s1 = _import(uid, _sealed_body(
        uid, b"material-one", client_job_id="cj-r1", job_kind="resident_redistill"))
    assert s1 == 200
    job1_id = b1["job"]["job_id"]
    assert db.genesis_get_job(uid, job1_id)["source_kind"] == "resident_redistill"
    assert db.genesis_get_job(uid, job1_id)["status"] == "awaiting_resident"

    # Different material/client_job_id -> different job_id -> hits the partial unique
    # index (NOT the (user_id, job_id) primary key) while job1 is still active.
    b2, s2 = _import(uid, _sealed_body(
        uid, b"material-two", client_job_id="cj-r2", job_kind="resident_redistill"))
    assert s2 == 409
    assert b2["error"] == "redistill_job_active"
    assert b2["active_job_id"] == job1_id


def test_same_request_id_retry_returns_original_job():
    uid = "usr_redistill_excl_2"
    seed_user(uid)
    payload = _sealed_body(
        uid, b"same-material", client_job_id="cj-idem", job_kind="resident_redistill")
    b1, s1 = _import(uid, payload)
    b2, s2 = _import(uid, payload)  # identical payload -> identical job_id -> PK conflict path
    assert s1 == 200 and s2 == 200
    assert b1["job"]["job_id"] == b2["job"]["job_id"]
    # Idempotent retry never conflicts with itself via the exclusivity index.
    assert len(db.genesis_list_chunks(uid, b1["job"]["job_id"])) == 1


def test_new_redistill_job_allowed_after_first_done():
    uid = "usr_redistill_excl_3"
    seed_user(uid)
    b1, s1 = _import(uid, _sealed_body(
        uid, b"material-a", client_job_id="cj-done-1", job_kind="resident_redistill"))
    assert s1 == 200
    job1_id = b1["job"]["job_id"]

    # Consumer reports completion (mirrors resident_complete's db call) -> job1 leaves the
    # exclusivity slot's status set (awaiting_resident/processing).
    db.genesis_complete_job(
        uid, job1_id, output={}, memory_action_count=0,
        identity_status="", persona_ref="", persona_sha256="")
    assert db.genesis_get_job(uid, job1_id)["status"] == "done"

    b2, s2 = _import(uid, _sealed_body(
        uid, b"material-b", client_job_id="cj-done-2", job_kind="resident_redistill"))
    assert s2 == 200
    assert b2["job"]["job_id"] != job1_id


def test_other_job_kinds_unaffected_by_redistill_exclusivity():
    uid = "usr_redistill_excl_4"
    seed_user(uid)
    # Two concurrent ordinary (non-redistill) resident sealed uploads for the same user —
    # the exclusivity index only watches source_kind='resident_redistill', so both succeed.
    b1, s1 = _import(uid, _sealed_body(uid, b"mem-a", client_job_id="cj-mem-1", mode="add_memory"))
    b2, s2 = _import(uid, _sealed_body(uid, b"mem-b", client_job_id="cj-mem-2", mode="add_memory"))
    assert s1 == 200 and s2 == 200
    assert b1["job"]["job_id"] != b2["job"]["job_id"]
    assert db.genesis_get_job(uid, b1["job"]["job_id"])["status"] == "awaiting_resident"
    assert db.genesis_get_job(uid, b2["job"]["job_id"])["status"] == "awaiting_resident"

    # A redistill job for the SAME user is still independently exclusive (its own slot,
    # unaffected by the two add_memory jobs above being active).
    b3, s3 = _import(uid, _sealed_body(
        uid, b"redistill-a", client_job_id="cj-redistill-1", job_kind="resident_redistill"))
    assert s3 == 200


def test_redistill_submission_omitting_job_kind_still_lands_as_resident_redistill():
    """I1b: the real T11 consumer always posts mode="update_identity" for its
    redistill IPC relay (see chat_resident_consumer._handle_redistill_ipc) —
    job_kind is a SEPARATE, independently-omittable field. Before the fix, a
    submission that forgot (or a caller that never knew to send) job_kind fell
    back to source_kind="update_identity" and silently dodged the 0023
    exclusivity index. Now mode="update_identity" alone is enough."""
    uid = "usr_redistill_excl_5"
    seed_user(uid)
    b1, s1 = _import(uid, _sealed_body(
        uid, b"material-one", client_job_id="cj-noflag-1", mode="update_identity"))
    assert s1 == 200
    job1_id = b1["job"]["job_id"]
    assert db.genesis_get_job(uid, job1_id)["source_kind"] == "resident_redistill"

    # A second concurrent redistill (also omitting job_kind) for the same user must
    # still hit the exclusivity index, not silently race the first.
    b2, s2 = _import(uid, _sealed_body(
        uid, b"material-two", client_job_id="cj-noflag-2", mode="update_identity"))
    assert s2 == 409
    assert b2["error"] == "redistill_job_active"
    assert b2["active_job_id"] == job1_id


def test_redistill_submission_with_forged_job_kind_still_lands_as_resident_redistill():
    """Same gap, but the client actively sets job_kind to something else (e.g. a
    naive/forged re-implementation of the consumer's payload) instead of merely
    omitting it — mode="update_identity" must still win server-side."""
    uid = "usr_redistill_excl_6"
    seed_user(uid)
    b1, s1 = _import(uid, _sealed_body(
        uid, b"material-one", client_job_id="cj-forged-1",
        mode="update_identity", job_kind="add_memory"))
    assert s1 == 200
    job1_id = b1["job"]["job_id"]
    assert db.genesis_get_job(uid, job1_id)["source_kind"] == "resident_redistill"

    b2, s2 = _import(uid, _sealed_body(
        uid, b"material-two", client_job_id="cj-forged-2",
        mode="update_identity", job_kind="add_memory"))
    assert s2 == 409
    assert b2["error"] == "redistill_job_active"
    assert b2["active_job_id"] == job1_id


def test_job_insert_without_chunk_write_is_backfilled_on_retry():
    """I5: genesis_create_job (the job row) and genesis_put_chunk (chunk 0) are two
    SEPARATE transactions. Simulate a crash that lands between them — the job commits,
    the chunk write never runs — by calling genesis_create_job directly (bypassing the
    chunk write _resident_sealed_import normally does right after it). Before the fix,
    retrying the SAME upload treated the pre-existing job row as "steady-state
    idempotent re-upload, chunk already stored" and silently skipped the chunk write
    forever, leaving a zero-chunk "awaiting_resident" job the resident consumer's claim
    path can never distill (chat_resident_consumer.py's claim path treats a chunkless
    job as malformed and just waits for the reaper — no self-heal)."""
    uid = "usr_redistill_crashgap_1"
    seed_user(uid)
    client_job_id = "cj-crash-1"
    payload = _sealed_body(uid, b"material-crash-gap", client_job_id=client_job_id,
                            job_kind="resident_redistill")
    env = payload["envelope"]
    encrypted_body = base64.b64decode(env["body_ct"])
    job_id = "genesis_" + hashlib.sha256(
        f"{uid}:{client_job_id}:{env['id']}".encode("utf-8")).hexdigest()[:16]

    # Simulate the crash: job row committed, chunk 0 never written.
    created = db.genesis_create_job(uid, {
        "job_id": job_id,
        "status": "awaiting_resident",
        "source_kind": "resident_redistill",
        "total_chunks": 1,
        "total_bytes": len(encrypted_body),
        "privacy_mode": "resident_sealed",
        "metadata": {"mode": "add_memory", "material_kind": "",
                     "client_job_id": client_job_id, "ingest": "resident_sealed",
                     "base_identity_replaced_at": ""},
    })
    assert created is not None
    assert db.genesis_missing_chunk_seqs(uid, job_id, total_chunks=1) == [0]  # malformed

    # Retry the SAME request (same request_id / envelope) through the real entry point.
    body, status = _import(uid, payload)
    assert status == 200
    assert body["job"]["job_id"] == job_id

    # Self-healed: chunk backfilled...
    assert db.genesis_missing_chunk_seqs(uid, job_id, total_chunks=1) == []
    chunks = db.genesis_list_chunks(uid, job_id)
    assert len(chunks) == 1 and chunks[0]["seq"] == 0

    # ...and the job is now genuinely claimable end-to-end (not just "has a chunk row"):
    # resident_pending returns real sealed material instead of sealed=None.
    claimed, claim_status = genesis_core.resident_pending(
        types.SimpleNamespace(user_id=uid), consumer_id="consumer-1")
    assert claim_status == 200
    claimed_job = next(j for j in claimed["jobs"] if j["job_id"] == job_id)
    assert claimed_job["sealed"] is not None


def test_retrying_a_fully_done_job_does_not_resurrect_deleted_chunks():
    """The backfill-on-retry fix must not undo a completed job's chunk cleanup: once
    the job is done and its ciphertext deleted, a stray retry of the same request
    should not write the chunk back."""
    uid = "usr_redistill_crashgap_2"
    seed_user(uid)
    client_job_id = "cj-done-crashgap"
    payload = _sealed_body(uid, b"material-done", client_job_id=client_job_id,
                            job_kind="resident_redistill")
    b1, s1 = _import(uid, payload)
    assert s1 == 200
    job_id = b1["job"]["job_id"]
    assert len(db.genesis_list_chunks(uid, job_id)) == 1

    db.genesis_complete_job(
        uid, job_id, output={}, memory_action_count=0,
        identity_status="", persona_ref="", persona_sha256="")
    db.genesis_delete_chunks(uid, job_id)
    assert db.genesis_list_chunks(uid, job_id) == []

    b2, s2 = _import(uid, payload)
    assert s2 == 200
    assert b2["job"]["job_id"] == job_id
    # Still no chunks — cleanup after completion is not undone by a stray retry.
    assert db.genesis_list_chunks(uid, job_id) == []
