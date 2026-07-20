"""Framework-neutral notify-relay core: enroll + push + DB/TEE writes.

Blocking on purpose (sync psycopg + outbound APNs HTTP) — routes call in via
``threadpool.run_db`` (CONTRIBUTING red line). Core functions return
``(status_code, body)`` tuples; the route layer turns them into JSON responses.

auth_token is stored in PLAINTEXT — deliberate (see the 0020 migration
docstring): the enroll endpoint must return the SAME token on re-enroll, and
the token only lets its holder push to their own enrolled device.

Every main-DB write mirrors to the TEE shadow via ``tee_shadow.mirror``
(best-effort; the reconciler heals gaps). ``notify_relay_logs.id`` is
GENERATED ALWAYS AS IDENTITY, so log mirrors pin the primary-assigned id with
``OVERRIDING SYSTEM VALUE`` — same invariant as ``db.log_append``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
import uuid

import db
from push import apns
from push.live_activity import build_content_state, build_live_activity_aps
from tee_shadow import mirror

AUTH_TOKEN_PREFIX = "nrt_"
_AUTH_TOKEN_RE = re.compile(r"^nrt_[0-9a-f]{48}$")
_DEVICE_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{32,200}$")
_APNS_ENVS = ("sandbox", "production")

# push_type wire values (public contract — deploy/SELF_HOSTING.md).
TYPE_ALERT = 1
TYPE_LIVE_ACTIVITY_UPDATE = 2   # Dynamic Island is the same Live Activity
TYPE_LIVE_ACTIVITY_START = 3    # push-to-start
TYPE_LIVE_ACTIVITY_END = 4

# log status values
STATUS_PENDING = 1
STATUS_SUCCESS = 2
STATUS_FAIL = 3

_ERR_MSG_MAX = 500
# user_id is optional free text (self-hosted callers may have no official
# account, and the id format of those who do isn't ours to dictate), so we only
# bound its length — an anonymous endpoint must not let a caller stuff megabytes
# of TEXT into the primary DB and the mirrored TEE row.
_USER_ID_MAX = 128

# Log retention: sweep rows older than N days so notify_relay_logs (and its TEE
# mirror) can't grow unbounded — a caller at the push rate limit writes ~170k
# rows/day. The sweep is triggered opportunistically off the log id (every Nth
# insert) rather than a dedicated scheduler, following the repo's write-time
# prune convention (core/store.py's device/track event pruning).
_LOG_PRUNE_EVERY = 500

_CONFIG_COLS = ("auth_token, device_token, user_id, apns_env, disabled, "
                "created_at, updated_at, last_used_at")

_CONFIG_MIRROR_UPSERT = (
    f"INSERT INTO notify_relay_configs ({_CONFIG_COLS}) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (auth_token) DO UPDATE SET "
    "device_token = EXCLUDED.device_token, user_id = EXCLUDED.user_id, "
    "apns_env = EXCLUDED.apns_env, disabled = EXCLUDED.disabled, "
    "created_at = EXCLUDED.created_at, updated_at = EXCLUDED.updated_at, "
    "last_used_at = EXCLUDED.last_used_at"
)


def _new_auth_token() -> str:
    return AUTH_TOKEN_PREFIX + secrets.token_hex(24)


def log_content_max() -> int:
    """How much of the request ``content`` lands in notify_relay_logs (chars).

    Privacy lever for self-hosted callers: their push content transits the
    official relay; this caps what is retained for triage. 0 disables content
    retention entirely (metadata-only logs)."""
    try:
        return max(0, int(os.environ.get("NOTIFY_RELAY_LOG_CONTENT_MAX", "") or 512))
    except (TypeError, ValueError):
        return 512


def _invalid(detail: str) -> tuple[int, dict]:
    # Same envelope the global RequestValidationError handler emits
    # (FRONTEND_ERROR_CONTRACT §三): 400 invalid_payload, never FastAPI's 422.
    return 400, {"error": "invalid_payload", "detail": detail}


# --------------------------------------------------------------------------- #
# Enroll (接口A)
# --------------------------------------------------------------------------- #

def register(payload: dict) -> tuple[int, dict]:
    device_token = str(payload.get("device_token") or "").strip()
    if not _DEVICE_TOKEN_RE.match(device_token):
        return _invalid("device_token must be a 32-200 char hex string")
    # apns_env is preserved on re-enroll unless the caller sends it explicitly,
    # so a re-enroll that omits it doesn't silently flip a sandbox device to the
    # 'production' default (review R4 [6]). Path B (new device) uses the default.
    apns_env_raw = payload.get("apns_env")
    apns_env_explicit = apns_env_raw is not None and str(apns_env_raw).strip() != ""
    apns_env = str(apns_env_raw).strip().lower() if apns_env_explicit else "production"
    if apns_env not in _APNS_ENVS:
        return _invalid("apns_env must be 'sandbox' or 'production'")
    user_id = str(payload.get("user_id") or "").strip() or None
    if user_id and len(user_id) > _USER_ID_MAX:
        return _invalid(f"user_id must be at most {_USER_ID_MAX} characters")
    presented = str(payload.get("auth_token") or "").strip() or None

    result: tuple[int, dict] | None = None
    mirror_row: tuple | None = None
    with db.get_pool().connection() as conn:
        with conn.transaction():
            # Path A — the caller PROVES possession of an existing, non-disabled
            # relay token (the actual bearer credential) and re-binds it to a
            # (possibly rotated) device_token. Possession alone is NOT license to
            # take over a device_token owned by ANOTHER token: we refuse to evict
            # it (review R4 [1] — otherwise any token holder could DoS/hijack an
            # arbitrary known device_token). A disabled/revoked token can't
            # re-enroll either (review R4 [2]).
            if presented and _AUTH_TOKEN_RE.match(presented):
                owns = conn.execute(
                    "SELECT 1 FROM notify_relay_configs "
                    "WHERE auth_token = %s AND disabled = FALSE",
                    (presented,),
                ).fetchone()
                if owns:
                    holder = conn.execute(
                        "SELECT auth_token FROM notify_relay_configs WHERE device_token = %s",
                        (device_token,),
                    ).fetchone()
                    if holder and holder[0] != presented:
                        result = (409, {"error": "device_already_enrolled",
                                        "detail": "device_token is bound to another relay token"})
                    else:
                        updated = conn.execute(
                            "UPDATE notify_relay_configs SET device_token = %s, "
                            "apns_env = COALESCE(%s, apns_env), "
                            "user_id = COALESCE(%s, user_id), updated_at = now() "
                            f"WHERE auth_token = %s RETURNING {_CONFIG_COLS}",
                            (device_token, apns_env if apns_env_explicit else None,
                             user_id, presented),
                        ).fetchone()
                        if updated is not None:
                            result = (200, _register_body(updated, created=False))
                            mirror_row = updated
                        # else: presented row vanished between SELECT and UPDATE
                        # (concurrent re-enroll) — fall through to mint, no 500.
                # not owns / disabled / vanished → fall through

            if result is None:
                # Path B — anonymous, keyed only by the NON-SECRET device_token.
                # A device_token is uploaded to the caller's own backend and can
                # leak, so it must NOT fetch back an existing relay token: enroll
                # a brand-new device (mint + return a token), but on an
                # already-enrolled device DO NOTHING and return WITHOUT a token.
                # Recovering a lost token requires proving possession via Path A
                # (the app keeps it in the Keychain).
                new_token = _new_auth_token()
                row = conn.execute(
                    "INSERT INTO notify_relay_configs "
                    "(auth_token, device_token, user_id, apns_env) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (device_token) DO NOTHING "
                    f"RETURNING {_CONFIG_COLS}",
                    (new_token, device_token, user_id, apns_env),
                ).fetchone()
                if row is None:
                    result = (200, {"device_token": device_token, "created": False,
                                    "already_enrolled": True})
                else:
                    result = (200, _register_body(row, created=True))
                    mirror_row = row
    # Mirror AFTER the primary transaction commits (review R4 [4]): never leave
    # the TEE shadow reflecting a write the primary rolled back at COMMIT.
    if mirror_row is not None:
        _mirror_config_row(mirror_row)
    return result


def _register_body(row: tuple, *, created: bool) -> dict:
    return {
        "auth_token": row[0],
        "device_token": row[1],
        "apns_env": row[3],
        "created": created,
    }


def _mirror_config_row(row: tuple) -> None:
    """Best-effort TEE mirror of one configs row (full-row upsert with the
    primary's values pinned, incl. timestamps). Register no longer evicts other
    rows, so there are no accompanying deletes to mirror."""
    mirror.execute(_CONFIG_MIRROR_UPSERT, tuple(row[:8]))


def get_config(auth_token: str) -> dict | None:
    if not _AUTH_TOKEN_RE.match(auth_token or ""):
        return None
    with db.get_pool().connection() as conn:
        row = conn.execute(
            f"SELECT {_CONFIG_COLS} FROM notify_relay_configs WHERE auth_token = %s",
            (auth_token,),
        ).fetchone()
    if row is None:
        return None
    return {
        "auth_token": row[0],
        "device_token": row[1],
        "user_id": row[2],
        "apns_env": row[3],
        "disabled": row[4],
    }


# --------------------------------------------------------------------------- #
# Push (接口B)
# --------------------------------------------------------------------------- #

def _build_alert(content: dict) -> tuple[dict, str, str]:
    alert: dict = {
        "title": str(content.get("title") or ""),
        "body": str(content.get("body") or ""),
    }
    subtitle = str(content.get("subtitle") or "").strip()
    if subtitle:
        alert["subtitle"] = subtitle
    aps: dict = {"alert": alert, "sound": content.get("sound") or "default"}
    badge = content.get("badge")
    if isinstance(badge, int) and not isinstance(badge, bool):
        aps["badge"] = badge
    payload: dict = {"aps": aps}
    data = content.get("data")
    if isinstance(data, dict):
        # Custom keys ride at the payload top level, like the chat-reply push
        # (push/service.py's "feedling" key).
        for key, value in data.items():
            if key != "aps":
                payload[str(key)] = value
    return payload, "alert", apns.BUNDLE_ID


_LA_TOPIC = f"{apns.BUNDLE_ID}.push-type.liveactivity"


def _relay_content_state(content: dict, *, default_visual_state: str) -> dict:
    # build_content_state reads aiStart/ai_start straight from the payload; the
    # official side has no identity for relay users, so there's no fallback.
    return build_content_state(content, default_visual_state=default_visual_state)


def _build_live_update(content: dict) -> tuple[dict, str, str]:
    alert_title = str(content.get("alert_title") or content.get("title") or "").strip()
    alert_body = str(content.get("alert_body") or content.get("body")
                     or content.get("desc") or "").strip()
    stale = content.get("stale_date")
    # Envelope shape shared with push_live_activity_dict via build_live_activity_aps
    # (single source of the wire keys iOS Codable reads). Non-empty alert text
    # makes the update user-visible.
    aps = build_live_activity_aps(
        _relay_content_state(content, default_visual_state="reply"),
        event="update",
        alert={"title": alert_title or "IO", "body": alert_body[:240]},
        stale_date=int(stale) if isinstance(stale, (int, float)) else None,
    )
    return aps, "liveactivity", _LA_TOPIC


def _build_live_start(content: dict) -> tuple[dict, str, str]:
    attributes = content.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {"activityId": f"la_{uuid.uuid4().hex[:8]}"}
    title = str(content.get("alert_title") or content.get("title") or "").strip()
    body = str(content.get("alert_body") or content.get("body")
               or content.get("desc") or "").strip()
    aps = build_live_activity_aps(
        _relay_content_state(content, default_visual_state="reply"),
        event="start",
        alert={"title": title or "IO", "body": (body or "Live Activity started")[:240]},
        attributes_type=str(content.get("attributes_type")
                            or content.get("attributes-type") or "ScreenActivityAttributes"),
        attributes=attributes,
    )
    return aps, "liveactivity", _LA_TOPIC


def _build_live_end(content: dict) -> tuple[dict, str, str]:
    dismissal = content.get("dismissal_date")
    aps = build_live_activity_aps(
        _relay_content_state(content, default_visual_state="default"),
        event="end",
        dismissal_date=int(dismissal) if isinstance(dismissal, (int, float)) else int(time.time()),
    )
    return aps, "liveactivity", _LA_TOPIC


_BUILDERS = {
    TYPE_ALERT: _build_alert,
    TYPE_LIVE_ACTIVITY_UPDATE: _build_live_update,
    TYPE_LIVE_ACTIVITY_START: _build_live_start,
    TYPE_LIVE_ACTIVITY_END: _build_live_end,
}


def push(config: dict, payload: dict) -> tuple[int, dict]:
    push_type = payload.get("type")
    if push_type not in _BUILDERS:
        return _invalid("type must be 1 (alert), 2 (live-activity update), "
                        "3 (live-activity start) or 4 (live-activity end)")

    target_token = str(payload.get("token") or "").strip()
    if push_type == TYPE_ALERT:
        # Alert pushes are scoped to the ENROLLED device only: the relay token
        # authorizes pushing to its own device, not to arbitrary APNs tokens —
        # otherwise a leaked relay token + a known device token would let a
        # caller alert-push to someone else's device under the official key.
        # (Types 2-4 stay pass-through by design: Live Activity tokens rotate
        # per activity and only the caller's own app uploads them to the
        # caller's own backend, so the same hijack shape doesn't apply.)
        if target_token and target_token.lower() != str(config["device_token"]).lower():
            return _invalid("type 1 always pushes to the enrolled device; "
                            "omit token or pass the enrolled device_token")
        target_token = config["device_token"]
    if not target_token:
        return _invalid("token is required for live-activity push types "
                        "(activity tokens rotate; pass the latest one)")
    if not _DEVICE_TOKEN_RE.match(target_token):
        return _invalid("token must be a 32-200 char hex string")

    apns_env = str(payload.get("apns_env") or "").strip().lower() or config["apns_env"]
    if apns_env not in _APNS_ENVS:
        return _invalid("apns_env must be 'sandbox' or 'production'")

    content = payload.get("content")
    if not isinstance(content, dict):
        return _invalid("content must be an object")

    if not apns.APNS_KEY:
        # Surface official-side misconfiguration/outage to the self-hosted
        # caller instead of quietly logging a push that never left the box.
        return 503, {"error": "service_unavailable",
                     "detail": "relay APNs key is not configured"}

    apns_payload, wire_push_type, topic = _BUILDERS[push_type](content)

    log_id = _log_pending(config["auth_token"], push_type, target_token, apns_env, content)

    try:
        result = apns._send_apns(
            target_token, apns_payload, wire_push_type, topic, preferred_env=apns_env)
    except Exception as exc:  # noqa: BLE001
        # `APNS_KEY` truthy but not a usable ES256 key (bad PEM / decoded to
        # garbage): _make_apns_jwt() raises OUTSIDE _send_apns_once's try, so
        # it propagates. Honor the (status, body) contract — a 500 traceback
        # to the caller and a log row stuck at STATUS_PENDING forever are both
        # wrong. Close the log as failed and report the same 503 as a missing
        # key (both are official-side "can't sign right now").
        _log_finish(log_id, delivered=False,
                    err_msg=f"apns_sign_error: {exc}"[:_ERR_MSG_MAX], apns_env=apns_env)
        return 503, {"error": "service_unavailable",
                     "detail": "relay APNs key is not usable"}

    delivered = result.get("status") == "delivered"
    reason = "" if delivered else str(
        result.get("reason") or result.get("status") or "error")[:_ERR_MSG_MAX]
    final_env = str(result.get("apns_env") or apns_env)
    _log_finish(log_id, delivered=delivered, err_msg=reason, apns_env=final_env,
                touch_auth_token=config["auth_token"] if delivered else None)
    if log_id and log_id % _LOG_PRUNE_EVERY == 0:
        try:
            prune_logs()
        except Exception:  # noqa: BLE001 — retention is best-effort, never blocks a push
            pass

    body: dict = {
        "status": "delivered" if delivered else "error",
        "apns_env": final_env,
        "log_id": log_id,
    }
    if not delivered:
        body["reason"] = reason
        if result.get("code") is not None:
            body["error_code"] = result.get("code")
    return 200, body


def _log_pending(auth_token: str, push_type: int, target_token: str,
                 apns_env: str, content: dict) -> int | None:
    limit = log_content_max()
    content_text: str | None = None
    if limit > 0:
        content_text = json.dumps(content, ensure_ascii=False)[:limit]
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "INSERT INTO notify_relay_logs "
            "(auth_token, push_type, target_token, apns_env, status, content) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "RETURNING id, created_at, updated_at",
            (auth_token, push_type, target_token, apns_env, STATUS_PENDING, content_text),
        ).fetchone()
    log_id, created_at, updated_at = row
    mirror.execute(
        "INSERT INTO notify_relay_logs "
        "(id, auth_token, push_type, target_token, apns_env, status, content, "
        "created_at, updated_at) "
        "OVERRIDING SYSTEM VALUE VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (id) DO NOTHING",
        (log_id, auth_token, push_type, target_token, apns_env, STATUS_PENDING,
         content_text, created_at, updated_at),
    )
    return log_id


def _log_finish(log_id: int | None, *, delivered: bool, err_msg: str, apns_env: str,
                touch_auth_token: str | None = None) -> None:
    """Close a pending log row, and (on delivery) refresh the config's
    last_used_at — both in ONE primary connection and ONE batched TEE mirror
    round-trip, instead of a separate connection+mirror per write (review R4
    [8]). Timestamps are pinned from the primary's RETURNING into the mirror so
    the two DBs stay byte-identical (no per-DB now() drift polluting verify)."""
    if log_id is None:
        return
    status = STATUS_SUCCESS if delivered else STATUS_FAIL
    mirror_stmts: list[tuple[str, tuple]] = []
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "UPDATE notify_relay_logs SET status = %s, err_msg = %s, "
            "apns_env = %s, updated_at = now() WHERE id = %s RETURNING updated_at",
            (status, err_msg or None, apns_env, log_id),
        ).fetchone()
        if row is not None:
            mirror_stmts.append((
                "UPDATE notify_relay_logs SET status = %s, err_msg = %s, "
                "apns_env = %s, updated_at = %s WHERE id = %s",
                (status, err_msg or None, apns_env, row[0], log_id)))
        if touch_auth_token:
            used = conn.execute(
                "UPDATE notify_relay_configs SET last_used_at = now() "
                "WHERE auth_token = %s RETURNING last_used_at",
                (touch_auth_token,),
            ).fetchone()
            if used is not None:
                mirror_stmts.append((
                    "UPDATE notify_relay_configs SET last_used_at = %s WHERE auth_token = %s",
                    (used[0], touch_auth_token)))
    if mirror_stmts:
        mirror.execute_many(mirror_stmts)


def log_retention_days() -> int:
    """Days of notify_relay_logs history to keep; 0 disables the sweep."""
    try:
        return max(0, int(os.environ.get("NOTIFY_RELAY_LOG_RETENTION_DAYS", "") or 30))
    except (TypeError, ValueError):
        return 30


def prune_logs() -> int:
    """Delete notify_relay_logs rows older than the retention window (primary +
    TEE mirror). Best-effort — walks the created_at index. Returns rows removed
    from the primary."""
    days = log_retention_days()
    if days <= 0:
        return 0
    # Compute the cutoff once on the PRIMARY clock and delete by that fixed
    # timestamp on both DBs — never re-evaluate now() on the TEE connection
    # (review R4 [5]): clock skew there would delete rows the primary still
    # holds, and the reconciler would keep re-copying them (never-converging
    # churn + verify noise).
    sql = "DELETE FROM notify_relay_logs WHERE created_at < %s"
    with db.get_pool().connection() as conn:
        cutoff = conn.execute(
            "SELECT now() - make_interval(days => %s)", (days,)).fetchone()[0]
        deleted = conn.execute(sql, (cutoff,)).rowcount
    mirror.execute(sql, (cutoff,))
    return deleted
