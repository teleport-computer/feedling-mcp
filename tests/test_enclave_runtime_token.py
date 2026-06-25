"""Stage D (slice 2): the enclave accepts a runtime token for /v1/envelope/decrypt.

The enclave doesn't verify credentials itself — it resolves the caller by
forwarding their credential to the backend's /v1/users/whoami (which became
runtime-token-aware in slice 1). So a hosted consumer/supervisor can unwrap an
envelope with a runtime token instead of the user's long-term API key: the
enclave just forwards whatever credential the caller presented.
"""

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import enclave_app  # noqa: E402


@pytest.fixture()
def decrypt_client(monkeypatch):
    prev_ready, prev_err = enclave_app._state.get("ready"), enclave_app._state.get("error")
    enclave_app._state["ready"] = True
    enclave_app._state["error"] = None
    monkeypatch.setattr(enclave_app, "_get_or_derive_content_sk", lambda: object())
    monkeypatch.setattr(enclave_app, "_decrypt_envelope", lambda _env, uid, _sk: f"plain:{uid}".encode())

    forwarded: list[dict] = []

    def fake_flask_get_headers(path, headers, params=None):
        forwarded.append({"path": path, "headers": dict(headers)})
        return {"user_id": "usr_from_whoami"}

    def fake_flask_get(path, api_key, params=None):
        # The api-key path stays on _flask_get; normalize to the same header shape.
        forwarded.append({"path": path, "headers": {"X-API-Key": api_key} if api_key else {}})
        return {"user_id": "usr_from_whoami"}

    monkeypatch.setattr(enclave_app, "_flask_get_headers", fake_flask_get_headers, raising=False)
    monkeypatch.setattr(enclave_app, "_flask_get", fake_flask_get)
    enclave_app.app.config.update(TESTING=True)
    with enclave_app.app.test_client() as client:
        yield client, forwarded
    enclave_app._state["ready"] = prev_ready
    enclave_app._state["error"] = prev_err


_ENV = {"v": 1, "id": "x", "owner_user_id": "usr_from_whoami", "ct": "Zm9v"}


def test_decrypt_forwards_runtime_token_to_whoami(decrypt_client):
    client, forwarded = decrypt_client
    res = client.post(
        "/v1/envelope/decrypt",
        json={"envelope": _ENV, "purpose": "model_api_provider_key"},
        headers={"X-Feedling-Runtime-Token": "tok.sig"},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["owner_user_id"] == "usr_from_whoami"
    assert base64.b64decode(body["plaintext_b64"]) == b"plain:usr_from_whoami"
    # the whoami round-trip carried the runtime token, NOT an api key
    whoami_calls = [c for c in forwarded if c["path"] == "/v1/users/whoami"]
    assert whoami_calls, "expected a whoami resolution"
    hdrs = whoami_calls[-1]["headers"]
    assert hdrs.get("X-Feedling-Runtime-Token") == "tok.sig"
    assert "X-API-Key" not in hdrs


def test_decrypt_still_forwards_api_key_when_no_token(decrypt_client):
    client, forwarded = decrypt_client
    res = client.post(
        "/v1/envelope/decrypt",
        json={"envelope": _ENV},
        headers={"X-API-Key": "k-legacy"},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    hdrs = [c for c in forwarded if c["path"] == "/v1/users/whoami"][-1]["headers"]
    assert hdrs.get("X-API-Key") == "k-legacy"
    assert "X-Feedling-Runtime-Token" not in hdrs


def test_decrypt_rejects_when_no_credential(decrypt_client):
    client, _ = decrypt_client
    res = client.post("/v1/envelope/decrypt", json={"envelope": _ENV})
    assert res.status_code == 401


# ---- slice 2b: the cached resolver (decrypt-and-serve routes) honors tokens ----


def test_whoami_cached_resolves_via_runtime_token(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(enclave_app, "_flask_get_headers",
                        lambda p, h, params=None: (calls.append(("headers", p, h)), {"user_id": "u_tok"})[1])
    monkeypatch.setattr(enclave_app, "_flask_get",
                        lambda p, k, params=None: (calls.append(("apikey", p, k)), {"user_id": "u_key"})[1])
    enclave_app._whoami_cache.clear()
    with enclave_app.app.test_request_context(headers={"X-Feedling-Runtime-Token": "tok.sig"}):
        out = enclave_app._whoami_cached("")  # api_key empty; token present
    assert out["user_id"] == "u_tok"
    assert calls[-1] == ("headers", "/v1/users/whoami", {"X-Feedling-Runtime-Token": "tok.sig"})


def test_whoami_cache_prunes_expired_entries():
    # Rotating runtime tokens each create a new cache key; without eviction the
    # dict would grow unbounded in the long-lived enclave. Pruning drops entries
    # past the TTL so the cache stays bounded to the live-credential set.
    enclave_app._whoami_cache.clear()
    now = 1000.0
    ttl = enclave_app._WHOAMI_CACHE_TTL
    enclave_app._whoami_cache["stale1"] = (now - ttl - 1, {"user_id": "a"})
    enclave_app._whoami_cache["stale2"] = (now - ttl - 100, {"user_id": "b"})
    enclave_app._whoami_cache["fresh"] = (now - 1, {"user_id": "c"})
    enclave_app._prune_whoami_cache(now)
    assert set(enclave_app._whoami_cache) == {"fresh"}


def test_flask_get_forwards_runtime_token(monkeypatch):
    # The decrypt-and-serve routes fetch the user's encrypted data from the
    # backend via _flask_get(api_key); under a runtime token (api_key absent) it
    # must forward the token instead, else those routes 401 at the backend.
    captured = {}
    monkeypatch.setattr(enclave_app, "_flask_get_headers",
                        lambda p, h, params=None: (captured.update(headers=h), {"ok": True})[1])
    with enclave_app.app.test_request_context(headers={"X-Feedling-Runtime-Token": "tok.sig"}):
        enclave_app._flask_get("/v1/chat/history", "")  # api_key empty, token present
    assert captured["headers"] == {"X-Feedling-Runtime-Token": "tok.sig"}
    # api-key path unchanged
    with enclave_app.app.test_request_context():
        enclave_app._flask_get("/v1/chat/history", "k-legacy")
    assert captured["headers"] == {"X-API-Key": "k-legacy"}


def test_chat_history_with_runtime_token_reaches_decrypt_not_401(monkeypatch):
    # P1 (Codex review): the chat/history guard used to 401 on a missing api_key
    # before _whoami_cached, wedging a token-only hosted consumer. With a token it
    # must pass the guard and serve.
    prev_ready, prev_err = enclave_app._state.get("ready"), enclave_app._state.get("error")
    enclave_app._state["ready"] = True
    enclave_app._state["error"] = None
    monkeypatch.setattr(enclave_app, "_whoami_cached", lambda k: {"user_id": "u_tok"})
    monkeypatch.setattr(enclave_app, "_flask_get", lambda p, k, params=None: {"messages": []})
    monkeypatch.setattr(enclave_app, "_get_or_derive_content_sk", lambda: object())
    enclave_app.app.config.update(TESTING=True)
    with enclave_app.app.test_client() as c:
        res = c.get("/v1/chat/history", headers={"X-Feedling-Runtime-Token": "tok.sig"})
    enclave_app._state["ready"], enclave_app._state["error"] = prev_ready, prev_err
    assert res.status_code == 200, res.get_data(as_text=True)
    assert res.get_json().get("messages") == []


def test_whoami_cached_api_key_path_unchanged(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(enclave_app, "_flask_get_headers",
                        lambda p, h, params=None: (calls.append(("headers",)), {"user_id": "u_tok"})[1])
    monkeypatch.setattr(enclave_app, "_flask_get",
                        lambda p, k, params=None: (calls.append(("apikey", k)), {"user_id": "u_key"})[1])
    enclave_app._whoami_cache.clear()
    with enclave_app.app.test_request_context():  # no token header
        out = enclave_app._whoami_cached("k-legacy")
    assert out["user_id"] == "u_key"
    assert calls[-1] == ("apikey", "k-legacy")  # still the _flask_get api-key path
