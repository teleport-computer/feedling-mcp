"""T680 read-cost changes: old SQL is an independent semantic oracle."""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import hashlib
import itertools

import psycopg
from psycopg.types.json import Jsonb
import pytest

import db
import test_data_track as data_track_tests
from accounts import registry
from admin import data_track
from test_data_track import (
    _admin_headers, _register, _seed_every_paged_slice,
    _seed_memory_users, _take_clock_fields, _append_chat_at,
)

client = data_track_tests.client

# Frozen from bf26c0d2, before T680. Do not implement this with the new helper.
def _legacy_memory_breakdowns_into(conn, ids: list[str], out: dict, ensure) -> None:
    """The original three memory queries and field mapping."""
    rows = conn.execute(
        """
        SELECT user_id,
               MIN(NULLIF(doc->>'created_at', '')) AS first_created_at,
               MIN(NULLIF(doc->>'occurred_at', '')) AS earliest_occurred_at,
               MAX(NULLIF(doc->>'occurred_at', '')) AS latest_occurred_at
        FROM memory_moments
        WHERE user_id = ANY(%s)
        GROUP BY user_id
        """,
        (ids,),
    ).fetchall()
    for uid, first_created_at, earliest_occurred_at, latest_occurred_at in rows:
        memory = ensure(out, uid).setdefault("memory", {})
        memory["first_created_at"] = first_created_at or ""
        memory["earliest_occurred_at"] = earliest_occurred_at or ""
        memory["latest_occurred_at"] = latest_occurred_at or ""

    for field, target in (("type", "by_type"), ("source", "by_source")):
        rows = conn.execute(
            """
            SELECT user_id, COALESCE(NULLIF(doc->>%s, ''), 'unknown') AS value,
                   COUNT(*)::int
            FROM memory_moments
            WHERE user_id = ANY(%s)
            GROUP BY user_id, value
            """,
            (field, ids),
        ).fetchall()
        for uid, value, count in rows:
            memory = ensure(out, uid).setdefault("memory", {})
            memory.setdefault(target, {})[value] = count


_FIVE_STREAMS_SQL = """
    SELECT user_id, stream, COUNT(*)::int, MAX(ts)
    FROM user_logs
    WHERE user_id = ANY(%s)
      AND stream IN ('memory_changes', 'gate_decisions', 'proactive_jobs',
                     'device_events', 'tracking_events')
    GROUP BY user_id, stream
"""


def _legacy_logs(conn, ids):
    out = {uid: {} for uid in ids}
    for uid, stream, count, last_ts in conn.execute(_FIVE_STREAMS_SQL, (ids,)):
        out[uid][stream] = {"count": count, "last_ts": last_ts}
    return out


def _use_legacy_reads(monkeypatch):
    """Keep unrelated reads/mappers; restore exactly the SQL changed by T680."""
    current_snapshot = db.admin_data_track_snapshot
    current_page = db.admin_paged_log_streams

    def snapshot(ids, **kwargs):
        result = current_snapshot(ids, **kwargs)
        with db._admin_data_track_connection() as conn:
            old = _legacy_logs(conn, ids)
        for uid, snap in result.items():
            logs = snap.setdefault("logs", {})
            for stream in ("memory_changes", "gate_decisions", "proactive_jobs",
                           "device_events", "tracking_events"):
                logs.pop(stream, None)
            logs.update(old[uid])
        return result

    def page(ids):
        result, status = current_page(ids)
        return {uid: {key: value for key, value in logs.items()
                      if key == "bootstrap_events"}
                for uid, logs in result.items()}, status

    monkeypatch.setattr(db, "admin_data_track_snapshot", snapshot)
    monkeypatch.setattr(db, "admin_paged_log_streams", page)
    monkeypatch.setattr(db, "_memory_breakdowns_into", _legacy_memory_breakdowns_into)


def _fingerprint_logs(ids):
    for index, uid in enumerate(ids):
        for n in range(index % 5 + 1):
            for stream in ("tracking_events", "device_events", "proactive_jobs",
                           "gate_decisions"):
                db.log_append(uid, stream, {"type": "other", "status": "posted"},
                              ts=1700000000 + index if n else None)


def test_tracking_device_page_counts_match_original_fleet_at_nonzero_offset(client):
    ids = _seed_memory_users(client, 5)
    _fingerprint_logs(ids)
    with db._admin_data_track_connection() as conn:
        old = _legacy_logs(conn, ids)
    response = client.get(
        "/v1/admin/data-track/users?sort=memory&dir=asc&limit=2&offset=2",
        headers=_admin_headers(),
    )
    assert response.status_code == 200
    rows = response.get_json()["users"]
    assert [row["user_id"] for row in rows] == ids[2:4]
    for row in rows:
        uid = row["user_id"]
        snap = {"logs": old[uid], "legacy_background_breakdowns_status": "omitted",
                "snapshot_read_status": {"level": "ok", "message": ""}}
        assert row["tracking"] == data_track._data_track_tracking_from_snapshot(snap)
        assert row["proactive"] == data_track._with_proactive_lens(
            data_track._data_track_proactive_from_snapshot(snap, row["chat"]))
        assert row["tracking"]["events"] == ids.index(uid) + 1
        assert row["proactive"]["device_events"] == ids.index(uid) + 1


@pytest.mark.parametrize("stream", ["tracking_events", "proactive_jobs", "gate_decisions"])
def test_off_page_log_timestamps_still_determine_fleet_activity(client, stream):
    ids = _seed_memory_users(client, 3)
    # Registration, memory and chat must not independently make anybody active.
    for user in registry._users:
        user["created_at"] = "2020-01-01T00:00:00Z"
    now = datetime.now(timezone.utc).timestamp()
    db.log_append(ids[0], stream, {"type": "other"}, ts=now - 3600)
    db.log_append(ids[1], stream, {"type": "other"}, ts=now - 2 * 86400)
    # NULL sorts first under DESC; MAX must still ignore it.
    db.log_append(ids[0], stream, {"type": "other"}, ts=None)
    body = client.get(
        "/v1/admin/data-track/users?sort=memory&dir=asc&limit=1&offset=2",
        headers=_admin_headers(),
    ).get_json()
    assert [row["user_id"] for row in body["users"]] == [ids[2]]
    assert body["summary"]["activation_funnel"]["active_1d"] == 1
    assert body["summary"]["activation_funnel"]["active_3d"] == 2
    assert body["summary"]["human_active_3d"] == 0


def test_paged_tracking_device_failure_remains_unknown(client, monkeypatch):
    ids = _seed_memory_users(client, 3)
    _fingerprint_logs(ids)

    def unavailable(*args, **kwargs):
        raise psycopg.errors.QueryCanceled("injected page timeout")

    monkeypatch.setattr(db, "_paged_log_streams_into", unavailable)
    body = client.get("/v1/admin/data-track/users?limit=2&offset=1",
                      headers=_admin_headers()).get_json()
    assert len(body["users"]) == 2
    for row in body["users"]:
        assert row["snapshot_read_status"]["level"] != "ok"
        assert row["tracking"]["counts_status"] == "unknown"
        assert row["proactive"]["counts_status"] == "unknown"
        assert row["bootstrap_events"]["counts_status"] == "unknown"


def test_memory_fused_reader_matches_three_queries_for_every_json_shape(monkeypatch):
    # Actual PG JSONB coercions, default planner and real admin reader. TEMP
    # names shadow the relation on this one local test connection only.
    large = "".join(hashlib.sha256(str(n).encode()).hexdigest() for n in range(180))
    docs = [
        {}, {"type": "", "source": "", "created_at": "", "occurred_at": ""},
        {"type": "fact", "source": "chat", "created_at": "2021-01-01",
         "occurred_at": "2020-01-01", "body_ct": large},
        {"type": 123, "source": False, "created_at": 9, "occurred_at": True},
        {"type": ["x", 1], "source": {"a": 1}, "created_at": [1, 2],
         "occurred_at": {"b": 2, "a": 1}},
        {"type": None, "source": None, "created_at": None, "occurred_at": None},
        None, [1, {"type": "nested"}], "scalar", 42, False,
    ]
    with db.get_pool().connection() as conn:
        conn.execute("CREATE TEMP TABLE memory_moments(user_id text, doc jsonb)")
        try:
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO memory_moments VALUES (%s, %s)",
                                [(f"u{n % 3}", Jsonb(doc)) for n, doc in enumerate(docs)])
            assert conn.execute("SELECT max(pg_column_size(doc)) FROM memory_moments").fetchone()[0] > 8191
            conn.execute("CREATE INDEX ON memory_moments(user_id)")
            conn.execute("ANALYZE memory_moments")
            ids = ["u0", "u1", "u2", "empty"]
            expected = {}
            _legacy_memory_breakdowns_into(conn, ids, expected,
                                            lambda bucket, uid: bucket.setdefault(uid, {}))
            calls = []

            class Recorded:
                def execute(self, sql, params=None):
                    calls.append(str(sql))
                    return conn.execute(sql, params)

            @contextlib.contextmanager
            def local_connection(**kwargs):
                yield Recorded()

            monkeypatch.setattr(db, "_admin_data_track_connection", local_connection)
            actual, status = db.admin_memory_breakdowns(ids)
            assert status == {"level": "ok", "message": ""}
            assert actual == {uid: row["memory"] for uid, row in expected.items()}
            assert len(calls) == 1, "restoring three scans must fail this cost guard"
            assert "MATERIALIZED" in calls[0]
            # Invalid JSON cannot be a stored JSONB doc. The failed insertion
            # rolls back its savepoint; the successful read remains unchanged.
            with pytest.raises(psycopg.errors.InvalidTextRepresentation):
                with conn.transaction():
                    conn.execute("INSERT INTO memory_moments VALUES ('bad', %s::jsonb)",
                                 ('{"type":',))
            assert db.admin_memory_breakdowns(ids) == (actual, status)
        finally:
            conn.execute("DROP TABLE memory_moments")


def test_full_payload_matrix_matches_old_sql_at_one_clock_and_snapshot(client, monkeypatch):
    ids = _seed_every_paged_slice(client, 5)
    # >100 users distinguishes all three normal page widths, plus max=500.
    ids.extend(_register(client)[0] for _ in range(101))
    _fingerprint_logs(ids)
    chat_epoch = datetime.now(timezone.utc).timestamp() - 3600
    for index, uid in enumerate(ids[:6]):
        _append_chat_at(uid, f"t680-{index}", "user", "chat", chat_epoch + index)
    fixed = datetime.now()

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(data_track, "datetime", FrozenDateTime)
    monkeypatch.setattr(data_track.time, "time", lambda: fixed.timestamp())
    with db.get_pool().connection() as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")

            @contextlib.contextmanager
            def same_snapshot(**kwargs):
                yield conn

            monkeypatch.setattr(db, "_admin_data_track_connection", same_snapshot)
            urls = [f"/v1/admin/data-track/users?limit={limit}&sort={sort}&dir={direction}&offset=2"
                    for limit, sort, direction in itertools.product(
                        [5, 20, 100, 500], ["memory", "proactive", "chat"], ["asc", "desc"])]
            current = []
            for url in urls:
                response = client.get(url, headers=_admin_headers())
                assert response.status_code == 200
                current.append(response.get_json())
            with monkeypatch.context() as old_patch:
                _use_legacy_reads(old_patch)
                for url, actual in zip(urls, current):
                    response = client.get(url, headers=_admin_headers())
                    assert response.status_code == 200
                    old = response.get_json()
                    assert actual["users"] and old["users"]
                    assert actual["summary"] == old["summary"], url
                    _take_clock_fields(actual)
                    _take_clock_fields(old)
                    differences = []
                    def compare(a, b, path=""):
                        if isinstance(a, dict) and isinstance(b, dict):
                            for key in a.keys() | b.keys():
                                compare(a.get(key), b.get(key), path + "." + key)
                        elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
                            for i, (left, right) in enumerate(zip(a, b)):
                                compare(left, right, path + f"[{i}]")
                        elif a != b:
                            differences.append((path, a, b))
                    compare(actual, old)
                    assert not differences, (url, differences[:10])
    # limit=0 is the HTTP clamp-to-one contract, not an unbounded request.
    response = client.get("/v1/admin/data-track/users?limit=0", headers=_admin_headers())
    assert response.status_code == 200
    assert len(response.get_json()["users"]) == 1
