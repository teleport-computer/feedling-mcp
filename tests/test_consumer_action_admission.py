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


@pytest.fixture(autouse=True)
def _reset_proactive_guard_state_between_tests():
    """Mirror of the same-named fixture in tests/test_chat_resident_consumer.py.
    This file also drives ``_process_proactive_jobs``, so it needs the identical
    reset of the module-global proactive state — an autouse fixture only applies
    within its own module, so it must be duplicated here rather than inherited.

    ``_last_proactive_turn_ts`` opens the across-batch wake-coalescing window
    (added with the proactive-coalescing work): without this reset, the first
    test to realize a turn leaves the window open, and every later test's wake
    folds into it and skips the agent call entirely — which is exactly why
    ``test_proactive_all_failed_action_does_not_kill_scheduled_wake_chain`` saw
    no schedule_wake despite providing one."""
    crc._self_wake_streak = 0
    crc._proactive_fail_streak = 0
    crc._proactive_backoff_until = 0.0
    crc._provider_payment_cooldown_until = 0.0
    crc._last_proactive_turn_ts = 0.0
    crc._last_proactive_turn_job_id = ""
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


def test_rewrite_reply_mixed_appends_generic_note_without_leaking_type_name():
    """Group ②: allowed+unknown 混合 → 附加句点名未执行项(计数, 不泄漏原始
    action type — minor #5), 原回复保留."""
    outcomes = [
        {"original_type": "memory.add", "canonical_type": "memory.add",
         "outcome": "applied", "error_code": ""},
        {"original_type": "memory.frobnicate", "canonical_type": "memory.frobnicate",
         "outcome": "rejected_allowlist", "error_code": ""},
    ]
    result = crc.rewrite_reply_for_outcomes(["记好了。"], outcomes, fallback_ok="记好了。")
    assert result[0] == "记好了。"
    assert len(result) == 2
    assert result[1] == crc._ACTION_OUTCOME_MIXED_NOTE_ONE_ZH
    assert "memory.frobnicate" not in result[1]


def test_rewrite_reply_mixed_with_empty_replies_uses_fallback_plus_note():
    outcomes = [
        {"original_type": "memory.add", "canonical_type": "memory.add",
         "outcome": "applied", "error_code": ""},
        {"original_type": "identity.replace", "canonical_type": "identity.replace",
         "outcome": "rejected_allowlist", "error_code": ""},
    ]
    result = crc.rewrite_reply_for_outcomes([], outcomes, fallback_ok="改好了。")
    assert result[0] == "改好了。"
    assert result[1] == crc._ACTION_OUTCOME_MIXED_NOTE_ONE_ZH
    assert "identity.replace" not in result[1]


def test_rewrite_reply_mixed_multiple_unexecuted_uses_count():
    outcomes = [
        {"original_type": "memory.add", "canonical_type": "memory.add",
         "outcome": "applied", "error_code": ""},
        {"original_type": "memory.frobnicate", "canonical_type": "memory.frobnicate",
         "outcome": "rejected_allowlist", "error_code": ""},
        {"original_type": "memory.upgrade", "canonical_type": "memory.upgrade",
         "outcome": "noop", "error_code": ""},
    ]
    result = crc.rewrite_reply_for_outcomes(["记好了。"], outcomes, fallback_ok="记好了。")
    assert result[1] == crc._ACTION_OUTCOME_MIXED_NOTE_MANY_ZH.format(n=2)


# ---------------------------------------------------------------------------
# rewrite_reply_for_outcomes — I2: noop must not read like a failure
# ---------------------------------------------------------------------------

def test_rewrite_reply_all_noop_empty_replies_uses_distinct_noop_copy():
    outcomes = [{"original_type": "identity.dimension_nudge", "canonical_type": "identity.dimension_nudge",
                 "outcome": "noop", "error_code": ""}]
    result = crc.rewrite_reply_for_outcomes([], outcomes, fallback_ok="改好了。")
    assert result == [crc._ACTION_OUTCOME_ALL_NOOP_ZH]
    assert result != [crc._ACTION_OUTCOME_ALL_FAILED_ZH]


def test_rewrite_reply_all_noop_nonempty_replies_appends_note_keeps_reply():
    outcomes = [{"original_type": "identity.dimension_nudge", "canonical_type": "identity.dimension_nudge",
                 "outcome": "noop", "error_code": ""}]
    result = crc.rewrite_reply_for_outcomes(["嗯。"], outcomes, fallback_ok="嗯。")
    assert result[0] == "嗯。", "model's original reply must be kept, not replaced"
    assert result[1] == crc._ACTION_OUTCOME_NOOP_NOTE_ZH


def test_rewrite_reply_zero_applied_mix_of_noop_and_bad_is_reported_as_failure():
    """A batch with any genuine rejected/failed outcome is reported as a
    failure even if other items in the same batch were merely noop — a real
    failure must never be shrugged off as 'just a noop'."""
    outcomes = [
        {"original_type": "identity.dimension_nudge", "canonical_type": "identity.dimension_nudge",
         "outcome": "noop", "error_code": ""},
        {"original_type": "memory.add", "canonical_type": "memory.add",
         "outcome": "failed_execution", "error_code": "RuntimeError"},
    ]
    result = crc.rewrite_reply_for_outcomes([], outcomes, fallback_ok="改好了。")
    assert result == [crc._ACTION_OUTCOME_ALL_FAILED_ZH]


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
        "error_code": "ActionsHTTPError",
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
    assert replies[1] == crc._ACTION_OUTCOME_MIXED_NOTE_ONE_ZH
    assert "memory.frobnicate" not in replies[1], "minor #5: never leak the raw action type to the user"


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

    result = crc.execute_agent_actions([{
        "type": "identity.patch",
        "patch": {"agent_name": "小秘", "self_introduction": "我是小秘。"},
    }])

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
    # I2: a noop is not a failure — distinct copy, not the failure sentence.
    replies = crc.rewrite_reply_for_outcomes([], result["outcomes"], fallback_ok="改好了。")
    assert replies == [crc._ACTION_OUTCOME_ALL_NOOP_ZH]


def test_execute_agent_actions_identity_failure_blocks_memory_bucket_c1(monkeypatch):
    """C1: restored sequential-with-early-abort — pre-Task-7 wire semantics
    NEVER sent the memory HTTP call once the identity call raised. Shadow
    mode's byte-identical-wire guarantee (content/order/COUNT of requests)
    must hold on the failure path too, not just the happy path."""
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
        {"type": "identity.profile_patch", "patch": {"agent_name": "x", "self_introduction": "hi"}},
        {"type": "memory.add", "memory": {"summary": "y"}},
    ])

    urls = [c[0] for c in calls]
    assert urls == [f"{crc.FEEDLING_API_URL}/v1/identity/actions"], (
        "memory bucket's HTTP call must NEVER happen once identity raised — "
        "exactly one request on the wire, matching pre-Task-7 byte-for-byte"
    )
    outcomes = {o["original_type"]: o for o in result["outcomes"]}
    assert outcomes["identity.profile_patch"]["outcome"] == "failed_execution"
    assert outcomes["memory.add"]["outcome"] == "failed_execution"
    assert outcomes["memory.add"]["error_code"] == "not_attempted", (
        "the never-sent memory action must be reported as not_attempted, never applied"
    )


def test_execute_agent_actions_still_raises_for_garbage_prefix():
    with pytest.raises(RuntimeError, match="unsupported_agent_actions"):
        crc.execute_agent_actions([{"type": "proactive.sleep"}])


# ---------------------------------------------------------------------------
# I3: a missing per-item result (server truncates a batch, e.g. at 20 items)
# must NEVER be read as "applied".
# ---------------------------------------------------------------------------

def test_execute_agent_actions_missing_result_for_tail_action_is_not_applied(monkeypatch):
    n = 5
    actions = [
        {"type": "memory.add", "memory": {"summary": f"item {i}"}}
        for i in range(n)
    ]
    # Server returns only n-1 results — the last submitted action was
    # truncated / never actually reported on.
    monkeypatch.setattr(
        crc._HTTP, "post",
        _http_router(memory=_Resp(200, {
            "status": "ok",
            "results": [{"status": "ok", "action": "memory.add"} for _ in range(n - 1)],
            "effects": [],
        })),
    )

    result = crc.execute_agent_actions(actions)

    assert len(result["outcomes"]) == n
    assert [o["outcome"] for o in result["outcomes"][: n - 1]] == ["applied"] * (n - 1)
    assert result["outcomes"][-1]["outcome"] == "failed_execution"
    assert result["outcomes"][-1]["error_code"] == "result_missing"


def test_action_result_outcome_never_returns_applied_for_missing_item():
    assert crc._action_result_outcome(None) == ("failed_execution", "result_missing")
    assert crc._action_result_outcome("not-a-dict") == ("failed_execution", "result_missing")


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
    assert posted[1] == crc._ACTION_OUTCOME_MIXED_NOTE_ONE_ZH
    assert "memory.frobnicate" not in posted[1], "minor #5: never leak the raw action type to the user"


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

    action = {"type": "identity.profile_patch", "patch": {"agent_name": "小秘", "self_introduction": "我是小秘。"}}
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
    # I4: this action-failure status update is no longer necessarily the LAST
    # status call — falling through (instead of `continue`) lets the rest of
    # the turn run, and with no other actions/replies left it naturally lands
    # on the pre-existing "failed: empty_agent_reply" terminal status too.
    # What matters is that the action failure itself was reported somewhere.
    failed_reasons = [s[2] for s in statuses if s[1] == "failed"]
    assert any(r.startswith("memory_identity_action_failed") for r in failed_reasons), (
        "non-introduction jobs must now also be marked failed on total action failure"
    )


def test_proactive_all_failed_action_does_not_kill_scheduled_wake_chain(monkeypatch):
    """I4: pre-Task-7, a non-introduction memory/identity action failure was
    silently swallowed and the turn ran to completion — including any
    schedule_wake the agent asked for in the SAME turn. The generalized
    'mark job failed' must not regress that: it may suppress the optimistic
    chat reply, but the scheduled-wake block (self-rewake chain) must still
    run exactly as before."""
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    crc._self_wake_streak = 0
    scheduled_calls = []
    statuses = []
    posted = []

    actions = [
        {"type": "identity.profile_patch", "patch": {"agent_name": "小秘", "self_introduction": "我是小秘。"}},
        {"type": "schedule_wake", "at": "2030-01-01T09:30:00", "tz": "Asia/Shanghai", "note": "check in"},
    ]
    monkeypatch.setattr(crc, "call_agent", lambda *a, **kw: {"actions": actions, "messages": ["改好了。"]})
    monkeypatch.setattr(crc, "post_reply", lambda reply, **kw: posted.append(reply) or {"id": "m5"})
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(
        crc, "update_proactive_job_status",
        lambda job_id, status, reason="", **kw: statuses.append((job_id, status, reason)),
    )
    monkeypatch.setattr(
        crc, "execute_scheduled_wake_actions",
        lambda acts, job: scheduled_calls.append((acts, job)) or {
            "results": [{"type": "schedule_wake_result", "status": "scheduled", "timer_id": "sched_1"}],
        },
    )
    monkeypatch.setattr(
        crc._HTTP, "post",
        _http_router(identity=_Resp(500, {"status": "error", "error": "boom"})),
    )

    job = _base_proactive_job(job_id="pj_admission_rewake", ts=9300.0)
    assert crc._process_proactive_jobs([job]) == pytest.approx(9300.0)

    assert posted == [], "the optimistic reply must still be suppressed"
    assert scheduled_calls, "the schedule_wake action must still be attempted — self-rewake chain preserved"
    assert [a.get("type") for a in scheduled_calls[0][0]] == ["schedule_wake"]
    failed = [s for s in statuses if s[1] == "failed" and "memory_identity_action_failed" in s[2]]
    assert failed, "the identity write failure must still be marked failed for observability"


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


# ---------------------------------------------------------------------------
# Round 3 (Codex mid-point review) — C2, M11, I5, I3
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# C2: a 4xx with a parseable body still carries results/effects for any
# leading actions that DID apply — must not be treated as a uniform
# whole-bucket failure (that would invite a retry that double-applies the
# leading, possibly non-idempotent actions).
# ---------------------------------------------------------------------------

def test_execute_agent_actions_memory_partial_200_maps_every_item(monkeypatch):
    body = {
        "status": "partial",
        "results": [
            {"status": "ok", "action": "memory.add", "http_status": 200},
            {
                "status": "error",
                "error": "source_invalid",
                "http_status": 400,
            },
            {"status": "ok", "action": "memory.add", "http_status": 200},
        ],
        "effects": [
            {"type": "memory_added", "memory_id": "mom_1"},
            {"type": "memory_added", "memory_id": "mom_3"},
        ],
        "total_count": 3,
        "applied_count": 2,
        "skipped_count": 0,
        "failed_count": 1,
    }
    monkeypatch.setattr(
        crc._HTTP,
        "post",
        _http_router(memory=_Resp(200, body)),
    )

    result = crc.execute_agent_actions([
        {"type": "memory.add", "memory": {"summary": "one"}},
        {"type": "memory.add", "memory": {"summary": "bad"}},
        {"type": "memory.add", "memory": {"summary": "three"}},
    ])

    assert [row["outcome"] for row in result["outcomes"]] == [
        "applied", "failed_execution", "applied",
    ]
    assert result["outcomes"][1]["error_code"] == "source_invalid"
    assert result["effects"] == body["effects"]

def test_execute_agent_actions_partial_4xx_maps_leading_success_failing_item_and_tail(monkeypatch):
    """Rolling-compatibility body from an older memory server.

    Current memory servers return 200 and one result per item; identity and an
    older memory deployment can still return this serial-abort 4xx shape.
    """
    # 4 actions submitted; the server applied #0 and #1, #2 failed, and per
    # the server's serial-abort-on-error design #3 (the tail) was NEVER
    # attempted at all — it's simply absent from results.
    server_4xx_body = {
        "status": "error",
        "error": "title_required",
        "results": [
            {"status": "ok", "action": "memory.add", "memory": {"id": "mom_1"}},
            {"status": "ok", "action": "memory.add", "memory": {"id": "mom_2"}},
            {"status": "error", "error": "title_required", "action": "memory.add"},
        ],
        "effects": [
            {"type": "memory_added", "memory_id": "mom_1"},
            {"type": "memory_added", "memory_id": "mom_2"},
        ],
    }
    monkeypatch.setattr(
        crc._HTTP, "post",
        _http_router(memory=_Resp(400, server_4xx_body)),
    )

    actions = [
        {"type": "memory.add", "memory": {"summary": f"item {i}"}}
        for i in range(4)
    ]
    result = crc.execute_agent_actions(actions)

    outcomes = result["outcomes"]
    assert len(outcomes) == 4
    assert outcomes[0]["outcome"] == "applied"
    assert outcomes[1]["outcome"] == "applied"
    assert outcomes[2]["outcome"] == "failed_execution"
    assert outcomes[2]["error_code"] == "title_required"
    assert outcomes[3]["outcome"] == "failed_execution"
    assert outcomes[3]["error_code"] == "not_attempted", (
        "the never-reached tail must be labeled not_attempted, not lumped in "
        "with the item that actually failed"
    )
    # C2's whole point: leading successes must be visible so nothing retries
    # (and thus double-applies) mom_1/mom_2.
    assert result["effects"] == server_4xx_body["effects"]

    # And the reply must be honest without over-claiming full success or
    # full failure — mixed, since 2 of 4 really did apply.
    replies = crc.rewrite_reply_for_outcomes(["记好了。"], outcomes, fallback_ok="记好了。")
    assert replies[0] == "记好了。"
    assert replies[1] == crc._ACTION_OUTCOME_MIXED_NOTE_MANY_ZH.format(n=2)


def test_execute_agent_actions_4xx_without_results_key_falls_back_to_whole_bucket_failure(monkeypatch):
    """An error body with no results[] at all (or an unparseable/non-JSON
    body) carries no per-item signal — must fall back to the pre-existing
    uniform whole-bucket failed_execution, not crash or fabricate items."""
    monkeypatch.setattr(
        crc._HTTP, "post",
        _http_router(memory=_Resp(400, {"status": "error", "error": "actions_required"})),
    )

    result = crc.execute_agent_actions([{"type": "memory.add", "memory": {"summary": "x"}}])

    assert result["outcomes"] == [{
        "original_type": "memory.add",
        "canonical_type": "memory.add",
        "outcome": "failed_execution",
        "error_code": "ActionsHTTPError",
    }]


def test_execute_agent_actions_4xx_unparseable_body_falls_back_to_whole_bucket_failure(monkeypatch):
    class _UnparseableResp:
        status_code = 400
        text = "<html>not json</html>"

        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(
        crc._HTTP, "post",
        _http_router(memory=_UnparseableResp()),
    )

    result = crc.execute_agent_actions([{"type": "memory.add", "memory": {"summary": "x"}}])

    assert result["outcomes"][0]["outcome"] == "failed_execution"
    assert result["outcomes"][0]["error_code"] == "ActionsHTTPError"


def test_execute_agent_actions_identity_partial_4xx_still_blocks_memory_bucket(monkeypatch):
    """C2 must not weaken C1: even with a RECOVERED partial identity
    failure, the identity REQUEST still failed, so memory must still never
    be sent."""
    calls = []
    monkeypatch.setattr(
        crc._HTTP, "post",
        _http_router(
            identity=_Resp(400, {
                "status": "error",
                "error": "boom",
                "results": [{"status": "ok", "action": "identity.profile_patch"}],
                "effects": [{"type": "identity_updated"}],
            }),
            memory=_Resp(200, {"status": "ok", "results": [{"status": "ok"}], "effects": []}),
            calls=calls,
        ),
    )

    result = crc.execute_agent_actions([
        {"type": "identity.profile_patch", "patch": {"agent_name": "a", "self_introduction": "hi"}},
        {"type": "identity.dimension_nudge", "dimension": "warmth", "delta": 1},
        {"type": "memory.add", "memory": {"summary": "y"}},
    ])

    urls = [c[0] for c in calls]
    assert urls == [f"{crc.FEEDLING_API_URL}/v1/identity/actions"], (
        "memory must still never be sent once the identity request failed"
    )
    outcomes = {o["original_type"]: o for o in result["outcomes"]}
    assert outcomes["identity.profile_patch"]["outcome"] == "applied"
    assert outcomes["identity.dimension_nudge"]["outcome"] == "failed_execution"
    assert outcomes["identity.dimension_nudge"]["error_code"] == "not_attempted", (
        "the recovered results[] only covers the first action — the second "
        "identity action was never reached by the server's serial write"
    )
    assert outcomes["memory.add"]["outcome"] == "failed_execution"
    assert outcomes["memory.add"]["error_code"] == "not_attempted"


# ---------------------------------------------------------------------------
# M11: an unrecognized/missing per-item status must never be treated as
# applied just because the item was "some dict".
# ---------------------------------------------------------------------------

def test_action_result_outcome_rejects_unknown_status_as_invalid_result():
    assert crc._action_result_outcome({}) == ("failed_execution", "invalid_result")
    assert crc._action_result_outcome({"status": "pending"}) == ("failed_execution", "invalid_result")
    assert crc._action_result_outcome({"action": "memory.add"}) == ("failed_execution", "invalid_result")


def test_action_result_outcome_accepts_the_success_status_family():
    assert crc._action_result_outcome({"status": "ok"}) == ("applied", "")
    assert crc._action_result_outcome({"status": "created"}) == ("applied", "")
    assert crc._action_result_outcome({"status": "replaced"}) == ("applied", "")


def test_execute_agent_actions_unknown_item_status_is_not_applied(monkeypatch):
    monkeypatch.setattr(
        crc._HTTP, "post",
        _http_router(memory=_Resp(200, {
            "status": "ok",
            "results": [{"action": "memory.add"}],  # no "status" key at all
            "effects": [],
        })),
    )

    result = crc.execute_agent_actions([{"type": "memory.add", "memory": {"summary": "x"}}])

    assert result["outcomes"][0]["outcome"] == "failed_execution"
    assert result["outcomes"][0]["error_code"] == "invalid_result"


# ---------------------------------------------------------------------------
# I5: notes must follow the ORIGINAL user message's language, not whatever
# Chinese boilerplate got prepended to the composed prompt (io_cli catalog).
# ---------------------------------------------------------------------------

def test_rewrite_reply_lang_param_overrides_autodetect():
    outcomes = [{"original_type": "memory.frobnicate", "canonical_type": "memory.frobnicate",
                 "outcome": "rejected_allowlist", "error_code": ""}]
    # fallback_ok is Chinese, but an explicit lang="en" must win.
    result = crc.rewrite_reply_for_outcomes([], outcomes, fallback_ok="改好了。", lang="en")
    assert result == [crc._ACTION_OUTCOME_ALL_FAILED_EN]

    result_zh = crc.rewrite_reply_for_outcomes([], outcomes, fallback_ok="Done.", lang="zh")
    assert result_zh == [crc._ACTION_OUTCOME_ALL_FAILED_ZH]


def test_rewrite_reply_lang_empty_falls_back_to_autodetect():
    outcomes = [{"original_type": "memory.frobnicate", "canonical_type": "memory.frobnicate",
                 "outcome": "rejected_allowlist", "error_code": ""}]
    result = crc.rewrite_reply_for_outcomes([], outcomes, fallback_ok="改好了。")
    assert result == [crc._ACTION_OUTCOME_ALL_FAILED_ZH]


def test_foreground_english_user_gets_english_note_despite_chinese_catalog_in_prompt(monkeypatch):
    """The bug: the io_cli catalog (Chinese) gets prepended to `content`
    BEFORE the action-execution block runs, so naive language auto-detect
    against the fully-composed prompt would wrongly conclude "Chinese" for
    an English-speaking user. The fix derives language from the raw,
    pre-injection message instead."""
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    monkeypatch.setenv("FEEDLING_ACTION_ALLOWLIST", "enforce")

    action = {"type": "memory.frobnicate", "memory_id": "m1"}
    msg = {"role": "user", "content": "please remember this for me", "ts": 5300.0}
    posted = []

    monkeypatch.setattr(crc, "call_agent", lambda *a, **kw: {"actions": [action], "messages": []})
    monkeypatch.setattr(crc, "post_reply", lambda reply, **kw: posted.append(reply) or {"id": "m6"})
    monkeypatch.setattr(crc._HTTP, "post", _http_router())
    # Simulate the io_cli catalog injection actually firing (Chinese text
    # prepended to `content` downstream of the raw-content snapshot) —
    # patched to a fixed Chinese string rather than depending on AGENT_MODE
    # plumbing/a real io_cli.py build.
    monkeypatch.setattr(
        crc, "_prepend_io_cli_capability_catalog",
        lambda content: "【可用命令目录：身份写卡、记忆写卡……全部中文说明】\n\n" + content,
    )

    crc._process_messages([msg])

    assert posted == [crc._ACTION_OUTCOME_ALL_FAILED_EN], (
        "an English user must get the English honest-failure copy even "
        "though the composed prompt now contains a Chinese catalog block"
    )


def test_foreground_chinese_user_still_gets_chinese_note(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    monkeypatch.setenv("FEEDLING_ACTION_ALLOWLIST", "enforce")

    action = {"type": "memory.frobnicate", "memory_id": "m1"}
    msg = {"role": "user", "content": "帮我记一下这件事", "ts": 5301.0}
    posted = []

    monkeypatch.setattr(crc, "call_agent", lambda *a, **kw: {"actions": [action], "messages": []})
    monkeypatch.setattr(crc, "post_reply", lambda reply, **kw: posted.append(reply) or {"id": "m7"})
    monkeypatch.setattr(crc._HTTP, "post", _http_router())

    crc._process_messages([msg])

    assert posted == [crc._ACTION_OUTCOME_ALL_FAILED_ZH]


# ---------------------------------------------------------------------------
# I3: same-source rename-pairing (D4) enforced in the agent-origin funnel,
# in ALL allowlist modes — not part of the shadow experiment.
# ---------------------------------------------------------------------------

def test_execute_agent_actions_rename_without_self_introduction_is_rejected_and_not_sent(monkeypatch):
    calls = []
    monkeypatch.setattr(crc._HTTP, "post", _http_router(calls=calls))

    result = crc.execute_agent_actions([
        {"type": "identity.profile_patch", "patch": {"agent_name": "老六"}},
    ])

    assert calls == [], "an unpaired rename must never reach the HTTP layer"
    assert result["outcomes"] == [{
        "original_type": "identity.profile_patch",
        "canonical_type": "identity.profile_patch",
        "outcome": "rejected_validation",
        "error_code": "rename_requires_self_introduction",
    }]


@pytest.mark.parametrize("mode", ["shadow", "enforce", "off"])
def test_execute_agent_actions_rename_pairing_enforced_in_every_allowlist_mode(monkeypatch, mode):
    """D4 is NOT part of the shadow/enforce/off allowlist experiment — it
    must reject in all three modes identically."""
    monkeypatch.setenv("FEEDLING_ACTION_ALLOWLIST", mode)
    calls = []
    monkeypatch.setattr(crc._HTTP, "post", _http_router(calls=calls))

    result = crc.execute_agent_actions([
        {"type": "identity.profile_patch", "patch": {"agent_name": "老六"}},
    ])

    assert calls == []
    assert result["outcomes"][0]["outcome"] == "rejected_validation"


def test_execute_agent_actions_paired_rename_is_forwarded(monkeypatch):
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

    result = crc.execute_agent_actions([
        {"type": "identity.profile_patch", "patch": {"agent_name": "老六", "self_introduction": "我是老六。"}},
    ])

    assert len(calls) == 1, "a properly paired rename must be forwarded"
    assert result["outcomes"][0]["outcome"] == "applied"


def test_execute_agent_actions_rename_pairing_via_identity_patch_alias(monkeypatch):
    """The pairing check must apply to the CANONICAL type, so the identity.patch
    alias (which canonicalizes to identity.profile_patch) is covered too."""
    calls = []
    monkeypatch.setattr(crc._HTTP, "post", _http_router(calls=calls))

    result = crc.execute_agent_actions([
        {"type": "identity.patch", "patch": {"agent_name": "老六"}},
    ])

    assert calls == []
    assert result["outcomes"][0]["outcome"] == "rejected_validation"


def test_execute_agent_actions_rename_pairing_top_level_fields_no_patch_key(monkeypatch):
    """Matches the server's merge extraction: when there's no "patch" dict
    key at all, the effective patch is built purely from top-level profile
    fields on the action — an agent that puts agent_name directly on the
    action (no nested "patch") is still caught."""
    calls = []
    monkeypatch.setattr(crc._HTTP, "post", _http_router(calls=calls))

    result = crc.execute_agent_actions([
        {"type": "identity.profile_patch", "agent_name": "老六"},
    ])

    assert calls == []
    assert result["outcomes"][0]["outcome"] == "rejected_validation"


def test_execute_agent_actions_rename_pairing_bypass_shape_now_rejected(monkeypatch):
    """Consolidated re-review regression: the funnel used to build its
    effective patch as `action.get("patch") if dict else action` — no
    top-level overlay. An agent could put the rename at the TOP level
    while "patch" carried something unrelated (or was merely present with
    other fields), so the funnel's naive check saw no "agent_name" inside
    "patch" and passed it through; the SERVER then merges the top-level
    agent_name into the patch (backend/identity/actions.py's
    _identity_profile_patch) and applies the unpaired rename anyway — on
    the VPS/api-key path the server's OWN pairing gate doesn't even fire
    (it's runtime-token-gated), so nothing else would have caught this.
    After the fix, the funnel merges top-level fields the same way the
    server does and catches it."""
    calls = []
    monkeypatch.setattr(crc._HTTP, "post", _http_router(calls=calls))

    result = crc.execute_agent_actions([
        {
            "type": "identity.profile_patch",
            "patch": {"category": "伙伴"},
            "agent_name": "老六",
        },
    ])

    assert calls == [], "the merged-in top-level rename must be caught before any HTTP call"
    assert result["outcomes"][0]["outcome"] == "rejected_validation"
    assert result["outcomes"][0]["error_code"] == "rename_requires_self_introduction"


def test_execute_agent_actions_rename_pairing_false_reject_shape_now_forwarded(monkeypatch):
    """The mirror-image bug: self_introduction riding at the TOP level
    (agent_name inside "patch") is exactly what the server would accept
    (it merges the top-level self_introduction in before validating) — the
    funnel must not falsely reject it just because self_introduction isn't
    physically inside the "patch" dict."""
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

    result = crc.execute_agent_actions([
        {
            "type": "identity.profile_patch",
            "patch": {"agent_name": "老六"},
            "self_introduction": "我是老六。",
        },
    ])

    assert len(calls) == 1, "a rename paired via a top-level self_introduction must be forwarded"
    assert result["outcomes"][0]["outcome"] == "applied"


def test_execute_agent_actions_rename_pairing_does_not_mutate_the_forwarded_action(monkeypatch):
    """The funnel's merge must be a read-only COPY for validation purposes
    only — it must never mutate action["patch"] in place (that would change
    the actual JSON body sent to the server, unlike the server's own
    in-place merge, which is fine there because the server owns that dict)."""
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

    patch_dict = {"agent_name": "老六"}
    action = {"type": "identity.profile_patch", "patch": patch_dict, "self_introduction": "我是老六。"}
    crc.execute_agent_actions([action])

    assert patch_dict == {"agent_name": "老六"}, "the original patch dict must be untouched"
    assert calls[0][1]["actions"][0]["patch"] == {"agent_name": "老六"}, (
        "the wire payload must be byte-identical to the input — self_introduction "
        "must NOT be duplicated into the forwarded patch dict"
    )


def test_execute_agent_actions_rename_pairing_only_applies_to_profile_patch():
    """A dimension_nudge or relationship_days_set action must never be
    caught by the rename-pairing check even if it happens to carry an
    'agent_name'-shaped key by coincidence (defensive; these action types
    don't have that field in practice)."""
    # No agent_name at all here — just confirms non-profile_patch canonical
    # types skip the check entirely (no AttributeError / false rejection).
    with patch.object(crc._HTTP, "post") as mock_post:
        mock_post.return_value = _Resp(200, {"status": "ok", "results": [{"status": "ok"}], "effects": []})
        result = crc.execute_agent_actions([
            {"type": "identity.dimension_nudge", "dimension": "warmth", "delta": 1},
        ])
    assert result["outcomes"][0]["outcome"] == "applied"


def test_foreground_unpaired_rename_via_action_is_rejected_not_forwarded(monkeypatch):
    """Full pipeline: an agent-emitted rename action missing self_introduction
    must be rejected by the funnel and never reach the HTTP layer, with an
    honest reply — not the fake 'Done' text."""
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    action = {"type": "identity.profile_patch", "patch": {"agent_name": "老六"}}
    msg = {"role": "user", "content": "改个名字叫老六", "ts": 5400.0}
    posted = []
    calls = []

    monkeypatch.setattr(crc, "call_agent", lambda *a, **kw: {"actions": [action], "messages": []})
    monkeypatch.setattr(crc, "post_reply", lambda reply, **kw: posted.append(reply) or {"id": "m8"})
    monkeypatch.setattr(crc._HTTP, "post", _http_router(calls=calls))

    crc._process_messages([msg])

    action_calls = [c for c in calls if "/v1/identity/actions" in c[0] or "/v1/memory/actions" in c[0]]
    assert action_calls == [], "an unpaired rename must never reach the HTTP layer"
    assert posted == [crc._ACTION_OUTCOME_ALL_FAILED_ZH]


def test_foreground_paired_rename_via_action_is_forwarded_and_reported_done(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    action = {"type": "identity.profile_patch", "patch": {"agent_name": "老六", "self_introduction": "我是老六。"}}
    msg = {"role": "user", "content": "改个名字叫老六，介绍也一起改", "ts": 5401.0}
    posted = []
    calls = []

    monkeypatch.setattr(crc, "call_agent", lambda *a, **kw: {"actions": [action], "messages": []})
    monkeypatch.setattr(crc, "post_reply", lambda reply, **kw: posted.append(reply) or {"id": "m9"})
    monkeypatch.setattr(
        crc._HTTP, "post",
        _http_router(identity=_Resp(200, {
            "status": "ok",
            "results": [{"status": "ok", "action": "identity.profile_patch"}],
            "effects": [{"type": "identity_updated"}],
        }), calls=calls),
    )

    crc._process_messages([msg])

    action_calls = [c for c in calls if "/v1/identity/actions" in c[0] or "/v1/memory/actions" in c[0]]
    assert len(action_calls) == 1
    assert posted == ["改好了。"]
