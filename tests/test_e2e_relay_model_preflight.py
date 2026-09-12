"""T544 — relay model-list preflight for the hosted P0 cell.

A configured relay model that the relay no longer sells self-tests as a 503
"no available channel", which is indistinguishable from a real product failure
(also seen 2026-08-17). The preflight consults the relay's model catalogue
first (via the canonical ``list_provider_models`` fetcher, so OpenRouter is
queried on its key-scoped ``/models/user`` route): a model that is off-sale is
reported as ``instrument_stale`` — its own result class, neither PASS nor FAIL.
'Could not read the catalogue' stays a distinct, non-gating outcome, and a VALID
empty catalogue counts as off-sale, not as unreadable.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.e2e import hosted, p0
from tools.e2e.config import HostedCell


def _catalog(ids, *, complete=True, warnings=None):
    return {"models": [{"id": i} for i in ids], "complete": complete,
            "warnings": warnings or [], "catalog_supported": True}


# ── relay_model_preflight: the verdicts, via the canonical fetcher ──────────

def test_preflight_in_list_when_a_candidate_is_on_sale(monkeypatch):
    seen = {}

    def _fake(provider, api_key, base_url=""):
        seen.update(provider=provider, api_key=api_key, base_url=base_url)
        return _catalog(["m-a", "m-b"])

    monkeypatch.setattr(hosted, "_list_provider_models", _fake)
    status, detail = hosted.relay_model_preflight(
        "openai_compatible", "https://relay.example/v1", "sk-secret", ["m-b"])
    assert status == "in_list" and "m-b" in detail
    assert "sk-secret" not in detail
    # the key is handed to the canonical fetcher (which scopes it correctly),
    # not embedded in any URL we build here.
    assert seen["provider"] == "openai_compatible"


def test_preflight_stale_when_relay_sells_none_of_the_candidates(monkeypatch):
    monkeypatch.setattr(hosted, "_list_provider_models",
                        lambda *a, **k: _catalog(["other-1", "other-2"]))
    status, detail = hosted.relay_model_preflight(
        "openai_compatible", "https://relay.example/v1", "sk-secret", ["gone-model"])
    assert status == "stale"
    assert "gone-model" in detail and "other-1" in detail
    assert "sk-secret" not in detail


def test_preflight_valid_empty_catalogue_is_stale_not_unverifiable(monkeypatch):
    # A real 200 with an empty `data` list is a valid catalogue that proves the
    # model is off-sale — it must be stale, never unverifiable (which would let
    # setup run and reintroduce the false product FAIL).
    monkeypatch.setattr(hosted, "_list_provider_models", lambda *a, **k: _catalog([]))
    status, detail = hosted.relay_model_preflight(
        "openai_compatible", "https://relay.example/v1", "sk-secret", ["m"])
    assert status == "stale" and "empty catalogue" in detail


def test_preflight_partial_catalogue_without_candidate_is_unverifiable(monkeypatch):
    # complete=False (a later page failed/truncated): the candidate could be on
    # a page we never fetched, so its absence from the prefix is NOT proof of
    # off-sale. Must be unverifiable (do not gate), never stale.
    monkeypatch.setattr(hosted, "_list_provider_models",
                        lambda *a, **k: _catalog(["other"], complete=False,
                                                 warnings=["model list truncated: page cap reached"]))
    status, detail = hosted.relay_model_preflight(
        "openai_compatible", "https://relay.example/v1", "sk", ["wanted"])
    assert status == "unverifiable" and "incomplete" in detail


def test_preflight_partial_catalogue_with_candidate_is_in_list(monkeypatch):
    # Presence is proven even from a partial prefix.
    monkeypatch.setattr(hosted, "_list_provider_models",
                        lambda *a, **k: _catalog(["wanted", "x"], complete=False))
    status, _ = hosted.relay_model_preflight(
        "openai_compatible", "https://relay.example/v1", "sk", ["wanted"])
    assert status == "in_list"


def test_preflight_unverifiable_when_fetch_raises(monkeypatch):
    class _Boom(Exception):
        pass

    def _raise(*a, **k):
        raise _Boom("model_catalog_invalid_response")

    monkeypatch.setattr(hosted, "_list_provider_models", _raise)
    monkeypatch.setattr(hosted, "_model_catalog_error_slug",
                        lambda e: "model_catalog_invalid_response")
    status, detail = hosted.relay_model_preflight(
        "openai_compatible", "https://relay.example/v1", "sk-secret", ["m"])
    assert status == "unverifiable" and "model_catalog_invalid_response" in detail
    assert "sk-secret" not in detail


def test_preflight_unverifiable_when_catalog_unsupported(monkeypatch):
    monkeypatch.setattr(hosted, "_list_provider_models",
                        lambda *a, **k: {"models": [], "catalog_supported": False})
    status, _ = hosted.relay_model_preflight(
        "openai_compatible", "https://relay.example/v1", "sk", ["m"])
    assert status == "unverifiable"


def test_preflight_openrouter_uses_default_endpoint_through_real_cell(monkeypatch):
    # The canonical HOSTED_CELLS openrouter cell carries NO base_url; the check
    # must still run (OpenRouter has a default endpoint), not skip.
    from tools.e2e.config import HOSTED_CELLS
    cell = next(c for c in HOSTED_CELLS if c.provider == "openrouter")
    captured = {}

    def _fake(provider, api_key, base_url=""):
        captured.update(provider=provider, base_url=base_url)
        return _catalog(cell.models)  # relay offers exactly the configured models

    monkeypatch.setattr(hosted, "_list_provider_models", _fake)
    status, _ = hosted.relay_model_preflight(
        cell.provider, cell.base_url({}), "sk", cell.models)
    assert status == "in_list"
    assert captured["provider"] == "openrouter"
    # empty base_url is passed through; the canonical fetcher fills the default
    # and uses /models/user — this test proves we do NOT skip openrouter.
    assert captured["base_url"] == ""


def test_preflight_skips_official_provider(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(hosted, "_list_provider_models",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or _catalog(["m"]))
    status, _ = hosted.relay_model_preflight("anthropic", "", "sk", ["claude"])
    assert status == "skip" and called["n"] == 0


def test_preflight_skips_openai_compatible_without_base_url(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(hosted, "_list_provider_models",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or _catalog(["m"]))
    status, _ = hosted.relay_model_preflight("openai_compatible", "", "sk", ["m"])
    assert status == "skip" and called["n"] == 0


def test_preflight_builds_no_verify_false_client():
    # BLOCKER: we must never disable TLS verification while carrying a real key.
    src = (Path(__file__).parent.parent / "tools" / "e2e" / "hosted.py").read_text()
    assert "verify=False" not in src


# ── run_hosted_cell: stale short-circuits BEFORE provisioning ───────────────

def test_stale_model_short_circuits_without_provisioning(monkeypatch):
    monkeypatch.setattr(hosted, "relay_model_preflight",
                        lambda *a, **k: ("stale", "none of ['x'] on sale; relay offers: y, z"))

    def _boom(*a, **k):
        raise AssertionError("must not provision an account for a stale instrument")

    monkeypatch.setattr(hosted.E2EClient, "provision", staticmethod(_boom))
    cell = HostedCell("relay-openai-compatible", "openai_compatible", "E2E_KEY_RELAY",
                      ["x"], base_url_env="E2E_RELAY_BASE")
    out = hosted.run_hosted_cell(cell, {"E2E_KEY_RELAY": "sk", "E2E_RELAY_BASE": "https://r/v1"})
    assert out["result"] == hosted.RESULT_INSTRUMENT_STALE
    assert out["steps"][0][0] == "model_preflight"
    assert out["steps"][0][1] == hosted.RESULT_INSTRUMENT_STALE


def test_unverifiable_preflight_does_not_gate_and_proceeds_to_setup(monkeypatch):
    """'could not read the catalogue' must NOT be treated as 'confirmed
    off-sale': it does not short-circuit — the cell proceeds to provision."""
    monkeypatch.setattr(hosted, "relay_model_preflight",
                        lambda *a, **k: ("unverifiable", "catalogue unreadable: timeout"))

    reached = {"provision": False}

    def _mark(*a, **k):
        reached["provision"] = True
        raise RuntimeError("PROVISION_REACHED")

    monkeypatch.setattr(hosted.E2EClient, "provision", staticmethod(_mark))
    cell = HostedCell("relay-openai-compatible", "openai_compatible", "E2E_KEY_RELAY",
                      ["x"], base_url_env="E2E_RELAY_BASE")
    with pytest.raises(RuntimeError, match="PROVISION_REACHED"):
        hosted.run_hosted_cell(cell, {"E2E_KEY_RELAY": "sk", "E2E_RELAY_BASE": "https://r/v1"})
    assert reached["provision"] is True


# ── mirror negative: the stale result is NOT a FAIL and does not block ──────

def test_instrument_stale_is_neither_pass_nor_fail_and_does_not_block():
    stale = {"cell": "relay-openai-compatible", "result": hosted.RESULT_INSTRUMENT_STALE}
    assert p0.p0_blocks_release([stale]) is False
    assert hosted.RESULT_INSTRUMENT_STALE not in ("ok", "fail")
    assert p0.p0_blocks_release([stale, {"result": "fail"}]) is True
