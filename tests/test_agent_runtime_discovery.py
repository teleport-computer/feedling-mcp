"""Stage C — auto user discovery.

The supervisor already talks to Postgres (the lease table), so it can discover
WHO to run directly from the DB instead of a static roster: users with a
``model_api`` config that is tested-ok and flipped onto the hosted runtime
(``agent_runtime_driver`` in claude|codex, set via POST /v1/model_api/driver).

Credentials (the user's api_key) still come from the roster until Stage D's
runtime-token — so discovery FILTERS the roster to the enabled set and takes the
driver from the backend flag (the control plane for gradual migration).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from agent_runtime import supervisor as supervisor_mod


# ---- pure merge: _apply_discovery ----


def test_apply_discovery_filters_roster_to_enabled_and_sets_driver():
    roster = [
        {"user_id": "u1", "api_key": "k1", "driver": "claude"},
        {"user_id": "u2", "api_key": "k2"},
        {"user_id": "u3", "api_key": "k3", "driver": "claude"},
    ]
    enabled = {"u1": "claude", "u2": "codex"}  # u3 not enabled in backend
    out = supervisor_mod._apply_discovery(roster, enabled)
    by_uid = {e["user_id"]: e for e in out}
    assert set(by_uid) == {"u1", "u2"}            # u3 dropped (not enabled)
    assert by_uid["u1"]["driver"] == "claude"
    assert by_uid["u2"]["driver"] == "codex"      # driver taken from backend flag
    assert by_uid["u1"]["api_key"] == "k1"        # credential preserved


def test_apply_discovery_empty_enabled_drops_all():
    roster = [{"user_id": "u1", "api_key": "k1"}]
    assert supervisor_mod._apply_discovery(roster, {}) == []


# ---- DB query: list_agent_runtime_enabled_users ----


@pytest.fixture()
def _clean_blobs():
    with db.get_pool().connection() as conn:
        conn.execute("TRUNCATE user_blobs")
    yield


def _seed_model_api(user_id: str, *, provider: str, test_status: str, enabled: bool):
    doc = {"provider": provider, "model": "x", "test_status": test_status,
           "agent_runtime_driver": "auto" if enabled else "legacy"}
    db.set_blob(user_id, "model_api", doc)


def test_list_enabled_users_derives_driver_from_provider(_clean_blobs):
    _seed_model_api("anthropic_on", provider="anthropic", test_status="ok", enabled=True)
    _seed_model_api("deepseek_on", provider="deepseek", test_status="ok", enabled=True)
    _seed_model_api("openai_on", provider="openai", test_status="ok", enabled=True)
    _seed_model_api("gemini_on", provider="gemini", test_status="ok", enabled=True)     # no fit → excluded
    _seed_model_api("anthropic_off", provider="anthropic", test_status="ok", enabled=False)  # not enabled
    _seed_model_api("openai_failed", provider="openai", test_status="failed", enabled=True)  # key not ok
    db.set_blob("noisy", "identity", {"foo": "bar"})                                    # unrelated kind

    by_uid = {u["user_id"]: u["driver"] for u in db.list_agent_runtime_enabled_users()}
    assert by_uid == {
        "anthropic_on": "claude",
        "deepseek_on": "claude",
        "openai_on": "codex",
    }


def test_list_enabled_users_empty_when_none_enabled(_clean_blobs):
    _seed_model_api("anthropic_off", provider="anthropic", test_status="ok", enabled=False)
    assert db.list_agent_runtime_enabled_users() == []
