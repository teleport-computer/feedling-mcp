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


def test_get_caps_nested_list(monkeypatch):
    """Amendment test: verify cap_data wraps success data (nested list case)."""
    monkeypatch.setattr(identity_core, "get_identity",
                        lambda store: ({"identity": {"signature": list(range(1000))}}, 200))
    r = cap_identity.get("STORE")
    assert r.ok is True and len(r.data["identity"]["signature"]) == 50
