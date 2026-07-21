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
    def fake_run_actions(store, payload, *, api_key, runtime_token):
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
    def fake_run_actions(store, payload, *, api_key, runtime_token):
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
                        lambda store, payload, *, api_key, runtime_token:
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
    def fake_run_actions(store, payload, *, api_key, runtime_token):
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
                        lambda store, payload, *, api_key, runtime_token:
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
    def must_not_run(store, payload, *, api_key, runtime_token):
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
