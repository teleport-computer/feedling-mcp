"""Hosted Runtime V2 D0 Task 1 — discovery-query mutual-exclusion gate.

``db.list_agent_runtime_enabled_users`` is the resident-CLI supervisor's
discovery query. Once a user is flipped onto the V2 runtime (the
``model_api_runtime`` blob's ``hosted_runtime_mode == 'db_action_v2'``), the
resident supervisor must stop picking them up — otherwise both runtimes would
answer the same user's turns (double-run). This asserts that gate.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db

from conftest import seed_user, configure_model_api_route

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires DATABASE_URL / postgres"
)


@pytest.fixture()
def _clean_blobs():
    # hosted_runtime_mode lives in user_blobs ('model_api_runtime'); provider config
    # moved to model_api_routes/credentials (model-api-multi-profile). Clean both so
    # the discovery query sees only this test's users.
    with db.get_pool().connection() as conn:
        conn.execute("TRUNCATE user_blobs, model_api_routes, model_api_credentials")
    yield


def _seed_enabled(user_id: str, *, provider: str = "anthropic", test_status: str = "ok"):
    seed_user(user_id)
    configure_model_api_route(user_id, provider=provider, model="x", test_status=test_status)


def test_db_action_v2_user_excluded_resident_cli_user_included(_clean_blobs):
    # Full cutover 2026-07-11: db_action_v2 is the default, so the resident roster
    # admits ONLY users who explicitly opted back to resident_cli.
    _seed_enabled("usr_resident")
    db.set_blob("usr_resident", "model_api_runtime", {"hosted_runtime_mode": "resident_cli"})
    _seed_enabled("usr_v2")
    db.set_blob("usr_v2", "model_api_runtime", {"hosted_runtime_mode": "db_action_v2"})

    rows = {u["user_id"] for u in db.list_agent_runtime_enabled_users()}

    assert "usr_resident" in rows
    assert "usr_v2" not in rows


def test_only_explicit_resident_cli_included_absent_defaults_to_v2(_clean_blobs):
    # Post-cutover: an explicit resident_cli opt-out stays resident; a user with NO
    # model_api_runtime blob now defaults to db_action_v2 and must be EXCLUDED from
    # the resident roster (else they double-run on both paths).
    _seed_enabled("usr_explicit_resident")
    db.set_blob("usr_explicit_resident", "model_api_runtime", {"hosted_runtime_mode": "resident_cli"})

    _seed_enabled("usr_no_runtime_blob")
    # no model_api_runtime blob written at all — default is now db_action_v2

    rows = {u["user_id"] for u in db.list_agent_runtime_enabled_users()}

    assert "usr_explicit_resident" in rows
    assert "usr_no_runtime_blob" not in rows
