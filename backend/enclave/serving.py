"""Enclave ASGI serving stack: TLS materialization + gunicorn/uvicorn wiring.

Verbatim-transcribed from enclave_app.py's gunicorn section (old
L2209-2325), retargeted at the FastAPI app (``enclave.routes.build_app``)
served under uvicorn worker processes instead of Flask/gthread. TLS posture
itself (the actual SSLContext) now lives in ``enclave.asgi_worker`` — this
module only materializes the PEM to files and assembles gunicorn options.
"""
from __future__ import annotations

import atexit
import os
import tempfile
from typing import Any

from enclave import config, state


def materialize_tls_files() -> tuple[str, str] | None:
    """Write the in-memory TLS PEM to two tmpfs files for gunicorn.

    gunicorn loads its server cert/key from file paths (and flips SSL on iff
    a certfile/keyfile is configured), so we materialize the PEM that
    bootstrap() derived. The files are mode 0600 and unlinked atexit. In a
    TDX CVM /tmp is an in-memory tmpfs, so the key never touches persistent
    storage or the operator's disk; outside TDX (local dev) they are ordinary
    temp files cleaned up on exit.

    Returns (cert_path, key_path), or None when TLS is disabled — in which
    case gunicorn serves plain HTTP, matching the old app.run(ssl_context=None)
    behaviour.
    """
    if not state._state["tls_enabled"]:
        return None
    cert_pem = state._state["tls_cert_pem"]
    key_pem = state._state["tls_key_pem"]
    if not cert_pem or not key_pem:
        return None

    paths: list[str] = []
    for pem in (cert_pem, key_pem):
        with tempfile.NamedTemporaryFile("wb", suffix=".pem", delete=False) as f:
            os.chmod(f.name, 0o600)
            f.write(pem)
            f.flush()
            paths.append(f.name)
    cert_path, key_path = paths

    # Guard cleanup to THIS (master) process. gunicorn forks its worker after
    # we register, so the worker inherits this atexit handler; a graceful
    # worker recycle (SIGHUP reload / max_requests) exits the child via
    # sys.exit, which runs atexit. Without the pid guard the dying worker would
    # unlink the cert/key while the master lives, and the respawned worker's
    # load_cert_chain would then FileNotFoundError into a boot crash-loop.
    owner_pid = os.getpid()

    def _cleanup() -> None:
        if os.getpid() != owner_pid:
            return
        for p in (cert_path, key_path):
            try: os.unlink(p)
            except OSError: pass
    atexit.register(_cleanup)
    return cert_path, key_path


def gunicorn_options(tls: tuple[str, str] | None) -> dict[str, Any]:
    """Build the gunicorn config for the enclave ASGI server. Worker count is
    env-driven (``FEEDLING_ENCLAVE_WORKERS``, default 1); bind/timeouts are
    the historical values. TLS certfile/keyfile flip gunicorn's is_ssl on
    (the actual SSLContext is built by ``asgi_worker``'s create_ssl_context
    patch, applied when gunicorn resolves the worker_class string)."""
    options: dict[str, Any] = {
        "bind": f"0.0.0.0:{config.ENCLAVE_PORT}",
        "workers": config.enclave_worker_count(),
        "worker_class": "enclave.asgi_worker.EnclaveUvicornWorker",
        "timeout": 120,
        "graceful_timeout": 30,
    }
    if tls is not None:
        cert_path, key_path = tls
        options["certfile"] = cert_path
        options["keyfile"] = key_path
    return options


def run_enclave_server(tls: tuple[str, str] | None) -> None:
    """Serve the FastAPI app under gunicorn's uvicorn worker (production ASGI).

    We embed gunicorn programmatically — via BaseApplication — instead of
    changing the compose entrypoint, so the command stays
    `python -u backend/enclave_app.py` and the published compose_hash is
    unaffected (CONTRIBUTING.md §7).

    Worker count comes from ``FEEDLING_ENCLAVE_WORKERS``. The code default is 1
    (mirroring the pre-ASGI single-process model, where the process-local
    whoami/content-key caches stay trivially coherent), but PROD DEPLOYS 4 —
    see deploy/docker-compose.phala.yaml — to parallelize the GIL-bound decrypts
    across cores. Each worker additionally serves requests on a 32-thread pool
    (``config.ENCLAVE_THREADS``), so the enclave is never single-threaded.
    """
    # Imported lazily so that `import enclave_app` in the test suite (which
    # never reaches this entrypoint) does not hard-require gunicorn, and so
    # `build_app()` (which wires all routes) is only constructed at actual
    # server start, not at module import time.
    import gunicorn.app.base

    from enclave.routes import build_app

    options = gunicorn_options(tls)

    class _EnclaveApplication(gunicorn.app.base.BaseApplication):
        def load_config(self):
            for key, value in options.items():
                self.cfg.set(key, value)

        def load(self):
            return build_app()

    _EnclaveApplication().run()
