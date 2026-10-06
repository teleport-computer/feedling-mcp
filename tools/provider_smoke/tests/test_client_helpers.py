from tools.provider_smoke import client


def test_is_hosted_response_true_for_202_contract():
    body = {"status": "processing",
            "runtime": {"engine": "feedling_agent_runtime", "mode": "hosted_agent"}}
    assert client.is_hosted_response(body)


def test_is_hosted_response_false_for_native_200():
    body = {"status": "ok", "reply": "hi", "runtime": {"engine": "native"}}
    assert not client.is_hosted_response(body)


def test_newest_openclaw_after_filters_and_picks_latest():
    msgs = [
        {"role": "user", "ts": 100, "body_ct": "x"},        # not openclaw
        {"role": "openclaw", "ts": 90, "body_ct": "old"},   # before cutoff
        {"role": "openclaw", "ts": 110, "body_ct": "a"},    # candidate
        {"role": "openclaw", "ts": 120, "body_ct": "b"},    # newest candidate
        {"role": "openclaw", "ts": 130, "body_ct": ""},     # no body -> skip
    ]
    picked = client.newest_openclaw_after(msgs, after_ts=100)
    assert picked["ts"] == 120 and picked["body_ct"] == "b"


def test_newest_openclaw_after_returns_none_when_empty():
    assert client.newest_openclaw_after([], after_ts=0) is None


def test_newest_openclaw_after_accepts_assistant_and_agent_roles():
    msgs = [
        {"role": "assistant", "ts": 110, "body_ct": "a"},
        {"role": "agent", "ts": 120, "body_ct": "b"},
    ]
    picked = client.newest_openclaw_after(msgs, after_ts=100)
    assert picked["ts"] == 120


def test_newest_openclaw_after_accepts_plaintext_body_reply():
    """T542: a hosted plaintext-tier reply (Runtime V2 new-account default) has
    no ``body_ct`` envelope — its content is a plaintext ``body``. It must be
    picked, not skipped as it was before."""
    msgs = [
        {"role": "openclaw", "ts": 110, "body": "hi there"},   # plaintext reply
        {"role": "openclaw", "ts": 105, "body_ct": "enc"},     # encrypted reply
        {"role": "openclaw", "ts": 108, "body": "", "body_ct": ""},  # empty -> skip
    ]
    picked = client.newest_openclaw_after(msgs, after_ts=100)
    assert picked["ts"] == 110 and picked.get("body") == "hi there"


def test_reply_has_content_requires_body_ct_or_body():
    assert client._reply_has_content({"body_ct": "enc"})
    assert client._reply_has_content({"body": "plain"})
    assert not client._reply_has_content({"body_ct": "", "body": ""})
    assert not client._reply_has_content({})


def test_is_hosted_response_false_when_processing_without_runtime():
    assert not client.is_hosted_response({"status": "processing"})


def test_identity_init_body_has_required_fields():
    body = client.identity_init_body()
    assert set(body) >= {"identity", "days_with_user", "relationship_anchor_evidence"}
    assert body["days_with_user"] == 0 and isinstance(body["days_with_user"], int)
    assert len(body["relationship_anchor_evidence"]) >= 8
    assert set(body["identity"]) >= {"agent_name", "self_introduction", "dimensions"}
    assert body["identity"]["dimensions"] == []


# ── T542: poll_turn binds the verdict to the turn and honours backend failure ──
# The executed poll path (not just the row helper) must: bind the reply to the
# sent message via reply_to_message_id, treat turn-activity `failure` as
# authoritative (fail even with nonempty fallback text), and fail closed when the
# verdict never settles.
import time as _time
from tools.provider_smoke import client as _client


class _StubClient(_client.SmokeClient):
    """Drives poll_turn against scripted (status, body) responses per path,
    without real HTTP. `activity` is consumed one entry per poll; `history` is
    the /v1/chat/history body returned for reply_for_turn."""

    def __init__(self, activity, history):
        self.base_url = "https://stub"
        self._activity = list(activity)
        self._history = history

    def _req(self, method, path, *, api_key=None, body=None, attempts=5, read_timeout=45):
        if path.startswith("/v1/chat/turn-activity/"):
            return self._activity.pop(0) if self._activity else (404, {})
        if path.startswith("/v1/chat/history"):
            return 200, self._history
        raise AssertionError(f"unexpected path {path}")


def _sess():
    return _client.Session(user_id="u", api_key="k", sk=b"\x00" * 32, pk=b"\x00" * 32)


def _no_sleep(monkeypatch):
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)


def test_poll_turn_returns_plaintext_reply_bound_to_turn_v2(monkeypatch):
    _no_sleep(monkeypatch)
    hist = {"messages": [
        {"role": "openclaw", "reply_to_message_id": "OTHER", "body": "not mine"},
        {"role": "openclaw", "reply_to_message_id": "T1", "body": "the answer"},
    ]}
    c = _StubClient(activity=[(200, {"complete": True})], history=hist)
    out = c.poll_turn(_sess(), "T1", timeout=5)
    assert out == {"settled": True, "failed": False, "failure": None, "reply": "the answer"}


def test_poll_turn_decrypts_encrypted_reply_bound_to_turn_v1(monkeypatch):
    _no_sleep(monkeypatch)
    monkeypatch.setattr(_client.crypto, "decrypt_reply", lambda m, sk, pk: "DECRYPTED")
    hist = {"messages": [
        {"role": "openclaw", "reply_to_message_id": "T1", "body_ct": "cipher", "id": "r1"},
    ]}
    c = _StubClient(activity=[(200, {"complete": True})], history=hist)
    out = c.poll_turn(_sess(), "T1", timeout=5)
    assert out["failed"] is False and out["reply"] == "DECRYPTED"


def test_poll_turn_failure_is_authoritative_even_with_fallback_reply(monkeypatch):
    _no_sleep(monkeypatch)
    # complete=True AND a bound reply exists, but backend reports a failure ->
    # must FAIL (nonempty fallback text must not be read as success). T532/T540.
    hist = {"messages": [
        {"role": "openclaw", "reply_to_message_id": "T1", "body": "sorry, upstream error"},
    ]}
    c = _StubClient(activity=[(200, {"complete": True, "failure": {"code": "provider_error"}})],
                    history=hist)
    out = c.poll_turn(_sess(), "T1", timeout=5)
    assert out == {"settled": True, "failed": True, "failure": "provider_error", "reply": None}


def test_poll_turn_fails_closed_when_verdict_never_settles(monkeypatch):
    _no_sleep(monkeypatch)
    # turn-activity keeps 404 (never registered) -> not a pass; fail closed.
    c = _StubClient(activity=[], history={"messages": []})
    out = c.poll_turn(_sess(), "T1", timeout=0.01)
    assert out["settled"] is False and out["failed"] is True
    assert "verdict_unsettled_timeout" in out["failure"] and out["reply"] is None


def test_poll_turn_complete_without_bound_reply_fails_closed(monkeypatch):
    _no_sleep(monkeypatch)
    # settled OK but no reply bound to this turn (an unrelated row only) -> fail.
    hist = {"messages": [{"role": "openclaw", "reply_to_message_id": "OTHER", "body": "x"}]}
    c = _StubClient(activity=[(200, {"complete": True})], history=hist)
    out = c.poll_turn(_sess(), "T1", timeout=5)
    assert out["failed"] is True and out["failure"] == "complete_without_bound_reply"
