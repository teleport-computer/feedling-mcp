"""Deterministic (no-network) tests for the claude2-owned deep probes.

Covers the exact-status/body contracts, correlated-reply guards, local_only
ID-exclusion, language classification, and qualification-vs-diagnostic exit
semantics — the paths codex2's gate review flagged as previously false-passing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.e2e import continuity_probe as cont
from tools.e2e import experience_probe as exp
from tools.e2e import memory_probe as mem
from tools.e2e.probe_common import (
    BLOCKED_EVIDENCE, BLOCKING, PASS, PRODUCT_FAIL, worst,
)


def _r(outcome):
    """Probe case functions return (result, detail); grab the result."""
    return outcome[0] if isinstance(outcome, tuple) else outcome


class FakeResp:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = text or str(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Scriptable client: `handler(method, path, **kw) -> FakeResp`."""
    def __init__(self, handler):
        self.handler = handler
        self.user_id = "usr_fake"
        self.api_url = "https://pre-api.feedling.app"

    def post(self, path, **kw):
        return self.handler("POST", path, **kw)

    def get(self, path, **kw):
        return self.handler("GET", path, **kw)

    def _request(self, method, path, **kw):
        return self.handler(method, path, **kw)

    def message_text(self, msg):
        return str((msg or {}).get("content") or "")


# -- pure functions ----------------------------------------------------------
def test_is_english_rejects_cjk_and_short_gibberish():
    # NOTE: _is_english is "latin-dominant, no CJK run" — it targets the §4.5
    # Chinese-drift bug, and deliberately does NOT claim to tell English from other
    # latin scripts (that needs a language-ID dep; see the probe docstring).
    assert exp._is_english("Here is a short encouraging sentence for you today.")
    assert not exp._is_english("今天你已经很努力了，继续加油哦。")          # Chinese drift
    assert not exp._is_english("字字字字都是中文")                          # pure CJK
    assert not exp._is_english("qwq")                                        # too few latin letters


def test_replies_for_ignores_empty_user_id():
    rows = [{"role": "agent", "id": "a1"}]           # agent row with no reply_to
    assert cont._replies_for(rows, "") == []          # empty uid must never match


def test_replies_for_correlates_by_ids():
    rows = [
        {"role": "user", "id": "u1", "reply_message_id": "a1"},
        {"role": "agent", "id": "a1"},
        {"role": "agent", "id": "a2", "reply_to_message_id": "u1"},
        {"role": "agent", "id": "a3"},                # unrelated
    ]
    ids = {m["id"] for m in cont._replies_for(rows, "u1")}
    assert ids == {"a1", "a2"}


# -- exact-status / body contracts -------------------------------------------
def test_send_raises_on_empty_user_message_id():
    c = FakeClient(lambda m, p, **kw: FakeResp(202, {"user_message": {"id": "", "ts": 1.0}}))
    with pytest.raises(RuntimeError):
        cont._send(c, "hi")


def test_long_message_requires_exact_413_contract():
    good = FakeClient(lambda m, p, **kw: FakeResp(413, {"error": "message too long", "max_chars": 12000}))
    assert _r(cont._long_message(good)) == PASS
    bad_body = FakeClient(lambda m, p, **kw: FakeResp(413, {"error": "nope"}))
    assert _r(cont._long_message(bad_body)) == PRODUCT_FAIL
    accepted = FakeClient(lambda m, p, **kw: FakeResp(202, {"user_message": {"id": "x", "ts": 1}}))
    assert _r(cont._long_message(accepted)) == PRODUCT_FAIL


def test_empty_material_requires_exact_400_title_required(monkeypatch):
    monkeypatch.setattr(mem, "mem_add", lambda c, **kw: (400, {"error": "title_required"}))
    assert _r(mem._empty(object())) == PASS
    monkeypatch.setattr(mem, "mem_add", lambda c, **kw: (200, {"status": "ok"}))
    assert _r(mem._empty(object())) == PRODUCT_FAIL
    monkeypatch.setattr(mem, "mem_add", lambda c, **kw: (500, {"error": "boom"}))
    assert _r(mem._empty(object())) == PRODUCT_FAIL


def test_no_model_send_requires_exact_503_runtime_policy():
    def h(clean):
        def handler(method, path, **kw):
            if path.endswith("/model_api/delete"):
                return FakeResp(200, {"deleted": True})
            return clean
        return handler
    ok = FakeClient(h(FakeResp(503, {"error": "runtime_policy_not_ready"})))
    assert _r(exp._error_attribution(ok, {})) == BLOCKED_EVIDENCE
    wrong = FakeClient(h(FakeResp(404, {"error": "not_found"})))
    assert _r(exp._error_attribution(wrong, {})) == PRODUCT_FAIL
    accepted = FakeClient(h(FakeResp(202, {"user_message": {"id": "x"}})))
    assert _r(exp._error_attribution(accepted, {})) == PRODUCT_FAIL


# -- local_only ID exclusion (crypto stubbed) --------------------------------
def test_local_only_excludes_by_id(monkeypatch):
    mid = "mid123"

    class SK:
        public_key = b"\x00" * 32

    # build_envelope is imported inside _local_only from content_encryption
    import content_encryption
    monkeypatch.setattr(content_encryption, "build_envelope", lambda **kw: {"body_ct": "x"})

    def make(index_items, fetch):
        def handler(method, path, **kw):
            if path.endswith("/memory/add"):
                return FakeResp(201, {"status": "created", "moment": {"id": mid}})
            if path.endswith("/memory/index"):
                return FakeResp(200, {"items": index_items})
            if path.endswith("/memory/fetch"):
                return FakeResp(200, fetch)
            return FakeResp(200, {})
        c = FakeClient(handler)
        c._sk = SK()
        c._enclave_pk = b"\x00" * 32
        return c

    # excluded from both index and fetch → PASS
    assert _r(mem._local_only(make([], {"items": [], "unavailable_ids": [mid]}))) == PASS
    # leaked into index → PRODUCT_FAIL
    assert _r(mem._local_only(make([{"id": mid}], {"items": []}))) == PRODUCT_FAIL
    # leaked via fetch → PRODUCT_FAIL
    assert _r(mem._local_only(make([], {"items": [{"id": mid}]}))) == PRODUCT_FAIL


# -- injected-text empty fetch is not a pass ---------------------------------
def test_injected_text_blocks_on_empty_fetch(monkeypatch):
    mk = "cat0001"

    def handler(method, path, **kw):
        if path.endswith("/chat/send"):
            return FakeResp(202, {"user_message": {"id": "u", "ts": 1}})
        if path.endswith("/capture/force"):
            return FakeResp(200, {})
        if path.endswith("/memory/index"):
            return FakeResp(200, {"items": [{"id": "c1", "summary": f"cat {mk}"}]})
        if path.endswith("/memory/fetch"):
            return FakeResp(200, {"items": []})            # empty → cannot audit
        return FakeResp(200, {})
    c = FakeClient(handler)
    # short-circuit the capture poll wait
    monkeypatch.setattr(exp, "new_marker", lambda: mk)
    monkeypatch.setattr(exp.time, "sleep", lambda *_a: None)
    # capture loop uses memory_summaries via index; give it the marker quickly
    c.memory_summaries = lambda limit=100: [f"cat {mk}"]
    assert _r(exp._injected_text(c)) == BLOCKED_EVIDENCE


# -- qualification vs diagnostic exit semantics ------------------------------
def test_blocked_evidence_is_nonpass_but_not_release_blocking():
    # qualification: overall != PASS → nonzero
    assert worst([PASS, BLOCKED_EVIDENCE]) != PASS
    # diagnostic: BLOCKED_EVIDENCE is not in the release-blocking set
    assert BLOCKED_EVIDENCE not in BLOCKING
    # a broken harness and a missing key DO block, even in diagnostic
    assert "AGENT_ERROR" in BLOCKING
    assert "BLOCKED_CREDENTIAL" in BLOCKING
