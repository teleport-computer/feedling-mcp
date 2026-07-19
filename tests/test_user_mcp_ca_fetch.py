"""Pure unit tests for tools/user_mcp_ca_fetch.py's chain-picking helper.

No network, no DB — the I/O half (fetch_trust_anchor) is covered by the
integration test in Task 2. See tests/conftest.py _PURE_UNIT for why this
module is collectable without a reachable Postgres.
"""

import http.server
import os
import ssl as _ssl
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from tools import user_mcp_ca_fetch as f  # noqa: E402

_A = "-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----"
_B = "-----BEGIN CERTIFICATE-----\nBBB\n-----END CERTIFICATE-----"
_C = "-----BEGIN CERTIFICATE-----\nCCC\n-----END CERTIFICATE-----"


def test_pick_single_self_signed_cert():
    # 单张自签名证书：它自己就是信任锚
    assert f._pick_trust_anchor([_A]) == _A


def test_pick_last_of_ca_plus_leaf():
    # 自建 CA + 叶子（靶子服务器的形态）：取最后一张 = 根 CA。
    # 实测依据：只信任叶子 → CERTIFICATE_VERIFY_FAILED；只信任根 → 通过。
    assert f._pick_trust_anchor([_A, _B]) == _B


def test_pick_last_of_leaf_intermediate_no_root():
    # 叶子 + 中间 CA（服务器不发根）：最后一张是中间 CA，也能当锚
    assert f._pick_trust_anchor([_A, _B, _C]) == _C


def test_pick_empty_chain_is_none():
    assert f._pick_trust_anchor([]) is None


def _make_self_signed(tmpdir: str) -> tuple[str, str]:
    """(certfile, keyfile) — 一张自签名证书，CN=localhost。"""
    cert = str(Path(tmpdir) / "srv.pem")
    key = str(Path(tmpdir) / "srv.key")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", key, "-out", cert, "-days", "2",
         "-subj", "/CN=localhost",
         "-addext", "subjectAltName=DNS:localhost"],
        check=True, capture_output=True)
    return cert, key


@pytest.fixture()
def tls_server():
    """真 TLS server(自签名)，yield 出它的 https://localhost:<port> 基址。"""
    with tempfile.TemporaryDirectory() as td:
        cert, key = _make_self_signed(td)
        httpd = http.server.HTTPServer(("127.0.0.1", 0),
                                       http.server.SimpleHTTPRequestHandler)
        ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            yield f"https://localhost:{httpd.server_port}/"
        finally:
            httpd.shutdown()


def test_fetch_returns_usable_anchor_for_self_signed(tls_server):
    pem = f.fetch_trust_anchor(tls_server, timeout=5.0)
    assert pem is not None
    assert "BEGIN CERTIFICATE" in pem
    # 决定性断言：拿它当 CA 真的能验通(不是"看起来像证书")
    ctx = _ssl.create_default_context(cadata=pem)
    import socket
    from urllib.parse import urlparse
    port = urlparse(tls_server).port
    with socket.create_connection(("localhost", port), timeout=5) as s:
        with ctx.wrap_socket(s, server_hostname="localhost") as ss:
            assert ss.version() is not None


def test_publicly_trusted_server_is_never_pinned(monkeypatch, tls_server):
    """红线 1 的**主守卫** —— 请勿删除、请勿弱化。

    正规证书的服务器**不该被钉**——不是因为钉了会在轮换后断连（两条投递路径都是
    叠加语义：NODE_EXTRA_CA_CERTS 对 Node 是 ADD，SSL_CERT_FILE 内容是
    certifi 公共根 + 我们的 bundle，公共根始终兜底，钉了也不会断），而是因为
    TOFU 锚定本身是新引入的攻击面（见 user_mcp_ca_fetch.py 模块里
    _verifies_against_public_roots 的 docstring 和 spec §8）：对一台已经能正常
    校验的 server 做 TOFU，除了平白多开一扇「抓取那一刻被冒充就会钉进攻击者 CA」
    的窗口之外没有任何收益。

    这条不碰外网：把「已被公共根信任」这个判定 monkeypatch 成 True，断言
    fetch_trust_anchor 直接返回 None **且根本没去抓链**。确定性、CI 必跑。
    （下面还有一条真打公网的集成检查，那条无外网会 skip —— 被 skip 的测试守不住
    任何东西，所以真正的守卫是这一条。）
    """
    fetched = []
    monkeypatch.setattr(f, "_verifies_against_public_roots",
                        lambda host, port, timeout: True)
    monkeypatch.setattr(f, "_fetch_chain",
                        lambda host, port, timeout: fetched.append(host) or [])
    assert f.fetch_trust_anchor(tls_server, timeout=5.0) is None
    assert fetched == []          # 闸在抓取之前就拦住了


@pytest.mark.skipif(os.environ.get("SKIP_NETWORK_TESTS") == "1",
                    reason="no outbound network")
def test_fetch_returns_none_for_publicly_trusted_server_live():
    """红线 1 的集成检查（真打公网，无外网时 skip）。

    验证面对真实世界的正规证书服务器时，那道闸确实不钉。主守卫是上面那条确定性
    单测；这条是额外的真实性背书，不是替代品。
    """
    assert f.fetch_trust_anchor("https://example.com/", timeout=8.0) is None


def test_fetch_returns_none_when_unreachable():
    # 不可达(端口没人听) → None，且**不抛**
    assert f.fetch_trust_anchor("https://127.0.0.1:1/", timeout=2.0) is None


def test_fetch_returns_none_for_non_https():
    assert f.fetch_trust_anchor("http://example.com/", timeout=2.0) is None


def test_fetch_returns_none_for_garbage_url():
    assert f.fetch_trust_anchor("not-a-url", timeout=2.0) is None


def test_is_well_formed_ca_rejects_oversized_pem():
    # 复现评审场景:一张塞满 padding、体积超过后端 MAX_CA_BYTES(32768 字节)的
    # PEM。大小检查必须先于/独立于解析检查生效——哪怕内容本身合法可 parse,
    # 超限就该被拒,不能靠"格式对不对"来判定。
    huge_body = "A" * (f.MAX_CA_BYTES + 1)
    huge_pem = f"-----BEGIN CERTIFICATE-----\n{huge_body}\n-----END CERTIFICATE-----"
    assert len(huge_pem.encode("utf-8")) > f.MAX_CA_BYTES
    assert f._is_well_formed_ca(huge_pem) is False


def test_is_well_formed_ca_accepts_real_cert_well_under_limit(tls_server):
    # 反向断言:新加的大小检查不能误伤真实场景——真实抓来的自签名证书远小于
    # 上限,_is_well_formed_ca 仍应放行。
    pem = f.fetch_trust_anchor(tls_server, timeout=5.0)
    assert pem is not None
    assert len(pem.encode("utf-8")) <= f.MAX_CA_BYTES
    assert f._is_well_formed_ca(pem) is True


def test_fetch_trust_anchor_rejects_oversized_padded_ca(monkeypatch):
    """端到端复现评审的攻击面:格式合法、塞大 padding 做体积的自签 CA，即使
    握手自验(_anchor_works)通过，也必须被 fetch_trust_anchor 挡在返回值之前
    ——不能只在单元函数层面挡住，管线整体也要挡住。这就是本次要堵的不对称
    边界:手动粘贴的 ca_pem 被后端限 32KB，自动抓取的之前完全没有上限。
    """
    huge_body = "A" * (f.MAX_CA_BYTES + 1)
    huge_pem = f"-----BEGIN CERTIFICATE-----\n{huge_body}\n-----END CERTIFICATE-----"
    monkeypatch.setattr(f, "_verifies_against_public_roots",
                        lambda host, port, timeout: False)
    monkeypatch.setattr(f, "_fetch_chain",
                        lambda host, port, timeout: [huge_pem])
    # 假装握手自验通过(“塞满巨大 SAN 仍能真握手验得过”是评审给出的真实威胁形态)，
    # 逼真相：唯一能挡住它的只剩大小检查。
    monkeypatch.setattr(f, "_anchor_works",
                        lambda pem, host, port, timeout: True)
    assert f.fetch_trust_anchor("https://example.invalid/", timeout=2.0) is None
