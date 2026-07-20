"""leaf_is_ca: 判服务器叶子证书是否 basicConstraints CA:TRUE（rustls CaUsedAsEndEntity 判据）。"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import user_mcp_ca_fetch as f  # noqa: E402


def _self_signed_ca_true(tmp_path: Path) -> str:
    # openssl req -x509 默认 CA:TRUE —— 业余用户最常见的单张自签名证书
    crt = tmp_path / "s.crt"
    key = tmp_path / "s.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(key),
         "-out", str(crt), "-days", "397", "-nodes", "-subj", "/CN=lone",
         "-addext", "subjectAltName=DNS:localhost"],
        check=True, capture_output=True)
    return crt.read_text()


def _leaf_ca_false(tmp_path: Path) -> str:
    ca_crt, ca_key = tmp_path / "ca.crt", tmp_path / "ca.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(ca_key),
         "-out", str(ca_crt), "-days", "397", "-nodes", "-subj", "/CN=ca"],
        check=True, capture_output=True)
    leaf_key, csr, leaf_crt = tmp_path / "l.key", tmp_path / "l.csr", tmp_path / "l.crt"
    subprocess.run(
        ["openssl", "req", "-newkey", "rsa:2048", "-keyout", str(leaf_key),
         "-out", str(csr), "-nodes", "-subj", "/CN=leaf"],
        check=True, capture_output=True)
    ext = tmp_path / "ext.cnf"
    ext.write_text("basicConstraints=CA:FALSE\nsubjectAltName=DNS:localhost\n")
    subprocess.run(
        ["openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca_crt),
         "-CAkey", str(ca_key), "-CAcreateserial", "-out", str(leaf_crt),
         "-days", "397", "-extfile", str(ext)],
        check=True, capture_output=True)
    return leaf_crt.read_text()


def test_lone_self_signed_leaf_is_ca_true(tmp_path):
    assert f.leaf_is_ca([_self_signed_ca_true(tmp_path)]) is True


def test_leaf_ca_false(tmp_path):
    # 链上第一张是叶子(CA:FALSE)，即使后面跟 CA 也只看 chain[0]
    assert f.leaf_is_ca([_leaf_ca_false(tmp_path)]) is False


def test_empty_chain_is_none(tmp_path):
    assert f.leaf_is_ca([]) is None


def test_garbage_pem_is_none(tmp_path):
    assert f.leaf_is_ca(["-----BEGIN CERTIFICATE-----\nnotpem\n-----END CERTIFICATE-----"]) is None


def test_fetch_trust_anchor_unchanged_signature(tmp_path):
    # 薄包装：非 https 仍返回 None
    assert f.fetch_trust_anchor("http://x") is None
