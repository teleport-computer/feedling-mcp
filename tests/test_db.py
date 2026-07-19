"""Unit tests for the PostgreSQL persistence layer (backend/db.py).

Requires a real PostgreSQL (JSONB, `||` merge, GENERATED IDENTITY, ON CONFLICT
have no SQLite equivalent). Point DATABASE_URL at a throwaway database:

    DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/feedling_test \
        pytest tests/test_db.py -v

Each test uses a unique user_id so they don't collide and the suite is
re-runnable without a fresh DB.
"""

import base64
from datetime import datetime
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

if not os.environ.get("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set — needs a real Postgres", allow_module_level=True)

import db  # noqa: E402

from conftest import seed_user  # noqa: E402

db.init_schema()


def _uid() -> str:
    return f"usr_{uuid.uuid4().hex[:16]}"


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


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
    seed_user(uid)
    assert db.get_blob(uid, "identity") is None
    db.set_blob(uid, "identity", {"agent_name": "Iris", "v": 1})
    assert db.get_blob(uid, "identity") == {"agent_name": "Iris", "v": 1}
    # list blob (tokens) round-trips too
    db.set_blob(uid, "tokens", [{"token": "abc", "status": "active"}])
    assert db.get_blob(uid, "tokens") == [{"token": "abc", "status": "active"}]


def test_set_blob_if_unchanged_cas():
    uid = _uid()
    seed_user(uid)
    base = {"fingerprint": "f0", "servers": [{"name": "a", "transport": "http"}]}
    db.set_blob(uid, "user_mcp", base)

    # CAS with the current value swaps and returns True; JSONB equality is
    # semantic, so a re-ordered but equal expected still matches.
    expected_reordered = {"servers": [{"transport": "http", "name": "a"}],
                          "fingerprint": "f0"}
    nxt = {"fingerprint": "f1", "servers": [{"name": "a", "transport": "sse"}]}
    assert db.set_blob_if_unchanged(uid, "user_mcp", expected_reordered, nxt) is True
    assert db.get_blob(uid, "user_mcp") == nxt

    # CAS against a now-stale expectation must NOT write and returns False.
    stale = base
    other = {"fingerprint": "f2", "servers": []}
    assert db.set_blob_if_unchanged(uid, "user_mcp", stale, other) is False
    assert db.get_blob(uid, "user_mcp") == nxt   # unchanged

    # Missing row (kind never written) also returns False without resurrecting.
    assert db.set_blob_if_unchanged(uid, "never", {"x": 1}, {"x": 2}) is False
    assert db.get_blob(uid, "never") is None


def test_get_blobs_for_users_batches_and_omits_missing_rows():
    uid_a = _uid()
    uid_b = _uid()
    seed_user(uid_a)
    seed_user(uid_b)
    db.set_blob(uid_a, "trace", {"events": [1]})
    db.set_blob(uid_b, "enabled", {"enabled": True})

    rows = db.get_blobs_for_users(
        [uid_a, uid_b, uid_a, ""],
        ["trace", "enabled", "trace", ""],
    )

    assert rows == {
        (uid_a, "trace"): {"events": [1]},
        (uid_b, "enabled"): {"enabled": True},
    }


def test_blob_delete_and_list_by_prefix():
    uid = _uid()
    seed_user(uid)
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
    seed_user(uid)
    for i in range(5):
        db.chat_append(uid, f"m{i}", float(i), {"id": f"m{i}", "body_ct": f"ct{i}"}, max_messages=3)
    loaded = db.chat_load(uid)
    # ring buffer keeps the newest 3, in insertion order
    assert [m["id"] for m in loaded] == ["m2", "m3", "m4"]
    assert loaded[0]["body_ct"] == "ct2"


def _idempotent_chat_doc(msg_id: str, client_msg_id: str, ts: float) -> dict:
    return {
        "id": msg_id,
        "role": "user",
        "source": "chat",
        "ts": ts,
        "v": 1,
        "body_ct": f"ct-{msg_id}",
        "client_msg_id": client_msg_id,
    }


def test_chat_append_idempotent_same_key_returns_first_row():
    uid = _uid()
    seed_user(uid)
    key = str(uuid.uuid4())
    now = time.time()
    first_doc = _idempotent_chat_doc("idem-first", key, now)
    retry_doc = _idempotent_chat_doc("idem-retry", key, now + 0.01)

    first, first_inserted = db.chat_append_idempotent(
        uid, first_doc["id"], first_doc["ts"], first_doc, 5000,
        client_msg_id=key, window_sec=600,
    )
    retry, retry_inserted = db.chat_append_idempotent(
        uid, retry_doc["id"], retry_doc["ts"], retry_doc, 5000,
        client_msg_id=key, window_sec=600,
    )

    assert first_inserted is True
    assert retry_inserted is False
    assert retry == first
    assert [row["id"] for row in db.chat_load(uid)] == ["idem-first"]


def test_chat_append_idempotent_cross_worker_race_has_one_winner():
    uid = _uid()
    seed_user(uid)
    key = str(uuid.uuid4())
    barrier = threading.Barrier(2)
    outcomes: list = [None, None]

    def _run(index: int) -> None:
        msg_id = f"race-{index}"
        ts = time.time()
        doc = _idempotent_chat_doc(msg_id, key, ts)
        barrier.wait()
        outcomes[index] = db.chat_append_idempotent(
            uid, msg_id, ts, doc, 5000,
            client_msg_id=key, window_sec=600,
        )

    threads = [threading.Thread(target=_run, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    (winner_a, inserted_a), (winner_b, inserted_b) = outcomes
    assert sorted([inserted_a, inserted_b]) == [False, True]
    assert winner_a == winner_b
    rows = db.chat_load(uid)
    assert len(rows) == 1
    assert rows[0] == winner_a


def test_chat_append_idempotent_distinct_keys_and_expired_key_insert():
    uid = _uid()
    seed_user(uid)
    now = time.time()
    old_key = str(uuid.uuid4())
    fresh_key = str(uuid.uuid4())
    old_doc = _idempotent_chat_doc("expired-first", old_key, now - 601)
    replacement = _idempotent_chat_doc("expired-second", old_key, now)
    distinct = _idempotent_chat_doc("distinct", fresh_key, now + 0.01)

    results = [
        db.chat_append_idempotent(
            uid, doc["id"], doc["ts"], doc, 5000,
            client_msg_id=doc["client_msg_id"], window_sec=600,
        )
        for doc in (old_doc, replacement, distinct)
    ]

    assert [inserted for _winner, inserted in results] == [True, True, True]
    assert [row["id"] for row in db.chat_load(uid)] == [
        "expired-first", "expired-second", "distinct",
    ]


def test_chat_update_metadata_merges():
    uid = _uid()
    seed_user(uid)
    db.chat_append(uid, "x1", 1.0, {"id": "x1", "body_ct": "ct", "visibility": "shared"}, max_messages=0)
    merged = db.chat_update_metadata(uid, "x1", {"alert_status": "sent"})
    assert merged["alert_status"] == "sent"
    assert merged["body_ct"] == "ct"  # original field preserved
    assert db.chat_update_metadata(uid, "missing", {"a": "b"}) is None


def test_chat_delete():
    uid = _uid()
    seed_user(uid)
    db.chat_append(uid, "d1", 1.0, {"id": "d1"}, max_messages=0)
    assert db.chat_delete(uid, "d1") is True
    assert db.chat_delete(uid, "d1") is False
    assert db.chat_load(uid) == []


def test_memory_upsert_replace_delete():
    uid = _uid()
    seed_user(uid)
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
    seed_user(uid)
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
    seed_user(uid)
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
    seed_user(uid)
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
    seed_user(uid)
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


def test_admin_data_track_snapshot_aggregates_app_sessions():
    active_uid = _uid()
    empty_uid = _uid()
    seed_user(active_uid)
    seed_user(empty_uid)

    db.log_append(
        active_uid,
        "tracking_events",
        {"type": "app_session_end", "payload": {"duration_sec": 45}},
        ts=100.0,
    )
    db.log_append(
        active_uid,
        "tracking_events",
        {"type": "app_session_end", "payload": {"duration_sec": 75}},
        ts=200.0,
    )
    # Malformed duration still counts as a session but contributes no time.
    db.log_append(
        active_uid,
        "tracking_events",
        {"type": "app_session_end", "payload": {"duration_sec": "bad"}},
        ts=300.0,
    )
    db.log_append(
        active_uid,
        "tracking_events",
        {"type": "app_open", "payload": {"duration_sec": 999}},
        ts=400.0,
    )

    snapshot = db.admin_data_track_snapshot([active_uid, empty_uid])

    assert snapshot[active_uid]["app_usage"] == {
        "foreground_sec": 120,
        "sessions": 3,
        "last_at": 300.0,
    }
    assert snapshot[empty_uid]["app_usage"] == {
        "foreground_sec": 0,
        "sessions": 0,
        "last_at": None,
    }


def test_admin_data_track_dau_aggregates_app_sessions_by_beijing_day():
    user_a = _uid()
    user_b = _uid()
    chat_only = _uid()
    for uid in (user_a, user_b, chat_only):
        seed_user(uid)

    since = _epoch("2030-06-01T17:00:00Z")
    day2_a = _epoch("2030-06-01T18:00:00Z")  # 2030-06-02 Beijing
    day2_b = _epoch("2030-06-01T18:10:00Z")
    day2_c = _epoch("2030-06-01T18:20:00Z")
    day3_bad = _epoch("2030-06-02T16:30:00Z")  # 2030-06-03 Beijing
    day4_chat = _epoch("2030-06-03T17:00:00Z")  # 2030-06-04 Beijing

    # Same Beijing day: three sessions across two users, avg = (60+180+120)/3.
    for uid, ts, duration in (
        (user_a, day2_a, 60),
        (user_a, day2_b, 180),
        (user_b, day2_c, 120),
    ):
        db.log_append(
            uid,
            "tracking_events",
            {"type": "app_session_end", "payload": {"duration_sec": duration}},
            ts=ts,
        )
    # Non-session tracking events still count for DAU but not usage duration.
    db.log_append(
        user_a,
        "tracking_events",
        {"type": "app_open", "payload": {"duration_sec": 999}},
        ts=day2_c + 10,
    )
    # Malformed duration follows the per-user snapshot contract: session yes, 0s.
    db.log_append(
        user_b,
        "tracking_events",
        {"type": "app_session_end", "payload": {"duration_sec": "bad"}},
        ts=day3_bad,
    )
    # Same Beijing day as day2, but before the caller's since bound.
    db.log_append(
        user_b,
        "tracking_events",
        {"type": "app_session_end", "payload": {"duration_sec": 1000}},
        ts=_epoch("2030-06-01T16:50:00Z"),
    )
    # A chat-only active day should carry zeroed usage fields.
    db.chat_append(
        chat_only,
        f"dau_chat_only_{chat_only}",
        day4_chat,
        {"id": f"dau_chat_only_{chat_only}", "role": "user", "source": "chat"},
        max_messages=0,
    )

    rows = db.admin_data_track_dau(since_epoch=since, days=10, tz="Asia/Shanghai")
    by_day = {row["day"]: row for row in rows}

    assert by_day["2030-06-02"]["avg_session_sec"] == 120.0
    assert by_day["2030-06-02"]["foreground_sec"] == 360
    assert by_day["2030-06-02"]["session_count"] == 3
    assert by_day["2030-06-02"]["session_dau"] == 2
    assert by_day["2030-06-02"]["tracking_events"] == 4

    assert by_day["2030-06-03"]["avg_session_sec"] == 0.0
    assert by_day["2030-06-03"]["foreground_sec"] == 0
    assert by_day["2030-06-03"]["session_count"] == 1
    assert by_day["2030-06-03"]["session_dau"] == 1

    assert by_day["2030-06-04"]["dau"] == 1
    assert by_day["2030-06-04"]["chat_dau"] == 1
    assert by_day["2030-06-04"]["avg_session_sec"] == 0.0
    assert by_day["2030-06-04"]["foreground_sec"] == 0
    assert by_day["2030-06-04"]["session_count"] == 0
    assert by_day["2030-06-04"]["session_dau"] == 0
    # Median of per-user daily totals: user_a=60+180=240, user_b=120 → median 180;
    # a chat-only day has no sessions → 0.
    assert by_day["2030-06-02"]["median_user_sec"] == 180.0
    assert by_day["2030-06-04"]["median_user_sec"] == 0.0


def test_admin_data_track_dau_median_is_robust_to_heavy_users():
    """Median per-user foreground ≠ the mean when a heavy user skews the day."""
    light_a = _uid()
    light_b = _uid()
    heavy = _uid()
    for uid in (light_a, light_b, heavy):
        seed_user(uid)

    since = _epoch("2031-03-01T17:00:00Z")
    base = _epoch("2031-03-01T18:00:00Z")  # 2031-03-02 Beijing
    # Per-user daily totals: 10, 20, 300 → mean 110, median 20.
    for uid, duration in ((light_a, 10), (light_b, 20), (heavy, 300)):
        db.log_append(
            uid,
            "tracking_events",
            {"type": "app_session_end", "payload": {"duration_sec": duration}},
            ts=base,
        )

    by_day = {
        row["day"]: row
        for row in db.admin_data_track_dau(since_epoch=since, days=10, tz="Asia/Shanghai")
    }
    day = by_day["2031-03-02"]
    assert day["session_dau"] == 3
    assert day["foreground_sec"] == 330            # mean per user = 110
    assert day["median_user_sec"] == 20.0          # median is not fooled by the heavy user


def test_dau_daily_snapshot_freezes_completed_days_and_preserves_live_fallback():
    old_live = _uid()
    frozen_chat = _uid()
    frozen_tracking = _uid()
    today_live = _uid()
    for uid in (old_live, frozen_chat, frozen_tracking, today_live):
        seed_user(uid)

    day2 = _epoch("2042-08-09T18:00:00Z")  # Beijing 2042-08-10 (pre-boundary)
    day3_chat = _epoch("2042-08-10T17:00:00Z")
    day3_tracking = _epoch("2042-08-10T18:00:00Z")
    day4 = _epoch("2042-08-11T17:00:00Z")  # Beijing 2042-08-12 (today at first tick)
    now_day4 = _epoch("2042-08-12T04:00:00+08:00")

    db.log_append(old_live, "tracking_events", {"type": "app_open"}, ts=day2)
    db.chat_append(
        frozen_chat,
        f"frozen_chat_{frozen_chat}",
        day3_chat,
        {"id": f"frozen_chat_{frozen_chat}", "role": "user", "source": "chat"},
        max_messages=0,
    )
    db.log_append(
        frozen_tracking,
        "tracking_events",
        {"type": "app_session_end", "payload": {"duration_sec": 120}},
        ts=day3_tracking,
    )
    db.log_append(today_live, "tracking_events", {"type": "app_open"}, ts=day4)

    pool = db.get_pool()
    with pool.connection() as conn:
        conn.execute(
            "DELETE FROM dau_daily_snapshot WHERE day BETWEEN %s AND %s",
            ("2042-08-10", "2042-08-13"),
        )

    try:
        # First run establishes the rollout boundary at yesterday only. Older
        # history remains live/understated; today must never be frozen.
        assert db.freeze_completed_dau_days(now_epoch=now_day4) == ["2042-08-11"]
        assert db.admin_dau_snapshot_bounds() == {
            "first_day": "2042-08-11",
            "last_day": "2042-08-11",
            "days": 1,
        }
        rows = db.admin_data_track_dau(days=10)
        by_day = {row["day"]: row for row in rows}
        assert by_day["2042-08-10"]["frozen"] is False  # before boundary
        assert by_day["2042-08-11"]["frozen"] is True
        assert by_day["2042-08-11"]["dau"] == 2
        assert by_day["2042-08-11"]["session_count"] == 1
        assert by_day["2042-08-11"]["foreground_sec"] == 120
        # Median per-user foreground survives the freeze round-trip (single user
        # with a 120s total → median 120).
        assert by_day["2042-08-11"]["median_user_sec"] == 120.0
        assert by_day["2042-08-12"]["frozen"] is False  # current Beijing day
        with pool.connection() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM dau_daily_snapshot WHERE day = %s",
                ("2042-08-12",),
            ).fetchone()[0] == 0

        # An intraday since cuts through the frozen day, so exact legacy filter
        # semantics win and that one day is computed live instead of using the
        # full-day snapshot.
        partial = db.admin_data_track_dau(since_epoch=day3_tracking, days=10)
        partial_day3 = {row["day"]: row for row in partial}["2042-08-11"]
        assert partial_day3["frozen"] is False
        assert partial_day3["dau"] == 1

        # Hard-delete both users behind the completed day. Its frozen metrics
        # and timestamps remain available and unchanged.
        db.delete_user(frozen_chat)
        db.delete_user(frozen_tracking)
        frozen_after_delete = {
            row["day"]: row for row in db.admin_data_track_dau(days=10)
        }["2042-08-11"]
        assert frozen_after_delete["frozen"] is True
        assert frozen_after_delete["dau"] == 2
        assert frozen_after_delete["session_count"] == 1
        assert frozen_after_delete["last_ts"] == day3_tracking

        # Existing rows are never overwritten, even when a later tick catches
        # up additional completed days after downtime.
        with pool.connection() as conn:
            conn.execute(
                "UPDATE dau_daily_snapshot SET dau = 77 WHERE day = %s",
                ("2042-08-11",),
            )
        now_day6 = _epoch("2042-08-14T04:00:00+08:00")
        assert db.freeze_completed_dau_days(now_epoch=now_day6) == [
            "2042-08-12",
            "2042-08-13",
        ]
        assert db.freeze_completed_dau_days(now_epoch=now_day6) == []
        with pool.connection() as conn:
            assert conn.execute(
                "SELECT dau FROM dau_daily_snapshot WHERE day = %s",
                ("2042-08-11",),
            ).fetchone()[0] == 77
            # No-activity days are still snapshotted to make the boundary and
            # catch-up range durable, but are omitted from the active-day API.
            assert conn.execute(
                "SELECT active_events FROM dau_daily_snapshot WHERE day = %s",
                ("2042-08-13",),
            ).fetchone()[0] == 0
        by_day = {row["day"]: row for row in db.admin_data_track_dau(days=10)}
        assert by_day["2042-08-12"]["frozen"] is True
        assert "2042-08-13" not in by_day
    finally:
        with pool.connection() as conn:
            conn.execute(
                "DELETE FROM dau_daily_snapshot WHERE day BETWEEN %s AND %s",
                ("2042-08-10", "2042-08-13"),
            )


def test_log_patch_item_only_if_status():
    uid = _uid()
    seed_user(uid)
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
    seed_user(uid)
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


def test_supervisor_instance_heartbeat_roundtrips_the_pi_capability_bit():
    """``pi`` MUST survive the write→read roundtrip.

    Unlike host_all/gateway, ``pi`` has no dedicated column — the supervisor only
    ever puts it in the JSONB ``payload``. The reader long SELECTed the promoted
    columns but not ``payload``, so ``pi`` silently vanished and every row came
    back WITHOUT the key. ``evaluate_supervisor_heartbeat`` then read
    ``hb.get("pi")`` → None → falsy → ``supervisor_pi_disabled``, so
    ``/v1/model_api/chat/send`` returned 503 for EVERY pi-driver user (i.e. every
    deepseek/gemini/openrouter/openai_compatible account) whenever a fresh
    instance row existed — the healthier the runner, the harder it 503'd, since a
    fresh row also suppressed the legacy-heartbeat fallback that still carried pi.
    Observed live on test 2026-07-13 (gated events, live_reason=supervisor_pi_disabled)."""
    owner = _owner()
    db.set_supervisor_instance_heartbeat(owner, _hb_payload(owner, pi=True))
    row = next(r for r in db.list_supervisor_instance_heartbeats() if r["owner"] == owner)
    assert row["pi"] is True

    owner_off = _owner()
    db.set_supervisor_instance_heartbeat(owner_off, _hb_payload(owner_off, pi=False))
    row_off = next(r for r in db.list_supervisor_instance_heartbeats() if r["owner"] == owner_off)
    assert row_off["pi"] is False


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
    seed_user(uid)
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
