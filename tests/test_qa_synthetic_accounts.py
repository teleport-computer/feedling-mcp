"""Security and retry contract for the test-only synthetic account reaper."""

from __future__ import annotations

import base64
import copy
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from accounts import registry
from admin import qa_build_identity
from admin import qa_synthetic_accounts as synthetic
from content import content_core


ADMIN_TOKEN = "qa-reaper-admin-token"
BUILD_SHA = "a" * 40


@pytest.fixture()
def qa_client(client, monkeypatch):
    monkeypatch.setenv("FEEDLING_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv(synthetic.ENABLED_ENV, "true")
    monkeypatch.setenv(synthetic.MAX_TTL_ENV, "3600")
    monkeypatch.setenv(synthetic.REAPER_INTERVAL_ENV, "30")
    monkeypatch.setenv(qa_build_identity.IMAGE_SHA_ENV, BUILD_SHA)
    monkeypatch.setenv(qa_build_identity.DEPLOY_SHA_ENV, BUILD_SHA)
    # Registration is intentionally unavailable until an elected janitor has
    # completed one full tick and persisted its cross-worker heartbeat.
    synthetic.db.set_global_blob_strict(synthetic.HEARTBEAT_KEY, {})
    with synthetic._last_run_lock:
        synthetic._last_run = None
    synthetic.reap_expired_accounts(
        now_epoch=int(time.time()), purge_archives=lambda _user_id: None
    )
    return client


def _admin_headers(token: str = ADMIN_TOKEN) -> dict[str, str]:
    return {"X-Admin-Token": token}


def _synthetic_payload(label: str = "agent-e2e-run-official-openai") -> dict:
    return {
        "public_key": base64.b64encode(b"q" * 32).decode("ascii"),
        "access_mode": "model_api",
        "archive_language": "en",
        "label": label,
        "ttl_seconds": 600,
    }


def _absence_payload(account: dict) -> dict:
    return {
        "user_id": account["user_id"],
        "lease_id": account["lease_id"],
        "absence_token": account["absence_token"],
    }


def _exists(user_id: str) -> bool:
    return registry._user_entry_snapshot(user_id) is not None


def _register_direct(label: str, *, now: int = 1_000, ttl: int = 60) -> dict:
    payload = _synthetic_payload(label)
    payload["public_key"] = ""
    payload["ttl_seconds"] = ttl
    return synthetic.register_synthetic_account(payload, now_epoch=now)


def test_admin_auth_status_and_registration_contract(qa_client):
    missing = qa_client.get("/v1/admin/qa/synthetic-account-reaper")
    assert missing.status_code == 401
    bad = qa_client.get(
        "/v1/admin/qa/synthetic-account-reaper",
        headers=_admin_headers("wrong"),
    )
    assert bad.status_code == 401

    status = qa_client.get(
        "/v1/admin/qa/synthetic-account-reaper", headers=_admin_headers()
    )
    assert status.status_code == 200
    assert status.get_json()["enabled"] is True
    assert status.get_json()["ready"] is True
    assert status.get_json()["heartbeat_fresh"] is True
    assert status.get_json()["label_prefix"] == "agent-e2e-"
    assert status.get_json()["max_ttl_seconds"] == 3600

    unauthenticated = qa_client.post(
        "/v1/admin/qa/synthetic-accounts/register", json=_synthetic_payload()
    )
    assert unauthenticated.status_code == 401

    created = qa_client.post(
        "/v1/admin/qa/synthetic-accounts/register",
        json=_synthetic_payload(),
        headers=_admin_headers(),
    )
    assert created.status_code == 201
    body = created.get_json()
    assert body["user_id"].startswith("usr_")
    assert body["api_key"]
    assert body["lease_id"].startswith("lease_")
    assert len(body["absence_token"]) == 64
    assert all(char in "0123456789abcdef" for char in body["absence_token"])
    stored = registry._user_entry_snapshot(body["user_id"])
    assert stored[synthetic.METADATA_FIELD]["kind"] == synthetic.METADATA_KIND
    assert stored[synthetic.METADATA_FIELD]["label"] == body["label"]
    assert stored[synthetic.METADATA_FIELD]["user_id"] == body["user_id"]
    assert (
        stored[synthetic.METADATA_FIELD]["api_key_hash"]
        == stored["api_keys"][0]["api_key_hash"]
    )


def test_absence_endpoint_uses_database_not_process_registry(qa_client):
    account = _register_direct("agent-e2e-authoritative-absence", now=1_000, ttl=1)
    user_id = account["user_id"]

    unauthenticated = qa_client.post(
        "/v1/admin/qa/synthetic-accounts/absence",
        json=_absence_payload(account),
    )
    assert unauthenticated.status_code == 401

    present = qa_client.post(
        "/v1/admin/qa/synthetic-accounts/absence",
        json=_absence_payload(account),
        headers=_admin_headers(),
    )
    assert present.status_code == 200
    assert present.get_json() == {
        "schema_version": 1,
        "status": "present",
        "user_id": user_id,
        "lease_id": account["lease_id"],
        "lease_attested": True,
        "database_authoritative": True,
    }

    # Model a worker that missed the best-effort users NOTIFY. The generic
    # data-track route now reports a process-local false 404 even though the
    # authoritative PostgreSQL row remains present.
    with registry._users_lock:
        registry._users[:] = [
            entry for entry in registry._users if entry.get("user_id") != user_id
        ]
        registry._rebuild_key_cache()
    stale_lookup = qa_client.get(
        f"/v1/admin/data-track/users/{user_id}", headers=_admin_headers()
    )
    assert stale_lookup.status_code == 404
    assert stale_lookup.get_json() == {"error": "user_not_found"}
    assert synthetic.db.load_user_document(user_id) is not None

    authoritative = qa_client.post(
        "/v1/admin/qa/synthetic-accounts/absence",
        json=_absence_payload(account),
        headers=_admin_headers(),
    )
    assert authoritative.status_code == 200
    assert authoritative.get_json()["status"] == "present"

    deleted = synthetic.reap_expired_accounts(
        now_epoch=1_002, purge_archives=lambda _user_id: None
    )
    assert deleted["deleted"] == 1
    absent = qa_client.post(
        "/v1/admin/qa/synthetic-accounts/absence",
        json=_absence_payload(account),
        headers=_admin_headers(),
    )
    assert absent.status_code == 200
    assert absent.get_json()["status"] == "absent"
    assert absent.get_json()["database_authoritative"] is True


def test_absence_endpoint_rejects_forgery_and_store_failure(qa_client, monkeypatch):
    account = _register_direct("agent-e2e-absence-fail-closed", now=1_000, ttl=60)
    forged = _absence_payload(account)
    token = account["absence_token"]
    forged["absence_token"] = ("0" if token[0] != "0" else "1") + token[1:]
    rejected = qa_client.post(
        "/v1/admin/qa/synthetic-accounts/absence",
        json=forged,
        headers=_admin_headers(),
    )
    assert rejected.status_code == 400
    assert rejected.get_json() == {"error": "invalid_synthetic_account_proof"}
    assert synthetic.db.load_user_document(account["user_id"]) is not None

    def unavailable(_user_id: str):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(synthetic.db, "load_user_document", unavailable)
    unavailable_response = qa_client.post(
        "/v1/admin/qa/synthetic-accounts/absence",
        json=_absence_payload(account),
        headers=_admin_headers(),
    )
    assert unavailable_response.status_code == 503
    assert unavailable_response.get_json() == {
        "error": "synthetic_account_store_unavailable"
    }


def test_protected_test_build_identity_requires_matching_full_shas(qa_client):
    missing = qa_client.get("/v1/admin/qa/build-identity")
    assert missing.status_code == 401
    wrong = qa_client.get(
        "/v1/admin/qa/build-identity", headers=_admin_headers("wrong")
    )
    assert wrong.status_code == 401

    response = qa_client.get(
        "/v1/admin/qa/build-identity", headers=_admin_headers()
    )
    assert response.status_code == 200
    assert response.get_json() == {
        "schema_version": 1,
        "environment": "test",
        "backend_sha": BUILD_SHA,
        "deployment_sha": BUILD_SHA,
        "identity_verified": True,
    }


@pytest.mark.parametrize(
    ("name", "value"),
    (
        (qa_build_identity.IMAGE_SHA_ENV, "dev"),
        (qa_build_identity.DEPLOY_SHA_ENV, "b" * 40),
        (synthetic.ENABLED_ENV, "false"),
    ),
)
def test_test_build_identity_fails_closed_when_unavailable(
    qa_client, monkeypatch, name, value
):
    monkeypatch.setenv(name, value)

    response = qa_client.get(
        "/v1/admin/qa/build-identity", headers=_admin_headers()
    )

    assert response.status_code == 503
    assert response.get_json() == {"error": "qa_build_identity_unavailable"}


def test_registration_refuses_missing_or_stale_heartbeat(qa_client):
    synthetic.db.set_global_blob_strict(synthetic.HEARTBEAT_KEY, {})
    missing = qa_client.get(
        "/v1/admin/qa/synthetic-account-reaper", headers=_admin_headers()
    )
    assert missing.status_code == 200
    assert missing.get_json()["enabled"] is True
    assert missing.get_json()["ready"] is False
    assert missing.get_json()["heartbeat_fresh"] is False
    refused = qa_client.post(
        "/v1/admin/qa/synthetic-accounts/register",
        json=_synthetic_payload(),
        headers=_admin_headers(),
    )
    assert refused.status_code == 503
    assert refused.get_json()["error"] == "synthetic_account_reaper_not_ready"

    stale = {
        "schema_version": synthetic.HEARTBEAT_SCHEMA_VERSION,
        "kind": synthetic.HEARTBEAT_KIND,
        "heartbeat_at_epoch": int(time.time()) - 91,
        "reaper_interval_seconds": 30,
        "process_id": "stale-worker",
    }
    synthetic.db.set_global_blob_strict(synthetic.HEARTBEAT_KEY, stale)
    stale_status = qa_client.get(
        "/v1/admin/qa/synthetic-account-reaper", headers=_admin_headers()
    )
    assert stale_status.get_json()["ready"] is False
    assert stale_status.get_json()["heartbeat_error"] == "heartbeat_missing_or_stale"

    synthetic.reap_expired_accounts(
        now_epoch=int(time.time()), purge_archives=lambda _user_id: None
    )
    recovered = qa_client.get(
        "/v1/admin/qa/synthetic-account-reaper", headers=_admin_headers()
    )
    assert recovered.get_json()["ready"] is True
    assert recovered.get_json()["heartbeat_fresh"] is True


def test_registration_rolls_back_primary_row_when_post_upsert_step_fails(
    qa_client, monkeypatch
):
    original_persist = registry.persist_user
    captured_user_id = ""

    def fail_after_upsert(entry):
        nonlocal captured_user_id
        captured_user_id = entry["user_id"]
        registry.db.upsert_user(entry)
        raise RuntimeError("synthetic notify failure")

    monkeypatch.setattr(registry, "persist_user", fail_after_upsert)
    with pytest.raises(RuntimeError, match="notify failure"):
        synthetic.register_synthetic_account(_synthetic_payload(), now_epoch=1_000)
    monkeypatch.setattr(registry, "persist_user", original_persist)

    assert captured_user_id
    assert registry._user_entry_snapshot(captured_user_id) is None
    assert all(
        user.get("user_id") != captured_user_id for user in registry.db.load_all_users()
    )


def test_qa_rollback_does_not_change_ordinary_registration_failure_semantics(
    qa_client, monkeypatch
):
    captured_user_id = ""

    def fail_before_upsert(entry):
        nonlocal captured_user_id
        captured_user_id = entry["user_id"]
        raise RuntimeError("ordinary persist failure")

    monkeypatch.setattr(registry, "persist_user", fail_before_upsert)
    with pytest.raises(RuntimeError, match="ordinary persist failure"):
        registry._register_user(access_mode="model_api", label="ordinary-user")

    # This intentionally matches the pre-QA behavior: the generic path is not
    # silently widened by the synthetic-account rollback policy.
    assert captured_user_id
    assert registry._user_entry_snapshot(captured_user_id) is not None
    assert registry.db.load_user_document(captured_user_id) is None
    with registry._users_lock:
        registry._users[:] = [
            entry
            for entry in registry._users
            if entry.get("user_id") != captured_user_id
        ]
        registry._rebuild_key_cache()


def test_disabled_or_invalid_config_fails_closed(qa_client, monkeypatch):
    monkeypatch.setenv(synthetic.ENABLED_ENV, "false")
    status = qa_client.get(
        "/v1/admin/qa/synthetic-account-reaper", headers=_admin_headers()
    )
    assert status.status_code == 200
    assert status.get_json()["enabled"] is False
    refused = qa_client.post(
        "/v1/admin/qa/synthetic-accounts/register",
        json=_synthetic_payload(),
        headers=_admin_headers(),
    )
    assert refused.status_code == 503

    monkeypatch.setenv(synthetic.ENABLED_ENV, "true")
    monkeypatch.setenv(synthetic.MAX_TTL_ENV, "14401")
    invalid = qa_client.get(
        "/v1/admin/qa/synthetic-account-reaper", headers=_admin_headers()
    )
    assert invalid.get_json()["enabled"] is False
    assert invalid.get_json()["config_error"].endswith("_out_of_range")


@pytest.mark.parametrize(
    "patch",
    [
        {"label": "ordinary-user"},
        {"label": "agent-e2e-"},
        {"label": "agent-e2e-bad space"},
        {"ttl_seconds": True},
        {"ttl_seconds": 0},
        {"ttl_seconds": 3601},
    ],
)
def test_registration_validates_label_and_ttl(qa_client, patch):
    payload = _synthetic_payload()
    payload.update(patch)
    response = qa_client.post(
        "/v1/admin/qa/synthetic-accounts/register",
        json=payload,
        headers=_admin_headers(),
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_synthetic_account"


def test_expiry_is_exact_and_reaper_is_idempotent(qa_client):
    expired = _register_direct("agent-e2e-expired", now=1_000, ttl=10)
    live = _register_direct("agent-e2e-live", now=1_000, ttl=100)
    public = registry._register_user(access_mode="model_api", label="ordinary-user")

    first = synthetic.reap_expired_accounts(
        now_epoch=1_011, purge_archives=lambda _user_id: None
    )
    assert first["eligible"] == 1
    assert first["deleted"] == 1
    assert first["failed"] == 0
    assert not _exists(expired["user_id"])
    assert _exists(live["user_id"])
    assert _exists(public["user_id"])

    second = synthetic.reap_expired_accounts(
        now_epoch=1_011, purge_archives=lambda _user_id: None
    )
    assert second["eligible"] == 0
    assert second["deleted"] == 0


def test_reaper_finds_db_account_missing_from_worker_registry_cache(qa_client):
    account = _register_direct("agent-e2e-missed-notify", now=1_000, ttl=1)
    user_id = account["user_id"]

    # Model the elected worker missing the best-effort users NOTIFY: Postgres
    # has the signed account, but this process's registry/key caches do not.
    with registry._users_lock:
        registry._users[:] = [
            entry for entry in registry._users if entry.get("user_id") != user_id
        ]
        registry._rebuild_key_cache()
    assert registry._user_entry_snapshot(user_id) is None
    assert registry.db.load_user_document(user_id) is not None

    result = synthetic.reap_expired_accounts(
        now_epoch=1_002, purge_archives=lambda _user_id: None
    )

    assert result["eligible"] == 1
    assert result["deleted"] == 1
    assert registry.db.load_user_document(user_id) is None


def test_public_prefix_and_forged_metadata_are_never_reaped(qa_client):
    public_response = qa_client.post(
        "/v1/users/register",
        json={
            "access_mode": "model_api",
            "label": "agent-e2e-public-forgery",
            # Public registration must ignore this entire caller-chosen object.
            synthetic.METADATA_FIELD: {
                "kind": synthetic.METADATA_KIND,
                "expires_at_epoch": 1,
            },
        },
    )
    assert public_response.status_code == 201
    user_id = public_response.get_json()["user_id"]
    assert synthetic.METADATA_FIELD not in registry._user_entry_snapshot(user_id)

    # Even a plausible-looking row-level forgery lacks the server signature and
    # must be preserved.  This models accidental/manual DB document pollution.
    with registry._users_lock:
        entry = registry._find_user_entry_locked(user_id)
        entry[synthetic.METADATA_FIELD] = {
            "kind": synthetic.METADATA_KIND,
            "label_prefix": synthetic.LABEL_PREFIX,
            "label": "agent-e2e-public-forgery",
            "lease_id": "lease_" + "0" * 32,
            "created_at_epoch": 1,
            "expires_at_epoch": 2,
            "signature": "0" * 64,
        }
        registry.persist_user(entry)

    result = synthetic.reap_expired_accounts(
        now_epoch=10, purge_archives=lambda _user_id: None
    )
    assert result["eligible"] == 0
    assert _exists(user_id)


def test_valid_lease_cannot_be_replayed_onto_another_same_label_account(qa_client):
    label = "agent-e2e-replay-proof"
    source = _register_direct(label, now=1_000, ttl=1)
    ordinary = registry._register_user(access_mode="model_api", label=label)

    source_entry = registry._user_entry_snapshot(source["user_id"])
    with registry._users_lock:
        target_entry = registry._find_user_entry_locked(ordinary["user_id"])
        target_entry[synthetic.METADATA_FIELD] = copy.deepcopy(
            source_entry[synthetic.METADATA_FIELD]
        )
        registry.persist_user(target_entry)

    result = synthetic.reap_expired_accounts(
        now_epoch=1_002, purge_archives=lambda _user_id: None
    )

    assert result["eligible"] == 1
    assert result["deleted"] == 1
    assert not _exists(source["user_id"])
    assert _exists(ordinary["user_id"])


def test_malformed_json_user_row_cannot_poison_reaper_tick(qa_client):
    malformed_user_id = "usr_" + "f" * 16
    with synthetic.db.get_pool().connection() as connection:
        connection.execute(
            "INSERT INTO users (user_id, created_at, doc) VALUES (%s, %s, %s::jsonb)",
            (
                malformed_user_id,
                "2000-01-01T00:00:00+00:00",
                '["qa_synthetic_account"]',
            ),
        )
    account = _register_direct("agent-e2e-after-poison", now=1_000, ttl=1)

    result = synthetic.reap_expired_accounts(
        now_epoch=1_002, purge_archives=lambda _user_id: None
    )

    assert result["scanned"] == 1
    assert result["deleted"] == 1
    assert not _exists(account["user_id"])
    assert synthetic.db.load_user_document(malformed_user_id) == [
        synthetic.METADATA_FIELD
    ]
    synthetic.db.delete_user(malformed_user_id)


def test_signed_expiry_cannot_be_tampered_into_eligibility(qa_client):
    account = _register_direct("agent-e2e-expiry-signature", now=1_000, ttl=100)
    with registry._users_lock:
        entry = registry._find_user_entry_locked(account["user_id"])
        # Make the lease appear expired without the server-side signing key.
        # The original signature must no longer validate.
        entry[synthetic.METADATA_FIELD]["expires_at_epoch"] = 1_001
        registry.persist_user(entry)

    result = synthetic.reap_expired_accounts(
        now_epoch=1_002, purge_archives=lambda _user_id: None
    )
    assert result["eligible"] == 0
    assert _exists(account["user_id"])


def test_archive_purge_failure_remains_retryable(qa_client):
    account = _register_direct("agent-e2e-purge-retry", now=1_000, ttl=1)
    calls = 0

    def flaky_purge(_user_id: str):
        nonlocal calls
        calls += 1
        return RuntimeError("r2 unavailable") if calls == 1 else None

    first = synthetic.reap_expired_accounts(now_epoch=1_002, purge_archives=flaky_purge)
    assert first["failed"] == 1
    assert first["deleted"] == 0
    assert _exists(account["user_id"])
    failed_status = synthetic.status_payload()
    assert failed_status["ready"] is False
    assert failed_status["heartbeat_fresh"] is False
    assert failed_status["heartbeat_error"] == "last_tick_failed"

    second = synthetic.reap_expired_accounts(
        now_epoch=1_002, purge_archives=flaky_purge
    )
    assert second["failed"] == 0
    assert second["deleted"] == 1
    assert not _exists(account["user_id"])
    assert synthetic.status_payload()["ready"] is True


def test_delete_exception_restores_account_for_retry(qa_client, monkeypatch):
    account = _register_direct("agent-e2e-delete-retry", now=1_000, ttl=1)
    original_delete = content_core.db.delete_user
    calls = 0

    def flaky_delete(user_id: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database unavailable")
        return original_delete(user_id)

    monkeypatch.setattr(content_core.db, "delete_user", flaky_delete)
    first = synthetic.reap_expired_accounts(
        now_epoch=1_002, purge_archives=lambda _user_id: None
    )
    assert first["failed"] == 1
    assert _exists(account["user_id"])

    second = synthetic.reap_expired_accounts(
        now_epoch=1_002, purge_archives=lambda _user_id: None
    )
    assert second["deleted"] == 1
    assert not _exists(account["user_id"])
