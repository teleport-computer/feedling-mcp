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
                      "base_url": "https://my.host/v1", "supports_responses": True,
                      "reasoning_effort": "medium"}}
    out = supervisor_mod._apply_discovery(roster, enabled)
    by_uid = {e["user_id"]: e for e in out}
    assert set(by_uid) == {"u1", "u2"}            # u3 dropped (not enabled)
    assert by_uid["u1"]["driver"] == "claude"
    assert by_uid["u2"]["driver"] == "codex"      # driver taken from backend flag
    assert by_uid["u2"]["provider"] == "openai_compatible"  # provider stamped for transport
    assert by_uid["u2"]["model"] == "qwen"        # model stamped for gateway routing
    assert by_uid["u2"]["base_url"] == "https://my.host/v1"  # custom endpoint preserved
    assert by_uid["u2"]["supports_responses"] is True  # /responses capability stamped for transport
    assert by_uid["u2"]["reasoning_effort"] == "medium"  # per-user gateway reasoning switch
    assert by_uid["u1"]["api_key"] == "k1"        # credential preserved


def test_apply_discovery_empty_enabled_drops_all():
    roster = [{"user_id": "u1", "api_key": "k1"}]
    assert supervisor_mod._apply_discovery(roster, {}) == []


# ---- DB query: list_agent_runtime_enabled_users ----


_ENV = {"v": 1, "body_ct": "ct", "nonce": "n"}


@pytest.fixture()
def _clean_blobs():
    # model_api_routes / model_api_credentials 取代了 user_blobs(kind='model_api')
    # 作为 roster 的数据源（Task 3）；三张表一起清空，保持本文件一直依赖的
    # 「每个测试都是干净全局状态」的精确 set-equality 断言成立。
    with db.get_pool().connection() as conn:
        conn.execute("TRUNCATE model_api_routes, model_api_credentials, user_blobs")
    yield


def _seed_model_api(user_id: str, *, provider: str, test_status: str,
                    enabled: bool | None = None, agent_runtime_driver: str | None = None,
                    model: str = "x", base_url: str = "", reasoning_effort: str = ""):
    # `enabled` / `agent_runtime_driver` 保留在签名里只为了不动调用方——两者从来
    # 不是 discovery 的 gate（旧 blob SQL 也没读过 doc->>'agent_runtime_driver'，
    # 见 test_list_enabled_users_ignores_explicit_opt_out_flag 的注释），新表更
    # 没有对应列，这里原样忽略。真正决定是否入 roster 的是 is_active + test_status。
    seed_user(user_id)
    cid = db.model_api_credential_create(
        user_id, provider=provider, base_url=base_url, label=f"{provider} key",
        api_key_envelope=_ENV, api_key_hint="sk-x...000", supports_responses=False,
    )
    rid = db.model_api_route_upsert(
        user_id, cid, model, reasoning_effort or None)
    if test_status:
        db.model_api_route_mark_test(user_id, rid, status=test_status)
    db.model_api_route_activate(user_id, rid)


def _seed_all(_clean_blobs):
    _seed_model_api("anthropic_on", provider="anthropic", test_status="ok", enabled=True)
    _seed_model_api("deepseek_on", provider="deepseek", test_status="ok", enabled=True)
    _seed_model_api("openai_on", provider="openai", test_status="ok", enabled=True)
    _seed_model_api("gemini_on", provider="gemini", test_status="ok", enabled=True, model="gemini-2.0-flash")  # pi driver
    _seed_model_api("openrouter_on", provider="openrouter", test_status="ok", enabled=True,
                    reasoning_effort="medium")  # pi driver
    _seed_model_api("compat_on", provider="openai_compatible", test_status="ok", enabled=True,
                    base_url="https://my.host/v1")  # pi driver
    _seed_model_api("anthropic_off", provider="anthropic", test_status="ok", enabled=False)  # not enabled
    _seed_model_api("openai_failed", provider="openai", test_status="failed", enabled=True)  # key not ok
    seed_user("noisy")
    db.set_blob("noisy", "identity", {"foo": "bar"})                                    # unrelated kind


def test_list_enabled_users_unconditional_all_fit_providers(_clean_blobs):
    # Discovery is unconditional now — no gateway proxy to avoid, so every fit
    # provider (native claude/codex plus pi-driven deepseek/gemini/openrouter/
    # openai_compatible) is discovered in one pass, no include_gateway switch.
    # Flag (agent_runtime_driver) is no longer a gate — test_ok + fit provider suffices;
    # anthropic_off (agent_runtime_driver='legacy') is now included.
    _seed_all(_clean_blobs)
    rows = {u["user_id"]: u for u in db.list_agent_runtime_enabled_users()}
    assert {uid: r["driver"] for uid, r in rows.items()} == {
        "anthropic_on": "claude",
        "anthropic_off": "claude",   # legacy flag no longer gates discovery
        "deepseek_on": "pi",
        "openai_on": "codex",
        "gemini_on": "pi",
        "openrouter_on": "pi",
        "compat_on": "pi",
    }
    # provider + model + base_url are carried so the supervisor can wire the
    # right transport (native codex vs. the pi driver's direct relay)
    assert rows["gemini_on"]["provider"] == "gemini"
    assert rows["gemini_on"]["model"] == "gemini-2.0-flash"
    assert rows["openai_on"]["provider"] == "openai"
    assert rows["openrouter_on"]["reasoning_effort"] == "medium"
    # openai_compatible's custom endpoint must survive into the pi driver's api_base
    assert rows["compat_on"]["base_url"] == "https://my.host/v1"


def test_discovery_pi_providers(_clean_blobs):
    for p in ("gemini", "openrouter", "openai_compatible", "deepseek"):
        _seed_model_api(f"{p}_u", provider=p, test_status="ok",
                        base_url="https://relay/v1" if p == "openai_compatible" else "")
    rows = {r["user_id"]: r for r in db.list_agent_runtime_enabled_users()}
    for p in ("gemini", "openrouter", "openai_compatible", "deepseek"):
        assert rows[f"{p}_u"]["driver"] == "pi"


def test_discovery_unconditional_no_include_gateway(_clean_blobs):
    import inspect
    assert "include_gateway" not in inspect.signature(db.list_agent_runtime_enabled_users).parameters


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
