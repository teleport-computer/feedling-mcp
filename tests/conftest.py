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

# Let hosting-ready check pass in tests by default. Tests that specifically
# test the assert_hosting_ready() raise path (test_hosted_agent_runtime_cutover.py)
# use monkeypatch.delenv to explicitly unset these, overriding setdefault.
os.environ.setdefault("FEEDLING_HOST_ALL", "1")
os.environ.setdefault("FEEDLING_RUNTIME_TOKEN_SECRET", "test-runtime-token-secret")

_ADMIN_URL = os.environ.get("FEEDLING_TEST_PG", "postgresql://postgres:test@127.0.0.1:55432/postgres")
_TEST_DB = f"feedling_test_{uuid.uuid4().hex[:12]}"


def _admin_url_for(dbname: str) -> str:
    # Swap the database component of the admin URL.
    base, _, _ = _ADMIN_URL.rpartition("/")
    return f"{base}/{dbname}"


_provisioned = False
_PROVISION_ERROR = None
_created_test_db = False
try:
    import psycopg

    _admin = psycopg.connect(_ADMIN_URL, autocommit=True)
    _admin.execute(f'CREATE DATABASE "{_TEST_DB}"')
    _admin.close()
    _created_test_db = True
    os.environ["DATABASE_URL"] = _admin_url_for(_TEST_DB)
    backend_dir = Path(__file__).parent.parent / "backend"
    sys.path.insert(0, str(backend_dir))
    import db

    db.init_schema()
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
        "test_object_storage.py",
        "test_wake_bus.py",
        "test_semantic_analysis.py",
        "test_proactive_runtime_v2.py",
        "test_proactive_observability_v2.py",
        "test_proactive_dashboard_v2.py",
        "test_proactive_tool_executor_v2.py",
        "test_proactive_scheduled_wake_v2.py",
        "test_perception.py",
        "test_ios_perception_contract_v2.py",
        "test_perception_ingress_v2.py",
        "test_provider_client.py",
        "test_history_import_identity.py",
        "test_model_api_prompts.py",
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


_DEFAULT_MODEL_API_ENVELOPE = {"v": 1, "body_ct": "ct", "nonce": "n"}


def configure_model_api_route(user_id: str, *, provider: str = "anthropic",
                              model: str = "claude-3-5-sonnet-latest",
                              base_url: str = "", envelope=None,
                              api_key_hint: str = "sk-a...451",
                              supports_responses: bool = False,
                              reasoning_effort=None,
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
        user_id, credential_id, model, reasoning_effort)
    if test_status:
        db.model_api_route_mark_test(user_id, route_id, status=test_status)
    if activate:
        db.model_api_route_activate(user_id, route_id)
    return credential_id, route_id


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
    """Drop the throwaway database at the end of the session."""
    if not _provisioned:
        return
    try:
        import psycopg

        admin = psycopg.connect(_ADMIN_URL, autocommit=True)
        # Terminate stragglers (subprocess backends may not have exited yet).
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
            (_TEST_DB,),
        )
        admin.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB}"')
        admin.close()
    except Exception:
        pass


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
