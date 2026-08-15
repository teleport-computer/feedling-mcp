"""Regression: legacy→v1 memory.upgrade and the AEAD-bound card id.

The card id is part of the content AEAD AAD (owner_user_id|v|id), so the body must
be SEALED with the same id the stored record carries. Two failure modes this locks:

1. (real-deploy red line) The migration path re-sealed the v1 body with a RANDOM id,
   then the server silently rewrote envelope["id"]=memory_id (commit 92f6849). The
   stored card then had a stable id but a body sealed under a different id -> the
   enclave could never decrypt it -> readside/fetch returned unavailable. Fix: never
   rewrite the id post-seal; the server REJECTS an envelope whose id != memory_id, and
   the consumer seals with item_id=memory_id.

2. The happy path (envelope.id == memory_id) upgrades in place, keeping the id.
"""
from __future__ import annotations

import sys
import threading
import types
from contextlib import nullcontext
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from memory import actions as memory_actions  # noqa: E402


def _existing(memory_id: str) -> dict:
    return {
        "id": memory_id,
        "owner_user_id": "usr_test",
        "occurred_at": "2026-01-01T00:00:00Z",
        "created_at": "2026-01-01T00:00:00Z",
        "source": "live_conversation",
        "body_ct": "old_legacy_ciphertext",
        "nonce": "n0",
        "K_user": "ku0",
        "visibility": "shared",
        "type": "moment",
    }


def _envelope(env_id: str) -> dict:
    return {
        "id": env_id,
        "body_ct": "new_v1_ciphertext",
        "nonce": "n1",
        "K_user": "ku1",
        "K_enclave": "ke1",
        "visibility": "shared",
        "owner_user_id": "usr_test",
        "occurred_at": "2026-06-01T00:00:00Z",
    }


def _patch(monkeypatch, existing: dict, captured: dict):
    def fake_upsert(user_id, slot_id, occurred_at, doc):
        captured["slot_id"] = slot_id
        captured["doc"] = dict(doc)
        return True

    # The upgrade applier short-circuits with noop:migration_disabled unless
    # FEEDLING_MIGRATE_ENABLE is truthy (memory.migration.migration_enabled).
    # These tests exercise the post-gate behaviour (id-mismatch reject + in-place
    # write), so enable migration explicitly.
    from memory import migration as _migration  # noqa: E402
    monkeypatch.setattr(_migration, "migration_enabled", lambda: True)
    monkeypatch.setattr(memory_actions.memory_service, "_load_moments", lambda _s: [dict(existing)])
    monkeypatch.setattr(
        memory_actions.memory_service,
        "mutation_lock",
        lambda _store: nullcontext(),
    )
    monkeypatch.setattr(memory_actions.db, "memory_upsert", fake_upsert)
    monkeypatch.setattr(memory_actions.memory_service, "_append_memory_change", lambda _s, c: {"id": "chg", **c})


def test_upgrade_rejects_envelope_id_mismatch(monkeypatch):
    memory_id = "mom_original_123"
    captured: dict = {}
    _patch(monkeypatch, _existing(memory_id), captured)
    store = types.SimpleNamespace(user_id="usr_test", memory_lock=threading.Lock())

    action = {"id": memory_id, "old_body_hash": "", "envelope": _envelope("mom_BRAND_NEW_random")}
    result, _effects, status = memory_actions._memory_upgrade_envelope_action(store, action)

    assert status == 400, result
    assert result.get("error") == "envelope_id_mismatch", result
    # Crucially, NO card was written — we never persist an undecryptable card.
    assert "doc" not in captured


def test_upgrade_writes_when_envelope_id_matches(monkeypatch):
    memory_id = "mom_original_123"
    captured: dict = {}
    existing = _existing(memory_id)
    existing["occurred_at"] = "2026-01-01T08:00:00+08:00"
    existing["created_at"] = "2026-01-01T00:00:00.123456"
    _patch(monkeypatch, existing, captured)
    store = types.SimpleNamespace(user_id="usr_test", memory_lock=threading.Lock())

    action = {"id": memory_id, "old_body_hash": "", "envelope": _envelope(memory_id)}
    result, _effects, status = memory_actions._memory_upgrade_envelope_action(store, action)

    assert status == 200, result
    assert result.get("status") == "ok", result
    assert captured["slot_id"] == memory_id
    assert captured["doc"]["id"] == memory_id  # id stays
    assert captured["doc"]["body_ct"] == "new_v1_ciphertext"  # content upgraded
    # An in-place mutation must not become an implicit historical backfill.
    assert captured["doc"]["occurred_at"] == "2026-01-01T08:00:00+08:00"
    assert captured["doc"]["created_at"] == "2026-01-01T00:00:00.123456"
