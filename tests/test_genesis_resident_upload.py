"""P2 request-layer: resident sealed upload branch (store the v1 envelope + awaiting_resident job).

DB-verifiable: size limit, job creation in awaiting_resident, the full envelope stored + roundtrips,
owner check, idempotent re-upload. (The enclave AEAD decrypt itself is verified by real VPS e2e.)
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


def _sealed_body(uid: str, ct: bytes, *, client_job_id="cj-1", mode="add_memory"):
    # Reuse the proven v1 content-envelope wire shape (ContentEncryption.Envelope.jsonBody).
    # nonce/K_user/K_enclave are placeholder base64 here — the DB roundtrip doesn't validate the
    # crypto; the real AEAD/enclave decrypt is a real-VPS e2e concern.
    return {
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


def _import(uid, payload):
    return genesis_core._resident_sealed_import(_ns(uid), payload)


def test_resident_upload_stores_envelope_and_creates_awaiting_job(monkeypatch):
    monkeypatch.delenv("FEEDLING_RESIDENT_DISTILL_MAX_BYTES", raising=False)
    uid = "usr_res_up_ok"
    seed_user(uid)
    ct = b"sealed-material-bytes-xyz"
    body, status = _import(uid, _sealed_body(uid, ct))
    assert status == 200
    jid = body["job"]["job_id"]
    assert body["job"]["status"] == "processing"       # app-facing status, not awaiting_resident

    assert db.genesis_get_job(uid, jid)["status"] == "awaiting_resident"   # internal → claimable
    chunks = db.genesis_list_chunks(uid, jid)
    assert len(chunks) == 1
    assert chunks[0]["encrypted_body"] == ct           # body_ct decoded + roundtrips
    assert chunks[0]["aad"]["K_user"]                  # the key fields are preserved for decrypt


def test_resident_upload_rejects_incomplete_envelope():
    uid = "usr_res_up_bad"
    seed_user(uid)
    body, status = _import(uid, {"format": "sealed_v1", "client_job_id": "x"})  # no envelope
    assert status == 400 and body["error"] == "sealed_envelope_incomplete"


def test_resident_upload_rejects_owner_mismatch():
    uid = "usr_res_up_owner"
    seed_user(uid)
    body = _sealed_body("someone_else", b"x")          # envelope owner != caller
    r, status = _import(uid, body)
    assert status == 403 and r["error"] == "envelope_owner_mismatch"


def test_resident_upload_enforces_size_limit(monkeypatch):
    monkeypatch.setenv("FEEDLING_RESIDENT_DISTILL_MAX_BYTES", "16")
    uid = "usr_res_up_big"
    seed_user(uid)
    body, status = _import(uid, _sealed_body(uid, b"x" * 64))   # 64 bytes > 16 limit
    assert status == 413 and body["error"] == "material_too_large" and body["max_bytes"] == 16


def test_status_hides_awaiting_resident_as_processing(monkeypatch):
    monkeypatch.delenv("FEEDLING_RESIDENT_DISTILL_MAX_BYTES", raising=False)
    uid = "usr_res_status"
    seed_user(uid)
    body, _ = _import(uid, _sealed_body(uid, b"m"))
    jid = body["job"]["job_id"]
    assert db.genesis_get_job(uid, jid)["status"] == "awaiting_resident"      # internal truth
    out, st = genesis_core.get_import_status(_ns(uid), jid, include_missing_raw=None)
    assert st == 200 and out["job"]["status"] == "processing"                 # app-facing arc


def test_resident_upload_is_idempotent(monkeypatch):
    monkeypatch.delenv("FEEDLING_RESIDENT_DISTILL_MAX_BYTES", raising=False)
    uid = "usr_res_up_idem"
    seed_user(uid)
    payload = _sealed_body(uid, b"same-material", client_job_id="cj-idem")
    b1, s1 = _import(uid, payload)
    b2, s2 = _import(uid, payload)
    assert s1 == 200 and s2 == 200
    assert b1["job"]["job_id"] == b2["job"]["job_id"]
    assert len(db.genesis_list_chunks(uid, b1["job"]["job_id"])) == 1


def test_resident_upload_snapshots_identity_baseline(monkeypatch):
    # P5 (Task 4): job creation snapshots the CURRENT identity's outer ``replaced_at``
    # (Task 3's concurrency baseline) into job metadata, so a later conflict check
    # (Task 5) compares against the identity that existed when this job was queued.
    monkeypatch.delenv("FEEDLING_RESIDENT_DISTILL_MAX_BYTES", raising=False)
    uid = "usr_res_up_baseline"
    seed_user(uid)
    db.set_blob(uid, "identity", {"v": 1, "id": "card1", "replaced_at": "2026-07-01T00:00:00"})
    body, status = _import(uid, _sealed_body(uid, b"m", client_job_id="cj-baseline"))
    assert status == 200
    job = db.genesis_get_job(uid, body["job"]["job_id"])
    assert job["metadata"]["base_identity_replaced_at"] == "2026-07-01T00:00:00"


def test_resident_upload_no_identity_baseline_is_empty(monkeypatch):
    # No identity on file at job-creation time → "" (back-compat: consumer skips check).
    monkeypatch.delenv("FEEDLING_RESIDENT_DISTILL_MAX_BYTES", raising=False)
    uid = "usr_res_up_no_identity"
    seed_user(uid)
    body, status = _import(uid, _sealed_body(uid, b"m", client_job_id="cj-no-identity"))
    assert status == 200
    job = db.genesis_get_job(uid, body["job"]["job_id"])
    assert job["metadata"]["base_identity_replaced_at"] == ""
