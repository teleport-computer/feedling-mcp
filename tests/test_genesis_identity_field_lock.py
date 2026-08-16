from __future__ import annotations

import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import debug_trace  # noqa: E402
from genesis import service as genesis_service  # noqa: E402
from identity import service as identity_service  # noqa: E402


def _store():
    return types.SimpleNamespace(user_id="u-lock")


def test_change_log_records_canonical_fields_and_empty_log_gets_time_fence(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr(identity_service.db, "log_append", lambda _u, _k, row: rows.append(row))
    monkeypatch.setattr(identity_service.db, "log_read_all", lambda *_args: [])

    row = identity_service._append_identity_change(_store(), {
        "action": "profile_patch",
        "fields": ["agent_name", "not_a_field"],
    })
    anchor = identity_service.identity_change_anchor(_store())

    assert row["fields"] == ["agent_name"]
    assert rows[0]["fields"] == ["agent_name"]
    assert anchor["ts"]
    assert anchor["id"] == ""


def test_legacy_post_anchor_change_locks_whole_card(monkeypatch):
    monkeypatch.setattr(identity_service.db, "log_read_all", lambda *_args: [{
        "id": "legacy",
        "ts": "2026-08-16T10:00:01",
        "action": "replace",
    }])

    lock = identity_service.identity_fields_changed_since(
        _store(),
        since="2026-08-16T10:00:00",
    )

    assert lock["outcome"] == "whole_card_fail_safe"
    assert lock["fields"] == list(identity_service.IDENTITY_CHANGE_FIELDS)


def test_field_lock_uses_server_job_anchor_and_emits_content_free_trace(monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(genesis_service.db, "genesis_get_job", lambda *_args: {
        "metadata": {"identity_change_anchor_ts": "2026-08-16T10:00:00"},
    })
    monkeypatch.setattr(identity_service.db, "log_read_all", lambda *_args: [{
        "id": "c1",
        "ts": "2026-08-16T10:00:01",
        "action": "profile_patch",
        "fields": ["agent_name"],
        "reason": "secret user prose",
    }])
    monkeypatch.setattr(debug_trace, "trace_event", lambda *_args, **kwargs: events.append(kwargs))

    lock = genesis_service.identity_field_lock_for_job(_store(), "j1")

    assert lock == {"outcome": "per_field", "fields": ["agent_name"], "change_count": 1}
    assert events[0]["type"] == "genesis.field_lock"
    assert events[0]["detail"] == {
        "outcome": "per_field",
        "change_count": 1,
        "locked_field_count": 1,
        "anchor_present": True,
    }
    assert "secret user prose" not in repr(events[0])


def test_locked_identity_fields_are_not_overlaid():
    merged = genesis_service._merge_identity_replace_payload(
        {
            "agent_name": "user-won-name",
            "self_introduction": "old intro",
            "dimensions": [{"name": "Warm", "value": 50, "description": "old"}],
        },
        {
            "agent_name": "stale-distill-name",
            "self_introduction": "new intro",
            "dimensions": [{"name": "Warm", "value": 90, "description": "stale"}],
        },
        locked_fields=("agent_name", "dimensions"),
    )

    assert merged["agent_name"] == "user-won-name"
    assert merged["dimensions"][0]["value"] == 50
    assert merged["self_introduction"] == "new intro"


def test_whole_card_fail_safe_returns_locked_without_write(monkeypatch):
    monkeypatch.setattr(identity_service, "_load_identity", lambda _store: {"id": "identity"})
    monkeypatch.setattr(
        genesis_service,
        "_existing_identity_plain_for_update",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not decrypt")),
    )

    status = genesis_service.replace_identity_preserving_anchor(
        _store(),
        {"identity": {"agent_name": "stale", "dimensions": []}},
        "key",
        field_lock={
            "outcome": "whole_card_fail_safe",
            "fields": list(identity_service.IDENTITY_CHANGE_FIELDS),
        },
    )

    assert status == "locked"


def test_final_identity_write_applies_only_unlocked_fields(monkeypatch):
    snapshot = {
        "id": "identity",
        "created_at": "old-created",
        "replaced_at": "old-replaced",
        "relationship_started_at": "2020-01-01",
        "relationship_anchor_source": "user_calibrated",
        "relationship_anchor_evidence": "user set it",
    }
    existing_plain = {
        "agent_name": "user-won-name",
        "self_introduction": "old intro",
        "dimensions": [{"name": "Warm", "value": 50, "description": "old"}],
    }
    encrypted_plaintexts: list[dict] = []
    saved: list[dict] = []
    audits: list[dict] = []
    monkeypatch.setattr(identity_service, "_load_identity", lambda _store: dict(snapshot))
    monkeypatch.setattr(
        genesis_service,
        "_existing_identity_plain_for_update",
        lambda *_args, **_kwargs: (dict(existing_plain), ""),
    )

    def fake_envelope(_store, raw, **_kwargs):
        encrypted_plaintexts.append(json.loads(raw.decode("utf-8")))
        return ({
            "id": "identity",
            "body_ct": "ct",
            "nonce": "nonce",
            "K_user": "ku",
            "K_enclave": "ke",
            "visibility": "shared",
            "owner_user_id": "u-lock",
        }, "")

    monkeypatch.setattr(genesis_service.core_envelope, "_build_shared_envelope_for_store", fake_envelope)
    monkeypatch.setattr(
        identity_service,
        "_save_identity_cas",
        lambda _store, _expected, document: saved.append(document) or True,
    )
    monkeypatch.setattr(identity_service, "_append_identity_change", lambda _store, audit: audits.append(audit))
    monkeypatch.setattr(genesis_service.boot_gates, "_log_bootstrap_event", lambda *_args, **_kwargs: None)

    status = genesis_service.replace_identity_preserving_anchor(
        _store(),
        {
            "identity": {
                "agent_name": "stale-distill-name",
                "self_introduction": "new intro",
                "dimensions": [{"name": "Warm", "value": 90, "description": "stale"}],
            },
            "relationship_anchor": {
                "relationship_started_at": "2026-01-01",
                "relationship_anchor_evidence": "stale upload",
            },
        },
        "key",
        field_lock={
            "outcome": "per_field",
            "fields": ["agent_name", "dimensions", "days_with_user"],
        },
    )

    assert status == "updated"
    assert encrypted_plaintexts[0]["agent_name"] == "user-won-name"
    assert encrypted_plaintexts[0]["dimensions"][0]["value"] == 50
    assert encrypted_plaintexts[0]["self_introduction"] == "new intro"
    assert saved[0]["relationship_started_at"] == "2020-01-01"
    assert audits[0]["fields"] == ["self_introduction"]
