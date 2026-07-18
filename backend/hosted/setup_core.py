"""Framework-neutral hosted-setup operations (ASGI-migration plan §5.3).

A pure relocation of the Flask ``hosted.setup_routes`` route bodies so both the
Flask adapter (``hosted.setup_routes``) and the native FastAPI router
(``hosted.setup_routes_asgi``) share one implementation and return
byte-identical responses.

E2E / enclave boundary (unchanged): ``/v1/model_api/key_envelope`` returns the
caller's OWN ``api_key_envelope`` ciphertext — the server never decrypts it; only
the enclave can. ``model_api_setup`` seals a freshly-supplied provider key into a
shared envelope via ``core.envelope._build_shared_envelope_for_store`` and, when
reusing a saved key, decrypts the existing envelope ONLY through the enclave
(``core.enclave._decrypt_envelope_via_enclave``). These functions take
already-parsed params + the store and the caller's credential as explicit
arguments — they never read ``flask.request`` — so no new server-side plaintext
is ever introduced here. Module-level references (``provider_client``,
``core_enclave`` …) are preserved so tests can monkeypatch the attributes.
"""

from __future__ import annotations

import threading
import uuid
from functools import wraps

import db
from core import enclave as core_enclave
from core import envelope as core_envelope
from core import util as core_util
from core.store import UserStore
from accounts import onboarding as accounts_onboarding
from memory import service as memory_service
import provider_client
from hosted import agent_runtime_cutover
from hosted import config_store as hosted_config_store
from hosted import turn as hosted_turn


_REASONING_EFFORT_OFF = {"off", "none", "no", "false", "0", "disabled"}
_REASONING_EFFORT_LEVELS = {"low", "medium", "high"}


def _normalize_reasoning_effort(value) -> str | None:
    """Canonical per-user reasoning switch for hosted model_api config.

    ``None`` means the field was absent/blank and should not be persisted, so the
    gateway follows its global default. ``off`` is persisted when explicitly set,
    which gives operations a visible per-user opt-out.
    """
    if value is None:
        return None
    raw = str(value).strip().lower()
    if not raw:
        return None
    if raw in _REASONING_EFFORT_OFF:
        return "off"
    if raw in _REASONING_EFFORT_LEVELS:
        return raw
    if raw.isdigit() and int(raw) > 0:
        return str(int(raw))
    raise ValueError("reasoning_effort must be off, low, medium, high, or a positive integer")


def _public_route(route: dict | None) -> dict:
    """active route → GET /v1/model_api/get 的扁平投影（与旧 blob 投影同形）。

    绝不带 ``api_key_envelope``——它只在 GET /v1/model_api/key_envelope 单独回。"""
    if not route:
        return {"configured": False}
    safe = {
        "provider": route["provider"],
        "model": route["model"],
        "base_url": route["base_url"],
        "api_key_hint": route["api_key_hint"],
        "test_status": route["test_status"],
        "last_test_at": route["last_test_at"],
        # created_at/updated_at were always in the pre-migration public_config()
        # projection (empty string when unset) — keep them so GET /v1/model_api/get's
        # response key set is unchanged.
        "created_at": route.get("created_at", ""),
        "updated_at": route.get("updated_at", ""),
        "last_test_error": route["last_test_error"],
        "configured": True,
        "privacy_mode": "tdx_cvm_backend_runtime_option_a",
    }
    if route.get("reasoning_effort"):
        safe["reasoning_effort"] = route["reasoning_effort"]
    return safe


def _resolve_provider_key(store, raw_key: str, existing: dict | None,
                          caller_api_key: str | None):
    """返回 (provider_key, envelope, api_key_hint)；失败时返回 (None, error_body, status)。

    raw_key 非空 → 新封一个信封。raw_key 为空 → 复用 existing credential 的信封，
    经 enclave 解出明文用于测活（这是「换 model 不重输 key」的路径）。两条路径都
    返回 3 元组——``model_api_setup`` 依赖这个元数解包，务必保持。"""
    if raw_key:
        envelope, err = core_envelope._build_shared_envelope_for_store(
            store, raw_key.encode("utf-8"), item_id=f"model_api_key_{uuid.uuid4().hex}")
        if envelope is None:
            return None, {
                "error": "cannot_encrypt_provider_key",
                "detail": err,
                "required": (
                    "The user must have a content public key and the enclave "
                    "attestation endpoint must be reachable before saving a provider key."
                ),
            }, 409
        return raw_key, envelope, provider_client.mask_api_key(raw_key)

    existing_envelope = (existing or {}).get("api_key_envelope")
    if not isinstance(existing_envelope, dict):
        return None, {"error": "api_key required"}, 400
    try:
        provider_key = core_enclave._decrypt_envelope_via_enclave(
            existing_envelope, caller_api_key, purpose="model_api_provider_key",
        ).decode("utf-8")
    except Exception as e:
        return None, {"error": "model_api_key_decrypt_failed", "detail": str(e)[:220]}, 400
    return provider_key, existing_envelope, str(existing.get("api_key_hint") or "saved key")


def _emit_responses_support_notice(store, provider: str, supports_responses: bool,
                                   base_url: str) -> list[dict]:
    """openai_compatible 中转不实现 /v1/responses → LiteLLM 强制 chat-completions 桥接
    → mangle codex 工具循环 → 记忆/工具静默不可靠(rc=0 但工具从不真调,无限"我再试
    一次")。配置期就能预知,双写:
      ① setup 响应带 warnings → 设置页保存后当场显示(不必等通知中心页)
      ② 通知中心 emit → 持久化,通知中心页/其它端也能看到
    换到支持 /v1/responses 的中转(或非 openai_compatible provider)时 resolve。"""
    warnings: list[dict] = []
    try:
        from notices import core as notices_core
        from notices import catalog as notices_catalog
        _ec = "responses_unsupported"
        if provider == "openai_compatible" and not supports_responses:
            _blame = notices_catalog.blame_for(_ec)
            _text = notices_catalog.user_text_for(_ec)
            warnings.append({"error_class": _ec, "blame": _blame,
                             "severity": "warning", "user_text": _text})
            notices_core.emit(
                store, source="model_api", error_class=_ec,
                blame=_blame, severity="warning", user_text=_text,
                detail=f"probe /v1/responses -> supported=False (base_url={base_url})",
                dedupe_key=f"model_api:{_ec}")
        else:
            notices_core.resolve(store, f"model_api:{_ec}")
    except Exception:
        pass  # 扇出绝不影响 setup 主职责(emit/resolve 本身已 never-raise,这是双保险)
    return warnings


def _apply_runtime_policy_or_error(store) -> tuple[dict, int] | None:
    """Apply environment-forced runtime ownership before reporting success."""
    try:
        hosted_config_store.apply_hosted_runtime_policy(store)
    except Exception as exc:  # noqa: BLE001 — control-plane failures fail closed
        print(
            f"[model_api:{store.user_id}] runtime policy apply FAILED "
            f"error={type(exc).__name__}:{str(exc)[:160]}"
        )
        return {"error": "runtime_policy_unavailable"}, 503
    return None


def _runtime_should_restore_v2(store) -> bool:
    """Whether an active-config replacement should resume V2 after fencing."""
    forced_mode = hosted_config_store.forced_hosted_runtime_mode()
    if forced_mode is not None:
        return (
            forced_mode
            == hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
        )
    return hosted_config_store.hosted_runtime_v2_enabled_strict(store)


def _restore_v2_or_error(store, *, required: bool) -> tuple[dict, int] | None:
    if not required:
        return None
    try:
        hosted_config_store.set_hosted_runtime_mode(
            store, hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
        )
    except Exception as exc:  # noqa: BLE001 — fail closed after config mutation
        print(
            f"[model_api:{store.user_id}] runtime V2 restore FAILED "
            f"error={type(exc).__name__}:{str(exc)[:160]}"
        )
        return {"error": "runtime_policy_unavailable"}, 503
    return None


def _fence_v2_config_change_or_error(
    store, *, required: bool
) -> tuple[dict, int] | None:
    """Invalidate the provider snapshot of any in-flight V2 turn."""
    if not required:
        return None
    try:
        hosted_config_store.fence_hosted_runtime_for_config_change(store)
    except Exception as exc:  # noqa: BLE001 — config mutation must fail closed
        print(
            f"[model_api:{store.user_id}] runtime config fence FAILED "
            f"error={type(exc).__name__}:{str(exc)[:160]}"
        )
        return {"error": "model_api_runtime_disable_failed"}, 500
    return None


def _restore_v2_if_runnable_or_error(
    store, *, required: bool
) -> tuple[dict, int] | None:
    """Best-effort rollback of a pre-mutation fence after a write failure."""
    if not required:
        return None
    active = db.model_api_active_route(store.user_id)
    if not active or active.get("test_status") != "ok":
        return None
    return _restore_v2_or_error(store, required=True)


def _restore_after_setup_write_failure(
    store,
    *,
    required: bool,
    active_credential_mutated: bool,
    credential_id: str,
) -> tuple[dict, int] | None:
    """Restore only when setup left the prior active provider identity intact."""
    if not active_credential_mutated:
        return _restore_v2_if_runnable_or_error(store, required=required)

    # The replacement key was tested for the requested model, not necessarily
    # for the previously active model. A later route write failed, so that old
    # route now points at a key whose own model permission is unproven. Keep the
    # runtime resident and remove the stale runnable proof; a successful setup
    # retry will test, mark OK, activate, and restore V2 atomically under the
    # per-user config lock.
    active = db.model_api_active_route(store.user_id)
    if active and active.get("credential_id") == credential_id:
        if not db.model_api_route_mark_test(
            store.user_id,
            active["id"],
            status="failed",
            error="config_update_incomplete",
        ):
            print(
                f"[model_api:{store.user_id}] failed to revoke stale active "
                "route proof after setup write failure"
            )
    return None


def _serialized_model_api_mutation(fn):
    """Cross-process per-user critical section for config + runtime ownership."""
    @wraps(fn)
    def _wrapped(store, *args, **kwargs):
        try:
            with db.hosted_runtime_config_mutation_lock(store.user_id):
                return fn(store, *args, **kwargs)
        except db.HostedRuntimeConfigBusyError:
            return {"error": "model_api_config_busy"}, 503

    return _wrapped


@_serialized_model_api_mutation
def model_api_setup(store, payload: dict, *, caller_api_key: str | None) -> tuple[dict, int]:
    provider = str(payload.get("provider") or "")
    model = str(payload.get("model") or "")
    base_url = str(payload.get("base_url") or "")
    raw_key = str(payload.get("api_key") or "").strip()
    try:
        reasoning_effort = _normalize_reasoning_effort(payload.get("reasoning_effort"))
    except ValueError as e:
        return {"error": "invalid_reasoning_effort", "detail": str(e)}, 400
    try:
        provider, model, base_url = provider_client.validate_config(provider, model, base_url)
    except provider_client.ProviderError as e:
        return {"error": str(e)}, 400

    # 幂等锚点：当前 active route 的 credential。若它的 (provider, base_url) 与
    # 请求匹配，就复用/更新它；否则新建一条。credentials 没有唯一索引（同 provider
    # 允许多把 key），所以幂等必须在这里用代码保证，不能靠 ON CONFLICT。
    active = hosted_config_store.load_active_route(store)
    reuse = bool(active
                 and active["provider"] == provider
                 and active["base_url"] == base_url)
    existing = None
    if reuse:
        existing = db.model_api_credential_get(store.user_id, active["credential_id"])

    # raw_key 为空 → 复用 existing 的信封（「换 model 不重输 key」的路径）
    provider_key, envelope, api_key_hint = _resolve_provider_key(
        store, raw_key, existing, caller_api_key)
    if provider_key is None:
        return envelope, api_key_hint      # (error_body, status)

    try:
        provider_client.test_provider_key(
            provider_client.ProviderConfig(provider, model, provider_key, base_url))
    except provider_client.ProviderError as e:
        # Log enough to triage a user-reported "key won't validate" without ever
        # logging the raw key: the failure detail (e.g. provider_http_404 for a
        # bad model name, or 401/429 for a bad/quota'd key) only lives here.
        print(
            f"[model_api:{store.user_id}] setup FAILED provider={provider} "
            f"model={model} status_code={e.status_code} detail={str(e)[:160]}"
        )
        return {"error": "provider_test_failed", "detail": str(e),
                "status_code": e.status_code}, 400

    # For openai_compatible relays, probe the Responses capability once and
    # persist it for the native provider client. This is transport metadata for
    # the unified V2 loop, not a runtime selector.
    supports_responses = False
    if provider == "openai_compatible":
        supports_responses = provider_client.probe_responses_support(
            provider_client.ProviderConfig(provider, model, provider_key, base_url))
        print(
            f"[model_api:{store.user_id}] openai_compatible /responses probe -> "
            f"supports={supports_responses} base_url={base_url}"
        )

    try:
        restore_v2 = _runtime_should_restore_v2(store)
    except Exception:
        return {"error": "runtime_control_unavailable"}, 503
    fence_error = _fence_v2_config_change_or_error(
        store, required=restore_v2
    )
    if fence_error is not None:
        return fence_error

    active_credential_mutated = False
    if reuse and existing:
        credential_id = existing["id"]
        # Must check: on rotate-key setup this envelope is the NEW key that just
        # passed test_provider_key above. A silently-swallowed write failure here
        # would leave the OLD envelope in the credential row while the response
        # below still says "configured" — the exact "false success" this
        # write-result-checking pass exists to catch (mirrors the credential_patch
        # rotate-key check further down).
        if not db.model_api_credential_update(
                store.user_id, credential_id,
                api_key_envelope=envelope, api_key_hint=api_key_hint,
                supports_responses=supports_responses):
            restore_error = _restore_v2_if_runnable_or_error(
                store, required=restore_v2
            )
            if restore_error is not None:
                return restore_error
            return {"error": "model_api_credential_write_failed"}, 500
        active_credential_mutated = True
    else:
        credential_id = db.model_api_credential_create(
            store.user_id, provider=provider, base_url=base_url,
            label=provider.replace("_", " ").title(),
            api_key_envelope=envelope, api_key_hint=api_key_hint,
            supports_responses=supports_responses)
        if not credential_id:
            restore_error = _restore_v2_if_runnable_or_error(
                store, required=restore_v2
            )
            if restore_error is not None:
                return restore_error
            return {"error": "model_api_credential_write_failed"}, 500

    route_id = db.model_api_route_upsert(
        store.user_id, credential_id, model, reasoning_effort)
    if not route_id:
        restore_error = _restore_after_setup_write_failure(
            store,
            required=restore_v2,
            active_credential_mutated=active_credential_mutated,
            credential_id=credential_id,
        )
        if restore_error is not None:
            return restore_error
        return {"error": "model_api_route_write_failed"}, 500
    # mark_test / activate can each report False — either a DB hiccup (db.py swallows
    # the exception) or a concurrent DELETE /v1/model_api/delete that CASCADE-removed
    # this route between the upsert and the activate (a race the old single set_blob
    # write could not hit). Surface 500 instead of a "configured" 200 describing a
    # route that was never activated.
    if not db.model_api_route_mark_test(store.user_id, route_id, status="ok"):
        restore_error = _restore_after_setup_write_failure(
            store,
            required=restore_v2,
            active_credential_mutated=active_credential_mutated,
            credential_id=credential_id,
        )
        if restore_error is not None:
            return restore_error
        return {"error": "model_api_route_write_failed"}, 500
    if not db.model_api_route_activate(store.user_id, route_id):
        restore_error = _restore_after_setup_write_failure(
            store,
            required=restore_v2,
            active_credential_mutated=active_credential_mutated,
            credential_id=credential_id,
        )
        if restore_error is not None:
            return restore_error
        return {"error": "model_api_route_write_failed"}, 500

    # Rollout flags / last_action_trace_* still live in the model_api_runtime blob;
    # seed it so onboarding validate's hosted_runtime step and GET /v1/model_api/runtime
    # see a live profile. (This is the ONLY blob setup still writes.)
    hosted_config_store._ensure_model_api_runtime_profile(
        store, {"provider": provider, "model": model}, touch=True)
    restore_error = _restore_v2_or_error(store, required=restore_v2)
    if restore_error is not None:
        return restore_error
    policy_error = _apply_runtime_policy_or_error(store)
    if policy_error is not None:
        return policy_error
    accounts_onboarding._save_onboarding_route(store, "model_api")
    print(f"[model_api:{store.user_id}] setup provider={provider} model={model}")

    warnings = _emit_responses_support_notice(store, provider, supports_responses, base_url)

    route = hosted_config_store.load_active_route(store)
    resp = {"status": "configured", "config": _public_route(route)}
    if warnings:
        resp["warnings"] = warnings
    return resp, 200


def model_api_get(store) -> tuple[dict, int]:
    return {"config": _public_route(hosted_config_store.load_active_route(store))}, 200


def model_api_set_hosting(store) -> tuple[dict, int]:
    """报告该用户派生的 agent driver。AGENT 由 provider 自动派生，配了即托管；
    本端点不再有 enable/disable 开关（保留以兼容旧 client）。"""
    route = hosted_config_store.load_active_route(store)
    if not route:
        return {"error": "model_api_not_configured"}, 404
    try:
        driver = agent_runtime_cutover.resolve_driver(route)
    except agent_runtime_cutover.UnsupportedProviderError:
        return {"error": "provider_not_hostable"}, 409
    print(f"[model_api:{store.user_id}] provider={route.get('provider')} -> driver={driver}")
    return {
        "status": "ok",
        "enabled": True,
        "driver": driver,
        "config": _public_route(route),
    }, 200


def model_api_key_envelope(store) -> tuple[dict, int]:
    """Return the caller's OWN ``api_key_envelope`` ciphertext (active credential).

    Compatibility endpoint for explicitly independent consumers that need to
    fetch the provider-key envelope and enclave-decrypt it JIT. Hosted Runtime
    V2 reads the active route directly and does not use a static roster. The
    envelope is ciphertext the API process cannot decrypt; only the enclave can.

    An unconfigured user (no active route) collapses to the same
    ``model_api_key_envelope_missing`` 404 the legacy blob path returned, keeping the
    HTTP contract unchanged."""
    route = hosted_config_store.load_active_route(store)
    envelope = (route or {}).get("api_key_envelope")
    if not isinstance(envelope, dict):
        return {"error": "model_api_key_envelope_missing"}, 404
    return {"api_key_envelope": envelope}, 200


def _test_active_route(store, route: dict, api_key: str | None) -> tuple[dict, int] | None:
    """Decrypt the active route's envelope via the enclave and test the provider key,
    writing the ok/failed result back to the route row. Returns ``None`` on success
    or ``(error_body, status)`` on failure.

    Unlike ``_load_runtime_provider_config`` this has NO ``test_status == 'ok'`` gate,
    so ``/v1/model_api/test`` can validate an as-yet-untested config (the old blob path
    could not). The server never sees plaintext except the transient decrypted key held
    to make the test call."""
    envelope = route.get("api_key_envelope")
    if not isinstance(envelope, dict):
        return {"error": "model_api_key_envelope_missing"}, 404
    try:
        provider_key = core_enclave._decrypt_envelope_via_enclave(
            envelope, api_key, purpose="model_api_provider_key").decode("utf-8")
    except Exception as e:
        return {"error": "model_api_key_decrypt_failed", "detail": str(e)[:220]}, 400
    try:
        provider, model, base_url = provider_client.validate_config(
            route["provider"], route["model"], route["base_url"])
    except provider_client.ProviderError as e:
        return {"error": "model_api_config_invalid", "detail": str(e)}, 400
    try:
        provider_client.test_provider_key(
            provider_client.ProviderConfig(provider, model, provider_key, base_url))
    except provider_client.ProviderError as e:
        # Not checked on purpose: the response below is already an accurate 400
        # (provider_test_failed) regardless of whether this write lands, so callers
        # never see a false success here. Worst case on a swallowed write failure is
        # the route's test_status stays at its pre-test value instead of flipping to
        # 'failed' — a latent staleness, not a lie told to this caller.
        db.model_api_route_mark_test(store.user_id, route["id"], status="failed",
                                     error=str(e)[:240])
        return {"error": "provider_test_failed", "detail": str(e),
                "status_code": e.status_code}, 400
    # Must check: returning None here tells model_api_test() "success" -> 200. If
    # this write silently fails, test_status never flips to 'ok', so the route can
    # stay excluded from the agent-runtime roster (which gates on test_status='ok')
    # even though the caller was just told the test succeeded — the false-success
    # pattern this pass exists to catch.
    if not db.model_api_route_mark_test(store.user_id, route["id"], status="ok"):
        return {"error": "model_api_route_write_failed"}, 500
    return None


@_serialized_model_api_mutation
def model_api_test(store, *, api_key: str | None) -> tuple[dict, int]:
    route = hosted_config_store.load_active_route(store)
    if not route:
        return {"error": "model_api_not_configured"}, 404
    err = _test_active_route(store, route, api_key)
    if err is not None:
        # The active route is no longer proven runnable. Fence the current V2
        # generation before returning the provider error so pending work cannot
        # survive into a later successful test/reconfiguration.
        try:
            hosted_config_store.prepare_model_api_delete(store)
        except Exception:
            return {"error": "model_api_runtime_disable_failed"}, 500
        return err
    policy_error = _apply_runtime_policy_or_error(store)
    if policy_error is not None:
        return policy_error
    print(f"[model_api:{store.user_id}] test ok provider={route['provider']} model={route['model']}")
    return {"status": "ok",
            "config": _public_route(hosted_config_store.load_active_route(store))}, 200


@_serialized_model_api_mutation
def model_api_delete(store) -> tuple[dict, int]:
    # Fence V2 and preserve its durable seq cursor BEFORE deleting provider
    # credentials. If this fails, fail closed: removing the credentials while a
    # current-generation turn/effect remains eligible would strand or misapply
    # work, and deleting the cursor would replay old history on reconfiguration.
    try:
        hosted_config_store.prepare_model_api_delete(store)
    except Exception:
        return {"error": "model_api_runtime_disable_failed"}, 500

    # Delete every credential (CASCADE removes routes) and the frozen legacy
    # model_api blob in one fail-loud transaction.  The old list/delete loop
    # used best-effort DB helpers; a read or delete outage could therefore
    # return 200 while BYOK ciphertext was still stored.  The
    # model_api_runtime control blob intentionally remains (resident mode +
    # correctness cursor/rollout state; provider-scoped keys were scrubbed by
    # prepare_model_api_delete above).
    try:
        deleted = db.model_api_config_delete_strict(store.user_id)
    except Exception:
        return {"error": "model_api_config_delete_failed"}, 500
    # Close the gap between the pre-delete fence and the credential transaction.
    # A concurrent setup/startup transition may have observed the route before it
    # disappeared and briefly moved the user back to V2. Re-fencing after the
    # delete invalidates every job/effect from that transient generation. The V2
    # transition itself revalidates an active tested route under the runtime-row
    # lock, so it cannot resurrect ownership after this point.
    try:
        hosted_config_store.prepare_model_api_delete(store)
    except Exception:
        return {"error": "model_api_runtime_disable_failed"}, 500
    # 配置没了,任何 config 期发出的 model_api 警告(如 responses_unsupported)也随之
    # 作废——否则 /v1/notices 会为一个已不存在的 provider 一直显示活跃警告。
    try:
        from notices import core as notices_core
        notices_core.resolve(store, "model_api:")
    except Exception:
        pass  # 扇出绝不影响 delete 主职责
    print(f"[model_api:{store.user_id}] deleted={deleted}")
    return {"deleted": deleted}, 200


def _model_api_recap_status(store: UserStore) -> dict:
    latest = hosted_turn._model_api_latest_recap_job(store)
    with hosted_turn._model_api_recap_active_lock:
        active = store.user_id in hosted_turn._model_api_recap_active_users
    if not latest:
        return {"status": "idle", "active": active}
    status = str(latest.get("status") or "idle")
    if active and status not in {"failed", "completed", "skipped"}:
        status = "running"
    return {
        "status": status,
        "active": active,
        "job_id": latest.get("job_id", ""),
        "mode": latest.get("mode", ""),
        "progress": latest.get("progress", 0),
        "updated_at": latest.get("completed_at") or latest.get("created_at") or "",
    }


def model_api_runtime_status(store, *, api_key: str | None) -> tuple[dict, int]:
    config = hosted_config_store._load_model_api_config(store)
    if not config:
        return {
            "configured": False,
            "runtime_mode": hosted_config_store.MODEL_API_RUNTIME_MODE,
            "runtime_version": hosted_config_store.MODEL_API_RUNTIME_VERSION,
            "recap_status": "idle",
            "memory_quality_warning": None,
        }, 200
    profile = hosted_config_store._ensure_model_api_runtime_profile(store, config) or {}
    scan = hosted_turn._model_api_memory_quality_scan(store, api_key=api_key, max_cards=120, fast=True)
    warning = scan.get("warning")
    if warning != profile.get("memory_quality_warning"):
        profile = hosted_config_store._patch_model_api_runtime_profile(store, {"memory_quality_warning": warning}) or profile
    latest_trace = hosted_config_store._latest_model_api_action_trace(store)
    recap = _model_api_recap_status(store)
    # last_runtime_error(_class) moved to the active route row (record_runtime_error
    # writes there now); the rest of the runtime metadata stays in the profile blob.
    active_route = hosted_config_store.load_active_route(store) or {}
    return {
        "configured": True,
        "runtime_mode": profile.get("runtime_mode") or hosted_config_store.MODEL_API_RUNTIME_MODE,
        "runtime_version": int(profile.get("runtime_version") or hosted_config_store.MODEL_API_RUNTIME_VERSION),
        "tool_action_enabled": bool(profile.get("tool_action_enabled", True)),
        "provider": config.get("provider", ""),
        "model": config.get("model", ""),
        "recap_status": recap.get("status", "idle"),
        "recap": recap,
        "memory_quality_warning": warning,
        "memory_quality": {
            "scanned": scan.get("scanned", 0),
            "issue_count": scan.get("issue_count", 0),
            "noisy_count": scan.get("noisy_count", 0),
            "duplicate_count": scan.get("duplicate_count", 0),
        },
        "last_action_trace_id": profile.get("last_action_trace_id", ""),
        "last_action_trace_at": profile.get("last_action_trace_at", ""),
        "last_action_trace_status": (latest_trace or {}).get("status", ""),
        "last_runtime_error": active_route.get("last_runtime_error", ""),
        "last_runtime_error_class": active_route.get("last_runtime_error_class", ""),
    }, 200


def model_api_memory_repair(store, payload: dict, *, api_key: str | None, testing: bool) -> tuple[dict, int]:
    mode = str(payload.get("mode") or "dry_run").strip().lower()
    if mode not in {"dry_run", "apply"}:
        return {"error": "mode must be dry_run or apply"}, 400
    archive_old = bool(payload.get("archive_old", True))
    scan = hosted_turn._model_api_memory_quality_scan(store, api_key=api_key, max_cards=2000)
    noisy_count = int(scan.get("noisy_count") or 0)
    new_cards_planned = max(6, min(30, noisy_count)) if noisy_count else 0
    preview = {
        "old_cards_detected": noisy_count,
        "issue_count": int(scan.get("issue_count") or 0),
        "duplicate_count": int(scan.get("duplicate_count") or 0),
        "new_cards_planned": new_cards_planned,
        "noisy_ids": scan.get("noisy_ids", [])[:80],
        "issues": scan.get("issues", [])[:20],
    }
    hosted_config_store._patch_model_api_runtime_profile(store, {
        "memory_quality_warning": scan.get("warning"),
        "last_memory_quality_scan_at": core_util._now_iso(),
    })
    if mode == "dry_run":
        return {
            "status": "completed",
            "mode": "dry_run",
            "preview": preview,
            "memory_quality": scan,
        }, 200
    if not preview["old_cards_detected"]:
        return {
            "status": "skipped",
            "mode": "apply",
            "reason": "no_noisy_memory_cards_detected",
            "preview": preview,
        }, 200

    runtime = hosted_config_store._load_runtime_provider_config(store, api_key)
    if isinstance(runtime, tuple):
        _, err = runtime
        return err, 400

    job = memory_service._append_memory_capture_job(store, {
        "mode": "repair",
        "status": "queued",
        "progress": 0,
        "old_cards_detected": preview["old_cards_detected"],
        "new_cards_planned": preview["new_cards_planned"],
        "repair_noisy_ids": preview["noisy_ids"],
        "archive_old": archive_old,
    })
    run_sync = bool(payload.get("synchronous") or payload.get("sync") or testing)
    if run_sync:
        hosted_turn._run_model_api_memory_repair_job(
            store,
            api_key,
            runtime,
            job["job_id"],
            noisy_ids=preview["noisy_ids"],
            archive_old=archive_old,
        )
        jobs = db.log_read(store.user_id, "memory_capture_jobs", limit=20)
        latest = next((item for item in reversed(jobs) if item.get("job_id") == job["job_id"]), job)
        return {
            "status": latest.get("status", "completed"),
            "mode": "apply",
            "job_id": job["job_id"],
            "job": latest,
            "preview": preview,
        }, 200

    thread = threading.Thread(
        target=hosted_turn._run_model_api_memory_repair_job,
        args=(store, api_key, runtime, job["job_id"]),
        kwargs={"noisy_ids": preview["noisy_ids"], "archive_old": archive_old},
        daemon=True,
    )
    thread.start()
    return {
        "status": "queued",
        "mode": "apply",
        "job_id": job["job_id"],
        "job": job,
        "preview": preview,
    }, 202


def state_receipts(store, limit_raw) -> tuple[dict, int]:
    try:
        limit = min(max(int(limit_raw), 1), 100)
    except (TypeError, ValueError):
        return {"error": "invalid limit"}, 400
    return {
        "receipts": hosted_turn._load_state_receipts(store, limit=limit),
        "pending": [
            {
                "id": item.get("id", ""),
                "created_at": item.get("created_at", ""),
                "expires_at": item.get("expires_at", 0),
                "action": ((item.get("runtime_action") or {}).get("runtime_type") or ""),
                "confidence": (item.get("runtime_action") or {}).get("confidence", 0),
            }
            for item in hosted_turn._state_pending_items(store)
        ],
    }, 200


def memory_capture_jobs(store, limit_raw) -> tuple[dict, int]:
    try:
        limit = min(max(int(limit_raw), 1), 100)
    except (TypeError, ValueError):
        return {"error": "invalid limit"}, 400
    jobs = db.log_read(store.user_id, "memory_capture_jobs", limit=limit)
    jobs.sort(key=lambda item: float(item.get("ts") or 0), reverse=True)
    with hosted_turn._model_api_recap_active_lock:
        active_recap = store.user_id in hosted_turn._model_api_recap_active_users
    return {
        "jobs": jobs,
        "active_recap": active_recap,
    }, 200


# ─────────────────── route / credential collection endpoints (Task 6+7) ───────────────────
#
# GET/POST /v1/model_api/routes, POST .../activate, POST .../test, DELETE .../{id},
# PATCH/DELETE /v1/model_api/credentials/{id}. Unlike /v1/model_api/setup (which
# stays a single-active-route idempotent upsert for the legacy one-config-per-user
# flow), these expose the full credentials/routes tables so iOS can manage several
# saved keys and several routes at once.


def _test_route_or_error(store, route: dict, caller_api_key: str | None):
    """对一条 route 跑真实测活。成功回写 test_status='ok' 并返回 None；
    失败回写 'failed' 并返回 (error_body, status)。"""
    envelope = route.get("api_key_envelope")
    if not isinstance(envelope, dict):
        cred = db.model_api_credential_get(store.user_id, route["credential_id"])
        envelope = (cred or {}).get("api_key_envelope")
    if not isinstance(envelope, dict):
        return {"error": "model_api_key_envelope_missing"}, 404
    try:
        provider_key = core_enclave._decrypt_envelope_via_enclave(
            envelope, caller_api_key, purpose="model_api_provider_key").decode("utf-8")
    except Exception as e:
        return {"error": "model_api_key_decrypt_failed", "detail": str(e)[:220]}, 400
    try:
        provider_client.test_provider_key(provider_client.ProviderConfig(
            route["provider"], route["model"], provider_key, route["base_url"]))
    except provider_client.ProviderError as e:
        # Not checked on purpose: both callers (route_test, route_activate) already
        # surface an accurate 400 below regardless of whether this write lands —
        # no false success. Swallowed failure just leaves test_status stale.
        db.model_api_route_mark_test(store.user_id, route["id"], status="failed", error=str(e))
        print(
            f"[model_api:{store.user_id}] route test FAILED provider={route['provider']} "
            f"model={route['model']} status_code={e.status_code} detail={str(e)[:160]}"
        )
        return {"error": "provider_test_failed", "detail": str(e),
                "status_code": e.status_code}, 400
    # Must check: model_api_route_activate() treats a None return here as "test
    # passed" and immediately flips is_active=True. If this write silently fails,
    # test_status never reaches 'ok', so the just-"activated" route is excluded from
    # the roster (is_active AND test_status='ok') even though the caller gets a 200
    # "activated" response — the false-success pattern this pass exists to catch.
    if not db.model_api_route_mark_test(store.user_id, route["id"], status="ok"):
        return {"error": "model_api_route_write_failed"}, 500
    return None


def model_api_routes_get(store) -> tuple[dict, int]:
    routes = db.model_api_routes_list(store.user_id)
    active = next((r["id"] for r in routes if r["is_active"]), None)
    return {"active_route_id": active, "routes": routes}, 200


def model_api_credentials_get(store) -> tuple[dict, int]:
    """列出全部 credential（不只是被某条 route 引用的那些）。

    iOS 此前只能靠对 GET /routes 的 credential_id 去重来拼凑凭据列表——一把凭据
    如果零 route 引用（例如用户删掉了它最后一条 route），就永远不出现在那份列表
    里，于是拿不到它的 id，DELETE /credentials/{id} 也就永远调不到它，密文会一直
    留在库里。这个端点直接查 credentials 表，独立于 routes 是否引用它。

    ``route_count`` 从 ``db.model_api_routes_list`` 在 Python 侧数出来，不新开
    SQL 查询。``db.model_api_credentials_list`` 本就不选 api_key_envelope 那一列
    （SQL 层面就没取），密文绝不会出现在这里。"""
    creds = db.model_api_credentials_list(store.user_id)
    routes = db.model_api_routes_list(store.user_id)
    counts: dict[str, int] = {}
    for r in routes:
        counts[r["credential_id"]] = counts.get(r["credential_id"], 0) + 1
    return {
        "credentials": [
            {**cred, "route_count": counts.get(cred["id"], 0)} for cred in creds
        ]
    }, 200


@_serialized_model_api_mutation
def model_api_route_create(store, payload: dict, *, caller_api_key: str | None) -> tuple[dict, int]:
    provider = str(payload.get("provider") or "")
    model = str(payload.get("model") or "")
    base_url = str(payload.get("base_url") or "")
    raw_key = str(payload.get("api_key") or "").strip()
    credential_id = str(payload.get("credential_id") or "").strip()
    activate = bool(payload.get("activate"))
    try:
        reasoning_effort = _normalize_reasoning_effort(payload.get("reasoning_effort"))
    except ValueError as e:
        return {"error": "invalid_reasoning_effort", "detail": str(e)}, 400
    if bool(raw_key) == bool(credential_id):
        return {"error": "api_key_or_credential_id_required",
                "detail": "supply exactly one of api_key / credential_id"}, 400

    # 在 credential_id 路径下，凭据才是 provider/base_url 的唯一真源——payload 里
    # 的这两个字段被完全忽略（连校验都不校验）。因此必须先把凭据取出来把
    # provider/base_url 覆盖成凭据的值，再统一跑 validate_config；顺序反了的话
    # validate_config 会对着 payload 的空 base_url 报 400，即便凭据本身有效
    # base_url（openai_compatible 复用旧凭据场景，见 API_MODEL_API_ROUTES.md）。
    cred = None
    if credential_id:
        cred = db.model_api_credential_get(store.user_id, credential_id)
        if not cred:
            return {"error": "credential_not_found"}, 404
        provider = cred["provider"]
        base_url = cred["base_url"]

    try:
        provider, model, base_url = provider_client.validate_config(provider, model, base_url)
    except provider_client.ProviderError as e:
        return {"error": str(e)}, 400

    # Tracks whether THIS request minted a new credential row (vs. reusing one via
    # credential_id) — only what we created here gets cleaned up on a later failure;
    # a reused credential must survive regardless of what happens next.
    created_credential_here = cred is None
    if cred is not None:
        credential_id = cred["id"]
    else:
        envelope, err = core_envelope._build_shared_envelope_for_store(
            store, raw_key.encode("utf-8"), item_id=f"model_api_key_{uuid.uuid4().hex}")
        if envelope is None:
            return {"error": "cannot_encrypt_provider_key", "detail": err}, 409
        # Mirror model_api_setup's one-time openai_compatible Responses probe so
        # the native provider client does not silently lose the relay capability.
        supports_responses = False
        if provider == "openai_compatible":
            supports_responses = provider_client.probe_responses_support(
                provider_client.ProviderConfig(provider, model, raw_key, base_url))
            print(
                f"[model_api:{store.user_id}] openai_compatible /responses probe -> "
                f"supports={supports_responses} base_url={base_url}"
            )
        # 显式带 api_key 就是「新建一把凭据」，总是插新行 —— 同 provider 允许多把 key。
        credential_id = db.model_api_credential_create(
            store.user_id, provider=provider, base_url=base_url,
            label=str(payload.get("label") or provider.replace("_", " ").title()),
            api_key_envelope=envelope,
            api_key_hint=provider_client.mask_api_key(raw_key),
            supports_responses=supports_responses)
        if not credential_id:
            return {"error": "model_api_credential_write_failed"}, 500

    route_id = db.model_api_route_upsert(
        store.user_id, credential_id, model, reasoning_effort)
    if not route_id:
        # Don't strand the credential this request just minted: it holds a real
        # encrypted provider key the user can never see (no route to surface it
        # via GET /routes) or delete (DELETE /credentials/{id} needs an id they
        # don't have) — a permanent orphan. A credential reused via credential_id
        # is untouched: it existed before this request and other routes may still
        # reference it.
        if created_credential_here:
            db.model_api_credential_delete(store.user_id, credential_id)
        return {"error": "model_api_route_write_failed"}, 500

    if activate:
        return model_api_route_activate(store, route_id, caller_api_key=caller_api_key)
    return {"route": db.model_api_route_get(store.user_id, route_id)}, 200


@_serialized_model_api_mutation
def model_api_route_activate(store, route_id: str, *, caller_api_key: str | None) -> tuple[dict, int]:
    """先同步测活，通过才切换。测不过 → 400，旧 active 纹丝不动。

    为什么必须 gate：Runtime V2 ownership 只接受 is_active AND
    test_status='ok' 的 supported route；未测活 route 不能成为 job executor 的
    provider config。
    """
    route = db.model_api_route_get(store.user_id, route_id)
    if not route:
        return {"error": "route_not_found"}, 404

    err = _test_route_or_error(store, route, caller_api_key)
    if err is not None:
        return err

    try:
        restore_v2 = _runtime_should_restore_v2(store)
    except Exception:
        return {"error": "runtime_control_unavailable"}, 503
    fence_error = _fence_v2_config_change_or_error(
        store, required=restore_v2
    )
    if fence_error is not None:
        return fence_error
    if not db.model_api_route_activate(store.user_id, route_id):
        restore_error = _restore_v2_if_runnable_or_error(
            store, required=restore_v2
        )
        if restore_error is not None:
            return restore_error
        return {"error": "route_not_found"}, 404
    restore_error = _restore_v2_or_error(store, required=restore_v2)
    if restore_error is not None:
        return restore_error
    policy_error = _apply_runtime_policy_or_error(store)
    if policy_error is not None:
        return policy_error
    # V2 provider config is pinned once per turn. The fence+restore above bumps
    # its generation around activation, so a turn on the old route can spend
    # provider quota but cannot commit a reply/tool effect after this endpoint
    # returns. No resident process participates in the handoff.
    print(f"[model_api:{store.user_id}] activated route model={route['model']}")
    return {"active_route_id": route_id,
            "route": db.model_api_route_get(store.user_id, route_id)}, 200


@_serialized_model_api_mutation
def model_api_route_test(store, route_id: str, *, api_key: str | None) -> tuple[dict, int]:
    """与 ``DELETE /routes/{id}`` 对称：测的若是当前 active route 且失败了，不能
    让它悬空——它仍 ``is_active=TRUE``（``_test_route_or_error`` 只翻 test_status，
    从不碰 is_active），但 V2 eligibility 只收 ``is_active AND
    test_status='ok'``。一次用户主动点的「测试连接」不应把 control tuple 留在
    看似 V2-ready、实际没有 runnable provider 的状态。

    陷阱：``model_api_autoselect_active`` 只挑 ``NOT is_active`` 的候选——对着仍
    active 的这条失败 route 直接 autoselect 会试图让第二行也变 active，撞
    ``model_api_routes_one_active`` partial unique index。必须先
    ``model_api_route_deactivate`` 腾位，再 autoselect。"""
    route = db.model_api_route_get(store.user_id, route_id)
    if not route:
        return {"error": "route_not_found"}, 404
    was_active = route["is_active"]
    try:
        restore_v2 = _runtime_should_restore_v2(store)
    except Exception:
        return {"error": "runtime_control_unavailable"}, 503
    err = _test_route_or_error(store, route, api_key)
    if err is not None:
        if was_active:
            db.model_api_route_deactivate(store.user_id, route_id)
            # deactivate()'s bool return is deliberately NOT trusted here: a False
            # is ambiguous between (a) a swallowed DB write failure — route is
            # STILL is_active=TRUE — and (b) a concurrent request already
            # deactivated it — route is already NOT active. The two demand
            # opposite responses, and the bool alone can't tell them apart. So
            # re-read reality instead of branching on the return value:
            # autoselect_active only considers NOT-active candidates, and if a
            # route is still active it would try to make a second row active and
            # collide with model_api_routes_one_active, silently returning None
            # and leaving the user stranded on the failed-active route — the
            # exact state this takeover exists to avoid.
            still_active = db.model_api_active_route(store.user_id)
            if still_active is None:
                # None is a legal outcome here too (no other 'ok' route left) —
                # it means the user now has no working config, not a write
                # failure to 500 on.
                err[0]["active_route_id"] = db.model_api_autoselect_active(store.user_id)
            else:
                # Deactivation didn't land (DB hiccup) — report the id only.
                # model_api_active_route() also carries api_key_envelope
                # ciphertext; never let that dict itself reach the response.
                err[0]["active_route_id"] = still_active["id"]
            # Whichever branch won, the formerly-active route just lost its
            # runnable proof. Fence its current V2 generation. If an already-ok
            # replacement was selected, Pre's forced policy may safely re-enter
            # V2 against that replacement after the fence.
            try:
                hosted_config_store.prepare_model_api_delete(store)
            except Exception:
                return {"error": "model_api_runtime_disable_failed"}, 500
            active_after = db.model_api_active_route(store.user_id)
            if active_after and active_after.get("test_status") == "ok":
                restore_error = _restore_v2_or_error(
                    store, required=restore_v2
                )
                if restore_error is not None:
                    return restore_error
        return err
    if was_active:
        policy_error = _apply_runtime_policy_or_error(store)
        if policy_error is not None:
            return policy_error
    return {"status": "ok", "route": db.model_api_route_get(store.user_id, route_id)}, 200


@_serialized_model_api_mutation
def model_api_route_remove(store, route_id: str) -> tuple[dict, int]:
    route = db.model_api_route_get(store.user_id, route_id)
    if not route:
        return {"error": "route_not_found"}, 404
    try:
        restore_v2 = _runtime_should_restore_v2(store)
    except Exception:
        return {"error": "runtime_control_unavailable"}, 503
    if not db.model_api_route_delete(store.user_id, route_id):
        return {"error": "route_not_found"}, 404
    # ``route['is_active']`` was only a pre-delete snapshot: a concurrent
    # activation may have made this route active immediately afterward. Re-read,
    # autoselect if needed, and fence every successful deletion so that stale
    # config work can never cross into the replacement lifetime.
    active_after = db.model_api_active_route(store.user_id)
    active_id = (
        active_after["id"]
        if active_after is not None
        else db.model_api_autoselect_active(store.user_id)
    )
    try:
        hosted_config_store.fence_hosted_runtime_for_config_change(store)
        if active_id is None:
            hosted_config_store.prepare_model_api_delete(store)
    except Exception:
        return {"error": "model_api_runtime_disable_failed"}, 500
    if active_id is not None:
        restore_error = _restore_v2_or_error(
            store, required=restore_v2
        )
        if restore_error is not None:
            return restore_error
    return {"status": "deleted", "active_route_id": active_id}, 200


@_serialized_model_api_mutation
def model_api_credential_patch(store, credential_id: str, payload: dict, *,
                               caller_api_key: str | None) -> tuple[dict, int]:
    cred = db.model_api_credential_get(store.user_id, credential_id)
    if not cred:
        return {"error": "credential_not_found"}, 404

    label = payload.get("label")
    raw_key = str(payload.get("api_key") or "").strip()

    if not raw_key:
        if label is None:
            return {"error": "nothing_to_update"}, 400
        # 别丢弃返回值：写库瞬时失败/并发删除会让 update 返回 False。假报 200 会让
        # 用户以为改名成功，与 model_api_setup 里 mark_test/activate 的处理保持一致。
        if not db.model_api_credential_update(store.user_id, credential_id, label=str(label)):
            return {"error": "model_api_credential_write_failed"}, 500
        return {"status": "ok"}, 200

    # 换 key：先封新信封，再对 active route（若属于本 credential）测活。测不过就整体不落库。
    envelope, err = core_envelope._build_shared_envelope_for_store(
        store, raw_key.encode("utf-8"), item_id=f"model_api_key_{uuid.uuid4().hex}")
    if envelope is None:
        return {"error": "cannot_encrypt_provider_key", "detail": err}, 409

    active = db.model_api_active_route(store.user_id)
    if active and active["credential_id"] == credential_id:
        try:
            provider_client.test_provider_key(provider_client.ProviderConfig(
                active["provider"], active["model"], raw_key, active["base_url"]))
        except provider_client.ProviderError as e:
            # 不落库：旧 key 与旧 test_status 都保持原样，用户不会掉出 roster。
            return {"error": "provider_test_failed", "detail": str(e),
                    "status_code": e.status_code}, 400

    active_key_change = bool(
        active and active["credential_id"] == credential_id
    )
    restore_v2 = False
    if active_key_change:
        try:
            restore_v2 = _runtime_should_restore_v2(store)
        except Exception:
            return {"error": "runtime_control_unavailable"}, 503
        fence_error = _fence_v2_config_change_or_error(
            store, required=restore_v2
        )
        if fence_error is not None:
            return fence_error

    # 换 key 是安全关键写：假成功会让用户去 provider 后台吊销旧 key，而服务端还
    # 攥着旧 envelope，下个 agent 回合对着作废 key 失败——正是 brief 警告的「打停
    # 托管 agent」，只不过经由一次假 200 而非中途崩溃。所以检查返回值。
    if not db.model_api_credential_update(
            store.user_id, credential_id,
            label=str(label) if label is not None else None,
            api_key_envelope=envelope,
            api_key_hint=provider_client.mask_api_key(raw_key)):
        restore_error = _restore_v2_if_runnable_or_error(
            store, required=restore_v2
        )
        if restore_error is not None:
            return restore_error
        return {"error": "model_api_credential_write_failed"}, 500

    # 新 key 刚在 active route 上验证过——刷新它的 last_test_at，否则 GET /routes 会
    # 显示轮换之前的陈旧测试时间戳。test_status 本就还是 'ok'（roster 不变量没破）。
    # Not checked: a swallowed failure here only leaves last_test_at stale
    # (cosmetic) — test_status was already 'ok' going in, so the roster
    # invariant this pass cares about isn't at risk either way.
    if active and active["credential_id"] == credential_id:
        db.model_api_route_mark_test(store.user_id, active["id"], status="ok")

    # 该 credential 下的非 active route 全部退回 untested（新 key 未在它们上验证过）。
    # Not checked: this is UI-freshness bookkeeping only. Activating any of these
    # routes always re-tests through _test_route_or_error before flipping
    # is_active, so a stale 'ok' left behind by a swallowed write here can never
    # let an unverified route reach the roster.
    for r in db.model_api_routes_list(store.user_id):
        if r["credential_id"] == credential_id and not r["is_active"]:
            db.model_api_route_mark_test(store.user_id, r["id"], status="untested")
    restore_error = _restore_v2_or_error(store, required=restore_v2)
    if restore_error is not None:
        return restore_error
    return {"status": "ok"}, 200


@_serialized_model_api_mutation
def model_api_credential_remove(store, credential_id: str) -> tuple[dict, int]:
    cred = db.model_api_credential_get(store.user_id, credential_id)
    if not cred:
        return {"error": "credential_not_found"}, 404
    try:
        restore_v2 = _runtime_should_restore_v2(store)
    except Exception:
        return {"error": "runtime_control_unavailable"}, 503
    # Must check: an unchecked False here (DB hiccup, or a concurrent delete racing
    # this one) would fall through to a "status": "deleted" 200 for a credential
    # that's still sitting in the DB with its key intact — a false success, and
    # mirrors model_api_route_remove's analogous route_delete check below.
    if not db.model_api_credential_delete(store.user_id, credential_id):   # CASCADE 带走 routes
        return {"error": "credential_not_found"}, 404
    active_after = db.model_api_active_route(store.user_id)
    active_id = (
        active_after["id"]
        if active_after is not None
        else db.model_api_autoselect_active(store.user_id)
    )
    try:
        hosted_config_store.fence_hosted_runtime_for_config_change(store)
        if active_id is None:
            hosted_config_store.prepare_model_api_delete(store)
    except Exception:
        return {"error": "model_api_runtime_disable_failed"}, 500
    if active_id is not None:
        restore_error = _restore_v2_or_error(
            store, required=restore_v2
        )
        if restore_error is not None:
            return restore_error
    return {"status": "deleted", "active_route_id": active_id}, 200
