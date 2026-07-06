#!/usr/bin/env python3
"""Snapshot the ASGI (FastAPI) backend's route table for route accounting.

Why this exists (ASGI 迁移计划 §6.1，计划稿已删，见 git 历史):
the FastAPI cutover had **no runtime fallback**, so a route missed during
migration would be a user-visible 404. Originally this snapshotted the Flask
url_map as the migration baseline; post-cutover the Flask app is gone, so it now
walks ``asgi_app.app.routes`` — the ground truth for "100% route accounting".
Regenerate and diff after any route change to catch accidental surface drift.

Output columns (tab-separated, stable-sorted by path then methods):

    PATH \t METHODS \t ENDPOINT \t OWNER

- METHODS excludes the auto-added HEAD/OPTIONS (kept as a trailing note) so the
  diff tracks real handler surface, not framework bookkeeping.
- OWNER is derived from the endpoint name's blueprint-style prefix (the part
  before the first '.'), which maps 1:1 to the domain package that owns the
  route.

Usage:

    # Against an explicit DB (must already have the schema / or let it upgrade):
    DATABASE_URL=postgresql://... python tools/gen_url_map.py [OUT_FILE]

    # Or self-provision a throwaway test DB (mirrors tests/conftest.py):
    python tools/gen_url_map.py [OUT_FILE]
      # uses FEEDLING_TEST_PG (default postgresql://postgres:test@127.0.0.1:55432/postgres)

If OUT_FILE is omitted the snapshot goes to stdout.
"""

import copy
import os
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"


def _ensure_env_and_db() -> None:
    """Replicate tests/conftest.py's minimal boot env so `import asgi_app` succeeds.

    The hosting-ready gate and a reachable DATABASE_URL are prerequisites for
    importing the backend (db.init_schema() runs at startup). We only
    provision a throwaway DB when DATABASE_URL is not already supplied.
    """
    os.environ.setdefault("FEEDLING_LITELLM_ENABLE", "1")
    os.environ.setdefault("FEEDLING_HOST_ALL", "1")
    os.environ.setdefault("FEEDLING_RUNTIME_TOKEN_SECRET", "url-map-snapshot-secret")

    if os.environ.get("DATABASE_URL"):
        return

    admin_url = os.environ.get(
        "FEEDLING_TEST_PG", "postgresql://postgres:test@127.0.0.1:55432/postgres"
    )
    test_db = f"feedling_urlmap_{uuid.uuid4().hex[:12]}"
    base, _, _ = admin_url.rpartition("/")
    import psycopg

    admin = psycopg.connect(admin_url, autocommit=True)
    admin.execute(f'CREATE DATABASE "{test_db}"')
    admin.close()
    os.environ["DATABASE_URL"] = f"{base}/{test_db}"


def _route_owner(endpoint) -> str:
    """Owner = the domain package that defines the handler.

    FastAPI routes carry no blueprint prefix, so we derive the owner from the
    handler's module: ``chat.routes_asgi`` -> ``chat``. Routes defined directly
    in ``asgi_app`` (no package) are attributed to ``(app)``.
    """
    module = getattr(endpoint, "__module__", "") or ""
    if not module or module in {"asgi_app", "app"}:
        return "(app)"
    return module.split(".", 1)[0]


def _flatten_routes(routes, prefix=""):
    """Depth-first expansion of the route table.

    fastapi 0.139 / starlette 1.3 made ``include_router`` lazy: ``app.routes``
    holds ``_IncludedRouter`` proxies (carrying ``original_router`` +
    ``include_context``) instead of flattened ``Route`` objects, so a plain
    isinstance walk saw ZERO routes and the drift snapshot went silently empty.
    Recurse through both the lazy proxies (new) and plain routers (old) so the
    snapshot works on either version.
    """
    from starlette.routing import BaseRoute, Router

    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:  # starlette 1.3 lazy include proxy
            ctx = getattr(route, "include_context", None)
            sub_prefix = prefix + (getattr(ctx, "prefix", "") or "")
            yield from _flatten_routes(original.routes, sub_prefix)
        elif isinstance(route, Router) and not isinstance(route, BaseRoute):
            # Pre-1.3 nested bare Router/APIRouter (NOT a Mount — Mounts are
            # BaseRoutes and stay pass-through so _iter_routes filters them).
            yield from _flatten_routes(route.routes, prefix + (getattr(route, "prefix", "") or ""))
        elif prefix and hasattr(route, "path"):
            clone = copy.copy(route)
            clone.path = prefix + route.path
            yield clone
        else:
            yield route


def _iter_routes(asgi_app_obj):
    from starlette.routing import Route, WebSocketRoute

    def sort_key(route):
        methods = sorted(getattr(route, "methods", None) or [])
        return (getattr(route, "path", ""), methods)

    for route in sorted(_flatten_routes(asgi_app_obj.routes), key=sort_key):
        endpoint = getattr(route, "endpoint", None)
        if isinstance(route, WebSocketRoute):
            yield {
                "path": route.path,
                "methods": "WS",
                "auto": "",
                "endpoint": getattr(endpoint, "__name__", "(ws)"),
                "owner": _route_owner(endpoint),
            }
            continue
        if not isinstance(route, Route):
            # Mounts / static — not a handler surface we account for.
            continue
        # Starlette auto-adds HEAD (for GET) and OPTIONS. Strip them from the
        # primary method set but note whether they were present, so the snapshot
        # tracks real handler surface, not framework bookkeeping.
        methods = set(route.methods or [])
        auto = sorted(methods & {"HEAD", "OPTIONS"})
        real = sorted(methods - {"HEAD", "OPTIONS"})
        yield {
            "path": route.path,
            "methods": ",".join(real),
            "auto": ",".join(auto),
            "endpoint": getattr(endpoint, "__name__", "(fn)"),
            "owner": _route_owner(endpoint),
        }


def main() -> int:
    out_path = sys.argv[1] if len(sys.argv) > 1 else None
    _ensure_env_and_db()
    sys.path.insert(0, str(BACKEND))

    import db

    db.init_schema()
    import asgi_app

    rows = list(_iter_routes(asgi_app.app))

    lines = [
        "# ASGI (FastAPI) backend route snapshot — post-cutover route accounting (§6.1).",
        "# Regenerate: python tools/gen_url_map.py <out-file>  (or omit for stdout; snapshots are not checked in)",
        "# Columns: PATH\\tMETHODS\\tENDPOINT\\tOWNER  (auto HEAD/OPTIONS noted in trailing comment)",
        f"# Total routes: {len(rows)}",
        "",
    ]
    for r in rows:
        note = f"  # auto:{r['auto']}" if r["auto"] else ""
        lines.append(f"{r['path']}\t{r['methods']}\t{r['endpoint']}\t{r['owner']}{note}")
    text = "\n".join(lines) + "\n"

    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(text)
        print(f"wrote {len(rows)} rules -> {out_path}", file=sys.stderr)
    else:
        sys.stdout.write(text)

    # Importing the route modules may start daemon background threads (wake_bus
    # listener, etc.) that keep reconnecting; nothing left to do, so exit hard to
    # avoid a hang.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
