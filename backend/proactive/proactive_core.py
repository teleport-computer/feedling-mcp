"""Framework-neutral core for the remaining proactive routes (ASGI-migration plan §7.4).

Per-route logic for every proactive route EXCEPT the long-poll (which lives in
``poll_core``), lifted out of the Flask ``proactive.routes`` so the native
FastAPI handlers (``proactive.routes_asgi``) reuse **identical** body / status /
validation semantics. No ``flask.request`` in here: each function takes the store
plus already-parsed params (query args, path ids, parsed JSON/form body) and
returns a neutral value — a plain dict (always-200 routes), a ``(body, status)``
tuple (routes with validation branches), or, for the two ``/debug`` dashboards,
an HTML string. The Flask routes and the ASGI routes both call in here, so their
responses are byte-identical.

The two debug-page renders read query args / ``Accept-Language`` from the neutral
``core.reqctx.request`` proxy deep inside ``dashboard._render_proactive_dashboard``;
to reproduce that off the event loop, ``debug_page_html`` runs the same renderer
inside a flask-free ``core.reqctx.bind`` context built from the caller's query
string + Accept-Language header (same technique as ``admin.admin_core``).
"""

from __future__ import annotations

import os
from datetime import datetime

from core import reqctx
from core import store as core_store
from core import util
from core import wake_bus as core_wake_bus
from hosted import config_store as hosted_config_store
from model_api_runtime.v2 import jobs_store
from proactive import capture_jobs, capture_scheduler, dashboard, dream_scheduler, gate, service
from proactive.observability_v2 import ROUND3_REVIEW_LABELS_V2

RESIDENT_RUNTIME_OWNER_ID_V2 = "resident_runtime_v2"


# --------------------------------------------------------------------------- #
# settings / state
# --------------------------------------------------------------------------- #

def _proactive_state_doc(settings: dict) -> dict:
    enabled = bool(settings.get("enabled", True))
    dnd = bool(settings.get("dnd", False))
    return {
        "version": settings.get("version", 2),
        "enabled": enabled,
        "dnd": dnd,
        "ambient": enabled,
        "scheduled": bool(settings.get("scheduled", True)),
        "reminders_delivery": not dnd,
        "user_state": settings.get("user_state", "default"),
        "manual_user_state": settings.get("manual_user_state", settings.get("user_state", "default")),
        "ai_state": settings.get("ai_state", "present"),
        "broadcast_state": settings.get("broadcast_state", "unknown"),
        "wake_interval_sec": core_store.normalize_proactive_wake_interval_sec(
            settings.get("wake_interval_sec")
        ),
        "dream_enabled": bool(settings.get("dream_enabled", True)),
        "capture_enabled": bool(settings.get("capture_enabled", True)),
        "screen_watch_enabled": bool(settings.get("screen_watch_enabled", True)),
        "photo_wake_enabled": bool(settings.get("photo_wake_enabled", True)),
        "arrival_wake_enabled": bool(settings.get("arrival_wake_enabled", True)),
        "unlock_wake_enabled": bool(settings.get("unlock_wake_enabled", True)),
        "updated_at": settings.get("updated_at", ""),
    }


def settings_get(store) -> dict:
    return store.load_proactive_settings()


def settings_save(store, payload: dict) -> dict:
    return store.save_proactive_settings(payload)


def state_get(store) -> dict:
    return _proactive_state_doc(store.load_proactive_settings())


def state_save(store, payload: dict) -> dict:
    settings = store.save_proactive_settings({
        key: payload.get(key)
        for key in (
            "user_state",
            "manual_user_state",
            "ai_state",
            "broadcast_state",
            "enabled",
            "dnd",
            "ambient",
            "scheduled",
            "reminders_delivery",
            "wake_interval_sec",
            "dream_enabled",
            "capture_enabled",
            "screen_watch_enabled",
            "photo_wake_enabled",
            "arrival_wake_enabled",
            "unlock_wake_enabled",
        )
        if key in payload
    })
    return _proactive_state_doc(settings)


def dream_status(store) -> dict:
    state = dream_scheduler.load_dream_state(store)
    return {
        "dreaming": capture_jobs._find_active_dream(store) is not None,
        "last_completed_at": state.get("last_dream_completed_at") or 0,
        "organized_count": int(state.get("last_dream_organized_count") or 0),
        "merged_count": int(state.get("last_dream_merged_count") or 0),
    }


# --------------------------------------------------------------------------- #
# device events
# --------------------------------------------------------------------------- #

def device_events_list(store, *, since_arg, limit_arg):
    try:
        since = float(since_arg)
    except (TypeError, ValueError):
        return {"error": "invalid since"}, 400
    try:
        limit = int(limit_arg)
    except (TypeError, ValueError):
        return {"error": "invalid limit"}, 400
    limit = max(1, min(limit, 200))
    return {"events": store.list_device_events(since_epoch=since, limit=limit)}, 200


def device_events_append(store, payload: dict) -> dict:
    inner_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    event = service._make_device_event(
        source=str(payload.get("source") or "ios"),
        event_type=str(payload.get("type") or payload.get("event_type") or "unknown"),
        payload=inner_payload,
    )
    store.append_device_event(event)
    # Runtime ownership is read strictly before choosing a capture substrate.
    # If the control-plane read fails, let the request fail: silently falling
    # back to the resident ``proactive_jobs`` queue would strand a V2 user's
    # capture forever.
    if _runtime_v2_scheduler_owned(store):
        capture = capture_scheduler.handle_device_event(
            store, event, submit=_submit_v2_capture
        )
    else:
        capture = capture_scheduler.handle_device_event(store, event)
    try:
        from perception import service as perception_service  # lazy; proactive can run without perception tests importing it
        # HOTFIX 2026-07-25 (sibling of the /report fix in
        # perception_read_core.report): device-event ingest is UNCONDITIONAL.
        # PR #107 tied this fork to the chat runtime fence and, unlike the
        # photo lane in service.photo_evaluate, there was no legacy branch to
        # fall through to — so every resident-chat user (≈ all of prod) stopped
        # producing device-event observations from 07-24 10:12Z. That silenced
        # the ONLY producers of the unlock_after_absence / screen_phash wakes
        # (perception/differ_v2.py); prod unlock wakes went ~13/h -> 0/h and
        # did not recover with the /report hotfix. Producing an observation is
        # data integrity, not a runtime lane: the differ's own switch gate
        # (proactive.controls_v2) still decides whether a wake is emitted.
        event["perception_v2"] = perception_service.ingest_device_event_v2(store.user_id, event)
        # Persist the device timezone from the (already-disclosed) app-presence
        # channel so proactive can localize its current_time anchor without the
        # perception-upload opt-in. Read from the RAW payload — the stored event
        # is redacted to an allowlist that (deliberately) excludes timezone. Kept
        # out of the wake path: time is a non-significant signal, no wake fires.
        tz = inner_payload.get("timezone")
        if tz:
            perception_service.record_context_timezone(store.user_id, str(tz), str(inner_payload.get("locale") or ""))
    except Exception as e:
        event["perception_v2"] = {"error": f"ingest_failed:{type(e).__name__}"}
    event["capture"] = _capture_response_doc(capture)
    return event


# --------------------------------------------------------------------------- #
# capture / dream response docs (shared by device-events, capture/dream ticks)
# --------------------------------------------------------------------------- #

def _capture_response_doc(result: dict) -> dict:
    job = result.get("job") if isinstance(result.get("job"), dict) else None
    state = result.get("state") if isinstance(result.get("state"), dict) else {}
    out = {
        "enqueued": bool(result.get("enqueued")),
        "reason": str(result.get("reason") or "")[:120],
        "state": {
            "last_captured_until_message_id": str(state.get("last_captured_until_message_id") or ""),
            "last_captured_until_ts": state.get("last_captured_until_ts") or 0,
            "last_captured_until_seq": state.get("last_captured_until_seq") or 0,
            "pending_capture_key": str(state.get("pending_capture_key") or ""),
            "last_capture_completed_at": state.get("last_capture_completed_at") or 0,
            "last_seen_message_id": str(state.get("last_seen_message_id") or ""),
            "last_seen_ts": state.get("last_seen_ts") or 0,
            "turns_since_capture": state.get("turns_since_capture") or 0,
            "message_count": state.get("message_count") or 0,
        },
    }
    if "quiet_for_sec" in result:
        out["quiet_for_sec"] = result.get("quiet_for_sec")
    if job is not None:
        out["job"] = {
            "job_id": str(job.get("job_id") or ""),
            "job_kind": str(job.get("job_kind") or ""),
            "status": str(job.get("status") or ""),
            "trigger": str(job.get("trigger") or ""),
            "capture_key": str(job.get("capture_key") or ""),
        }
    return out


def _dream_response_doc(result: dict) -> dict:
    job = result.get("job") if isinstance(result.get("job"), dict) else None
    state = result.get("state") if isinstance(result.get("state"), dict) else {}
    out = {
        "enqueued": bool(result.get("enqueued")),
        "reason": str(result.get("reason") or "")[:120],
        "new_cards": result.get("new_cards") or 0,
        "new_turns": result.get("new_turns") or 0,
        "state": {
            "last_dream_completed_at": state.get("last_dream_completed_at") or 0,
            "last_dream_organized_count": state.get("last_dream_organized_count") or 0,
            "last_dream_merged_count": state.get("last_dream_merged_count") or 0,
            "last_dreamed_until": str(state.get("last_dreamed_until") or ""),
            "last_dreamed_card_count": state.get("last_dreamed_card_count") or 0,
            "last_dreamed_turn_count": state.get("last_dreamed_turn_count") or 0,
            "last_dream_signature": str(state.get("last_dream_signature") or ""),
            "pending_dream_key": str(state.get("pending_dream_key") or ""),
        },
    }
    snapshot = result.get("snapshot") if isinstance(result.get("snapshot"), dict) else {}
    if snapshot:
        out["snapshot"] = {
            "card_count": snapshot.get("card_count") or 0,
            "turn_count": snapshot.get("turn_count") or 0,
            "signature": str(snapshot.get("signature") or ""),
            "last_until": str(snapshot.get("last_until") or ""),
        }
    if job is not None:
        out["job"] = {
            "job_id": str(job.get("job_id") or ""),
            "job_kind": str(job.get("job_kind") or ""),
            "status": str(job.get("status") or ""),
            "trigger": str(job.get("trigger") or ""),
            "dream_key": str(job.get("dream_key") or ""),
        }
    return out


# --------------------------------------------------------------------------- #
# capture / dream / proactive ticks
# --------------------------------------------------------------------------- #

def _runtime_v2_scheduler_owned(store) -> bool:
    return (
        hosted_config_store.get_hosted_runtime_mode_strict(store)
        == hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
    )


def _v2_capture_feature_enabled() -> bool:
    return os.environ.get("FEEDLING_V2_CAPTURE_ENABLED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _submit_v2_capture(store, *, trigger, now, window, capture_key) -> dict:
    """Submit every V2 Capture trigger through the shared agent_jobs lane."""
    if not _v2_capture_feature_enabled():
        return {"enqueued": False, "reason": "capture_disabled", "job": None}
    job_id, coalesced = jobs_store.enqueue_job(
        store.user_id,
        "capture",
        reason=str(trigger or "capture")[:200],
    )
    if not coalesced:
        core_wake_bus.notify("v2_jobs", store.user_id)
    return {
        "enqueued": not coalesced,
        "reason": "v2_coalesced" if coalesced else "v2",
        "job": {
            "id": job_id,
            "job_id": str(job_id),
            "job_kind": "memory_capture",
            "source": "memory_capture",
            "status": "pending",
            "trigger": str(trigger or "capture"),
            "capture_key": str(capture_key or ""),
            "capture_window": dict(window),
        },
    }


def _v2_dream_scheduler_noop(store) -> dict:
    return {
        "enqueued": False,
        "reason": "v2_scheduler_owned",
        "state": dream_scheduler.load_dream_state(store),
        "job": None,
        "snapshot": {},
        "new_cards": 0,
        "new_turns": 0,
    }

def capture_tick(store, payload: dict):
    now = None
    if "now" in payload:
        try:
            now = float(payload.get("now"))
        except (TypeError, ValueError):
            return {"error": "invalid now"}, 400
    if _runtime_v2_scheduler_owned(store):
        out = _capture_response_doc(
            capture_scheduler.tick_quiet_capture(
                store, now=now, submit=_submit_v2_capture
            )
        )
        out["dream"] = _dream_response_doc(_v2_dream_scheduler_noop(store))
        out["migrate"] = {"enqueued": False, "reason": "v2_scheduler_owned"}
        return out, 200
    result = capture_scheduler.tick_quiet_capture(store, now=now)
    out = _capture_response_doc(result)
    out["dream"] = _dream_response_doc(dream_scheduler.tick_memory_dream(store, now=now))
    migrate = capture_scheduler.tick_quiet_migrate(store, now=now)
    out["migrate"] = {"enqueued": bool(migrate.get("enqueued")), "reason": migrate.get("reason", "")}
    return out, 200


def capture_force(store) -> dict:
    if _runtime_v2_scheduler_owned(store):
        return _capture_response_doc(
            capture_scheduler.force_capture(store, submit=_submit_v2_capture)
        )
    result = capture_scheduler.force_capture(store)
    return _capture_response_doc(result)


def dream_tick(store, payload: dict):
    now = None
    if "now" in payload:
        try:
            now = float(payload.get("now"))
        except (TypeError, ValueError):
            return {"error": "invalid now"}, 400
    if _runtime_v2_scheduler_owned(store):
        return _dream_response_doc(_v2_dream_scheduler_noop(store)), 200
    result = dream_scheduler.tick_memory_dream(store, now=now, force=bool(payload.get("force")))
    return _dream_response_doc(result), 200


def proactive_tick(store, payload: dict, *, api_key) -> dict:
    if hosted_config_store.get_hosted_runtime_mode_strict(store) == hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2:
        # db_action_v2 users have no resident consumer (D0 exclusivity guard). A MANUAL
        # wake becomes a V2 manual_wake job; non-manual (heartbeat) ticks are owned by the
        # V2 scheduler, so we create NO resident proactive_job (it would never be claimed).
        is_manual = (
            service._proactive_bool(payload, "force", "force_response")
            or service._proactive_bool(payload, "manual", "manual_wake", "user_initiated")
            or bool(str(payload.get("context_hint") or "").strip())
        )
        if is_manual:
            job_id, coalesced = jobs_store.enqueue_job(store.user_id, "manual_wake", reason="manual_tick")
            core_wake_bus.notify("v2_jobs", store.user_id)
            return {"decision": None, "job": {"id": job_id, "lane": "manual_wake"}, "enqueued": not coalesced, "v2": True}
        return {"decision": None, "job": None, "enqueued": False, "v2": True}

    decision = gate._build_proactive_v2_wake_decision(store, payload, api_key=api_key)
    store.append_gate_decision(decision)

    job = None
    if decision.get("should_wake_agent", decision.get("should_reach_out")):
        job = store.append_proactive_job(gate._proactive_job_from_decision(decision))
        if gate._is_throttled_heartbeat_scope(
            decision.get("trigger", ""),
            manual=bool(decision.get("manual")),
            job_kind=decision.get("job_kind", ""),
        ):
            decision["heartbeat_next_tick_at"] = (
                core_store.advance_proactive_heartbeat_tick(
                    store.user_id,
                    now=float(decision.get("ts") or 0.0),
                    wake_interval_sec=decision.get("wake_interval_sec"),
                )
            )
    elif decision.get("reason") == gate.HEARTBEAT_THROTTLED_REASON:
        skipped = gate._proactive_job_from_decision(decision)
        skipped.update({
            "status": "skipped",
            "status_reason": gate.HEARTBEAT_THROTTLED_REASON,
            "failed_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        })
        store.append_skipped_proactive_job(skipped)

    return {
        "decision": decision,
        "job": job,
        "enqueued": job is not None,
    }


# --------------------------------------------------------------------------- #
# job status patch + claim / status
# --------------------------------------------------------------------------- #

def _job_status_patch(payload: dict, *, default_status: str = "") -> dict:
    status = str(payload.get("status") or default_status).strip().lower()
    reason = str(payload.get("reason") or payload.get("status_reason") or "").strip()
    consumer_id = str(payload.get("consumer_id") or "").strip()
    now_iso = datetime.now().isoformat()
    patch: dict = {}
    if status:
        patch["status"] = status[:80]
        if status == "claimed":
            patch["claimed_at"] = now_iso
        elif status == "realizing":
            patch["realizing_at"] = now_iso
        elif status in {"posted", "delivered"}:
            patch["posted_at"] = now_iso
        elif status == "completed":
            patch["completed_at"] = now_iso
        elif status in {"failed", "skipped"}:
            patch["failed_at"] = now_iso
    if reason:
        patch["status_reason"] = reason[:500]
    if consumer_id:
        patch["consumer_id"] = consumer_id[:160]
    if payload.get("chat_message_id"):
        patch["chat_message_id"] = str(payload.get("chat_message_id"))[:160]
    if payload.get("agent_action"):
        patch["agent_action"] = str(payload.get("agent_action"))[:120]
    if payload.get("agent_action_status"):
        patch["agent_action_status"] = str(payload.get("agent_action_status"))[:240]
    if isinstance(payload.get("agent_actions"), list):
        safe_actions = []
        for action in payload.get("agent_actions", [])[:10]:
            if not isinstance(action, dict):
                continue
            safe_action = {
                str(k)[:80]: (v if isinstance(v, (bool, int, float)) or v is None else str(v)[:500])
                for k, v in action.items()
                if str(k)
            }
            safe_actions.append(safe_action)
        patch["agent_actions"] = safe_actions
    if payload.get("wake_result"):
        patch["wake_result"] = str(payload.get("wake_result"))[:120]
    if payload.get("ai_state"):
        ai_state = service._normalize_proactive_state(payload.get("ai_state"), core_store.PROACTIVE_AI_STATES, "")
        if ai_state:
            patch["ai_state"] = ai_state
    if payload.get("broadcast_state"):
        broadcast_state = service._normalize_proactive_state(payload.get("broadcast_state"), core_store.PROACTIVE_BROADCAST_STATES, "")
        if broadcast_state:
            patch["broadcast_state"] = broadcast_state
    if isinstance(payload.get("request_broadcast"), dict):
        req = payload.get("request_broadcast") or {}
        try:
            duration_sec = int(req.get("duration_sec") or 0)
        except (TypeError, ValueError):
            duration_sec = 0
        patch["request_broadcast"] = {
            "reason": str(req.get("reason") or "")[:500],
            "duration_sec": max(0, min(duration_sec, 3600)),
            "copy": str(req.get("copy") or req.get("message") or "")[:500],
        }
    if isinstance(payload.get("capture_result"), dict):
        patch["capture_result"] = _safe_capture_doc(payload.get("capture_result"), max_items=20)
    if isinstance(payload.get("dream_result"), dict):
        patch["dream_result"] = _safe_capture_doc(payload.get("dream_result"), max_items=20)
    if isinstance(payload.get("capture_window"), dict):
        patch["capture_window"] = _safe_capture_doc(payload.get("capture_window"), max_items=12)
    if isinstance(payload.get("memory_action_status"), (dict, list)):
        patch["memory_action_status"] = _safe_capture_doc(payload.get("memory_action_status"), max_items=20)
    elif payload.get("memory_action_status"):
        patch["memory_action_status"] = str(payload.get("memory_action_status"))[:500]
    if isinstance(payload.get("memory_results"), list):
        patch["memory_results"] = _safe_capture_doc(payload.get("memory_results"), max_items=20)
    if isinstance(payload.get("questions"), list):
        patch["questions"] = _safe_capture_doc(payload.get("questions"), max_items=10)
    for key in ("cards_added", "cards_superseded", "cards_merged", "organized_count", "merged_count"):
        if key in payload:
            try:
                patch[key] = max(0, int(payload.get(key) or 0))
            except (TypeError, ValueError):
                patch[key] = 0
    if payload.get("noop_reason"):
        patch["noop_reason"] = str(payload.get("noop_reason"))[:500]
    return patch


def _safe_capture_doc(value, *, max_items: int = 20):
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:max_items]:
            skey = str(key)[:80]
            if isinstance(item, (bool, int, float)) or item is None:
                out[skey] = item
            elif isinstance(item, str):
                out[skey] = item[:1000]
            elif isinstance(item, (dict, list)):
                out[skey] = _safe_capture_doc(item, max_items=max_items)
            else:
                out[skey] = str(item)[:500]
        return out
    if isinstance(value, list):
        return [_safe_capture_doc(item, max_items=max_items) for item in value[:max_items]]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:1000]


def _find_proactive_job(store, job_id: str) -> dict | None:
    for row in store.list_proactive_jobs(since_epoch=0, limit=0):
        if str(row.get("job_id") or "") == str(job_id):
            return row
    return None


def job_claim(store, job_id, payload: dict) -> dict:
    patch = _job_status_patch(payload, default_status="claimed")
    job = store.update_proactive_job(job_id, patch, only_if_status="pending")
    if job is None:
        current = _find_proactive_job(store, str(job_id))
        return {"claimed": False, "job": current, "reason": "not_pending_or_missing"}
    return {"claimed": True, "job": job}


def job_status(store, job_id, payload: dict):
    patch = _job_status_patch(payload)
    if not patch:
        return {"error": "empty_status_patch"}, 400
    incoming_consumer = str(payload.get("consumer_id") or "").strip()
    current = _find_proactive_job(store, str(job_id))
    current_consumer = str((current or {}).get("consumer_id") or "").strip()
    if incoming_consumer and current_consumer and incoming_consumer != current_consumer:
        return {
            "error": "consumer_mismatch",
            "job": current,
            "expected_consumer_id": current_consumer,
        }, 409
    prev_status = str((current or {}).get("status") or "").strip().lower()
    job = store.update_proactive_job(job_id, patch)
    if job is None:
        return {"error": "job_not_found"}, 404
    new_status = str(patch.get("status") or payload.get("status") or "").strip().lower()
    # 同状态的重复终态上报（consumer 重试/重放）不再触发 recorder——否则失败
    # streak 会被同一次失败重复累计、退避被无故翻倍。
    if new_status and new_status != prev_status:
        if capture_jobs.is_memory_capture_job(job):
            capture_scheduler.record_capture_job_status(store, job, status=new_status)
        if capture_jobs.is_memory_dream_job(job):
            dream_scheduler.record_dream_job_status(store, job, status=new_status)
        if capture_jobs.is_memory_migrate_job(job):
            capture_scheduler.record_migrate_job_status(store, job, status=new_status)
    return {"job": job}, 200


# --------------------------------------------------------------------------- #
# scheduled wake actions / fire
# --------------------------------------------------------------------------- #

def scheduled_actions(store, payload: dict):
    actions = payload.get("actions")
    if not isinstance(actions, list):
        return {"error": "actions_required"}, 400
    from proactive.adapters_v2 import legacy_job_from_wake_event_v2
    from proactive.scheduled_wake_v2 import DBScheduledWakeStoreV2, ScheduledWakeServiceV2
    from proactive.store_v2 import DBProactiveSettingsStoreV2

    settings = DBProactiveSettingsStoreV2().load(store.user_id)
    scheduler = ScheduledWakeServiceV2(DBScheduledWakeStoreV2(), owner_id=RESIDENT_RUNTIME_OWNER_ID_V2)

    def _submit(event):
        store.append_proactive_job(legacy_job_from_wake_event_v2(event))
        return type("_Accepted", (), {"accepted": True, "reason": "queued_as_compat_job"})()

    results = scheduler.apply_turn_actions(
        store.user_id,
        actions,
        settings=settings,
        turn_id=str(payload.get("turn_id") or ""),
        wake_ids=tuple(str(item) for item in (payload.get("wake_ids") or ()) if str(item)),
        origin_refs=tuple(str(item) for item in (payload.get("origin_refs") or ()) if str(item)),
        self_wake=service._proactive_bool(payload, "self_wake"),
        submit_wake=_submit,
    )
    return {"results": [result.as_dict() for result in results]}, 200


def scheduled_fire(store) -> dict:
    from proactive.adapters_v2 import legacy_job_from_wake_event_v2
    from proactive.controls_v2 import WakeControlDecisionV2
    from proactive.scheduled_wake_v2 import DBScheduledWakeStoreV2, ScheduledWakeServiceV2
    from proactive.store_v2 import DBProactiveSettingsStoreV2

    settings = DBProactiveSettingsStoreV2().load(store.user_id)
    scheduler = ScheduledWakeServiceV2(DBScheduledWakeStoreV2(), owner_id=RESIDENT_RUNTIME_OWNER_ID_V2)
    queued_jobs: list[dict] = []

    def _submit(event):
        job = store.append_proactive_job(legacy_job_from_wake_event_v2(event))
        queued_jobs.append(job)
        return WakeControlDecisionV2(True, "queued_as_compat_job", settings)

    results = scheduler.fire_due_timers(
        store.user_id,
        settings=settings,
        submit_wake=_submit,
        owner_id=RESIDENT_RUNTIME_OWNER_ID_V2,
    )
    return {
        "results": [result.as_dict() for result in results],
        "jobs": queued_jobs,
        "queued": len(queued_jobs),
    }


# --------------------------------------------------------------------------- #
# decisions / reviews
# --------------------------------------------------------------------------- #

def list_decisions(store, *, since_arg, limit_arg):
    try:
        since = float(since_arg)
    except (TypeError, ValueError):
        return {"error": "invalid since"}, 400
    try:
        limit = int(limit_arg)
    except (TypeError, ValueError):
        return {"error": "invalid limit"}, 400
    limit = max(1, min(limit, 200))
    return {"decisions": store.list_gate_decisions(since_epoch=since, limit=limit)}, 200


def list_reviews(store, *, since_arg, limit_arg):
    try:
        since = float(since_arg)
    except (TypeError, ValueError):
        return {"error": "invalid since"}, 400
    try:
        limit = int(limit_arg)
    except (TypeError, ValueError):
        return {"error": "invalid limit"}, 400
    limit = max(1, min(limit, 500))
    return {"reviews": store.list_gate_reviews(since_epoch=since, limit=limit)}, 200


def decision_review(store, decision_id, payload: dict, *, ts, is_json: bool, accept: str):
    """Record a human review label. Returns ``(kind, body, status)`` where kind is
    ``"json"`` (body is a dict) or ``"html"`` (body is the saved-page HTML string).
    """
    decision_id = str(decision_id or "").strip()
    if not decision_id:
        return "json", {"error": "decision_id_required"}, 400

    decision = None
    for row in store.list_gate_decisions(since_epoch=0, limit=0):
        if str(row.get("decision_id") or "") == decision_id:
            decision = row
            break
    if decision is None:
        return "json", {"error": "decision_not_found"}, 404

    label = str(payload.get("label") or "").strip()
    legacy_gate_labels = {
        "correct_true",
        "correct_false",
        "missed_opportunity",
        "spam",
        "weak_connection",
        "repeated",
        "privacy_bad",
        "great_companion_moment",
    }
    allowed_labels = set(ROUND3_REVIEW_LABELS_V2) | legacy_gate_labels
    if label not in allowed_labels:
        return "json", {"error": "invalid_label", "allowed": sorted(allowed_labels)}, 400

    review = {
        "review_id": util._new_public_id("gr"),
        "decision_id": decision_id,
        "ts": ts,
        "created_at": datetime.now().isoformat(),
        "label": label,
        "label_family": "round3" if label in ROUND3_REVIEW_LABELS_V2 else "legacy_gate",
        "notes": str(payload.get("notes") or "")[:500],
        "reviewer": str(payload.get("reviewer") or "human")[:80],
        "expected_should_reach_out": payload.get("expected_should_reach_out"),
        "correct_connection_source_id": str(payload.get("correct_connection_source_id") or "")[:160],
        "decision_should_reach_out": bool(decision.get("should_reach_out")),
        "decision_reason": str(decision.get("reason") or decision.get("abstention_reason") or "")[:240],
        "decision_intent_label": str(decision.get("intent_label") or "")[:120],
        "decision_connection": decision.get("connection") or {},
        "frame_ids": decision.get("frame_ids") or [],
    }
    store.append_gate_review(review)
    if not is_json and "text/html" in accept:
        return "html", "<html><body><p>Review saved.</p><script>history.back()</script></body></html>", 200
    return "json", {"review": review}, 200


# --------------------------------------------------------------------------- #
# debug snapshot / dashboard page
# --------------------------------------------------------------------------- #

def debug_snapshot(store) -> dict:
    return dashboard._proactive_debug_snapshot(store)


def debug_page_html(store, *, query_string="", accept_language: str = "") -> str:
    # Bind a neutral, flask-free request context (query args + Accept-Language)
    # so ``_render_proactive_dashboard`` can read them off the event loop; mirrors
    # ``admin.admin_core``.
    snapshot = dashboard._proactive_debug_snapshot(store)
    headers = {}
    if accept_language:
        headers["Accept-Language"] = accept_language
    with reqctx.bind(query_string=query_string, headers=headers):
        return dashboard._render_proactive_dashboard(snapshot)
