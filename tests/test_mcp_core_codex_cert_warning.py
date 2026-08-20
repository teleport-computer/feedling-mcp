"""mcp_probe.leaf_is_ca + test_server 的 codex_cert_chain_required kind (Task 3)."""
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from accounts import registry  # noqa: E402
from core import store as core_store  # noqa: E402
from hosted import mcp_core, mcp_probe  # noqa: E402


def _serve_tls(tmp_path, cert_pem_files):
    """起一个 TLS server(单张 or 链)，返回 (port, stop)。用 http.server + ssl 最小化。"""
    import http.server
    import ssl as _ssl
    crt, key = cert_pem_files
    ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(crt), keyfile=str(key))
    httpd = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.handle_request, daemon=True)  # 一次即可
    t.start()
    return port, httpd


def _lone(tmp_path):
    crt, key = tmp_path / "s.crt", tmp_path / "s.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout",
                    str(key), "-out", str(crt), "-days", "397", "-nodes",
                    "-subj", "/CN=lone", "-addext", "subjectAltName=IP:127.0.0.1"],
                   check=True, capture_output=True)
    return crt, key


def test_leaf_is_ca_true_for_lone_self_signed(tmp_path, monkeypatch):
    # NOTE (deviation from brief's literal test): 127.0.0.1 is loopback, i.e.
    # non-global — the *reused* blocked_url_kind SSRF guard rejects it before
    # ever dialing (same as the existing test_backend_never_probes_non_global
    # regression in test_user_mcp_probe.py, which asserts exactly that for
    # probe()). That guard is working as intended and must stay reused; this
    # unit only wants to exercise the leaf-cert inspection past the gate, so
    # bypass the gate here the same way test_user_mcp_probe.py's happy-path
    # tests bypass DNS resolution to simulate a globally-routable host.
    monkeypatch.setattr(mcp_probe, "blocked_url_kind", lambda url: None)
    crt, key = _lone(tmp_path)
    port, httpd = _serve_tls(tmp_path, (crt, key))
    try:
        assert mcp_probe.leaf_is_ca(f"https://127.0.0.1:{port}/mcp") is True
    finally:
        httpd.server_close()


def test_leaf_is_ca_none_when_unreachable():
    assert mcp_probe.leaf_is_ca("https://127.0.0.1:1/mcp") is None


def test_leaf_is_ca_none_for_blocked_url():
    # 非 global 地址：blocked_url_kind 前置闸 → None（不拨号）
    assert mcp_probe.leaf_is_ca("https://169.254.169.254/mcp") is None


# ---------------------------------------------------------------------------
# test_server 的 driver-gated kind（需 PG 真 store）
# ---------------------------------------------------------------------------


@pytest.fixture()
def mcp_store(backend_env):
    user = registry._register_user(public_key="A" * 43 + "=", archive_language="en")
    return core_store.get_store(user["user_id"])


def _fake_envelope(monkeypatch):
    from core import envelope as core_envelope
    monkeypatch.setattr(
        core_envelope, "_build_shared_envelope_for_store",
        lambda store, raw, item_id=None: ({"v": 1, "id": item_id, "body_ct": raw.hex()}, ""),
    )


def _fake_decrypt(monkeypatch):
    from core import enclave as core_enclave
    monkeypatch.setattr(
        core_enclave, "_decrypt_envelope_via_enclave",
        lambda env, key, *, purpose, runtime_token="": json.dumps(
            {"url": "https://mcp.example.com/mcp", "headers": {}}).encode())


def _seed_probe_server(store, monkeypatch):
    _fake_envelope(monkeypatch)
    body, status = mcp_core.upsert_server(store, {
        "name": "probe", "url": "https://mcp.example.com/mcp", "headers": {}})
    assert status == 200, body
    _fake_decrypt(monkeypatch)


def test_test_server_codex_lone_cert_returns_kind(monkeypatch, mcp_store):
    _seed_probe_server(mcp_store, monkeypatch)
    monkeypatch.setattr(mcp_core, "_user_driver", lambda store, key: "codex")
    monkeypatch.setattr(mcp_probe, "leaf_is_ca", lambda url, **k: True)
    # probe 本身对自签名会 tls 失败
    monkeypatch.setattr(mcp_probe, "probe",
                        lambda *a, **k: (_ for _ in ()).throw(mcp_probe.ProbeError("tls", "self-signed")))
    body, status = mcp_core.test_server(mcp_store, "probe", "api-key")
    assert status == 400
    assert body["error"]["kind"] == "codex_cert_chain_required"


def test_test_server_codex_leaf_none_stays_tls(monkeypatch, mcp_store):
    # codex 驱动但 leaf_is_ca 返回 None（无证书/连不上/非全局地址）→ 门控 `is True`
    # 为假 → 正确回落原 tls，不误报 codex_cert_chain_required。
    _seed_probe_server(mcp_store, monkeypatch)
    monkeypatch.setattr(mcp_core, "_user_driver", lambda store, key: "codex")
    monkeypatch.setattr(mcp_probe, "leaf_is_ca", lambda url, **k: None)
    monkeypatch.setattr(mcp_probe, "probe",
                        lambda *a, **k: (_ for _ in ()).throw(mcp_probe.ProbeError("tls", "self-signed")))
    body, status = mcp_core.test_server(mcp_store, "probe", "api-key")
    assert status == 400
    assert body["error"]["kind"] == "tls"


def test_test_server_claude_lone_cert_stays_tls(monkeypatch, mcp_store):
    _seed_probe_server(mcp_store, monkeypatch)
    monkeypatch.setattr(mcp_core, "_user_driver", lambda store, key: "claude")
    monkeypatch.setattr(mcp_probe, "leaf_is_ca", lambda url, **k: True)
    monkeypatch.setattr(mcp_probe, "probe",
                        lambda *a, **k: (_ for _ in ()).throw(mcp_probe.ProbeError("tls", "self-signed")))
    body, status = mcp_core.test_server(mcp_store, "probe", "api-key")
    assert status == 400
    assert body["error"]["kind"] == "tls"
