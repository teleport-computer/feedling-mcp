"""Memory HTTP surface: /v1/memory/*."""

import json
import os
import re
import time
import uuid
from datetime import date, datetime

from flask import jsonify, request

import db
from core.store import UserStore
from flask import Blueprint, Response
import threading

from accounts import auth
from accounts import registry
from accounts import runtime_auth
from bootstrap import gates as boot_gates
from identity import service as identity_service
from memory import actions as memory_actions_mod
from memory import service as memory_service
import memory_readside_core

bp = Blueprint("memory", __name__)


# Default only. Effective readside windows are parsed in memory_readside_core so
# FEEDLING_MEMORY_READSIDE_LIMIT=0 can mean "full window up to HARD_MAX".
_MEMORY_READSIDE_LIMIT = memory_readside_core.MEMORY_READSIDE_DEFAULT_LIMIT
_MEMORY_READSIDE_SALIENCE_WEIGHT = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}
_MEMORY_READSIDE_INACTIVE_STATUSES = {
    "archived",
    "deleted",
    "superseded",
}


def _memory_readside_status(moment: dict) -> str:
    return str(moment.get("status") or "active").strip().lower() or "active"


def _memory_readside_salience(moment: dict) -> str:
    salience = str(moment.get("salience") or "medium").strip().lower()
    return salience if salience in _MEMORY_READSIDE_SALIENCE_WEIGHT else "medium"


def _memory_readside_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _memory_readside_time_key(moment: dict) -> str:
    for key in ("last_active", "updated_at", "occurred_at", "created_at"):
        value = str(moment.get(key) or "").strip()
        if value:
            return value
    return ""


def _memory_readside_available(
    moment: dict,
    owner_user_id: str,
    *,
    include_archived: bool = False,
    include_superseded: bool = False,
) -> bool:
    if not isinstance(moment, dict):
        return False
    if moment.get("owner_user_id") != owner_user_id:
        return False
    if moment.get("visibility") == "local_only":
        return False
    if not moment.get("K_enclave"):
        return False
    status = _memory_readside_status(moment)
    if status == "superseded" and not include_superseded:
        return False
    if status in {"archived", "deleted"} and not include_archived:
        return False
    if status in _MEMORY_READSIDE_INACTIVE_STATUSES and status not in {"archived", "superseded"}:
        return False
    if memory_service._memory_is_archived(moment) and not include_archived:
        return False
    return True


def _memory_readside_score(moment: dict) -> float:
    salience_score = _MEMORY_READSIDE_SALIENCE_WEIGHT.get(_memory_readside_salience(moment), 2)
    importance = _memory_readside_float(moment.get("importance"), 0.5)
    open_bonus = 1.0 if moment.get("is_open_thread") is True else 0.0
    return round(open_bonus + salience_score + importance, 4)


def _memory_readside_candidates(moments: list, owner_user_id: str, *, limit: int | None = None) -> list[dict]:
    candidates = [
        dict(moment, score=_memory_readside_score(moment))
        for moment in moments
        if _memory_readside_available(moment, owner_user_id)
    ]
    candidates.sort(
        key=lambda m: (
            1 if m.get("is_open_thread") is True else 0,
            _MEMORY_READSIDE_SALIENCE_WEIGHT.get(_memory_readside_salience(m), 2),
            _memory_readside_float(m.get("importance"), 0.5),
            _memory_readside_time_key(m),
            str(m.get("id") or ""),
        ),
        reverse=True,
    )
    return candidates[:memory_readside_core.effective_readside_limit(limit)]


def _memory_readside_post_enclave(
    api_key: str | None,
    candidates: list[dict],
    *,
    operation: str,
    payload: dict | None = None,
) -> dict:
    return memory_readside_core.post_enclave_readside(
        api_key,
        candidates,
        operation=operation,
        payload=payload,
    )


@bp.route("/v1/memory/index", methods=["POST"])
def memory_index():
    store = auth.require_user()
    api_key = auth._extract_api_key()
    payload = request.get_json(silent=True) or {}
    try:
        requested_limit = memory_readside_core.effective_readside_limit(payload.get("limit"))
    except ValueError:
        return jsonify({"error": "invalid limit"}), 400
    try:
        response = memory_readside_core.memory_index_core(
            store,
            api_key,
            {**payload, "limit": requested_limit},
            post_enclave=_memory_readside_post_enclave,
        )
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    return jsonify(response)


@bp.route("/v1/memory/fetch", methods=["POST"])
def memory_fetch():
    store = auth.require_user()
    api_key = auth._extract_api_key()
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids")
    if not isinstance(ids, list) or any(not isinstance(mid, str) or not mid.strip() for mid in ids):
        return jsonify({"error": "ids must be a list of non-empty strings"}), 400
    try:
        response = memory_readside_core.memory_fetch_core(
            store,
            api_key,
            payload,
            post_enclave=_memory_readside_post_enclave,
        )
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(response)


def _terms_from_memory_items(items: list[dict]) -> tuple[list[str], list[str]]:
    buckets: list[str] = []
    threads: list[str] = []

    def add_unique(target: list[str], value) -> None:
        text = str(value or "").strip()[:120]
        if text and text not in target:
            target.append(text)

    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "active").strip().lower() != "active":
            continue
        add_unique(buckets, item.get("bucket"))
        for thread in item.get("threads") or []:
            add_unique(threads, thread)
    return sorted(buckets), sorted(threads)


def _memory_existing_terms(store: UserStore, api_key: str | None) -> tuple[list[str], list[str]]:
    try:
        response = memory_readside_core.memory_index_core(
            store,
            api_key,
            {"limit": 0},
            post_enclave=_memory_readside_post_enclave,
        )
        items = response.get("items") if isinstance(response.get("items"), list) else []
        buckets, threads = _terms_from_memory_items(items)
        if buckets or threads:
            return buckets, threads
    except Exception:
        pass
    return _terms_from_memory_items(memory_service._load_moments(store))


@bp.route("/v1/memory/buckets", methods=["GET"])
def memory_buckets():
    store = auth.require_user()
    api_key = auth._extract_api_key()
    buckets, _threads = _memory_existing_terms(store, api_key)
    return jsonify({"buckets": buckets})


@bp.route("/v1/memory/threads", methods=["GET"])
def memory_threads():
    store = auth.require_user()
    api_key = auth._extract_api_key()
    _buckets, threads = _memory_existing_terms(store, api_key)
    return jsonify({"threads": threads})


@bp.route("/v1/memory/actions", methods=["POST"])
def memory_actions():
    store = auth.require_user()
    runtime_auth.authorize_scope("memory")  # slice 4: token must carry the memory scope
    api_key = auth._extract_api_key()
    payload = request.get_json(silent=True) or {}
    actions = payload.get("actions")
    if actions is None and isinstance(payload.get("action"), dict):
        actions = [payload["action"]]
    elif actions is None and (payload.get("type") or payload.get("action")):
        actions = [payload]
    if not isinstance(actions, list):
        return jsonify({"error": "actions required"}), 400
    body, status = memory_actions_mod._execute_memory_actions(store, api_key, actions)
    return jsonify(body), status


@bp.route("/v1/memory/list", methods=["GET"])
def memory_list():
    store = auth.require_user()
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    since = request.args.get("since", "")

    include_archived = str(request.args.get("include_archived") or "").lower() in {"1", "true", "yes"}
    moments = memory_service._load_moments(store)
    if not include_archived:
        moments = memory_service._active_memory_moments(moments)
    if since:
        moments = [m for m in moments if m.get("occurred_at", "") >= since]
    moments = sorted(moments, key=lambda m: m.get("occurred_at", ""), reverse=True)
    return jsonify({"moments": moments[:limit], "total": len(moments)})


@bp.route("/v1/memory/get", methods=["GET"])
def memory_get():
    store = auth.require_user()
    moment_id = request.args.get("id", "")
    if not moment_id:
        return jsonify({"error": "id required"}), 400
    moments = memory_service._load_moments(store)
    for m in moments:
        if m.get("id") == moment_id:
            return jsonify({"moment": m})
    return jsonify({"error": "not_found"}), 404


@bp.route("/v1/memory/add", methods=["POST"])
def memory_add():
    """Add a memory moment as a v1 envelope.

    body_ct wraps the user-visible payload (title/description/her_quote/…).
    Plaintext envelope metadata the server uses for indexing + gating:
      - occurred_at (mandatory, ISO 8601)
      - source (chat/bootstrap/live_conversation/user_initiated)
      - type (one of memory_service.MEMORY_TYPES; mandatory)
      - anchor_memory_ids (required for insight + reflection)

    Type-specific gates (see memory_service.MEMORY_TYPES module commentary):
      - insight: anchor_memory_ids ≥1 referencing existing memories
      - reflection: anchor_memory_ids ≥2 + per-tier time cap
    """
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    envelope = payload.get("envelope")
    now = datetime.now().isoformat()

    if envelope is None:
        return jsonify({"error": "envelope required (v1 encryption is mandatory)"}), 400

    required = ["body_ct", "nonce", "K_user", "visibility", "owner_user_id"]
    missing = [f for f in required if not envelope.get(f)]
    if missing:
        return jsonify({"error": f"envelope missing fields: {missing}"}), 400
    if envelope["visibility"] not in ("shared", "local_only"):
        return jsonify({"error": "envelope.visibility must be 'shared' or 'local_only'"}), 400
    if envelope["visibility"] == "shared" and not envelope.get("K_enclave"):
        return jsonify({"error": "envelope with visibility=shared requires K_enclave"}), 400
    occurred_at = (envelope.get("occurred_at") or "").strip()
    if not occurred_at:
        return jsonify({"error": "occurred_at required (plaintext metadata for ordering)"}), 400
    if envelope["owner_user_id"] != store.user_id:
        return jsonify({"error": "envelope.owner_user_id does not match caller"}), 403

    mem_type = (envelope.get("type") or "").strip()
    if not mem_type:
        return jsonify({
            "error": "type_required",
            "allowed": list(memory_service.MEMORY_TYPES),
            "required": (
                "type is mandatory and must be one of moment/quote/fact/event/"
                "insight/reflection. See skill 'Memory types' section."
            ),
        }), 400
    if mem_type not in memory_service.MEMORY_TYPES:
        return jsonify({
            "error": "type_invalid",
            "got": mem_type,
            "allowed": list(memory_service.MEMORY_TYPES),
        }), 400

    moments = memory_service._load_moments(store)
    anchor_ids = envelope.get("anchor_memory_ids") or []

    if mem_type == "insight":
        if not anchor_ids:
            return jsonify({
                "error": "insight_requires_anchor",
                "required": (
                    "insight must reference ≥1 prior memory (anchor_memory_ids). "
                    "An insight is the agent's understanding of the user grounded in "
                    "concrete cards; if you can't point to a card, write fact/event first."
                ),
            }), 400
        ok, err = memory_service._validate_anchor_ids(moments, anchor_ids, store.user_id)
        if not ok:
            return jsonify(err), 400

    if mem_type == "reflection":
        if not isinstance(anchor_ids, list) or len(anchor_ids) < 2:
            return jsonify({
                "error": "reflection_requires_substrate",
                "required": (
                    "reflection must reference ≥2 prior memories (anchor_memory_ids). "
                    "A reflection is the agent's standalone thinking; it needs at "
                    "least 2 pieces of substrate to count as thought, not vibes."
                ),
            }), 400
        ok, err = memory_service._validate_anchor_ids(moments, anchor_ids, store.user_id)
        if not ok:
            return jsonify(err), 400
        days = identity_service._relationship_age_days(store)
        ok, err = memory_service._reflection_time_cap_ok(moments, days)
        if not ok:
            return jsonify(err), 429  # rate-limit semantics

    moment = {
        "v": 1,
        "id": envelope.get("id") or f"mom_{uuid.uuid4().hex[:12]}",
        "type": mem_type,
        "occurred_at": occurred_at,
        "created_at": now,
        "source": (envelope.get("source") or "live_conversation").strip(),
        "body_ct": envelope["body_ct"],
        "nonce": envelope["nonce"],
        "K_user": envelope["K_user"],
        "enclave_pk_fpr": envelope.get("enclave_pk_fpr", ""),
        "visibility": envelope["visibility"],
        "owner_user_id": envelope["owner_user_id"],
    }
    if envelope.get("K_enclave"):
        moment["K_enclave"] = envelope["K_enclave"]
    if anchor_ids:
        moment["anchor_memory_ids"] = list(anchor_ids)
    moments.append(moment)
    memory_service._save_moments(store, moments)
    boot_gates._log_bootstrap_event(store, "memory_moment_added_v1", success=True)
    print(f"[memory:{store.user_id}] added v1 type={mem_type} id={moment['id']} "
          f"visibility={envelope['visibility']} anchors={len(anchor_ids)}")
    return jsonify({"status": "created", "moment": moment, "v": 1}), 201


@bp.route("/v1/memory/retype", methods=["POST"])
def memory_retype():
    """Change an existing memory's `type` (and anchor_memory_ids when moving
    into insight/reflection). Used when the agent decides on reflection
    that an older memory was misclassified.

    Time cap on reflection is waived for retypes — this is recategorization,
    not new substrate, so the cadence gate doesn't apply. Substrate gate
    (≥1 anchor for insight, ≥2 for reflection) is still enforced.

    Body: {"id": "...", "type": "...", "anchor_memory_ids": [...] (optional)}
    """
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    memory_id = (payload.get("id") or "").strip()
    new_type = (payload.get("type") or "").strip()
    if not memory_id:
        return jsonify({"error": "id required"}), 400
    if new_type not in memory_service.MEMORY_TYPES:
        return jsonify({
            "error": "type_invalid",
            "got": new_type,
            "allowed": list(memory_service.MEMORY_TYPES),
        }), 400

    moments = memory_service._load_moments(store)
    target_idx = None
    for i, m in enumerate(moments):
        if isinstance(m, dict) and m.get("id") == memory_id:
            target_idx = i
            break
    if target_idx is None:
        return jsonify({"error": "not_found"}), 404

    target = moments[target_idx]
    if target.get("owner_user_id") != store.user_id:
        return jsonify({"error": "not_owned"}), 403

    anchor_ids = payload.get("anchor_memory_ids") or []
    if new_type in ("insight", "reflection"):
        minimum = 1 if new_type == "insight" else 2
        if not isinstance(anchor_ids, list) or len(anchor_ids) < minimum:
            return jsonify({
                "error": f"{new_type}_requires_anchor",
                "min_anchors": minimum,
                "required": (
                    f"Retyping into {new_type} requires ≥{minimum} anchor_memory_ids."
                ),
            }), 400
        # Don't allow self-reference.
        if memory_id in anchor_ids:
            return jsonify({
                "error": "anchor_self_reference",
                "required": "A memory cannot anchor itself.",
            }), 400
        ok, err = memory_service._validate_anchor_ids(moments, anchor_ids, store.user_id)
        if not ok:
            return jsonify(err), 400
        target["anchor_memory_ids"] = list(anchor_ids)
    else:
        # Demoting away from insight/reflection drops anchors.
        target.pop("anchor_memory_ids", None)

    target["type"] = new_type
    target["retyped_at"] = datetime.now().isoformat()
    moments[target_idx] = target
    memory_service._save_moments(store, moments)
    print(f"[memory:{store.user_id}] retyped id={memory_id} → {new_type} "
          f"anchors={len(anchor_ids)}")
    return jsonify({"status": "retyped", "moment": target})


@bp.route("/v1/memory/delete", methods=["DELETE"])
def memory_delete():
    store = auth.require_user()
    moment_id = request.args.get("id", "")
    if not moment_id:
        return jsonify({"error": "id required"}), 400
    moments = memory_service._load_moments(store)
    new_moments = [m for m in moments if m.get("id") != moment_id]
    if len(new_moments) == len(moments):
        return jsonify({"error": "not_found"}), 404
    memory_service._save_moments(store, new_moments)
    print(f"[memory:{store.user_id}] deleted: {moment_id}")
    return jsonify({"status": "deleted"})



@bp.route("/v1/memory/verify", methods=["GET"])
def memory_verify():
    """Check memory garden state against per-tab floors + quality signals.

    Returns:
      {
        counts: {story, about_me, ta_thinking, total},
        floors: {story, about_me, ta_thinking, total},
        below_floor: {story: bool, about_me: bool, ta_thinking: bool},
        relationship_days: int,
        issues: [...],
        suggestions: [...],
        passing: bool,            # Story + About me floors met (TA 在想 advisory)
        passing_full: bool,       # All three tab floors met (target, not gate)
      }

    Agent should call this after Pass 3 to decide whether to sweep again.
    `passing` is the bootstrap gate (Story + About me); `passing_full` is
    the aspirational target including TA 在想.
    """
    store = auth.require_user()
    moments = memory_service._load_moments(store)
    counts = memory_service._count_by_tab(moments)
    days = identity_service._relationship_age_days(store)
    floors = memory_service._per_tab_floors_for_days(days)

    issues = []
    suggestions = []

    below_floor = {
        "story":       counts["story"]       < floors["story"],
        "about_me":    counts["about_me"]    < floors["about_me"],
        "ta_thinking": counts["ta_thinking"] < floors["ta_thinking"],
    }

    # Time distribution — server-visible plaintext metadata
    occurred_ts = []
    for m in moments:
        if not isinstance(m, dict):
            continue
        occ = m.get("occurred_at", "")
        if occ:
            try:
                dt = datetime.fromisoformat(occ.replace("Z", "+00:00"))
                if dt.tzinfo:
                    dt = dt.replace(tzinfo=None)
                occurred_ts.append(dt)
            except Exception:
                pass
    if occurred_ts and len(occurred_ts) >= 5:
        # All within last 7 days = suspicious "recent only" sweep
        spread_days = (max(occurred_ts) - min(occurred_ts)).days
        if spread_days < 7 and days > 14:
            issues.append({
                "type": "narrow_time_window",
                "spread_days": spread_days,
                "relationship_days": days,
            })
            suggestions.append(
                f"All {len(occurred_ts)} of your cards are within {spread_days} days of each other, "
                f"but your relationship is {days} days old. Sweep older history — "
                "you missed at least 80% of the relationship's span."
            )

    # Per-tab suggestions: be specific about which tab is underfilled and
    # which types feed it. The skill maps types→tabs but reminding helps
    # agents that haven't re-read the skill mid-bootstrap.
    if below_floor["story"]:
        suggestions.append(
            f"Story tab: {counts['story']}/{floors['story']} — write more "
            "moment/quote memories (the things between you and the user). "
            "feedling_identity_init will 409 until Story + About me floors are met."
        )
    if below_floor["about_me"]:
        suggestions.append(
            f"About me tab: {counts['about_me']}/{floors['about_me']} — this is the "
            "density layer. Sweep for facts (preferences, relationships, dates, habits) "
            "and events (specific things that happened in the user's life)."
        )
    if below_floor["ta_thinking"]:
        suggestions.append(
            f"TA 在想 tab: {counts['ta_thinking']}/{floors['ta_thinking']} — write "
            "insights (your understanding of the user, each anchored to ≥1 prior memory) "
            "and reflections (your standalone thinking, ≥2 anchors). This tab is not "
            "blocking for identity_init but it's how the relationship feels reciprocal."
        )

    # passing semantics: identity_init gate = Story + About me only.
    # passing_full = all three tabs at floor.
    passing = (not below_floor["story"]) and (not below_floor["about_me"]) and not issues
    passing_full = passing and (not below_floor["ta_thinking"])

    resp = {
        "counts": counts,
        "floors": floors,
        "below_floor": below_floor,
        "relationship_days": days,
        "issues": issues,
        "suggestions": suggestions,
        "passing": passing,
        "passing_full": passing_full,
        # Backwards-compatible flat fields — iOS / older tests may still
        # read these. The per-tab fields above are the new source of truth.
        "count": counts["total"],
        "floor": floors["total"],
    }
    archive_language = registry._get_user_archive_language(store.user_id)
    if archive_language:
        # Defense layer 2: agent reads this every time it verifies and
        # treats it as authoritative — overrides anything it might
        # otherwise infer from recent chat language drift. Skill rule
        # "Lock the Memory Garden language" consumes this field.
        resp["archive_language"] = archive_language
    return jsonify(resp)
