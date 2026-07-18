"""User-configured remote HTTP MCP servers — storage/CRUD core (Task 1).

Exercises ``hosted.mcp_core`` directly against a real per-user store backed
by the test Postgres DB (see conftest.py). ``_build_shared_envelope_for_store``
depends on a reachable enclave for the real key material, so tests stub it
with a deterministic fake — this module only cares that mcp_core round-trips
whatever the envelope builder hands back and never leaks plaintext secrets
into the public (masked) view.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from core import store as core_store  # noqa: E402
from hosted import mcp_core  # noqa: E402

from _ca_helpers import self_signed_ca_pem  # noqa: E402


@pytest.fixture()
def store(backend_env):
    user = registry._register_user(public_key="A" * 43 + "=", archive_language="en")
    return core_store.get_store(user["user_id"])


def _fake_envelope(monkeypatch):
    # Envelope construction depends on the enclave being reachable; stub it
    # with a deterministic fake for these unit-level tests.
    from core import envelope as core_envelope
    monkeypatch.setattr(
        core_envelope, "_build_shared_envelope_for_store",
        lambda store, raw, item_id=None: ({"v": 1, "id": item_id, "ct": raw.hex()}, ""),
    )
    # No SSRF/DNS stub needed anymore: upsert_server no longer does a
    # network-facing pre-check (Task 2 removed the blocked_url_kind call —
    # http/private-IP URLs are now accepted at the storage layer; SSRF
    # defense, if any, belongs to mcp_probe's connect-time path, not here).


def test_list_empty(store):
    body, status = mcp_core.list_servers(store)
    assert status == 200
    assert body == {"servers": []}
    assert mcp_core.fingerprint_for_store(store) == ""


def test_upsert_and_list_masks_secrets(store, monkeypatch):
    _fake_envelope(monkeypatch)
    body, status = mcp_core.upsert_server(store, {
        "name": "jira",
        "url": "https://mcp.example.com/mcp",
        "headers": {"Authorization": "Bearer secret-token"},
    })
    assert status == 200, body
    body, _ = mcp_core.list_servers(store)
    (srv,) = body["servers"]
    assert srv["name"] == "jira"
    assert srv["url_hint"] == "mcp.example.com"
    assert srv["header_names"] == ["Authorization"]
    assert srv["enabled"] is True
    assert "secret-token" not in str(body)
    assert "config_envelope" not in srv


def test_malformed_url_returns_400_not_500(store, monkeypatch):
    # An unterminated IPv6 literal makes urlparse (or .hostname access) raise
    # ValueError; the endpoint must translate that to a clean 400/invalid_url,
    # never let the exception escape as a 500.
    _fake_envelope(monkeypatch)
    body, status = mcp_core.upsert_server(store, {
        "name": "x", "url": "https://[::1", "headers": {}})
    assert status == 400
    assert body["error"]["kind"] == "invalid_url"


def test_upsert_overwrites_same_name(store, monkeypatch):
    _fake_envelope(monkeypatch)
    first, _ = mcp_core.upsert_server(store, {
        "name": "jira", "url": "https://a.example.com", "headers": {"X-Old": "1"}})
    second, status = mcp_core.upsert_server(store, {
        "name": "jira", "url": "https://b.example.com", "headers": {"X-New": "2"}})
    assert status == 200
    # Same logical server: id and created_at survive the overwrite.
    assert second["id"] == first["id"]
    assert second["created_at"] == first["created_at"]
    assert second["url_hint"] == "b.example.com"
    assert second["header_names"] == ["X-New"]
    body, _ = mcp_core.list_servers(store)
    assert len(body["servers"]) == 1


def test_limits(store, monkeypatch):
    _fake_envelope(monkeypatch)
    for i in range(10):
        _, s = mcp_core.upsert_server(store, {
            "name": f"s{i}", "url": "https://a.example.com", "headers": {}})
        assert s == 200
    body, status = mcp_core.upsert_server(store, {
        "name": "s10", "url": "https://a.example.com", "headers": {}})
    assert status == 400 and body["error"]["kind"] == "too_many_servers"


def test_too_many_headers_rejected(store, monkeypatch):
    _fake_envelope(monkeypatch)
    headers = {f"X-H{i}": "v" for i in range(21)}
    body, status = mcp_core.upsert_server(store, {
        "name": "x", "url": "https://a.example.com", "headers": headers})
    assert status == 400
    assert body["error"]["kind"] == "too_many_headers"


def test_headers_too_large_rejected(store, monkeypatch):
    _fake_envelope(monkeypatch)
    body, status = mcp_core.upsert_server(store, {
        "name": "x", "url": "https://a.example.com",
        "headers": {"X-Big": "a" * 9000}})
    assert status == 400
    assert body["error"]["kind"] == "headers_too_large"


def test_forbidden_host_header_rejected(store, monkeypatch):
    _fake_envelope(monkeypatch)
    body, status = mcp_core.upsert_server(store, {
        "name": "x", "url": "https://a.example.com", "headers": {"Host": "evil.example.com"}})
    assert status == 400
    assert body["error"]["kind"] == "forbidden_header"

    body, status = mcp_core.upsert_server(store, {
        "name": "y", "url": "https://a.example.com", "headers": {"host": "evil.example.com"}})
    assert status == 400
    assert body["error"]["kind"] == "forbidden_header"


def test_bad_name_rejected(store, monkeypatch):
    _fake_envelope(monkeypatch)
    body, status = mcp_core.upsert_server(store, {
        "name": "Not Valid!", "url": "https://a.example.com", "headers": {}})
    assert status == 400
    assert body["error"]["kind"] == "invalid_name"


def test_patch_enabled_keeps_envelope(store, monkeypatch):
    _fake_envelope(monkeypatch)
    mcp_core.upsert_server(store, {"name": "jira", "url": "https://a.example.com", "headers": {}})
    before = mcp_core.envelopes_payload(store)[0]["servers"][0]["config_envelope"]
    fp_before = mcp_core.fingerprint_for_store(store)
    body, status = mcp_core.set_enabled(store, "jira", {"enabled": False})
    assert status == 200 and body["enabled"] is False
    after = mcp_core.envelopes_payload(store)[0]["servers"][0]
    assert after["config_envelope"] == before and after["enabled"] is False
    assert mcp_core.fingerprint_for_store(store) != fp_before


def test_patch_read_only_approvals_preserves_encrypted_connection_config(
    store, monkeypatch,
):
    _fake_envelope(monkeypatch)
    from core import enclave as core_enclave

    monkeypatch.setattr(
        core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda envelope, api_key, *, purpose, runtime_token="": bytes.fromhex(
            envelope["ct"]
        ),
    )
    mcp_core.upsert_server(store, {
        "name": "jira",
        "url": "https://mcp.example.com/rpc",
        "headers": {"Authorization": "Bearer secret"},
        "ca_pem": self_signed_ca_pem(),
    })
    before = mcp_core.envelopes_payload(store)[0]["servers"][0]["config_envelope"]
    fingerprint = "a" * 64

    body, status = mcp_core.set_enabled(
        store,
        "jira",
        {"read_only_tool_fingerprints": {"search": fingerprint}},
        "api-key",
    )

    assert status == 200, body
    assert body["enabled"] is True
    after = mcp_core.envelopes_payload(store)[0]["servers"][0]["config_envelope"]
    assert after != before
    secret = json.loads(bytes.fromhex(after["ct"]))
    assert secret["url"] == "https://mcp.example.com/rpc"
    assert secret["headers"] == {"Authorization": "Bearer secret"}
    assert "BEGIN CERTIFICATE" in secret["ca_pem"]
    assert secret["read_only_tool_fingerprints"] == {"search": fingerprint}


def test_patch_read_only_approvals_fails_without_changing_enabled(
    store, monkeypatch,
):
    _fake_envelope(monkeypatch)
    from core import enclave as core_enclave

    monkeypatch.setattr(
        core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("secret")),
    )
    mcp_core.upsert_server(
        store,
        {"name": "jira", "url": "https://mcp.example.com", "headers": {}},
    )

    body, status = mcp_core.set_enabled(
        store,
        "jira",
        {
            "enabled": False,
            "read_only_tool_fingerprints": {"search": "a" * 64},
        },
        "api-key",
    )

    assert status == 400
    assert body == {"error": {"kind": "decrypt_failed", "detail": ""}}
    listed, _ = mcp_core.list_servers(store)
    assert listed["servers"][0]["enabled"] is True


def test_patch_empty_read_only_approvals_revokes_existing_map(
    store, monkeypatch,
):
    _fake_envelope(monkeypatch)
    from core import enclave as core_enclave

    monkeypatch.setattr(
        core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda envelope, api_key, *, purpose, runtime_token="": bytes.fromhex(
            envelope["ct"]
        ),
    )
    mcp_core.upsert_server(store, {
        "name": "jira",
        "url": "https://mcp.example.com",
        "headers": {},
        "read_only_tool_fingerprints": {"search": "a" * 64},
    })

    _, status = mcp_core.set_enabled(
        store,
        "jira",
        {"read_only_tool_fingerprints": {}},
        "api-key",
    )

    assert status == 200
    envelope = mcp_core.envelopes_payload(store)[0]["servers"][0]["config_envelope"]
    secret = json.loads(bytes.fromhex(envelope["ct"]))
    assert "read_only_tool_fingerprints" not in secret


def test_delete_server(store, monkeypatch):
    _fake_envelope(monkeypatch)
    mcp_core.upsert_server(store, {"name": "jira", "url": "https://a.example.com", "headers": {}})
    body, status = mcp_core.delete_server(store, "jira")
    assert status == 200 and body == {"deleted": "jira"}
    body, _ = mcp_core.list_servers(store)
    assert body == {"servers": []}
    body, status = mcp_core.delete_server(store, "jira")
    assert status == 404
    assert body["error"]["kind"] == "not_found"


def test_fingerprint_changes_per_mutation(store, monkeypatch):
    _fake_envelope(monkeypatch)
    assert mcp_core.fingerprint_for_store(store) == ""
    mcp_core.upsert_server(store, {"name": "jira", "url": "https://a.example.com", "headers": {}})
    fp1 = mcp_core.fingerprint_for_store(store)
    assert fp1 != ""
    mcp_core.upsert_server(store, {"name": "confluence", "url": "https://b.example.com", "headers": {}})
    fp2 = mcp_core.fingerprint_for_store(store)
    assert fp2 != fp1
    mcp_core.delete_server(store, "confluence")
    fp3 = mcp_core.fingerprint_for_store(store)
    assert fp3 != fp2 and fp3 == fp1


def test_envelopes_payload_shape(store, monkeypatch):
    _fake_envelope(monkeypatch)
    mcp_core.upsert_server(store, {"name": "jira", "url": "https://a.example.com", "headers": {}})
    body, status = mcp_core.envelopes_payload(store)
    assert status == 200
    assert body["fingerprint"] == mcp_core.fingerprint_for_store(store)
    (srv,) = body["servers"]
    assert set(srv) == {"name", "enabled", "config_envelope"}


def _spy_wakes(store, monkeypatch):
    """Count the store-local + cross-worker wake calls _save fires."""
    calls = {"waiters": 0, "wake_bus": 0}
    monkeypatch.setattr(
        store, "notify_chat_waiters",
        lambda: calls.__setitem__("waiters", calls["waiters"] + 1))
    monkeypatch.setattr(
        mcp_core.wake_bus, "notify",
        lambda channel, uid: calls.__setitem__("wake_bus", calls["wake_bus"] + 1))
    return calls


def test_save_wakes_chat_poller_on_upsert(store, monkeypatch):
    _fake_envelope(monkeypatch)
    calls = _spy_wakes(store, monkeypatch)
    _, status = mcp_core.upsert_server(
        store, {"name": "jira", "url": "https://a.example.com", "headers": {}})
    assert status == 200
    assert calls == {"waiters": 1, "wake_bus": 1}


def test_save_wakes_chat_poller_on_set_enabled(store, monkeypatch):
    _fake_envelope(monkeypatch)
    mcp_core.upsert_server(store, {"name": "jira", "url": "https://a.example.com", "headers": {}})
    calls = _spy_wakes(store, monkeypatch)
    _, status = mcp_core.set_enabled(store, "jira", {"enabled": False})
    assert status == 200
    assert calls == {"waiters": 1, "wake_bus": 1}


def test_save_wakes_chat_poller_on_delete(store, monkeypatch):
    _fake_envelope(monkeypatch)
    mcp_core.upsert_server(store, {"name": "jira", "url": "https://a.example.com", "headers": {}})
    calls = _spy_wakes(store, monkeypatch)
    _, status = mcp_core.delete_server(store, "jira")
    assert status == 200
    assert calls == {"waiters": 1, "wake_bus": 1}


def test_http_url_is_accepted(store, monkeypatch):
    _fake_envelope(monkeypatch)
    body, status = mcp_core.upsert_server(
        store, {"name": "img", "url": "http://mcp.example.com/mcp"})
    assert status == 200, body
    assert body["name"] == "img"


def test_private_ip_url_is_accepted(store, monkeypatch):
    # 放开后私网地址必须能保存：agent 可能够得着（v2 spec §6 已判定 agent 路径
    # 不构成新增攻击面）。后端 probe 够不够得着是 probe 的事，不是存储的事。
    _fake_envelope(monkeypatch)
    body, status = mcp_core.upsert_server(
        store, {"name": "lan", "url": "http://192.168.1.5:8080/mcp"})
    assert status == 200, body


def test_bad_scheme_still_rejected(store, monkeypatch):
    _fake_envelope(monkeypatch)
    body, status = mcp_core.upsert_server(
        store, {"name": "x", "url": "ftp://mcp.example.com/mcp"})
    assert status == 400
    assert body["error"]["kind"] == "invalid_url"


def test_ca_pem_roundtrip_and_has_ca(store, monkeypatch):
    _fake_envelope(monkeypatch)
    ca = self_signed_ca_pem()
    body, status = mcp_core.upsert_server(
        store, {"name": "sec", "url": "https://mcp.example.com/mcp", "ca_pem": ca})
    assert status == 200, body
    assert body["has_ca"] is True
    # 明文视图永不含证书内容
    assert "ca_pem" not in body
    listed, _ = mcp_core.list_servers(store)
    assert listed["servers"][0]["has_ca"] is True


def test_no_ca_means_has_ca_false(store, monkeypatch):
    _fake_envelope(monkeypatch)
    body, _ = mcp_core.upsert_server(
        store, {"name": "plain", "url": "https://mcp.example.com/mcp"})
    assert body["has_ca"] is False


def test_legacy_record_without_has_ca_key_defaults_false(store):
    """v2 (2026-07-08) 时期写入的老记录没有 ``has_ca`` 键。``_public`` 用
    ``srv.get("has_ca")`` 透传会输出 ``None``，违反
    ``tools/public_openapi_contracts.py`` 声明的 ``{"type": "boolean"}``。
    prod 现存的记录在用户重新保存之前全部是这个形状 —— 必须强制转 bool，
    同时保留 ``.get()`` 的向后兼容意图（老记录没这个键不能 KeyError）。
    """
    import db
    legacy_record = {
        "id": "srv_legacy1",
        "name": "legacy",
        "enabled": True,
        "config_envelope": {"v": 1, "id": "x", "ct": "00"},
        "url_hint": "mcp.example.com",
        "header_names": [],
        # deliberately no "has_ca" key — pre-existing-field shape.
        "created_at": "2026-07-08T00:00:00Z",
        "updated_at": "2026-07-08T00:00:00Z",
    }
    db.set_blob(store.user_id, mcp_core.USER_MCP_BLOB, {
        "fingerprint": mcp_core.compute_fingerprint([legacy_record]),
        "servers": [legacy_record],
    })

    listed, status = mcp_core.list_servers(store)
    assert status == 200
    (srv,) = listed["servers"]
    assert srv["has_ca"] is False
    assert srv["has_ca"] is not None

    public = mcp_core._public(legacy_record)
    assert public["has_ca"] is False


def test_garbage_ca_rejected(store, monkeypatch):
    _fake_envelope(monkeypatch)
    body, status = mcp_core.upsert_server(
        store, {"name": "bad", "url": "https://mcp.example.com/mcp",
                "ca_pem": "-----BEGIN CERTIFICATE-----\nnot base64!!\n-----END CERTIFICATE-----"})
    assert status == 400
    assert body["error"]["kind"] == "invalid_ca"


def test_oversized_ca_rejected(store, monkeypatch):
    _fake_envelope(monkeypatch)
    body, status = mcp_core.upsert_server(
        store, {"name": "big", "url": "https://mcp.example.com/mcp",
                "ca_pem": "x" * (mcp_core.MAX_CA_BYTES + 1)})
    assert status == 400
    assert body["error"]["kind"] == "ca_too_large"
