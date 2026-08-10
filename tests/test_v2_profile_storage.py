from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import conftest
import db
from model_api_runtime.v2 import profile_store
from model_api_runtime.v2 import serve_worker


def _reset(user_id: str) -> None:
    conftest.seed_user(user_id)
    with db.get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM user_blobs WHERE user_id=%s AND kind=%s",
            (user_id, profile_store.PROFILE_BLOB_KIND),
        )
    with psycopg.connect(os.environ["TEE_DATABASE_URL"]) as conn:
        conn.execute(
            "INSERT INTO users (user_id,created_at,doc) VALUES (%s,%s,'{}') "
            "ON CONFLICT (user_id) DO NOTHING",
            (user_id, "2026-07-31T00:00:00Z"),
        )
        conn.execute(
            "DELETE FROM user_blobs WHERE user_id=%s AND kind=%s",
            (user_id, profile_store.PROFILE_BLOB_KIND),
        )


def _source(
    count: int,
    *,
    generated_at: str = "2026-07-31T00:00:00Z",
) -> dict:
    return {
        "card_count": count,
        "max_updated_at": f"2026-07-{min(31, count + 1):02d}T00:00:00Z",
        "generated_at": generated_at,
    }


def _attempt(attempts: int = 1, *, reject_code: str = "") -> dict:
    return {
        "at": "2026-07-31T00:00:00Z",
        "reject_code": reject_code,
        "attempts": attempts,
        "retry_not_before": 0,
    }


def _seal(_user_id: str, text: str) -> dict:
    return {
        "v": 1,
        "body_ct": "cipher:" + hashlib.sha256(text.encode()).hexdigest(),
        "nonce": "test-nonce",
    }


def _ok_doc(user_id: str, count: int, *, suffix: str = "") -> dict:
    return profile_store.build_profile_document(
        user_id,
        state="ok",
        source=_source(count),
        last_attempt=_attempt(),
        memory_text=f"memory-{count}{suffix}",
        user_text=f"user-{count}{suffix}",
        seal_text=_seal,
    )


def test_profile_document_seals_both_fields_and_keeps_only_bounded_metadata():
    doc = _ok_doc("u-profile-shape", 3)
    rendered = json.dumps(doc, ensure_ascii=False, sort_keys=True)

    assert doc["memory"]["chars"] == len("memory-3")
    assert doc["user"]["chars"] == len("user-3")
    assert "memory-3" not in rendered
    assert "user-3" not in rendered
    assert doc["memory"]["envelope"]["body_ct"].startswith("cipher:")
    assert set(doc) == {
        "v",
        "state",
        "memory",
        "user",
        "source",
        "last_attempt",
        "disabled",
    }


def test_profile_document_rejects_torn_or_plaintext_fields():
    with pytest.raises(profile_store.ProfileStorageError, match="profile_fields_torn"):
        profile_store.build_profile_document(
            "u-profile-torn",
            state="ok",
            source=_source(1),
            last_attempt=_attempt(),
            memory_text="memory only",
            user_text=None,
            seal_text=_seal,
        )

    doc = _ok_doc("u-profile-plaintext-envelope", 1)
    doc["memory"]["envelope"]["plaintext"] = "must never reach JSONB"
    with pytest.raises(
        profile_store.ProfileStorageError, match="plaintext_memory_envelope"
    ):
        profile_store.validate_profile_document(doc)


def test_production_builder_locally_seals_each_field_before_jsonb(monkeypatch):
    store = object()
    calls: list[tuple[object, bytes]] = []
    monkeypatch.setattr(profile_store.core_store, "get_store", lambda _uid: store)

    def _build(got_store, plaintext):
        calls.append((got_store, plaintext))
        return (
            {
                "v": 1,
                "body_ct": "cipher:" + hashlib.sha256(plaintext).hexdigest(),
                "nonce": "n",
            },
            "",
        )

    monkeypatch.setattr(
        profile_store.core_envelope,
        "_build_shared_envelope_for_store",
        _build,
    )

    document = profile_store.build_profile_document(
        "u-profile-local-seal",
        state="ok",
        source=_source(1),
        last_attempt=_attempt(),
        memory_text="memory plaintext",
        user_text="user plaintext",
    )

    assert calls == [
        (store, b"memory plaintext"),
        (store, b"user plaintext"),
    ]
    rendered = json.dumps(document, sort_keys=True)
    assert "memory plaintext" not in rendered
    assert "user plaintext" not in rendered


def test_initial_insert_and_ok_degraded_ok_state_machine_true_pg():
    uid = "u-profile-state-machine"
    _reset(uid)

    first = profile_store.update_profile_cas(uid, lambda _expected: _ok_doc(uid, 1))
    assert first.status == "written"
    assert first.cas_attempts == 1
    ok = db.get_blob_strict(uid, profile_store.PROFILE_BLOB_KIND)
    first_memory_envelope = ok["memory"]["envelope"]

    def _degraded(expected):
        return profile_store.build_profile_document(
            uid,
            state="degraded",
            source=_source(1),
            last_attempt=_attempt(2, reject_code="provider_unavailable"),
            previous=expected,
            seal_text=_seal,
        )

    degraded = profile_store.update_profile_cas(uid, _degraded)
    assert degraded.status == "written"
    stored_degraded = db.get_blob_strict(uid, profile_store.PROFILE_BLOB_KIND)
    assert stored_degraded["state"] == "degraded"
    assert stored_degraded["memory"]["envelope"] == first_memory_envelope

    recovered = profile_store.update_profile_cas(
        uid, lambda _expected: _ok_doc(uid, 2, suffix="-recovered")
    )
    assert recovered.status == "written"
    stored_ok = db.get_blob_strict(uid, profile_store.PROFILE_BLOB_KIND)
    assert stored_ok["state"] == "ok"
    assert stored_ok["source"]["card_count"] == 2
    assert stored_ok["memory"]["envelope"] != first_memory_envelope


def test_profile_source_stats_tracks_count_and_latest_update_true_pg():
    uid = "u-profile-source-stats"
    _reset(uid)

    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO memory_moments (user_id, moment_id, occurred_at, doc) "
            "VALUES (%s, %s, %s, %s::jsonb), (%s, %s, %s, %s::jsonb)",
            (
                uid,
                "m1",
                "2026-07-01T00:00:00Z",
                json.dumps({"updated_at": "2026-07-10T00:00:00Z"}),
                uid,
                "m2",
                "2026-07-02T00:00:00Z",
                json.dumps({"updated_at": "2026-07-20T00:00:00Z"}),
            ),
        )

    assert db.memory_profile_source_stats(uid) == (
        2,
        "2026-07-20T00:00:00Z",
    )

    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE memory_moments SET doc=%s::jsonb "
            "WHERE user_id=%s AND moment_id=%s",
            (
                json.dumps({"updated_at": "2026-07-31T00:00:00Z"}),
                uid,
                "m1",
            ),
        )

    assert db.memory_profile_source_stats(uid) == (
        2,
        "2026-07-31T00:00:00Z",
    )


def test_first_write_uses_empty_expected_and_insert_if_missing(monkeypatch):
    captured = {}
    monkeypatch.setattr(profile_store.db, "get_blob_strict", lambda *_args: None)

    def _cas(_uid, _kind, expected, candidate, *, insert_if_missing):
        captured.update(
            expected=expected,
            candidate=candidate,
            insert_if_missing=insert_if_missing,
        )
        return True

    monkeypatch.setattr(profile_store.db, "set_blob_if_unchanged", _cas)
    result = profile_store.update_profile_cas(
        "u-profile-first-write",
        lambda _expected: _ok_doc("u-profile-first-write", 1),
    )

    assert result.status == "written"
    assert captured["expected"] == {}
    assert captured["insert_if_missing"] is True
    assert captured["candidate"]["state"] == "ok"


def test_two_connection_stale_snapshot_recomputes_instead_of_replaying():
    uid = "u-profile-cas-race"
    _reset(uid)

    # Hold two independent PostgreSQL connections and read the same missing-row
    # snapshot.  Writer B must not replay its first result after writer A wins.
    with db.get_pool().connection() as conn_a, db.get_pool().connection() as conn_b:
        row_a = conn_a.execute(
            "SELECT doc FROM user_blobs WHERE user_id=%s AND kind=%s",
            (uid, profile_store.PROFILE_BLOB_KIND),
        ).fetchone()
        row_b = conn_b.execute(
            "SELECT doc FROM user_blobs WHERE user_id=%s AND kind=%s",
            (uid, profile_store.PROFILE_BLOB_KIND),
        ).fetchone()
    expected_a = dict(row_a[0]) if row_a else {}
    expected_b = dict(row_b[0]) if row_b else {}
    assert expected_a == expected_b == {}

    result_a = profile_store._update_profile_cas_from_expected(
        uid, expected_a, lambda _expected: _ok_doc(uid, 1, suffix="-a")
    )
    assert result_a.status == "written"

    b_calls: list[dict] = []

    def _recompute_b(expected):
        b_calls.append(dict(expected))
        return _ok_doc(uid, 2, suffix=f"-b{len(b_calls)}")

    result_b = profile_store._update_profile_cas_from_expected(
        uid, expected_b, _recompute_b
    )

    assert result_b.status == "written"
    assert result_b.cas_attempts == 2
    assert result_b.recomputations == 2
    assert b_calls[0] == {}
    assert b_calls[1]["source"]["card_count"] == 1
    final = db.get_blob_strict(uid, profile_store.PROFILE_BLOB_KIND)
    assert final == result_b.document
    assert final["memory"]["envelope"] == _seal(uid, "memory-2-b2")


def test_async_cas_recomputes_after_loss_instead_of_replaying(monkeypatch):
    uid = "u-profile-async-cas-race"
    winner = _ok_doc(uid, 1, suffix="-winner")
    reads = iter([{}, winner])
    landed = iter([False, True])
    candidates = []
    recompute_inputs = []

    monkeypatch.setattr(
        profile_store.db,
        "get_blob_strict",
        lambda *_args: next(reads),
    )

    def _cas(_uid, _kind, _expected, candidate, **_kwargs):
        candidates.append(candidate)
        return next(landed)

    monkeypatch.setattr(profile_store.db, "set_blob_if_unchanged", _cas)

    async def _recompute(expected):
        recompute_inputs.append(expected)
        return _ok_doc(uid, len(recompute_inputs) + 1, suffix="-async")

    result = asyncio.run(profile_store.update_profile_cas_async(uid, _recompute))

    assert result.status == "written"
    assert result.cas_attempts == 2
    assert result.recomputations == 2
    assert recompute_inputs[0] == {}
    assert recompute_inputs[1] == winner
    assert candidates[0] != candidates[1]
    assert result.document == candidates[1]


def test_newer_winner_discards_stale_candidate_without_replay(monkeypatch):
    uid = "u-profile-newer-winner"
    stale = _ok_doc(uid, 1)
    winner = profile_store.build_profile_document(
        uid,
        state="ok",
        source=_source(3, generated_at="2026-08-01T00:00:00Z"),
        last_attempt=_attempt(),
        memory_text="newer-memory",
        user_text="newer-user",
        seal_text=_seal,
    )
    reads = iter([{}, winner])
    calls = []
    monkeypatch.setattr(
        profile_store.db,
        "get_blob_strict",
        lambda *_args: next(reads),
    )
    monkeypatch.setattr(
        profile_store.db,
        "set_blob_if_unchanged",
        lambda *_args, **_kwargs: False,
    )

    result = profile_store.update_profile_cas(
        uid, lambda expected: calls.append(expected) or stale
    )

    assert result.status == "superseded"
    assert result.document == winner
    assert result.recomputations == 1
    assert calls == [{}]


def test_metadata_only_winner_cannot_supersede_successful_candidate(monkeypatch):
    uid = "u-profile-metadata-winner"
    winner = profile_store.build_profile_document(
        uid,
        state="degraded",
        source=_source(5, generated_at=""),
        last_attempt=_attempt(2, reject_code="provider_unavailable"),
        seal_text=_seal,
    )
    reads = iter([{}, winner])
    cas_calls: list[tuple[dict, dict]] = []
    cas_results = iter([False, True])
    recomputations: list[dict] = []
    monkeypatch.setattr(
        profile_store.db,
        "get_blob_strict",
        lambda *_args: next(reads),
    )

    def _cas(_uid, _kind, expected, candidate, **_kwargs):
        cas_calls.append((expected, candidate))
        return next(cas_results)

    monkeypatch.setattr(profile_store.db, "set_blob_if_unchanged", _cas)

    result = profile_store.update_profile_cas(
        uid,
        lambda expected: (
            recomputations.append(dict(expected))
            or _ok_doc(uid, 5, suffix=f"-try{len(recomputations)}")
        ),
    )

    assert result.status == "written"
    assert result.cas_attempts == 2
    assert result.recomputations == 2
    assert recomputations == [{}, winner]
    assert cas_calls[1][0] == winner
    assert cas_calls[1][1] == result.document
    assert result.document["state"] == "ok"
    assert "memory" in result.document
    # Symmetric cases intentionally remain superseding: an envelope-bearing
    # winner may replace an equivalent envelope-bearing or metadata candidate.
    assert profile_store._winner_supersedes(_ok_doc(uid, 5), _ok_doc(uid, 5))
    assert profile_store._winner_supersedes(
        _ok_doc(uid, 5),
        profile_store.build_profile_document(
            uid,
            state="degraded",
            source=_source(5),
            last_attempt=_attempt(2, reject_code="provider_unavailable"),
            seal_text=_seal,
        ),
    )


def test_second_cas_loss_fails_without_a_third_write_or_field_merge(monkeypatch):
    uid = "u-profile-double-cas-loss"
    reads = iter([{}, {}, {}])
    cas_candidates: list[dict] = []
    recomputations: list[dict] = []
    monkeypatch.setattr(
        profile_store.db,
        "get_blob_strict",
        lambda *_args: next(reads),
    )
    monkeypatch.setattr(
        profile_store.db,
        "set_blob_if_unchanged",
        lambda _uid, _kind, _expected, candidate, **_kwargs: (
            cas_candidates.append(candidate) or False
        ),
    )

    result = profile_store.update_profile_cas(
        uid,
        lambda expected: (
            recomputations.append(dict(expected))
            or _ok_doc(uid, len(recomputations), suffix="-retry")
        ),
    )

    assert result.status == "cas_failed"
    assert result.cas_attempts == 2
    assert result.recomputations == 2
    assert len(cas_candidates) == 2
    assert recomputations == [{}, {}]
    assert cas_candidates[0] is not cas_candidates[1]


def test_strict_read_failure_falls_back_to_summary_and_emits_event_and_count(
    caplog,
):
    reason = "strict_read_failed:runtimeerror"
    before = profile_store.profile_turn_fallback_counts().get(reason, 0)

    with caplog.at_level(logging.WARNING, logger="feedling.runtime_v2.profile_store"):
        selection = profile_store.select_profile_for_turn(
            "u-profile-read-failure",
            "- durable summary",
            enabled=True,
            decrypt_envelope=lambda *_args: b"must-not-run",
            read_blob=lambda *_args: (_ for _ in ()).throw(
                RuntimeError("database unavailable")
            ),
        )

    assert selection.summary == "- durable summary"
    assert selection.used_profile is False
    assert selection.fallback_reason == reason
    assert profile_store.profile_turn_fallback_counts()[reason] == before + 1
    assert "turn profile fallback" in caplog.text
    assert "database unavailable" not in caplog.text


def test_production_turn_adapter_uses_strict_read_and_observable_summary_fallback(
    monkeypatch,
):
    reason = "strict_read_failed:runtimeerror"
    before = profile_store.profile_turn_fallback_counts().get(reason, 0)
    monkeypatch.setattr(
        serve_worker.db,
        "get_blob_strict",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("pg unavailable")),
    )
    monkeypatch.setattr(
        serve_worker,
        "_mint_runtime_token",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("strict DB failure must fall back before decrypt")
        ),
    )

    selection = serve_worker._select_agent_profile_for_turn(
        "u-profile-production-read",
        "- existing encrypted summary",
        enabled=True,
    )

    assert selection.summary == "- existing encrypted summary"
    assert selection.used_profile is False
    assert selection.fallback_reason == reason
    assert profile_store.profile_turn_fallback_counts()[reason] == before + 1


def test_production_profile_decrypts_declare_both_trace_scopes(monkeypatch):
    doc = _ok_doc("u-profile-scopes", 1)
    plaintext = iter((b"memory-1", b"user-1"))
    scopes = []

    @contextmanager
    def record_scope(purpose):
        scopes.append(purpose)
        yield

    monkeypatch.setattr(serve_worker.db, "get_blob_strict", lambda *_args: doc)
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "rt")
    monkeypatch.setattr(
        serve_worker.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: next(plaintext),
    )
    monkeypatch.setattr(
        serve_worker.core_enclave,
        "coalesced_success_trace",
        record_scope,
    )

    selection = serve_worker._select_agent_profile_for_turn(
        "u-profile-scopes",
        "old summary",
        enabled=True,
    )

    assert selection.used_profile is True
    assert scopes == ["v2_profile_memory_read", "v2_profile_user_read"]


def test_ok_profile_suppresses_summary_only_after_both_fields_decrypt():
    doc = _ok_doc("u-profile-turn", 1)
    plaintext = {
        doc["memory"]["envelope"]["body_ct"]: b"memory-1",
        doc["user"]["envelope"]["body_ct"]: b"user-1",
    }
    selection = profile_store.select_profile_for_turn(
        "u-profile-turn",
        "- old summary",
        enabled=True,
        read_blob=lambda *_args: doc,
        decrypt_envelope=lambda envelope, _field: plaintext[envelope["body_ct"]],
    )

    assert selection.used_profile is True
    assert selection.summary == ""
    assert selection.memory == "memory-1"
    assert selection.user == "user-1"


def test_disabled_ok_profile_keeps_summary_without_decrypting_fields():
    doc = profile_store.build_profile_document(
        "u-profile-disabled",
        state="ok",
        source=_source(1),
        last_attempt=_attempt(),
        memory_text="memory-disabled",
        user_text="user-disabled",
        disabled=True,
        seal_text=_seal,
    )

    selection = profile_store.select_profile_for_turn(
        "u-profile-disabled",
        "- old summary",
        enabled=True,
        read_blob=lambda *_args: doc,
        decrypt_envelope=lambda *_args: (_ for _ in ()).throw(
            AssertionError("disabled profile must not decrypt")
        ),
    )

    assert selection == profile_store.ProfilePromptSelection(
        summary="- old summary",
        fallback_reason="disabled",
    )


def test_winning_cas_mirrors_only_ciphertext_to_real_tee_shadow(monkeypatch):
    uid = "u-profile-tee-shadow"
    _reset(uid)
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")

    memory_plaintext = "PRIVATE MEMORY PLAINTEXT"
    user_plaintext = "PRIVATE USER PLAINTEXT"
    document = profile_store.build_profile_document(
        uid,
        state="ok",
        source=_source(4),
        last_attempt=_attempt(),
        memory_text=memory_plaintext,
        user_text=user_plaintext,
        seal_text=_seal,
    )
    result = profile_store.update_profile_cas(uid, lambda _expected: document)
    assert result.status == "written"

    with psycopg.connect(os.environ["TEE_DATABASE_URL"]) as conn:
        row = conn.execute(
            "SELECT doc FROM user_blobs WHERE user_id=%s AND kind=%s",
            (uid, profile_store.PROFILE_BLOB_KIND),
        ).fetchone()
    assert row is not None
    shadow = dict(row[0])
    assert shadow == db.get_blob_strict(uid, profile_store.PROFILE_BLOB_KIND)
    rendered = json.dumps(shadow, ensure_ascii=False, sort_keys=True)
    assert memory_plaintext not in rendered
    assert user_plaintext not in rendered
    assert set(shadow["memory"]) == {"envelope", "chars"}
    assert set(shadow["user"]) == {"envelope", "chars"}
