#!/usr/bin/env python3
"""Dev / hermetic-test HTTP entry point for the assembled ASGI backend.

Production serves via gunicorn's UvicornWorker (deploy/*compose*,
``asgi_app:app``); this ``python backend/serve_dev.py`` path boots the SAME
assembled ASGI app under a single uvicorn process so the subprocess-based
integration tests (and local dev) get a real HTTP server with the full
lifespan (wake-bus, threadpool limiter, wake hook). Plain uvicorn (no
gunicorn fork) sidesteps the macOS fork+C-extension SIGSEGV.

Replaces the old ``python backend/app.py`` dev entry (the Flask parity
facade deleted per ASGI-migration §13).
"""

from __future__ import annotations

import os


def main() -> None:
    # Fail-fast: gateway-only codex providers have no consumer spawned unless
    # the in-CVM LiteLLM gateway is up; validate here so a misconfig surfaces
    # at launch, not at request time (gunicorn runs the same check in
    # on_starting).
    from hosted import agent_runtime_cutover

    agent_runtime_cutover.assert_hosting_ready()

    import uvicorn

    import asgi_app

    port = int(os.environ.get("FEEDLING_PORT", os.environ.get("PORT", "5001")))
    print(f"Feedling backend (ASGI) running at http://0.0.0.0:{port} (mode=multi-tenant, auth=api-key)")
    uvicorn.run(asgi_app.app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
