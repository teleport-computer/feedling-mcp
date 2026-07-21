#!/usr/bin/env python3
"""Export the public Feedling API contract without enabling runtime docs routes.

The production FastAPI app intentionally disables /docs, /redoc, and
/openapi.json. FastAPI still builds a schema in process, so the documentation
site can consume a checked-in, filtered snapshot at build time.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

if __package__:
    from .public_openapi_contracts import apply_public_contracts
else:
    from public_openapi_contracts import apply_public_contracts


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
DEFAULT_OUTPUT = ROOT / "docs-site" / "openapi" / "public.json"

# Never publish operator-only surfaces in the public contract.
EXCLUDED_PREFIXES = (
    "/admin",
    "/debug",
    "/v1/admin",
    "/v1/debug",
)

EXCLUDED_OPERATIONS = {
    # Operator-only writer guarded by FEEDLING_ADMIN_TOKEN.  The public GET on
    # the same path is intentionally retained, so exposure must be per method.
    ("post", "/v1/copytext"),
    # User-authenticated implementation diagnostics, not a product API.
    ("get", "/v1/proactive/debug"),
}

PUBLIC_OPERATIONS = {
    ("get", "/healthz"),
    ("post", "/v1/access/claim-token"),
    ("post", "/v1/account/recover/challenge"),
    ("post", "/v1/account/recover/verify"),
    ("post", "/v1/users/register"),
    ("get", "/v1/copytext"),
    # Anonymous by design: self-hosted users often have no official account.
    # Abuse is damped by a per-IP rate limit (notify_relay/routes_asgi.py).
    ("post", "/v1/notify-relay/register"),
}

# Authenticated by the relay's own credential (X-Relay-Token issued by the
# register operation), outside the account api-key/runtime-token system.
RELAY_TOKEN_OPERATIONS = {
    ("post", "/v1/notify-relay/push"),
}

# These operations intentionally reject runtime tokens. Perception report is
# API-key-only until sensitive-signal credentials are forwarded to the enclave.
API_KEY_ONLY_OPERATIONS = {
    ("get", "/v1/web/settings"),
    ("post", "/v1/web/settings"),
    ("post", "/v1/access/link-token"),
    ("post", "/v1/account/reset"),
    ("get", "/v1/mcp/servers"),
    ("post", "/v1/mcp/servers"),
    ("patch", "/v1/mcp/servers/{name}"),
    ("delete", "/v1/mcp/servers/{name}"),
    ("post", "/v1/mcp/servers/{name}/test"),
    ("post", "/v1/perception/report"),
}

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}

TAG_RULES = (
    (("/healthz",), "System"),
    (("/v1/users", "/v1/account", "/v1/access"), "Accounts"),
    (("/v1/bootstrap", "/v1/onboarding", "/v1/genesis", "/v1/history_import"), "Onboarding"),
    (("/v1/model_api/chat", "/v1/chat"), "Chat"),
    (("/v1/model_api",), "Model API"),
    (("/v1/mcp",), "MCP"),
    (("/v1/memory",), "Memory"),
    (("/v1/identity",), "Identity"),
    (("/v1/worldbook",), "Worldbook"),
    (("/v1/perception",), "Perception"),
    (("/v1/screen", "/v1/sources"), "Screen Context"),
    (("/v1/content",), "Content"),
    (("/v1/proactive", "/v1/device", "/v1/capture", "/v1/dream"), "Proactive"),
    (("/v1/push",), "Push"),
    (("/v1/notify-relay",), "Notify Relay"),
    (("/v1/notices",), "Notices"),
    (("/v1/diagnostics",), "Diagnostics"),
    (("/v1/track",), "Tracking"),
    (("/v1/copytext",), "Copy Text"),
    (("/v1/agent",), "Agent"),
)

TAG_DESCRIPTIONS = {
    "System": "Service status endpoints.",
    "Accounts": "Registration, authentication, recovery, preferences, and access modes.",
    "Onboarding": "Bootstrap, imports, and onboarding state.",
    "Chat": "Encrypted chat messages and hosted-agent conversations.",
    "Model API": "Model provider credentials, routes, and runtime configuration.",
    "MCP": "User-configured MCP servers and connection management.",
    "Memory": "Memory records, buckets, threads, and migration state.",
    "Identity": "Agent identity and relationship state.",
    "Worldbook": "Worldbook entries and contextual matching.",
    "Perception": "Device perception reports, snapshots, and photos.",
    "Screen Context": "Screen frames, sources, summaries, and analysis.",
    "Content": "Encrypted content, key rotation, exports, and account reset.",
    "Proactive": "Proactive jobs, scheduling, capture, and device events.",
    "Push": "Push notification and Live Activity integration.",
    "Notify Relay": (
        "Push relay for self-hosted deployments: enroll a device for a relay "
        "token, then push APNs notifications and Live Activities through the "
        "official backend, which holds the APNs signing key."
    ),
    "Notices": "User-facing service notices.",
    "Diagnostics": "Client diagnostic uploads.",
    "Tracking": "Product telemetry events.",
    "Copy Text": "Shared copy-text state.",
    "Agent": "Agent-facing perception summaries.",
    "Other": "Additional authenticated Feedling APIs.",
}


def _tag_for_path(path: str) -> str:
    for prefixes, tag in TAG_RULES:
        if any(path == prefix or path.startswith(f"{prefix}/") for prefix in prefixes):
            return tag
    return "Other"


def _load_schema() -> dict[str, Any]:
    # Importing the app creates its compatibility data directory. Keep docs
    # generation self-contained and out of the operator's home directory.
    os.environ.setdefault(
        "FEEDLING_DATA_DIR",
        str(Path(tempfile.gettempdir()) / "feedling-openapi"),
    )
    sys.path.insert(0, str(BACKEND))

    from asgi_app import app  # noqa: PLC0415

    return app.openapi()


def _build_public_schema(schema: dict[str, Any]) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    used_tags: set[str] = set()

    for path, path_item in sorted(schema.get("paths", {}).items()):
        if any(path == prefix or path.startswith(f"{prefix}/") for prefix in EXCLUDED_PREFIXES):
            continue

        tag = _tag_for_path(path)
        rendered_item: dict[str, Any] = {}
        for key, value in path_item.items():
            if key.lower() not in HTTP_METHODS or not isinstance(value, dict):
                rendered_item[key] = value
                continue

            operation_key = (key.lower(), path)
            if operation_key in EXCLUDED_OPERATIONS:
                continue

            operation = dict(value)
            operation["tags"] = [tag]
            if operation_key in PUBLIC_OPERATIONS:
                operation["security"] = []
            elif operation_key in API_KEY_ONLY_OPERATIONS:
                operation["security"] = [{"ApiKeyAuth": []}]
            elif operation_key in RELAY_TOKEN_OPERATIONS:
                operation["security"] = [{"RelayTokenAuth": []}]
            rendered_item[key] = operation
            used_tags.add(tag)
        if any(key.lower() in HTTP_METHODS for key in rendered_item):
            paths[path] = rendered_item

    components = dict(schema.get("components", {}))
    security_schemes = dict(components.get("securitySchemes", {}))
    security_schemes.update(
        {
            "ApiKeyAuth": {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Key",
                "description": "Long-lived Feedling user API key.",
            },
            "RuntimeTokenAuth": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Feedling-Runtime-Token",
                "description": "Short-lived scoped token used by hosted runtimes.",
            },
            "RelayTokenAuth": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Relay-Token",
                "description": (
                    "Notify-relay token (nrt_…) issued by "
                    "POST /v1/notify-relay/register. Not an account credential."
                ),
            },
        }
    )
    components["securitySchemes"] = security_schemes

    public_schema = {
        "openapi": schema.get("openapi", "3.1.0"),
        "info": {
            "title": "Feedling API",
            "version": "v1",
            "description": (
                "HTTP API for Feedling accounts, encrypted chat, agent memory, "
                "model routing, perception, and proactive experiences."
            ),
        },
        "servers": [
            {"url": "https://api.feedling.app", "description": "Production"},
            {"url": "https://test-api.feedling.app", "description": "Staging"},
        ],
        "security": [{"ApiKeyAuth": []}, {"RuntimeTokenAuth": []}],
        "tags": [
            {"name": name, "description": TAG_DESCRIPTIONS[name]}
            for name in TAG_DESCRIPTIONS
            if name in used_tags
        ],
        "paths": paths,
        "components": components,
    }
    return apply_public_contracts(public_schema)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    public_schema = _build_public_schema(_load_schema())
    output.write_text(
        json.dumps(public_schema, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(public_schema['paths'])} public paths to {output}")


if __name__ == "__main__":
    main()
