"""T498 (2026-09-06): the runner supervisor's autoverify must not re-probe users
the server already knows to be verified.

Prod, both 2026-09-04 and 2026-09-06 deploys: the runner restarted, its
in-memory ``autoverify_state`` was lost, and every hosted resident (226) got a
fresh ``/v1/chat/verify_loop`` — each one a REAL model call on the user's own
key. 140 dormant users with dead keys failed inside 15 minutes. All 226 already
carried a persisted ``chat_loop_verified`` event.

Three cells, mirrored:
  * flag on  + already verified  -> no ping, passing + already_verified
  * flag on  + not verified      -> ping as before
  * flag off + already verified  -> ping as before (iOS manual check unchanged)
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from agent_runtime import supervisor as supervisor_mod  # noqa: E402
from chat import chat_core  # noqa: E402


class _Store:
    user_id = "usr_only_if_unverified"

    def __init__(self):
        self.chat_lock = threading.RLock()
        self.chat_messages: list[dict] = []
        self.chat_waiters: list[threading.Event] = []
        self.chat_waiters_lock = threading.Lock()
        self.pings: list[dict] = []

    def append_chat(self, role, source, envelope):
        msg = {**envelope, "id": "ping1", "role": role, "source": source, "ts": 100.0}
        self.pings.append(msg)
        # Stop the loop right after the ping is inserted: these cells only ask
        # whether a ping was attempted, not whether a consumer answered it.
        raise _PingInserted()

    def notify_chat_waiters(self):
        return None


class _PingInserted(Exception):
    pass


def _resident_store(monkeypatch, *, verified: bool) -> _Store:
    store = _Store()
    from hosted import config_store as hosted_config_store

    monkeypatch.setattr(hosted_config_store, "load_active_route", lambda _store: None)
    monkeypatch.setattr(
        chat_core.boot_gates, "_chat_loop_verified_by_server", lambda _store: verified
    )
    return store


def test_flag_on_and_already_verified_skips_the_ping(monkeypatch):
    store = _resident_store(monkeypatch, verified=True)
    body, code = chat_core.verify_loop(store, {"timeout_sec": 5, "only_if_unverified": True})
    assert code == 200
    assert body["passing"] is True
    assert body["already_verified"] is True
    assert body["ping_id"] == ""
    # Not a liveness measurement: no exact-ack observation, no timing.
    assert body["loop_alive"] is None
    assert body["response_time_sec"] is None
    assert store.pings == []


@pytest.mark.parametrize(
    "payload, verified",
    [
        ({"timeout_sec": 5, "only_if_unverified": True}, False),   # flag on, unverified
        ({"timeout_sec": 5}, True),                                # flag off, verified
        ({"timeout_sec": 5, "only_if_unverified": False}, True),   # explicit off
    ],
)
def test_ping_still_happens_unless_flag_and_verified_together(monkeypatch, payload, verified):
    store = _resident_store(monkeypatch, verified=verified)
    with pytest.raises(_PingInserted):
        chat_core.verify_loop(store, payload)
    assert len(store.pings) == 1
    assert store.pings[0]["source"] == "verify_ping"


def test_supervisor_autoverify_asks_only_if_unverified(monkeypatch):
    """The supervisor is the caller whose whole purpose is gate-opening; it must
    send the flag, otherwise the server-side short-circuit never engages."""
    posted: list[dict] = []

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"passing": True, "already_verified": True}

    def _post(url, headers=None, json=None, timeout=None):
        posted.append({"url": url, "json": json})
        return _Resp()

    monkeypatch.setattr(supervisor_mod._HTTP, "post", _post)
    assert supervisor_mod._post_verify_loop("http://api", {"X-Feedling-Runtime-Token": "t"}) == {
        "passing": True, "already_verified": True,
    }
    assert len(posted) == 1
    assert posted[0]["url"].endswith("/v1/chat/verify_loop")
    assert posted[0]["json"]["only_if_unverified"] is True


@pytest.mark.parametrize("bad_flag", ["true", "false", 1, "yes", [True]])
def test_only_the_json_literal_true_opts_in(monkeypatch, bad_flag):
    """codex2 review: bool("false") is True. A caller mistake must fall through
    to the real probe, never into the short-circuit."""
    store = _resident_store(monkeypatch, verified=True)
    with pytest.raises(_PingInserted):
        chat_core.verify_loop(store, {"timeout_sec": 5, "only_if_unverified": bad_flag})
    assert len(store.pings) == 1


def test_already_verified_marks_done_but_is_not_a_fresh_pass(monkeypatch):
    """codex2 review: the host-all tick calls _enqueue_introduction when
    _maybe_autoverify returns True. A server short-circuit is not a fresh pass:
    the user must be marked done (never re-probed) yet must NOT trigger the
    introduction recovery."""
    state: dict = {}
    calls: list[int] = []

    def post_verify(api_url, headers):
        calls.append(1)
        return {"passing": True, "already_verified": True}

    fresh = supervisor_mod._maybe_autoverify(
        "u_dormant", mint_token=lambda u: "t", api_url="a",
        state=state, post_verify=post_verify, now=lambda: 100.0)
    again = supervisor_mod._maybe_autoverify(
        "u_dormant", mint_token=lambda u: "t", api_url="a",
        state=state, post_verify=post_verify, now=lambda: 200.0)
    assert fresh is False and again is False
    assert state["u_dormant"]["done"] is True
    assert len(calls) == 1


def test_fresh_probe_pass_still_counts_as_passed(monkeypatch):
    state: dict = {}
    passed = supervisor_mod._maybe_autoverify(
        "u_new", mint_token=lambda u: "t", api_url="a", state=state,
        post_verify=lambda a, h: {"passing": True, "already_verified": False},
        now=lambda: 100.0)
    assert passed is True and state["u_new"]["done"] is True


def test_post_verify_loop_surfaces_already_verified(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"passing": True, "already_verified": True}

    monkeypatch.setattr(supervisor_mod._HTTP, "post", lambda *a, **k: _Resp())
    assert supervisor_mod._post_verify_loop("http://api", {}) == {
        "passing": True, "already_verified": True,
    }
