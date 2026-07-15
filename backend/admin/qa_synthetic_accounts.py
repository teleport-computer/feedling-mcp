"""Short-lived synthetic accounts for the deployed API-key QA suite.

This surface is deliberately fail-closed and test-only.  A public registration
label is never enough to make an account eligible for deletion: the admin-only
registration path writes a server-signed lease into the user document at the
same time the account is first persisted, and the reaper verifies that lease,
the authoritative row/user identity, the exact minted key, and the expiry
before calling the normal account-reset path. Registration also requires a
fresh, healthy janitor heartbeat persisted for cross-worker verification.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Callable

import db
from accounts import registry
from content import content_core
from core import store as core_store

log = logging.getLogger("feedling.qa_synthetic_accounts")

ENABLED_ENV = "FEEDLING_QA_SYNTHETIC_ACCOUNTS_ENABLED"
MAX_TTL_ENV = "FEEDLING_QA_SYNTHETIC_ACCOUNT_MAX_TTL_SECONDS"
REAPER_INTERVAL_ENV = "FEEDLING_QA_SYNTHETIC_REAPER_INTERVAL_SECONDS"

LABEL_PREFIX = "agent-e2e-"
METADATA_FIELD = "qa_synthetic_account"
METADATA_KIND = "agent_e2e"
HEARTBEAT_KEY = "qa_synthetic_account_reaper_heartbeat_v1"
HEARTBEAT_KIND = "qa_synthetic_account_reaper"
HEARTBEAT_SCHEMA_VERSION = 1
MAX_ALLOWED_TTL_SECONDS = 14_400
DEFAULT_MAX_TTL_SECONDS = MAX_ALLOWED_TTL_SECONDS
DEFAULT_REAPER_INTERVAL_SECONDS = 60
MIN_HEARTBEAT_MAX_AGE_SECONDS = 30
HEARTBEAT_INTERVAL_MULTIPLIER = 3
MAX_RUN_ID_LENGTH = 48
RUN_CLEANUP_KIND = "qa_synthetic_run_cleanup"
RUN_CLOSED_KIND = "qa_synthetic_run_closed"
RUN_CLOSED_KEY_PREFIX = "qa_synthetic_run_closed_v1:"

_LEASE_RE = re.compile(r"^lease_[0-9a-f]{32}$")
_LABEL_RE = re.compile(r"^agent-e2e-[A-Za-z0-9_.-]{1,112}$")
_USER_ID_RE = re.compile(r"^usr_[0-9a-f]{16}$")
_PRINCIPAL_ID_RE = re.compile(r"^prn_[0-9a-f]{16}$")
_API_KEY_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ABSENCE_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(rf"^[A-Za-z0-9_.-]{{1,{MAX_RUN_ID_LENGTH}}}$")
_SIGNING_CONTEXT = b"feedling-qa-synthetic-account-v1\0"
_ABSENCE_TOKEN_CONTEXT = b"feedling-qa-synthetic-account-absence-v1\0"
_PROCESS_ID = secrets.token_hex(8)

_last_run_lock = threading.Lock()
_last_run: dict | None = None


class SyntheticAccountDisabled(RuntimeError):
    pass


class SyntheticAccountNotReady(RuntimeError):
    pass


class SyntheticAccountBadRequest(ValueError):
    pass


class SyntheticAccountStoreUnavailable(RuntimeError):
    pass


class SyntheticAccountRunClosed(RuntimeError):
    pass


def _env_enabled() -> bool:
    return (os.environ.get(ENABLED_ENV) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _bounded_int(
    name: str, default: int, *, minimum: int, maximum: int
) -> tuple[int, str]:
    raw = (os.environ.get(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default, f"{name}_invalid"
    if not minimum <= value <= maximum:
        return default, f"{name}_out_of_range"
    return value, ""


def config() -> dict:
    max_ttl, ttl_error = _bounded_int(
        MAX_TTL_ENV,
        DEFAULT_MAX_TTL_SECONDS,
        minimum=1,
        maximum=MAX_ALLOWED_TTL_SECONDS,
    )
    interval, interval_error = _bounded_int(
        REAPER_INTERVAL_ENV,
        DEFAULT_REAPER_INTERVAL_SECONDS,
        minimum=5,
        maximum=600,
    )
    error = ttl_error or interval_error
    requested = _env_enabled()
    return {
        "enabled": requested and not error,
        "requested": requested,
        "label_prefix": LABEL_PREFIX,
        "max_ttl_seconds": max_ttl,
        "reaper_interval_seconds": interval,
        "config_error": error,
    }


def _heartbeat_max_age_seconds(cfg: dict) -> int:
    return max(
        MIN_HEARTBEAT_MAX_AGE_SECONDS,
        int(cfg["reaper_interval_seconds"]) * HEARTBEAT_INTERVAL_MULTIPLIER,
    )


def _heartbeat_is_fresh(value, *, cfg: dict, now_epoch: int) -> bool:
    if not isinstance(value, dict):
        return False
    heartbeat_at = value.get("heartbeat_at_epoch")
    return bool(
        value.get("schema_version") == HEARTBEAT_SCHEMA_VERSION
        and value.get("kind") == HEARTBEAT_KIND
        and value.get("healthy") is True
        and type(heartbeat_at) is int
        and type(value.get("reaper_interval_seconds")) is int
        and value["reaper_interval_seconds"] == cfg["reaper_interval_seconds"]
        and 0 <= now_epoch - heartbeat_at <= _heartbeat_max_age_seconds(cfg)
    )


def _operational_status(*, cfg: dict, now_epoch: int | None = None) -> dict:
    now = int(time.time() if now_epoch is None else now_epoch)
    heartbeat = None
    heartbeat_error = ""
    if cfg["enabled"]:
        try:
            heartbeat = db.get_global_blob_strict(HEARTBEAT_KEY)
        except Exception:
            heartbeat_error = "heartbeat_store_unavailable"
    fresh = _heartbeat_is_fresh(heartbeat, cfg=cfg, now_epoch=now)
    if cfg["enabled"] and not heartbeat_error and heartbeat is not None and not fresh:
        heartbeat_error = (
            "last_tick_failed"
            if isinstance(heartbeat, dict) and heartbeat.get("healthy") is False
            else "heartbeat_missing_or_stale"
        )
    return {
        "ready": bool(cfg["enabled"] and fresh),
        "heartbeat_fresh": fresh,
        "heartbeat_max_age_seconds": _heartbeat_max_age_seconds(cfg),
        "heartbeat": dict(heartbeat) if isinstance(heartbeat, dict) else None,
        "heartbeat_error": heartbeat_error,
    }


def status_payload(*, now_epoch: int | None = None) -> dict:
    cfg = config()
    operational = _operational_status(cfg=cfg, now_epoch=now_epoch)
    with _last_run_lock:
        last = dict(_last_run) if _last_run else None
    payload = {
        "enabled": cfg["enabled"],
        "ready": operational["ready"],
        "label_prefix": LABEL_PREFIX,
        "max_ttl_seconds": cfg["max_ttl_seconds"],
        "reaper_interval_seconds": cfg["reaper_interval_seconds"],
        "metadata_kind": METADATA_KIND,
        "heartbeat_fresh": operational["heartbeat_fresh"],
        "heartbeat_max_age_seconds": operational["heartbeat_max_age_seconds"],
        "heartbeat": operational["heartbeat"],
        "last_run": last,
    }
    if cfg["config_error"]:
        payload["config_error"] = cfg["config_error"]
    if operational["heartbeat_error"]:
        payload["heartbeat_error"] = operational["heartbeat_error"]
    return payload


def _signing_key() -> bytes:
    # The account-key pepper is random, server-owned, and persisted in
    # server_config.  Derive a domain-separated sub-key so a user cannot forge
    # a lease even if they can choose every public registration field.
    return hmac.new(registry._pepper(), _SIGNING_CONTEXT, hashlib.sha256).digest()


def _signature_payload(metadata: dict) -> bytes:
    signed = {
        "api_key_hash": metadata.get("api_key_hash"),
        "created_at_epoch": metadata.get("created_at_epoch"),
        "expires_at_epoch": metadata.get("expires_at_epoch"),
        "kind": metadata.get("kind"),
        "key_id": metadata.get("key_id"),
        "label": metadata.get("label"),
        "label_prefix": metadata.get("label_prefix"),
        "lease_id": metadata.get("lease_id"),
        "principal_id": metadata.get("principal_id"),
        "user_id": metadata.get("user_id"),
    }
    return json.dumps(signed, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sign_metadata(metadata: dict) -> str:
    return hmac.new(
        _signing_key(), _signature_payload(metadata), hashlib.sha256
    ).hexdigest()


def _absence_token(*, user_id: str, lease_id: str) -> str:
    """Mint a read-only attestation binding one synthetic identity and lease.

    This token never authorizes deletion.  It lets the cleanup verifier prove
    that an absence query refers to a lease actually minted by this server even
    after the authoritative ``users`` row (and its signed metadata) is gone.
    A separate signing context prevents reuse as reaper metadata.
    """
    payload = json.dumps(
        {"lease_id": lease_id, "user_id": user_id},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(
        registry._pepper(), _ABSENCE_TOKEN_CONTEXT + payload, hashlib.sha256
    ).hexdigest()


def _new_metadata(
    *,
    label: str,
    now_epoch: int,
    ttl_seconds: int,
    user_id: str,
    principal_id: str,
    key_id: str,
    api_key_hash: str,
) -> dict:
    expires_at = now_epoch + ttl_seconds
    metadata = {
        "kind": METADATA_KIND,
        "label_prefix": LABEL_PREFIX,
        "label": label,
        "user_id": user_id,
        "principal_id": principal_id,
        "key_id": key_id,
        "api_key_hash": api_key_hash,
        "lease_id": f"lease_{secrets.token_hex(16)}",
        "created_at_epoch": now_epoch,
        "expires_at_epoch": expires_at,
        "created_at": datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat(),
        "expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
    }
    metadata["signature"] = _sign_metadata(metadata)
    return metadata


def _parse_ttl(value, *, maximum: int) -> int:
    if value is None or value == "":
        return maximum
    if isinstance(value, bool):
        raise SyntheticAccountBadRequest("ttl_seconds must be an integer")
    try:
        ttl = int(value)
    except (TypeError, ValueError) as exc:
        raise SyntheticAccountBadRequest("ttl_seconds must be an integer") from exc
    if not 1 <= ttl <= maximum:
        raise SyntheticAccountBadRequest(f"ttl_seconds must be between 1 and {maximum}")
    return ttl


def register_synthetic_account(payload: dict, *, now_epoch: int | None = None) -> dict:
    cfg = config()
    if not cfg["enabled"]:
        raise SyntheticAccountDisabled("synthetic account registration is disabled")
    if not _operational_status(cfg=cfg)["ready"]:
        raise SyntheticAccountNotReady("synthetic account reaper is not ready")
    if not isinstance(payload, dict):
        raise SyntheticAccountBadRequest("JSON object required")

    run_id = _cleanup_run_id(payload)
    label = str(payload.get("label") or "").strip()
    if not _LABEL_RE.fullmatch(label):
        raise SyntheticAccountBadRequest(
            f"label must start with {LABEL_PREFIX} and contain only safe identifier characters"
        )
    if not label.startswith(f"{LABEL_PREFIX}{run_id}-"):
        raise SyntheticAccountBadRequest("label does not match the normalized run_id")
    ttl = _parse_ttl(payload.get("ttl_seconds"), maximum=cfg["max_ttl_seconds"])
    now = int(time.time() if now_epoch is None else now_epoch)

    metadata_holder: dict[str, dict] = {}

    def build_metadata(entry: dict) -> dict:
        primary = (entry.get("api_keys") or [None])[0]
        if not isinstance(primary, dict):
            raise RuntimeError("synthetic account primary key is missing")
        metadata = _new_metadata(
            label=label,
            now_epoch=now,
            ttl_seconds=ttl,
            user_id=str(entry.get("user_id") or ""),
            principal_id=str(entry.get("principal_id") or ""),
            key_id=str(primary.get("key_id") or ""),
            api_key_hash=str(primary.get("api_key_hash") or ""),
        )
        metadata_holder["value"] = metadata
        return metadata

    with _serialized_run(run_id):
        if _run_is_closed(run_id):
            raise SyntheticAccountRunClosed("synthetic account run is closed")
        result = registry._register_user(
            public_key=str(payload.get("public_key") or "").strip() or None,
            archive_language=str(payload.get("archive_language") or "").strip()
            or None,
            access_mode=str(payload.get("access_mode") or "model_api"),
            label=label,
            _qa_synthetic_metadata_builder=build_metadata,
        )
    metadata = metadata_holder["value"]
    # Return only the receipt fields the deterministic provisioner needs. The
    # destructive reaper signature and account-key pepper never cross the
    # boundary; ``absence_token`` is separately domain-separated and can only
    # attest the identity of a subsequent read-only absence query.
    return {
        **result,
        "label": label,
        "lease_id": metadata["lease_id"],
        "absence_token": _absence_token(
            user_id=metadata["user_id"], lease_id=metadata["lease_id"]
        ),
        "expires_at": metadata["expires_at"],
        "expires_at_epoch": metadata["expires_at_epoch"],
    }


def synthetic_account_absence(payload: dict) -> dict:
    """Authoritatively report whether one attested synthetic row is absent.

    The ordinary data-track lookup reads a process-local registry snapshot and
    can be stale after a cross-worker notification is missed.  Cleanup evidence
    must instead use this strict PostgreSQL read, which raises on store failure
    and therefore never turns an outage into a false absence.
    """
    if not config()["enabled"]:
        raise SyntheticAccountDisabled("synthetic account verification is disabled")
    if not isinstance(payload, dict):
        raise SyntheticAccountBadRequest("JSON object required")

    user_id = str(payload.get("user_id") or "").strip()
    lease_id = str(payload.get("lease_id") or "").strip()
    absence_token = str(payload.get("absence_token") or "").strip()
    if _USER_ID_RE.fullmatch(user_id) is None:
        raise SyntheticAccountBadRequest("user_id is invalid")
    if _LEASE_RE.fullmatch(lease_id) is None:
        raise SyntheticAccountBadRequest("lease_id is invalid")
    if _ABSENCE_TOKEN_RE.fullmatch(absence_token) is None:
        raise SyntheticAccountBadRequest("absence_token is invalid")
    expected = _absence_token(user_id=user_id, lease_id=lease_id)
    if not hmac.compare_digest(absence_token, expected):
        raise SyntheticAccountBadRequest("absence_token is invalid")

    try:
        entry = db.load_user_document(user_id)
    except Exception as exc:
        raise SyntheticAccountStoreUnavailable(
            "synthetic account store is unavailable"
        ) from exc

    base = {
        "schema_version": 1,
        "user_id": user_id,
        "lease_id": lease_id,
        "lease_attested": True,
        "database_authoritative": True,
    }
    if entry is None:
        return {**base, "status": "absent"}

    metadata = _valid_metadata(entry, authoritative_user_id=user_id)
    if not metadata or metadata.get("lease_id") != lease_id:
        raise SyntheticAccountBadRequest("synthetic account identity mismatch")
    return {**base, "status": "present"}


def _active_key_matches_lease(entry: dict, metadata: dict) -> bool:
    return any(
        isinstance(key, dict)
        and not key.get("revoked_at")
        and str(key.get("label") or "") == metadata.get("label")
        and str(key.get("key_id") or "") == metadata.get("key_id")
        and str(key.get("api_key_hash") or "") == metadata.get("api_key_hash")
        for key in (entry.get("api_keys") or [])
    )


def _valid_metadata(
    entry: dict,
    *,
    authoritative_user_id: str,
    require_active_key: bool = True,
) -> dict | None:
    if not isinstance(entry, dict):
        return None
    metadata = entry.get(METADATA_FIELD)
    if not isinstance(metadata, dict):
        return None
    label = metadata.get("label")
    lease_id = metadata.get("lease_id")
    user_id = metadata.get("user_id")
    principal_id = metadata.get("principal_id")
    key_id = metadata.get("key_id")
    api_key_hash = metadata.get("api_key_hash")
    created = metadata.get("created_at_epoch")
    expires = metadata.get("expires_at_epoch")
    signature = metadata.get("signature")
    if (
        metadata.get("kind") != METADATA_KIND
        or metadata.get("label_prefix") != LABEL_PREFIX
        or not isinstance(authoritative_user_id, str)
        or _USER_ID_RE.fullmatch(authoritative_user_id) is None
        or entry.get("user_id") != authoritative_user_id
        or user_id != authoritative_user_id
        or not isinstance(principal_id, str)
        or _PRINCIPAL_ID_RE.fullmatch(principal_id) is None
        or entry.get("principal_id") != principal_id
        or not isinstance(key_id, str)
        or not key_id
        or not isinstance(api_key_hash, str)
        or _API_KEY_HASH_RE.fullmatch(api_key_hash) is None
        or not isinstance(label, str)
        or not _LABEL_RE.fullmatch(label)
        or not isinstance(lease_id, str)
        or not _LEASE_RE.fullmatch(lease_id)
        or isinstance(created, bool)
        or not isinstance(created, int)
        or isinstance(expires, bool)
        or not isinstance(expires, int)
        or not 1 <= expires - created <= MAX_ALLOWED_TTL_SECONDS
        or not isinstance(signature, str)
        or len(signature) != 64
        or (require_active_key and not _active_key_matches_lease(entry, metadata))
    ):
        return None
    expected = _sign_metadata(metadata)
    if not hmac.compare_digest(signature, expected):
        return None
    return metadata


def _cleanup_run_id(payload: dict) -> str:
    if not isinstance(payload, dict):
        raise SyntheticAccountBadRequest("JSON object required")
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
        raise SyntheticAccountBadRequest("run_id must be normalized")
    return run_id


def _run_closed_key(run_id: str) -> str:
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    return f"{RUN_CLOSED_KEY_PREFIX}{digest}"


def _run_lock_key(run_id: str) -> int:
    payload = f"feedling-qa-synthetic-run-v1\0{run_id}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big", signed=True)


@contextmanager
def _serialized_run(run_id: str):
    """Serialize registration and terminal cleanup across backend workers."""
    connection = None
    try:
        connection = db.listen_connection()
        connection.execute("SELECT pg_advisory_lock(%s)", (_run_lock_key(run_id),))
    except Exception as exc:
        raise SyntheticAccountStoreUnavailable(
            "synthetic account store is unavailable"
        ) from exc
    try:
        yield
    finally:
        if connection is not None:
            try:
                connection.execute(
                    "SELECT pg_advisory_unlock(%s)", (_run_lock_key(run_id),)
                )
            except Exception:
                # Closing the session releases a session-level advisory lock.
                log.exception("[qa-run] advisory unlock failed")
            finally:
                try:
                    connection.close()
                except Exception:
                    pass


def _run_is_closed(run_id: str) -> bool:
    try:
        marker = db.get_global_blob_strict(_run_closed_key(run_id))
    except Exception as exc:
        raise SyntheticAccountStoreUnavailable(
            "synthetic account store is unavailable"
        ) from exc
    if marker is None:
        return False
    expected_hash = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    if not isinstance(marker, dict) or marker != {
        "schema_version": 1,
        "kind": RUN_CLOSED_KIND,
        "run_id_sha256": expected_hash,
    }:
        # A malformed marker must close the run rather than allow a late account.
        raise SyntheticAccountRunClosed("synthetic account run is closed")
    return True


def _close_run(run_id: str) -> None:
    try:
        db.set_global_blob_strict(
            _run_closed_key(run_id),
            {
                "schema_version": 1,
                "kind": RUN_CLOSED_KIND,
                "run_id_sha256": hashlib.sha256(run_id.encode("utf-8")).hexdigest(),
            },
        )
    except Exception as exc:
        raise SyntheticAccountStoreUnavailable(
            "synthetic account store is unavailable"
        ) from exc


def _run_metadata(
    entry: dict,
    *,
    authoritative_user_id: str,
    label_prefix: str,
) -> dict | None:
    """Return one server-signed lease belonging to an exact QA run prefix.

    Run cleanup deliberately does not require the leased API key to remain
    active. A prior, interrupted cleanup may already have revoked that key;
    the server-owned HMAC still binds the row identity, principal, key hash,
    lease, and label strongly enough to authorize deletion of that synthetic
    row. Ordinary accounts that merely copy the public label prefix remain
    ineligible.
    """
    metadata = _valid_metadata(
        entry,
        authoritative_user_id=authoritative_user_id,
        require_active_key=False,
    )
    if not metadata or not str(metadata.get("label") or "").startswith(label_prefix):
        return None
    return metadata


def _raw_run_label_matches(entry: dict, *, label_prefix: str) -> bool:
    """Detect a claimed run before trusting any destructive metadata."""
    if not isinstance(entry, dict):
        return False
    metadata = entry.get(METADATA_FIELD)
    return bool(
        isinstance(metadata, dict)
        and isinstance(metadata.get("label"), str)
        and metadata["label"].startswith(label_prefix)
    )


def _cleanup_synthetic_run_locked(
    payload: dict,
    *,
    purge_archives: Callable[[str], Exception | None] | None = None,
) -> dict:
    """Delete every signed synthetic account for one normalized QA run.

    This is the manifest-independent recovery path for a registration whose
    response was lost before the client could checkpoint its credentials. The
    users table is scanned authoritatively, every candidate is reloaded and
    signature-checked immediately before reset, and the authoritative table is
    scanned again before returning. The receipt contains aggregates only, so it
    is safe to retain as CI evidence and safe to retry after any partial run.
    """
    run_id = _cleanup_run_id(payload)
    label_prefix = f"{LABEL_PREFIX}{run_id}-"
    purge = purge_archives or content_core._purge_onboarding_archives_with_retry

    try:
        scanned = db.load_user_documents_with_field(METADATA_FIELD)
    except Exception as exc:
        raise SyntheticAccountStoreUnavailable(
            "synthetic account store is unavailable"
        ) from exc

    candidates: list[tuple[str, str]] = []
    invalid_initial_count = 0
    for authoritative_user_id, entry in scanned:
        if not _raw_run_label_matches(entry, label_prefix=label_prefix):
            continue
        metadata = _run_metadata(
            entry,
            authoritative_user_id=authoritative_user_id,
            label_prefix=label_prefix,
        )
        if metadata is not None:
            candidates.append((authoritative_user_id, str(metadata["signature"])))
        else:
            # Never delete from an untrusted label alone, but also never report
            # a clean run while a row claiming that exact run has malformed or
            # tampered server metadata.
            invalid_initial_count += 1

    deleted_count = 0
    already_absent_count = 0
    operation_failure_count = invalid_initial_count
    for authoritative_user_id, scanned_signature in candidates:
        try:
            live = db.load_user_document(authoritative_user_id)
        except Exception as exc:
            raise SyntheticAccountStoreUnavailable(
                "synthetic account store is unavailable"
            ) from exc
        if live is None:
            already_absent_count += 1
            continue
        metadata = _run_metadata(
            live,
            authoritative_user_id=authoritative_user_id,
            label_prefix=label_prefix,
        )
        if metadata is None or not hmac.compare_digest(
            str(metadata.get("signature") or ""), scanned_signature
        ):
            operation_failure_count += 1
            continue
        try:
            body, status = content_core.account_reset(
                core_store.get_store(authoritative_user_id),
                {"confirm": "delete-all-data"},
                purge_archives=purge,
            )
            if status == 200 and body.get("deleted") is True:
                deleted_count += 1
            else:
                operation_failure_count += 1
        except Exception:
            operation_failure_count += 1
            log.exception(
                "[qa-run-cleanup] reset raised; preserving for retry user=%s",
                authoritative_user_id,
            )
            _restore_for_retry(live)

    try:
        final_rows = db.load_user_documents_with_field(METADATA_FIELD)
    except Exception as exc:
        raise SyntheticAccountStoreUnavailable(
            "synthetic account store is unavailable"
        ) from exc
    remaining_count = sum(
        _raw_run_label_matches(entry, label_prefix=label_prefix)
        for _authoritative_user_id, entry in final_rows
    )
    complete = operation_failure_count == 0 and remaining_count == 0
    return {
        "schema_version": 1,
        "kind": RUN_CLEANUP_KIND,
        "run_id_sha256": hashlib.sha256(run_id.encode("utf-8")).hexdigest(),
        "label_prefix_sha256": hashlib.sha256(
            label_prefix.encode("utf-8")
        ).hexdigest(),
        "database_authoritative": True,
        "matched_count": len(candidates) + invalid_initial_count,
        "deleted_count": deleted_count,
        "already_absent_count": already_absent_count,
        "operation_failure_count": operation_failure_count,
        "remaining_count": remaining_count,
        "complete": complete,
    }


def cleanup_synthetic_run(
    payload: dict,
    *,
    purge_archives: Callable[[str], Exception | None] | None = None,
) -> dict:
    """Close one run, then sweep it under the cross-worker run lock.

    The durable closure marker prevents a registration handler that was delayed
    before acquiring the lock from committing after the final authoritative
    cleanup scan has reported zero remaining rows.
    """
    run_id = _cleanup_run_id(payload)
    with _serialized_run(run_id):
        _close_run(run_id)
        return _cleanup_synthetic_run_locked(
            payload,
            purge_archives=purge_archives,
        )


def _expired_reaper_eligible(
    entry: dict, *, authoritative_user_id: str, now_epoch: int
) -> bool:
    metadata = _valid_metadata(
        entry, authoritative_user_id=authoritative_user_id
    )
    return bool(metadata and metadata["expires_at_epoch"] <= now_epoch)


def _restore_for_retry(entry: dict) -> None:
    """Restore an account snapshot if reset raised during the DB delete step.

    Archive-cleanup failures return 503 before mutating anything.  Unexpected
    delete exceptions can happen after account_reset removed the in-process
    registry row, so put the signed snapshot back in both stores.  This favors
    a retryable synthetic account over a false-success orphan.
    """
    try:
        db.upsert_user(entry)
    except Exception:  # pragma: no cover - depends on a simultaneous DB outage
        log.exception(
            "[qa-reaper] failed to re-persist retry snapshot user=%s",
            entry.get("user_id"),
        )
    with registry._users_lock:
        user_id = entry.get("user_id")
        if not any(user.get("user_id") == user_id for user in registry._users):
            registry._users.append(entry)
        registry._rebuild_key_cache()
    registry.notify_users_changed()


def _publish_heartbeat(summary: dict) -> dict:
    """Persist a cross-worker readiness heartbeat after a complete safe tick."""
    global _last_run

    cfg = config()
    heartbeat_at = int(time.time())
    heartbeat = {
        "schema_version": HEARTBEAT_SCHEMA_VERSION,
        "kind": HEARTBEAT_KIND,
        "healthy": int(summary.get("failed") or 0) == 0,
        "heartbeat_at_epoch": heartbeat_at,
        "reaper_interval_seconds": cfg["reaper_interval_seconds"],
        "process_id": _PROCESS_ID,
        "scanned": int(summary.get("scanned") or 0),
        "eligible": int(summary.get("eligible") or 0),
        "deleted": int(summary.get("deleted") or 0),
        "failed": int(summary.get("failed") or 0),
    }
    # Strict persistence is part of the readiness contract. If it fails, the
    # tick raises and registration remains disabled once the prior heartbeat
    # ages out; a process-local success must not mislead another worker.
    db.set_global_blob_strict(HEARTBEAT_KEY, heartbeat)
    completed = dict(summary)
    completed["heartbeat_at_epoch"] = heartbeat_at
    with _last_run_lock:
        _last_run = completed
    return heartbeat


def reap_expired_accounts(
    *,
    now_epoch: int | None = None,
    purge_archives: Callable[[str], Exception | None] | None = None,
) -> dict:
    """Delete only expired, server-signed QA accounts; safe to call repeatedly."""
    cfg = config()
    now = int(time.time() if now_epoch is None else now_epoch)
    summary = {
        "ran_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "scanned": 0,
        "eligible": 0,
        "deleted": 0,
        "failed": 0,
    }
    if not cfg["enabled"]:
        summary["disabled"] = True
        return summary

    # PostgreSQL is authoritative.  The elected reaper may run in a different
    # worker from registration, and the cross-worker NOTIFY is deliberately
    # best-effort; scanning that worker's registry cache could therefore miss a
    # signed lease forever after one dropped notification.
    candidates = db.load_user_documents_with_field(METADATA_FIELD)
    summary["scanned"] = len(candidates)
    purge = purge_archives or content_core._purge_onboarding_archives_with_retry

    for authoritative_user_id, candidate in candidates:
        if not _expired_reaper_eligible(
            candidate,
            authoritative_user_id=authoritative_user_id,
            now_epoch=now,
        ):
            continue
        summary["eligible"] += 1
        # Recheck the authoritative live row immediately before deletion.  A
        # stale in-process registry snapshot must never authorize a reset.
        live = db.load_user_document(authoritative_user_id)
        if not live or not _expired_reaper_eligible(
            live,
            authoritative_user_id=authoritative_user_id,
            now_epoch=now,
        ):
            continue
        user_id = authoritative_user_id
        try:
            body, status = content_core.account_reset(
                core_store.get_store(user_id),
                {"confirm": "delete-all-data"},
                purge_archives=purge,
            )
            if status == 200 and body.get("deleted") is True:
                summary["deleted"] += 1
            else:
                summary["failed"] += 1
                log.warning(
                    "[qa-reaper] reset deferred user=%s status=%s error=%s",
                    user_id,
                    status,
                    body.get("error") if isinstance(body, dict) else "invalid_response",
                )
        except Exception:
            summary["failed"] += 1
            log.exception(
                "[qa-reaper] reset raised; preserving for retry user=%s", user_id
            )
            _restore_for_retry(live)

    _publish_heartbeat(summary)
    return summary


def _loop() -> None:
    while True:
        try:
            reap_expired_accounts()
        except Exception:  # noqa: BLE001 - a janitor tick must never kill the loop
            log.exception("[qa-reaper] tick failed")
        time.sleep(config()["reaper_interval_seconds"])


def start() -> None:
    """Spawn the elected janitor loop and return immediately."""
    threading.Thread(target=_loop, daemon=True, name="qa-synthetic-reaper").start()
