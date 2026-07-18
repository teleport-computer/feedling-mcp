"""Model API config / runtime profile / action traces (hosted line)."""

import os
import time
import uuid


import db
from core import enclave as core_enclave
from provider_client import validate_config as validate_provider_config
from core.store import UserStore


from core import util as core_util
import provider_client
from notices import core as notices_core
from notices import catalog as notices_catalog


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
        "api_key_hint": route["api_key_hint"],
        "supports_responses": route["supports_responses"],
        "test_status": route["test_status"],
        "last_test_at": route["last_test_at"],
        "last_test_error": route["last_test_error"],
    }
    if route.get("reasoning_effort"):
        config["reasoning_effort"] = route["reasoning_effort"]
    return config


def _save_model_api_config(store: UserStore, config: dict) -> dict:
    data = dict(config)
    data["route"] = "model_api"
    data["updated_at"] = core_util._now_iso()
    if not data.get("created_at"):
        data["created_at"] = data["updated_at"]
    db.set_blob(store.user_id, "model_api", data)
    return data


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


def _save_model_api_runtime_profile(store: UserStore, profile: dict) -> dict:
    data = dict(profile)
    data["runtime_mode"] = MODEL_API_RUNTIME_MODE
    data["runtime_version"] = MODEL_API_RUNTIME_VERSION
    data["updated_at"] = core_util._now_iso()
    if not data.get("created_at"):
        data["created_at"] = data["updated_at"]
    db.set_blob(store.user_id, MODEL_API_RUNTIME_BLOB, data)
    return data


def _ensure_model_api_runtime_profile(
    store: UserStore,
    config: dict | None = None,
    *,
    touch: bool = False,
) -> dict | None:
    """Lazily migrate model_api users into the hosted resident runtime.

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
    merged = dict(profile)
    merged.update({k: v for k, v in patch.items() if v is not None})
    return _save_model_api_runtime_profile(store, merged)


def record_runtime_error(store: UserStore, *, error: str, error_class: str = "") -> tuple[dict, int]:
    """agent-runner consumer 上报（或清空）最近一次回合失败原因。

    写 active route 行（``model_api_routes.last_runtime_error*``）。读侧是 setup_core 的
    last_runtime_error（iOS 设置页，也已切到读 route）与 GET /v1/model_api/routes。
    legacy inline 路径经 action-trace 写同一字段；本函数是 agent-runner 路径的对等写侧
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
    return provider_client.ProviderConfig(provider=provider, model=model, api_key=api_key, base_url=base_url)


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
    # A hosted (host-all) turn authenticates with a runtime token, not the
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
