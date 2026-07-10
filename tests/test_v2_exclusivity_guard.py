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

from conftest import seed_user

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires DATABASE_URL / postgres"
)


@pytest.fixture()
def _clean_blobs():
    with db.get_pool().connection() as conn:
        conn.execute("TRUNCATE user_blobs")
    yield


def _seed_enabled(user_id: str, *, provider: str = "anthropic", test_status: str = "ok"):
    seed_user(user_id)
    db.set_blob(user_id, "model_api", {
        "provider": provider, "model": "x", "test_status": test_status, "base_url": "",
    })


def test_db_action_v2_user_excluded_resident_cli_user_included(_clean_blobs):
    _seed_enabled("usr_resident")
    _seed_enabled("usr_v2")
    db.set_blob("usr_v2", "model_api_runtime", {"hosted_runtime_mode": "db_action_v2"})

    rows = {u["user_id"] for u in db.list_agent_runtime_enabled_users()}

    assert "usr_resident" in rows
    assert "usr_v2" not in rows


def test_resident_cli_mode_or_absent_still_included(_clean_blobs):
    _seed_enabled("usr_explicit_resident")
    db.set_blob("usr_explicit_resident", "model_api_runtime", {"hosted_runtime_mode": "resident_cli"})

    _seed_enabled("usr_no_runtime_blob")
    # no model_api_runtime blob written at all — default is resident_cli

    rows = {u["user_id"] for u in db.list_agent_runtime_enabled_users()}

    assert "usr_explicit_resident" in rows
    assert "usr_no_runtime_blob" in rows
