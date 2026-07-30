"""Stage D (slice 2): the enclave accepts a runtime token for /v1/envelope/decrypt.

The enclave doesn't verify credentials itself — it resolves the caller by
forwarding their credential to the backend's /v1/users/whoami (which became
runtime-token-aware in slice 1). So a hosted consumer/supervisor can unwrap an
envelope with a runtime token instead of the user's long-term API key: the
enclave just forwards whatever credential the caller presented.
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from asgi_test_client import _AsgiTestClient  # noqa: E402
from enclave import auth  # noqa: E402
from enclave import backend_client, config, envelope as envmod, keys  # noqa: E402
from enclave import state as enclave_state  # noqa: E402
from enclave.routes import build_app  # noqa: E402


@pytest.fixture()
def decrypt_client(monkeypatch):
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    auth.reset_cache()

    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda _env, uid, _sk: f"plain:{uid}".encode())

    forwarded: list[dict] = []

    async def fake_backend_get(path, headers, params=None):
        forwarded.append({"path": path, "headers": dict(headers or {})})
        return {"user_id": "usr_from_whoami"}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)

    client = _AsgiTestClient(build_app())
    yield client, forwarded


_ENV = {"v": 1, "id": "x", "owner_user_id": "usr_from_whoami", "body_ct": "Zm9v"}


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


def test_decrypt_verifies_runtime_token_locally_without_whoami(decrypt_client, monkeypatch):
    """B: with the shared secret set, /v1/envelope/decrypt resolves a runtime-token
    caller locally (HMAC) and skips the live whoami round-trip. The api_key path is
    unaffected — it still resolves live (test_decrypt_still_forwards_api_key…)."""
    client, forwarded = decrypt_client
    secret, tok = _mint_live_token(user_id="usr_from_whoami")  # must match envelope owner
    monkeypatch.setattr(config, "RUNTIME_TOKEN_SECRET", secret)
    res = client.post(
        "/v1/envelope/decrypt",
        json={"envelope": _ENV, "purpose": "model_api_provider_key"},
        headers={"X-Feedling-Runtime-Token": tok},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    assert res.get_json()["owner_user_id"] == "usr_from_whoami"
    assert [c for c in forwarded if c["path"] == "/v1/users/whoami"] == []


def test_decrypt_falls_back_to_live_whoami_on_bad_token(decrypt_client, monkeypatch):
    """B: an invalid/expired runtime token falls back to the live whoami round-trip
    (never harder-fails than today)."""
    client, forwarded = decrypt_client
    secret, tok = _mint_live_token(user_id="usr_from_whoami", ttl=-1.0)  # expired
    monkeypatch.setattr(config, "RUNTIME_TOKEN_SECRET", secret)
    res = client.post(
        "/v1/envelope/decrypt",
        json={"envelope": _ENV},
        headers={"X-Feedling-Runtime-Token": tok},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    assert [c for c in forwarded if c["path"] == "/v1/users/whoami"], "expected live fallback"


# ---- slice 2b: the cached resolver (decrypt-and-serve routes) honors tokens ----


def test_whoami_cached_resolves_via_runtime_token(monkeypatch):
    calls: list[tuple] = []

    async def fake_backend_get(path, headers, params=None):
        calls.append(("headers", path, dict(headers or {})))
        return {"user_id": "u_tok"}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    auth.reset_cache()
    ctx = auth.AuthContext(api_key="", runtime_token="tok.sig")
    out = asyncio.run(auth.whoami_cached(ctx))
    assert out["user_id"] == "u_tok"
    assert calls[-1] == ("headers", "/v1/users/whoami", {"X-Feedling-Runtime-Token": "tok.sig"})


def test_whoami_cache_prunes_expired_entries():
    # Rotating runtime tokens each create a new cache key; without eviction the
    # dict would grow unbounded in the long-lived enclave. Pruning drops entries
    # past the TTL so the cache stays bounded to the live-credential set.
    auth.reset_cache()
    now = 1000.0
    ttl = auth.WHOAMI_CACHE_TTL
    auth._whoami_cache["stale1"] = (now - ttl - 1, {"user_id": "a"})
    auth._whoami_cache["stale2"] = (now - ttl - 100, {"user_id": "b"})
    auth._whoami_cache["fresh"] = (now - 1, {"user_id": "c"})
    auth._prune_whoami_cache(now)
    assert set(auth._whoami_cache) == {"fresh"}


def test_forward_headers_prefers_runtime_token_over_api_key():
    # The decrypt-and-serve routes fetch the user's encrypted data from the
    # backend via ctx.forward_headers; under a runtime token (api_key absent) it
    # must forward the token instead, else those routes 401 at the backend.
    ctx = auth.AuthContext(api_key="", runtime_token="tok.sig")
    assert ctx.forward_headers == {"X-Feedling-Runtime-Token": "tok.sig"}
    # api-key path unchanged
    ctx = auth.AuthContext(api_key="k-legacy", runtime_token="")
    assert ctx.forward_headers == {"X-API-Key": "k-legacy"}


def test_chat_history_with_runtime_token_reaches_decrypt_not_401(monkeypatch):
    # P1 (Codex review): the chat/history guard used to 401 on a missing api_key
    # before _whoami_cached, wedging a token-only hosted consumer. With a token it
    # must pass the guard and serve.
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    auth.reset_cache()

    async def fake_backend_get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": "u_tok"}
        return {"messages": [], "total": 0}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)

    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)

    c = _AsgiTestClient(build_app())
    res = c.get("/v1/chat/history", headers={"X-Feedling-Runtime-Token": "tok.sig"})
    assert res.status_code == 200, res.get_data(as_text=True)
    assert res.get_json().get("messages") == []


# ---- local runtime-token verification (skip the backend whoami round-trip) ----
#
# The enclave shares FEEDLING_RUNTIME_TOKEN_SECRET with the backend + supervisor
# (same TDX domain). A runtime token is a self-contained HMAC token carrying the
# user_id claim, so when the secret is configured the enclave can verify it
# LOCALLY and skip the /v1/users/whoami reentrant round-trip that otherwise
# serializes the read-only decrypt-and-serve routes under cold-cache load.


def _mint_live_token(*, user_id="u_local", ttl=900.0):
    from core import runtime_token as rt
    secret = b"enclave-shared-secret"
    return secret, rt.mint(secret, user_id=user_id, runtime_instance_id="ri-1",
                           scope=["decrypt"], ttl=ttl)


def test_whoami_cached_verifies_runtime_token_locally_without_backend(monkeypatch):
    """Secret configured + valid token → resolve user_id from claims, NO round-trip."""
    secret, tok = _mint_live_token(user_id="u_local")
    monkeypatch.setattr(config, "RUNTIME_TOKEN_SECRET", secret)
    called: list[str] = []

    async def fake_backend_get(path, headers, params=None):
        called.append(path)
        return {"user_id": "u_backend"}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    auth.reset_cache()
    ctx = auth.AuthContext(api_key="", runtime_token=tok)
    out = asyncio.run(auth.whoami_cached(ctx))
    assert out["user_id"] == "u_local"
    assert called == [], "local verify must not round-trip to the backend"


def test_whoami_cached_falls_back_to_backend_on_expired_token(monkeypatch):
    """A token that fails local verify (expired/bad-sig) falls back to the backend
    whoami — never harder-fails than today's round-trip."""
    secret, tok = _mint_live_token(user_id="u_local", ttl=-1.0)  # already expired
    monkeypatch.setattr(config, "RUNTIME_TOKEN_SECRET", secret)
    called: list[str] = []

    async def fake_backend_get(path, headers, params=None):
        called.append(path)
        return {"user_id": "u_backend"}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    auth.reset_cache()
    ctx = auth.AuthContext(api_key="", runtime_token=tok)
    out = asyncio.run(auth.whoami_cached(ctx))
    assert out["user_id"] == "u_backend"
    assert called == ["/v1/users/whoami"]


def test_whoami_cached_falls_back_when_no_secret_configured(monkeypatch):
    """No shared secret → cannot verify locally → round-trip (unchanged behavior)."""
    _secret, tok = _mint_live_token(user_id="u_local")
    monkeypatch.setattr(config, "RUNTIME_TOKEN_SECRET", b"")
    called: list[str] = []

    async def fake_backend_get(path, headers, params=None):
        called.append(path)
        return {"user_id": "u_backend"}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    auth.reset_cache()
    ctx = auth.AuthContext(api_key="", runtime_token=tok)
    out = asyncio.run(auth.whoami_cached(ctx))
    assert out["user_id"] == "u_backend"
    assert called == ["/v1/users/whoami"]


def test_whoami_cached_api_key_path_unchanged(monkeypatch):
    calls: list[tuple] = []

    async def fake_backend_get(path, headers, params=None):
        if "X-Feedling-Runtime-Token" in (headers or {}):
            calls.append(("headers",))
            return {"user_id": "u_tok"}
        calls.append(("apikey", (headers or {}).get("X-API-Key")))
        return {"user_id": "u_key"}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    auth.reset_cache()
    ctx = auth.AuthContext(api_key="k-legacy", runtime_token="")  # no token
    out = asyncio.run(auth.whoami_cached(ctx))
    assert out["user_id"] == "u_key"
    assert calls[-1] == ("apikey", "k-legacy")  # still the api-key forwarding path
