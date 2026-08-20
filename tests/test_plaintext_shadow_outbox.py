from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from plaintext_shadow import outbox


class _Pool:
    def __init__(self, dsn: str):
        self.dsn = dsn

    @contextmanager
    def connection(self):
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            yield conn


@pytest.fixture
def primary_pool(monkeypatch):
    pool = _Pool(os.environ["TEE_DATABASE_URL"])
    monkeypatch.setattr(outbox.db, "get_pool", lambda: pool)
    with pool.connection() as conn:
        conn.execute("TRUNCATE plaintext_shadow_dirty_keys")
    yield pool
    with pool.connection() as conn:
        conn.execute("TRUNCATE plaintext_shadow_dirty_keys")


def _seed(pool, *, generation=10, attempts=0, table="server_config", key=None):
    key = {"key": "shadow-test"} if key is None else key
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO plaintext_shadow_dirty_keys "
            "(table_name, key_json, operation, generation, attempts) "
            "VALUES (%s, %s, 'UPDATE', %s, %s)",
            (table, Jsonb(key), generation, attempts),
        )
    return table, key, generation


def _row(pool):
    with pool.connection() as conn:
        return conn.execute(
            "SELECT table_name, key_json, generation, attempts, next_attempt_at, "
            "last_error_slug, quarantined_at FROM plaintext_shadow_dirty_keys"
        ).fetchone()


def test_success_acknowledges_only_claimed_generation(primary_pool, monkeypatch):
    _seed(primary_pool)
    monkeypatch.setattr(outbox, "apply_key", lambda _row, **_kwargs: {"applied": 1})

    report = outbox.drain_once(limit=1)

    assert report.applied == 1
    assert _row(primary_pool) is None


def test_newer_generation_survives_old_ack(primary_pool, monkeypatch):
    _seed(primary_pool, generation=10)

    def bump_generation(_claimed, **_kwargs):
        with primary_pool.connection() as conn:
            conn.execute(
                "UPDATE plaintext_shadow_dirty_keys SET generation=11, "
                "next_attempt_at=now(), attempts=0 WHERE table_name='server_config'"
            )
        return {"applied": 1}

    monkeypatch.setattr(outbox, "apply_key", bump_generation)
    outbox.drain_once(limit=1)

    assert _row(primary_pool)[2] == 11


def test_failure_records_slug_not_plaintext(primary_pool, monkeypatch):
    _seed(primary_pool)

    def fail(_row, **_kwargs):
        raise RuntimeError("secret body must not be persisted")

    monkeypatch.setattr(outbox, "apply_key", fail)
    report = outbox.drain_once(limit=1)
    row = _row(primary_pool)

    assert report.retried == 1
    assert row[3] == 1
    assert row[5] == "shadow_apply_failed"
    assert "secret body" not in json.dumps(row, default=str)


def test_retry_delay_is_bounded_exponential():
    assert [outbox.retry_delay(n) for n in range(1, 7)] == [
        timedelta(seconds=30),
        timedelta(seconds=60),
        timedelta(seconds=120),
        timedelta(seconds=240),
        timedelta(seconds=300),
        timedelta(seconds=300),
    ]


def test_twentieth_failure_quarantines_and_keeps_key(primary_pool, monkeypatch):
    _seed(primary_pool, attempts=19)
    monkeypatch.setattr(
        outbox,
        "apply_key",
        lambda _row, **_kwargs: (_ for _ in ()).throw(RuntimeError("bad envelope")),
    )

    report = outbox.drain_once(limit=1)
    row = _row(primary_pool)

    assert report.quarantined == 1
    assert row[3] == 20
    assert row[5] == "shadow_apply_failed"
    assert row[6] is not None


def test_claim_skips_quarantined_and_future_rows(primary_pool, monkeypatch):
    _seed(primary_pool, generation=10)
    with primary_pool.connection() as conn:
        conn.execute(
            "UPDATE plaintext_shadow_dirty_keys SET next_attempt_at=now() + interval '1 hour'"
        )
    called = []
    monkeypatch.setattr(outbox, "apply_key", lambda row, **_kwargs: called.append(row))

    report = outbox.drain_once(limit=10)

    assert report.claimed == 0
    assert called == []


@pytest.mark.parametrize(
    ("table", "key", "owner", "method"),
    [
        ("server_config", {"key": "x"}, outbox.reconciler, "reconcile_keys"),
        (
            "chat_messages",
            {"user_id": "u", "msg_id": "m"},
            outbox.worker,
            "run_keys",
        ),
        ("agent_jobs", {"id": "j"}, outbox.snapshot, "snapshot_table"),
    ],
)
def test_apply_key_routes_registry_lanes(monkeypatch, table, key, owner, method):
    calls = []
    monkeypatch.setattr(owner, method, lambda *args, **kwargs: calls.append((args, kwargs)) or {})
    row = outbox.DirtyKey(table, key, "UPDATE", 1, 0)

    outbox.apply_key(row, target_policy="policy")

    assert calls
    assert calls[0][1]["target_policy"] == "policy"


def test_user_blob_identity_routes_to_ciphertext_worker(monkeypatch):
    calls = []
    monkeypatch.setattr(
        outbox.worker,
        "run_keys",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {},
    )
    row = outbox.DirtyKey(
        "user_blobs", {"user_id": "u", "kind": "identity"}, "UPDATE", 1, 0
    )

    outbox.apply_key(row)

    assert calls[0][0] == ("identity", [row.key_json])


def test_retry_survives_restart_and_recovers(primary_pool, monkeypatch):
    _seed(primary_pool)
    monkeypatch.setattr(
        outbox,
        "apply_key",
        lambda _row, **_kwargs: (_ for _ in ()).throw(TimeoutError("target timeout")),
    )
    first = outbox.drain_once(limit=1)
    assert first.retried == 1

    with primary_pool.connection() as conn:
        conn.execute(
            "UPDATE plaintext_shadow_dirty_keys SET next_attempt_at=now()"
        )
    monkeypatch.setattr(outbox, "apply_key", lambda _row, **_kwargs: {"applied": 1})
    second = outbox.drain_once(limit=1)

    assert second.applied == 1
    assert _row(primary_pool) is None


def test_snapshot_table_is_applied_once_per_claimed_batch(primary_pool, monkeypatch):
    _seed(primary_pool, generation=20, table="agent_jobs", key={"id": "job-a"})
    _seed(primary_pool, generation=21, table="agent_jobs", key={"id": "job-b"})
    calls = []
    monkeypatch.setattr(
        outbox,
        "apply_key",
        lambda row, **_kwargs: calls.append(row.table_name) or {"ok": True},
    )

    report = outbox.drain_once(limit=10)

    assert report.claimed == 2
    assert calls == ["agent_jobs"]
    assert _row(primary_pool) is None
