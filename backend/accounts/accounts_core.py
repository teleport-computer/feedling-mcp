"""Framework-neutral account route cores (ASGI-migration plan §9.4 / §7).

Per-route request logic for the remaining ``accounts`` endpoints (access modes,
link/claim tokens, register, keypair recovery, preferences, onboarding route),
lifted out of the Flask routes so the native ASGI handlers reuse byte-identical
bodies and status codes. There is **no** ``flask.request`` here: callers parse
headers/JSON themselves and pass plain params (plus the resolved ``UserStore``
for the user-authed routes). Each function returns ``(body_dict, status_code)``.

Cross-module calls stay routed through the *module* (``registry.<fn>`` etc.) so
tests that monkeypatch ``registry`` / ``accounts.access`` continue to work
unchanged. These functions touch sync ``db.py`` (registry / blob writes) and a
blocking enclave-free envelope build, so ASGI callers must run them on the
threadpool, never the event loop (plan §5.2 / §5.0).
"""

from __future__ import annotations

import hmac
import secrets
import time
from contextlib import nullcontext
from datetime import datetime

import db
from accounts import access as accounts_access
from accounts import onboarding, recover, registry
from content_encryption import build_envelope
from core import envelope as core_envelope
from core import store as core_store
from core.store import UserStore


# ---------------------------------------------------------------------------
# Access modes
# ---------------------------------------------------------------------------


class _RuntimeControlUnavailable(RuntimeError):
    """A resident selection could not converge its ownership state."""


def access_modes_get(store: UserStore):
    return accounts_access._access_modes_payload(store), 200


def access_modes_switch(store: UserStore, payload: dict):
    mode = registry._normalize_access_mode(str(payload.get("access_mode") or payload.get("route") or ""))
    if mode not in registry.ACCESS_MODES:
        return {"error": "access_mode must be resident, model_api, or official_import"}, 400
    try:
        data = _select_access_mode(store, mode)
    except _RuntimeControlUnavailable:
        return {"error": "runtime_control_unavailable"}, 503
    with registry._users_lock:
        user_entry = registry._find_user_entry_locked(store.user_id)
        if user_entry:
            registry._upsert_access_binding_locked(user_entry, mode, touch_seen=True)
            registry.persist_user(user_entry)
    print(f"[access:{store.user_id}] active_route={data['route']}")
    return accounts_access._access_modes_payload(store), 200


def _select_access_mode(store: UserStore, mode: str) -> dict:
    from hosted import config_store, new_user_v2_cohort

    mode = registry._normalize_access_mode(mode)
    if mode not in onboarding.MODEL_API_ROUTES:
        # Preserve the validation-only path without acquiring a control lock.
        return onboarding._save_onboarding_route(store, mode)

    lock_acquired = False
    try:
        with db.hosted_runtime_config_mutation_lock(store.user_id):
            lock_acquired = True
            if mode != "resident":
                # Non-resident selection remains route persistence only. In
                # particular, selecting Model API is not rollout authority.
                return onboarding._save_onboarding_route(store, mode)

            previous_route_doc = db.get_blob_strict(
                store.user_id, "onboarding_route"
            )
            previous_runtime_mode = (
                config_store.get_hosted_runtime_control_strict(store)[0]
            )
            previous_allowlist = db.get_runtime_allowlist_entry(store.user_id)
            try:
                data = onboarding._save_onboarding_route(store, mode)
                new_user_v2_cohort.pin_resident(store)
            except Exception as transition_error:
                compensation_error = None
                try:
                    new_user_v2_cohort.restore_allowlist(
                        store.user_id, previous_allowlist
                    )
                except Exception as exc:
                    compensation_error = exc
                try:
                    if previous_route_doc is None:
                        db.delete_onboarding_route_strict(store.user_id)
                    else:
                        db.set_onboarding_route_strict(
                            store.user_id, previous_route_doc
                        )
                except Exception as exc:
                    compensation_error = compensation_error or exc
                try:
                    config_store.set_hosted_runtime_mode(
                        store, previous_runtime_mode
                    )
                except Exception as exc:
                    compensation_error = compensation_error or exc
                raise _RuntimeControlUnavailable from (
                    compensation_error or transition_error
                )
            return data
    except _RuntimeControlUnavailable:
        raise
    except Exception as exc:
        if mode == "resident" or not lock_acquired:
            raise _RuntimeControlUnavailable from exc
        raise


def access_link_token_create(store: UserStore, payload: dict):
    mode = registry._normalize_access_mode(str(payload.get("access_mode") or payload.get("route") or onboarding._load_onboarding_route(store)))
    if mode not in registry.ACCESS_MODES:
        return {"error": "access_mode must be resident, model_api, or official_import"}, 400
    label = str(payload.get("label") or registry.ACCESS_MODE_LABELS.get(mode, mode)).strip()[:80]
    raw_token = "flt_" + secrets.token_urlsafe(32)
    token_hash = registry._hash_api_key(raw_token)
    now_epoch = time.time()
    expires_at_epoch = now_epoch + accounts_access.ACCESS_LINK_TOKEN_TTL_SEC
    with registry._users_lock:
        user_entry = registry._find_user_entry_locked(store.user_id)
        if not user_entry:
            return {"error": "user not found"}, 404
        principal_id = user_entry.get("principal_id", "")
        existing_status = ""
        for binding in user_entry.get("access_bindings") or []:
            if isinstance(binding, dict) and registry._normalize_access_mode(str(binding.get("access_mode") or "")) == mode:
                existing_status = str(binding.get("status") or "")
                break
        registry._upsert_access_binding_locked(user_entry, mode, status=existing_status or "pending")
        registry.persist_user(user_entry)
    entry = {
        "token_id": f"flt_{secrets.token_hex(6)}",
        "token_hash": token_hash,
        "user_id": store.user_id,
        "principal_id": principal_id,
        "access_mode": mode,
        "label": label,
        "created_at": datetime.now().isoformat(),
        "expires_at": datetime.fromtimestamp(expires_at_epoch).isoformat(),
        "expires_at_epoch": expires_at_epoch,
        "used_at": "",
    }
    with accounts_access._access_link_tokens_lock:
        rows = accounts_access._trim_access_link_tokens(accounts_access._load_access_link_tokens())
        rows.append(entry)
        accounts_access._save_access_link_tokens(rows)
    return {
        "token": raw_token,
        "token_id": entry["token_id"],
        "access_mode": mode,
        "route": mode,
        "label": label,
        "expires_at": entry["expires_at"],
        "expires_in_seconds": accounts_access.ACCESS_LINK_TOKEN_TTL_SEC,
        "claim_endpoint": "/v1/access/claim-token",
    }, 201


def access_link_token_claim(payload: dict):
    raw_token = str(payload.get("token") or "").strip()
    if not raw_token:
        return {"error": "token required"}, 400
    token_hash = registry._hash_api_key(raw_token)
    now_epoch = time.time()
    client_label = str(payload.get("label") or payload.get("client_label") or "").strip()[:80]
    public_key = str(payload.get("public_key") or "").strip()
    archive_language = str(payload.get("archive_language") or "").strip()
    make_active = bool(payload.get("make_active", True))
    with accounts_access._access_link_tokens_lock:
        rows = accounts_access._load_access_link_tokens()
        match = None
        for row in rows:
            if row.get("token_hash") == token_hash:
                match = row
                break
        if not match:
            return {"error": "invalid_token"}, 404
        if match.get("used_at"):
            return {"error": "token_already_used"}, 409
        try:
            expires_at_epoch = float(match.get("expires_at_epoch") or 0)
        except Exception:
            expires_at_epoch = 0
        if expires_at_epoch and expires_at_epoch < now_epoch:
            return {"error": "token_expired"}, 410
        user_id = str(match.get("user_id") or "")
        mode = registry._normalize_access_mode(str(match.get("access_mode") or ""))
        if mode not in registry.ACCESS_MODES:
            return {"error": "token_access_mode_invalid"}, 400
        runtime_lock = (
            db.hosted_runtime_config_mutation_lock(user_id)
            if make_active
            else nullcontext()
        )
        try:
            with runtime_lock:
                # Validate the target user and any supplied public key before a
                # route transition, but defer every user/key/token mutation
                # until the resident ownership operation has succeeded.
                with registry._users_lock:
                    user_entry = registry._find_user_entry_locked(user_id)
                    if not user_entry:
                        return {"error": "user not found"}, 404
                    if public_key and not str(user_entry.get("public_key") or "").strip():
                        _, err = core_envelope._decode_content_public_key(public_key)
                        if err:
                            return {"error": err}, 400

                if make_active:
                    store = core_store.get_store(user_id)
                    # The outer per-user lock is deliberately reentrant.  In
                    # particular, a resident token must use the same durable
                    # pin + compensated fence transition as every other
                    # resident-selection surface.
                    _select_access_mode(store, mode)

                with registry._users_lock:
                    user_entry = registry._find_user_entry_locked(user_id)
                    if not user_entry:
                        return {"error": "user not found"}, 404
                    if public_key and not str(user_entry.get("public_key") or "").strip():
                        user_entry["public_key"] = public_key
                    if archive_language and not user_entry.get("archive_language"):
                        user_entry["archive_language"] = archive_language
                    issued = registry._issue_api_key_for_user_locked(
                        user_entry,
                        access_mode=mode,
                        label=client_label or str(match.get("label") or registry.ACCESS_MODE_LABELS.get(mode, mode)),
                    )
                    registry.persist_user(user_entry)
                    principal_id = user_entry.get("principal_id", "")
                match["used_at"] = datetime.now().isoformat()
                match["claimed_label"] = client_label
                accounts_access._save_access_link_tokens(
                    accounts_access._trim_access_link_tokens(rows)
                )
        except (db.HostedRuntimeConfigBusyError, _RuntimeControlUnavailable):
            return {"error": "runtime_control_unavailable"}, 503
    print(f"[access:{user_id}] claimed mode={mode} key={issued['key_entry']['key_id']}")
    return {
        "status": "connected",
        "user_id": user_id,
        "principal_id": principal_id,
        "api_key": issued["api_key"],
        "access_mode": mode,
        "route": mode,
        "active_route": onboarding._load_onboarding_route(core_store.get_store(user_id)),
        "key_id": issued["key_entry"]["key_id"],
    }, 201


# ---------------------------------------------------------------------------
# Register (public — no user auth) + keypair recovery (pre-auth PoP)
# ---------------------------------------------------------------------------


def users_register(payload: dict):
    public_key = (payload.get("public_key") or "").strip()
    archive_language = (payload.get("archive_language") or "").strip()
    access_mode = str(payload.get("access_mode") or payload.get("route") or "official_import")
    label = str(payload.get("label") or "").strip()
    # Server-side orphan backstop: never mint a second account for a content
    # public key that already has one. The device holds the matching private
    # key, so it must recover the existing account instead of registering. This
    # closes the orphan gap even when the client's recover-first guard is
    # bypassed (offline at first launch, iCloud Keychain sync lag, old app
    # version). The Reset-and-reimport flow wipes the keypair first, so it gets a
    # fresh public_key and is unaffected.
    if public_key and registry._canonical_account_for_pubkey(public_key) is not None:
        return {
            "error": "account_exists_for_key",
            "detail": "An account already exists for this content public key. "
                      "Recover it instead of registering a new one.",
            "recover_endpoint": "/v1/account/recover/challenge",
        }, 409
    result = registry._register_user(
        public_key=public_key or None,
        archive_language=archive_language or None,
        access_mode=access_mode,
        label=label or None,
    )
    return result, 201


def account_recover_challenge(payload: dict):
    """Step 1 of keypair recovery. Given a content public_key, seal a random
    challenge to it (local_only envelope — the device decrypts with the matching
    private key) so possession can be proven without an api_key. 404 when no
    account uses this key (caller should register a fresh account instead)."""
    public_key = str(payload.get("public_key") or "").strip()
    pk_bytes, err = core_envelope._decode_content_public_key(public_key)
    if err:
        return {"error": err}, 400
    account = registry._canonical_account_for_pubkey(public_key)
    if not account:
        return {"error": "no_recoverable_account"}, 404
    challenge = secrets.token_hex(32)
    challenge_id = "rec_" + secrets.token_hex(12)
    envelope = build_envelope(
        plaintext=challenge.encode("utf-8"),
        owner_user_id=account["user_id"],
        user_pk_bytes=pk_bytes,
        enclave_pk_bytes=None,
        visibility="local_only",
    )
    now = time.time()
    with recover._recover_challenges_lock:
        recover._prune_recover_challenges_locked(now)
        recover._recover_challenges[challenge_id] = {
            "public_key": public_key,
            "user_id": account["user_id"],
            "challenge": challenge,
            "expires_at": now + recover.RECOVER_CHALLENGE_TTL_SEC,
        }
    print(f"[recover:challenge] user_id={account['user_id']} challenge_id={challenge_id}")
    return {"challenge_id": challenge_id, "envelope": envelope}, 200


def account_recover_verify(payload: dict):
    """Step 2 of keypair recovery. The device returns the decrypted challenge,
    proving it holds the private key. On a match, issue a fresh api_key for the
    EXISTING account (no new user). The challenge is single-use + short-lived."""
    challenge_id = str(payload.get("challenge_id") or "").strip()
    answer = str(payload.get("answer") or "")
    now = time.time()
    with recover._recover_challenges_lock:
        recover._prune_recover_challenges_locked(now)
        entry = recover._recover_challenges.pop(challenge_id, None)  # one-time use
    if not entry or entry.get("expires_at", 0) < now:
        return {"error": "invalid_or_expired_challenge"}, 401
    if not hmac.compare_digest(answer, str(entry.get("challenge") or "")):
        return {"error": "challenge_failed"}, 401
    user_id = entry["user_id"]
    with registry._users_lock:
        user_entry = registry._find_user_entry_locked(user_id)
        if not user_entry:
            return {"error": "account_not_found"}, 404
        existing = [k for k in (user_entry.get("api_keys") or [])
                    if isinstance(k, dict) and not k.get("revoked_at")]
        mode = (existing[0].get("access_mode") if existing else "") or "official_import"
        if mode not in registry.ACCESS_MODES:
            mode = "official_import"
        issued = registry._issue_api_key_for_user_locked(user_entry, access_mode=mode,
                                                label="Recovered (key)")
        registry.persist_user(user_entry)
        principal_id = user_entry.get("principal_id", "")
    print(f"[recover:verify] user_id={user_id} recovered via keypair PoP")
    return {
        "user_id": user_id,
        "principal_id": principal_id,
        "api_key": issued["api_key"],
        "public_key": entry["public_key"],
    }, 200


# ---------------------------------------------------------------------------
# Preferences + onboarding route (both require an authenticated user)
# ---------------------------------------------------------------------------


def users_set_preferences(store: UserStore, payload: dict):
    raw = payload.get("archive_language")
    if raw is not None and not isinstance(raw, str):
        return {"error": "archive_language must be a string or null"}, 400
    has_lang = "archive_language" in payload
    new_value = (raw or "").strip() if isinstance(raw, str) else ""

    tz_raw = payload.get("timezone")
    has_tz = "timezone" in payload
    if has_tz and tz_raw is not None and not isinstance(tz_raw, str):
        return {"error": "timezone must be a string or null"}, 400

    if not has_lang and not has_tz:
        return {
            "error": "provide archive_language and/or timezone (string or null)",
        }, 400

    # Validate timezone up front so an invalid zone can't half-apply the
    # archive_language write below.
    if has_tz and isinstance(tz_raw, str) and tz_raw.strip() and not registry._is_valid_iana_timezone(tz_raw.strip()):
        return {"error": "timezone must be a valid IANA zone or null"}, 400

    updated = False
    with registry._users_lock:
        for u in registry._users:
            if u.get("user_id") == store.user_id:
                if has_lang:
                    if new_value:
                        u["archive_language"] = new_value
                    else:
                        u.pop("archive_language", None)
                updated = True
                break
        if updated and has_lang:
            registry.persist_user(u)

    if not updated:
        return {"error": "user not found"}, 404

    if has_tz:
        if not registry._set_user_timezone(store.user_id, tz_raw):
            return {"error": "timezone must be a valid IANA zone or null"}, 400

    tz_now = registry._get_user_timezone(store.user_id)
    print(f"[users] {store.user_id} prefs archive_language={new_value or 'unchanged'} timezone={tz_now or 'unset'}")
    return {
        "status": "updated",
        "archive_language": registry._get_user_archive_language(store.user_id),
        "timezone": tz_now or None,
    }, 200


def onboarding_route_get(store: UserStore):
    return {
        "route": onboarding._load_onboarding_route(store),
        "allowed": sorted(onboarding.MODEL_API_ROUTES),
    }, 200


def onboarding_route_post(store: UserStore, payload: dict):
    try:
        data = _select_access_mode(
            store, str(payload.get("route") or "")
        )
    except ValueError as e:
        return {"error": str(e), "allowed": sorted(onboarding.MODEL_API_ROUTES)}, 400
    except _RuntimeControlUnavailable:
        return {"error": "runtime_control_unavailable"}, 503
    with registry._users_lock:
        user_entry = registry._find_user_entry_locked(store.user_id)
        if user_entry:
            registry._upsert_access_binding_locked(user_entry, data["route"], touch_seen=True)
            registry.persist_user(user_entry)
    print(f"[onboarding:{store.user_id}] route={data['route']}")
    return data, 200
