"""Shared pytest setup for the PostgreSQL-backed backend.

The backend persists to PostgreSQL (see backend/db.py), so every test — whether
it drives the app in-process (``asgi_test_client.make_client``) or spawns the
backend as a subprocess (``backend/serve_dev.py``) — needs a ``DATABASE_URL``.
This conftest provisions a throwaway test database once per session and points
``DATABASE_URL`` at it BEFORE any test module is collected.

Configure the Postgres server to use via ``FEEDLING_TEST_PG`` (a libpq URL
whose database is the maintenance db, e.g. ``.../postgres``). If unset, it
defaults to the local throwaway container used in development:

    postgresql://postgres:test@127.0.0.1:55432/postgres

If no Postgres is reachable, the whole suite is skipped with a clear message
rather than failing with confusing connection errors.
"""

import os
import sys
import uuid
from pathlib import Path

import pytest

# Hosted execution is V2-only. Tests that exercise the runtime-token prerequisite
# explicitly unset this secret with monkeypatch.
os.environ.setdefault("FEEDLING_RUNTIME_TOKEN_SECRET", "test-runtime-token-secret")

_ADMIN_URL = os.environ.get("FEEDLING_TEST_PG", "postgresql://postgres:test@127.0.0.1:55432/postgres")
_TEST_DB = f"feedling_test_{uuid.uuid4().hex[:12]}"
_TEE_DB = f"feedling_tee_test_{uuid.uuid4().hex[:12]}"


def _admin_url_for(dbname: str) -> str:
    # Swap the database component of the admin URL.
    base, _, _ = _ADMIN_URL.rpartition("/")
    return f"{base}/{dbname}"


_provisioned = False
_PROVISION_ERROR = None
_created_test_db = False
_created_tee_db = False
try:
    import psycopg

    _admin = psycopg.connect(_ADMIN_URL, autocommit=True)
    _admin.execute(f'CREATE DATABASE "{_TEST_DB}"')
    _admin.close()
    _created_test_db = True
    os.environ["DATABASE_URL"] = _admin_url_for(_TEST_DB)

    with psycopg.connect(_ADMIN_URL, autocommit=True) as _c:
        _c.execute(f'CREATE DATABASE "{_TEE_DB}"')
    _created_tee_db = True
    os.environ["TEE_DATABASE_URL"] = _admin_url_for(_TEE_DB)
    os.environ["TEE_MIGRATION_DATABASE_URL"] = os.environ["TEE_DATABASE_URL"]

    backend_dir = Path(__file__).parent.parent / "backend"
    sys.path.insert(0, str(backend_dir))
    import db

    db.init_schema()

    from alembic_tee import upgrade_head

    upgrade_head()

    _provisioned = True
except Exception as e:  # noqa: BLE001 — any failure means "no usable PG"
    _PROVISION_ERROR = e
    if _created_test_db:
        try:
            import psycopg

            admin = psycopg.connect(_ADMIN_URL, autocommit=True)
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (_TEST_DB,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB}"')
            admin.close()
        except Exception:
            pass
    if _created_tee_db:
        try:
            import psycopg

            admin = psycopg.connect(_ADMIN_URL, autocommit=True)
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (_TEE_DB,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{_TEE_DB}"')
            admin.close()
        except Exception:
            pass

# If we couldn't provision a test DB, do NOT collect the backend test modules.
# Most of them hit the DB (directly or through the app); without a reachable
# Postgres that would turn a graceful skip into a hard collection error.
# ``collect_ignore`` is honored at collection time, so on a developer machine
# with no Postgres `pytest` exits cleanly instead of erroring. CI always
# provisions Postgres, so coverage there is unaffected.
if not _provisioned:
    # Pure-unit modules that don't touch the DB — keep them collectable so a
    # no-Postgres dev machine still runs something useful.
    _PURE_UNIT = {
        "test_card_guard.py",
        "test_bucket_lang_normalize.py",
        "test_branch_flow_guard.py",
        "test_memory_actions_guard.py",
        "test_protocol_leak.py",
        "test_memory_lane_torn_protocol_no_write.py",
        "test_web_settings_store.py",
        "test_v2_web_gate.py",
        "test_web_settings_core.py",
        "test_web_execution_core.py",
        "test_object_storage.py",
        "test_wake_bus.py",
        "test_chat_idempotency_unit.py",
        "test_chat_activity_projection.py",
        "test_chat_turn_activity_unit.py",
        "test_access_mode_runtime_sync_unit.py",
        "test_semantic_analysis.py",
        "test_proactive_runtime_v2.py",
        "test_proactive_observability_v2.py",
        "test_proactive_dashboard_v2.py",
        "test_proactive_tool_executor_v2.py",
        "test_proactive_scheduled_wake_v2.py",
        "test_perception.py",
        "test_perception_recent_apps.py",
        "test_perception_recent_apps_flow.py",
        "test_ios_perception_contract_v2.py",
        "test_perception_ingress_v2.py",
        "test_provider_client.py",
        "test_provider_catalog_unit.py",
        "test_provider_health_unit.py",
        "test_provider_usage.py",
        "test_history_import_identity.py",
        "test_model_api_file_payload.py",
        "test_onboarding_validation_genesis.py",
        "test_enclave_frame_caption.py",
        "test_enclave_visual_plaintext.py",
        "test_screen_caption_backend.py",
        "test_screen_caption_flag.py",
        "test_agent_perception_route.py",
        "test_agent_runtime_tokens.py",
        "test_agent_runtime_resolve_cache.py",
        "test_agent_runtime_spawners.py",
        "test_spawners_catalog.py",
        "test_agent_runtime_resident_contract.py",
        "test_hosted_agent_runtime_cutover.py",
        "test_worldbook_match.py",
        "test_worldbook_readside_core.py",
        "test_no_app_py_regression.py",
        "test_asgi_waiters.py",
        "test_quoted_memory_context.py",
        "test_chat_resident_consumer_file.py",
        "test_user_mcp_probe.py",
        "test_user_mcp_materialize.py",
        "test_v2_coalesce.py",
        "test_v2_status_stream.py",
        "test_v2_dependency_direction.py",
        "test_v2_provider_usage_tool.py",
        "test_v2_history_tools.py",
        "test_user_mcp_ca_fetch.py",
        "test_user_mcp_ca_fetch_leaf.py",
        "test_identity_value_write_path.py",
        "test_v2_workspace_unit.py",
        "test_identity_rename_pairing.py",
        "test_identity_nudge_cap.py",
        "test_identity_list_ops.py",
        "test_io_cli_catalog.py",
        "test_io_cli_web.py",
        "test_io_cli_identity_write_full.py",
        "test_io_cli_mutating_verb_sourcing_rule.py",
        "test_consumer_action_admission.py",
        "test_consumer_capability_inject.py",
        "test_consumer_web_capability.py",
        # Batch 5: web verbs advertised only while the poll's web policy is on.
        # Pure — exercises the consumer's decision helpers with no DB/network.
        "test_consumer_web_advertise.py",
        "test_identity_redistill_ipc.py",
        "test_redistill_server_merge.py",
        "test_update_stall_reason.py",
        "test_resident_maintenance_unit.py",
        "test_genesis_failure_codes.py",
        # B2: pure stdlib (only imports identity.distill_prompt_v1) — was
        # missing from this list even before this task, fixed in passing.
        "test_identity_distill_prompt.py",
        # TEE Redis：配置不变量（读 yaml/sh + subprocess，无 DB）与连接池
        # （构造不建连接，无 DB）。
        "test_redis_cvm_config.py",
        "test_redis_pool.py",
        # TEE 注册表守卫的元守卫：断言 CI 上 PG 真的起了（守卫本体需要 PG，
        # 无 PG 时会被下面的 collect_ignore 静默忽略）。它自己不碰 DB，必须
        # 留在可收集列表里，否则连它也会被忽略。
        "test_tee_registry_guard_enforced.py",
        "test_self_thinking_parse.py",
        # Voice hangup summary prompt builder. Pure — no DB.
        "test_voice_cleanup.py",
        # History-search 纯逻辑内核（planner/cursor/归一化）。Pure — 只
        # import model_api_runtime.v2.history_search（零 IO 模块），无 DB。
        # DB 侧的 test_v2_history_search_store.py 不在此名单。
        "test_v2_history_search_unit.py",
    }
    collect_ignore = sorted(
        f
        for f in os.listdir(os.path.dirname(os.path.abspath(__file__)))
        if f.startswith("test_") and f.endswith(".py") and f not in _PURE_UNIT
    )


def seed_user(user_id: str, **doc) -> None:
    """Test-only: insert a minimal row into the ``users`` table so per-user
    writes aren't rejected by the 0011 CASCADE FK. Production guarantees
    users-first via the registration path; tests that write per-user tables
    directly must call this first.

    Also mirrors the entry into the in-memory registry (``accounts.registry
    ._users`` — the registry's own in-memory truth), since a
    real ``/v1/users/register`` call populates both the DB row and the
    process-local registry. Callers that skip the registry half end up with a
    DB-only "ghost" user that ``registry._user_entry_snapshot()`` can't see —
    which trips gates like the push-service account-existence check into
    treating the seeded user as deleted. Idempotent: won't double-append if
    the user_id is already present (e.g. re-seeded across tests sharing the
    session-scoped DB/registry)."""
    import db
    from accounts import registry

    entry = {"user_id": user_id, **doc}
    db.upsert_user(entry)
    with registry._users_lock:
        if not any(u.get("user_id") == user_id for u in registry._users):
            registry._users.append(entry)


def set_v2_runtime_owner(user_id: str, *, generation: int | None = None) -> None:
    """Test helper: make the authoritative runtime row explicitly V2-owned.

    Production reaches this state through the atomic hosted-runtime cutover;
    low-level worker/job tests intentionally bypass that assembly path and
    therefore opt in through this helper before claiming work.
    """
    import db

    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_runtime_state "
            "(user_id,hosted_runtime_state,runtime_generation) "
            "VALUES (%s,'v2',COALESCE(%s,1)) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "hosted_runtime_state='v2', "
            "runtime_generation=COALESCE(%s,v2_runtime_state.runtime_generation), "
            "updated_at=now()",
            (user_id, generation, generation),
        )


_DEFAULT_MODEL_API_ENVELOPE = {"v": 1, "body_ct": "ct", "nonce": "n"}


def configure_model_api_route(user_id: str, *, provider: str = "anthropic",
                              model: str = "claude-3-5-sonnet-latest",
                              base_url: str = "", envelope=None,
                              api_key_hint: str = "sk-a...451",
                              supports_responses: bool = False,
                              reasoning_effort=None,
                              context_window_tokens: int | None = None,
                              test_status: str = "ok",
                              activate: bool = True):
    """Test-only: configure a user's model_api via the new credentials + routes
    tables (post model-api-multi-profile migration), mirroring what
    POST /v1/model_api/setup now persists. Replaces the old
    ``config_store._save_model_api_config`` / ``db.set_blob(uid, 'model_api', ...)``
    fixtures, which no longer feed the read side (``_load_model_api_config`` and the
    setup endpoints now read the active route). Returns ``(credential_id, route_id)``.

    ``envelope`` defaults to a small ciphertext-shaped dict; pass an explicit dict to
    control what GET /v1/model_api/key_envelope returns."""
    import db

    credential_id = db.model_api_credential_create(
        user_id, provider=provider, base_url=base_url,
        label=provider.replace("_", " ").title(),
        api_key_envelope=envelope if isinstance(envelope, dict) else _DEFAULT_MODEL_API_ENVELOPE,
        api_key_hint=api_key_hint, supports_responses=supports_responses)
    route_id = db.model_api_route_upsert(
        user_id, credential_id, model, reasoning_effort, context_window_tokens)
    if test_status:
        db.model_api_route_mark_test(user_id, route_id, status=test_status)
    if activate:
        db.model_api_route_activate(user_id, route_id)
    return credential_id, route_id


def seed_api_key(user_id: str) -> str:
    """Test-only: mint a real (hashed) api key for an already-``seed_user``-ed
    user and register it in ``accounts.registry`` the same way
    ``/v1/users/register`` does, so ``require_auth``/``_resolve_user`` — which
    hash-looks-up ``api_key_hash`` — accept it. Returns the plaintext key for
    the ``Authorization: Bearer <key>`` header. Idempotent-ish: appends a new
    key entry each call (fine for tests, which mint once per user)."""
    import secrets

    from accounts import registry

    api_key = secrets.token_hex(32)
    api_key_hash = registry._hash_api_key(api_key)
    with registry._users_lock:
        for u in registry._users:
            if u.get("user_id") == user_id:
                u["api_key_hash"] = api_key_hash
                u.setdefault("api_keys", []).append({
                    "key_id": f"key_test_{api_key_hash[:8]}",
                    "api_key_hash": api_key_hash,
                    "access_mode": "official_import",
                    "label": "Test",
                    "created_at": "",
                    "revoked_at": "",
                })
        registry._key_to_user[api_key_hash] = user_id
    return api_key

def pytest_report_header(config):
    """Surface WHY the DB-backed suite was skipped (collect_ignore is silent)."""
    if _provisioned:
        return f"feedling: provisioned throwaway test DB {_TEST_DB}"
    return (
        "feedling: no test Postgres reachable "
        f"({type(_PROVISION_ERROR).__name__}: {_PROVISION_ERROR}) — "
        "skipping all DB-backed tests. Set FEEDLING_TEST_PG to a reachable "
        "Postgres maintenance URL to run them."
    )


def pytest_unconfigure(config):
    """Drop the throwaway database(s) at the end of the session."""
    if not _provisioned:
        return
    try:
        import psycopg

        admin = psycopg.connect(_ADMIN_URL, autocommit=True)
        # Terminate stragglers (subprocess backends may not have exited yet).
        for _dbname in (_TEST_DB, _TEE_DB):
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (_dbname,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{_dbname}"')
        admin.close()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_enclave_http_client():
    """Drop the pooled enclave client around every test.

    ``core.enclave`` keeps one ``httpx.Client`` per process so the V2 prompt
    path stops paying a TLS handshake per decrypted chat row. That cache
    outlives a test: any test that monkeypatches ``core_enclave.httpx.Client``
    would otherwise be served the previous test's stub (or leak its own into
    the next one). Reset on both sides so pooling stays invisible to tests.
    """
    from core import enclave as core_enclave

    core_enclave.reset_http_client()
    yield
    core_enclave.reset_http_client()


@pytest.fixture(autouse=True)
def _reset_admin_page_cache():
    """Clear admin_core's 60s page-html TTL cache between tests.

    The cache is module-level state keyed on a sha256 digest of the
    canonical (first-value-wins) query params **including** ``admin_key``,
    so within one pytest process a page built (or monkeypatched) by an
    earlier test would otherwise be served to a later test hitting the same
    effective key — with the cache note appended and the later test's
    builders never running. Clearing between tests keeps the cache's
    production semantics (TTL, single-flight, stale-on-error) fully
    testable inside a single test.
    """
    yield
    mod = sys.modules.get("admin.admin_core")
    if mod is None:
        return
    with mod._page_cache_lock:
        mod._page_cache.clear()
        mod._page_cache_builds.clear()
        mod._page_cache_last_failure.clear()
    # verdicts JSON 的 30s 缓存同理：不清会把上一个测试的判定喂给下一个。
    with mod._verdicts_cache_lock:
        mod._verdicts_cache = None


@pytest.fixture(autouse=True)
def _disable_setup_auto_vision_probe(monkeypatch, request):
    """Keep setup tests from starting real provider calls in daemon threads.

    Tests for the scheduler override this stub explicitly. Production has no
    such fixture, so every successful setup still launches the probe.
    """
    if "enable_setup_auto_vision_probe" in request.fixturenames:
        return

    from hosted import setup_core

    monkeypatch.setattr(
        setup_core,
        "_kick_setup_main_vision_test",
        lambda *_args, **_kwargs: None,
    )


@pytest.fixture()
def enable_setup_auto_vision_probe():
    """Opt a focused scheduler test into the production background runner."""


@pytest.fixture()
def backend_env(tmp_path, monkeypatch):
    """Fresh per-test backend state: FEEDLING_DIR → tmp_path, registry + store
    caches reset.

    Mutation is in-place on purpose (``_users[:] =`` / ``.clear()``): the list
    and dict objects are shared by identity across modules (CONTRIBUTING §4);
    rebinding them here would silently desync every other holder.
    """
    from accounts import registry
    from core import config as core_config
    from core import store as core_store

    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    with registry._users_lock:
        registry._users[:] = []
        registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    yield


@pytest.fixture()
def client(backend_env):
    """Sync HTTP client over the real assembled ASGI app, on fresh state."""
    from asgi_test_client import make_client

    return make_client()
