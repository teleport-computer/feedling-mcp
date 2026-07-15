"""Framework-neutral memory operations (ASGI-migration plan §5.3).

A pure relocation of the Flask ``/v1/memory/*`` route bodies so both the Flask
adapter (``memory.routes``) and the native FastAPI router
(``memory.routes_asgi``) share one implementation and return byte-identical
responses.

E2E boundary (unchanged): memory ``body_ct`` fields are v1 E2E envelopes. The
server NEVER decrypts them here. Read/write store operations stay plaintext-free;
the readside (index/fetch/buckets/threads) and the migration decrypt
(legacy_batch) forward the caller's credential (api key OR runtime token) to the
enclave, which owns decryption. These functions take already-parsed params + the
store + the credential (or a pre-bound ``post_enclave`` callable) as arguments —
they never read ``flask.request`` — so no new server-side plaintext is ever
introduced here.

Each function returns ``(body_dict, status_int)`` (or ``(body_dict, status)`` via
the action executor) so the framework adapter just serializes it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import db
import debug_trace
from accounts import registry
from bootstrap import gates as boot_gates
from identity import service as identity_service
from memory import actions as memory_actions_mod
from memory import migration as memory_migration
from memory import service as memory_service
import memory_readside_core


# --------------------------------------------------------------------------- #
# readside term helpers (buckets / threads)
# --------------------------------------------------------------------------- #

def _terms_from_memory_items(items: list) -> tuple[list[str], list[str]]:
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


def existing_terms(store, api_key, *, post_enclave) -> tuple[list[str], list[str]]:
    try:
        response = memory_readside_core.memory_index_core(
            store,
            api_key,
            {"limit": 0},
            post_enclave=post_enclave,
        )
        items = response.get("items") if isinstance(response.get("items"), list) else []
        buckets, threads = _terms_from_memory_items(items)
        if buckets or threads:
            return buckets, threads
    except Exception:
        pass
    return _terms_from_memory_items(memory_service._load_moments(store))


def existing_terms_via_api_key(store, api_key) -> tuple[list[str], list[str]]:
    """``hosted/turn.py`` path: derive existing bucket/thread terms with no Flask
    request context (the deleted ``memory.routes._memory_existing_terms`` bound a
    request-reading closure). The hosted caller supplies an ``api_key``, so bind a
    ``post_enclave`` that forwards it (no runtime token)."""
    def _post(api_key_, candidates, *, operation, payload=None):
        return memory_readside_core.post_enclave_readside(
            api_key_, candidates, operation=operation, payload=payload, runtime_token=None,
        )
    return existing_terms(store, api_key, post_enclave=_post)


# --------------------------------------------------------------------------- #
# readside routes
# --------------------------------------------------------------------------- #

def index(store, api_key, payload: dict, *, post_enclave) -> tuple[dict, int]:
    try:
        requested_limit = memory_readside_core.effective_readside_limit(payload.get("limit"))
    except ValueError:
        return {"error": "invalid limit"}, 400
    try:
        response = memory_readside_core.memory_index_core(
            store,
            api_key,
            {**payload, "limit": requested_limit},
            post_enclave=post_enclave,
        )
    except RuntimeError as e:
        debug_trace.trace_event(
            store, subsystem="memory", type="memory.index.called", actor="agent",
            status="failed", summary="index failed", detail={"reason": str(e)[:80]})
        return {"error": str(e)}, 503
    _items = response.get("items") if isinstance(response.get("items"), list) else []
    debug_trace.trace_event(
        store, subsystem="memory", type="memory.index.called", actor="agent",
        summary=f"index returned {len(_items)} items",
        detail={"counts": {"items": len(_items), "limit": requested_limit}},
    )
    return response, 200


def fetch(store, api_key, payload: dict, *, post_enclave) -> tuple[dict, int]:
    ids = payload.get("ids")
    if not isinstance(ids, list) or any(not isinstance(mid, str) or not mid.strip() for mid in ids):
        return {"error": "ids must be a list of non-empty strings"}, 400
    try:
        response = memory_readside_core.memory_fetch_core(
            store,
            api_key,
            payload,
            post_enclave=post_enclave,
        )
    except RuntimeError as e:
        debug_trace.trace_event(
            store, subsystem="memory", type="memory.fetch.called", actor="agent",
            status="failed", summary="fetch failed", detail={"reason": str(e)[:80]})
        return {"error": str(e)}, 503
    except ValueError as e:
        debug_trace.trace_event(
            store, subsystem="memory", type="memory.fetch.called", actor="agent",
            status="failed", summary="fetch failed", detail={"reason": str(e)[:80]})
        return {"error": str(e)}, 400
    _items = response.get("items") if isinstance(response.get("items"), list) else []
    debug_trace.trace_event(
        store, subsystem="memory", type="memory.fetch.called", actor="agent",
        summary=f"fetched {len(_items)}/{len(ids)} cards",
        detail={"counts": {"requested": len(ids), "fetched": len(_items)}, "ids": ids[:20]},
    )
    return response, 200


def buckets(store, api_key, *, post_enclave) -> tuple[dict, int]:
    bkts, _threads = existing_terms(store, api_key, post_enclave=post_enclave)
    return {"buckets": bkts}, 200


def threads(store, api_key, *, post_enclave) -> tuple[dict, int]:
    _buckets, thrds = existing_terms(store, api_key, post_enclave=post_enclave)
    return {"threads": thrds}, 200


# --------------------------------------------------------------------------- #
# write actions
# --------------------------------------------------------------------------- #

def actions(store, api_key, payload: dict) -> tuple[dict, int]:
    acts = payload.get("actions")
    if acts is None and isinstance(payload.get("action"), dict):
        acts = [payload["action"]]
    elif acts is None and (payload.get("type") or payload.get("action")):
        acts = [payload]
    if not isinstance(acts, list):
        return {"error": "actions required"}, 400
    body, status = memory_actions_mod._execute_memory_actions(store, api_key, acts)
    _types = [str(a.get("type") or a.get("action") or "") for a in acts if isinstance(a, dict)][:20]
    _results = body.get("results") if isinstance(body.get("results"), list) else []
    debug_trace.trace_event(
        store, subsystem="memory", type="memory.write.actions", actor="agent",
        status="ok" if status < 400 else "failed",
        summary=f"{len(acts)} action(s): " + ",".join(t.split('.')[-1] for t in _types)[:80],
        detail={
            "counts": {"actions": len(acts)},
            "types": _types,
            "results": [str(r.get("skipped") or r.get("status") or "") for r in _results][:20],
        },
    )
    return body, status


# --------------------------------------------------------------------------- #
# migration state + legacy batch
# --------------------------------------------------------------------------- #

def migration_state_get(store) -> tuple[dict, int]:
    state = db.get_blob(store.user_id, memory_migration.MIGRATION_STATE_BLOB)
    return {"state": state or memory_migration.initial_state()}, 200


def migration_state_post(store, payload: dict) -> tuple[dict, int]:
    try:
        migrated = int(payload.get("migrated") or 0)
        legacy_remaining = int(payload.get("legacy_remaining") or 0)
    except (TypeError, ValueError):
        return {"error": "migrated/legacy_remaining must be ints"}, 400
    failed_raw = payload.get("failed_ids")
    failed_ids = [str(i) for i in failed_raw if str(i or "").strip()] if isinstance(failed_raw, list) else []
    current = db.get_blob(store.user_id, memory_migration.MIGRATION_STATE_BLOB)
    # A11: count this round's failures per card BEFORE advancing the state machine, so a
    # card that just hit the cap is already 'skipped' and won't keep 'pending' alive.
    if failed_ids:
        current = memory_migration.bump_attempts(current, failed_ids)
    new_state = memory_migration.next_state(current, migrated=migrated, legacy_remaining=legacy_remaining)
    db.set_blob(store.user_id, memory_migration.MIGRATION_STATE_BLOB, new_state)
    return {"state": new_state}, 200


def legacy_batch(store, api_key, runtime_token: str, payload: dict) -> tuple[dict, int]:
    try:
        batch_size = max(1, min(int(payload.get("batch_size") or memory_migration.DEFAULT_MIGRATE_BATCH), 50))
    except (TypeError, ValueError):
        batch_size = memory_migration.DEFAULT_MIGRATE_BATCH
    moments = memory_service._active_memory_moments(memory_service._load_moments(store))
    decrypted: list[tuple[dict, dict]] = []
    for m in moments:
        if not isinstance(m, dict) or m.get("visibility") == "local_only":
            continue
        inner, _err = memory_actions_mod._memory_plain_from_envelope(m, api_key, runtime_token=runtime_token)
        if isinstance(inner, dict):
            decrypted.append((m, inner))
    # A11: drop cards that hit the per-card attempt cap so they're never re-selected
    # and legacy_remaining can reach 0 (status → done). They stay legacy + readable.
    state = db.get_blob(store.user_id, memory_migration.MIGRATION_STATE_BLOB)
    skip = memory_migration.capped_ids(state)
    batch = memory_migration.select_legacy_batch(decrypted, batch_size=batch_size, exclude_ids=skip)
    return {"batch": batch, "legacy_remaining": memory_migration.count_legacy(decrypted, exclude_ids=skip)}, 200


# --------------------------------------------------------------------------- #
# plain store reads
# --------------------------------------------------------------------------- #

def list_moments(store, *, limit_raw, since: str, include_archived_raw) -> tuple[dict, int]:
    try:
        limit = min(int(limit_raw), 200)
    except (TypeError, ValueError):
        return {"error": "invalid limit"}, 400
    include_archived = str(include_archived_raw or "").lower() in {"1", "true", "yes"}
    moments = memory_service._load_moments(store)
    if not include_archived:
        moments = memory_service._active_memory_moments(moments)
    if since:
        moments = [m for m in moments if m.get("occurred_at", "") >= since]
    moments = sorted(moments, key=lambda m: m.get("occurred_at", ""), reverse=True)
    return {"moments": moments[:limit], "total": len(moments)}, 200


def get_moment(store, moment_id: str) -> tuple[dict, int]:
    if not moment_id:
        return {"error": "id required"}, 400
    moments = memory_service._load_moments(store)
    for m in moments:
        if m.get("id") == moment_id:
            return {"moment": m}, 200
    return {"error": "not_found"}, 404


def delete_moment(store, moment_id: str) -> tuple[dict, int]:
    if not moment_id:
        return {"error": "id required"}, 400
    with memory_service.mutation_lock(store):
        moments = memory_service._load_moments(store)
        new_moments = [m for m in moments if m.get("id") != moment_id]
        if len(new_moments) == len(moments):
            return {"error": "not_found"}, 404
        memory_service._save_moments(store, new_moments)
    print(f"[memory:{store.user_id}] deleted: {moment_id}")
    return {"status": "deleted"}, 200


# --------------------------------------------------------------------------- #
# v1 envelope writes
# --------------------------------------------------------------------------- #

def add(store, payload: dict) -> tuple[dict, int]:
    envelope = payload.get("envelope")
    now = datetime.now().isoformat()

    if envelope is None:
        return {"error": "envelope required (v1 encryption is mandatory)"}, 400

    required = ["body_ct", "nonce", "K_user", "visibility", "owner_user_id"]
    missing = [f for f in required if not envelope.get(f)]
    if missing:
        return {"error": "envelope_missing_fields", "detail": missing}, 400
    if envelope["visibility"] not in ("shared", "local_only"):
        return {"error": "envelope.visibility must be 'shared' or 'local_only'"}, 400
    if envelope["visibility"] == "shared" and not envelope.get("K_enclave"):
        return {"error": "envelope with visibility=shared requires K_enclave"}, 400
    occurred_at = (envelope.get("occurred_at") or "").strip()
    if not occurred_at:
        return {"error": "occurred_at required (plaintext metadata for ordering)"}, 400
    if envelope["owner_user_id"] != store.user_id:
        return {"error": "envelope.owner_user_id does not match caller"}, 403

    mem_type = (envelope.get("type") or "").strip()
    if not mem_type:
        return {
            "error": "type_required",
            "allowed": list(memory_service.MEMORY_TYPES),
            "required": (
                "type is mandatory and must be one of moment/quote/fact/event/"
                "insight/reflection. See skill 'Memory types' section."
            ),
        }, 400
    if mem_type not in memory_service.MEMORY_TYPES:
        return {
            "error": "type_invalid",
            "got": mem_type,
            "allowed": list(memory_service.MEMORY_TYPES),
        }, 400

    moments = memory_service._load_moments(store)
    anchor_ids = envelope.get("anchor_memory_ids") or []

    if mem_type == "insight":
        if not anchor_ids:
            return {
                "error": "insight_requires_anchor",
                "required": (
                    "insight must reference ≥1 prior memory (anchor_memory_ids). "
                    "An insight is the agent's understanding of the user grounded in "
                    "concrete cards; if you can't point to a card, write fact/event first."
                ),
            }, 400
        ok, err = memory_service._validate_anchor_ids(moments, anchor_ids, store.user_id)
        if not ok:
            return err, 400

    if mem_type == "reflection":
        if not isinstance(anchor_ids, list) or len(anchor_ids) < 2:
            return {
                "error": "reflection_requires_substrate",
                "required": (
                    "reflection must reference ≥2 prior memories (anchor_memory_ids). "
                    "A reflection is the agent's standalone thinking; it needs at "
                    "least 2 pieces of substrate to count as thought, not vibes."
                ),
            }, 400
        ok, err = memory_service._validate_anchor_ids(moments, anchor_ids, store.user_id)
        if not ok:
            return err, 400
        days = identity_service._relationship_age_days(store)
        ok, err = memory_service._reflection_time_cap_ok(moments, days)
        if not ok:
            return err, 429  # rate-limit semantics

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
    # Re-read + append + save under one memory_lock hold so a concurrent
    # same-user write can't lost-update (the load above was for validation only).
    with memory_service.mutation_lock(store):
        moments = memory_service._load_moments(store)
        moments.append(moment)
        memory_service._save_moments(store, moments)
    boot_gates._log_bootstrap_event(store, "memory_moment_added_v1", success=True)
    print(f"[memory:{store.user_id}] added v1 type={mem_type} id={moment['id']} "
          f"visibility={envelope['visibility']} anchors={len(anchor_ids)}")
    return {"status": "created", "moment": moment, "v": 1}, 201


def retype(store, payload: dict) -> tuple[dict, int]:
    memory_id = (payload.get("id") or "").strip()
    new_type = (payload.get("type") or "").strip()
    if not memory_id:
        return {"error": "id required"}, 400
    if new_type not in memory_service.MEMORY_TYPES:
        return {
            "error": "type_invalid",
            "got": new_type,
            "allowed": list(memory_service.MEMORY_TYPES),
        }, 400

    # Hold memory_lock across load→modify→save so a concurrent same-user write
    # can't lost-update (re-read happens inside the lock).
    with memory_service.mutation_lock(store):
        moments = memory_service._load_moments(store)
        target_idx = None
        for i, m in enumerate(moments):
            if isinstance(m, dict) and m.get("id") == memory_id:
                target_idx = i
                break
        if target_idx is None:
            return {"error": "not_found"}, 404

        target = moments[target_idx]
        if target.get("owner_user_id") != store.user_id:
            return {"error": "not_owned"}, 403

        anchor_ids = payload.get("anchor_memory_ids") or []
        if new_type in ("insight", "reflection"):
            minimum = 1 if new_type == "insight" else 2
            if not isinstance(anchor_ids, list) or len(anchor_ids) < minimum:
                return {
                    "error": "anchor_required",
                    "detail": {"mem_type": new_type},
                    "min_anchors": minimum,
                    "required": (
                        f"Retyping into {new_type} requires ≥{minimum} anchor_memory_ids."
                    ),
                }, 400
            # Don't allow self-reference.
            if memory_id in anchor_ids:
                return {
                    "error": "anchor_self_reference",
                    "required": "A memory cannot anchor itself.",
                }, 400
            ok, err = memory_service._validate_anchor_ids(moments, anchor_ids, store.user_id)
            if not ok:
                return err, 400
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
    return {"status": "retyped", "moment": target}, 200


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #

def verify(store) -> tuple[dict, int]:
    """GUIDANCE-ONLY memory status signal — NEVER a gate.

    v1/v2 has no tabs and no hard floor: ``passing`` is always True (memory
    is never a gate — hx: 优先 onboarding 成功率, quality is backfilled by
    later re-distillation, not by blocking onboarding on card counts). The
    per-tab ``below_floor`` keys are kept for response-shape compat only and
    are always False — nothing reads a per-tab distinction anymore.

    The one live signal this endpoint still carries is the days-scaled
    consistency curve: ``memory_floor`` / ``memory_below_floor`` (same
    curve as ``bootstrap/status``'s and ``onboarding_validation``'s fields
    of the same name — see ``memory_service._memory_floor_for_days``). When
    below floor, at most ONE guidance suggestion is returned, phrased as a
    non-blocking nudge to record more *real* history — never to fabricate
    cards to hit a number.
    """
    moments = memory_service._load_moments(store)
    counts = memory_service._count_by_tab(moments)
    days = identity_service._relationship_age_days(store)
    floors = memory_service._per_tab_floors_for_days(days)
    memory_floor = memory_service._memory_floor_for_days(days)
    memory_below_floor = counts["total"] < memory_floor

    issues = []
    suggestions = []

    # v2 has no tabs — these three keys are response-shape compat only and
    # never fire. memory_below_floor (below) is the one signal that matters.
    below_floor = {"story": False, "about_me": False, "ta_thinking": False}

    # Time distribution — still useful diagnostic metadata (informational
    # only; never gates and never produces a suggestion string).
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

    if memory_below_floor:
        suggestions.append(
            f"参考下限约 {memory_floor} 张(按相处天数);材料/对话里真实支持的事实"
            "尽量都记,绝不编造凑数——这是引导,不拦任何流程。"
        )

    # Memory is NEVER a gate — passing is unconditionally True.
    passing = True

    resp = {
        "counts": counts,
        "floors": floors,
        "below_floor": below_floor,
        "memory_floor": memory_floor,
        "memory_below_floor": memory_below_floor,
        "relationship_days": days,
        "issues": issues,
        "suggestions": suggestions,
        "passing": passing,
        # Backwards-compatible flat fields — iOS / older tests may still
        # read these. below/floor above are the new source of truth.
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
    return resp, 200
