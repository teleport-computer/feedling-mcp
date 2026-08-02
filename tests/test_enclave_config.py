"""enclave.config 单元测试：常量存在性 + 两个纯函数的解析语义。"""
from __future__ import annotations

import sys
import os
import subprocess
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from enclave import config  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


def _probe(tls: str, mode: str | None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "backend")
    env["FEEDLING_ENCLAVE_TLS"] = tls
    if mode is None:
        env.pop("FEEDLING_ENCLAVE_TRANSPORT_MODE", None)
    else:
        env["FEEDLING_ENCLAVE_TRANSPORT_MODE"] = mode
    return subprocess.run(
        [sys.executable, "-c", "from enclave import config; print(config.ENCLAVE_TRANSPORT_MODE)"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(("tls", "mode", "expected"), [
    ("true", None, "direct_tls"),
    ("false", None, "operator_tls"),
    ("false", "attested_ingress", "attested_ingress"),
])
def test_enclave_transport_mode_defaults_and_override(tls, mode, expected):
    result = _probe(tls, mode)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize(
    ("tls", "mode"),
    [("true", "attested_ingress"), ("false", "direct_tls"), ("false", "bogus")],
)
def test_enclave_transport_mode_rejects_invalid_combinations(tls, mode):
    result = _probe(tls, mode)
    assert result.returncode != 0
    assert "FEEDLING_ENCLAVE_TRANSPORT_MODE" in result.stderr


def test_constants_exist_and_typed():
    assert isinstance(config.ENCLAVE_PORT, int)
    assert isinstance(config.ENCLAVE_TLS, bool)
    assert config.FLASK_URL.startswith("http")
    assert isinstance(config.RUNTIME_TOKEN_SECRET, bytes)
    assert isinstance(config.RELEASE, dict) and "git_commit" in config.RELEASE
    assert isinstance(config.APP_AUTH, dict) and "contract" in config.APP_AUTH
    assert config.ENCLAVE_THREADS >= 1


def test_env_flag_enabled(monkeypatch):
    monkeypatch.setenv("X_FLAG", "TRUE")
    assert config.env_flag_enabled("X_FLAG") is True
    monkeypatch.setenv("X_FLAG", "off")
    assert config.env_flag_enabled("X_FLAG") is False
    monkeypatch.delenv("X_FLAG", raising=False)
    assert config.env_flag_enabled("X_FLAG") is False
    assert config.env_flag_enabled("X_FLAG", default="true") is True


def test_enclave_worker_count(monkeypatch):
    monkeypatch.setenv("FEEDLING_ENCLAVE_WORKERS", "")
    assert config.enclave_worker_count() == 1  # CI 注入空串不能崩
    monkeypatch.setenv("FEEDLING_ENCLAVE_WORKERS", "4")
    assert config.enclave_worker_count() == 4
    monkeypatch.setenv("FEEDLING_ENCLAVE_WORKERS", "0")
    assert config.enclave_worker_count() == 1  # clamp ≥1
