"""APNs config, JWT signing, and delivery (with env fallback + token expiry)."""

import base64
import json
import os
import time
from pathlib import Path

import httpx
import jwt

from core import config as core_config
from core.store import UserStore
from push import tokens as push_tokens

TEAM_ID = os.environ.get("APNS_TEAM_ID", "").strip() or "DC9JH5DRMY"
KEY_ID = os.environ.get("APNS_KEY_ID", "").strip() or "5TH55X5U7T"
BUNDLE_ID = os.environ.get("APNS_BUNDLE_ID", "").strip() or "com.feedling.mcp"
APNS_SANDBOX = os.environ.get("APNS_SANDBOX", "true").strip().lower() != "false"

APNS_KEY = None
# Prefer env vars over filesystem: CVM deploys inject the key via
# docker compose env, not mounted files. APNS_KEY_P8_B64 is base64 to
# survive GH Actions → compose shell quoting of the multi-line PEM.
_env_b64 = os.environ.get("APNS_KEY_P8_B64", "").strip()
if _env_b64:
    try:
        APNS_KEY = base64.b64decode(_env_b64).decode("utf-8")
        print(f"[apns] key loaded from APNS_KEY_P8_B64 (len={len(APNS_KEY)})")
    except Exception as e:
        print(f"[apns] APNS_KEY_P8_B64 decode failed: {e}")
if not APNS_KEY:
    _env_raw = os.environ.get("APNS_KEY_P8", "").strip()
    if _env_raw:
        APNS_KEY = _env_raw
        print(f"[apns] key loaded from APNS_KEY_P8 (len={len(APNS_KEY)})")
if not APNS_KEY:
    _env_path = os.environ.get("APNS_KEY_PATH", "").strip()
    _KEY_SEARCH = [
        Path(_env_path) if _env_path else None,
        core_config.FEEDLING_DIR / f"AuthKey_{KEY_ID}.p8",
        Path(__file__).parent / f"AuthKey_{KEY_ID}.p8",
    ]
    for _p in _KEY_SEARCH:
        if _p and _p.exists():
            APNS_KEY = _p.read_text()
            print(f"[apns] key loaded from {_p}")
            break
if not APNS_KEY:
    print("[apns] WARNING: .p8 key not found — push endpoints will log only, not deliver")


def _make_apns_jwt() -> str:
    return jwt.encode(
        {"iss": TEAM_ID, "iat": int(time.time())},
        APNS_KEY,
        algorithm="ES256",
        headers={"kid": KEY_ID},
    )


def _apns_env_name(sandbox: bool) -> str:
    return "sandbox" if sandbox else "production"


def _apns_host(sandbox: bool) -> str:
    return "api.sandbox.push.apple.com" if sandbox else "api.push.apple.com"


def _apns_reason_text(result: dict) -> str:
    reason = str((result or {}).get("reason") or "")
    try:
        parsed = json.loads(reason)
        if isinstance(parsed, dict) and parsed.get("reason"):
            return str(parsed.get("reason"))
    except Exception:
        pass
    return reason


def _apns_should_retry_other_env(result: dict) -> bool:
    if (result or {}).get("status") != "error":
        return False
    reason = _apns_reason_text(result)
    return any(
        marker in reason
        for marker in (
            "BadDeviceToken",
            "BadEnvironmentKeyInToken",
            "BadEnvironmentKeyIdInToken",
            "BadCertificateEnvironment",
            # Live Activity tokens can surface this for an environment
            # mismatch. Try the other APNs host before expiring the token.
            "ExpiredToken",
        )
    )


def _apns_token_should_expire(result: dict) -> bool:
    if (result or {}).get("status") != "error":
        return False
    reason = _apns_reason_text(result)
    return any(
        marker in reason
        for marker in (
            "BadDeviceToken",
            "BadEnvironmentKeyInToken",
            "BadEnvironmentKeyIdInToken",
            "BadCertificateEnvironment",
            "DeviceTokenNotForTopic",
            "ExpiredToken",
            "TopicDisallowed",
            "Unregistered",
        )
    )


def _send_apns_once(device_token: str, payload: dict, push_type: str, topic: str, *, sandbox: bool) -> dict:
    host = _apns_host(sandbox)
    url = f"https://{host}/3/device/{device_token}"
    env_name = _apns_env_name(sandbox)
    headers = {
        "authorization": f"bearer {_make_apns_jwt()}",
        "apns-push-type": push_type,
        "apns-topic": topic,
        "apns-expiration": "0",
        "apns-priority": "10",
    }
    try:
        with httpx.Client(http2=True, timeout=10) as client:
            resp = client.post(url, json=payload, headers=headers)
        if resp.status_code == 200:
            return {"status": "delivered", "apns_env": env_name}
        return {"status": "error", "code": resp.status_code, "reason": resp.text, "apns_env": env_name}
    except Exception as e:
        return {"status": "error", "reason": str(e), "apns_env": env_name}


def _apns_sandbox_for_env(env: str | None) -> bool | None:
    clean = str(env or "").strip().lower()
    if clean == "sandbox":
        return True
    if clean == "production":
        return False
    return None


def _send_apns(
    device_token: str,
    payload: dict,
    push_type: str,
    topic: str,
    *,
    preferred_env: str | None = None,
) -> dict:
    if not APNS_KEY:
        print(f"[apns] no key — logged only → {device_token[:16]}… {payload}")
        return {"status": "logged_only"}

    preferred_sandbox = _apns_sandbox_for_env(preferred_env)
    primary_sandbox = APNS_SANDBOX if preferred_sandbox is None else preferred_sandbox
    attempts: list[dict] = []
    for idx, sandbox in enumerate((primary_sandbox, not primary_sandbox)):
        if idx == 1 and attempts and not _apns_should_retry_other_env(attempts[0]):
            break
        result = _send_apns_once(device_token, payload, push_type, topic, sandbox=sandbox)
        attempts.append(result)
        if result.get("status") == "delivered":
            if idx > 0:
                result["fallback_attempted"] = True
                result["fallback_from"] = attempts[0].get("apns_env", _apns_env_name(primary_sandbox))
            return result

    last = dict(attempts[-1]) if attempts else {"status": "error", "reason": "not_attempted"}
    if len(attempts) > 1:
        last["fallback_attempted"] = True
        last["fallback_from"] = attempts[0].get("apns_env", _apns_env_name(primary_sandbox))
        last["first_error"] = attempts[0]
    last["attempted_envs"] = [str(a.get("apns_env") or "") for a in attempts]
    return last


def _send_apns_to_active_tokens(
    store: UserStore,
    predicate,
    payload: dict,
    *,
    push_type: str,
    topic: str,
    activity_id: str | None = None,
) -> dict:
    candidates = push_tokens._select_tokens(store, predicate, activity_id=activity_id, active_only=True)
    if not candidates and activity_id:
        candidates = push_tokens._select_tokens(store, predicate, active_only=True)
    if not candidates:
        return {"status": "skipped", "reason": "no_active_token", "attempts": 0}

    errors = []
    for entry in candidates:
        result = _send_apns(
            entry["token"],
            payload,
            push_type=push_type,
            topic=topic,
            preferred_env=entry.get("apns_env"),
        )
        if result.get("status") == "delivered":
            push_tokens._mark_active_token_success(store, entry, apns_env=result.get("apns_env"))
            result["attempts"] = len(errors) + 1
            return result

        reason_text = _apns_reason_text(result)
        if reason_text:
            push_tokens._update_token_lifecycle(store, entry, last_error=reason_text)
        errors.append({
            "type": entry.get("type", ""),
            "activity_id": entry.get("activity_id", ""),
            "registered_at": entry.get("registered_at", ""),
            "apns_env": result.get("apns_env", ""),
            "attempted_envs": result.get("attempted_envs", []),
            "reason": reason_text or str(result.get("reason", "")),
        })
        if _apns_token_should_expire(result):
            push_tokens._mark_expired_token(store, entry, reason_text or str(result.get("reason", "")))
            continue

    last = errors[-1] if errors else {}
    return {
        "status": "error",
        "reason": last.get("reason", "all_tokens_failed"),
        "attempts": len(errors),
        "errors": errors[-5:],
    }
