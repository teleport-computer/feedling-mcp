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

from conftest import seed_user


# ---- pure merge: _apply_discovery ----


def test_apply_discovery_filters_roster_to_enabled_and_sets_driver_and_provider():
    roster = [
        {"user_id": "u1", "api_key": "k1", "driver": "claude"},
        {"user_id": "u2", "api_key": "k2"},
        {"user_id": "u3", "api_key": "k3", "driver": "claude"},
    ]
    # backend flag carries both the derived driver AND the provider (so a codex
    # user can be wired native-vs-gateway at spawn).
    enabled = {"u1": {"driver": "claude", "provider": "anthropic", "model": "claude-x", "base_url": ""},
               "u2": {"driver": "codex", "provider": "openai_compatible", "model": "qwen",
                      "base_url": "https://my.host/v1", "supports_responses": True}}
    out = supervisor_mod._apply_discovery(roster, enabled)
    by_uid = {e["user_id"]: e for e in out}
    assert set(by_uid) == {"u1", "u2"}            # u3 dropped (not enabled)
    assert by_uid["u1"]["driver"] == "claude"
    assert by_uid["u2"]["driver"] == "codex"      # driver taken from backend flag
    assert by_uid["u2"]["provider"] == "openai_compatible"  # provider stamped for transport
    assert by_uid["u2"]["model"] == "qwen"        # model stamped for gateway routing
    assert by_uid["u2"]["base_url"] == "https://my.host/v1"  # custom endpoint preserved
    assert by_uid["u2"]["supports_responses"] is True  # /responses capability stamped for transport
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


def _seed_model_api(user_id: str, *, provider: str, test_status: str,
                    enabled: bool | None = None, agent_runtime_driver: str | None = None,
                    model: str = "x", base_url: str = ""):
    seed_user(user_id)
    doc: dict = {"provider": provider, "model": model, "test_status": test_status,
                 "base_url": base_url}
    if agent_runtime_driver is not None:
        doc["agent_runtime_driver"] = agent_runtime_driver
    elif enabled is not None:
        doc["agent_runtime_driver"] = "auto" if enabled else "legacy"
    db.set_blob(user_id, "model_api", doc)


def _seed_all(_clean_blobs):
    _seed_model_api("anthropic_on", provider="anthropic", test_status="ok", enabled=True)
    _seed_model_api("deepseek_on", provider="deepseek", test_status="ok", enabled=True)
    _seed_model_api("openai_on", provider="openai", test_status="ok", enabled=True)
    _seed_model_api("gemini_on", provider="gemini", test_status="ok", enabled=True, model="gemini-2.0-flash")  # codex via gateway
    _seed_model_api("openrouter_on", provider="openrouter", test_status="ok", enabled=True)  # codex via gateway
    _seed_model_api("compat_on", provider="openai_compatible", test_status="ok", enabled=True,
                    base_url="https://my.host/v1")  # codex via gateway
    _seed_model_api("anthropic_off", provider="anthropic", test_status="ok", enabled=False)  # not enabled
    _seed_model_api("openai_failed", provider="openai", test_status="failed", enabled=True)  # key not ok
    seed_user("noisy")
    db.set_blob("noisy", "identity", {"foo": "bar"})                                    # unrelated kind


def test_list_enabled_users_native_only_by_default(_clean_blobs):
    # With the LiteLLM gateway OFF (default), gateway-only providers must NOT be
    # discovered — else they'd be spawned with gateway transport into a proxy that
    # isn't running. Only the native-fit providers (claude/openai) come back.
    # Flag (agent_runtime_driver) is no longer a gate — test_ok + fit provider suffices;
    # anthropic_off (agent_runtime_driver='legacy') is now included.
    _seed_all(_clean_blobs)
    rows = {u["user_id"]: u for u in db.list_agent_runtime_enabled_users()}
    assert {uid: r["driver"] for uid, r in rows.items()} == {
        "anthropic_on": "claude",
        "anthropic_off": "claude",   # legacy flag no longer gates discovery
        "deepseek_on": "claude",
        "openai_on": "codex",
    }


def test_list_enabled_users_includes_gateway_when_enabled(_clean_blobs):
    _seed_all(_clean_blobs)
    rows = {u["user_id"]: u for u in db.list_agent_runtime_enabled_users(include_gateway=True)}
    assert {uid: r["driver"] for uid, r in rows.items()} == {
        "anthropic_on": "claude",
        "anthropic_off": "claude",   # legacy flag no longer gates discovery
        "deepseek_on": "claude",
        "openai_on": "codex",
        "gemini_on": "codex",
        "openrouter_on": "codex",
        "compat_on": "codex",
    }
    # provider + model + base_url are carried so the supervisor can wire codex
    # native vs gateway (and build the per-user LiteLLM routing for gateway users)
    assert rows["gemini_on"]["provider"] == "gemini"
    assert rows["gemini_on"]["model"] == "gemini-2.0-flash"
    assert rows["openai_on"]["provider"] == "openai"
    # openai_compatible's custom endpoint must survive into LiteLLM's api_base
    assert rows["compat_on"]["base_url"] == "https://my.host/v1"


def test_list_enabled_users_empty_when_no_tested_config(_clean_blobs):
    # 无任何 test_status='ok' 的配置时结果为空（flag 已不再是 gate）
    _seed_model_api("anthropic_failed", provider="anthropic", test_status="failed", enabled=True)
    assert db.list_agent_runtime_enabled_users() == []



# ---- new semantics: test_ok + fit provider → discovered, no per-user flag ----


def test_list_enabled_users_includes_configured_without_flag(_clean_blobs):
    # 无 agent_runtime_driver flag，只要 test_status=ok + fit provider 就纳入
    _seed_model_api("usr_a", provider="anthropic", test_status="ok")  # 无 flag
    rows = {u["user_id"]: u for u in db.list_agent_runtime_enabled_users()}
    assert "usr_a" in rows and rows["usr_a"]["driver"] == "claude"


def test_list_enabled_users_excludes_untested(_clean_blobs):
    _seed_model_api("usr_b", provider="anthropic", test_status="")  # 未测通
    assert db.list_agent_runtime_enabled_users() == []


def test_list_enabled_users_excludes_non_fit_provider(_clean_blobs):
    _seed_model_api("usr_c", provider="weird", test_status="ok")
    assert db.list_agent_runtime_enabled_users() == []


def test_list_enabled_users_ignores_explicit_opt_out_flag(_clean_blobs):
    # 彻底对齐：连显式 agent_runtime_driver=legacy 也不再排除（kill switch 改用删 config/改 test_status）
    _seed_model_api("usr_d", provider="openai", test_status="ok", agent_runtime_driver="legacy")
    rows = {u["user_id"]: u for u in db.list_agent_runtime_enabled_users()}
    assert "usr_d" in rows and rows["usr_d"]["driver"] == "codex"


# ---- pi driver: openai_compatible discovered directly, no gateway ----


def test_list_enabled_users_pi_takes_openai_compatible(_clean_blobs):
    # pi 开关开：openai_compatible 被发现为 pi driver，且不需要 gateway
    _seed_all(_clean_blobs)
    rows = {u["user_id"]: u for u in db.list_agent_runtime_enabled_users(include_pi=True)}
    assert rows["compat_on"]["driver"] == "pi"
    assert rows["compat_on"]["base_url"] == "https://my.host/v1"   # 直连中转站要用
    # gateway 仍关：gemini/openrouter 照旧不被发现
    assert "gemini_on" not in rows and "openrouter_on" not in rows


def test_list_enabled_users_pi_and_gateway_together(_clean_blobs):
    # 两开关都开：openai_compatible 归 pi，gemini/openrouter 仍是 codex+gateway
    _seed_all(_clean_blobs)
    rows = {u["user_id"]: u for u in db.list_agent_runtime_enabled_users(
        include_gateway=True, include_pi=True)}
    assert rows["compat_on"]["driver"] == "pi"
    assert rows["gemini_on"]["driver"] == "codex"
    assert rows["openrouter_on"]["driver"] == "codex"
