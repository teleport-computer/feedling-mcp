"""ASGI route surface for user MCP server config (Task 3).

Drives ``hosted.mcp_routes_asgi`` end-to-end through the assembled ASGI app
(``asgi_test_client.make_client``), mirroring ``tests/test_diagnostics_routes.py``'s
client/_register fixture pattern. Envelope construction and enclave decryption
are both monkeypatched (as in ``tests/test_user_mcp_core.py``) so these tests
never touch a real enclave or the network.
"""

from __future__ import annotations

import base64
import itertools
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import runtime_token  # noqa: E402
from core import store as core_store  # noqa: E402

_RUNTIME_SECRET = "test-runtime-secret"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _mint(user_id: str, *, scope) -> str:
    """Mint a supervisor-style runtime token with the given scope list."""
    return runtime_token.mint(
        _RUNTIME_SECRET.encode("utf-8"),
        user_id=user_id,
        runtime_instance_id="ri_test",
        scope=scope,
        ttl=900.0,
    )


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    with make_client() as c:
        yield c


_pk_counter = itertools.count(1)


def _register(client) -> tuple[str, str]:
    raw = next(_pk_counter).to_bytes(32, "big")
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(raw), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _fake_envelope(monkeypatch):
    from core import envelope as core_envelope
    from hosted import mcp_probe as _probe

    monkeypatch.setattr(
        core_envelope, "_build_shared_envelope_for_store",
        lambda store, raw, item_id=None: ({"v": 1, "id": item_id, "body_ct": raw.hex()}, ""),
    )
    # SSRF DNS resolve is environment-dependent; stub the upsert-time guard so
    # these route tests only exercise mcp_routes_asgi wiring.
    monkeypatch.setattr(_probe, "blocked_url_kind", lambda url: None)


def test_list_requires_auth(client):
    r = client.get("/v1/mcp/servers")
    assert r.status_code == 401


def test_crud_roundtrip(client, monkeypatch):
    _fake_envelope(monkeypatch)
    _, key = _register(client)
    h = {"X-API-Key": key}

    r = client.post("/v1/mcp/servers", headers=h, json={
        "name": "jira", "url": "https://mcp.example.com/mcp",
        "headers": {"Authorization": "Bearer tok"}})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["name"] == "jira"
    assert r.get_json()["resident"] is False

    r = client.get("/v1/mcp/servers", headers=h)
    assert r.status_code == 200
    servers = r.get_json()["servers"]
    assert len(servers) == 1
    assert servers[0]["url_hint"] == "mcp.example.com"
    assert "config_envelope" not in servers[0]
    assert servers[0]["resident"] is False

    r = client.open("/v1/mcp/servers/jira", method="PATCH", headers=h,
                     json={"enabled": False, "resident": True})
    assert r.status_code == 200
    assert r.get_json()["enabled"] is False
    assert r.get_json()["resident"] is True

    r = client.get("/v1/mcp/envelopes", headers=h)
    assert r.status_code == 200
    body = r.get_json()
    assert body["fingerprint"].startswith("sha256:")
    assert len(body["servers"]) == 1
    assert body["servers"][0]["config_envelope"]
    assert body["servers"][0]["enabled"] is False
    assert body["servers"][0]["resident"] is True

    r = client.delete("/v1/mcp/servers/jira", headers=h)
    assert r.status_code == 200
    assert r.get_json() == {"deleted": "jira"}
    assert client.get("/v1/mcp/servers", headers=h).get_json() == {"servers": []}


def test_list_serializes_optional_runtime_summary(client, monkeypatch):
    _fake_envelope(monkeypatch)
    user_id, key = _register(client)
    h = {"X-API-Key": key}
    created = client.post("/v1/mcp/servers", headers=h, json={
        "name": "jira",
        "url": "https://mcp.example.com/mcp",
        "headers": {},
    })
    assert created.status_code == 200

    from hosted import mcp_status

    monkeypatch.setattr(
        mcp_status,
        "runtime_summaries_for_store",
        lambda store: ({
            "jira": {
                "last_kind": "transport_failure",
                "last_at": 1786370000.0,
                "recent_ok": 0,
                "recent_total": 10,
            },
        } if store.user_id == user_id else {}),
    )

    listed = client.get("/v1/mcp/servers", headers=h)

    assert listed.status_code == 200
    assert listed.get_json()["servers"][0]["runtime"] == {
        "last_kind": "transport_failure",
        "last_at": 1786370000.0,
        "recent_ok": 0,
        "recent_total": 10,
    }


@pytest.mark.parametrize(
    ("payload", "kind"),
    [
        ({"name": "jira", "url": "https://mcp.example.com", "enabled": "false"},
         "invalid_enabled"),
        ({"name": "jira", "url": "https://mcp.example.com", "resident": "yes"},
         "invalid_resident"),
        ({"name": "jira", "url": "https://mcp.example.com", "unknown": True},
         "invalid_request"),
        ({"name": 123, "url": "https://mcp.example.com"}, "invalid_name"),
        ({"name": "jira", "url": 123}, "invalid_url"),
        ({"name": "jira", "url": "https://mcp.example.com", "headers": {"X": 1}},
         "invalid_headers"),
        ({"name": "jira", "url": "https://mcp.example.com", "ca_pem": 1},
         "invalid_ca"),
        ({"name": "jira", "url": "https://mcp.example.com",
          "read_only_tool_fingerprints": None},
         "invalid_read_only_tool_fingerprints"),
        (["name", "url"], "invalid_request"),
        ([], "invalid_request"),
        (False, "invalid_request"),
        (None, "invalid_request"),
    ],
)
def test_upsert_rejects_bodies_outside_openapi_schema(
    client, monkeypatch, payload, kind,
):
    _fake_envelope(monkeypatch)
    _, key = _register(client)

    response = client.post(
        "/v1/mcp/servers", headers={"X-API-Key": key}, json=payload)

    assert response.status_code == 400
    assert response.get_json()["error"]["kind"] == kind
    listed = client.get(
        "/v1/mcp/servers", headers={"X-API-Key": key}).get_json()
    assert listed == {"servers": []}


@pytest.mark.parametrize(
    ("payload", "kind"),
    [
        ({"enabled": "false"}, "invalid_enabled"),
        ({"enabled": 1}, "invalid_enabled"),
        ({"resident": "yes"}, "invalid_resident"),
        ({"resident": 1}, "invalid_resident"),
        ({"enabled": False, "unknown": True}, "invalid_patch"),
        (["enabled"], "invalid_patch"),
        ([], "invalid_patch"),
        (False, "invalid_patch"),
        (None, "invalid_patch"),
        ({}, "invalid_patch"),
    ],
)
def test_patch_rejects_bodies_outside_openapi_schema_without_mutating(
    client, monkeypatch, payload, kind,
):
    _fake_envelope(monkeypatch)
    _, key = _register(client)
    headers = {"X-API-Key": key}
    created = client.post("/v1/mcp/servers", headers=headers, json={
        "name": "jira", "url": "https://mcp.example.com", "headers": {},
    })
    assert created.status_code == 200

    response = client.open(
        "/v1/mcp/servers/jira",
        method="PATCH",
        headers=headers,
        json=payload,
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["kind"] == kind
    listed = client.get("/v1/mcp/servers", headers=headers).get_json()
    assert listed["servers"][0]["enabled"] is True


def test_patch_read_only_approvals_without_resending_secrets(
    client, monkeypatch,
):
    _fake_envelope(monkeypatch)
    from core import enclave as core_enclave

    monkeypatch.setattr(
        core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda envelope, key, *, purpose, runtime_token="": bytes.fromhex(
            envelope["body_ct"]
        ),
    )
    _, key = _register(client)
    h = {"X-API-Key": key}
    created = client.post("/v1/mcp/servers", headers=h, json={
        "name": "jira",
        "url": "https://mcp.example.com/mcp",
        "headers": {"Authorization": "Bearer secret"},
    })
    assert created.status_code == 200

    response = client.open(
        "/v1/mcp/servers/jira",
        method="PATCH",
        headers=h,
        json={"read_only_tool_fingerprints": {"search": "a" * 64}},
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["enabled"] is True
    # The management view remains secret-free after the approval-only patch.
    listed = client.get("/v1/mcp/servers", headers=h).get_json()
    assert "secret" not in json.dumps(listed)


def test_patch_rejects_invalid_read_only_fingerprint(client, monkeypatch):
    _fake_envelope(monkeypatch)
    _, key = _register(client)
    h = {"X-API-Key": key}
    client.post("/v1/mcp/servers", headers=h, json={
        "name": "jira", "url": "https://mcp.example.com", "headers": {},
    })

    response = client.open(
        "/v1/mcp/servers/jira",
        method="PATCH",
        headers=h,
        json={"read_only_tool_fingerprints": {"search": "not-a-sha256"}},
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["kind"] == (
        "invalid_read_only_tool_fingerprint")


def test_patch_unknown_server_404(client, monkeypatch):
    _fake_envelope(monkeypatch)
    _, key = _register(client)
    h = {"X-API-Key": key}
    r = client.open("/v1/mcp/servers/nope", method="PATCH", headers=h,
                     json={"enabled": False})
    assert r.status_code == 404


def test_delete_unknown_server_404(client, monkeypatch):
    _fake_envelope(monkeypatch)
    _, key = _register(client)
    h = {"X-API-Key": key}
    r = client.delete("/v1/mcp/servers/nope", headers=h)
    assert r.status_code == 404


def test_envelopes_requires_auth(client):
    r = client.get("/v1/mcp/envelopes")
    assert r.status_code == 401


def test_test_endpoint_requires_auth(client):
    r = client.post("/v1/mcp/servers/jira/test")
    assert r.status_code == 401


def test_test_endpoint_decrypts_and_probes(client, monkeypatch):
    _fake_envelope(monkeypatch)
    from core import enclave as core_enclave
    from hosted import mcp_probe

    monkeypatch.setattr(
        core_enclave, "_decrypt_envelope_via_enclave",
        lambda env, key, *, purpose, runtime_token="": json.dumps(
            {"url": "https://mcp.example.com/mcp", "headers": {}}).encode())
    monkeypatch.setattr(
        mcp_probe, "probe",
        lambda url, headers, *, ca_pem=None, transport=None, transport_hint="": {
            "ok": True,
            "tool_count": 1,
            "tool_names": ["search"],
            "read_only_tool_fingerprints": {"search": "a" * 64},
            "transport": "http",
        })
    _, key = _register(client)
    h = {"X-API-Key": key}
    client.post("/v1/mcp/servers", headers=h, json={
        "name": "jira", "url": "https://mcp.example.com/mcp", "headers": {}})
    r = client.post("/v1/mcp/servers/jira/test", headers=h)
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["ok"] is True
    assert body["tool_count"] == 1
    assert body["tool_names"] == ["search"]
    assert body["read_only_tool_fingerprints"] == {"search": "a" * 64}


def test_test_endpoint_probe_error_returns_400(client, monkeypatch):
    _fake_envelope(monkeypatch)
    from core import enclave as core_enclave
    from hosted import mcp_probe

    monkeypatch.setattr(
        core_enclave, "_decrypt_envelope_via_enclave",
        lambda env, key, *, purpose, runtime_token="": json.dumps(
            {"url": "https://mcp.example.com/mcp", "headers": {}}).encode())

    def _raise(url, headers, *, ca_pem=None, transport=None, transport_hint=""):
        raise mcp_probe.ProbeError("timeout", "connect timeout")

    monkeypatch.setattr(mcp_probe, "probe", _raise)
    _, key = _register(client)
    h = {"X-API-Key": key}
    client.post("/v1/mcp/servers", headers=h, json={
        "name": "jira", "url": "https://mcp.example.com/mcp", "headers": {}})
    r = client.post("/v1/mcp/servers/jira/test", headers=h)
    assert r.status_code == 400
    body = r.get_json()
    assert body["error"]["kind"] == "timeout"


def test_test_endpoint_decrypt_failure_never_leaks_exception_detail(
    client, monkeypatch,
):
    _fake_envelope(monkeypatch)
    from core import enclave as core_enclave

    _, key = _register(client)
    headers = {"X-API-Key": key}
    created = client.post("/v1/mcp/servers", headers=headers, json={
        "name": "jira", "url": "https://mcp.example.com", "headers": {},
    })
    assert created.status_code == 200
    monkeypatch.setattr(
        core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("DO_NOT_LEAK_THIS_SECRET")),
    )

    response = client.post("/v1/mcp/servers/jira/test", headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {
        "error": {"kind": "decrypt_failed", "detail": ""}}
    assert "DO_NOT_LEAK_THIS_SECRET" not in response.get_data(as_text=True)


@pytest.mark.parametrize("plaintext", [
    b"[]",
    b"{}",
    b'{"url":""}',
    b'{"url":"file:///tmp/mcp"}',
    b'{"url":"https://mcp.example.com","headers":[]}',
    b'{"url":"https://mcp.example.com","headers":{"X-Test":1}}',
])
def test_test_endpoint_corrupt_decrypted_config_is_stable_decrypt_failure(
    client, monkeypatch, plaintext,
):
    _fake_envelope(monkeypatch)
    from core import enclave as core_enclave

    _, key = _register(client)
    headers = {"X-API-Key": key}
    created = client.post("/v1/mcp/servers", headers=headers, json={
        "name": "jira", "url": "https://mcp.example.com", "headers": {},
    })
    assert created.status_code == 200
    monkeypatch.setattr(
        core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *args, **kwargs: plaintext,
    )

    response = client.post("/v1/mcp/servers/jira/test", headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {
        "error": {"kind": "decrypt_failed", "detail": ""}}


def test_test_endpoint_unknown_server_404(client, monkeypatch):
    _fake_envelope(monkeypatch)
    _, key = _register(client)
    h = {"X-API-Key": key}
    r = client.post("/v1/mcp/servers/nope/test", headers=h)
    assert r.status_code == 404


# ---- auth model: management endpoints are api-key only; envelopes needs the
#      envelope_decrypt scope (Codex review fixes 1 & 2) ----

# Mirrors the scope the supervisor actually mints for a hosted consumer
# (supervisor.py). Notably it INCLUDES envelope_decrypt.
_CONSUMER_SCOPE = ["chat", "memory", "identity", "perception", "envelope_decrypt"]


def test_management_endpoint_rejects_runtime_token(client, monkeypatch):
    # A hosted consumer (runtime token) must NOT be able to mutate config, even
    # with the full consumer scope set — management is the iOS control plane.
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _RUNTIME_SECRET)
    _fake_envelope(monkeypatch)
    user_id, _ = _register(client)
    tok = _mint(user_id, scope=_CONSUMER_SCOPE)
    r = client.post(
        "/v1/mcp/servers",
        headers={"X-Feedling-Runtime-Token": tok},
        json={"name": "jira", "url": "https://mcp.example.com/mcp", "headers": {}},
    )
    assert r.status_code == 403, r.get_data(as_text=True)


def test_management_endpoint_allows_api_key(client, monkeypatch):
    # Same endpoint with the long-term api key still works (200).
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _RUNTIME_SECRET)
    _fake_envelope(monkeypatch)
    _, key = _register(client)
    r = client.post(
        "/v1/mcp/servers",
        headers={"X-API-Key": key},
        json={"name": "jira", "url": "https://mcp.example.com/mcp", "headers": {}},
    )
    assert r.status_code == 200, r.get_data(as_text=True)


def test_envelopes_allows_runtime_token_with_envelope_decrypt_scope(client, monkeypatch):
    # The hosted consumer path: runtime token carrying envelope_decrypt → 200.
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _RUNTIME_SECRET)
    _fake_envelope(monkeypatch)
    user_id, _ = _register(client)
    tok = _mint(user_id, scope=_CONSUMER_SCOPE)
    r = client.get("/v1/mcp/envelopes", headers={"X-Feedling-Runtime-Token": tok})
    assert r.status_code == 200, r.get_data(as_text=True)


def test_envelopes_rejects_runtime_token_without_envelope_decrypt_scope(client, monkeypatch):
    # A narrower runtime token (no envelope_decrypt) must be forbidden.
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _RUNTIME_SECRET)
    _fake_envelope(monkeypatch)
    user_id, _ = _register(client)
    tok = _mint(user_id, scope=["chat", "memory"])
    r = client.get("/v1/mcp/envelopes", headers={"X-Feedling-Runtime-Token": tok})
    assert r.status_code == 403, r.get_data(as_text=True)


def test_envelopes_allows_api_key(client, monkeypatch):
    # api-key auth is full-access: the scope gate is a no-op → 200.
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _RUNTIME_SECRET)
    _fake_envelope(monkeypatch)
    _, key = _register(client)
    r = client.get("/v1/mcp/envelopes", headers={"X-API-Key": key})
    assert r.status_code == 200, r.get_data(as_text=True)
