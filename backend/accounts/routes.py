"""Accounts HTTP surface: registration, recovery, whoami, access modes.

Routes kept byte-identical to their app.py originals — only the decorator
target (Blueprint) and cross-module call prefixes changed.
"""

import hmac
import secrets
import time
from datetime import datetime

from flask import Blueprint, jsonify, request

import db
from accounts import access as accounts_access
from accounts import auth, onboarding, recover, registry
from content_encryption import build_envelope
from core import enclave
from core import envelope as core_envelope
from core import store as core_store

bp = Blueprint("accounts", __name__)

@bp.route("/v1/access/modes", methods=["GET"])
def access_modes_get():
    store = auth.require_user()
    return jsonify(accounts_access._access_modes_payload(store))


@bp.route("/v1/access/modes/switch", methods=["POST"])
def access_modes_switch():
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    mode = registry._normalize_access_mode(str(payload.get("access_mode") or payload.get("route") or ""))
    if mode not in registry.ACCESS_MODES:
        return jsonify({"error": "access_mode must be resident, model_api, or official_import"}), 400
    data = onboarding._save_onboarding_route(store, mode)
    with registry._users_lock:
        user_entry = registry._find_user_entry_locked(store.user_id)
        if user_entry:
            registry._upsert_access_binding_locked(user_entry, mode, touch_seen=True)
            registry._save_users()
    print(f"[access:{store.user_id}] active_route={data['route']}")
    return jsonify(accounts_access._access_modes_payload(store))


@bp.route("/v1/access/link-token", methods=["POST"])
def access_link_token_create():
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    mode = registry._normalize_access_mode(str(payload.get("access_mode") or payload.get("route") or onboarding._load_onboarding_route(store)))
    if mode not in registry.ACCESS_MODES:
        return jsonify({"error": "access_mode must be resident, model_api, or official_import"}), 400
    label = str(payload.get("label") or registry.ACCESS_MODE_LABELS.get(mode, mode)).strip()[:80]
    raw_token = "flt_" + secrets.token_urlsafe(32)
    token_hash = registry._hash_api_key(raw_token)
    now_epoch = time.time()
    expires_at_epoch = now_epoch + accounts_access.ACCESS_LINK_TOKEN_TTL_SEC
    with registry._users_lock:
        user_entry = registry._find_user_entry_locked(store.user_id)
        if not user_entry:
            return jsonify({"error": "user not found"}), 404
        principal_id = user_entry.get("principal_id", "")
        existing_status = ""
        for binding in user_entry.get("access_bindings") or []:
            if isinstance(binding, dict) and registry._normalize_access_mode(str(binding.get("access_mode") or "")) == mode:
                existing_status = str(binding.get("status") or "")
                break
        registry._upsert_access_binding_locked(user_entry, mode, status=existing_status or "pending")
        registry._save_users()
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
    return jsonify({
        "token": raw_token,
        "token_id": entry["token_id"],
        "access_mode": mode,
        "route": mode,
        "label": label,
        "expires_at": entry["expires_at"],
        "expires_in_seconds": accounts_access.ACCESS_LINK_TOKEN_TTL_SEC,
        "claim_endpoint": "/v1/access/claim-token",
    }), 201


@bp.route("/v1/access/claim-token", methods=["POST"])
def access_link_token_claim():
    payload = request.get_json(silent=True) or {}
    raw_token = str(payload.get("token") or "").strip()
    if not raw_token:
        return jsonify({"error": "token required"}), 400
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
            return jsonify({"error": "invalid_token"}), 404
        if match.get("used_at"):
            return jsonify({"error": "token_already_used"}), 409
        try:
            expires_at_epoch = float(match.get("expires_at_epoch") or 0)
        except Exception:
            expires_at_epoch = 0
        if expires_at_epoch and expires_at_epoch < now_epoch:
            return jsonify({"error": "token_expired"}), 410
        user_id = str(match.get("user_id") or "")
        mode = registry._normalize_access_mode(str(match.get("access_mode") or ""))
        if mode not in registry.ACCESS_MODES:
            return jsonify({"error": "token_access_mode_invalid"}), 400
        with registry._users_lock:
            user_entry = registry._find_user_entry_locked(user_id)
            if not user_entry:
                return jsonify({"error": "user not found"}), 404
            if public_key and not str(user_entry.get("public_key") or "").strip():
                _, err = core_envelope._decode_content_public_key(public_key)
                if err:
                    return jsonify({"error": err}), 400
                user_entry["public_key"] = public_key
            if archive_language and not user_entry.get("archive_language"):
                user_entry["archive_language"] = archive_language
            issued = registry._issue_api_key_for_user_locked(
                user_entry,
                access_mode=mode,
                label=client_label or str(match.get("label") or registry.ACCESS_MODE_LABELS.get(mode, mode)),
            )
            registry._save_users()
            principal_id = user_entry.get("principal_id", "")
        if make_active:
            onboarding._save_onboarding_route(core_store.get_store(user_id), mode)
        match["used_at"] = datetime.now().isoformat()
        match["claimed_label"] = client_label
        accounts_access._save_access_link_tokens(accounts_access._trim_access_link_tokens(rows))
    print(f"[access:{user_id}] claimed mode={mode} key={issued['key_entry']['key_id']}")
    return jsonify({
        "status": "connected",
        "user_id": user_id,
        "principal_id": principal_id,
        "api_key": issued["api_key"],
        "access_mode": mode,
        "route": mode,
        "active_route": onboarding._load_onboarding_route(core_store.get_store(user_id)),
        "key_id": issued["key_entry"]["key_id"],
    }), 201


# ---------------------------------------------------------------------------
# Users: register endpoint (public — no auth required)
# ---------------------------------------------------------------------------


@bp.route("/v1/users/register", methods=["POST"])
def users_register():
    payload = request.get_json(silent=True) or {}
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
        return jsonify({
            "error": "account_exists_for_key",
            "detail": "An account already exists for this content public key. "
                      "Recover it instead of registering a new one.",
            "recover_endpoint": "/v1/account/recover/challenge",
        }), 409
    result = registry._register_user(
        public_key=public_key or None,
        archive_language=archive_language or None,
        access_mode=access_mode,
        label=label or None,
    )
    return jsonify(result), 201


@bp.route("/v1/account/recover/challenge", methods=["POST"])
def account_recover_challenge():
    """Step 1 of keypair recovery. Given a content public_key, seal a random
    challenge to it (local_only envelope — the device decrypts with the matching
    private key) so possession can be proven without an api_key. 404 when no
    account uses this key (caller should register a fresh account instead)."""
    payload = request.get_json(silent=True) or {}
    public_key = str(payload.get("public_key") or "").strip()
    pk_bytes, err = core_envelope._decode_content_public_key(public_key)
    if err:
        return jsonify({"error": err}), 400
    account = registry._canonical_account_for_pubkey(public_key)
    if not account:
        return jsonify({"error": "no_recoverable_account"}), 404
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
    return jsonify({"challenge_id": challenge_id, "envelope": envelope}), 200


@bp.route("/v1/account/recover/verify", methods=["POST"])
def account_recover_verify():
    """Step 2 of keypair recovery. The device returns the decrypted challenge,
    proving it holds the private key. On a match, issue a fresh api_key for the
    EXISTING account (no new user). The challenge is single-use + short-lived."""
    payload = request.get_json(silent=True) or {}
    challenge_id = str(payload.get("challenge_id") or "").strip()
    answer = str(payload.get("answer") or "")
    now = time.time()
    with recover._recover_challenges_lock:
        recover._prune_recover_challenges_locked(now)
        entry = recover._recover_challenges.pop(challenge_id, None)  # one-time use
    if not entry or entry.get("expires_at", 0) < now:
        return jsonify({"error": "invalid_or_expired_challenge"}), 401
    if not hmac.compare_digest(answer, str(entry.get("challenge") or "")):
        return jsonify({"error": "challenge_failed"}), 401
    user_id = entry["user_id"]
    with registry._users_lock:
        user_entry = registry._find_user_entry_locked(user_id)
        if not user_entry:
            return jsonify({"error": "account_not_found"}), 404
        existing = [k for k in (user_entry.get("api_keys") or [])
                    if isinstance(k, dict) and not k.get("revoked_at")]
        mode = (existing[0].get("access_mode") if existing else "") or "official_import"
        if mode not in registry.ACCESS_MODES:
            mode = "official_import"
        issued = registry._issue_api_key_for_user_locked(user_entry, access_mode=mode,
                                                label="Recovered (key)")
        registry._save_users()
        principal_id = user_entry.get("principal_id", "")
    print(f"[recover:verify] user_id={user_id} recovered via keypair PoP")
    return jsonify({
        "user_id": user_id,
        "principal_id": principal_id,
        "api_key": issued["api_key"],
        "public_key": entry["public_key"],
    }), 200


@bp.route("/v1/users/whoami", methods=["GET"])
def users_whoami():
    """Identify the caller and return the public material needed to wrap
    content for them.

    Returns:
      - `public_key` — the caller's own X25519 content pubkey (base64),
        from the user record.
      - `enclave_content_public_key_hex` — the live enclave's content
        pubkey, fetched from /attestation and cached for 60s. Missing
        when no enclave is reachable.
      - `archive_language` — the locale code the iOS app supplied at
        registration (e.g. "en", "zh-Hans"). Null for legacy accounts;
        callers fall back to inferring from existing card content.
    """
    store = auth.require_user()
    access = accounts_access._access_modes_payload(store)
    resp: dict = {
        "user_id": store.user_id,
        "principal_id": access.get("principal_id", ""),
        "active_route": access.get("active_route", ""),
        "access_modes": access.get("access_modes", []),
    }
    pk = registry._get_user_public_key(store.user_id)
    if pk:
        resp["public_key"] = pk
    info = enclave._get_enclave_info()
    if info:
        resp["enclave_content_public_key_hex"] = info["content_pk_hex"]
        resp["enclave_compose_hash"] = info["compose_hash"]
    archive_language = registry._get_user_archive_language(store.user_id)
    if archive_language:
        resp["archive_language"] = archive_language
    return jsonify(resp)


@bp.route("/v1/users/preferences", methods=["POST"])
def users_set_preferences():
    """Update mutable preferences on the authenticated user's record.

    Currently the only supported preference is `archive_language` — the
    locale code that the agent should use as the source of truth for
    Memory Garden / Identity Card language. iOS posts this on first
    launch for legacy accounts that registered before the field existed,
    and again whenever the user explicitly changes their iOS system
    language and re-launches the app.

    Body: {"archive_language": "<bcp-47 string>" | null}
    Pass null to clear (agent falls back to inferred behavior).
    """
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    if "archive_language" not in payload:
        return jsonify({
            "error": "archive_language required (string or null)",
        }), 400
    raw = payload.get("archive_language")
    if raw is not None and not isinstance(raw, str):
        return jsonify({"error": "archive_language must be a string or null"}), 400
    new_value = (raw or "").strip() if isinstance(raw, str) else ""

    updated = False
    with registry._users_lock:
        for u in registry._users:
            if u.get("user_id") == store.user_id:
                if new_value:
                    u["archive_language"] = new_value
                else:
                    u.pop("archive_language", None)
                updated = True
                break
        if updated:
            db.upsert_user(u)

    if not updated:
        return jsonify({"error": "user not found"}), 404
    print(f"[users] {store.user_id} archive_language → {new_value or 'cleared'}")
    return jsonify({
        "status": "updated",
        "archive_language": new_value or None,
    })


@bp.route("/v1/onboarding/route", methods=["GET", "POST"])
def onboarding_route():
    store = auth.require_user()
    if request.method == "GET":
        return jsonify({
            "route": onboarding._load_onboarding_route(store),
            "allowed": sorted(onboarding.MODEL_API_ROUTES),
        })
    payload = request.get_json(silent=True) or {}
    try:
        data = onboarding._save_onboarding_route(store, str(payload.get("route") or ""))
    except ValueError as e:
        return jsonify({"error": str(e), "allowed": sorted(onboarding.MODEL_API_ROUTES)}), 400
    with registry._users_lock:
        user_entry = registry._find_user_entry_locked(store.user_id)
        if user_entry:
            registry._upsert_access_binding_locked(user_entry, data["route"], touch_seen=True)
            registry._save_users()
    print(f"[onboarding:{store.user_id}] route={data['route']}")
    return jsonify(data)
