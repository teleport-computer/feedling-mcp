"""Real PostgreSQL checks for managed admission, before enabling the worker lane."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
import uuid

import psycopg
from psycopg.types.json import Jsonb
import pytest

from conftest import configure_model_api_route, seed_user
import db
import genesis_managed_store as store
from accounts import registry
from core import envelope
from genesis import managed_eligibility as eligibility


@pytest.fixture
def account(monkeypatch):
    uid = "u_managed_" + uuid.uuid4().hex
    seed_user(uid, archive_language="zh")
    monkeypatch.setattr(envelope, "PLAINTEXT_WRITES_ACCEPTED", True)
    monkeypatch.setattr(envelope, "resolve_content_encryption", registry.effective_content_encryption)
    key = {"body": "test-managed-key"}
    configure_model_api_route(uid, provider="openai", model="gpt-test", envelope=key)
    return uid, key


def upload(uid, jid="j", *, created=None):
    db.genesis_create_job(uid, {"job_id": jid, "total_chunks": 1,
                               "source_kind": "history_import", "metadata": {"keep": 1}})
    db.genesis_put_chunk(
        uid, jid, seq=0, byte_start=0, byte_end=5,
        ciphertext_sha256=hashlib.sha256(b"hello").hexdigest(), content_sha256="",
        aad={"envelope_meta": {"owner_user_id": uid, "body_object_format": "plaintext_v1"}},
        encrypted_body=b"hello",
    )
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE genesis_import_jobs SET status='uploaded',finalized_at=now(),"
                     "output=%s,created_at=COALESCE(%s,created_at) WHERE user_id=%s AND job_id=%s",
                     (Jsonb({"keep_output": 2}), created, uid, jid))
    return db.genesis_get_job(uid, jid)


def snapshot(uid, jid="j"):
    with db.get_pool().connection() as conn:
        return store.input_snapshot(conn, store.read_job(conn, uid, jid))


def claim(account, jid="j", prepare=eligibility.prepare):
    uid, key = account
    return store.prepare_claim(uid, jid, "worker-1", key, prepare)


def test_real_snapshot_claim_retains_only_private_hash(account):
    uid, key = account
    before = upload(uid)
    job, prepared, witness = claim(account)
    assert prepared.texts == ["hello"] and prepared.runtime.api_key == key["body"]
    assert prepared.cards == [] and prepared.locale == "zh"
    assert job["status"] == "processing" and job["worker_claimed_by"] == "worker-1"
    assert job["finalized_at"] == before["finalized_at"]
    assert job["metadata"]["keep"] == 1 and job["output"]["keep_output"] == 2
    assert store.verify(job, witness, phases=("prepared",))["mode"] == "local_plaintext"
    persisted = json.dumps(job, default=str)
    assert witness["capability"] not in persisted and key["body"] not in persisted
    assert "hello" not in repr(prepared)
    assert claim(account) is None


@pytest.mark.parametrize("dependency", ["chunk", "persona", "voice", "identity", "provider_credential", "garden"])
@pytest.mark.parametrize("invalid", ["sealed", "foreign", "malformed"])
def test_dependency_validation_cannot_certify_unreadable_inputs(account, dependency, invalid):
    uid, key = account
    upload(uid)
    raw = snapshot(uid)
    item = {"owner_user_id": uid, "body": "{}"}
    if invalid == "sealed":
        item["body_ct"] = "ciphertext-wins-over-body"
    elif invalid == "foreign":
        item["owner_user_id"] = "another-user"
    else:
        item["body"] = "not-json"
    auth = key
    if dependency == "provider_credential":
        item = {"body": "different-key"} if invalid == "foreign" else item
        if invalid == "malformed":
            item = {"body": " "}
        raw["route"]["api_key_envelope"] = item
        auth = key if invalid == "foreign" else item
    elif dependency == "chunk":
        raw["chunks"][0]["aad"]["envelope_meta"].update(item)
        if invalid == "malformed":
            raw["chunks"][0]["encrypted_body"] = b"\xff"
    elif dependency == "garden":
        raw["moments"] = [{**item, "id": "card", "visibility": "shared", "K_enclave": "k"}]
    elif dependency == "identity":
        raw["blobs"]["identity"] = item
    else:
        if dependency == "persona" and invalid == "malformed":
            item["body_b64"] = "invalid-base64"
        raw["blobs"]["genesis_" + dependency] = {"content_envelope": item}
    with pytest.raises(store.DependencyUnavailable) as caught:
        eligibility.prepare(raw, auth)
    assert caught.value.dependency == dependency


def test_deferral_coalesces_without_attempt_or_business_timestamp_changes(account):
    uid, _ = account
    before = upload(uid)
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE genesis_import_chunks SET aad='{}' WHERE user_id=%s", (uid,))
    assert claim(account) is None
    first = db.genesis_get_job(uid, "j")
    assert claim(account) is None
    second = db.genesis_get_job(uid, "j")
    assert first == second
    for field in ("status", "updated_at", "finalized_at", "worker_claimed_at",
                  "worker_claimed_by", "resident_attempts", "processed_chunks", "metadata"):
        assert second[field] == before[field]
    assert second["output"]["keep_output"] == 2
    assert second["output"]["managed_unavailable"]["dependency"] == "chunk"
    with db.get_pool().connection() as conn:
        assert conn.execute("SELECT attempts FROM genesis_import_chunks WHERE user_id=%s", (uid,)).fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM genesis_import_outputs WHERE user_id=%s", (uid,)).fetchone() == (0,)


def test_two_claimers_skip_locked_without_waiting(account):
    uid, _ = account
    upload(uid)
    entered, release = threading.Event(), threading.Event()

    def slow_prepare(raw, key):
        result = eligibility.prepare(raw, key)
        entered.set()
        assert release.wait(5)
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(claim, account, prepare=slow_prepare)
        try:
            assert entered.wait(5)
            assert pool.submit(claim, account).result(timeout=3) is None
        finally:
            release.set()
        assert first.result(timeout=5)[0]["status"] == "processing"


def test_same_body_aad_writer_does_not_deadlock_snapshot(account):
    uid, _ = account
    upload(uid)
    pool = db.get_pool()
    with pool.connection() as writer:
        with writer.transaction():
            writer.execute("SET LOCAL lock_timeout='3s'")
            writer.execute("UPDATE genesis_import_chunks SET aad=%s WHERE user_id=%s",
                           (Jsonb({"envelope_meta": {"owner_user_id": "foreign",
                                                    "body_object_format": "plaintext_v1"}}), uid))
            with ThreadPoolExecutor(max_workers=1) as threads:
                result = threads.submit(claim, account).result(timeout=4)
            assert result[1].texts == ["hello"]
            writer.execute("UPDATE genesis_import_jobs SET updated_at=now() WHERE user_id=%s", (uid,))
    assert snapshot(uid)["chunks"][0]["aad"]["envelope_meta"]["owner_user_id"] == "foreign"
    # Claim executes its captured old snapshot, never a tolerant later reread.
    assert result[1].texts == ["hello"]


def test_real_serialization_conflict_returns_no_executable_claim(account, monkeypatch):
    uid, _ = account
    upload(uid)
    original = store.read_job

    def race(conn, user_id, job_id, **kwargs):
        conn.execute("SELECT 1 FROM genesis_import_jobs WHERE user_id=%s", (uid,)).fetchone()
        with db.get_pool().connection() as writer:
            writer.execute("UPDATE genesis_import_jobs SET metadata=%s WHERE user_id=%s",
                           (Jsonb({"concurrent": True}), uid))
        return original(conn, user_id, job_id, **kwargs)

    monkeypatch.setattr(store, "read_job", race)
    with pytest.raises(psycopg.errors.SerializationFailure):
        claim(account)
    row = db.genesis_get_job(uid, "j")
    assert row["status"] == "uploaded" and row["metadata"] == {"concurrent": True}


@pytest.mark.parametrize("tamper", ["missing", "capability", "generation", "worker", "phase"])
def test_claim_authority_rejects_tampering_without_mutation(account, tamper):
    uid, _ = account
    upload(uid)
    job, _, witness = claim(account)
    forged = deepcopy(witness)
    if tamper == "missing":
        forged = None
    elif tamper in ("capability", "generation"):
        forged[tamper] = "forged"
    else:
        with db.get_pool().connection() as conn:
            if tamper == "worker":
                conn.execute("UPDATE genesis_import_jobs SET worker_claimed_by='replacement' WHERE user_id=%s", (uid,))
            else:
                conn.execute("UPDATE genesis_import_jobs SET status='failed' WHERE user_id=%s", (uid,))
    before = db.genesis_get_job(uid, "j")
    with pytest.raises(store.ClaimRejected):
        store.set_phase(uid, "j", forged, ("prepared",), "execution_started")
    assert db.genesis_get_job(uid, "j") == before


def test_finite_sweep_revisits_repaired_head_under_continuous_arrivals(account, monkeypatch):
    uid, key = account
    epoch = datetime(2000, 1, 1, tzinfo=timezone.utc)
    for i in range(3):
        upload(uid, str(i), created=epoch + timedelta(seconds=i))
    eligible = set()
    visited = []
    original_prepare = eligibility.prepare

    def prepare(raw, authenticated):
        visited.append(raw["job"]["job_id"])
        if raw["job"]["job_id"] not in eligible:
            raise store.DependencyUnavailable("chunk")
        return original_prepare(raw, authenticated)

    monkeypatch.setattr(eligibility, "prepare", prepare)
    monkeypatch.setattr(eligibility, "fetch_key_envelope", lambda *_: key)
    sweep = eligibility.Sweep()
    for i in range(3):
        assert sweep.claim(api_url="unused", worker_id="w", mint_token=lambda _: "t", page_size=1) is None
        upload(uid, str(3 + i), created=epoch + timedelta(seconds=3 + i))
    assert visited == ["0", "1", "2"]
    eligible.add("0")
    assert sweep.claim(api_url="unused", worker_id="w", mint_token=lambda _: "t", page_size=1) is None
    result = sweep.claim(api_url="unused", worker_id="w", mint_token=lambda _: "t", page_size=1)
    assert result[0]["job_id"] == "0"
    assert "managed_unavailable" not in result[0]["output"]


@pytest.mark.parametrize("value", [None, {"id": "missing-body"}, {"body_b64": "!!!", "body": "key"}])
def test_key_shape_precedence_rejects_invalid_plaintext(account, value):
    uid, _ = account
    upload(uid)
    raw = snapshot(uid)
    raw["route"]["api_key_envelope"] = value
    with pytest.raises(store.DependencyUnavailable):
        eligibility.prepare(raw, value)


def test_unreadable_active_garden_is_not_silently_an_empty_index(account):
    uid, key = account
    upload(uid)
    raw = snapshot(uid)
    raw["moments"] = [{"id": "bad", "owner_user_id": uid, "visibility": "shared"}]
    with pytest.raises(store.DependencyUnavailable, match="garden"):
        eligibility.prepare(raw, key)
    raw["moments"][0]["status"] = "archived"
    assert eligibility.prepare(raw, key).cards == []


@pytest.mark.parametrize("policy_change", ["gate", "assembly", "missing_user"])
def test_policy_change_before_admission_cannot_claim(account, monkeypatch, policy_change):
    uid, _ = account
    before = upload(uid)
    if policy_change == "gate":
        monkeypatch.setattr(envelope, "PLAINTEXT_WRITES_ACCEPTED", False)
    elif policy_change == "assembly":
        monkeypatch.setattr(envelope, "resolve_content_encryption", lambda _: "on")
    else:
        # Registry absence is sufficient to fail closed, even if its DB row remains.
        with registry._users_lock:
            monkeypatch.setattr(registry, "_users", [u for u in registry._users if u["user_id"] != uid])
    assert claim(account) is None
    after = db.genesis_get_job(uid, "j")
    assert after["status"] == "uploaded" and after["updated_at"] == before["updated_at"]
    assert after["output"]["managed_unavailable"]["dependency"] == "account_policy"


def test_snapshot_uses_one_primary_connection_and_rolls_back_on_failure(account, monkeypatch):
    uid, _ = account
    before = upload(uid)
    pool = db.get_pool()
    connections = []

    class ObservedPool:
        @contextmanager
        def connection(self):
            with pool.connection() as conn:
                connections.append(conn.info.backend_pid)
                yield conn

    monkeypatch.setattr(db, "get_pool", lambda: ObservedPool())

    def failure_after_snapshot(raw, key):
        assert eligibility.prepare(raw, key).texts == ["hello"]
        assert len(connections) == 1
        raise psycopg.OperationalError("injected unavailable database")

    with pytest.raises(psycopg.OperationalError):
        claim(account, prepare=failure_after_snapshot)
    assert len(connections) == 1
    assert db.genesis_get_job(uid, "j") == before


def test_phase_conflict_never_rewinds_existing_claim(account):
    uid, _ = account
    upload(uid)
    _, _, witness = claim(account)
    started = store.set_phase(uid, "j", witness, ("prepared",), "execution_started")
    assert store.marker(started)["phase"] == "execution_started"
    with pytest.raises(store.ClaimRejected):
        store.set_phase(uid, "j", witness, ("prepared",), "execution_started")
    assert db.genesis_get_job(uid, "j") == started
    failed = store.fail(uid, "j", witness, "test_failure_after_start")
    assert failed["status"] == "failed" and failed["failed_phase"] == "execution_started"
    with pytest.raises(store.ClaimRejected):
        store.set_phase(uid, "j", witness, ("execution_started",), "applying")
    assert db.genesis_get_job(uid, "j") == failed


def test_locked_head_does_not_block_next_candidate_and_restart_revisits(account, monkeypatch):
    uid, key = account
    epoch = datetime(2000, 1, 1, tzinfo=timezone.utc)
    upload(uid, "head", created=epoch)
    upload(uid, "tail", created=epoch + timedelta(seconds=1))
    monkeypatch.setattr(eligibility, "fetch_key_envelope", lambda *_: key)
    args = dict(api_url="unused", worker_id="w", mint_token=lambda _: "t", page_size=2)
    with db.get_pool().connection() as lock:
        with lock.transaction():
            lock.execute("SELECT 1 FROM genesis_import_jobs WHERE user_id=%s AND job_id='head' FOR UPDATE", (uid,))
            with ThreadPoolExecutor(max_workers=1) as threads:
                result = threads.submit(eligibility.Sweep().claim, **args).result(timeout=4)
            assert result[0]["job_id"] == "tail"
    assert eligibility.Sweep().claim(**args)[0]["job_id"] == "head"


def test_changed_credential_after_authenticated_read_defers(account):
    uid, key = account
    before = upload(uid)
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE model_api_credentials SET api_key_envelope=%s WHERE user_id=%s",
                     (Jsonb({"body": "new-key"}), uid))
    assert claim(account) is None
    after = db.genesis_get_job(uid, "j")
    assert after["updated_at"] == before["updated_at"]
    assert after["output"]["managed_unavailable"]["reason"] == "dependency_changed"
    assert key["body"] not in json.dumps(after, default=str)


def garden_rows(uid, count):
    return [{"id": f"card-{i}", "owner_user_id": uid, "visibility": "shared",
             "status": "active", "importance": 1 - i / (count + 1),
             "body": json.dumps({"summary": f"valid memory {i}", "content": f"known fact {i}"})}
            for i in range(count)]


def persist_garden(uid, docs):
    with db.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO memory_moments(user_id,moment_id,occurred_at,doc) VALUES(%s,%s,%s,%s)",
                [(uid, d["id"], "2026-10-01T00:00:00Z", Jsonb(d)) for d in docs],
            )


@pytest.mark.parametrize("count", [0, 1, 1001])
def test_complete_nonempty_plaintext_garden_claim(account, count):
    uid, _ = account
    upload(uid)
    docs = garden_rows(uid, count)
    persist_garden(uid, docs)
    result = claim(account)
    assert result is not None, db.genesis_get_job(uid, "j")["output"]
    cards = result[1].cards
    assert len(cards) == count
    assert {c["id"] for c in cards} == {d["id"] for d in docs}
    assert {c["summary"] for c in cards} == {f"valid memory {i}" for i in range(count)}


@pytest.mark.parametrize("bad_shape", ["sealed", "foreign", "missing", "bad_json"])
def test_garden_beyond_readside_cap_cannot_hide_unreadable_tail(account, bad_shape):
    uid, _ = account
    before = upload(uid)
    docs = garden_rows(uid, 1001)
    tail = docs[-1]
    if bad_shape == "sealed":
        tail["body_ct"] = "sealed-takes-precedence"
    elif bad_shape == "foreign":
        tail["owner_user_id"] = "foreign"
    elif bad_shape == "missing":
        del tail["body"]
    else:
        tail["body"] = "invalid-json"
    persist_garden(uid, docs)
    assert claim(account) is None
    after = db.genesis_get_job(uid, "j")
    assert after["status"] == "uploaded" and after["updated_at"] == before["updated_at"]
    assert after["output"]["managed_unavailable"]["dependency"] == "garden"


@pytest.mark.parametrize("excluded", ["archived", "local_only"])
def test_unreadable_excluded_card_does_not_block_real_nonempty_garden(account, excluded):
    uid, _ = account
    upload(uid)
    docs = garden_rows(uid, 2)
    del docs[-1]["body"]
    docs[-1]["body_ct"] = "not-read"
    docs[-1]["K_enclave"] = "not-used"
    if excluded == "archived":
        docs[-1]["status"] = "archived"
    else:
        docs[-1]["visibility"] = "local_only"
    persist_garden(uid, docs)
    result = claim(account)
    assert result is not None
    assert [(c["id"], c["summary"]) for c in result[1].cards] == [("card-0", "valid memory 0")]
