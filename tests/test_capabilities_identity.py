import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))  # noqa: E402

from identity import identity_core  # noqa: E402
from capabilities import identity as cap_identity  # noqa: E402


def test_get_wraps(monkeypatch):
    monkeypatch.setattr(identity_core, "get_identity",
                        lambda store: ({"identity": {"days_with_user": 3}}, 200))
    r = cap_identity.get("STORE")
    assert r.ok is True and r.data["identity"]["days_with_user"] == 3


def test_patch_builds_profile_patch_action(monkeypatch):
    seen = {}
    def fake_run_actions(store, payload, *, api_key, runtime_token, trusted_relationship_anchor=None):
        seen["payload"] = payload
        seen["runtime_token"] = runtime_token
        return {"applied": True}, 200
    monkeypatch.setattr(identity_core, "run_actions", fake_run_actions)
    r = cap_identity.patch("STORE", api_key="k", runtime_token="rt",
                           params={"self_introduction": "hi", "signature": ["a", "b"]})
    assert r.ok is True and r.data == {"applied": True}
    assert seen["payload"] == {"action": {"type": "identity.profile_patch",
                                          "patch": {"self_introduction": "hi", "signature": ["a", "b"]}}}
    assert seen["runtime_token"] == "rt"


def test_patch_carries_agent_name(monkeypatch):
    """Renaming must reach the server.

    Regression: the top-level fallback only picked up self_introduction/signature,
    so an agent asked to rename itself could rewrite its self-introduction ("我是
    老6…") while agent_name stayed stale — the app kept showing the old name and
    the agent reported success. Same gap the V1 io_cli had.
    """
    seen = {}
    def fake_run_actions(store, payload, *, api_key, runtime_token, trusted_relationship_anchor=None):
        seen["payload"] = payload
        return {"applied": True}, 200
    monkeypatch.setattr(identity_core, "run_actions", fake_run_actions)

    r = cap_identity.patch("STORE", api_key="k", runtime_token="rt",
                           params={"agent_name": "老6"})
    assert r.ok is True
    assert seen["payload"]["action"]["patch"] == {"agent_name": "老6"}


def test_patch_carries_agent_name_alongside_intro(monkeypatch):
    """Rename + re-introduce must land in ONE patch: identity actions are applied
    one-by-one and are not atomic across actions, so splitting them can half-apply."""
    seen = {}
    monkeypatch.setattr(identity_core, "run_actions",
                        lambda store, payload, *, api_key, runtime_token, trusted_relationship_anchor=None:
                        (seen.update(payload=payload), ({"applied": True}, 200))[1])
    cap_identity.patch("STORE", params={"agent_name": "老6", "self_introduction": "我是老6"})
    assert seen["payload"]["action"]["patch"] == {
        "agent_name": "老6", "self_introduction": "我是老6"}


def test_patch_merges_top_level_field_into_an_explicit_patch_object(monkeypatch):
    """Mixed input must not silently drop the top-level field.

    `patch` used to win outright, so {"agent_name": "老6", "patch": {"self_introduction":
    "..."}} — a very natural shape once agent_name is an advertised top-level param —
    passed validation and reached the server with the rename stripped out. That is the
    original bug reproduced exactly: intro updates, name doesn't, no error anywhere.
    """
    seen = {}
    def fake_run_actions(store, payload, *, api_key, runtime_token, trusted_relationship_anchor=None):
        seen["payload"] = payload
        return {"applied": True}, 200
    monkeypatch.setattr(identity_core, "run_actions", fake_run_actions)

    r = cap_identity.patch("STORE", params={
        "agent_name": "老6",
        "patch": {"self_introduction": "我是老6"},
    })
    assert r.ok is True
    assert seen["payload"]["action"]["patch"] == {
        "agent_name": "老6", "self_introduction": "我是老6"}


def test_patch_keeps_the_explicit_patch_value_when_a_field_is_given_twice(monkeypatch):
    """Same key on both sides: the explicit `patch` wins, exactly as before.

    Rejecting the ambiguity was tempting but unsafe. This normalization is SHARED
    with the persisted-effect replay check — serve_worker validates a decrypted
    effect through validate_tool_args — and an effect enqueued BEFORE this change
    may legitimately carry both keys: the old code read `patch` and ignored the
    top level. Turning that shape into an error makes replay raise a plain
    RuntimeError, which the outbox treats as RETRYABLE, so an effect queued by a
    pre-upgrade worker would retry forever instead of applying. Keeping the old
    precedence keeps every already-stored payload interpreted the way it was written.
    """
    seen = {}
    monkeypatch.setattr(identity_core, "run_actions",
                        lambda store, payload, *, api_key, runtime_token, trusted_relationship_anchor=None:
                        (seen.update(payload=payload), ({"applied": True}, 200))[1])
    r = cap_identity.patch("STORE", params={
        "agent_name": "老6", "patch": {"agent_name": "老七"}})
    assert r.ok is True
    assert seen["payload"]["action"]["patch"] == {"agent_name": "老七"}


def test_patch_rejects_a_non_object_patch_instead_of_dropping_it(monkeypatch):
    """A malformed `patch` must fail closed.

    The model-facing schema already rejects it, but the capability is callable
    without the validator. Coercing it to {} would apply the top-level fields and
    report success for a call that was partly garbage; the previous code passed it
    through and let the server refuse it. retryable=False keeps the outbox's
    terminal-discard semantics for a deterministic 4xx.
    """
    def must_not_run(store, payload, *, api_key, runtime_token, trusted_relationship_anchor=None):
        raise AssertionError("must not reach the server")
    monkeypatch.setattr(identity_core, "run_actions", must_not_run)
    r = cap_identity.patch("STORE", params={"patch": "not-an-object", "agent_name": "老6"})
    assert r.ok is False
    assert r.error["code"] == "capability_invalid_input"
    assert r.error["retryable"] is False


def test_get_caps_nested_list(monkeypatch):
    """Amendment test: verify cap_data wraps success data (nested list case)."""
    monkeypatch.setattr(identity_core, "get_identity",
                        lambda store: ({"identity": {"signature": list(range(1000))}}, 200))
    r = cap_identity.get("STORE")
    assert r.ok is True and len(r.data["identity"]["signature"]) == 50


def test_nudge_builds_dimension_nudge_action(monkeypatch):
    captured = {}

    def fake_run_actions(store, payload, *, api_key, runtime_token, trusted_relationship_anchor=None):
        captured["payload"] = payload
        captured["rt"] = runtime_token
        return {"status": "ok", "action": "identity.dimension_nudge"}, 200

    monkeypatch.setattr(identity_core, "run_actions", fake_run_actions)
    r = cap_identity.nudge("STORE", api_key="k", runtime_token="rt",
                           params={"dimension": "playfulness", "delta": 3, "reason": "更活泼"})
    assert r.ok is True
    action = captured["payload"]["action"]
    assert action["type"] == "identity.dimension_nudge"
    assert action["dimension"] == "playfulness"
    assert action["delta"] == 3
    assert action["reason"] == "更活泼"
    assert captured["rt"] == "rt"


def test_nudge_coerces_numeric_string_delta_and_omits_blank_reason(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        identity_core, "run_actions",
        lambda store, payload, *, api_key, runtime_token, trusted_relationship_anchor=None: (captured.update(payload=payload) or ({"status": "ok"}, 200)))
    cap_identity.nudge("STORE", params={"dimension": "warmth", "delta": "2"})
    action = captured["payload"]["action"]
    assert action["delta"] == 2
    assert "reason" not in action


def test_nudge_fails_closed_on_non_integer_delta():
    # Deterministic bad input -> terminal discard, never retried (like patch's
    # malformed-'patch' guard). run_actions must not even be called.
    r = cap_identity.nudge("STORE", params={"dimension": "warmth", "delta": "abc"})
    assert r.ok is False
    assert r.error["retryable"] is False


def test_nudge_fails_closed_on_missing_dimension():
    r = cap_identity.nudge("STORE", params={"delta": 1})
    assert r.ok is False
    assert r.error["retryable"] is False


def test_patch_threads_frozen_relationship_anchor_via_call_path_param(monkeypatch):
    """Round-4 (Important 1) sink path: when the identity sink calls this
    capability with the trusted frozen relationship_started_at (resolved at
    enqueue time, stripped from the model args), it must reach run_actions as the
    EXPLICIT ``trusted_relationship_anchor`` keyword argument — NOT stuffed into
    the action dict. Threading it via the call path is what keeps it trustworthy
    only on this sink route; the public request path never passes it, so a request
    body carrying relationship_started_at can never forge a frozen anchor."""
    seen = {}
    monkeypatch.setattr(identity_core, "run_actions",
                        lambda store, payload, *, api_key, runtime_token, trusted_relationship_anchor=None:
                        (seen.update(payload=payload,
                                     trusted_relationship_anchor=trusted_relationship_anchor),
                         ({"applied": True}, 200))[1])
    cap_identity.patch("STORE", params={
        "patch": {"relationship_days": 300},
        "relationship_started_at": "2026-04-10",
    })
    action = seen["payload"]["action"]
    assert action["patch"] == {"relationship_days": 300}
    # The anchor is NOT in the action dict — it rides the call-path param.
    assert "relationship_started_at" not in action
    assert seen["trusted_relationship_anchor"] == "2026-04-10"


def test_patch_ignores_blank_frozen_anchor(monkeypatch):
    seen = {}
    monkeypatch.setattr(identity_core, "run_actions",
                        lambda store, payload, *, api_key, runtime_token, trusted_relationship_anchor=None:
                        (seen.update(payload=payload,
                                     trusted_relationship_anchor=trusted_relationship_anchor),
                         ({"applied": True}, 200))[1])
    cap_identity.patch("STORE", params={"patch": {"relationship_days": 5}, "relationship_started_at": "  "})
    assert "relationship_started_at" not in seen["payload"]["action"]
    # A blank frozen anchor is normalized to None (falls back to days at the sink).
    assert seen["trusted_relationship_anchor"] is None


def test_relationship_days_error_live_check():
    from capabilities import identity as ci
    assert ci.relationship_days_error({"patch": {"relationship_days": 300}}) is None
    assert ci.relationship_days_error({"patch": {"relationship_days": 0}}) is None
    assert ci.relationship_days_error({"self_introduction": "hi"}) is None  # not present
    assert ci.relationship_days_error({"patch": {"relationship_days": "300"}}) == \
        "relationship_days_must_be_non_negative_int"
    assert ci.relationship_days_error({"patch": {"relationship_days": True}}) == \
        "relationship_days_must_be_non_negative_int"
    assert ci.relationship_days_error({"patch": {"relationship_days": -1}}) == \
        "relationship_days_must_be_non_negative_int"
    assert ci.relationship_days_error({"patch": {"relationship_days": 10 ** 9}}) == \
        "relationship_days_out_of_range"


# --------------------------------------------------------------------------- #
# get() must DECRYPT — the model is not an iOS client
# --------------------------------------------------------------------------- #
#
# `identity_core.get_identity` returns the raw E2E envelope on purpose: the
# public GET /v1/identity/get exists for iOS, which decrypts locally. The V2
# capability shares that core but has no local key — so it has to go through the
# enclave, exactly like every memory readside does. It didn't: `get()` accepted
# api_key/runtime_token and dropped both, handing the model `body_ct` and no
# agent_name/self_introduction on every single call (prod: usr_81a0645d, V2).

import json  # noqa: E402

from core import enclave as core_enclave  # noqa: E402


def _envelope(**overrides) -> dict:
    env = {
        "v": 1, "id": "idc_1",
        "body_ct": "CIPHERTEXT", "nonce": "n", "K_user": "ku", "K_enclave": "ke",
        "owner_user_id": "usr_x", "visibility": "shared",
        "created_at": "2026-07-14T13:35:35", "updated_at": "2026-07-22T18:00:29",
        "enclave_pk_fpr": "fpr",
        # get_identity injects this live-computed field alongside the envelope
        "days_with_user": 47,
    }
    env.update(overrides)
    return env


_INNER = {
    "agent_name": "裴晟",
    "self_introduction": "我陪你写代码",
    "dimensions": [{"name": "温度", "value": 70}],
    "category": "companion",
    "signature": ["直接", "不哄"],
    "custom_persona_prompt": "别叫我宝宝",
}


def test_get_decrypts_the_card_instead_of_handing_the_model_ciphertext(monkeypatch):
    monkeypatch.setattr(identity_core, "get_identity",
                        lambda store: ({"identity": _envelope()}, 200))
    seen = {}

    def fake_decrypt(env, api_key, *, purpose, runtime_token=""):
        seen["api_key"], seen["runtime_token"] = api_key, runtime_token
        seen["envelope"] = env
        return json.dumps(_INNER).encode("utf-8")

    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", fake_decrypt)

    r = cap_identity.get("STORE", api_key=None, runtime_token="rt")

    assert r.ok is True
    card = r.data["identity"]
    assert card["agent_name"] == "裴晟"
    assert card["self_introduction"] == "我陪你写代码"
    assert card["decrypt_status"] == "ok"
    assert card["days_with_user"] == 47          # live value survives the decrypt
    assert "body_ct" not in json.dumps(r.data, ensure_ascii=False)
    # a hosted worker has no api_key — the token is the only usable credential
    assert seen["runtime_token"] == "rt" and seen["api_key"] is None


def test_get_forwards_every_profile_field_not_just_the_headline_ones(monkeypatch):
    """card_policy owns the field list; a hand-copied subset silently ERASES the
    rest on the next partial update (this exact drift once dropped
    custom_persona_prompt from the enclave's own route)."""
    monkeypatch.setattr(identity_core, "get_identity",
                        lambda store: ({"identity": _envelope()}, 200))
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave",
                        lambda *a, **k: json.dumps(_INNER).encode("utf-8"))

    card = cap_identity.get("STORE", runtime_token="rt").data["identity"]

    assert card["custom_persona_prompt"] == "别叫我宝宝"
    assert card["signature"] == ["直接", "不哄"]


def test_get_reports_failure_rather_than_falling_back_to_ciphertext(monkeypatch):
    """The one thing worse than "I can't read your card" is handing the model
    ciphertext under ok=True: the agent then reports the card as unreadable
    garbage and the real cause is destroyed."""
    monkeypatch.setattr(identity_core, "get_identity",
                        lambda store: ({"identity": _envelope()}, 200))

    def boom(*_a, **_k):
        raise RuntimeError("enclave_unavailable")

    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", boom)

    r = cap_identity.get("STORE", runtime_token="rt")

    assert r.ok is False
    assert "CIPHERTEXT" not in json.dumps(r.error, ensure_ascii=False)
    assert "enclave_unavailable" in r.error["message"]


def test_get_does_not_call_the_enclave_for_a_local_only_card(monkeypatch):
    """local_only means the user opted the agent OUT. Spending an enclave decrypt
    on it would be both wasteful and a policy violation."""
    monkeypatch.setattr(identity_core, "get_identity",
                        lambda store: ({"identity": _envelope(visibility="local_only")}, 200))
    calls = {"n": 0}

    def counted(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("must not decrypt a local_only card")

    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", counted)

    r = cap_identity.get("STORE", runtime_token="rt")

    assert calls["n"] == 0
    assert r.ok is True
    assert r.data["identity"]["decrypt_status"] == "local_only_agent_cannot_read"
    assert "body_ct" not in json.dumps(r.data, ensure_ascii=False)


def test_get_passes_through_an_absent_card(monkeypatch):
    monkeypatch.setattr(identity_core, "get_identity",
                        lambda store: ({"identity": None}, 200))
    r = cap_identity.get("STORE", runtime_token="rt")
    assert r.ok is True and r.data["identity"] is None
