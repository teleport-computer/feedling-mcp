"""Shared pytest setup for the PostgreSQL-backed backend.

The backend now persists to PostgreSQL (see backend/db.py) instead of files, so
every test — whether it imports ``app`` in-process or spawns the backend as a
subprocess — needs a ``DATABASE_URL``. This conftest provisions a throwaway
test database once per session and points ``DATABASE_URL`` at it BEFORE any
test module is collected (module-level ``import app`` then succeeds).

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
# Several of them import backend/app.py at module scope, which now calls
# db.init_schema() and raises without a reachable Postgres — that would turn a
# graceful skip into a hard collection error. ``collect_ignore_glob`` is honored
# at collection time (before those modules are imported), so on a developer
# machine with no Postgres `pytest` exits cleanly instead of erroring. CI always
# provisions Postgres, so coverage there is unaffected.
if not _provisioned:
    # Pure-unit modules that neither import app nor touch the DB — keep them
    # collectable so a no-Postgres dev machine still runs something useful.
    _PURE_UNIT = {
        "test_wake_bus.py",
        "test_semantic_analysis.py",
        "test_model_api_wake.py",
        "test_proactive_runtime_v2.py",
        "test_proactive_observability_v2.py",
        "test_proactive_dashboard_v2.py",
        "test_proactive_tool_executor_v2.py",
        "test_proactive_scheduled_wake_v2.py",
        "test_hosted_wake_v2_cutover.py",
        "test_perception.py",
        "test_ios_perception_contract_v2.py",
        "test_perception_ingress_v2.py",
        "test_provider_client.py",
        "test_history_import_identity.py",
        "test_model_api_prompts.py",
        "test_enclave_frame_caption.py",
        "test_screen_caption_backend.py",
        "test_screen_caption_flag.py",
        "test_tool_loop_v2.py",
        "test_agent_perception_route.py",
        "test_agent_runtime_tokens.py",
        "test_agent_runtime_spawners.py",
        "test_agent_runtime_resident_contract.py",
        "test_hosted_agent_runtime_cutover.py",
    }
    collect_ignore = sorted(
        f
        for f in os.listdir(os.path.dirname(os.path.abspath(__file__)))
        if f.startswith("test_") and f.endswith(".py") and f not in _PURE_UNIT
    )


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
