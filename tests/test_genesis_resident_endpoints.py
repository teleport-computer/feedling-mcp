"""P2 request-layer: resident consumer endpoints core (pending/complete/heartbeat).

Integration against real Postgres: upload -> pending (claim + return sealed) -> complete
(done + delete material) / heartbeat (owner-only). Routes are thin wrappers over these.
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


def _sealed_body(uid: str, ct: bytes, *, client_job_id="cj", mode="add_memory"):
    # Full v1 content-envelope wire shape (ContentEncryption.Envelope.jsonBody).
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


def _upload(uid, ct=b"material", **kw):
    seed_user(uid)
    body, st = genesis_core._resident_sealed_import(_ns(uid), _sealed_body(uid, ct, **kw))
    assert st == 200
    return body["job"]["job_id"]


def test_pending_claims_and_returns_sealed(monkeypatch):
    monkeypatch.delenv("FEEDLING_RESIDENT_DISTILL_MAX_BYTES", raising=False)
    uid = "usr_ep_pending"
    jid = _upload(uid, b"hello-sealed-material")
    body, st = genesis_core.resident_pending(_ns(uid), consumer_id="cons-A")
    assert st == 200
    mine = [j for j in body["jobs"] if j["job_id"] == jid]
    assert len(mine) == 1
    assert mine[0]["mode"] == "add_memory"
    env = mine[0]["sealed"]["envelope"]
    assert env["body_ct"] == base64.b64encode(b"hello-sealed-material").decode()
    assert env["K_user"] and env["nonce"] and env["owner_user_id"] == uid   # keys rebuilt for decrypt
    assert db.genesis_get_job(uid, jid)["status"] == "processing"     # claimed


def test_pending_returns_identity_baseline_from_job_metadata(monkeypatch):
    # P5 (Task 4): pending surfaces the base_identity_replaced_at snapshotted at
    # job-creation time (Task 3's outer identity ``replaced_at`` field).
    monkeypatch.delenv("FEEDLING_RESIDENT_DISTILL_MAX_BYTES", raising=False)
    uid = "usr_ep_baseline"
    seed_user(uid)
    db.set_blob(uid, "identity", {"v": 1, "id": "card1", "replaced_at": "2026-07-01T00:00:00"})
    jid = _upload(uid, b"m", client_job_id="cj-baseline")
    body, st = genesis_core.resident_pending(_ns(uid), consumer_id="cons-A")
    assert st == 200
    mine = [j for j in body["jobs"] if j["job_id"] == jid]
    assert len(mine) == 1
    assert mine[0]["base_identity_replaced_at"] == "2026-07-01T00:00:00"


def test_pending_legacy_job_without_baseline_key_returns_empty(monkeypatch):
    # Back-compat: a job created BEFORE this field existed has no metadata key at all —
    # pending must return "" (Task 5's consumer treats "" as "no baseline, skip check"),
    # never KeyError/None.
    monkeypatch.delenv("FEEDLING_RESIDENT_DISTILL_MAX_BYTES", raising=False)
    uid = "usr_ep_legacy"
    seed_user(uid)
    ct = b"legacy-material"
    created = db.genesis_create_job(uid, {
        "job_id": "genesis_legacy_no_baseline",
        "status": "awaiting_resident",
        "source_kind": "resident",
        "total_chunks": 1,
        "total_bytes": len(ct),
        "privacy_mode": "resident_sealed",
        "metadata": {"mode": "add_memory", "material_kind": "", "client_job_id": "cj-legacy",
                     "ingest": "resident_sealed"},   # no base_identity_replaced_at key
    })
    assert created is not None
    db.genesis_put_chunk(
        uid, "genesis_legacy_no_baseline",
        seq=0, byte_start=0, byte_end=len(ct),
        ciphertext_sha256=hashlib.sha256(ct).hexdigest(),
        content_sha256="",
        aad={"v": 1, "nonce": base64.b64encode(b"nonce").decode(), "K_user": base64.b64encode(b"ku").decode(),
             "owner_user_id": uid, "visibility": "shared"},
        encrypted_body=ct,
    )
    body, st = genesis_core.resident_pending(_ns(uid), consumer_id="cons-A")
    assert st == 200
    mine = [j for j in body["jobs"] if j["job_id"] == "genesis_legacy_no_baseline"]
    assert len(mine) == 1
    assert mine[0]["base_identity_replaced_at"] == ""


def test_pending_requires_consumer_id():
    uid = "usr_ep_noc"
    seed_user(uid)
    body, st = genesis_core.resident_pending(_ns(uid), consumer_id="")
    assert st == 400 and body["error"] == "consumer_id_required"


def test_complete_marks_done_and_deletes_material(monkeypatch):
    monkeypatch.delenv("FEEDLING_RESIDENT_DISTILL_MAX_BYTES", raising=False)
    uid = "usr_ep_complete"
    jid = _upload(uid)
    genesis_core.resident_pending(_ns(uid), consumer_id="cons-A")     # claim first
    body, st = genesis_core.resident_complete(_ns(uid), jid, {"memory_action_count": 5})
    assert st == 200 and body["job"]["status"] == "done" and body["job"]["memory_action_count"] == 5
    assert db.genesis_get_job(uid, jid)["status"] == "done"
    assert db.genesis_list_chunks(uid, jid) == []                     # sealed material deleted


def test_complete_job_not_found():
    uid = "usr_ep_nf"
    seed_user(uid)
    body, st = genesis_core.resident_complete(_ns(uid), "genesis_nope", {})
    assert st == 404 and body["error"] == "job_not_found"


def test_heartbeat_owner_only(monkeypatch):
    monkeypatch.delenv("FEEDLING_RESIDENT_DISTILL_MAX_BYTES", raising=False)
    uid = "usr_ep_hb"
    jid = _upload(uid)
    genesis_core.resident_pending(_ns(uid), consumer_id="cons-A")     # claim as cons-A
    b1, s1 = genesis_core.resident_heartbeat(_ns(uid), jid, consumer_id="cons-A")
    assert s1 == 200 and b1["ok"] is True
    b2, s2 = genesis_core.resident_heartbeat(_ns(uid), jid, consumer_id="cons-B")
    assert s2 == 409 and b2["error"] == "heartbeat_rejected"
