"""
Task 7 — 夹带通道类型白名单 + 结果真实化
=========================================

Pure-function + HTTP-monkeypatched coverage for:
  - canonicalize_action_type / _ACTION_ALLOWLIST / _action_allowlist_mode
  - execute_agent_actions' outcomes (applied|noop|rejected_allowlist|failed_execution)
  - rewrite_reply_for_outcomes (pure)
  - the foreground (_process_messages) and proactive (_process_proactive_jobs)
    call sites that consume outcomes instead of always claiming success.

Run with: pytest tests/test_consumer_action_admission.py -v
"""

import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Module bootstrap — set required env vars before the module is imported.
# Mirrors tests/test_chat_resident_consumer.py EXACTLY (double-import trap:
# some other test files do `import chat_resident_consumer` with `tools/` on
# sys.path directly, which creates a SECOND module object with its own copy
# of every module-level global — `import tools.chat_resident_consumer as crc`
# is the form this file must use to share state with that suite).
# ---------------------------------------------------------------------------

_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_action_admission_checkpoint.json",
}

for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules.setdefault("content_encryption", _fake_enc)

import tools.chat_resident_consumer as crc  # noqa: E402  (after env setup)


@pytest.fixture(autouse=True)
def _reset_allowlist_mode_env(monkeypatch):
    """Every test controls the mode explicitly — never leak FEEDLING_ACTION_ALLOWLIST
    from the real environment or a previous test into the next one."""
    monkeypatch.delenv("FEEDLING_ACTION_ALLOWLIST", raising=False)
    crc._action_allowlist_mode_warned = False
    yield


class _Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {"status": "ok", "results": [], "effects": []}

    def json(self):
        return self._body

    @property
    def text(self):
        return json.dumps(self._body)


def _http_router(*, identity=None, memory=None, calls=None):
    """Fake for crc._HTTP.post routing on URL, recording every call made."""

    def _post(url, json=None, headers=None, timeout=None):  # noqa: A002 (shadows builtin, matches call site kwarg name)
        if calls is not None:
            calls.append((url, json))
        if "/v1/identity/actions" in url:
            return identity if identity is not None else _Resp()
        if "/v1/memory/actions" in url:
            return memory if memory is not None else _Resp()
        raise AssertionError(f"unexpected POST url={url}")

    return _post


# ---------------------------------------------------------------------------
# canonicalize_action_type — pure
# ---------------------------------------------------------------------------

def test_canonicalize_action_type_known_aliases():
    assert crc.canonicalize_action_type("memory.create") == "memory.add"
    assert crc.canonicalize_action_type("memory.add_correction") == "memory.add"
    assert crc.canonicalize_action_type("memory.patch") == "memory.supersede"
    assert crc.canonicalize_action_type("memory.content_patch") == "memory.supersede"
    assert crc.canonicalize_action_type("identity.patch") == "identity.profile_patch"


def test_canonicalize_action_type_passthrough_for_unknown_and_canonical():
    assert crc.canonicalize_action_type("memory.add") == "memory.add"
    assert crc.canonicalize_action_type("identity.profile_patch") == "identity.profile_patch"
    assert crc.canonicalize_action_type("identity.replace") == "identity.replace"
    assert crc.canonicalize_action_type("memory.frobnicate") == "memory.frobnicate"
    assert crc.canonicalize_action_type("") == ""
    assert crc.canonicalize_action_type(None) == ""


# ---------------------------------------------------------------------------
# _ACTION_ALLOWLIST — spec 3.4 十二类型, identity.replace excluded
# ---------------------------------------------------------------------------

def test_action_allowlist_has_the_twelve_spec_types_and_excludes_replace():
    assert crc._ACTION_ALLOWLIST == frozenset({
        "memory.add", "memory.create", "memory.add_correction",
        "memory.patch", "memory.content_patch", "memory.supersede",
        "memory.upgrade", "memory.delete",
        "identity.profile_patch", "identity.patch",
        "identity.dimension_nudge", "identity.relationship_days_set",
    })
    assert "identity.replace" not in crc._ACTION_ALLOWLIST


# ---------------------------------------------------------------------------
# _action_allowlist_mode — env-driven, default shadow, invalid -> shadow+warn
# ---------------------------------------------------------------------------

def test_action_allowlist_mode_defaults_to_shadow(monkeypatch):
    monkeypatch.delenv("FEEDLING_ACTION_ALLOWLIST", raising=False)
    assert crc._action_allowlist_mode() == "shadow"


@pytest.mark.parametrize("value", ["shadow", "enforce", "off", "ENFORCE", " Off "])
def test_action_allowlist_mode_reads_valid_values_case_insensitive(monkeypatch, value):
    monkeypatch.setenv("FEEDLING_ACTION_ALLOWLIST", value)
    assert crc._action_allowlist_mode() == value.strip().lower()


def test_action_allowlist_mode_invalid_value_falls_back_to_shadow_with_one_warning(monkeypatch, caplog):
    monkeypatch.setenv("FEEDLING_ACTION_ALLOWLIST", "bogus_mode")
    import logging
    with caplog.at_level(logging.WARNING, logger=crc.log.name):
        assert crc._action_allowlist_mode() == "shadow"
        assert crc._action_allowlist_mode() == "shadow"
    warnings = [r for r in caplog.records if "invalid FEEDLING_ACTION_ALLOWLIST" in r.getMessage()]
    assert len(warnings) == 1, "must warn exactly once, not on every call"


# ---------------------------------------------------------------------------
# rewrite_reply_for_outcomes — pure
# ---------------------------------------------------------------------------

def test_rewrite_reply_outcomes_empty_is_passthrough():
    assert crc.rewrite_reply_for_outcomes(["改好了。"], [], fallback_ok="改好了。") == ["改好了。"]
    assert crc.rewrite_reply_for_outcomes([], [], fallback_ok="改好了。") == []


def test_rewrite_reply_all_applied_keeps_existing_replies():
    outcomes = [{"original_type": "memory.add", "canonical_type": "memory.add",
                 "outcome": "applied", "error_code": ""}]
    assert crc.rewrite_reply_for_outcomes(["记下了。"], outcomes, fallback_ok="改好了。") == ["记下了。"]


def test_rewrite_reply_all_applied_empty_replies_uses_fallback():
    outcomes = [{"original_type": "memory.add", "canonical_type": "memory.add",
                 "outcome": "applied", "error_code": ""}]
    assert crc.rewrite_reply_for_outcomes([], outcomes, fallback_ok="改好了。") == ["改好了。"]


def test_rewrite_reply_all_applied_empty_replies_empty_fallback_stays_silent():
    """Proactive background writes never had a synthesized success bubble —
    an empty fallback_ok must not manufacture one."""
    outcomes = [{"original_type": "memory.add", "canonical_type": "memory.add",
                 "outcome": "applied", "error_code": ""}]
    assert crc.rewrite_reply_for_outcomes([], outcomes, fallback_ok="") == []


def test_rewrite_reply_all_failed_overrides_with_honest_text_not_fake_success():
    """Group ①: 全未知类型 + 无正文 → 回复为"未执行"说明, 不是"改好了"."""
    outcomes = [{"original_type": "memory.frobnicate", "canonical_type": "memory.frobnicate",
                 "outcome": "rejected_allowlist", "error_code": ""}]
    result = crc.rewrite_reply_for_outcomes([], outcomes, fallback_ok="改好了。")
    assert result == [crc._ACTION_OUTCOME_ALL_FAILED_ZH]
    assert "改好了" not in result[0]


def test_rewrite_reply_all_failed_english_variant():
    outcomes = [{"original_type": "memory.frobnicate", "canonical_type": "memory.frobnicate",
                 "outcome": "failed_execution", "error_code": "RuntimeError"}]
    result = crc.rewrite_reply_for_outcomes([], outcomes, fallback_ok="Done.")
    assert result == [crc._ACTION_OUTCOME_ALL_FAILED_EN]


def test_rewrite_reply_mixed_appends_note_naming_unexecuted_item():
    """Group ②: allowed+unknown 混合 → 附加句点名未执行项, 原回复保留."""
    outcomes = [
        {"original_type": "memory.add", "canonical_type": "memory.add",
         "outcome": "applied", "error_code": ""},
        {"original_type": "memory.frobnicate", "canonical_type": "memory.frobnicate",
         "outcome": "rejected_allowlist", "error_code": ""},
    ]
    result = crc.rewrite_reply_for_outcomes(["记好了。"], outcomes, fallback_ok="记好了。")
    assert result[0] == "记好了。"
    assert len(result) == 2
    assert "memory.frobnicate" in result[1]


def test_rewrite_reply_mixed_with_empty_replies_uses_fallback_plus_note():
    outcomes = [
        {"original_type": "memory.add", "canonical_type": "memory.add",
         "outcome": "applied", "error_code": ""},
        {"original_type": "identity.replace", "canonical_type": "identity.replace",
         "outcome": "rejected_allowlist", "error_code": ""},
    ]
    result = crc.rewrite_reply_for_outcomes([], outcomes, fallback_ok="改好了。")
    assert result[0] == "改好了。"
    assert "identity.replace" in result[1]


# ---------------------------------------------------------------------------
# execute_agent_actions — action-only (Group ①: 全未知类型) with HTTP monkeypatched
# ---------------------------------------------------------------------------

def test_execute_agent_actions_enforce_mode_rejects_unknown_type_without_forwarding(monkeypatch):
    monkeypatch.setenv("FEEDLING_ACTION_ALLOWLIST", "enforce")
    calls = []
    monkeypatch.setattr(crc._HTTP, "post", _http_router(calls=calls))

    result = crc.execute_agent_actions([{"type": "memory.frobnicate", "memory_id": "m1"}])

    assert calls == [], "enforce mode must drop the action before any HTTP call"
    assert result["outcomes"] == [{
        "original_type": "memory.frobnicate",
        "canonical_type": "memory.frobnicate",
        "outcome": "rejected_allowlist",
        "error_code": "",
    }]
    replies = crc.rewrite_reply_for_outcomes([], result["outcomes"], fallback_ok="改好了。")
    assert replies == [crc._ACTION_OUTCOME_ALL_FAILED_ZH]


def test_execute_agent_actions_shadow_mode_forwards_unknown_type_and_counts_it(monkeypatch):
    """Group ③: shadow 档未知类型放行且计数."""
    monkeypatch.setenv("FEEDLING_ACTION_ALLOWLIST", "shadow")
    before = crc._action_allowlist_shadow_unknown_count
    calls = []
    monkeypatch.setattr(
        crc._HTTP, "post",
        _http_router(memory=_Resp(400, {"status": "error", "error": "unsupported_memory_action"}), calls=calls),
    )

    result = crc.execute_agent_actions([{"type": "memory.frobnicate", "memory_id": "m1"}])

    assert len(calls) == 1 and "/v1/memory/actions" in calls[0][0], "shadow mode must still forward unknown types"
    assert crc._action_allowlist_shadow_unknown_count == before + 1
    assert result["outcomes"] == [{
        "original_type": "memory.frobnicate",
        "canonical_type": "memory.frobnicate",
        "outcome": "failed_execution",
        "error_code": "RuntimeError",
    }]


def test_execute_agent_actions_off_mode_forwards_unknown_type_without_counting(monkeypatch):
    """Group ④: off 档放行 (且不计数, 对照 enforce 档拦截)."""
    monkeypatch.setenv("FEEDLING_ACTION_ALLOWLIST", "off")
    before = crc._action_allowlist_shadow_unknown_count
    calls = []
    monkeypatch.setattr(
        crc._HTTP, "post",
        _http_router(memory=_Resp(200, {"status": "ok", "results": [{"status": "ok"}], "effects": []}), calls=calls),
    )

    result = crc.execute_agent_actions([{"type": "memory.frobnicate", "memory_id": "m1"}])

    assert len(calls) == 1
    assert crc._action_allowlist_shadow_unknown_count == before, "off mode must not touch the shadow counter"
    assert result["outcomes"][0]["outcome"] == "applied"


def test_execute_agent_actions_enforce_mode_still_drops_unknown_type():
    """Group ④, other half: enforce 档拦截 (paired with the off-mode test above)."""
    os.environ["FEEDLING_ACTION_ALLOWLIST"] = "enforce"
    try:
        with patch.object(crc._HTTP, "post") as mock_post:
            result = crc.execute_agent_actions([{"type": "identity.replace", "identity": {}}])
            mock_post.assert_not_called()
        assert result["outcomes"][0]["outcome"] == "rejected_allowlist"
    finally:
        del os.environ["FEEDLING_ACTION_ALLOWLIST"]


# ---------------------------------------------------------------------------
# execute_agent_actions — mixed batch (Group ②) with HTTP monkeypatched
# ---------------------------------------------------------------------------

def test_execute_agent_actions_mixed_allowed_and_unknown_in_enforce_mode(monkeypatch):
    monkeypatch.setenv("FEEDLING_ACTION_ALLOWLIST", "enforce")
    calls = []
    monkeypatch.setattr(
        crc._HTTP, "post",
        _http_router(
            memory=_Resp(200, {
                "status": "ok",
                "results": [{"status": "ok", "action": "memory.add"}],
                "effects": [{"type": "memory_updated"}],
            }),
            calls=calls,
        ),
    )

    actions = [
        {"type": "memory.add", "memory": {"summary": "x"}},
        {"type": "memory.frobnicate", "memory_id": "m1"},
    ]
    result = crc.execute_agent_actions(actions)

    # Only the allowed action reaches the wire; the forwarded batch is
    # exactly [memory.add] — the rejected action never appears in it.
    assert len(calls) == 1
    forwarded_types = [a.get("type") for a in calls[0][1]["actions"]]
    assert forwarded_types == ["memory.add"]

    outcomes = {o["original_type"]: o["outcome"] for o in result["outcomes"]}
    assert outcomes["memory.add"] == "applied"
    assert outcomes["memory.frobnicate"] == "rejected_allowlist"

    replies = crc.rewrite_reply_for_outcomes(["记好了。"], result["outcomes"], fallback_ok="记好了。")
    assert replies[0] == "记好了。"
    assert "memory.frobnicate" in replies[1]


# ---------------------------------------------------------------------------
# execute_agent_actions — Group ⑤: identity.patch canonicalize -> profile_patch,
# admitted even in enforce mode, and the WIRE type stays "identity.patch"
# (canonicalize must never rewrite the payload).
# ---------------------------------------------------------------------------

def test_execute_agent_actions_identity_patch_canonicalizes_and_is_admitted(monkeypatch):
    monkeypatch.setenv("FEEDLING_ACTION_ALLOWLIST", "enforce")
    calls = []
    monkeypatch.setattr(
        crc._HTTP, "post",
        _http_router(
            identity=_Resp(200, {
                "status": "ok",
                "results": [{"status": "ok", "action": "identity.profile_patch"}],
                "effects": [{"type": "identity_updated"}],
            }),
            calls=calls,
        ),
    )

    result = crc.execute_agent_actions([{"type": "identity.patch", "patch": {"agent_name": "小秘"}}])

    assert len(calls) == 1, "identity.patch must not be dropped by the allowlist gate"
    # The action forwarded on the wire is UNCHANGED — still "identity.patch",
    # never rewritten to "identity.profile_patch".
    assert calls[0][1]["actions"][0]["type"] == "identity.patch"
    assert result["outcomes"] == [{
        "original_type": "identity.patch",
        "canonical_type": "identity.profile_patch",
        "outcome": "applied",
        "error_code": "",
    }]


# ---------------------------------------------------------------------------
# execute_agent_actions — noop outcome (结果真实化 core case: HTTP succeeds but
# nothing actually changed)
# ---------------------------------------------------------------------------

def test_execute_agent_actions_noop_result_is_not_reported_as_applied(monkeypatch):
    monkeypatch.setattr(
        crc._HTTP, "post",
        _http_router(memory=_Resp(200, {
            "status": "ok",
            "results": [{"status": "ok", "action": "memory.upgrade", "skipped": "not_found", "noop": True}],
            "effects": [],
        })),
    )

    result = crc.execute_agent_actions([{"type": "memory.upgrade", "memory_id": "missing"}])

    assert result["outcomes"][0]["outcome"] == "noop"
    replies = crc.rewrite_reply_for_outcomes([], result["outcomes"], fallback_ok="改好了。")
    assert replies == [crc._ACTION_OUTCOME_ALL_FAILED_ZH]


def test_execute_agent_actions_identity_failure_does_not_block_memory_bucket(monkeypatch):
    """Buckets are tried independently: an identity HTTP failure must not
    prevent the memory bucket's HTTP call in the same batch."""
    calls = []
    monkeypatch.setattr(
        crc._HTTP, "post",
        _http_router(
            identity=_Resp(500, {"status": "error", "error": "boom"}),
            memory=_Resp(200, {"status": "ok", "results": [{"status": "ok"}], "effects": []}),
            calls=calls,
        ),
    )

    result = crc.execute_agent_actions([
        {"type": "identity.profile_patch", "patch": {"agent_name": "x"}},
        {"type": "memory.add", "memory": {"summary": "y"}},
    ])

    urls = [c[0] for c in calls]
    assert any("/v1/identity/actions" in u for u in urls)
    assert any("/v1/memory/actions" in u for u in urls), "memory bucket must still be attempted"
    outcomes = {o["original_type"]: o["outcome"] for o in result["outcomes"]}
    assert outcomes["identity.profile_patch"] == "failed_execution"
    assert outcomes["memory.add"] == "applied"


def test_execute_agent_actions_still_raises_for_garbage_prefix():
    with pytest.raises(RuntimeError, match="unsupported_agent_actions"):
        crc.execute_agent_actions([{"type": "proactive.sleep"}])


# ---------------------------------------------------------------------------
# Group "foreground": full _process_messages pipeline, HTTP monkeypatched,
# execute_agent_actions NOT mocked — the real allowlist+outcome logic runs.
# ---------------------------------------------------------------------------

def test_foreground_posts_honest_failure_when_action_type_is_rejected(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    monkeypatch.setenv("FEEDLING_ACTION_ALLOWLIST", "enforce")

    action = {"type": "memory.frobnicate", "memory_id": "m1"}
    msg = {"role": "user", "content": "帮我记一下", "ts": 5100.0}
    posted = []
    calls = []

    monkeypatch.setattr(crc, "call_agent", lambda *a, **kw: {"actions": [action], "messages": []})
    monkeypatch.setattr(crc, "post_reply", lambda reply, **kw: posted.append(reply) or {"id": "m1"})
    monkeypatch.setattr(crc._HTTP, "post", _http_router(calls=calls))

    result_ts = crc._process_messages([msg])

    action_calls = [c for c in calls if "/v1/memory/actions" in c[0] or "/v1/identity/actions" in c[0]]
    assert action_calls == [], "enforce mode must drop the action before any identity/memory HTTP call"
    assert result_ts == pytest.approx(5100.0)
    assert posted == [crc._ACTION_OUTCOME_ALL_FAILED_ZH]
    assert "改好了" not in posted[0]


def test_foreground_mixed_outcome_appends_note_to_real_reply(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    monkeypatch.setenv("FEEDLING_ACTION_ALLOWLIST", "enforce")

    actions = [
        {"type": "memory.add", "memory": {"summary": "喜欢咖啡"}},
        {"type": "memory.frobnicate", "memory_id": "m1"},
    ]
    msg = {"role": "user", "content": "记住我喜欢咖啡, 顺便干个不存在的操作", "ts": 5200.0}
    posted = []

    monkeypatch.setattr(crc, "call_agent", lambda *a, **kw: {"actions": actions, "messages": ["记下了。"]})
    monkeypatch.setattr(crc, "post_reply", lambda reply, **kw: posted.append(reply) or {"id": "m2"})
    monkeypatch.setattr(
        crc._HTTP, "post",
        _http_router(memory=_Resp(200, {
            "status": "ok",
            "results": [{"status": "ok", "action": "memory.add"}],
            "effects": [{"type": "memory_updated"}],
        })),
    )

    result_ts = crc._process_messages([msg])

    assert result_ts == pytest.approx(5200.0)
    assert posted[0] == "记下了。"
    assert len(posted) == 2
    assert "memory.frobnicate" in posted[1]


# ---------------------------------------------------------------------------
# Group "proactive": full _process_proactive_jobs pipeline, HTTP monkeypatched
# ---------------------------------------------------------------------------

def _base_proactive_job(**overrides):
    job = {
        "schema_version": 2,
        "job_id": "pj_admission_1",
        "wake_id": "wake_admission_1",
        "gate_decision_id": "gd_admission_1",
        "source": crc.PROACTIVE_JOB_SOURCE,
        "ts": 9100.0,
        "trigger": "screen_tick",
        "wake_kind": "screen",
        "user_state": "default",
        "ai_state": "present",
        "broadcast_state": "on",
        "current_app": "Docs",
        "frame_ids": [],
    }
    job.update(overrides)
    return job


def test_proactive_marks_job_failed_when_memory_identity_action_all_fails(monkeypatch):
    """Task 7 generalizes the existing introduction-only 'mark failed + skip
    posting' pattern to every proactive job, not just introductions."""
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    statuses = []
    posted = []

    action = {"type": "identity.profile_patch", "patch": {"agent_name": "小秘"}}
    monkeypatch.setattr(crc, "call_agent", lambda *a, **kw: {"actions": [action], "messages": ["改好了。"]})
    monkeypatch.setattr(crc, "post_reply", lambda reply, **kw: posted.append(reply) or {"id": "m3"})
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(
        crc, "update_proactive_job_status",
        lambda job_id, status, reason="", **kw: statuses.append((job_id, status, reason)),
    )
    monkeypatch.setattr(
        crc._HTTP, "post",
        _http_router(identity=_Resp(500, {"status": "error", "error": "boom"})),
    )

    job = _base_proactive_job()
    assert crc._process_proactive_jobs([job]) == pytest.approx(9100.0)

    assert not posted, "an all-failed batch must not post the agent's optimistic reply"
    failed = [s for s in statuses if s[1] == "failed"]
    assert failed, "non-introduction jobs must now also be marked failed on total action failure"
    assert failed[-1][2].startswith("memory_identity_action_failed")


def test_proactive_leaves_silent_background_noop_untouched(monkeypatch):
    """A routine no-op (e.g. an idempotent nudge) with NO chat reply must stay
    completely silent — Task 7 must not start posting 'didn't work' bubbles
    for background writes that were never going to produce a chat bubble."""
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    posted = []
    statuses = []

    action = {"type": "identity.dimension_nudge", "dimension": "warmth", "delta": 1}
    monkeypatch.setattr(crc, "call_agent", lambda *a, **kw: {"actions": [action], "messages": []})
    monkeypatch.setattr(crc, "post_reply", lambda reply, **kw: posted.append(reply) or {"id": "m4"})
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(
        crc, "update_proactive_job_status",
        lambda job_id, status, reason="", **kw: statuses.append((job_id, status, reason)),
    )
    monkeypatch.setattr(
        crc._HTTP, "post",
        _http_router(identity=_Resp(200, {
            "status": "ok",
            "results": [{"status": "ok", "action": "identity.profile_patch", "noop": True, "skipped": "cap_reached"}],
            "effects": [],
        })),
    )

    job = _base_proactive_job(job_id="pj_admission_noop", ts=9200.0)
    assert crc._process_proactive_jobs([job]) == pytest.approx(9200.0)

    assert posted == [], "no chat bubble should be synthesized for a silent background noop"
    # A fully silent proactive turn (no chat text, no proactive actions) is
    # already marked "failed: empty_agent_reply" downstream — pre-existing,
    # unrelated to Task 7. What Task 7 must NOT do is additionally blame the
    # noop identity/memory action for that failure.
    assert not any(s[1] == "failed" and "memory_identity_action_failed" in s[2] for s in statuses), (
        "a noop must not be reported as an action failure"
    )
