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
