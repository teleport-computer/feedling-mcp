"""FastAPI backend assembly (ASGI-migration plan §5.3).

The backend's single assembly layer — **assembly only**, no business logic
(CONTRIBUTING §1). It wires the lifespan, the access-log middleware, the
fixed-body exception handlers, includes the native routers, and injects the
cross-package seams at the bottom of this file. The old Flask parity facade
``app.py`` was deleted post-cutover (migration §13); tests drive this app via
``asgi_test_client.make_client()``.

Cutover completed 2026-07-04 with 100% route accounting (the migration-era
url_map ledger BACKEND_ASGI_ROUTE_MATRIX is deleted; see git history). Use
``tools/gen_url_map.py`` to snapshot/diff the live route surface.

Start command (plan §5.2):
    gunicorn --chdir backend --config backend/gunicorn_conf.py \\
        -k asgi.worker.FeedlingUvicornWorker --timeout 120 \\
        -b 0.0.0.0:5001 asgi_app:app
"""

from __future__ import annotations

import importlib

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

from asgi import health, middleware
from asgi.lifespan import lifespan
from asgi.settings import settings

# Domain packages exposing register_asgi(app), the ASGI counterpart of app.py's
# pkg.register(app). Added as each package's routes_asgi.py lands (plan §5.3).
# Assembly-only: names live here, logic lives in the packages.
_ASGI_PACKAGES = (
    "accounts.routes_asgi",
    "bootstrap.routes_asgi",
    "chat.routes_asgi",
    "web.routes_asgi",
    "proactive.routes_asgi",
    "agent.routes_asgi",
    "copytext.routes_asgi",
    "tracking.routes_asgi",
    "push.routes_asgi",
    "diagnostics.routes_asgi",
    "worldbook.routes_asgi",
    "identity.routes_asgi",
    "memory.routes_asgi",
    "screen.routes_asgi",
    "content.routes_asgi",
    "perception.routes_asgi",
    "admin.routes_asgi",
    "genesis.routes_asgi",
    "onboarding_archive.routes_asgi",
    "hosted.setup_routes_asgi",
    "hosted.mcp_routes_asgi",
    "hosted.chat_routes_asgi",
    "hosted.history_import_asgi",
    "hosted.onboarding_validation_asgi",
    "notices.routes_asgi",
    "notify_relay.routes_asgi",
)

# Disable the auto OpenAPI/docs routes so the ASGI route surface is exactly the
# migrated routes — nothing outside the url_map ledger, which keeps the
# post-cutover 404 monitoring (plan §15.1) meaningful.
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

# gzip when the client sends Accept-Encoding: gzip — the ASGI heir of Flask's
# Compress(app). CVM egress is ~30-50 KB/s and large payloads (decrypt-with-image
# shipped 470 KB of JSON) get a 3-5x latency win from compression. minimum_size
# mirrors flask-compress's 500-byte default. Added BEFORE the access log so the
# log middleware wraps it and records the final on-the-wire Content-Length /
# Content-Encoding (starlette: last add_middleware = outermost).
app.add_middleware(GZipMiddleware, minimum_size=500)

# Permit the public documentation playground to call the API directly from a
# browser.  Origins and request headers are deliberately explicit: CORS is a
# browser access policy, not a substitute for the existing per-route auth.
if settings.cors_allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "If-None-Match",
            "Range",
            "X-API-Key",
            "X-Byte-End",
            "X-Byte-Start",
            "X-Ciphertext-SHA256",
            "X-Content-SHA256",
            "X-Envelope-Meta",
            "X-Feedling-Consumer",
            "X-Feedling-Consumer-Commit",
            "X-Feedling-Consumer-Id",
            "X-Feedling-Consumer-Version",
            "X-Feedling-Runtime-Token",
            "X-Relay-Token",
        ],
        expose_headers=[
            "Accept-Ranges",
            "Content-Disposition",
            "Content-Range",
            "ETag",
            "X-Request-Id",
        ],
        max_age=600,
    )

# Outermost: structured access log + ?key= redaction + cancelled-request lines.
app.add_middleware(middleware.AccessLogMiddleware)

# Fixed-body error mapping (parity with app.py errorhandler 401/403/503).
middleware.register_exception_handlers(app)

# Native routers. /healthz has no auth/package; domain routers fan out via
# register_asgi(app), mirroring app.py's pkg.register(app).
app.include_router(health.router)
for _mod_name in _ASGI_PACKAGES:
    importlib.import_module(_mod_name).register_asgi(app)

# Assembly wiring (dependency direction: identity sits above push, hosted above
# admin — the lower module declares a stub, assembly injects the real impl).
# Moved here from app.py at its deletion; the ASGI cutover had left these three
# unwired, so Live Activity identity and admin data-track import/validation
# stats were silently empty in production.
from admin import data_track as _admin_data_track  # noqa: E402
from hosted import onboarding_validation as _hosted_onboarding_validation  # noqa: E402
from identity import service as _identity_service  # noqa: E402
from push import live_activity as _push_live_activity  # noqa: E402

_push_live_activity.load_identity = _identity_service._load_identity
_admin_data_track._latest_history_import_job = _hosted_onboarding_validation._latest_history_import_job
_admin_data_track._onboarding_validation_payload = _hosted_onboarding_validation._onboarding_validation_payload
