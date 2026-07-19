"""Model API config / runtime profile / action traces (hosted line)."""

import os
import time
import types
import uuid

import db
from core import enclave as core_enclave
from core import util as core_util
from core.store import UserStore
from notices import catalog as notices_catalog
from notices import core as notices_core
import provider_client
from provider_client import public_config as public_provider_config
from provider_client import validate_config as validate_provider_config


def _load_model_api_config(store: UserStore) -> dict | None:
    """The user's active model_api config, projected to the legacy blob shape.

    Post model-api-multi-profile migration the config lives in
    ``model_api_routes`` + ``model_api_credentials`` (one active route per user,
    see alembic 0014). This projection lets every existing reader — driver
    resolution (chat_send/setup), onboarding validate, perception/screen rollout
    flags, runtime status, admin data-track — keep consuming the same dict shape
    without each learning the new tables. The frozen ``user_blobs(kind='model_api')``
    snapshot is a rollback artifact only and is deliberately no longer read here.

    ``api_key_envelope`` is intentionally NOT projected: the only path that needs
    the ciphertext (enclave decrypt) reads it straight off ``load_active_route``,
    so keeping it out of this shape shrinks the accidental-serialization surface.
    """
    route = db.model_api_active_route(store.user_id)
    if not route:
        return None
    config = {
        "route": "model_api",
        "provider": route["provider"],
        "model": route["model"],
        "base_url": route["base_url"],
        "context_window_tokens": route.get("context_window_tokens"),
        "api_key_hint": route["api_key_hint"],
        "supports_responses": route["supports_responses"],
        "test_status": route["test_status"],
        "last_test_at": route["last_test_at"],
        "last_test_error": route["last_test_error"],
    }
    if route.get("reasoning_effort"):
        config["reasoning_effort"] = route["reasoning_effort"]
    return config


MODEL_API_RUNTIME_BLOB = "model_api_runtime"
MODEL_API_RUNTIME_VERSION = 2
MODEL_API_RUNTIME_MODE = "hosted_resident"
# One-time scrub of legacy auto-seeded `<flag>=False` artifacts. These flags are
# env-gated rollout baselines (core/util.runtime_v2_default_on); seeding them as
# False used to pin every profile and defeat the baseline. We scrub the seeded
# False ONCE per flag (tracked in V2_AUTOSEED_SCRUBBED_FLAGS), then leave the flag
# alone so a deliberate per-user opt-out written later as False survives. No setter
# ever writes False, so any pre-scrub False is a seed artifact; explicit True is
# always preserved.
PERCEPTION_V2_AUTOSEED_SCRUBBED = "perception_v2_autoseed_scrubbed"  # legacy bool marker (rev 1)
V2_AUTOSEED_SCRUBBED_FLAGS = "v2_autoseed_scrubbed_flags"
AUTOSEED_SCRUB_FLAGS = (
    "perception_ingress_runtime_v2_enabled",
    "hosted_wake_runtime_v2_enabled",
    "hosted_chat_full_tool_loop_v2_enabled",
    "screen_caption_enabled",
)
MODEL_API_ACTION_TRACE_STREAM = "model_api_action_traces"
# One append per model-API action (then patched by trace_id on completion).
# High frequency; cap the stream. A background trace is appended as ``queued``
# and only later patched to a terminal status, so trim must only evict rows that
# have already reached a terminal status — never an in-flight one, or the
# completion patch would target a deleted row (returns None, losing the result).
MODEL_API_ACTION_TRACE_MAX = int(os.environ.get("FEEDLING_MODEL_API_ACTION_TRACE_MAX", 1000))
MODEL_API_ACTION_TRACE_TERMINAL_STATUSES = ["ok", "completed", "failed", "skipped"]


def _load_model_api_runtime_profile(store: UserStore) -> dict | None:
    data = db.get_blob(store.user_id, MODEL_API_RUNTIME_BLOB)
    return data if isinstance(data, dict) else None


def _save_model_api_runtime_profile(
    store: UserStore, profile: dict, *, strict: bool = False,
) -> dict:
    data = dict(profile)
    data["runtime_mode"] = MODEL_API_RUNTIME_MODE
    data["runtime_version"] = MODEL_API_RUNTIME_VERSION
    data["updated_at"] = core_util._now_iso()
    if not data.get("created_at"):
        data["created_at"] = data["updated_at"]
    # hosted_runtime_mode and the V2 reply cursor have dedicated atomic
    # writers.  Normalization starts from a potentially stale full profile, so
    # it must never echo either correctness key back through a generic merge.
    data.pop("hosted_runtime_mode", None)
    data.pop("v2_reply_cursor_seq", None)
    scrubbed = set(data.get(V2_AUTOSEED_SCRUBBED_FLAGS) or [])
    remove_keys = [
        key for key in (*AUTOSEED_SCRUB_FLAGS, PERCEPTION_V2_AUTOSEED_SCRUBBED)
        if key not in data and (key in scrubbed or key == PERCEPTION_V2_AUTOSEED_SCRUBBED)
    ]
    if strict:
        persisted = db.patch_blob_strict(
            store.user_id, MODEL_API_RUNTIME_BLOB, data, remove_keys=remove_keys)
    else:
        persisted = db.patch_blob(
            store.user_id, MODEL_API_RUNTIME_BLOB, data, remove_keys=remove_keys)
    return persisted if isinstance(persisted, dict) else data


def _ensure_model_api_runtime_profile(
    store: UserStore,
    config: dict | None = None,
    *,
    touch: bool = False,
) -> dict | None:
    """Lazily materialize Runtime V2 profile metadata for model-API users.

    This is intentionally metadata-only: provider key envelopes, chat,
    identity, and memory cards remain untouched.
    """
    config = config if isinstance(config, dict) else _load_model_api_config(store)
    if not config:
        return None
    existing = _load_model_api_runtime_profile(store) or {}
    profile = dict(existing)
    changed = touch

    defaults = {
        "runtime_mode": MODEL_API_RUNTIME_MODE,
        "runtime_version": MODEL_API_RUNTIME_VERSION,
        "tool_action_enabled": True,
        "recap_cursor": None,
        "last_recap_at": None,
        "last_action_trace_id": None,
        "memory_quality_warning": None,
        # The env-gated rollout flags (perception_ingress / hosted_wake /
        # hosted_chat_full_tool_loop / screen_caption) are intentionally NOT seeded
        # here. Their default comes from core/util.runtime_v2_default_on(); seeding
        # them as False would pin every profile and defeat that baseline. See
        # AUTOSEED_SCRUB_FLAGS for the one-time cleanup of legacy seeded values.
        "provider": str(config.get("provider") or ""),
        "model": str(config.get("model") or ""),
    }
    for key, value in defaults.items():
        if profile.get(key) != value and (
            key in {"runtime_mode", "runtime_version", "tool_action_enabled"}
            or key not in profile
            or key in {"provider", "model"}
        ):
            profile[key] = value
            changed = True
    # One-time migration: scrub the legacy auto-seeded `<flag>=False` for each
    # env-gated rollout flag exactly once, so existing profiles fall through to the
    # baseline. We must NOT scrub on every read — a deliberate per-user opt-out
    # written later as False would be deleted before the reader sees it. Each flag is
    # recorded in V2_AUTOSEED_SCRUBBED_FLAGS after its one scrub; afterwards an
    # explicit False survives and wins over the baseline.
    scrubbed = set(profile.get(V2_AUTOSEED_SCRUBBED_FLAGS) or [])
    if profile.pop(PERCEPTION_V2_AUTOSEED_SCRUBBED, None):  # migrate legacy rev-1 marker
        scrubbed.add("perception_ingress_runtime_v2_enabled")
        changed = True
    for flag in AUTOSEED_SCRUB_FLAGS:
        if flag not in scrubbed:
            if profile.get(flag) is False:
                profile.pop(flag, None)
            scrubbed.add(flag)
            changed = True
    if profile.get(V2_AUTOSEED_SCRUBBED_FLAGS) != sorted(scrubbed):
        profile[V2_AUTOSEED_SCRUBBED_FLAGS] = sorted(scrubbed)
        changed = True
    if changed or not existing:
        profile = _save_model_api_runtime_profile(store, profile)
    return profile


def _patch_model_api_runtime_profile(store: UserStore, patch: dict) -> dict | None:
    profile = _ensure_model_api_runtime_profile(store) or {}
    if not profile:
        return None
    values = {k: v for k, v in patch.items() if v is not None}
    values.update({
        "runtime_mode": MODEL_API_RUNTIME_MODE,
        "runtime_version": MODEL_API_RUNTIME_VERSION,
        "updated_at": core_util._now_iso(),
    })
    # Ownership mode and reply cursor are correctness keys with dedicated
    # transactional/monotonic writers, never generic profile fields.
    values.pop("hosted_runtime_mode", None)
    values.pop("v2_reply_cursor_seq", None)
    persisted = db.patch_blob(store.user_id, MODEL_API_RUNTIME_BLOB, values)
    if isinstance(persisted, dict):
        return persisted
    merged = dict(profile)
    merged.update(values)
    return merged


def record_runtime_error(store: UserStore, *, error: str, error_class: str = "") -> tuple[dict, int]:
    """Runtime V2 worker 上报（或清空）最近一次回合失败原因。

    写 active route 行（``model_api_routes.last_runtime_error*``）。读侧是 setup_core 的
    last_runtime_error（iOS 设置页，也已切到读 route）与 GET /v1/model_api/routes。
    legacy inline 路径经 action-trace 写同一字段；本函数是 pooled V2 路径的写侧
    （spec 2026-07-06-upstream-error-surfacing 腿②）。"""
    if not db.model_api_route_mark_runtime_error(
            store.user_id, error=error, error_class=error_class):
        return {"error": "model_api_runtime_profile_missing"}, 404
    try:
        if error:
            ec = error_class or "unknown"
            notices_core.emit(
                store, source="chat", error_class=ec,
                blame=notices_catalog.blame_for(ec), severity="error",
                user_text=notices_catalog.user_text_for(ec),
                detail=error, dedupe_key=f"chat:{ec}")
        else:
            notices_core.resolve(store, "chat:")
    except Exception:
        pass   # 扇出绝不影响 record_runtime_error 主职责（emit/resolve 本身已 never-raise，这是双保险）
    return {"ok": True}, 200


def _append_model_api_action_trace(store: UserStore, entry: dict) -> dict:
    record = {
        "trace_id": entry.get("trace_id") or f"mat_{uuid.uuid4().hex[:16]}",
        "ts": time.time(),
        "created_at": core_util._now_iso(),
        "runtime_mode": MODEL_API_RUNTIME_MODE,
        "runtime_version": MODEL_API_RUNTIME_VERSION,
        "status": str(entry.get("status") or "ok")[:80],
    }
    for key in (
        "provider", "model", "user_message_id", "assistant_message_id",
        "state_receipt_id", "background_execution", "runtime", "effects", "identity_actions",
        "memory_actions", "capture", "context", "error", "duration_ms",
        "usage", "reason", "progress",
    ):
        if key in entry:
            record[key] = entry[key]
    db.log_append(
        store.user_id,
        MODEL_API_ACTION_TRACE_STREAM,
        record,
        ts=record["ts"],
        item_key=record["trace_id"],
    )
    db.log_trim(
        store.user_id, MODEL_API_ACTION_TRACE_STREAM, MODEL_API_ACTION_TRACE_MAX,
        only_statuses=MODEL_API_ACTION_TRACE_TERMINAL_STATUSES,
    )
    patch = {
        "last_action_trace_id": record["trace_id"],
        "last_action_trace_at": record["created_at"],
    }
    runtime_error = None
    if record["status"] == "ok":
        runtime_error = ""
    elif record.get("error"):
        runtime_error = str(record.get("error"))[:300]
    if runtime_error is not None:
        patch["last_runtime_error"] = runtime_error
    _patch_model_api_runtime_profile(store, patch)
    if runtime_error is not None:
        # GET /v1/model_api/runtime (and /routes) now read last_runtime_error off
        # the active route row (record_runtime_error's write side), not this blob —
        # write there too so the legacy inline action-trace path's errors actually
        # surface. error_class=None preserves whatever class the agent-runner path
        # (record_runtime_error) already wrote: this path never computes a class.
        # model_api_route_mark_runtime_error swallows its own exceptions and
        # returns False when there's no active route — best-effort, same tolerance
        # _patch_model_api_runtime_profile above already has (returns None when the
        # runtime blob can't be seeded).
        db.model_api_route_mark_runtime_error(
            store.user_id, error=runtime_error, error_class=None)
    return record


def set_last_runtime_error(store: UserStore, message: str) -> None:
    """Public direct lever to surface a terminal runtime failure to iOS's error
    chip. The active route is the current read-side truth; the legacy runtime
    profile remains a rollback/debug mirror. This is for callers — namely the
    V2 worker and independent reaper — that have no action-trace entry."""
    value = str(message)[:300]
    _patch_model_api_runtime_profile(store, {"last_runtime_error": value})
    db.model_api_route_mark_runtime_error(
        store.user_id, error=value, error_class=None)


def _patch_model_api_action_trace(store: UserStore, trace_id: str, patch: dict) -> dict | None:
    merged = dict(patch)
    if patch.get("status") in {"completed", "failed", "skipped"}:
        merged.setdefault("completed_at", core_util._now_iso())
    record = db.log_patch_item(store.user_id, MODEL_API_ACTION_TRACE_STREAM, trace_id, merged)
    profile_patch: dict = {
        "last_action_trace_id": trace_id,
        "last_action_trace_at": core_util._now_iso(),
    }
    runtime_error = None
    if patch.get("status") in {"completed", "skipped", "ok"}:
        runtime_error = ""
    elif patch.get("error"):
        runtime_error = str(patch.get("error"))[:300]
    if runtime_error is not None:
        profile_patch["last_runtime_error"] = runtime_error
    _patch_model_api_runtime_profile(store, profile_patch)
    if runtime_error is not None:
        # See _append_model_api_action_trace above: writer/reader parity fix —
        # error_class=None preserves the agent-runner path's class.
        db.model_api_route_mark_runtime_error(
            store.user_id, error=runtime_error, error_class=None)
    return record


def _latest_model_api_action_trace(store: UserStore) -> dict | None:
    traces = db.log_read(store.user_id, MODEL_API_ACTION_TRACE_STREAM, limit=1)
    return traces[-1] if traces else None


def _provider_config_from_plain(config: dict, api_key: str) -> provider_client.ProviderConfig:
    provider, model, base_url = validate_provider_config(
        str(config.get("provider") or ""),
        str(config.get("model") or ""),
        str(config.get("base_url") or ""),
    )
    return provider_client.ProviderConfig(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        context_window_tokens=config.get("context_window_tokens"),
    )


def load_active_route(store: UserStore) -> dict | None:
    """当前生效的 route（含其 credential 的 api_key_envelope）。

    这是 hosted 线读 model_api 配置的唯一入口。返回形状见 db.model_api_active_route。
    """
    return db.model_api_active_route(store.user_id)


def _load_runtime_provider_config(store: UserStore, api_key: str | None, *, runtime_token: str = "") -> provider_client.ProviderConfig | tuple[None, dict]:
    route = load_active_route(store)
    if not route:
        return None, {"error": "model_api_not_configured"}
    if route.get("test_status") != "ok":
        return None, {"error": "model_api_not_tested", "test_status": route.get("test_status", "")}
    envelope = route.get("api_key_envelope")
    if not isinstance(envelope, dict):
        return None, {"error": "model_api_key_envelope_missing"}
    # A Runtime V2 turn authenticates with a runtime token, not the
    # long-term api_key — forward it so the enclave can authorize the unwrap.
    # The enclave's /v1/envelope/decrypt accepts either credential. Only pass
    # runtime_token through when present, so api-key callers are unchanged.
    decrypt_kwargs = {"runtime_token": runtime_token} if runtime_token else {}
    try:
        provider_key = core_enclave._decrypt_envelope_via_enclave(
            envelope,
            api_key,
            purpose="model_api_provider_key",
            **decrypt_kwargs,
        ).decode("utf-8")
    except Exception as e:
        return None, {"error": "model_api_key_decrypt_failed", "detail": str(e)[:220]}
    try:
        return _provider_config_from_plain(route, provider_key)
    except provider_client.ProviderError as e:
        return None, {"error": "model_api_config_invalid", "detail": str(e)}


# ---------------------------------------------------------------------------
# Hosted model-API execution is Runtime V2 only. ``resident_cli`` remains a
# persisted *dormant fence* while credentials/routes are being deleted or
# replaced and as the control default for explicitly independent `/v1/chat/*`
# resident accounts. It is not a selectable hosted runtime.
# ---------------------------------------------------------------------------

HOSTED_RUNTIME_MODE_RESIDENT = "resident_cli"
HOSTED_RUNTIME_MODE_DB_ACTION_V2 = "db_action_v2"
_PERSISTED_HOSTED_RUNTIME_MODES = {
    HOSTED_RUNTIME_MODE_RESIDENT,
    HOSTED_RUNTIME_MODE_DB_ACTION_V2,
}
_SELECTABLE_HOSTED_RUNTIME_MODES = {HOSTED_RUNTIME_MODE_DB_ACTION_V2}

HOSTED_RUNTIME_POLICY_ENV = "FEEDLING_HOSTED_RUNTIME_POLICY"
HOSTED_RUNTIME_POLICY_V2_ONLY = "v2_only"
_HOSTED_RUNTIME_POLICIES = {HOSTED_RUNTIME_POLICY_V2_ONLY}

# Provider/config-scoped fields can be discarded when the user deletes every
# model API credential. Correctness state (notably v2_reply_cursor_seq), rollout
# flags, and unknown future control keys deliberately remain in the blob.
_MODEL_API_DELETE_REMOVE_KEYS = (
    "provider",
    "model",
    "memory_quality_warning",
    "last_runtime_error",
    "last_runtime_error_class",
    "recap_cursor",
    "last_recap_at",
    "last_action_trace_id",
)


def effective_hosted_runtime_mode(value: object) -> str:
    """Normalize the persisted ownership fence.

    ``resident_cli`` is retained only as a dormant/deletion fence and for
    explicitly independent `/v1/chat/*` consumers. Hosted send requires the
    exact V2 tuple and fails closed for every other value. Strict callers
    surface database read failures before reaching this normalizer.
    """
    mode = str(value or "")
    return (
        mode
        if mode in _PERSISTED_HOSTED_RUNTIME_MODES
        else HOSTED_RUNTIME_MODE_RESIDENT
    )


def hosted_runtime_policy() -> str:
    """Return the process-wide ownership policy, rejecting configuration typos.

    The hosted resident rollback selector is retired. The only valid value is
    ``v2_only``; ownership is materialized through the existing generation-
    fenced transition before the HTTP workers start.
    """
    policy = str(
        os.environ.get(HOSTED_RUNTIME_POLICY_ENV, HOSTED_RUNTIME_POLICY_V2_ONLY)
        or HOSTED_RUNTIME_POLICY_V2_ONLY
    ).strip().lower()
    if policy not in _HOSTED_RUNTIME_POLICIES:
        raise RuntimeError(
            f"{HOSTED_RUNTIME_POLICY_ENV} must be one of "
            f"{sorted(_HOSTED_RUNTIME_POLICIES)!r}; got {policy!r}"
        )
    return policy


def forced_hosted_runtime_mode() -> str:
    hosted_runtime_policy()  # validate a possibly supplied environment value
    return HOSTED_RUNTIME_MODE_DB_ACTION_V2


def get_hosted_runtime_mode(store: UserStore) -> str:
    """读取持久化 ownership fence；hosted send 只接受精确 V2 tuple。"""
    profile = _load_model_api_runtime_profile(store) or {}
    return effective_hosted_runtime_mode(profile.get("hosted_runtime_mode"))


def get_hosted_runtime_mode_strict(store: UserStore) -> str:
    """Control-plane read that distinguishes a DB error from an absent flag."""
    profile = db.get_blob_strict(store.user_id, MODEL_API_RUNTIME_BLOB)
    profile = profile if isinstance(profile, dict) else {}
    return effective_hosted_runtime_mode(profile.get("hosted_runtime_mode"))


def get_hosted_runtime_control_strict(
    store: UserStore,
) -> tuple[str, str, int]:
    """Read normalized blob mode plus authoritative state/generation once."""
    raw_mode, state, generation = db.get_hosted_runtime_control_strict(
        store.user_id)
    if state not in {"resident", "draining", "v2"}:
        raise RuntimeError(f"invalid hosted runtime state: {state!r}")
    return effective_hosted_runtime_mode(raw_mode), state, generation


def hosted_runtime_v2_enabled_strict(store: UserStore) -> bool:
    """True only when routing and the authoritative ownership row agree."""
    mode, state, _generation = get_hosted_runtime_control_strict(store)
    return mode == HOSTED_RUNTIME_MODE_DB_ACTION_V2 and state == "v2"


def _set_hosted_runtime_mode_for_user_id(
    user_id: str,
    mode: str,
    *,
    store: UserStore | None = None,
) -> str:
    """Serialize ownership changes with provider-config mutation for this user."""
    with db.hosted_runtime_config_mutation_lock(user_id):
        return _set_hosted_runtime_mode_for_user_id_locked(
            user_id, mode, store=store
        )


def _set_hosted_runtime_mode_for_user_id_locked(
    user_id: str,
    mode: str,
    *,
    store: UserStore | None = None,
) -> str:
    """Generation-fenced runtime transition used by policy and admin paths."""
    if mode not in _SELECTABLE_HOSTED_RUNTIME_MODES:
        raise ValueError(
            f"hosted resident runtime is retired; expected "
            f"{HOSTED_RUNTIME_MODE_DB_ACTION_V2!r}"
        )
    # Fleet reconciliation runs before Gunicorn forks and must remain a DB-only
    # control-plane operation. Constructing UserStore here hydrates chat, frames,
    # memory, push state, and other per-user data; doing that serially for every
    # account makes a safe startup backfill scale with conversation history.
    # These config/profile helpers only require ``user_id``, so use a tiny ref.
    store_ref = store or types.SimpleNamespace(user_id=str(user_id))
    config = _load_model_api_config(store_ref)
    if not config and mode == HOSTED_RUNTIME_MODE_DB_ACTION_V2:
        raise ValueError("cannot set hosted_runtime_mode: user has no model_api config")

    if mode == HOSTED_RUNTIME_MODE_DB_ACTION_V2:
        # Seed first. Flipping ownership before a wake schedule exists creates a
        # window where the resident has been reaped but V2 proactive work has no
        # durable clock. Preserve an existing schedule rather than resetting its
        # next heartbeat every time startup reconciliation runs.
        from model_api_runtime.v2 import jobs_store

        if jobs_store.get_wake_schedule(store_ref.user_id) is None:
            jobs_store.upsert_wake_schedule(
                store_ref.user_id, next_heartbeat_at=time.time()
            )
    # Read control state fail-loud. A missing profile is a real state (seed it
    # for configured users); a DB failure must never be mistaken for that state.
    existing = db.get_blob_strict(store_ref.user_id, MODEL_API_RUNTIME_BLOB)
    if isinstance(existing, dict) and config:
        persisted = _ensure_model_api_runtime_profile(store_ref, config)
    elif isinstance(existing, dict):
        persisted = dict(existing)
    elif config:
        persisted = _ensure_model_api_runtime_profile(store_ref, config)
    else:
        # Route deletion must fence orphaned V2 controls even after every route
        # was removed. The correctness blob can be materialized without provider
        # metadata; deletion intentionally preserves it for the reply cursor.
        persisted = {}
    if persisted is None:
        raise ValueError("cannot set hosted_runtime_mode: user has no model_api config")
    expected_state = "v2" if mode == HOSTED_RUNTIME_MODE_DB_ACTION_V2 else "resident"
    raw_mode, state, _generation = db.get_hosted_runtime_control_strict(
        store_ref.user_id
    )
    if (
        raw_mode == mode
        and state == expected_state
        and persisted.get("runtime_mode") == MODEL_API_RUNTIME_MODE
        and persisted.get("runtime_version") == MODEL_API_RUNTIME_VERSION
    ):
        if mode == HOSTED_RUNTIME_MODE_DB_ACTION_V2:
            _recover_cutover_chat_if_needed(store_ref.user_id)
        return mode
    db.patch_blob_strict(
        store_ref.user_id,
        MODEL_API_RUNTIME_BLOB,
        {
            "hosted_runtime_mode": mode,
            "runtime_mode": MODEL_API_RUNTIME_MODE,
            "runtime_version": MODEL_API_RUNTIME_VERSION,
            "updated_at": core_util._now_iso(),
        },
        runtime_state_target=(
            "v2" if mode == HOSTED_RUNTIME_MODE_DB_ACTION_V2 else "resident"
        ),
        require_active_hosted_route=(
            mode == HOSTED_RUNTIME_MODE_DB_ACTION_V2
        ),
    )
    readback = db.get_blob_strict(store_ref.user_id, MODEL_API_RUNTIME_BLOB)
    if not isinstance(readback, dict) or readback.get("hosted_runtime_mode") != mode:
        raise RuntimeError("hosted_runtime_mode persistence verification failed")
    raw_mode, state, _generation = db.get_hosted_runtime_control_strict(
        store_ref.user_id
    )
    if raw_mode != mode or state != expected_state:
        raise RuntimeError("hosted_runtime ownership verification failed")
    if mode == HOSTED_RUNTIME_MODE_DB_ACTION_V2:
        _recover_cutover_chat_if_needed(store_ref.user_id)
    return mode


def _recover_cutover_chat_if_needed(user_id: str) -> None:
    """Immediately hand any resident-era unanswered row to the V2 chat lane."""
    if not db.reconcile_unenqueued_v2_message_for_user(user_id):
        return
    from core import wake_bus

    wake_bus.notify("v2_jobs", user_id=user_id)


def set_hosted_runtime_mode(store: UserStore, mode: str) -> str:
    """切换该用户的 hosted 运行时模式。非法值、或用户尚无 model_api config
    导致无法落地时，抛 ValueError（绝不返回假成功）。返回真正落地后的 mode。"""
    forced_mode = forced_hosted_runtime_mode()
    if mode != forced_mode:
        raise ValueError(
            "hosted resident runtime is retired; "
            f"policy {hosted_runtime_policy()!r} requires {forced_mode!r}"
        )
    return _set_hosted_runtime_mode_for_user_id(
        store.user_id, mode, store=store
    )


def apply_hosted_runtime_policy(store: UserStore) -> str | None:
    """Materialize a forced policy for one configured user, if configured."""
    target = forced_hosted_runtime_mode()
    return _set_hosted_runtime_mode_for_user_id(
        store.user_id, target, store=store
    )


def reconcile_hosted_runtime_policy() -> dict:
    """Synchronously materialize forced ownership for every runnable user.

    Called before HTTP workers fork. Any failure propagates so a V2-only Pre
    cannot start while silently leaving an account on the resident runtime.
    Reapplying the same target is idempotent at the runtime-generation layer.
    """
    policy = hosted_runtime_policy()
    target = forced_hosted_runtime_mode()
    controls = db.list_hosted_runtime_eligible_controls()
    user_ids = [row[0] for row in controls]
    for user_id in user_ids:
        _set_hosted_runtime_mode_for_user_id(user_id, target)
    result = {
        "policy": policy,
        "eligible": len(user_ids),
        "reconciled": len(user_ids),
    }
    print(f"[hosted-runtime-policy] {result}", flush=True)
    return result


def hosted_runtime_policy_status() -> dict:
    """Read-only deploy gate for policy coverage across runnable accounts."""
    policy = hosted_runtime_policy()
    target = forced_hosted_runtime_mode()
    controls = db.list_hosted_runtime_eligible_controls()
    ready = 0
    inconsistent: list[str] = []
    for user_id, raw_mode, state, _generation in controls:
        effective_mode = effective_hosted_runtime_mode(raw_mode)
        expected_state = (
            "v2"
            if effective_mode == HOSTED_RUNTIME_MODE_DB_ACTION_V2
            else "resident"
        )
        target_ready = effective_mode == target
        if state == expected_state and target_ready:
            ready += 1
        else:
            inconsistent.append(user_id)
    return {
        "policy": policy,
        "target_mode": target,
        "eligible_count": len(controls),
        "ready_count": ready,
        "inconsistent_count": len(inconsistent),
        "inconsistent_user_ids": inconsistent,
    }


def prepare_model_api_delete(store: UserStore) -> dict:
    """Fence V2 work before credentials disappear, preserving reply history.

    This does not require an active provider route: deletion is idempotent and
    must also repair stale/split-brain runtime state. The runtime row and blob
    mode move to resident in the same transaction, bumping generation whenever
    routing actually changes so pending V2 effects become ineligible. The
    durable seq cursor remains in ``model_api_runtime``; deleting that cursor
    would replay the user's old conversation if they configured a provider
    again later.
    """
    persisted = db.patch_blob_strict(
        store.user_id,
        MODEL_API_RUNTIME_BLOB,
        {
            "hosted_runtime_mode": HOSTED_RUNTIME_MODE_RESIDENT,
            "runtime_mode": MODEL_API_RUNTIME_MODE,
            "runtime_version": MODEL_API_RUNTIME_VERSION,
            "updated_at": core_util._now_iso(),
        },
        remove_keys=_MODEL_API_DELETE_REMOVE_KEYS,
        runtime_state_target="resident",
    )
    if (
        not isinstance(persisted, dict)
        or persisted.get("hosted_runtime_mode") != HOSTED_RUNTIME_MODE_RESIDENT
    ):
        raise RuntimeError("model_api delete runtime fence did not persist")
    return persisted


def fence_hosted_runtime_for_config_change(store: UserStore) -> dict:
    """Invalidate current V2 work while preserving live config metadata.

    Route/credential mutation can race a stale ``is_active`` snapshot. Fencing
    every successful deletion is conservative but safe; unlike full config
    deletion this deliberately keeps provider-scoped profile fields because an
    already-tested replacement may become active immediately afterward.
    """
    persisted = db.patch_blob_strict(
        store.user_id,
        MODEL_API_RUNTIME_BLOB,
        {
            "hosted_runtime_mode": HOSTED_RUNTIME_MODE_RESIDENT,
            "runtime_mode": MODEL_API_RUNTIME_MODE,
            "runtime_version": MODEL_API_RUNTIME_VERSION,
            "updated_at": core_util._now_iso(),
        },
        runtime_state_target="resident",
    )
    if (
        not isinstance(persisted, dict)
        or persisted.get("hosted_runtime_mode") != HOSTED_RUNTIME_MODE_RESIDENT
    ):
        raise RuntimeError("model_api config-change runtime fence did not persist")
    return persisted
