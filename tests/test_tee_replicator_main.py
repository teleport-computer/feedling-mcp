"""Subprocess smoke test for backend/tee_replicator/__main__.py's sys.path fixup.

``python -m backend.tee_replicator`` (repo root) and ``python -m tee_replicator``
(cwd=backend/) are BOTH real invocation shapes — the admin workflow (Task 8)
uses the former, a developer's shell running from backend/ uses the latter.
Before the fixup (copied from tee_shadow/__main__.py's own sys.path patch),
the repo-root form crashed with ``ModuleNotFoundError: No module named
'tee_replicator'`` because ``from tee_replicator import worker`` is a bare,
non-package-qualified import that only resolves once backend/ itself (not the
repo root) is on sys.path. No DB/env fixtures needed — invoking with no
subcommand hits argparse's ``required=True`` error path before any DB code
runs, which is enough to prove the import succeeded.
"""
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND_DIR = _REPO_ROOT / "backend"


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args], cwd=str(cwd), capture_output=True, text=True, timeout=30,
    )


def test_module_form_from_repo_root_does_not_importerror():
    proc = _run(["-m", "backend.tee_replicator"], cwd=_REPO_ROOT)
    assert "ModuleNotFoundError" not in proc.stderr, proc.stderr
    assert "ImportError" not in proc.stderr, proc.stderr
    # argparse's required-subcommand error (exit code 2), not a crash.
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "usage:" in proc.stderr.lower()


def test_module_form_from_backend_dir_does_not_importerror():
    proc = _run(["-m", "tee_replicator"], cwd=_BACKEND_DIR)
    assert "ModuleNotFoundError" not in proc.stderr, proc.stderr
    assert "ImportError" not in proc.stderr, proc.stderr
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "usage:" in proc.stderr.lower()
