import asyncio
import base64
import copy
import errno
import hashlib
import hmac
import html
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
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import jwt
import websockets
from flask import Flask, abort, g, jsonify, request, Response, send_file
from flask_compress import Compress

from content_encryption import build_envelope
from provider_client import (
    ProviderConfig,
    ProviderError,
    chat_completion,
    mask_api_key,
    public_config as public_provider_config,
    test_provider_key,
    validate_config as validate_provider_config,
)
from context_memory_selection import memory_relevance_details
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

import db
import accounts
from accounts import access as accounts_access
from accounts import auth as accounts_auth
from accounts import onboarding as accounts_onboarding
from accounts import recover as accounts_recover
from accounts import registry as accounts_registry
from core import config as core_config
import admin as admin_pkg
import hosted as hosted_pkg
from hosted import chat_routes as hosted_chat_routes
from hosted import config_store as hosted_config_store
from hosted import context as hosted_context
from hosted import history_import as hosted_history_import
from hosted import onboarding_validation as hosted_onboarding_validation
from hosted import setup_routes as hosted_setup_routes
from hosted import turn as hosted_turn
from hosted import wake_consumer as hosted_wake_consumer
import bootstrap as bootstrap_pkg
import content as content_pkg
import tracking as tracking_pkg
from admin import data_track as admin_data_track
from content import routes as content_routes
from tracking import routes as tracking_routes
import chat as chat_pkg
import identity as identity_pkg
import memory as memory_pkg
import proactive as proactive_pkg
from bootstrap import gates as boot_gates
from bootstrap import routes as bootstrap_routes
from chat import consumer as chat_consumer
from chat import service as chat_service
from identity import actions as identity_actions
from identity import service as identity_service
from memory import actions as memory_actions
from memory import service as memory_service
import push as push_pkg
from proactive import dashboard as proactive_dashboard
from proactive import gate as proactive_gate
from proactive import service as proactive_service
import screen as screen_pkg
from push import apns as push_apns
from push import live_activity as push_live_activity
from push import service as push_service
from push import tokens as push_tokens_mod
from screen import frames as screen_frames
from screen import summary as screen_summary
from screen import ws as screen_ws
from core import enclave as core_enclave
from core import envelope as core_envelope
from core import store as core_store
from core import util as core_util

# ---------------------------------------------------------------------------
# Root directory + deployment mode
# ---------------------------------------------------------------------------

# COMPAT re-exports（迁移期）: these symbols moved into core/. The bindings
# below keep old readers working; code that tests monkeypatch must call
# through the core module attribute instead (core_enclave.func()).
FEEDLING_DIR = core_config.FEEDLING_DIR
_now_iso = core_util._now_iso
_safe_zoneinfo = core_util._safe_zoneinfo
_new_public_id = core_util._new_public_id
_strip_json_code_fence = core_util._strip_json_code_fence
_json_from_model_text = core_util._json_from_model_text
_to_epoch = core_util._to_epoch
_epoch_to_iso = core_util._epoch_to_iso
_enclave_get_json_for_gate = core_enclave._enclave_get_json_for_gate
_get_enclave_info = core_enclave._get_enclave_info
_decrypt_envelope_via_enclave = core_enclave._decrypt_envelope_via_enclave
_decode_content_public_key = core_envelope._decode_content_public_key
_content_public_key_fingerprint = core_envelope._content_public_key_fingerprint
_model_api_key_encryption_material = core_envelope._model_api_key_encryption_material
_build_shared_envelope_for_store = core_envelope._build_shared_envelope_for_store
_enclave_content_public_key_material = core_envelope._enclave_content_public_key_material

# Create the PostgreSQL schema (idempotent) before any load/save runs at import
# time. Fails fast if DATABASE_URL is unset / the DB is unreachable — the
# backend now requires Postgres and must not silently fall back to files.
db.init_schema()

# ---------------------------------------------------------------------------
# Users registry (multi-tenant). Every request is auth'd by api_key.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Users registry — moved to accounts/registry.py（COMPAT re-exports 迁移期）.
# _users / _key_to_user keep object identity — tests clear them in place.
# ---------------------------------------------------------------------------
_users_lock = accounts_registry._users_lock
_users = accounts_registry._users
_key_to_user = accounts_registry._key_to_user
ACCESS_MODES = accounts_registry.ACCESS_MODES
ACCESS_MODE_LABELS = accounts_registry.ACCESS_MODE_LABELS
_ACCESS_MODE_ALIASES = accounts_registry._ACCESS_MODE_ALIASES
ACCESS_LINK_TOKEN_TTL_SEC = accounts_access.ACCESS_LINK_TOKEN_TTL_SEC
_access_link_tokens_lock = accounts_access._access_link_tokens_lock
_server_pepper = accounts_registry._server_pepper
_hash_api_key = accounts_registry._hash_api_key
_normalize_access_mode = accounts_registry._normalize_access_mode
_new_principal_id = accounts_registry._new_principal_id
_new_key_id = accounts_registry._new_key_id
_new_binding_id = accounts_registry._new_binding_id
_normalize_api_key_entries = accounts_registry._normalize_api_key_entries
_normalize_access_bindings = accounts_registry._normalize_access_bindings
_normalize_user_entry = accounts_registry._normalize_user_entry
_normalize_all_users = accounts_registry._normalize_all_users
_rebuild_key_cache = accounts_registry._rebuild_key_cache
_load_users = accounts_registry.load_users
_save_users = accounts_registry._save_users
_resolve_user = accounts_registry._resolve_user
_USER_ID_RE = accounts_registry._USER_ID_RE
_register_user = accounts_registry._register_user
_get_user_archive_language = accounts_registry._get_user_archive_language
CHAT_HISTORY_INLINE_BODY_CT_MAX = chat_service.CHAT_HISTORY_INLINE_BODY_CT_MAX
CHAT_POLL_CLAIM_TTL_SEC = chat_service.CHAT_POLL_CLAIM_TTL_SEC

accounts_registry.load_users()

# ---------------------------------------------------------------------------
# Per-user state store — moved to core/store.py（COMPAT re-exports 迁移期）
# ---------------------------------------------------------------------------

MAX_FRAMES = core_store.MAX_FRAMES
MAX_CHAT_MESSAGES = core_store.MAX_CHAT_MESSAGES
PUSH_COOLDOWN_SECONDS = core_store.PUSH_COOLDOWN_SECONDS
LIVE_ACTIVITY_DEDUPE_SEC = core_store.LIVE_ACTIVITY_DEDUPE_SEC
LIVE_ACTIVITY_START_COOLDOWN_SEC = core_store.LIVE_ACTIVITY_START_COOLDOWN_SEC
DEVICE_EVENT_RETENTION_DAYS = core_store.DEVICE_EVENT_RETENTION_DAYS
TRACK_EVENT_RETENTION_DAYS = core_store.TRACK_EVENT_RETENTION_DAYS
TRACK_EVENT_MAX = core_store.TRACK_EVENT_MAX
PROACTIVE_JOB_MAX = core_store.PROACTIVE_JOB_MAX
PROACTIVE_USER_STATES = core_store.PROACTIVE_USER_STATES
PROACTIVE_AI_STATES = core_store.PROACTIVE_AI_STATES
PROACTIVE_BROADCAST_STATES = core_store.PROACTIVE_BROADCAST_STATES
PROACTIVE_DEFAULT_TIMEZONE = core_store.PROACTIVE_DEFAULT_TIMEZONE
_normalize_token_entry = core_store._normalize_token_entry
UserStore = core_store.UserStore
STORE_CACHE_TTL_SECONDS = core_store.STORE_CACHE_TTL_SECONDS
_stores = core_store._stores
_stores_lock = core_store._stores_lock
_wake_store_waiters = core_store._wake_store_waiters
_evict_store = core_store._evict_store
get_store = core_store.get_store

APP_FOREGROUND_FRESH_SEC = push_service.APP_FOREGROUND_FRESH_SEC
PROACTIVE_JOB_SOURCE = proactive_service.PROACTIVE_JOB_SOURCE
PROACTIVE_V2_WAKE_TTL_SEC = proactive_service.PROACTIVE_V2_WAKE_TTL_SEC
PROACTIVE_WAKE_MAX_FRAMES = proactive_service.PROACTIVE_WAKE_MAX_FRAMES
PROACTIVE_WAKE_FRAME_CANDIDATE_MAX = screen_frames.PROACTIVE_WAKE_FRAME_CANDIDATE_MAX



# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


# Auth moved to accounts/auth.py（COMPAT re-exports 迁移期）
_extract_api_key = accounts_auth._extract_api_key
require_user = accounts_auth.require_user


# ---------------------------------------------------------------------------
# Frames helpers
# ---------------------------------------------------------------------------


# Frames helpers — moved to screen/frames.py（COMPAT re-exports 迁移期）
_frame_url = screen_frames._frame_url
_save_frame = screen_frames._save_frame
_save_frame_envelope = screen_frames._save_frame_envelope


# ---------------------------------------------------------------------------
# Token entry helpers (pure functions over the per-user list)
# ---------------------------------------------------------------------------


# Token helpers — moved to push/tokens.py（COMPAT re-exports 迁移期）
_is_live_activity_token = push_tokens_mod._is_live_activity_token
_is_push_to_start_token = push_tokens_mod._is_push_to_start_token
_is_device_token = push_tokens_mod._is_device_token
_entry_is_active = push_tokens_mod._entry_is_active
_select_token = push_tokens_mod._select_token
_select_tokens = push_tokens_mod._select_tokens
_update_token_lifecycle = push_tokens_mod._update_token_lifecycle
_mark_expired_token = push_tokens_mod._mark_expired_token
_mark_active_token_success = push_tokens_mod._mark_active_token_success


# ---------------------------------------------------------------------------
# Semantic screen classifier — imported from a portable module so the iOS
# port can translate 1:1. See backend/semantic_analysis.py and
# docs/DESIGN_E2E.md §4 for the "classification on iOS" plan.
# ---------------------------------------------------------------------------

from semantic_analysis import analyze as _semantic_analysis  # noqa: E402


# ---------------------------------------------------------------------------
# Proactive wake helpers
# ---------------------------------------------------------------------------

# Presence + push decision — moved to push/service.py（COMPAT re-exports 迁移期）
_latest_app_presence = push_service._latest_app_presence
_ai_push_decision = push_service._ai_push_decision

# Proactive substrate — moved to proactive/（COMPAT re-exports 迁移期）
_DEVICE_EVENT_ALLOWED_KEYS = proactive_service._DEVICE_EVENT_ALLOWED_KEYS
_DEVICE_EVENT_DROP_RE = proactive_service._DEVICE_EVENT_DROP_RE
_redact_device_payload = proactive_service._redact_device_payload
_make_device_event = proactive_service._make_device_event


_recent_user_chat_active = chat_service._recent_user_chat_active


# Wake-frame helpers — moved to screen/frames.py（COMPAT re-exports 迁移期）
_recent_frame_meta = screen_frames._recent_frame_meta
_sample_frames_for_wake = screen_frames._sample_frames_for_wake
_frame_ids = screen_frames._frame_ids
_base64_payload = screen_frames._base64_payload
_decrypt_frame_metadata_for_gate = screen_frames._decrypt_frame_metadata_for_gate
_current_app_from_frames = screen_frames._current_app_from_frames
_ocr_summary = screen_frames._ocr_summary


_recent_device_events_for_wake = proactive_service._recent_device_events_for_wake
_payload_float = proactive_service._payload_float
_normalize_proactive_state = proactive_service._normalize_proactive_state
_proactive_bool = proactive_service._proactive_bool


_proactive_trigger = proactive_gate._proactive_trigger
_proactive_v2_auto_wake_block_reason = proactive_gate._proactive_v2_auto_wake_block_reason
_proactive_v2_wake_kind = proactive_gate._proactive_v2_wake_kind
_latest_payload_state_from_events = proactive_gate._latest_payload_state_from_events
_effective_broadcast_state = proactive_gate._effective_broadcast_state
_build_proactive_v2_wake_decision = proactive_gate._build_proactive_v2_wake_decision
_proactive_job_from_decision = proactive_gate._proactive_job_from_decision


# ---------------------------------------------------------------------------
# Hosted wake consumer (model_api route only)
# ---------------------------------------------------------------------------

# Hosted wake consumer — moved to hosted/wake_consumer.py（COMPAT 迁移期）
_hosted_wake_base_eligible = hosted_wake_consumer._hosted_wake_base_eligible
_maybe_start_hosted_wake_consumer = hosted_wake_consumer._maybe_start_hosted_wake_consumer
_run_model_api_wake_job_inner = hosted_wake_consumer._run_model_api_wake_job_inner
_run_hosted_tick_once = hosted_wake_consumer._run_hosted_tick_once
_reconcile_hosted_jobs = hosted_wake_consumer._reconcile_hosted_jobs
HOSTED_WAKE_CONSUMER_ID = hosted_wake_consumer.HOSTED_WAKE_CONSUMER_ID
HOSTED_TICK_INTERVAL_SEC = hosted_wake_consumer.HOSTED_TICK_INTERVAL_SEC

# Proactive debug dashboard — moved to proactive/dashboard.py（COMPAT 迁移期）
PROACTIVE_DEBUG_DECISION_READ_MAX = proactive_dashboard.PROACTIVE_DEBUG_DECISION_READ_MAX
PROACTIVE_DEBUG_JOB_READ_MAX = proactive_dashboard.PROACTIVE_DEBUG_JOB_READ_MAX
PROACTIVE_DEBUG_EVENT_READ_MAX = proactive_dashboard.PROACTIVE_DEBUG_EVENT_READ_MAX
PROACTIVE_DEBUG_REVIEW_READ_MAX = proactive_dashboard.PROACTIVE_DEBUG_REVIEW_READ_MAX
PROACTIVE_DEBUG_MESSAGE_READ_MAX = proactive_dashboard.PROACTIVE_DEBUG_MESSAGE_READ_MAX
PROACTIVE_DEBUG_FRAME_READ_MAX = proactive_dashboard.PROACTIVE_DEBUG_FRAME_READ_MAX
OPENROUTER_API_KEY = proactive_dashboard.OPENROUTER_API_KEY
PROACTIVE_DEBUG_TRANSLATION_MODEL = proactive_dashboard.PROACTIVE_DEBUG_TRANSLATION_MODEL
PROACTIVE_DEBUG_TRANSLATION_TIMEOUT_SEC = proactive_dashboard.PROACTIVE_DEBUG_TRANSLATION_TIMEOUT_SEC
_debug_translation_cache = proactive_dashboard._debug_translation_cache
_debug_translation_lock = proactive_dashboard._debug_translation_lock
_proactive_debug_snapshot = proactive_dashboard._proactive_debug_snapshot
_gate_input_dict = proactive_dashboard._gate_input_dict
_gate_decision_has_frame_context = proactive_dashboard._gate_decision_has_frame_context
_debug_translation_candidate = proactive_dashboard._debug_translation_candidate
_translate_debug_texts_to_zh = proactive_dashboard._translate_debug_texts_to_zh
_render_proactive_dashboard = proactive_dashboard._render_proactive_dashboard

# ---------------------------------------------------------------------------
# WebSocket ingest server
# ---------------------------------------------------------------------------

# WebSocket ingest server — moved to screen/ws.py; same start timing as before.
WS_PORT = screen_ws.WS_PORT
screen_ws.start()

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Per-request access log
# ---------------------------------------------------------------------------
# gunicorn (production) does not emit Flask's old dev-server access line, so
# without this the only per-request visibility in `phala cvms logs` is whatever
# individual handlers happen to print. This logs ONE structured line per
# request with the server-side handler time (dur_ms) and the on-the-wire
# response size + encoding — enough to tell "backend slow" from "network slow"
# and to spot oversized responses straight from the CVM logs. Registered
# before Compress(app) so it runs AFTER compression and records the final
# Content-Length / Content-Encoding.
@app.before_request
def _access_log_start():
    g._req_start = time.monotonic()


@app.after_request
def _access_log_end(response):
    # /healthz is hit constantly by the HAProxy/uptime probe — skip the noise.
    if request.path == "/healthz":
        return response
    start = getattr(g, "_req_start", None)
    dur_ms = int((time.monotonic() - start) * 1000) if start is not None else -1
    uid = getattr(g, "user_id", "-")
    clen = response.headers.get("Content-Length", "?")
    enc = response.headers.get("Content-Encoding", "-")
    # Keep the query string (limit/since/include_image_body — the useful bits)
    # but REDACT the `key` param: _extract_api_key() accepts `?key=` as an auth
    # method, so the URL can carry a live API key that must never reach the logs.
    if request.args.get("key"):
        pairs = [
            (k, "REDACTED" if k.lower() == "key" else v)
            for k, v in request.args.items(multi=True)
        ]
        path = f"{request.path}?{urlencode(pairs)}"
    else:
        path = request.full_path.rstrip("?")
    print(
        f"[req] uid={uid} {request.method} {path} status={response.status_code} "
        f"bytes={clen} enc={enc} dur_ms={dur_ms}",
        flush=True,
    )
    return response


# gzip responses when the client sends Accept-Encoding: gzip. CVM egress
# throughput is ~30-50 KB/s for large payloads; the decrypt-with-image
# path was shipping 470 KB of JSON per call, and dropping that in half
# via compression is a 3-5x latency win for "show me the screen" calls.
Compress(app)

# Extended Perception — self-contained feature module (backend/perception/).
# Mounts the /v1/perception/* blueprint; all its logic lives in that package.
from perception import register as register_perception  # noqa: E402

accounts.register(app)
push_pkg.register(app)
proactive_pkg.register(app)
identity_pkg.register(app)
memory_pkg.register(app)
bootstrap_pkg.register(app)
chat_pkg.register(app)
tracking_pkg.register(app)
admin_pkg.register(app)
content_pkg.register(app)
hosted_pkg.register(app)
screen_pkg.register(app)
from perception import snapshot_for_wake as _perception_wake_snapshot  # noqa: E402
register_perception(app)

# ---------------------------------------------------------------------------
# APNs config (global — one Apple dev key for the app)
# ---------------------------------------------------------------------------

# APNs — moved to push/apns.py（COMPAT re-exports 迁移期）
TEAM_ID = push_apns.TEAM_ID
KEY_ID = push_apns.KEY_ID
BUNDLE_ID = push_apns.BUNDLE_ID
APNS_SANDBOX = push_apns.APNS_SANDBOX
APNS_KEY = push_apns.APNS_KEY
_make_apns_jwt = push_apns._make_apns_jwt
_apns_env_name = push_apns._apns_env_name
_apns_host = push_apns._apns_host
_apns_reason_text = push_apns._apns_reason_text
_apns_should_retry_other_env = push_apns._apns_should_retry_other_env
_apns_token_should_expire = push_apns._apns_token_should_expire
_send_apns_once = push_apns._send_apns_once
_apns_sandbox_for_env = push_apns._apns_sandbox_for_env
_send_apns = push_apns._send_apns
_send_apns_to_active_tokens = push_apns._send_apns_to_active_tokens


# ---------------------------------------------------------------------------
# Aggregation helpers (stateless)
# ---------------------------------------------------------------------------

# Aggregation — moved to screen/summary.py（COMPAT re-exports 迁移期）
TODAY = screen_summary.TODAY
IOS_FALLBACK_DATA = screen_summary.IOS_FALLBACK_DATA
_humanize_app_name = screen_summary._humanize_app_name
_category_for_app = screen_summary._category_for_app
_build_ios_data = screen_summary._build_ios_data
MAC_DATA = screen_summary.MAC_DATA
SOURCES_DATA = screen_summary.SOURCES_DATA


_log_bootstrap_event = boot_gates._log_bootstrap_event
_load_bootstrap_events = boot_gates._load_bootstrap_events


# Consumer state — moved to chat/consumer.py（COMPAT 迁移期）
_OFFICIAL_CONSUMER_NAME = chat_consumer._OFFICIAL_CONSUMER_NAME
_CONSUMER_RECENT_SEC = chat_consumer._CONSUMER_RECENT_SEC
_load_consumer_state = chat_consumer._load_consumer_state
_save_consumer_state = chat_consumer._save_consumer_state
_record_consumer_event = chat_consumer._record_consumer_event
_consumer_validation_state = chat_consumer._consumer_validation_state

# ---------------------------------------------------------------------------
# Access modes: one user/principal, multiple API keys and entry points
# ---------------------------------------------------------------------------


# COMPAT re-exports（迁移期）: accounts helpers / routes moved to accounts/.
# The user pubkey lives in the users registry, which sits above core in the
# dependency stack — inject the lookup instead of core importing accounts.
core_envelope.get_user_public_key = accounts_registry._get_user_public_key

_find_user_entry_locked = accounts_registry._find_user_entry_locked
_user_entry_snapshot = accounts_registry._user_entry_snapshot
_principal_id_for_user = accounts_registry._principal_id_for_user
_upsert_access_binding_locked = accounts_registry._upsert_access_binding_locked
_issue_api_key_for_user_locked = accounts_registry._issue_api_key_for_user_locked
_public_access_mode_state = accounts_registry._public_access_mode_state
_recover_account_rank = accounts_registry._recover_account_rank
_canonical_account_for_pubkey = accounts_registry._canonical_account_for_pubkey
_get_user_public_key = accounts_registry._get_user_public_key
_set_user_public_key = accounts_registry._set_user_public_key
_access_modes_payload = accounts_access._access_modes_payload
_load_access_link_tokens = accounts_access._load_access_link_tokens
_save_access_link_tokens = accounts_access._save_access_link_tokens
_trim_access_link_tokens = accounts_access._trim_access_link_tokens
RECOVER_CHALLENGE_TTL_SEC = accounts_recover.RECOVER_CHALLENGE_TTL_SEC
_recover_challenges = accounts_recover._recover_challenges
_recover_challenges_lock = accounts_recover._recover_challenges_lock
_prune_recover_challenges_locked = accounts_recover._prune_recover_challenges_locked
MODEL_API_ROUTES = accounts_onboarding.MODEL_API_ROUTES
_normalize_onboarding_route = accounts_onboarding._normalize_onboarding_route
_load_onboarding_route = accounts_onboarding._load_onboarding_route
_save_onboarding_route = accounts_onboarding._save_onboarding_route










# Encrypted-content counts — moved to content/routes.py（COMPAT 迁移期）
_has_encrypted_content_record = content_routes._has_encrypted_content_record
_encrypted_content_counts = content_routes._encrypted_content_counts

# ---------------------------------------------------------------------------
# IO-hosted Model API key route
# ---------------------------------------------------------------------------





# Model API config — moved to hosted/config_store.py（COMPAT 迁移期）
_load_model_api_config = hosted_config_store._load_model_api_config
_save_model_api_config = hosted_config_store._save_model_api_config
_load_model_api_runtime_profile = hosted_config_store._load_model_api_runtime_profile
_ensure_model_api_runtime_profile = hosted_config_store._ensure_model_api_runtime_profile
_append_model_api_action_trace = hosted_config_store._append_model_api_action_trace
_latest_model_api_action_trace = hosted_config_store._latest_model_api_action_trace
_provider_config_from_plain = hosted_config_store._provider_config_from_plain
_load_runtime_provider_config = hosted_config_store._load_runtime_provider_config




# History import — moved to hosted/history_import.py（COMPAT 迁移期）
_load_history_import_jobs = hosted_history_import._load_history_import_jobs
_latest_history_import_job_row = None  # removed; see hosted.onboarding_validation


# Hosted context — moved to hosted/context.py（COMPAT 迁移期）
_model_api_context_messages = hosted_context._model_api_context_messages


# Chat thinking metadata re-exports（迁移期；批6迁至 chat/service.py）
_chat_thinking_extra_from_envelope = chat_service._chat_thinking_extra_from_envelope
_bounded_chat_metadata = chat_service._bounded_chat_metadata
_boolish_chat_metadata = chat_service._boolish_chat_metadata
_chat_thinking_metadata_from_payload = chat_service._chat_thinking_metadata_from_payload
_chat_plaintext_thinking_from_payload = chat_service._chat_plaintext_thinking_from_payload
_chat_plaintext_thinking_extra_for_store = chat_service._chat_plaintext_thinking_extra_for_store

# Hosted turn/jobs — moved to hosted/turn.py（COMPAT 迁移期）
_model_api_parse_turn_output = hosted_turn._model_api_parse_turn_output
_model_api_turn_count = hosted_turn._model_api_turn_count
_run_model_api_web_searches = hosted_turn._run_model_api_web_searches
_start_model_api_state_action_job = hosted_turn._start_model_api_state_action_job
_model_api_maybe_run_memory_capture = hosted_turn._model_api_maybe_run_memory_capture
_state_pending_items = hosted_turn._state_pending_items
_load_state_receipts = hosted_turn._load_state_receipts



# ---------------------------------------------------------------------------
# Screen / aggregation
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


# Push / Live Activity — moved to push/（COMPAT re-exports 迁移期）
_live_activity_identity_context = push_live_activity._live_activity_identity_context
_live_activity_content_state = push_live_activity._live_activity_content_state
_live_activity_body = push_live_activity._live_activity_body
_live_activity_top_app = push_live_activity._live_activity_top_app
push_live_activity_inner = push_live_activity.push_live_activity_inner
push_live_activity_end_inner = push_live_activity.push_live_activity_end_inner
push_live_start_inner = push_live_activity.push_live_start_inner
push_live_activity_hybrid_inner = push_live_activity.push_live_activity_hybrid_inner
_send_chat_alert = push_service._send_chat_alert
_json_body_from_response = push_service._json_body_from_response
_deliver_ai_message_push_if_background = push_service._deliver_ai_message_push_if_background














# ---------------------------------------------------------------------------
# Screen frames
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# Proactive hidden jobs
# ---------------------------------------------------------------------------

# Tracking — moved to tracking/routes.py（COMPAT 迁移期）
_make_tracking_event = tracking_routes._make_tracking_event

# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------




_chat_history_item = chat_service._chat_history_item




_request_chat_consumer_id = chat_service._request_chat_consumer_id
_request_bool_arg = chat_service._request_bool_arg
_float_meta = chat_service._float_meta
_chat_message_claimable = chat_service._chat_message_claimable
_pending_chat_messages_for_poll = chat_service._pending_chat_messages_for_poll



# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


# Identity storage — moved to identity/service.py（COMPAT 迁移期）
_load_identity = identity_service._load_identity
_save_identity = identity_service._save_identity
_append_identity_change = identity_service._append_identity_change
_load_identity_changes = identity_service._load_identity_changes
_parse_iso_calendar_date = identity_service._parse_iso_calendar_date
_earliest_memory_date = identity_service._earliest_memory_date
_anchor_from_days = identity_service._anchor_from_days
_live_days_with_user = identity_service._live_days_with_user
_IDENTITY_RUNTIME_LABELS = identity_service._IDENTITY_RUNTIME_LABELS
_IDENTITY_PROFILE_STRING_FIELDS = identity_service._IDENTITY_PROFILE_STRING_FIELDS
_IDENTITY_PROFILE_LIST_FIELDS = identity_service._IDENTITY_PROFILE_LIST_FIELDS
_IDENTITY_PROFILE_FIELDS = identity_service._IDENTITY_PROFILE_FIELDS


# Identity actions — moved to identity/actions.py（COMPAT 迁移期）
_identity_plain_for_action = identity_actions._identity_plain_for_action
_identity_payload_from_plain = identity_actions._identity_payload_from_plain
_execute_identity_action = identity_actions._execute_identity_action
_execute_identity_actions = identity_actions._execute_identity_actions



# ---------------------------------------------------------------------------
# Bootstrap stage gating
# ---------------------------------------------------------------------------
#
# Multiple production incidents (2026-05-13..15) showed Agent runtimes
# (OpenClaw in particular) skipping the memory-write phase of bootstrap
# and going straight to identity_init / chat_post. The Agent's own narrative
# would claim "I wrote 18 cards" while the server actually had zero — pure
# hallucination of completion.
#
# Skill-text rules alone cannot enforce this; behavior depends on the
# runtime's attention, prompt adherence, and failure modes. Server-side
# gates make the contract explicit: writes that violate the protocol
# return 409 with the missing prerequisite, and the Agent must satisfy
# the prerequisite before retrying.
#
# Floor is dynamic by relationship age — see _memory_floor_for_days
# (defined further down with the verify endpoints). A 6-month relationship
# legitimately demands more cards than a "we just met today" one. The
# previous hardcoded floor=3 was a compromise that was too strict for
# brand-new relationships AND too lax for established ones; replaced
# 2026-05-19 with the per-age table that's already documented in skill.

# Public skill URL — included in 409 responses so Agents that don't carry
# the skill in context can refetch it. Single source of truth.
# Bootstrap gates — moved to bootstrap/gates.py（COMPAT 迁移期）
_SKILL_URL = boot_gates._SKILL_URL
_bootstrap_state = boot_gates._bootstrap_state
_gate_required_for_missing_tabs = boot_gates._gate_required_for_missing_tabs
_chat_loop_verified_by_server = boot_gates._chat_loop_verified_by_server
_reply_is_for_pending_verify_ping = boot_gates._reply_is_for_pending_verify_ping
_gate_bootstrap_for_chat = boot_gates._gate_bootstrap_for_chat
_gate_bootstrap_for_identity_init = boot_gates._gate_bootstrap_for_identity_init



# ---------------------------------------------------------------------------
# Memory garden
# ---------------------------------------------------------------------------
#
# Memory model (post-2026-05-22 Friend-Test → typed-density rewrite):
#
#   Story tab        : moment, quote
#   About me tab     : fact, event
#   TA 在想 tab      : insight, reflection
#
# Each memory carries plaintext `type` metadata (alongside occurred_at /
# source / visibility / owner_user_id) so the server can validate the
# enum, count per-tab, gate identity_init by per-tab floor, and enforce
# reflection's substrate gate (≥2 anchor memories) + time cap.
#
# Insight requires anchor_memory_ids of length ≥1 (existing memories).
# Reflection requires anchor_memory_ids of length ≥2 AND obeys a per-tier
# time cap on the rolling window between reflections — agents shouldn't
# spam reflections, they should accumulate substrate.
#
# The ciphertext body still wraps the user-visible payload
# {title, description, type, her_quote?, context?, linked_dimension?}.
# `type` is duplicated inside body_ct for client-side rendering, but the
# plaintext copy on the envelope is the server source of truth (the only
# value the server can validate).

# Memory storage — moved to memory/service.py（COMPAT 迁移期）
MEMORY_TYPES = memory_service.MEMORY_TYPES
TAB_FOR_TYPE = memory_service.TAB_FOR_TYPE
_load_moments = memory_service._load_moments
_memory_is_archived = memory_service._memory_is_archived
_active_memory_moments = memory_service._active_memory_moments
_save_moments = memory_service._save_moments
_append_memory_change = memory_service._append_memory_change
_append_memory_capture_job = memory_service._append_memory_capture_job
_count_by_tab = memory_service._count_by_tab
_validate_anchor_ids = memory_service._validate_anchor_ids
_reflection_time_cap_ok = memory_service._reflection_time_cap_ok


# Memory actions — moved to memory/actions.py（COMPAT 迁移期）
_memory_action_text = memory_actions._memory_action_text
_memory_plain_from_envelope = memory_actions._memory_plain_from_envelope
_memory_validate_write = memory_actions._memory_validate_write
_build_memory_envelope_for_store = memory_actions._build_memory_envelope_for_store
_memory_record_from_envelope = memory_actions._memory_record_from_envelope
_execute_memory_action = memory_actions._execute_memory_action
_execute_memory_actions = memory_actions._execute_memory_actions





# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


# Bootstrap routes — moved to bootstrap/routes.py（COMPAT 迁移期）
_load_bootstrap = bootstrap_routes._load_bootstrap

# ---------------------------------------------------------------------------
# Verification endpoints — Phase 2 of the post-2026-05-15 onboarding
# robustness work. Per-module checks the Agent can call after each
# bootstrap module to confirm what landed matches what was intended.
#
# Distinct from the bootstrap GATES (/v1/chat/response 409s without
# memory+identity): gates enforce a small hard threshold (≥3 cards);
# verify endpoints expose the relationship-age floor and state info so
# the Agent can self-assess before identity derivation.
#
# Server can only see plaintext metadata (counts, timestamps) on
# encrypted modules. Deeper quality checks (template-title detection,
# dimension variance) happen at envelope-build time in mcp_server.py —
# see Phase 1 _check_identity_quality / _check_memory_quality.
# ---------------------------------------------------------------------------


# Floors / verify — moved to memory|identity（COMPAT 迁移期）
_relationship_age_days = identity_service._relationship_age_days
_per_tab_floors_for_days = memory_service._per_tab_floors_for_days
_memory_floor_for_days = memory_service._memory_floor_for_days


# Onboarding validation — moved to hosted/onboarding_validation.py（COMPAT 迁移期）
_visible_agent_message_count = hosted_onboarding_validation._visible_agent_message_count
_real_user_agent_exchange_verified = hosted_onboarding_validation._real_user_agent_exchange_verified
_model_api_hosted_chat_verified = hosted_onboarding_validation._model_api_hosted_chat_verified
_latest_history_import_job = hosted_onboarding_validation._latest_history_import_job
_onboarding_validation_payload = hosted_onboarding_validation._onboarding_validation_payload

# ---------------------------------------------------------------------------
# Beta data track admin surface
# ---------------------------------------------------------------------------

# Admin data-track — moved to admin/data_track.py（COMPAT 迁移期）
_extract_admin_token = admin_data_track._extract_admin_token
require_admin = admin_data_track.require_admin
_latest_epoch = admin_data_track._latest_epoch
_count_rows = admin_data_track._count_rows

# ---------------------------------------------------------------------------
# Envelope swap: replace an existing chat/memory item's ciphertext in place.
#
# Used by the per-item visibility toggle in iOS Settings. The client
# re-wraps its own plaintext with a new envelope (either including
# K_enclave for `shared` or omitting it for `local_only`) and POSTs
# {items: [{type, id, envelope}]}. Server swaps in place, preserving
# plaintext metadata (id/role/ts/source/occurred_at/created_at).
#
# NOT a migration endpoint — all stored items are already v1 — so
# there is no "already_v1" short-circuit. A v0 item (ancient data
# from before the strip) will fail with "not_found" if its v is < 1.
# ---------------------------------------------------------------------------







# Hosted Model API proactive heartbeat. Single gunicorn worker (see the
# UserStore cache comment) so exactly one scheduler runs. Set
# FEEDLING_HOSTED_TICK_ENABLED=0 to disable (tests / one-off scripts).
if os.environ.get("FEEDLING_HOSTED_TICK_ENABLED", "1") == "1":
    hosted_wake_consumer.start_tick()


@app.errorhandler(401)
def _unauthorized(e):
    return jsonify({"error": "unauthorized"}), 401


@app.errorhandler(403)
def _forbidden(e):
    return jsonify({"error": "forbidden"}), 403


@app.errorhandler(503)
def _unavailable(e):
    return jsonify({"error": "service_unavailable", "detail": "admin token is not configured"}), 503


# Assembly wiring: the hosted wake consumer threads need the Flask app object
# for app_context (push helpers deep-call jsonify).
hosted_wake_consumer.flask_app = app

# Assembly wiring: identity sits above push — inject the identity-card
# loader used by Live Activity content state.
push_live_activity.load_identity = _load_identity

# Assembly wiring: onboarding validation still lives here until the hosted
# line is extracted — inject into the admin data-track stats.
admin_data_track._latest_history_import_job = hosted_onboarding_validation._latest_history_import_job
admin_data_track._onboarding_validation_payload = hosted_onboarding_validation._onboarding_validation_payload

# COMPAT（迁移期兜底）: hosted 域符号体量大，逐一列举 re-export 易漏 —
# 把 hosted 模块的顶层定义统一回灌到 app 命名空间，供老测试/工具直接
# 取用。批 11 收敛兼容层时整体删除。
import types as _types

for _mod in (hosted_config_store, hosted_context, hosted_history_import,
             hosted_turn, hosted_setup_routes, hosted_chat_routes,
             hosted_onboarding_validation, hosted_wake_consumer):
    for _name, _obj in vars(_mod).items():
        if _name.startswith("__") or isinstance(_obj, _types.ModuleType):
            continue
        if _name in globals():
            continue
        globals()[_name] = _obj


if __name__ == "__main__":
    # PORT is read so isolation/load tests can spin up a hermetic backend on
    # a random free port without colliding with a developer's local dev
    # server on 5001 (or with another test running in parallel). Production
    # deploys can leave it unset — the 5001 default matches the published
    # compose/Dockerfile contract.
    port = int(os.environ.get("FEEDLING_PORT", os.environ.get("PORT", "5001")))
    # Production and all containers run gunicorn (see deploy/Dockerfile CMD
    # and the compose files): the Werkzeug dev server below is single-process
    # and stalls on large responses under the TDX CVM. Keep this path for
    # local dev and the hermetic test harness ONLY — never for real traffic.
    print(f"Feedling DEV server running at http://0.0.0.0:{port} (mode=multi-tenant, auth=api-key; prod uses gunicorn)")
    app.run(host="0.0.0.0", port=port, debug=False)
