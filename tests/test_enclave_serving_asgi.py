from __future__ import annotations

import datetime as dt
import ssl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))



def _self_signed(tmp_path):
    """测试用自签 ECDSA P-256 证书（与 dstack_tls 产物同形）。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(dt.datetime(2020, 1, 1))
            .not_valid_after(dt.datetime(2040, 1, 1))
            .sign(key, hashes.SHA256()))
    cert_p = tmp_path / "cert.pem"
    key_p = tmp_path / "key.pem"
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_p.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return str(cert_p), str(key_p)


def test_custom_ssl_context_semantics(tmp_path):
    from enclave import asgi_worker
    cert, key = _self_signed(tmp_path)
    ctx = asgi_worker._enclave_create_ssl_context(certfile=cert, keyfile=key)
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2


def test_ssl_context_matches_uvicorn_call_conventions(tmp_path):
    """uvicorn 会用它自己的完整参数集调 create_ssl_context——签名是
    (certfile, keyfile, password, ssl_version, cert_reqs, ca_certs, ciphers)。
    shim 必须对全关键字(当前 uvicorn 0.50 的实际行为)和全位置(未来版本可能)
    两种调用都产出有效的 TLS1.2 context，不能因签名不匹配在 TLS enclave 启动时
    TypeError 崩死。"""
    from enclave import asgi_worker
    cert, key = _self_signed(tmp_path)

    # (A) 全关键字 7 参数 —— uvicorn 0.49/0.50 Config.load 的真实调用方式
    ctx_kw = asgi_worker._enclave_create_ssl_context(
        keyfile=key, certfile=cert, password=None,
        ssl_version=ssl.PROTOCOL_TLS_SERVER, cert_reqs=ssl.CERT_NONE,
        ca_certs=None, ciphers=None,
    )
    assert isinstance(ctx_kw, ssl.SSLContext)
    assert ctx_kw.minimum_version == ssl.TLSVersion.TLSv1_2

    # (B) 全位置 7 参数 —— 防将来 uvicorn 改成位置调用(下限 >=0.30 允许)
    ctx_pos = asgi_worker._enclave_create_ssl_context(
        cert, key, None, ssl.PROTOCOL_TLS_SERVER, ssl.CERT_NONE, None, None,
    )
    assert isinstance(ctx_pos, ssl.SSLContext)
    assert ctx_pos.minimum_version == ssl.TLSVersion.TLSv1_2


def test_import_does_not_patch_uvicorn_globally(monkeypatch):
    """import enclave.asgi_worker 不得有进程级副作用：同进程里其他 uvicorn
    使用方（pytest 全家、未来同进程嵌入部署）不能被静默换成 enclave 的
    精简 TLS context。patch 只在 EnclaveUvicornWorker.init_process 里装。"""
    import importlib
    import uvicorn.config
    from enclave import asgi_worker

    sentinel = object()
    monkeypatch.setattr(uvicorn.config, "create_ssl_context", sentinel)
    importlib.reload(asgi_worker)
    assert uvicorn.config.create_ssl_context is sentinel


def test_worker_init_process_installs_ssl_patch(monkeypatch):
    """enclave worker 进程启动时（init_process，fork 后子进程）才安装
    create_ssl_context 替换，随后交还 UvicornWorker 正常启动。"""
    import uvicorn.config
    from enclave import asgi_worker

    monkeypatch.setattr(uvicorn.config, "create_ssl_context", object())
    called = {}
    monkeypatch.setattr(asgi_worker.UvicornWorker, "init_process",
                        lambda self: called.setdefault("super", True))
    worker = object.__new__(asgi_worker.EnclaveUvicornWorker)
    worker.init_process()
    assert uvicorn.config.create_ssl_context is asgi_worker._enclave_create_ssl_context
    assert called.get("super") is True


def test_gunicorn_options(monkeypatch, tmp_path):
    from enclave import serving
    monkeypatch.setenv("FEEDLING_ENCLAVE_WORKERS", "2")
    opts = serving.gunicorn_options(None)
    assert opts["workers"] == 2
    assert opts["worker_class"] == "enclave.asgi_worker.EnclaveUvicornWorker"
    assert opts["timeout"] == 120
    assert "certfile" not in opts
    cert, key = _self_signed(tmp_path)
    opts = serving.gunicorn_options((cert, key))
    assert opts["certfile"] == cert and opts["keyfile"] == key


def test_materialize_tls_files_roundtrip(monkeypatch):
    from enclave import serving, state
    monkeypatch.setitem(state._state, "tls_enabled", True)
    monkeypatch.setitem(state._state, "tls_cert_pem", b"CERT")
    monkeypatch.setitem(state._state, "tls_key_pem", b"KEY")
    tls = serving.materialize_tls_files()
    assert tls is not None
    cert_path, key_path = tls
    assert Path(cert_path).read_bytes() == b"CERT"
    assert Path(key_path).read_bytes() == b"KEY"
    assert (Path(cert_path).stat().st_mode & 0o777) == 0o600
    monkeypatch.setitem(state._state, "tls_enabled", False)
    assert serving.materialize_tls_files() is None


def test_thin_entrypoint_importable_without_flask():
    import importlib
    for m in ("flask", "flask_compress"):
        sys.modules.pop(m, None)
    import enclave_app  # noqa: F401
    importlib.reload(enclave_app)
    assert "flask" not in sys.modules  # 薄入口不再触碰 flask
