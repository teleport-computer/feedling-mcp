"""Hosted proactive wake consumer + tick scheduler (model_api route only)."""

import base64
import copy
import hashlib
import io
import json
import os
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from flask import Blueprint, Response, jsonify, request, g

import db
from core import envelope as core_envelope
from core.store import UserStore

from hosted_runtime import (
    ACTION_RESPONSE_FORMAT as HOSTED_RUNTIME_ACTION_RESPONSE_FORMAT,
    ACTION_METHOD as HOSTED_RUNTIME_ACTION_METHOD,
    BACKGROUND_METHOD as HOSTED_RUNTIME_BACKGROUND_METHOD,
    BACKGROUND_NOT_STARTED_METHOD as HOSTED_RUNTIME_BACKGROUND_NOT_STARTED_METHOD,
    NOOP_METHOD as HOSTED_RUNTIME_NOOP_METHOD,
    PENDING_CONFIRM_METHOD as HOSTED_RUNTIME_PENDING_CONFIRM_METHOD,
    PENDING_REJECT_METHOD as HOSTED_RUNTIME_PENDING_REJECT_METHOD,
    RUNTIME_ENGINE_NATIVE as HOSTED_RUNTIME_ENGINE_NATIVE,
    build_background_execution_messages as build_hosted_runtime_background_execution_messages,
    background_execution_trace as hosted_runtime_background_trace,
    companion_turn_contract_message as hosted_runtime_companion_turn_contract_message,
    coerce_pending_decision as coerce_hosted_runtime_pending_decision,
    coerce_runtime_action as coerce_hosted_runtime_action,
)
from model_api_runtime.prompts import (
    build_foreground_chat_messages as build_model_api_foreground_chat_messages,
    build_memory_capture_messages as build_model_api_memory_capture_messages,
    build_pending_confirmation_messages as build_model_api_pending_confirmation_messages,
    build_web_search_results_message as build_model_api_web_search_results_message,
    web_search_followup_message as model_api_web_search_followup_message,
)
from model_api_runtime.tools import (
    extract_web_search_requests as extract_model_api_web_search_requests,
    run_web_searches as run_model_api_web_searches,
    web_search_trace as model_api_web_search_trace,
)
from model_api_runtime.wake import (
    wake_turn_contract_message as model_api_wake_turn_contract_message,
    build_wake_event_message as build_model_api_wake_event_message,
    hosted_tick_trigger as model_api_hosted_tick_trigger,
    parse_wake_actions as parse_model_api_wake_actions,
)
from context_memory_selection import memory_relevance_details
from content_encryption import build_envelope

from accounts import onboarding as accounts_onboarding
from accounts import registry
from core import store as core_store
from proactive import gate as proactive_gate
from push import service as push_service
import provider_client
from hosted import config_store as hosted_config_store
from hosted import context as hosted_context
from hosted import turn as hosted_turn


# Injected by the assembly layer: the Flask app object (threads need an app
# context for the push helpers). app.py sets this at startup.
flask_app = None

HOSTED_WAKE_CONSUMER_ID = "hosted_runtime"
# Thinking/reasoning models share this budget between reasoning and output
# tokens (same note as the chat path) — too small and the visible reply gets
# truncated into an unparseable_wake_reply failure.
HOSTED_WAKE_MAX_TOKENS = 2048


def _hosted_wake_base_eligible(store: UserStore) -> tuple[bool, str]:
    """Static preconditions for consuming ANY hosted wake job: hosted route,
    tested provider config, and a usable plaintext api_key in the process
    cache. The enabled/dnd gate is deliberately NOT here — it is per-job
    (_hosted_wake_job_block) because manual and forced summons bypass it."""
    if accounts_onboarding._load_onboarding_route(store) != "model_api":
        return False, "route_not_model_api"
    config = hosted_config_store._load_model_api_config(store)
    if not config or config.get("test_status") != "ok":
        return False, "model_api_not_ready"
    if not store.last_seen_api_key:
        return False, "api_key_unavailable"
    return True, ""


def _hosted_wake_job_block(store: UserStore, job: dict) -> str:
    """Per-job mechanical gate, mirroring proactive_gate._build_proactive_v2_wake_decision:
    forced wakes bypass the enabled switch, manual wakes (incl. forced —
    the decision builder folds force into manual) bypass dnd. A blocked job
    is terminally skipped, not deferred — a wake describes a moment, and
    running it later when the user re-enables proactive would be wrong."""
    settings = store.load_proactive_settings()
    if not settings.get("enabled", True) and not job.get("forced"):
        return "proactive_disabled"
    if settings.get("dnd", False) and not job.get("manual"):
        return "dnd_enabled"
    return ""


# Concurrency caps for the per-job consumer threads. A capped-out job simply
# stays pending — the reconcile pass in the tick loop retries it — so the cap
# bounds resource use without dropping work.
HOSTED_WAKE_MAX_PER_USER = 2
HOSTED_WAKE_MAX_TOTAL = 16
_hosted_wake_slots_lock = threading.Lock()
_hosted_wake_slots: dict[str, int] = {}  # user_id -> active consumer threads


def _hosted_wake_slot_acquire(user_id: str) -> bool:
    with _hosted_wake_slots_lock:
        if _hosted_wake_slots.get(user_id, 0) >= HOSTED_WAKE_MAX_PER_USER:
            return False
        if sum(_hosted_wake_slots.values()) >= HOSTED_WAKE_MAX_TOTAL:
            return False
        _hosted_wake_slots[user_id] = _hosted_wake_slots.get(user_id, 0) + 1
        return True


def _hosted_wake_slot_release(user_id: str) -> None:
    with _hosted_wake_slots_lock:
        n = _hosted_wake_slots.get(user_id, 0) - 1
        if n > 0:
            _hosted_wake_slots[user_id] = n
        else:
            _hosted_wake_slots.pop(user_id, None)


def _spawn_hosted_wake_thread(store: UserStore, job: dict) -> bool:
    """Start one consumer thread under the concurrency caps. Returns False
    when at capacity — the job stays pending and the reconcile pass retries."""
    if not _hosted_wake_slot_acquire(store.user_id):
        return False
    try:
        threading.Thread(
            target=_run_model_api_wake_job,
            args=(store, store.last_seen_api_key, dict(job)),
            daemon=True,
        ).start()
        return True
    except Exception:
        _hosted_wake_slot_release(store.user_id)
        raise


def _maybe_start_hosted_wake_consumer(store: UserStore, job: dict) -> None:
    """append_proactive_job hook: hosted users get an in-backend consumer
    thread per job; resident users are untouched (their consumer polls).
    Only the base gate runs here — the runner applies the per-job enabled/dnd
    gate (with manual/forced bypasses) after claiming, so blocked jobs get a
    terminal skipped status instead of stranding pending."""
    try:
        eligible, _reason = _hosted_wake_base_eligible(store)
        if not eligible:
            return  # stays pending; the reconcile pass retries when base recovers
        if not _spawn_hosted_wake_thread(store, job):
            print(f"[hosted-wake:{store.user_id}] concurrency cap hit, job stays pending")
    except Exception as e:
        print(f"[hosted-wake:{store.user_id}] consumer start failed: {e}")


# core.store must not import the hosted domain — the consumer is attached as
# an append_proactive_job hook here instead (before any request can append).
core_store.on_proactive_job_appended.append(_maybe_start_hosted_wake_consumer)


def _run_model_api_wake_job(store: UserStore, api_key: str, job: dict) -> None:
    """Thread entry: the push path (jsonify deep inside the Live Activity /
    APNs helpers) needs a Flask app context, which a bare daemon thread
    doesn't have. Always releases its concurrency slot."""
    try:
        with flask_app.app_context():
            _run_model_api_wake_job_inner(store, api_key, job)
    finally:
        _hosted_wake_slot_release(store.user_id)


def _run_model_api_wake_job_inner(store: UserStore, api_key: str, job: dict) -> None:
    """Realize one V2 wake job through the user's own hosted model.

    Claim is atomic (only_if_status=pending) so a duplicate consumer or a
    stray resident poller can never double-realize. Every exit path writes a
    terminal job status — the wake dashboard must never show a silently
    half-consumed job."""
    job_id = str(job.get("job_id") or "")
    if not job_id:
        return
    claimed = store.update_proactive_job(
        job_id,
        {"status": "claimed", "consumer_id": HOSTED_WAKE_CONSUMER_ID},
        only_if_status="pending",
    )
    if claimed is None:
        return  # already taken / not pending
    try:
        eligible, reason = _hosted_wake_base_eligible(store)
        if not eligible:
            store.update_proactive_job(job_id, {"status": "skipped", "status_reason": reason})
            return
        block = _hosted_wake_job_block(store, job)
        if block:
            store.update_proactive_job(job_id, {"status": "skipped", "status_reason": block})
            return
        runtime = hosted_config_store._load_runtime_provider_config(store, api_key)
        if isinstance(runtime, tuple):
            _, err = runtime
            store.update_proactive_job(job_id, {
                "status": "failed",
                "status_reason": str(err.get("error") or "runtime_load_failed")[:500],
            })
            return

        wake_event_msg = build_model_api_wake_event_message(job)
        provider_messages, _context_payload, _screen_images = hosted_context._model_api_context_messages(
            store,
            api_key,
            wake_event_msg["content"],
            include_screen_context=False,
        )
        provider_messages.insert(2, model_api_wake_turn_contract_message())
        store.update_proactive_job(job_id, {"status": "realizing"})
        result = provider_client.chat_completion(
            runtime,
            provider_messages,
            max_tokens=HOSTED_WAKE_MAX_TOKENS,
            temperature=0.7,
            timeout=90.0,
            include_reasoning=hosted_turn.MODEL_API_PROVIDER_REASONING_ENABLED,
        )
        actions = parse_model_api_wake_actions(str(result.get("reply") or ""))
        if actions is None:
            # 不把原始回包写进 chat —— 防内部 JSON/推理泄给用户。
            store.update_proactive_job(job_id, {
                "status": "failed", "status_reason": "unparseable_wake_reply",
            })
            return

        executed: list[dict] = []
        sent_message_ids: list[str] = []
        message_attempts = 0
        for action in actions:
            if action["type"] == "send_message":
                message_attempts += 1
                env, env_err = core_envelope._build_shared_envelope_for_store(
                    store, action["text"].encode("utf-8"))
                if env is None:
                    executed.append({"type": "send_message",
                                     "status": f"envelope_failed:{str(env_err)[:120]}"})
                    continue
                row = store.append_chat(
                    "openclaw", "model_api", env,
                    extra={"proactive_job_id": job_id},
                )
                store.notify_chat_waiters()
                delivery_fields = push_service._deliver_ai_message_push_if_background(
                    store,
                    body=action["text"],
                    title="IO",
                    data={"source": "model_api_proactive"},
                    visual_state="reply",
                )
                store.update_chat_message_metadata(row["id"], delivery_fields)
                sent_message_ids.append(row["id"])
                executed.append({"type": "send_message", "status": "posted",
                                 "chat_message_id": row["id"]})
            elif action["type"] == "set_ai_state":
                store.save_proactive_settings({"ai_state": action["state"]})
                executed.append({"type": "set_ai_state", "status": "ok",
                                 "state": action["state"]})
            else:  # sleep
                executed.append({"type": "sleep", "status": "ok"})

        if message_attempts and not sent_message_ids:
            # 模型想说话但一条都没写成（如 enclave 故障导致信封创建全部
            # 失败）——这是投递失败，不是 sleep。标 failed 让 dashboard 和
            # 后续重试看到真相，不能让"成功 sleep"吞掉主动消息。
            store.update_proactive_job(job_id, {
                "status": "failed",
                "status_reason": "message_envelope_failed",
                "agent_action": "send_message_failed",
                "agent_actions": executed[:10],
            })
            return
        if sent_message_ids:
            wake_result = "message_sent"
        elif job.get("manual"):
            # 用户主动召唤却没有任何可见回应 —— V2 评审词表里的
            # ignored_manual，单独标出供 dashboard 复盘（契约要求 manual
            # 至少给最小回应，模型违约时这里如实记录）。
            wake_result = "ignored_manual"
        elif any(a.get("type") == "set_ai_state" for a in executed):
            wake_result = "state_updated"
        else:
            wake_result = "sleep"
        final_patch = {
            "status": "completed",
            "wake_result": wake_result,
            "agent_action": wake_result,
            "agent_actions": executed[:10],
        }
        if sent_message_ids:
            final_patch["chat_message_id"] = sent_message_ids[0]
            final_patch["posted_at"] = datetime.now().isoformat()
        store.update_proactive_job(job_id, final_patch)
        print(f"[hosted-wake:{store.user_id}] job={job_id} result={wake_result}")
    except provider_client.ProviderError as e:
        store.update_proactive_job(job_id, {
            "status": "failed",
            "status_reason": f"provider_chat_failed:{str(e)[:200]}",
        })
    except Exception as e:
        store.update_proactive_job(job_id, {
            "status": "failed",
            "status_reason": f"wake_runner_error:{type(e).__name__}:{str(e)[:200]}",
        })


HOSTED_TICK_INTERVAL_SEC = float(os.environ.get("FEEDLING_HOSTED_TICK_INTERVAL_SEC", "1800"))
HOSTED_TICK_LOOP_SEC = 60.0
# Reconcile leases: a claimed/realizing hosted job older than this means the
# process died mid-run (provider timeout is 90s, so 10 min is generous).
HOSTED_WAKE_LEASE_SEC = 600.0
# Pending jobs without an expires_at (perception wakes) go stale after this —
# the context_hint describes a moment, not a standing request.
HOSTED_WAKE_PENDING_MAX_AGE_SEC = 3600.0


def _job_age_ref_epoch(job: dict) -> float:
    """Best timestamp for staleness checks: updated_at (ISO) else ts."""
    raw = str(job.get("updated_at") or "")
    if raw:
        try:
            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            pass
    try:
        return float(job.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _reconcile_hosted_jobs(store: UserStore, now: float) -> None:
    """Self-heal the hosted consumer across restarts/crashes. The append hook
    is the only normal consumer, so anything it missed (process died between
    append and thread completion, or the concurrency cap deferred a spawn)
    would otherwise strand forever:
      - pending: expired -> skipped; fresh -> re-spawn a consumer (the atomic
        claim makes a duplicate spawn harmless).
      - claimed/realizing owned by hosted_runtime past the lease -> failed.
        NOT re-run: the crash may have landed after the chat write, so a
        re-run could double-send.
    Resident jobs are untouched: this runs only for hosted-eligible users and
    skips claims owned by other consumer_ids."""
    for job in store.list_proactive_jobs(limit=50):
        job_id = str(job.get("job_id") or "")
        if not job_id:
            continue
        status = str(job.get("status") or "")
        if status == "pending":
            expired = False
            raw_exp = str(job.get("expires_at") or "")
            if raw_exp:
                try:
                    expired = datetime.fromisoformat(raw_exp).timestamp() <= now
                except ValueError:
                    pass
            elif now - _job_age_ref_epoch(job) > HOSTED_WAKE_PENDING_MAX_AGE_SEC:
                expired = True
            if expired:
                store.update_proactive_job(job_id, {
                    "status": "skipped", "status_reason": "expired_unconsumed",
                }, only_if_status="pending")
            else:
                _spawn_hosted_wake_thread(store, job)
        elif status in ("claimed", "realizing"):
            if str(job.get("consumer_id") or "") != HOSTED_WAKE_CONSUMER_ID:
                continue
            if now - _job_age_ref_epoch(job) > HOSTED_WAKE_LEASE_SEC:
                store.update_proactive_job(job_id, {
                    "status": "failed",
                    "status_reason": "stale_claim_reaped",
                }, only_if_status=status)
                print(f"[hosted-wake:{store.user_id}] reaped stale {status} job={job_id}")


def _run_hosted_tick_once(now: float | None = None) -> int:
    """One scheduler pass: create heartbeat wakes for due hosted users.
    Returns the number of jobs enqueued (the append hook consumes them).
    The trigger name comes from the user's broadcast state so the V2
    mechanical suppression treats hosted heartbeats exactly like resident
    ones."""
    now = now or time.time()
    with registry._users_lock:
        user_ids = [str(u.get("user_id") or "") for u in registry._users if u.get("user_id")]
    created = 0
    for user_id in user_ids:
        try:
            store = core_store.get_store(user_id)
            eligible, _reason = _hosted_wake_base_eligible(store)
            if not eligible:
                continue
            # Every pass (60s), not just on heartbeat: rescue stranded jobs
            # (restart/crash/concurrency-cap deferrals) before the dedup gate.
            # Runs on the BASE gate so a pending manual summon still gets
            # consumed (with its dnd bypass) while proactive is dnd'd off.
            _reconcile_hosted_jobs(store, now)
            settings = store.load_proactive_settings()
            # 心跳是自动 wake，不享受 manual/forced 豁免：开关关/勿扰时
            # 不创建（也不浪费 stamp），等用户打开后下一轮恢复。
            if not settings.get("enabled", True) or settings.get("dnd", False):
                continue
            last = db.get_blob(user_id, "hosted_tick") or {}
            if now - float(last.get("ts") or 0) < HOSTED_TICK_INTERVAL_SEC:
                continue
            # 先记 ts 再判定：判定被 suppress 也算一次 tick，避免对被
            # suppress 的用户每 60s 重复跑判定。
            db.set_blob(user_id, "hosted_tick", {
                "ts": now, "at": datetime.fromtimestamp(now).isoformat(),
            })
            # Same effective source as the decision builder (device events
            # win, settings fall back) — see _effective_broadcast_state.
            decision = proactive_gate._build_proactive_v2_wake_decision(
                store,
                {"trigger": model_api_hosted_tick_trigger(
                    proactive_gate._effective_broadcast_state(store, settings))},
                api_key=store.last_seen_api_key or None,
            )
            store.append_gate_decision(decision)
            if decision.get("should_wake_agent"):
                store.append_proactive_job(proactive_gate._proactive_job_from_decision(decision))
                created += 1
        except Exception as e:
            print(f"[hosted-tick:{user_id}] failed: {e}")
    return created


def _hosted_tick_loop() -> None:
    while True:
        time.sleep(HOSTED_TICK_LOOP_SEC)
        try:
            created = _run_hosted_tick_once()
            if created:
                print(f"[hosted-tick] created={created}")
        except Exception as e:
            print(f"[hosted-tick] loop error: {e}")


def start_tick():
    """Start the hosted proactive heartbeat. Single gunicorn worker, so
    exactly one scheduler runs. Called by app.py under the same
    FEEDLING_HOSTED_TICK_ENABLED gate as before."""
    threading.Thread(target=_hosted_tick_loop, daemon=True, name="hosted-tick").start()
