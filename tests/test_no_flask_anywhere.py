# tests/test_no_flask_anywhere.py
"""ASGI 迁移收尾守卫：全 backend 不再 import flask（spec §8.4）。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).parent.parent / "backend"
TESTS = Path(__file__).parent
TOOLS = Path(__file__).parent.parent / "tools"


def test_no_flask_imports_in_backend():
    # Scan backend/, tests/, and tools/ (not just backend/) so a dead flask
    # import left behind in a test file (as happened in test_perception.py)
    # can't silently pass CI once flask is genuinely uninstalled.
    scan_dirs = [str(d) for d in (BACKEND, TESTS, TOOLS) if d.is_dir()]
    out = subprocess.run(
        ["grep", "-rn", "-E", r"^\s*(import flask|from flask)", *scan_dirs,
         "--include=*.py"],
        capture_output=True, text=True)
    assert out.stdout.strip() == "", f"flask imports remain:\n{out.stdout}"


def test_enclave_package_imports_clean():
    sys.path.insert(0, str(BACKEND))
    for mod in ("enclave.config", "enclave.keys", "enclave.attestation",
                "enclave.state", "enclave.envelope", "core.visual",
                "enclave.readside", "enclave.backend_client", "enclave.auth",
                "enclave.routes", "enclave.serving", "enclave.asgi_worker"):
        __import__(mod)
    assert "flask" not in sys.modules
