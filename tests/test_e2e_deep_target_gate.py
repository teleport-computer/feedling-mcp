"""T545 — deep.py P1 target-environment gate.

The P1 deep suite used to hard-code pre-api as the only permitted target, which
left the release regression with no way to run P1 against test. It is now
parameterised: FEEDLING_E2E_API must be named EXPLICITLY and resolve to an
allowed non-prod host (test or pre). Fail-closed is preserved — unset, prod, or
any other host is refused — so the suite can never silently qualify the wrong
deployment.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.e2e import deep


@pytest.fixture(autouse=True)
def _clear_target(monkeypatch):
    monkeypatch.delenv("FEEDLING_E2E_API", raising=False)


def test_test_api_is_allowed(monkeypatch):
    monkeypatch.setenv("FEEDLING_E2E_API", "https://test-api.feedling.app")
    target, err = deep._resolve_deep_target()
    assert target == "https://test-api.feedling.app" and err == ""


def test_pre_api_is_allowed_and_normalised(monkeypatch):
    monkeypatch.setenv("FEEDLING_E2E_API", "https://pre-api.feedling.app/")
    target, err = deep._resolve_deep_target()
    assert target == "https://pre-api.feedling.app" and err == ""  # trailing slash trimmed


def test_prod_is_refused(monkeypatch):
    monkeypatch.setenv("FEEDLING_E2E_API", "https://api.feedling.app")
    target, err = deep._resolve_deep_target()
    assert target is None and "api.feedling.app" in err


def test_unset_is_refused_not_defaulted(monkeypatch):
    # The client defaults TEST_API to test when unset; the gate must NOT inherit
    # that default — an unnamed target is refused, never guessed.
    target, err = deep._resolve_deep_target()
    assert target is None and "not set" in err


def test_local_and_other_hosts_are_refused(monkeypatch):
    for url in ("https://127.0.0.1:8000", "http://localhost:5000", "https://evil.example"):
        monkeypatch.setenv("FEEDLING_E2E_API", url)
        target, err = deep._resolve_deep_target()
        assert target is None, url
        assert "not an allowed P1 target" in err


def test_noncanonical_origins_of_an_allowed_host_are_refused(monkeypatch):
    # A host-only check would have admitted all of these; the exact-origin match
    # must reject cleartext, nondefault port, userinfo, path, query, and
    # scheme-relative forms that resolve to an allowed hostname.
    for url in (
        "http://test-api.feedling.app",                 # cleartext
        "https://test-api.feedling.app:444",            # nondefault port
        "https://user@test-api.feedling.app",           # userinfo
        "https://test-api.feedling.app/v1/chat",        # path
        "https://test-api.feedling.app/?x=1",           # query
        "//test-api.feedling.app",                      # scheme-relative
        "https://test-api.feedling.app.evil.com",       # suffix host
    ):
        monkeypatch.setenv("FEEDLING_E2E_API", url)
        target, _ = deep._resolve_deep_target()
        assert target is None, f"must refuse noncanonical origin: {url}"


def test_resolved_target_is_passed_to_provisioning(monkeypatch):
    """Executed-path proof: the target reaches account provisioning, so the
    tool cannot validate one deployment and create/delete accounts on another
    (the imported-TEST_API default)."""
    from tools.e2e.config import HostedCell

    captured = {}

    def _capture(*, route, api_url, **kw):
        captured["route"] = route
        captured["api_url"] = api_url
        raise RuntimeError("STOP_AFTER_PROVISION")

    monkeypatch.setattr(deep.E2EClient, "provision", staticmethod(_capture))
    cell = HostedCell("relay-openai-compatible", "openai_compatible", "E2E_KEY_RELAY",
                      ["m"], base_url_env="E2E_RELAY_BASE")
    with pytest.raises(RuntimeError, match="STOP_AFTER_PROVISION"):
        deep.run_provider(cell, {"E2E_KEY_RELAY": "sk", "E2E_RELAY_BASE": "https://r/v1"},
                          run_invariants=False, areas={"memory"},
                          api_url="https://test-api.feedling.app")
    assert captured["api_url"] == "https://test-api.feedling.app"


def test_main_threads_resolved_target_not_imported_default():
    # Guard against regressing to the imported TEST_API default: main must pass
    # the resolved `target` to run_provider and serialize the same `target`.
    src = (Path(__file__).parent.parent / "tools" / "e2e" / "deep.py").read_text()
    assert "api_url=target" in src
    assert '"target": target' in src
    assert "TEST_API" not in src.split("def _resolve_deep_target")[0]  # not imported/used pre-helper


def test_prod_is_never_in_the_allowlist():
    # Mirror negative: removing prod-refusal would require prod in the allowlist;
    # it must never be there, so no env value can qualify prod.
    joined = " ".join(deep._DEEP_ALLOWED_TARGETS)
    assert "https://api.feedling.app" not in deep._DEEP_ALLOWED_TARGETS
    assert "api.feedling.app" not in joined.replace("pre-api.feedling.app", "").replace("test-api.feedling.app", "")
    assert set(deep._DEEP_ALLOWED_TARGETS) == {
        "https://test-api.feedling.app", "https://pre-api.feedling.app"}


# ── secondary accounts must land on the SAME resolved target (T545 r2) ──────
# The deep suite provisions extra accounts inside two probes (memory cross-user
# isolation, experience language isolation). Both used the import-time default
# TEST_API, so a run could validate one target and create/delete secondary
# accounts on a stale one. These prove both now use the resolved target.

def test_memory_isolation_secondary_account_uses_primary_target(monkeypatch):
    from tools.e2e import memory_probe

    captured = {}

    def _capture(*, route, api_url, **kw):
        captured["api_url"] = api_url
        raise RuntimeError("STOP_AT_SECONDARY_PROVISION")

    monkeypatch.setattr(memory_probe, "mem_add", lambda c, **kw: (200, {}))
    monkeypatch.setattr(memory_probe, "_id_of", lambda c, mk: "card-A")
    monkeypatch.setattr(memory_probe.E2EClient, "provision", staticmethod(_capture))

    class _PrimaryC:
        api_url = "https://pre-api.feedling.app"

    with pytest.raises(RuntimeError, match="STOP_AT_SECONDARY_PROVISION"):
        memory_probe._isolation(_PrimaryC())
    assert captured["api_url"] == "https://pre-api.feedling.app"


def test_language_isolation_secondary_account_uses_resolved_target(monkeypatch):
    from tools.e2e import experience_probe

    captured = {}

    def _capture(*, route, api_url, **kw):
        captured["api_url"] = api_url
        raise RuntimeError("STOP_AT_SECONDARY_PROVISION")

    monkeypatch.setattr(experience_probe.E2EClient, "provision", staticmethod(_capture))
    cfg = {"api_url": "https://test-api.feedling.app", "provider": "openai_compatible",
           "model": "m", "key": "sk", "base_url": "https://r/v1"}
    with pytest.raises(RuntimeError, match="STOP_AT_SECONDARY_PROVISION"):
        experience_probe._language_isolated(cfg, p=None)
    assert captured["api_url"] == "https://test-api.feedling.app"
