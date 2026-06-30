"""Unit tests for the PostgreSQL persistence layer (backend/db.py).

Requires a real PostgreSQL (JSONB, `||` merge, GENERATED IDENTITY, ON CONFLICT
have no SQLite equivalent). Point DATABASE_URL at a throwaway database:

    DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/feedling_test \
        pytest tests/test_db.py -v

Each test uses a unique user_id so they don't collide and the suite is
re-runnable without a fresh DB.
"""

import base64
import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

if not os.environ.get("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set — needs a real Postgres", allow_module_level=True)

import db  # noqa: E402

db.init_schema()


def _uid() -> str:
    return f"usr_{uuid.uuid4().hex[:16]}"


def test_healthcheck():
    assert db.healthcheck() is True


def test_pepper_insert_once():
    key = f"pepper_test_{uuid.uuid4().hex[:8]}"
    first = b"\x01" * 32
    second = b"\x02" * 32
    got1 = db.set_config_if_absent(key, first)
    got2 = db.set_config_if_absent(key, second)
    assert got1 == first
    assert got2 == first  # second writer does not overwrite
    assert db.get_config(key) == first


def test_users_roundtrip_and_archive_language_omitted_when_null():
    uid = _uid()
    entry = {
        "user_id": uid,
        "api_key_hash": "hash_" + uid,
        "public_key": "pubkey123",
        "created_at": "2026-05-31T00:00:00",
    }
    db.insert_user(entry)
    users = {u["user_id"]: u for u in db.load_all_users()}
    assert uid in users
    assert "archive_language" not in users[uid]  # NULL → omitted, matches file era
    assert users[uid]["public_key"] == "pubkey123"

    # upsert sets archive_language
    entry["archive_language"] = "zh-Hans"
    db.upsert_user(entry)
    users = {u["user_id"]: u for u in db.load_all_users()}
    assert users[uid]["archive_language"] == "zh-Hans"

    db.delete_user(uid)
    assert uid not in {u["user_id"] for u in db.load_all_users()}


def test_users_full_doc_and_save_all():
    # The users table stores the full doc, including a rich api_keys[] shape.
    u1 = {"user_id": _uid(), "principal_id": "prn_1", "created_at": "2026-01-01",
          "api_keys": [{"key_id": "k1", "api_key_hash": "h1", "revoked_at": ""}]}
    u2 = {"user_id": _uid(), "principal_id": "prn_2", "created_at": "2026-01-02",
          "public_key": "pk2"}
    db.save_all_users([u1, u2])
    loaded = {u["user_id"]: u for u in db.load_all_users()}
    assert loaded[u1["user_id"]]["api_keys"][0]["key_id"] == "k1"
    assert loaded[u2["user_id"]]["public_key"] == "pk2"
    # save_all_users replaces the whole table (removed users disappear).
    db.save_all_users([u2])
    ids = {u["user_id"] for u in db.load_all_users()}
    assert u1["user_id"] not in ids and u2["user_id"] in ids
    # upsert one user's doc in place
    u2["public_key"] = "pk2-rotated"
    db.upsert_user(u2)
    assert {u["user_id"]: u for u in db.load_all_users()}[u2["user_id"]]["public_key"] == "pk2-rotated"
    db.delete_user(u2["user_id"])
    assert u2["user_id"] not in {u["user_id"] for u in db.load_all_users()}


def test_global_blob():
    key = f"glob_{uuid.uuid4().hex[:8]}"
    assert db.get_global_blob(key) is None
    db.set_global_blob(key, [{"token": "t1"}, {"token": "t2"}])
    assert [r["token"] for r in db.get_global_blob(key)] == ["t1", "t2"]
    db.set_global_blob(key, [])  # overwrite
    assert db.get_global_blob(key) == []


def test_blob_get_set():
    uid = _uid()
    assert db.get_blob(uid, "identity") is None
    db.set_blob(uid, "identity", {"agent_name": "Iris", "v": 1})
    assert db.get_blob(uid, "identity") == {"agent_name": "Iris", "v": 1}
    # list blob (tokens) round-trips too
    db.set_blob(uid, "tokens", [{"token": "abc", "status": "active"}])
    assert db.get_blob(uid, "tokens") == [{"token": "abc", "status": "active"}]


def test_blob_delete_and_list_by_prefix():
    uid = _uid()
    db.set_blob(uid, "model_api", {"provider": "openrouter"})
    assert db.delete_blob(uid, "model_api") is True
    assert db.delete_blob(uid, "model_api") is False
    assert db.get_blob(uid, "model_api") is None
    # collection-style blobs keyed by prefix (history-import jobs)
    db.set_blob(uid, "history_import_job:a", {"job_id": "a", "updated_at": "2026-01-01"})
    db.set_blob(uid, "history_import_job:b", {"job_id": "b", "updated_at": "2026-02-01"})
    db.set_blob(uid, "identity", {"unrelated": True})
    jobs = db.list_blobs(uid, "history_import_job:")
    assert {j["job_id"] for j in jobs} == {"a", "b"}  # prefix isolates the collection


def test_chat_append_order_and_ring_buffer():
    uid = _uid()
    for i in range(5):
        db.chat_append(uid, f"m{i}", float(i), {"id": f"m{i}", "body_ct": f"ct{i}"}, max_messages=3)
    loaded = db.chat_load(uid)
    # ring buffer keeps the newest 3, in insertion order
    assert [m["id"] for m in loaded] == ["m2", "m3", "m4"]
    assert loaded[0]["body_ct"] == "ct2"


def test_chat_update_metadata_merges():
    uid = _uid()
    db.chat_append(uid, "x1", 1.0, {"id": "x1", "body_ct": "ct", "visibility": "shared"}, max_messages=0)
    merged = db.chat_update_metadata(uid, "x1", {"alert_status": "sent"})
    assert merged["alert_status"] == "sent"
    assert merged["body_ct"] == "ct"  # original field preserved
    assert db.chat_update_metadata(uid, "missing", {"a": "b"}) is None


def test_chat_delete():
    uid = _uid()
    db.chat_append(uid, "d1", 1.0, {"id": "d1"}, max_messages=0)
    assert db.chat_delete(uid, "d1") is True
    assert db.chat_delete(uid, "d1") is False
    assert db.chat_load(uid) == []


def test_memory_upsert_replace_delete():
    uid = _uid()
    db.memory_upsert(uid, "a", "2026-01-02", {"id": "a", "content": "one"})
    db.memory_upsert(uid, "b", "2026-01-01", {"id": "b", "content": "two"})
    loaded = db.memory_load(uid)
    assert [m["id"] for m in loaded] == ["b", "a"]  # ordered by occurred_at
    db.memory_upsert(uid, "a", "2026-01-02", {"id": "a", "content": "one-edited"})
    assert {m["id"]: m["content"] for m in db.memory_load(uid)}["a"] == "one-edited"
    db.memory_replace_all(uid, [{"id": "c", "occurred_at": "2026-03-03", "content": "c"}])
    assert [m["id"] for m in db.memory_load(uid)] == ["c"]
    assert db.memory_delete(uid, "c") is True
    assert db.memory_load(uid) == []


def test_memory_replace_all_diff_semantics():
    """memory_replace_all reconciles to the input set (full-replace semantics)
    while only touching changed rows. We assert the final state is exactly the
    input; the diff optimization is internal but must not change observable
    behavior."""
    uid = _uid()
    base = [
        {"id": "a", "occurred_at": "2026-01-01", "content": "a"},
        {"id": "b", "occurred_at": "2026-01-02", "content": "b"},
        {"id": "c", "occurred_at": "2026-01-03", "content": "c"},
    ]
    db.memory_replace_all(uid, base)
    assert {m["id"]: m["content"] for m in db.memory_load(uid)} == {
        "a": "a", "b": "b", "c": "c"
    }

    # Edit only b; a and c are byte-identical and should survive untouched.
    edited = [
        {"id": "a", "occurred_at": "2026-01-01", "content": "a"},
        {"id": "b", "occurred_at": "2026-01-02", "content": "b-edited"},
        {"id": "c", "occurred_at": "2026-01-03", "content": "c"},
    ]
    db.memory_replace_all(uid, edited)
    assert {m["id"]: m["content"] for m in db.memory_load(uid)} == {
        "a": "a", "b": "b-edited", "c": "c"
    }

    # Drop c, add d; a and b unchanged. Final set must be exactly {a, b, d}.
    reshaped = [
        {"id": "a", "occurred_at": "2026-01-01", "content": "a"},
        {"id": "b", "occurred_at": "2026-01-02", "content": "b-edited"},
        {"id": "d", "occurred_at": "2026-01-04", "content": "d"},
    ]
    db.memory_replace_all(uid, reshaped)
    assert {m["id"] for m in db.memory_load(uid)} == {"a", "b", "d"}

    # id-less dicts are skipped; empty list clears the set.
    db.memory_replace_all(uid, [{"content": "no-id"}])
    assert db.memory_load(uid) == []


def test_memory_replace_all_rewrites_stale_occurred_at_column():
    """If the occurred_at column drifts out of sync with the doc (e.g. a row
    written separately via memory_upsert), an otherwise-unchanged doc must
    still rewrite the column — otherwise memory_load (ORDER BY occurred_at)
    returns the wrong order. Mirrors the old full-replace semantics."""
    uid = _uid()
    # Seed two rows whose ordering column disagrees with the doc's own field:
    # x sorts first by column ("1"), y second ("2"), but the docs' occurred_at
    # fields are the reverse.
    db.memory_upsert(uid, "x", "1", {"id": "x", "occurred_at": "2026-12-31"})
    db.memory_upsert(uid, "y", "2", {"id": "y", "occurred_at": "2026-01-01"})
    assert [m["id"] for m in db.memory_load(uid)] == ["x", "y"]

    # replace_all with the same docs must re-derive the column from each doc,
    # flipping the order to match occurred_at fields.
    db.memory_replace_all(uid, [
        {"id": "x", "occurred_at": "2026-12-31"},
        {"id": "y", "occurred_at": "2026-01-01"},
    ])
    assert [m["id"] for m in db.memory_load(uid)] == ["y", "x"]


def test_frame_upsert_get_exists_prune():
    uid = _uid()
    for i in range(5):
        db.frame_upsert(uid, f"f{i}", float(i), {"id": f"f{i}", "body_ct": f"big{i}"})
    assert db.frame_exists(uid, "f3") is True
    assert db.frame_exists(uid, "nope") is False
    assert db.frame_get(uid, "f2")["body_ct"] == "big2"
    evicted = db.frame_prune_to(uid, 2)  # keep newest 2 by ts (f3, f4)
    assert set(evicted) == {"f0", "f1", "f2"}
    remaining = {m["id"] for m in db.frame_list_meta(uid)}
    assert remaining == {"f3", "f4"}
    db.frame_delete(uid, "f3")
    assert db.frame_exists(uid, "f3") is False


def test_log_append_read_trim_prune():
    uid = _uid()
    for i in range(10):
        db.log_append(uid, "device_events", {"event": i, "ts": float(i)}, ts=float(i))
    # newest 3, chronological
    recent = db.log_read(uid, "device_events", limit=3)
    assert [r["event"] for r in recent] == [7, 8, 9]
    # since_epoch filter
    after = db.log_read(uid, "device_events", limit=100, since_epoch=7.0)
    assert [r["event"] for r in after] == [8, 9]
    # prune older than cutoff
    db.log_prune_older_than(uid, "device_events", 5.0)
    kept = [r["event"] for r in db.log_read_all(uid, "device_events")]
    assert kept == [5, 6, 7, 8, 9]
    # trim to newest N
    db.log_trim(uid, "device_events", 2)
    assert [r["event"] for r in db.log_read_all(uid, "device_events")] == [8, 9]


def test_log_patch_item_only_if_status():
    uid = _uid()
    db.log_append(uid, "proactive_jobs", {"job_id": "j1", "status": "pending"},
                  ts=1.0, item_key="j1")
    # guard mismatch → no change
    assert db.log_patch_item(uid, "proactive_jobs", "j1", {"status": "done"},
                             only_if_status="claimed") is None
    # guard match → patched
    patched = db.log_patch_item(uid, "proactive_jobs", "j1", {"status": "claimed"},
                                only_if_status="pending")
    assert patched["status"] == "claimed"
    # unknown item_key → None
    assert db.log_patch_item(uid, "proactive_jobs", "nope", {"status": "x"}) is None


def test_delete_user_data_wipes_everything():
    uid = _uid()
    db.set_blob(uid, "identity", {"a": 1})
    db.chat_append(uid, "c1", 1.0, {"id": "c1"}, max_messages=0)
    db.memory_upsert(uid, "m1", "2026-01-01", {"id": "m1"})
    db.frame_upsert(uid, "f1", 1.0, {"id": "f1", "body_ct": "x"})
    db.log_append(uid, "gate_decisions", {"x": 1}, ts=1.0)
    db.delete_user_data(uid)
    assert db.get_blob(uid, "identity") is None
    assert db.chat_load(uid) == []
    assert db.memory_load(uid) == []
    assert db.frame_list_meta(uid) == []
    assert db.log_read_all(uid, "gate_decisions") == []


# ---- multi-instance supervisor heartbeats (agent_runtime_supervisor_heartbeats) ----
# Each runner writes its OWN per-owner row, so multiple runners don't clobber a
# single global key (the legacy server_config heartbeat's flaw). The backend's
# wedge guard aggregates these rows to decide whether any runner is hosting.


def _owner() -> str:
    return f"sup_{uuid.uuid4().hex[:12]}"


def _hb_payload(owner, **over):
    base = {
        "ts": 1_000_000.0, "owner": owner, "host": "runner-A",
        "host_all": True, "gateway": True,
        "active_children": 3, "max_children": 4,
        "shard_index": 0, "shard_count": 1, "version": "abc123",
    }
    base.update(over)
    return base


def test_supervisor_instance_heartbeat_roundtrip():
    owner = _owner()
    db.set_supervisor_instance_heartbeat(owner, _hb_payload(owner))
    rows = [r for r in db.list_supervisor_instance_heartbeats() if r["owner"] == owner]
    assert len(rows) == 1
    r = rows[0]
    assert r["host_all"] is True and r["gateway"] is True
    assert r["active_children"] == 3 and r["max_children"] == 4
    assert r["shard_index"] == 0 and r["shard_count"] == 1
    # ``ts`` is the row's updated_at as an epoch float so the guard can age-check it.
    assert isinstance(r["ts"], float) and r["ts"] > 0


def test_supervisor_instance_heartbeats_do_not_clobber_across_owners():
    a, b = _owner(), _owner()
    db.set_supervisor_instance_heartbeat(a, _hb_payload(a, host="A", active_children=1))
    db.set_supervisor_instance_heartbeat(b, _hb_payload(b, host="B", active_children=2))
    owners = {r["owner"]: r for r in db.list_supervisor_instance_heartbeats()}
    assert a in owners and b in owners
    assert owners[a]["active_children"] == 1
    assert owners[b]["active_children"] == 2


def test_supervisor_instance_heartbeat_upsert_updates_same_owner():
    owner = _owner()
    db.set_supervisor_instance_heartbeat(owner, _hb_payload(owner, active_children=1))
    db.set_supervisor_instance_heartbeat(owner, _hb_payload(owner, active_children=5))
    rows = [r for r in db.list_supervisor_instance_heartbeats() if r["owner"] == owner]
    assert len(rows) == 1 and rows[0]["active_children"] == 5


def test_prune_supervisor_instance_heartbeats_removes_old_rows():
    owner = _owner()
    db.set_supervisor_instance_heartbeat(owner, _hb_payload(owner))
    # Age the row well past the prune window via raw SQL (set() always stamps now()).
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_runtime_supervisor_heartbeats "
            "SET updated_at = now() - interval '1 hour' WHERE owner = %s",
            (owner,),
        )
    db.prune_supervisor_instance_heartbeats(60.0)  # older than 60s → gone
    rows = [r for r in db.list_supervisor_instance_heartbeats() if r["owner"] == owner]
    assert rows == []


def test_envelope_fields_stored_byte_for_byte():
    """Crypto-fidelity guard: the opaque base64 envelope fields the enclave
    needs to decrypt must survive a store→load round-trip unchanged."""
    uid = _uid()
    env = {
        "id": "abc123",
        "v": 1,
        "body_ct": base64.b64encode(b"\x00\xff\x10ciphertext\x80").decode(),
        "nonce": base64.b64encode(b"123456789012").decode(),
        "K_user": base64.b64encode(b"user-sealed-key-bytes").decode(),
        "K_enclave": base64.b64encode(b"enclave-sealed-key-bytes").decode(),
        "visibility": "shared",
        "owner_user_id": uid,
    }
    db.chat_append(uid, "abc123", 1.0, dict(env, role="user"), max_messages=0)
    got = db.chat_load(uid)[0]
    for k in ("body_ct", "nonce", "K_user", "K_enclave", "visibility", "owner_user_id", "v"):
        assert got[k] == env[k], f"field {k} drifted in storage"

    # frame envelope path too
    db.frame_upsert(uid, "abc123", 1.0, env)
    assert db.frame_get(uid, "abc123") == env
