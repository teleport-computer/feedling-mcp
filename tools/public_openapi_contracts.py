"""Documentation-only contracts for legacy raw-``Request`` API handlers.

Most Feedling routes intentionally parse ``Request`` themselves to preserve the
pre-ASGI wire behavior.  FastAPI therefore cannot infer their bodies or query
parameters.  This module enriches the exported *public* schema without changing
runtime parsing, validation, or response serialization.

New routes should prefer typed FastAPI/Pydantic contracts.  Entries here are a
compatibility bridge and are guarded by :func:`validate_public_contract`.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


Operation = tuple[str, str]


BODYLESS_OPERATIONS: set[Operation] = {
    ("post", "/v1/bootstrap"),
    ("post", "/v1/capture/force"),
    ("post", "/v1/genesis/persona_backfill"),
    ("post", "/v1/mcp/servers/{name}/test"),
    ("post", "/v1/model_api/driver"),
    ("post", "/v1/model_api/test"),
    ("post", "/v1/model_api/routes/{route_id}/activate"),
    ("post", "/v1/model_api/routes/{route_id}/test"),
    ("post", "/v1/image-generation/main/test"),
    ("post", "/v1/image-generation/routes/{route_id}/test"),
    ("post", "/v1/vision/main/test"),
    ("post", "/v1/vision/routes/{route_id}/test"),
    ("post", "/v1/proactive/scheduled/fire"),
}


def _schema(type_: str, **kwargs: Any) -> dict[str, Any]:
    return {"type": type_, **kwargs}


def _parameter(
    name: str,
    where: str,
    schema: dict[str, Any],
    description: str,
    *,
    required: bool = False,
    example: Any | None = None,
    deprecated: bool = False,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "name": name,
        "in": where,
        "required": required,
        "description": description,
        "schema": schema,
    }
    if example is not None:
        value["example"] = example
    if deprecated:
        value["deprecated"] = True
    return value


def _query(
    name: str,
    schema: dict[str, Any],
    description: str,
    *,
    required: bool = False,
    example: Any | None = None,
    deprecated: bool = False,
) -> dict[str, Any]:
    return _parameter(
        name,
        "query",
        schema,
        description,
        required=required,
        example=example,
        deprecated=deprecated,
    )


def _header(
    name: str,
    schema: dict[str, Any],
    description: str,
    *,
    required: bool = False,
    example: Any | None = None,
) -> dict[str, Any]:
    return _parameter(
        name,
        "header",
        schema,
        description,
        required=required,
        example=example,
    )


TIMESTAMP = _schema("number", minimum=0)
BOOL = _schema("boolean")


CONSUMER_HEADERS = [
    _header(
        "X-Feedling-Consumer",
        _schema("string", maxLength=160),
        "Resident consumer name. Used when X-Feedling-Consumer-Id is absent.",
        example="feedling-resident",
    ),
    _header(
        "X-Feedling-Consumer-Id",
        _schema("string", maxLength=160),
        "Stable resident consumer identifier.",
        example="resident_mbp_01",
    ),
    _header(
        "X-Feedling-Consumer-Version",
        _schema("string", maxLength=80),
        "Resident consumer release version.",
        example="1.4.0",
    ),
    _header(
        "X-Feedling-Consumer-Commit",
        _schema("string", maxLength=80),
        "Resident consumer source revision.",
        example="a1b2c3d",
    ),
    _header(
        "X-Feedling-Consumer-Capabilities",
        _schema("string", maxLength=500),
        "Comma-separated capabilities advertised by the current official resident poll. "
        "agent_image_generation_v1 means the configured agent entry exposes a callable "
        "native image-generation tool; it is not inferred from VPS deployment alone.",
        example="vision_observer_v1,agent_image_generation_v1",
    ),
]

CONSUMER_COMPAT_HEADERS = [
    _header(
        "X-Feedling-Consumer-Compat-Commit",
        _schema("string", maxLength=80),
        "Optional poll-only backend target that an older running resident "
        "intentionally skipped as irrelevant. It suppresses commit-mismatch "
        "maintenance only when a running commit is present and this value "
        "matches the backend's current expected commit.",
        example="a1b2c3d",
    ),
]

DECRYPT_HEALTH_HEADERS = [
    _header(
        "X-Feedling-Decrypt-Status",
        _schema(
            "string",
            enum=["ok", "degraded", "unconfigured", "unreachable"],
        ),
        "Current resident decrypt-source health. Report this on poll heartbeats; "
        "omitting it on a later official poll clears the previous report to unknown.",
        example="ok",
    ),
    _header(
        "X-Feedling-Decrypt-Checked-At",
        TIMESTAMP,
        "Unix epoch seconds when the resident last confirmed the reported decrypt status. "
        "The backend treats missing, invalid, future, or stale values as unknown.",
        example=1784625600.125,
    ),
]


OPERATION_PARAMETERS: dict[Operation, list[dict[str, Any]]] = {
    ("get", "/v1/chat/poll"): [
        _query("since", TIMESTAMP, "Return messages newer than this Unix timestamp.", example=1783962000.0),
        _query("timeout", _schema("number", minimum=0, maximum=60, default=30), "Long-poll wait in seconds; the server caps it at 60.", example=30),
        _query("consumer_id", _schema("string", maxLength=160), "Stable resident consumer identifier.", example="resident_mbp_01"),
        _query("claim", _schema("boolean", default=True), "Whether this poll may claim queued work.", example=True),
        *CONSUMER_HEADERS,
        *CONSUMER_COMPAT_HEADERS,
        *DECRYPT_HEALTH_HEADERS,
    ],
    ("post", "/v1/chat/response"): [
        _query("consumer_id", _schema("string", maxLength=160), "Stable resident consumer identifier.", example="resident_mbp_01"),
        *CONSUMER_HEADERS,
    ],
    ("get", "/v1/chat/history"): [
        _query("limit", _schema("integer", minimum=1, maximum=200, default=200), "Maximum messages in this page.", example=50),
        _query("after_seq", _schema("integer", minimum=0), "Lossless forward cursor. Return messages whose durable append sequence is greater than this value; takes precedence over timestamp cursors.", example=1042),
        _query("before_seq", _schema("integer", minimum=1), "Lossless backward cursor. Return messages whose durable append sequence is less than this value; takes precedence over timestamp cursors.", example=1042),
        _query("since", TIMESTAMP, "Return messages with ts strictly greater than this watermark.", example=1783962000.0),
        _query("before", TIMESTAMP, "Return older messages with ts strictly less than this watermark; takes precedence over since.", example=1783962000.0),
        _query("include_image_body", _schema("boolean", default=True), "Set false to omit image and oversized inline bodies.", example=False),
        _query("include_image_bodies", BOOL, "Compatibility alias for include_image_body.", deprecated=True),
    ],
    ("get", "/v1/identity/changes"): [
        _query("limit", _schema("integer", minimum=1, maximum=200, default=50), "Maximum changes to return.", example=50),
        _query("since", _schema("string"), "Return changes whose ISO-8601 ts is strictly newer than this value.", example="2026-07-01T00:00:00Z"),
    ],
    ("get", "/v1/memory/list"): [
        _query("limit", _schema("integer", minimum=1, maximum=500, default=50), "Page size; values above 500 are capped.", example=50),
        _query("cursor", _schema("string", minLength=1, maxLength=1024), "Opaque continuation token from next_cursor. Omit for the first page.", example="eyJoIjp0cnVlLCJpIjoibWVtXzEyMyIsInQiOiIyMDI2LTA3LTEzVDEyOjMwOjAwKzAwOjAwIiwidiI6MX0"),
        _query("since", _schema("string"), "Return memories whose occurred_at is at or after this ISO-8601 value.", example="2026-07-01T00:00:00Z"),
        _query("include_archived", _schema("boolean", default=False), "Include archived memories.", example=False),
    ],
    ("get", "/v1/memory/get"): [
        _query("id", _schema("string", minLength=1), "Memory identifier.", required=True, example="mem_abc123"),
    ],
    ("delete", "/v1/memory/delete"): [
        _query("id", _schema("string", minLength=1), "Memory identifier.", required=True, example="mem_abc123"),
    ],
    ("get", "/v1/memory/capture_jobs"): [
        _query("limit", _schema("integer", minimum=1, maximum=100, default=30), "Maximum capture jobs to return.", example=30),
    ],
    ("delete", "/v1/worldbook/delete"): [
        _query("id", _schema("string", minLength=1), "Worldbook entry identifier.", required=True, example="wb_abc123"),
    ],
    ("get", "/v1/agent/perception"): [
        _query("signals", _schema("string"), "Comma-separated agent signal keys to include.", example="now,motion"),
    ],
    ("get", "/v1/agent/perception/trend"): [
        _query("signal", _schema("string", minLength=1), "Agent signal key.", required=True, example="sleep"),
        _query("field", _schema("string", minLength=1), "Optional numeric field to aggregate.", example="deep_minutes"),
        _query("days", _schema("integer", minimum=1, maximum=365, default=30), "Lookback window in days.", example=30),
    ],
    ("get", "/v1/agent/perception/history"): [
        _query("signal", _schema("string", minLength=1), "Agent signal key.", required=True, example="motion"),
        _query("days", _schema("integer", minimum=1, maximum=365, default=14), "Lookback window in days.", example=14),
    ],
    ("get", "/v1/agent/perception/recent_apps"): [
        _query("limit", _schema("integer", minimum=1, maximum=100, default=20), "Maximum merged app open/close events to return.", example=20),
        _query("hours", _schema("number", exclusiveMinimum=0), "Only include app events within the last N hours.", example=24),
    ],
    ("get", "/v1/agent/perception/digest"): [
        _query("days", _schema("integer", minimum=1, maximum=365, default=30), "Lookback window in days.", example=30),
    ],
    ("get", "/v1/genesis/imports"): [
        _query("limit", _schema("integer", minimum=1, maximum=100, default=20), "Maximum import jobs to return.", example=20),
    ],
    ("get", "/v1/genesis/imports/{job_id}"): [
        _query("include_missing", _schema("boolean", default=False), "Include missing chunk sequence numbers.", example=True),
    ],
    ("get", "/v1/genesis/resident/pending"): [
        _query(
            "consumer_id",
            _schema("string", minLength=1, maxLength=160),
            "Stable resident consumer identifier.",
            required=True,
            example="resident_mbp_01",
        ),
    ],
    ("get", "/v1/perception/photos"): [
        _query("limit", _schema("integer", minimum=1, maximum=200, default=20), "Maximum photo evaluations to return.", example=20),
    ],
    ("get", "/v1/perception/items/{kind}"): [
        _query("limit", _schema("integer", minimum=1, maximum=200, default=20), "Maximum compatibility items to return.", example=20),
    ],
    ("get", "/v1/perception/app_open"): [
        _query(
            "app",
            _schema("string", minLength=1),
            "Application name or identifier. The public contract requires this canonical parameter; bundle_id remains a deprecated runtime alias.",
            required=True,
            example="com.apple.MobileSafari",
        ),
        _query("bundle_id", _schema("string"), "Compatibility alias for app.", deprecated=True),
        _query("category", _schema("string"), "Optional application category.", example="browser"),
        _query("ts", TIMESTAMP, "Client event Unix timestamp.", example=1783962000.0),
        _query("client_ts", TIMESTAMP, "Compatibility alias for ts.", deprecated=True),
    ],
    ("get", "/v1/perception/app_close"): [
        _query(
            "app",
            _schema("string", minLength=1),
            "Application name or identifier. The public contract requires this canonical parameter; bundle_id remains a deprecated runtime alias.",
            required=True,
            example="com.apple.MobileSafari",
        ),
        _query("bundle_id", _schema("string"), "Compatibility alias for app.", deprecated=True),
        _query("category", _schema("string"), "Optional application category.", example="browser"),
        _query("ts", TIMESTAMP, "Client event Unix timestamp.", example=1783962000.0),
        _query("client_ts", TIMESTAMP, "Compatibility alias for ts.", deprecated=True),
    ],
    ("get", "/v1/proactive/jobs/poll"): [
        _query("since", TIMESTAMP, "Return jobs newer than this Unix timestamp.", example=1783962000.0),
        _query("timeout", _schema("number", minimum=0, maximum=60, default=30), "Long-poll wait in seconds.", example=30),
        _query("limit", _schema("integer", minimum=1, maximum=100, default=20), "Maximum jobs to return.", example=20),
    ],
    ("get", "/v1/device/events"): [
        _query("since", TIMESTAMP, "Return events newer than this Unix timestamp.", example=1783962000.0),
        _query("limit", _schema("integer", minimum=1, maximum=200, default=100), "Maximum events to return.", example=100),
    ],
    ("get", "/v1/proactive/decisions"): [
        _query("since", TIMESTAMP, "Return decisions newer than this Unix timestamp.", example=1783962000.0),
        _query("limit", _schema("integer", minimum=1, maximum=200, default=100), "Maximum decisions to return.", example=100),
    ],
    ("get", "/v1/proactive/reviews"): [
        _query("since", TIMESTAMP, "Return reviews newer than this Unix timestamp.", example=1783962000.0),
        _query("limit", _schema("integer", minimum=1, maximum=500, default=200), "Maximum reviews to return.", example=200),
    ],
    ("get", "/v1/push/tokens"): [
        _query("active_only", _schema("boolean", default=False), "Return only active push tokens.", example=True),
    ],
    ("get", "/v1/screen/ios"): [
        _query("window_sec", _schema("number", minimum=300, maximum=172800, default=86400), "Recent-event window in seconds.", example=3600),
    ],
    ("get", "/v1/screen/frames"): [
        _query("limit", _schema("integer", minimum=1, maximum=100, default=20), "Maximum frame records to return.", example=20),
    ],
    ("get", "/v1/screen/frames/{frame_id}/decrypt"): [
        _query("include_image", _schema("boolean", default=True), "Include base64 image data in the JSON response.", example=False),
    ],
    ("get", "/v1/screen/frames/{frame_id}/image"): [
        _header(
            "Range",
            _schema("string", pattern=r"^bytes=(?:\d+-\d*|-\d+)$"),
            "Optional single HTTP byte range.",
            example="bytes=0-1048575",
        ),
    ],
    ("get", "/v1/screen/analyze"): [
        _query("window_sec", _schema("number", minimum=30, maximum=3600, default=300), "Analysis lookback in seconds.", example=300),
        _query("min_continuous_min", _schema("number", minimum=1, maximum=120, default=3), "Minimum continuous duration in minutes.", example=3),
    ],
    ("get", "/v1/state/receipts"): [
        _query("limit", _schema("integer", minimum=1, maximum=100, default=30), "Maximum receipts to return.", example=30),
    ],
    ("get", "/v1/notices"): [
        _query("include_resolved", _schema("boolean", default=True), "Include resolved notices from the recent retention window.", example=False),
    ],
    ("get", "/v1/copytext"): [
        _header("If-None-Match", _schema("string"), "Return 304 when the current ETag matches.", example='"copytext-42"'),
    ],
}


CHUNK_METADATA = (
    ("X-Envelope-Meta", "envelope_meta", _schema("string"), "JSON-encoded encrypted-envelope metadata."),
    ("X-Byte-Start", "byte_start", _schema("integer", minimum=0), "Inclusive plaintext byte offset."),
    ("X-Byte-End", "byte_end", _schema("integer", minimum=0), "Exclusive plaintext byte offset."),
    ("X-Content-SHA256", "content_sha256", _schema("string", pattern=r"^[A-Fa-f0-9]{64}$"), "SHA-256 of the plaintext content."),
    ("X-Ciphertext-SHA256", "ciphertext_sha256", _schema("string", pattern=r"^[A-Fa-f0-9]{64}$"), "SHA-256 of this ciphertext chunk."),
)
OPERATION_PARAMETERS[("put", "/v1/genesis/imports/{job_id}/chunks/{seq}")] = [
    *[
        _header(header_name, schema, description)
        for header_name, _query_name, schema, description in CHUNK_METADATA
    ],
    *[
        _query(query_name, schema, f"Compatibility fallback for {header_name}.", deprecated=True)
        for header_name, query_name, schema, _description in CHUNK_METADATA
    ],
]


COMPONENT_SCHEMAS: dict[str, dict[str, Any]] = {
    "VoiceCallCancelRequest": {
        "type": "object",
        "required": ["call_id", "reason"],
        "properties": {
            "call_id": {
                "type": "string",
                "pattern": "^vcall_",
                "maxLength": 96,
            },
            "reason": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "description": (
                    "Bounded client cancellation reason, for example "
                    "user_hangup, connect_failed, transcript_empty, or "
                    "app_terminated."
                ),
            },
        },
        "additionalProperties": False,
    },
    "ModelApiModelsRequest": {
        "type": "object",
        "description": (
            "List a provider's live model catalog. Supply the credential "
            "**exactly one** of two ways: a fresh `api_key`, or a stored "
            "`credential_id` whose provider/base_url are the source of truth "
            "(any provider/base_url sent alongside a credential_id is ignored)."
        ),
        "required": ["provider"],
        "additionalProperties": True,
        # Strict XOR that matches the runtime (setup_core.model_api_models):
        # exactly one of api_key / credential_id must be PRESENT and non-empty.
        # `oneOf` on `required` gives "exactly one present"; `minLength: 1` on the
        # value gives "non-empty"; dropping OpenAPI-3.0 `nullable` forbids an
        # explicit null (this document is 3.1.0, where null is a JSON-Schema type,
        # not a `nullable` flag). So {"api_key":"k","credential_id":null} — which
        # a presence-only oneOf + nullable used to accept while the runtime
        # rejected it — is now invalid on BOTH sides.
        "oneOf": [
            {"required": ["api_key"]},
            {"required": ["credential_id"]},
        ],
        "properties": {
            "provider": {
                "type": "string",
                "enum": [
                    "openai",
                    "openrouter",
                    "anthropic",
                    "bedrock",
                    "gemini",
                    "deepseek",
                    "openai_compatible",
                ],
                "description": "Provider to enumerate. `openai_compatible` requires `base_url`.",
            },
            "base_url": {
                "type": "string",
                "description": (
                    "Override base URL. Required for `openai_compatible`; must be "
                    "`https://` or local `http://127.0.0.1`."
                ),
            },
            "api_key": {
                "type": "string",
                "writeOnly": True,
                "minLength": 1,
                "description": "Fresh provider key. Mutually exclusive with `credential_id`.",
            },
            "credential_id": {
                "type": "string",
                "minLength": 1,
                "description": "Stored credential id. Mutually exclusive with `api_key`.",
            },
        },
        "example": {"provider": "openai", "api_key": "sk-..."},
    },
    "ModelApiModelItem": {
        "type": "object",
        "required": ["id", "display_name"],
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string", "description": "Provider model id (≤160 chars)."},
            "display_name": {"type": "string", "description": "Human label; falls back to id."},
        },
    },
    "ModelApiModelsResponse": {
        "type": "object",
        "description": (
            "The account's visible model catalog for this provider — a live "
            "pull, NOT an io compatibility guarantee. `complete=false` means the "
            "list was truncated (see `warnings`); `catalog_supported=false` means "
            "the provider has no listable catalog (e.g. bedrock, or a custom "
            "endpoint with no `/models`) and the client should fall back to "
            "manual model entry."
        ),
        "required": ["provider", "models", "complete", "catalog_supported", "warnings"],
        "additionalProperties": False,
        "properties": {
            "provider": {"type": "string"},
            "models": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/ModelApiModelItem"},
            },
            "complete": {"type": "boolean"},
            "catalog_supported": {"type": "boolean"},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    },
    "WebSettingsUpdateRequest": {
        "type": "object",
        "description": (
            "Set the user's web-search preference. `enabled` must be a real "
            "JSON boolean — strings such as \"no\" are rejected with 400 "
            "rather than coerced."
        ),
        "required": ["enabled"],
        "additionalProperties": False,
        "properties": {
            "enabled": {
                "type": "boolean",
                "description": "The user's saved preference. Defaults to false.",
            }
        },
    },
    "WebSettingsResponse": {
        "type": "object",
        "description": (
            "Web-search state. `enabled` is the user's saved preference and is "
            "written only by the user — an operator halt never rewrites it. "
            "`runtime_supported` is true when this account's runtime can reach "
            "the web tools: a Runtime V2 tool loop (intrinsic), or a Runtime V1 "
            "consumer — hosted runner OR self-hosted resident — but only once a "
            "new consumer build actually advertises the web capability on a "
            "fresh poll. An old consumer build never claims it, a stale "
            "heartbeat, or any read error all fail closed to false, so the "
            "switch stays inert for that account. `effective` is derived, and "
            "covers every lane: the proactive companion's background turns "
            "follow the same switch as foreground chat."
        ),
        "required": [
            "enabled",
            "runtime_supported",
            "status",
            "effective",
            "tools",
        ],
        "additionalProperties": False,
        "properties": {
            "enabled": {"type": "boolean", "description": "The user's saved preference."},
            "runtime_supported": {
                "type": "boolean",
                "description": "Whether this account's runtime has the web tools at all.",
            },
            "status": {
                "type": "string",
                "enum": ["available", "degraded", "unavailable"],
                "description": (
                    "`degraded` means one of the two tools is halted and the "
                    "other still works — not a synonym for unavailable."
                ),
            },
            "effective": {
                "type": "boolean",
                "description": "Whether this account's turns actually get web tools.",
            },
            "tools": {
                "type": "object",
                "required": ["web_search", "web_fetch"],
                "additionalProperties": False,
                "properties": {
                    "web_search": {"$ref": "#/components/schemas/WebToolState"},
                    "web_fetch": {"$ref": "#/components/schemas/WebToolState"},
                },
            },
        },
    },
    "WebToolState": {
        "type": "object",
        "required": ["available"],
        "additionalProperties": False,
        "properties": {"available": {"type": "boolean"}},
    },
    "WebSearchRequest": {
        "type": "object",
        "description": "Web-search input for POST /v1/agent/web/search.",
        "required": ["query"],
        "additionalProperties": False,
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Search terms. A query that looks like it carries sensitive "
                    "data (email addresses, API keys, phone numbers) is refused "
                    "before any network call — the response is HTTP 200 with "
                    "ok:false and error.code capability_invalid_input."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "default": 5,
                "description": "Maximum results. Clamped to 1..10; defaults to 5.",
            },
        },
        "example": {"query": "weather in Tokyo today", "limit": 5},
    },
    "WebFetchRequest": {
        "type": "object",
        "description": "Web-fetch input for POST /v1/agent/web/fetch.",
        "required": ["url"],
        "additionalProperties": False,
        "properties": {
            "url": {
                "type": "string",
                "format": "uri",
                "description": (
                    "Absolute http(s) URL. Private, loopback, and link-local "
                    "targets are refused, and every redirect hop (up to 5) is "
                    "independently re-validated. HTML is reduced to readable "
                    "text; non-HTML (JSON, plain text, source) is returned as-is. "
                    "The response body is size-capped and flags truncation."
                ),
            }
        },
        "example": {"url": "https://example.com/article"},
    },
    "WebCapabilityResult": {
        "type": "object",
        "description": (
            "Uniform capability envelope returned by the V1 web execution "
            "endpoints. ALWAYS delivered as HTTP 200 — a transport 200 does NOT "
            "mean the call succeeded, so branch on `ok`, never on the HTTP "
            "status. When ok is true, `data` holds the tool payload (search: "
            "`{query, results[], truncated}`; fetch: `{url, text, truncated}`). "
            "When ok is false, `error.code` is a stable slug: capability_disabled "
            "(the user's web switch is off — not retryable), "
            "capability_rate_limited (per-user budget spent — retryable), "
            "capability_invalid_input (missing/sensitive query, or a bad/blocked "
            "url), capability_unavailable (operator halt), capability_not_found / "
            "capability_upstream_error (upstream fetch status)."
        ),
        "required": ["ok"],
        "additionalProperties": True,
        "properties": {
            "ok": {"type": "boolean", "description": "True only when the tool ran and produced a result."},
            "data": {
                "type": "object",
                "additionalProperties": True,
                "description": "Tool payload; present when ok is true.",
            },
            "error": {
                "type": "object",
                "required": ["code", "message", "retryable"],
                "additionalProperties": True,
                "description": "Present when ok is false.",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "Stable error slug: capability_disabled, "
                            "capability_rate_limited, capability_invalid_input, "
                            "capability_unavailable, capability_not_found, "
                            "capability_upstream_error, capability_forbidden."
                        ),
                    },
                    "message": {"type": "string"},
                    "retryable": {"type": "boolean"},
                },
            },
            "trace": {"type": "object", "additionalProperties": True},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "example": {
            "ok": True,
            "data": {
                "query": "weather in Tokyo today",
                "truncated": False,
                "results": [
                    {"title": "Tokyo weather", "url": "https://example.com/tokyo", "snippet": "…"}
                ],
            },
            "trace": {},
            "warnings": [],
        },
    },
    "FreeFormJsonObject": {
        "type": "object",
        "description": "Legacy compatibility payload. Consult the operation description before sending fields.",
        "additionalProperties": True,
    },
    "GenericJsonResponse": {
        "type": "object",
        "description": "JSON response object. Endpoint-specific fields may be added compatibly.",
        "additionalProperties": True,
    },
    "MemoryListResponse": {
        "type": "object",
        "description": (
            "One page of raw encrypted Memory Garden records. total is the "
            "filtered full count and is independent of limit and cursor."
        ),
        "required": ["moments", "total", "next_cursor"],
        "additionalProperties": False,
        "properties": {
            "moments": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/FreeFormJsonObject"},
            },
            "total": {"type": "integer", "minimum": 0},
            "next_cursor": {
                "anyOf": [
                    {"type": "string", "minLength": 1, "maxLength": 1024},
                    {"type": "null"},
                ],
                "description": "Opaque continuation token; null exactly when the filtered result is exhausted.",
            },
        },
    },
    "DreamStatusResponse": {
        "type": "object",
        "description": (
            "Memory Garden banner status. Dream counts describe the latest "
            "completed Dream. Capture fields expose a monotonic total of cards "
            "added by Capture; clients subtract the total saved when the user "
            "last dismissed the banner. Legal zero-card Capture runs do not "
            "reset or refresh the fields."
        ),
        "required": [
            "dreaming",
            "last_completed_at",
            "organized_count",
            "merged_count",
            "capture_completed_at",
            "capture_cards_added",
        ],
        "additionalProperties": False,
        "properties": {
            "dreaming": {"type": "boolean"},
            "last_completed_at": TIMESTAMP,
            "organized_count": {"type": "integer", "minimum": 0},
            "merged_count": {"type": "integer", "minimum": 0},
            "capture_completed_at": {
                **TIMESTAMP,
                "description": (
                    "Unix timestamp of the latest Capture completion that "
                    "actually added at least one card; zero when none exists."
                ),
            },
            "capture_cards_added": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Monotonic total of cards actually added across Capture runs."
                ),
            },
        },
    },
    # Pin the FastAPI-generated ValidationError shape so the published contract
    # doesn't gain/lose `input`/`ctx` with the fastapi/pydantic version of
    # whoever runs the exporter (older builds omit them, newer include them).
    # Fixing it here keeps `npm run openapi:generate` deterministic regardless
    # of the generating environment.
    "ValidationError": {
        "type": "object",
        "required": ["loc", "msg", "type"],
        "properties": {
            "loc": {
                "type": "array",
                "items": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
                "title": "Location",
            },
            "msg": {"type": "string", "title": "Message"},
            "type": {"type": "string", "title": "Error Type"},
            "input": {"title": "Input"},
            "ctx": {"type": "object", "title": "Context"},
        },
        "title": "ValidationError",
    },
    "ErrorResponse": {
        "type": "object",
        "required": ["error"],
        "properties": {
            "error": {
                "description": "Machine-readable error slug, or the legacy structured MCP error.",
                "oneOf": [
                    {"type": "string"},
                    {
                        "type": "object",
                        "required": ["kind"],
                        "properties": {
                            "kind": {"type": "string"},
                            "detail": {"type": "string"},
                        },
                        "additionalProperties": True,
                    },
                ],
            },
            "detail": {
                "description": "Human-readable context or structured validation details; do not branch on prose.",
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {}},
                    {"type": "object", "additionalProperties": True},
                ],
            },
            "blame": {
                "type": "string",
                "enum": ["user_provider", "provider_transient", "user_environment", "system"],
                "description": "Stable responsibility classification when the endpoint can identify it.",
            },
            "request_id": {"type": "string", "description": "Support correlation identifier when available."},
        },
        "additionalProperties": True,
        "example": {"error": "invalid_payload", "detail": "The request body is invalid", "request_id": "req_abc123"},
    },
    "ChatBootstrapIncompleteResponse": {
        "type": "object",
        "required": ["error", "stage", "retryable"],
        "properties": {
            "error": {"type": "string", "const": "bootstrap_incomplete"},
            "stage": {
                "type": "string",
                "enum": [
                    "needs_resident_consumer",
                    "needs_decrypt_source",
                    "needs_live_connection",
                ],
            },
            "retryable": {
                "type": "boolean",
                "description": (
                    "True only when the current resident response may be retried "
                    "without repeating onboarding. Missing is false for compatibility "
                    "with older servers."
                ),
            },
        },
        "allOf": [
            {
                "if": {"properties": {"stage": {"const": "needs_resident_consumer"}}},
                "then": {"properties": {"retryable": {"const": True}}},
                "else": {"properties": {"retryable": {"const": False}}},
            },
        ],
        "additionalProperties": True,
    },
    "EncryptedEnvelope": {
        "type": "object",
        "required": ["visibility", "owner_user_id"],
        "properties": {
            "v": {"type": "integer", "const": 1, "default": 1},
            "id": {"type": "string"},
            "body_ct": {"type": "string", "description": "Base64 ciphertext."},
            "body_b64": {
                "type": "string",
                "format": "byte",
                "description": "Base64 plaintext bytes. Accepted by binary content "
                               "surfaces (Chat image/file, Perception photo, and "
                               "screen-frame ingest) when content_encryption_effective "
                               "is \"off\"; text-envelope surfaces reject this shape. "
                               "Mutually exclusive with body_ct and body.",
            },
            "body_size_bytes": {
                "type": "integer",
                "minimum": 0,
                "description": "Optional decoded byte count for body_b64. When "
                               "provided it must match exactly.",
            },
            "body": {
                "type": "string",
                "description": "Plaintext body. Only valid when the caller's "
                               "content_encryption_effective (see /v1/users/whoami) "
                               "is \"off\"; otherwise the write is rejected with "
                               "plaintext_envelope_not_enabled_for_this_account. "
                               "Mutually exclusive with body_ct.",
            },
            "nonce": {"type": "string", "description": "Base64 nonce."},
            "K_user": {"type": "string", "description": "Content key wrapped for the user."},
            "K_enclave": {"type": "string", "description": "Content key wrapped for the enclave; required for shared visibility."},
            "visibility": {"type": "string", "enum": ["shared", "local_only"]},
            "owner_user_id": {"type": "string"},
            "enclave_pk_fpr": {"type": "string"},
            "content_pk_fpr": {
                "type": "string",
                "description": "Optional label: first 16 hex chars of SHA-256 of the "
                               "user public key K_user was sealed to. When present on a "
                               "chat write and it does not match the user's currently "
                               "registered content key, the write is rejected with 409 "
                               "content_pk_fpr_mismatch — re-fetch whoami and re-seal.",
            },
        },
        "oneOf": [
            {
                "title": "Encrypted body",
                "required": ["body_ct", "nonce", "K_user"],
                "if": {
                    "properties": {"visibility": {"const": "shared"}},
                    "required": ["visibility"],
                },
                "then": {"required": ["K_enclave"]},
            },
            {
                "title": "Plaintext body",
                "description": "local_only requires encryption, so this branch pins "
                               "visibility to shared.",
                "required": ["body"],
                "properties": {"visibility": {"const": "shared"}},
            },
            {
                "title": "Binary plaintext body",
                "description": "Only supported binary content paths accept this "
                               "branch, and only while the account's effective tier "
                               "is off.",
                "required": ["body_b64"],
                "properties": {"visibility": {"const": "shared"}},
            },
        ],
        "additionalProperties": True,
        "example": {
            "v": 1,
            "id": "env_abc123",
            "body_ct": "BASE64_CIPHERTEXT",
            "nonce": "BASE64_NONCE",
            "K_user": "BASE64_KEY_WRAPPED_TO_USER",
            "K_enclave": "BASE64_KEY_WRAPPED_TO_ENCLAVE",
            "visibility": "shared",
            "owner_user_id": "usr_0123456789abcdef",
        },
    },
    "MemoryEnvelope": {
        "type": "object",
        "required": ["visibility", "owner_user_id", "type", "occurred_at"],
        "properties": {
            "v": {"type": "integer", "const": 1, "default": 1},
            "id": {"type": "string"},
            "body_ct": {"type": "string", "description": "Base64 ciphertext."},
            "body": {
                "type": "string",
                "description": "Plaintext body. Only valid when the caller's "
                               "content_encryption_effective (see /v1/users/whoami) "
                               "is \"off\"; otherwise the write is rejected with "
                               "plaintext_envelope_not_enabled_for_this_account. "
                               "Mutually exclusive with body_ct.",
            },
            "nonce": {"type": "string", "description": "Base64 nonce."},
            "K_user": {"type": "string", "description": "Content key wrapped for the user."},
            "K_enclave": {"type": "string", "description": "Content key wrapped for the enclave; required for shared visibility."},
            "visibility": {"type": "string", "enum": ["shared", "local_only"]},
            "owner_user_id": {"type": "string"},
            "enclave_pk_fpr": {"type": "string"},
            "type": {"type": "string", "enum": ["moment", "quote", "fact", "event", "insight", "reflection"]},
            "occurred_at": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Plaintext ISO-8601 event time. Accepted datetime values are "
                    "normalized to the equivalent UTC value with a Z suffix; "
                    "date-only values remain date-only."
                ),
            },
            "source": {
                "type": "string",
                "enum": [
                    "bootstrap",
                    "chat",
                    "genesis_import",
                    "genesis_resident_distill",
                    "history_import",
                    "hosted_runtime_state",
                    "live_conversation",
                    "memory_capture",
                    "memory_dream",
                    "memory_migrate",
                    "model_api_capture",
                    "model_api_correction",
                    "model_api_repair",
                    "ombre_brain_sync",
                    "resident_absorb",
                    "resident_patch",
                ],
                "default": "live_conversation",
            },
            "anchor_memory_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
        },
        "oneOf": [
            {
                "title": "Encrypted body",
                "required": ["body_ct", "nonce", "K_user"],
                "if": {
                    "properties": {"visibility": {"const": "shared"}},
                    "required": ["visibility"],
                },
                "then": {"required": ["K_enclave"]},
            },
            {
                "title": "Plaintext body",
                "description": "local_only requires encryption, so this branch pins "
                               "visibility to shared.",
                "required": ["body"],
                "properties": {"visibility": {"const": "shared"}},
            },
        ],
        "additionalProperties": True,
        "example": {
            "v": 1,
            "id": "mem_abc123",
            "body_ct": "BASE64_CIPHERTEXT",
            "nonce": "BASE64_NONCE",
            "K_user": "BASE64_KEY_WRAPPED_TO_USER",
            "K_enclave": "BASE64_KEY_WRAPPED_TO_ENCLAVE",
            "visibility": "shared",
            "owner_user_id": "usr_0123456789abcdef",
            "type": "fact",
            "occurred_at": "2026-07-13T14:30:00Z",
        },
    },
    "RegisterRequest": {
        "type": "object",
        "properties": {
            "public_key": {"type": "string", "description": "Base64 content-encryption public key."},
            "archive_language": {"type": "string", "example": "en"},
            "access_mode": {"type": "string", "enum": ["resident", "model_api", "official_import"], "default": "official_import"},
            "label": {"type": "string", "maxLength": 80},
        },
        "additionalProperties": False,
    },
    "RegisterResponse": {
        "type": "object",
        "required": ["user_id", "principal_id", "api_key"],
        "properties": {
            "user_id": {"type": "string"},
            "principal_id": {"type": "string"},
            "api_key": {"type": "string", "description": "Shown once; store it securely."},
            "public_key": {"type": "string"},
            "access_mode": {"type": "string"},
        },
        "additionalProperties": True,
    },
    "LinkTokenRequest": {
        "type": "object",
        "properties": {
            "access_mode": {"type": "string", "enum": ["resident", "model_api", "official_import"]},
            "label": {"type": "string", "maxLength": 80},
        },
        "additionalProperties": False,
    },
    "ClaimTokenRequest": {
        "type": "object",
        "required": ["token"],
        "properties": {
            "token": {"type": "string", "minLength": 1},
            "label": {"type": "string", "maxLength": 80},
            "client_label": {"type": "string", "maxLength": 80, "deprecated": True},
            "public_key": {"type": "string"},
            "archive_language": {"type": "string"},
            "make_active": {"type": "boolean", "default": True},
        },
        "additionalProperties": False,
    },
    "LinkTokenResponse": {
        "type": "object",
        "required": ["token", "token_id", "access_mode", "expires_at", "expires_in_seconds"],
        "properties": {
            "token": {"type": "string", "description": "One-time bearer token; handle as a secret."},
            "token_id": {"type": "string"},
            "access_mode": {"type": "string"},
            "route": {"type": "string"},
            "label": {"type": "string"},
            "expires_at": {"type": "string"},
            "expires_in_seconds": {"type": "integer", "example": 900},
            "claim_endpoint": {"type": "string", "const": "/v1/access/claim-token"},
        },
        "additionalProperties": True,
    },
    "IssuedApiKeyResponse": {
        "type": "object",
        "required": ["user_id", "principal_id", "api_key"],
        "properties": {
            "user_id": {"type": "string"},
            "principal_id": {"type": "string"},
            "api_key": {"type": "string", "description": "New key shown once; existing keys remain active."},
            "key_id": {"type": "string"},
            "access_mode": {"type": "string"},
            "route": {"type": "string"},
            "active_route": {"type": "string"},
            "public_key": {"type": "string"},
            "status": {"type": "string"},
        },
        "additionalProperties": True,
    },
    "RecoverChallengeRequest": {
        "type": "object",
        "required": ["public_key"],
        "properties": {"public_key": {"type": "string", "description": "Base64 content-encryption public key."}},
        "additionalProperties": False,
    },
    "RecoverVerifyRequest": {
        "type": "object",
        "required": ["challenge_id", "answer"],
        "properties": {
            "challenge_id": {"type": "string"},
            "answer": {"type": "string", "description": "Plaintext recovered by decrypting the challenge envelope locally."},
        },
        "additionalProperties": False,
    },
    "RecoverChallengeResponse": {
        "type": "object",
        "required": ["challenge_id", "envelope"],
        "properties": {
            "challenge_id": {"type": "string"},
            "envelope": {"$ref": "#/components/schemas/EncryptedEnvelope"},
        },
        "additionalProperties": False,
    },
    "AccountResetRequest": {
        "type": "object",
        "required": ["confirm"],
        "properties": {"confirm": {"type": "string", "const": "delete-all-data"}},
        "additionalProperties": False,
    },
    "ChatContextReference": {
        "type": "object",
        "required": ["type", "id"],
        "properties": {
            "type": {"type": "string", "maxLength": 40, "example": "memory"},
            "id": {"type": "string", "maxLength": 160, "example": "mem_abc123"},
            "title": {"type": "string", "maxLength": 240, "example": "Morning preferences"},
        },
        "additionalProperties": False,
    },
    "HostedChatSendRequest": {
        "type": "object",
        "properties": {
            "client_msg_id": {
                "type": "string",
                "format": "uuid",
                "description": "Optional logical-send identifier. Reusing it for the same user within 600 seconds returns the first stored user message without starting another turn.",
            },
            "message": {"type": "string", "maxLength": 12000, "example": "Help me plan tomorrow."},
            "content": {"type": "string", "maxLength": 12000, "deprecated": True, "description": "Compatibility alias for message."},
            "context_refs": {"type": "array", "maxItems": 8, "items": {"$ref": "#/components/schemas/ChatContextReference"}},
            "include_reasoning": {
                "type": "boolean",
                "default": False,
                "description": "Request the assistant's per-turn thinking for this Hosted Runtime V2 turn. With the self-authored thinking chain enabled (the default), the returned thinking is io's own first-person summary rather than the provider's raw chain-of-thought, falling back to native reasoning when no self-authored block was produced. Omitted values preserve the historical disabled behavior; resident runtimes ignore this field.",
            },
            "image_b64": {"type": "string", "contentEncoding": "base64", "description": "Image data; decoded size must not exceed 2,000,000 bytes."},
            "image_base64": {"type": "string", "contentEncoding": "base64", "deprecated": True},
            "image_mime": {"type": "string", "enum": ["image/jpeg", "image/png", "image/webp", "image/gif"]},
            "file_b64": {"type": "string", "contentEncoding": "base64", "description": "File data; decoded size must not exceed 26,214,400 bytes."},
            "file_name": {"type": "string", "maxLength": 120},
            "file_mime": {"type": "string"},
        },
        "anyOf": [
            {"required": ["message"]},
            {"required": ["content"]},
            {"required": ["image_b64"]},
            {"required": ["image_base64"]},
            {"required": ["file_b64"]},
        ],
        "additionalProperties": True,
        "example": {
            "message": "Help me plan tomorrow.",
            "context_refs": [{"type": "memory", "id": "mem_abc123", "title": "Morning preferences"}],
        },
    },
    "ModelApiRuntimeErrorRequest": {
        "type": "object",
        "properties": {
            "error": {
                "type": "string",
                "maxLength": 300,
                "description": "Latest runtime error detail, or an empty string to clear it.",
            },
            "error_class": {
                "type": "string",
                "maxLength": 64,
                "description": "Stable classified error name.",
            },
            "provider_result": {
                "type": "string",
                "enum": ["success", "failure"],
                "description": (
                    "Optional provider-call outcome. Residents report success "
                    "for a usable provider reply and failure with error_class."
                ),
            },
        },
        "additionalProperties": True,
        "example": {
            "error": "provider_http_402 insufficient balance",
            "error_class": "quota_insufficient",
            "provider_result": "failure",
        },
    },
    "VisionConfigUpdateRequest": {
        "type": "object",
        "required": ["mode"],
        "properties": {
            "mode": {"type": "string", "enum": ["follow_main", "dedicated"]},
            "route_id": {
                "type": "string",
                "format": "uuid",
                "description": "Required when mode is dedicated.",
            },
        },
        "if": {"properties": {"mode": {"const": "dedicated"}}, "required": ["mode"]},
        "then": {"required": ["route_id"]},
        "additionalProperties": False,
    },
    "VisionRouteCreateRequest": {
        "type": "object",
        "required": ["model"],
        "properties": {
            "provider": {"type": "string"},
            "model": {"type": "string", "minLength": 1},
            "api_key": {"type": "string", "minLength": 1, "writeOnly": True},
            "credential_id": {"type": "string", "format": "uuid"},
            "label": {"type": "string"},
            "base_url": {"type": "string", "format": "uri"},
            "context_window_tokens": {"type": "integer", "minimum": 8192},
        },
        "oneOf": [
            {"required": ["api_key"], "not": {"required": ["credential_id"]}},
            {"required": ["credential_id"], "not": {"required": ["api_key"]}},
        ],
        "additionalProperties": False,
    },
    "ImageGenerationConfigUpdateRequest": {
        "type": "object",
        "required": ["mode"],
        "properties": {
            "mode": {"type": "string", "enum": ["follow_main", "dedicated"]},
            "route_id": {
                "type": "string",
                "format": "uuid",
                "description": "Required when mode is dedicated.",
            },
        },
        "if": {"properties": {"mode": {"const": "dedicated"}}, "required": ["mode"]},
        "then": {"required": ["route_id"]},
        "additionalProperties": False,
    },
    "ImageGenerationRouteCreateRequest": {
        "type": "object",
        "required": ["model"],
        "properties": {
            "provider": {"type": "string"},
            "model": {"type": "string", "minLength": 1},
            "api_key": {"type": "string", "minLength": 1, "writeOnly": True},
            "credential_id": {"type": "string", "format": "uuid"},
            "label": {"type": "string"},
            "base_url": {"type": "string", "format": "uri"},
            "context_window_tokens": {"type": "integer", "minimum": 8192},
        },
        "oneOf": [
            {"required": ["api_key"], "not": {"required": ["credential_id"]}},
            {"required": ["credential_id"], "not": {"required": ["api_key"]}},
        ],
        "additionalProperties": False,
    },
    "ImageGenerationRequest": {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string", "minLength": 1, "maxLength": 8000},
        },
        "additionalProperties": False,
    },
    "VisionObserveRequest": {
        "type": "object",
        "required": ["message_id", "route_id"],
        "properties": {
            "message_id": {"type": "string", "minLength": 1},
            "route_id": {"type": "string", "minLength": 1},
        },
        "additionalProperties": False,
    },
    "VisionMainTestResponse": {
        "type": "object",
        "required": ["status", "source", "provider", "model"],
        "properties": {
            "status": {
                "type": "string",
                "enum": ["ok", "unsupported", "failed", "untested", "testing"],
            },
            "source": {"type": "string", "enum": ["model_api", "resident"]},
            "provider": {"type": "string"},
            "model": {"type": "string"},
            "error_code": {"type": "string"},
            "retryable": {"type": "boolean"},
            "status_code": {"type": "integer"},
            "detail": {"type": "string"},
            "probe_id": {"type": "string"},
            "expires_at_epoch": {"type": "number"},
            "poll_after_ms": {"type": "integer", "minimum": 0},
        },
        "additionalProperties": False,
    },
    "HostedChatAcceptedResponse": {
        "type": "object",
        "required": ["status", "reply_ready", "user_message", "runtime"],
        "properties": {
            "status": {"type": "string", "const": "processing"},
            "reply_ready": {"type": "boolean"},
            "user_message": {"$ref": "#/components/schemas/ChatMessagePointer"},
            "assistant_message": {"$ref": "#/components/schemas/ChatMessagePointer"},
            "runtime": {"type": "object", "additionalProperties": True},
        },
        "additionalProperties": True,
    },
    "ModelApiUsageMetricAmount": {
        "type": "object",
        "description": "One line item within a metric's `amounts` breakdown (e.g. one currency balance among several).",
        "required": ["amount", "unit"],
        "additionalProperties": False,
        "properties": {
            "amount": {
                "oneOf": [{"type": "number"}, {"type": "string"}],
                "description": "Provider-native numeric value. Kept as a string when the provider returns it that way (e.g. DeepSeek's \"25.06\") to avoid float rounding.",
            },
            "unit": {"type": "string", "description": "Currency or unit code as reported by the provider (e.g. \"USD\", \"CNY\", or a provider-defined quota unit)."},
        },
    },
    "ModelApiUsageMetric": {
        "type": "object",
        "description": "One usage metric. `status` is always present; the remaining fields are populated only when relevant, and vary by provider/adapter.",
        "required": ["status"],
        "additionalProperties": False,
        "properties": {
            "status": {
                "type": "string",
                "enum": ["ok", "unsupported", "failed"],
                "description": "`unsupported` — this provider/adapter never reports this metric. `failed` — the provider was queried but the call failed; see `reason`.",
            },
            "amount": {
                "oneOf": [{"type": "number"}, {"type": "string"}],
                "description": "Single scalar value, used when the metric is not split into multiple line items.",
            },
            "amounts": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/ModelApiUsageMetricAmount"},
                "description": "Multi-currency/multi-line breakdown, used instead of `amount` when the provider reports more than one line (e.g. DeepSeek balance per currency).",
            },
            "unit": {"type": "string", "description": "Currency or unit code, when `amount` (not `amounts`) is used."},
            "scope": {"type": "string", "description": "What the figure is scoped to, e.g. \"account\" or \"api_key\", when the provider distinguishes them."},
            "timezone": {"type": "string", "description": "Timezone the provider uses to bucket this figure, e.g. \"UTC\" for usage_today/usage_month."},
            "reason": {"type": "string", "description": "Machine-readable failure slug when status is \"failed\" (e.g. usage_timeout, usage_unreachable, usage_bad_response, usage_http_429)."},
        },
    },
    "ModelApiUsageResponse": {
        "type": "object",
        "description": (
            "Live provider balance/usage snapshot — queried from the provider on every call, never "
            "cached. Field support depends entirely on the configured provider/adapter: DeepSeek and "
            "OpenRouter are queried through their official APIs, and an openai_compatible relay/中转站 "
            "is queried through its dashboard-style endpoints when reachable. Metrics the adapter does "
            "not support report status \"unsupported\" rather than being omitted."
        ),
        "required": ["provider", "adapter", "status", "as_of", "metrics"],
        "additionalProperties": False,
        "properties": {
            "provider": {"type": "string", "description": "Normalized provider name from the caller's active model_api route (e.g. \"deepseek\", \"openrouter\", \"openai_compatible\")."},
            "adapter": {
                "type": "string",
                "enum": [
                    "deepseek", "openrouter", "relay", "unsupported",
                    "deepseek_balance", "openrouter_key", "relay_token", "relay_dashboard",
                ],
                "description": "Which billing adapter actually served this response. On success this is fine-grained (deepseek_balance, openrouter_key, relay_token, relay_dashboard — the specific endpoint(s) queried); the coarse deepseek/openrouter/relay/unsupported values only appear on failure paths (unsupported provider, blocked URL, or timeout before an adapter ran).",
            },
            "status": {
                "type": "string",
                "enum": ["ok", "partial", "error"],
                "description": "\"ok\" — every queried metric succeeded. \"partial\" — some metrics succeeded and some failed. \"error\" — every queried metric failed (or the adapter is unsupported); see `error`.",
            },
            "as_of": {"type": "string", "format": "date-time", "description": "ISO 8601 UTC timestamp of this query, not a cache time — every call re-queries the provider."},
            "metrics": {
                "type": "object",
                "description": "Always all five fixed keys, regardless of provider support.",
                "required": ["balance", "remaining", "usage_total", "usage_today", "usage_month"],
                "additionalProperties": False,
                "properties": {
                    "balance": {"$ref": "#/components/schemas/ModelApiUsageMetric"},
                    "remaining": {"$ref": "#/components/schemas/ModelApiUsageMetric"},
                    "usage_total": {"$ref": "#/components/schemas/ModelApiUsageMetric"},
                    "usage_today": {"$ref": "#/components/schemas/ModelApiUsageMetric"},
                    "usage_month": {"$ref": "#/components/schemas/ModelApiUsageMetric"},
                },
            },
            "error": {
                "type": "string",
                "description": "Top-level failure slug, present when status is \"error\" (e.g. usage_unsupported_provider, usage_blocked_url, usage_timeout) or when every attempted metric failed for the same reason.",
            },
        },
    },
    "ChatMessagePointer": {
        "type": "object",
        "required": ["id", "ts"],
        "properties": {"id": {"type": "string"}, "ts": {"type": "number"}},
        "additionalProperties": True,
    },
    "ChatTransportRequest": {
        "type": "object",
        "required": ["envelope"],
        "properties": {
            "envelope": {
                "allOf": [{"$ref": "#/components/schemas/EncryptedEnvelope"}],
                "description": "For content_type image/file, plaintext-tier clients "
                               "may send body_b64 instead of an encrypted body_ct "
                               "envelope. Text messages do not accept body_b64.",
            },
            "client_msg_id": {
                "type": "string",
                "format": "uuid",
                "description": "Optional logical-send identifier. Reusing it for the same user within 600 seconds returns the first stored row and emits no second consumer wake.",
            },
            "content_type": {"type": "string", "enum": ["text", "image", "file"], "default": "text"},
            "file_name": {"type": "string"},
            "file_mime": {"type": "string"},
            "caption_envelope": {
                "allOf": [{"$ref": "#/components/schemas/EncryptedEnvelope"}],
                "description": "Optional shape-aware user text sent alongside an image/file. It follows content_encryption_effective independently of the binary main envelope. Ignored for content_type=text.",
            },
            "context_refs": {
                "type": "array",
                "maxItems": 8,
                "description": "Optional plaintext routing metadata: memory references the user explicitly attached to this turn (Garden 「talk in chat」). Same contract as the hosted send: only the first eight entries are considered, only type=memory is honored, and ids only are persisted (as quoted_memory_ids) for the enclave to expand into decrypted memory context. Titles are for client display and are not stored.",
                "items": {"$ref": "#/components/schemas/ChatContextReference"},
            },
        },
        "additionalProperties": True,
    },
    "ChatFileFollowup": {
        "type": "object",
        "required": ["envelope", "file_name", "file_mime", "file_byte_count"],
        "properties": {
            "envelope": {"$ref": "#/components/schemas/EncryptedEnvelope"},
            "file_name": {"type": "string", "minLength": 1, "maxLength": 120},
            "file_mime": {"type": "string", "minLength": 1, "maxLength": 120},
            "file_byte_count": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000000,
            },
        },
        "additionalProperties": False,
    },
    "ChatImageFollowup": {
        "type": "object",
        "required": ["envelope", "image_mime", "image_byte_count"],
        "properties": {
            "envelope": {"$ref": "#/components/schemas/EncryptedEnvelope"},
            "image_mime": {
                "type": "string",
                "enum": ["image/jpeg", "image/png", "image/webp"],
            },
            "image_byte_count": {
                "type": "integer",
                "minimum": 1,
                "maximum": 2000000,
            },
        },
        "additionalProperties": False,
    },
    "ChatResponseRequest": {
        "type": "object",
        "required": ["envelope"],
        "properties": {
            "envelope": {
                "allOf": [{"$ref": "#/components/schemas/EncryptedEnvelope"}],
                "description": "For content_type image, plaintext-tier clients may "
                               "send body_b64 instead of an encrypted body_ct "
                               "envelope. Text replies do not accept body_b64.",
            },
            "source": {
                "type": "string",
                "enum": [
                    "chat",
                    "live_activity",
                    "heartbeat",
                    "verify_ping",
                    "resident_maintenance",
                    "agent_initiated_proactive",
                ],
                "default": "chat",
                "description": "Resident protocol source. Ordinary replies should use chat.",
            },
            "reply_to_message_id": {"type": "string", "description": "Parent user message; strongly recommended for duplicate-reply protection."},
            "content_type": {"type": "string", "enum": ["text", "image"], "default": "text"},
            "file_followups": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "items": {"$ref": "#/components/schemas/ChatFileFollowup"},
                "description": "Optional encrypted download cards committed atomically after a text primary. Chat source only; reply_to_message_id is required. The order in this array is the display order below the primary reply.",
            },
            "image_followups": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {"$ref": "#/components/schemas/ChatImageFollowup"},
                "description": "Optional encrypted generated images committed atomically after a text primary. Each row is returned as content_type=image and renders directly in Chat. Chat source only; reply_to_message_id is required.",
            },
            "turn_failure_error_class": {
                "type": "string",
                "maxLength": 64,
                "description": (
                    "Set only when this reply is a fallback standing in for a turn the agent "
                    "could not answer. Carries the classified failure so the client can show the "
                    "real reason on the user's own message instead of the generic fallback line. "
                    "Omit on successful replies."
                ),
            },
            "turn_failure_blame": {
                "type": "string",
                "enum": ["user_provider", "provider_transient", "system"],
                "readOnly": True,
                "description": (
                    "Who the failure belongs to. Server-derived from error_class via the "
                    "notices catalog — a value sent here is NOT trusted (an unknown class "
                    "always falls back to system, so our own faults are never blamed on the "
                    "user). user_provider is actionable by the user (top up / fix key / fix "
                    "model name); system must never point the user at their own configuration."
                ),
            },
            "turn_failure_user_text": {
                "type": "string",
                "maxLength": 500,
                "readOnly": True,
                "description": (
                    "User-facing copy for the failure. Server-derived from error_class via the "
                    "notices catalog — a value sent here is NOT trusted, so the response never "
                    "carries raw provider detail (which can hold provider HTML, request ids or "
                    "sensitive context)."
                ),
            },
        },
        "additionalProperties": True,
    },
    "ChatActivityEventRequest": {
        "type": "object",
        "required": ["activity_id", "tool_name", "state"],
        "properties": {
            "activity_id": {"type": "string", "maxLength": 160},
            "call_id": {"type": "string", "maxLength": 160},
            "tool_name": {"type": "string", "maxLength": 120},
            "state": {"type": "string", "enum": ["running", "success", "failure"]},
            "duration_ms": {"type": "number", "minimum": 0},
            "result_code": {
                "type": "string",
                "maxLength": 64,
                "description": (
                    "Display-safe result token. Failed generate_image events "
                    "preserve an allowlisted image_generation_* code; result "
                    "bodies are never included."
                ),
            },
            "memory_count": {"type": "integer", "minimum": 0, "maximum": 1000},
            "memory_categories": {
                "type": "array",
                "description": "Complete canonical breakdown only; counts must sum to memory_count.",
                "items": {
                    "type": "object",
                    "required": ["key", "count"],
                    "properties": {
                        "key": {
                            "type": "string",
                            "enum": [
                                "work", "growth", "family", "friends", "pets",
                                "relationship", "feelings", "preferences", "values",
                                "health", "interests", "money", "food", "travel",
                            ],
                        },
                        "count": {"type": "integer", "minimum": 1},
                    },
                    "additionalProperties": False,
                },
            },
        },
        "additionalProperties": False,
    },
    "ChatHistoryClearRequest": {
        "type": "object",
        "required": ["confirm"],
        "properties": {"confirm": {"type": "string", "const": "clear-chat-history"}},
        "additionalProperties": False,
    },
    "MemoryIndexRequest": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 0, "description": "0 or omitted requests the deployment hard cap."},
            "bucket": {"type": "string", "maxLength": 120},
            "thread": {"type": "string", "maxLength": 120},
            "include_sensitive": {"type": "boolean", "default": False},
            "ambient": {"type": "boolean", "default": False},
            "ambient_top_n": {"type": "integer", "minimum": 1},
        },
        "additionalProperties": False,
        "example": {"limit": 50, "bucket": "Collaboration", "thread": "communication style", "include_sensitive": False},
    },
    "MemoryFetchRequest": {
        "type": "object",
        "required": ["ids"],
        "properties": {
            "ids": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
            "limit": {"type": "integer", "minimum": 0},
            "include_archived": {"type": "boolean", "default": False},
            "include_superseded": {"type": "boolean", "default": False},
        },
        "additionalProperties": False,
        "example": {"ids": ["mem_abc123", "mem_def456"]},
    },
    "MemoryRecordInput": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["moment", "quote", "fact", "event", "insight", "reflection"]},
            "title": {"type": "string", "maxLength": 180},
            "description": {"type": "string", "maxLength": 2000},
            "summary": {"type": "string", "maxLength": 2000},
            "content": {"type": "string", "maxLength": 5000},
            "bucket": {"type": "string", "maxLength": 80},
            "threads": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 80}},
            "importance": {"type": "number", "minimum": 0, "maximum": 1},
            "pulse": {"type": "number", "minimum": 0, "maximum": 1},
            "occurred_at": {"type": "string", "maxLength": 80},
            "anchor_memory_ids": {"type": "array", "items": {"type": "string"}},
            "source": {
                "type": "string",
                "enum": [
                    "bootstrap",
                    "chat",
                    "genesis_import",
                    "genesis_resident_distill",
                    "history_import",
                    "hosted_runtime_state",
                    "live_conversation",
                    "memory_capture",
                    "memory_dream",
                    "memory_migrate",
                    "model_api_capture",
                    "model_api_correction",
                    "model_api_repair",
                    "ombre_brain_sync",
                    "resident_absorb",
                    "resident_patch",
                ],
            },
        },
        "additionalProperties": True,
    },
    "MemoryAction": {
        "type": "object",
        "required": ["type"],
        "properties": {
            "type": {
                "type": "string",
                "enum": ["memory.add", "memory.supersede", "memory.delete", "memory.retype"],
                "description": "Accepted memory.add actions always create a new card; repeated content is not deduplicated.",
            },
            "envelope": {"$ref": "#/components/schemas/MemoryEnvelope"},
            "memory": {
                "$ref": "#/components/schemas/MemoryRecordInput",
                "description": "Plaintext server-encryption compatibility form. Prefer envelope for sensitive content.",
            },
            "memory_id": {"type": "string"},
            "id": {"type": "string"},
            "new_type": {"type": "string", "enum": ["moment", "quote", "fact", "event", "insight", "reflection"]},
            "supersedes": {
                "oneOf": [
                    {"type": "string", "minLength": 1},
                    {"type": "array", "minItems": 1, "maxItems": 20, "items": {"type": "string", "minLength": 1}},
                ]
            },
            "anchor_memory_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "reason": {"type": "string"},
            "capture_mode": {
                "type": "string",
                "enum": [
                    "agent_tool",
                    "genesis_import",
                    "genesis_resident_distill",
                    "memory_capture",
                    "memory_dream",
                    "repair",
                    "state",
                ],
            },
            "source_chat_message_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
        },
        "additionalProperties": True,
    },
    "MemoryActionsRequest": {
        "type": "object",
        "required": ["actions"],
        "properties": {"actions": {"type": "array", "minItems": 1, "maxItems": 20, "items": {"$ref": "#/components/schemas/MemoryAction"}}},
        "additionalProperties": False,
        "example": {
            "actions": [
                {
                    "type": "memory.add",
                    "envelope": {
                        "v": 1,
                        "id": "mom_planning_preference",
                        "body_ct": "BASE64_CIPHERTEXT",
                        "nonce": "BASE64_NONCE",
                        "K_user": "BASE64_KEY_WRAPPED_TO_USER",
                        "K_enclave": "BASE64_KEY_WRAPPED_TO_ENCLAVE",
                        "visibility": "shared",
                        "owner_user_id": "usr_0123456789abcdef",
                        "type": "fact",
                        "occurred_at": "2026-07-13T14:30:00Z",
                    },
                    "reason": "Durable collaboration preference.",
                }
            ]
        },
    },
    "MemoryActionResult": {
        "type": "object",
        "required": ["status", "http_status"],
        "properties": {
            "status": {"type": "string", "enum": ["ok", "error"]},
            "http_status": {"type": "integer", "description": "Per-item execution status; inspect it independently of the outer batch status."},
            "action": {"type": "string"},
            "error": {"type": "string"},
            "id": {"type": "string"},
            "memory_id": {"type": "string"},
            "noop": {"type": "boolean"},
            "skipped": {"type": "string"},
        },
        "additionalProperties": True,
    },
    "MemoryActionsResponse": {
        "type": "object",
        "required": ["status", "results", "effects", "total_count", "applied_count", "skipped_count", "failed_count"],
        "properties": {
            "status": {"type": "string", "enum": ["ok", "partial", "failed"]},
            "results": {"type": "array", "items": {"$ref": "#/components/schemas/MemoryActionResult"}},
            "effects": {"type": "array", "items": {"type": "object"}},
            "total_count": {"type": "integer", "minimum": 0},
            "applied_count": {"type": "integer", "minimum": 0},
            "skipped_count": {"type": "integer", "minimum": 0},
            "failed_count": {"type": "integer", "minimum": 0},
            "error": {"type": "string", "description": "For an HTTP 400 all-failed batch, the first failed item's stable error slug."},
            "detail": {
                "description": "Optional validation detail copied from the first failed item in an HTTP 400 all-failed batch.",
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {}},
                    {"type": "object", "additionalProperties": True},
                ],
            },
        },
        "additionalProperties": False,
    },
    "MemoryAddRequest": {
        "type": "object",
        "required": ["envelope"],
        "properties": {"envelope": {"$ref": "#/components/schemas/MemoryEnvelope"}},
        "additionalProperties": False,
    },
    "MemoryRetypeRequest": {
        "type": "object",
        "required": ["id", "type"],
        "properties": {
            "id": {"type": "string", "minLength": 1},
            "type": {"type": "string", "enum": ["moment", "quote", "fact", "event", "insight", "reflection"]},
            "anchor_memory_ids": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": False,
    },
    "PerceptionSignal": {
        "type": "object",
        "required": ["key"],
        "properties": {
            "key": {"type": "string"},
            "data": {"type": "string", "description": "JSON-encoded value for non-sensitive operation signals."},
            "message": {"type": "string"},
            "envelope": {"$ref": "#/components/schemas/EncryptedEnvelope"},
            "changed": {"type": "boolean", "description": "Required by encrypted sensitive signals."},
        },
        "if": {"required": ["envelope"]},
        "then": {"required": ["changed"]},
        "additionalProperties": True,
    },
    "PerceptionReportRequest": {
        "type": "object",
        "properties": {
            "context_snapshot": {"type": "array", "minItems": 1, "items": {"$ref": "#/components/schemas/PerceptionSignal"}},
            "items": {
                "type": "object",
                "minProperties": 1,
                "description": "Map of perception kind to compatibility item rows.",
                "additionalProperties": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                },
            },
            "config": {"type": "object", "minProperties": 1, "additionalProperties": True},
            "client_ts": {"oneOf": [{"type": "string"}, {"type": "number"}]},
        },
        "anyOf": [{"required": ["context_snapshot"]}, {"required": ["items"]}, {"required": ["config"]}],
        "additionalProperties": False,
        "example": {
            "context_snapshot": [
                {"key": "time", "data": "{\"timezone\":\"America/New_York\"}", "message": "Local time and locale"},
                {"key": "battery", "data": "{\"charging\":true,\"level\":0.82}", "message": "Battery state"},
            ],
            "client_ts": "1783962000",
        },
    },
    "PerceptionReportResponse": {
        "type": "object",
        "required": ["results"],
        "properties": {"results": {"type": "object", "additionalProperties": True}},
        "additionalProperties": True,
        "example": {"results": {"time": "accepted", "motion_state": "accepted", "unsupported": "ignored"}},
    },
    "ProactiveDecisionReviewRequest": {
        "type": "object",
        "required": ["label"],
        "properties": {
            "label": {
                "type": "string",
                "enum": [
                    "correct_false",
                    "correct_true",
                    "good_presence",
                    "great_companion_moment",
                    "ignored_manual",
                    "late_irrelevant",
                    "missed_moment",
                    "missed_opportunity",
                    "privacy_bad",
                    "repeated",
                    "spam",
                    "stutter",
                    "too_chatty",
                    "too_much_buzz",
                    "weak_connection",
                    "went_dark",
                    "wrong_voice",
                ],
            },
            "notes": {"type": "string", "maxLength": 500},
            "reviewer": {"type": "string", "maxLength": 80, "default": "human"},
            "expected_should_reach_out": {"type": "boolean"},
            "correct_connection_source_id": {"type": "string", "maxLength": 160},
        },
        "additionalProperties": False,
        "example": {
            "label": "good_presence",
            "notes": "The timing and context were appropriate.",
            "reviewer": "human",
        },
    },
    "PerceptionPhotoEvaluateRequest": {
        "type": "object",
        "required": ["content_envelope"],
        "properties": {
            "content_envelope": {
                "allOf": [{"$ref": "#/components/schemas/EncryptedEnvelope"}],
                "description": "Encrypted body_ct or plaintext-tier body_b64 image envelope.",
            },
            "metadata": {
                "type": "object",
                "properties": {
                    "has_faces": {"oneOf": [{"type": "boolean"}, {"type": "string"}]},
                    "face_count": {"oneOf": [{"type": "integer", "minimum": 0}, {"type": "string"}]},
                    "scene_hint": {"type": "string"},
                    "scene_confidence": {"oneOf": [{"type": "number", "minimum": 0, "maximum": 1}, {"type": "string"}]},
                    "time_of_day": {"type": "string"},
                    "is_burst": {"oneOf": [{"type": "boolean"}, {"type": "string"}]},
                    "is_indoor": {"oneOf": [{"type": "boolean"}, {"type": "string"}]},
                    "has_text_block": {"oneOf": [{"type": "boolean"}, {"type": "string"}]},
                    "is_screenshot": {"oneOf": [{"type": "boolean"}, {"type": "string"}]},
                },
                "additionalProperties": False,
            },
            "meta_envelope": {
                "allOf": [{"$ref": "#/components/schemas/EncryptedEnvelope"}],
                "description": "Optional encrypted or plaintext-tier UTF-8 metadata envelope.",
            },
            "exif_gps": {"type": "object", "deprecated": True, "description": "Legacy compatibility only; clients should not upload raw EXIF GPS."},
        },
        "additionalProperties": False,
    },
    "McpServerUpsertRequest": {
        "type": "object",
        "required": ["name", "url"],
        "properties": {
            "name": {
                "type": "string",
                "pattern": "^[a-z0-9_-]{1,32}$",
                "description": "Stable identifier. Upserting an existing name replaces that server's config.",
            },
            "url": {
                "type": "string",
                "description": (
                    "MCP server endpoint. http:// and https:// are both accepted, including "
                    "loopback and private-network hosts. Saving the config does not dial it. "
                    "POST .../test dials public endpoints from the API backend on request, and "
                    "Hosted Runtime V2 contacts each enabled server at turn start for tools/list "
                    "before any later tool calls."
                ),
            },
            "headers": {
                "type": "object",
                "maxProperties": 20,
                "additionalProperties": {"type": "string"},
                "description": "Extra HTTP headers (e.g. Authorization) sent with every MCP request. Max 20 entries, 8192 bytes combined across names and values. The Host header is forbidden.",
            },
            "enabled": {"type": "boolean", "default": True},
            "resident": {
                "type": "boolean",
                "default": False,
                "description": (
                    "When true, send this server's complete tool argument schemas "
                    "to the model at turn start. When false, keep only tool names "
                    "and short descriptions resident and load complete schemas on "
                    "demand with mcp_tool_search."
                ),
            },
            "ca_pem": {
                "type": "string",
                "maxLength": 32768,
                "description": (
                    "PEM-encoded CA certificate to trust in addition to the public CA store, for a "
                    "self-signed or private-CA server. This adds trust; it does not disable "
                    "certificate verification — the server still has to present a certificate "
                    "chaining to this CA and prove possession of the matching private key. Omit or "
                    "send empty to clear a previously stored CA."
                ),
            },
            "read_only_tool_fingerprints": {
                "type": "object",
                "maxProperties": 64,
                "propertyNames": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "pattern": r"^[^\u0000-\u001f\u007f]{1,256}$",
                },
                "additionalProperties": {
                    "type": "string",
                    "pattern": "^[a-f0-9]{64}$",
                },
                "description": (
                    "Exact tool-name to catalog-fingerprint approvals returned by POST "
                    ".../test. Runtime V2 treats a tool as a parallel read only while the "
                    "remote catalog still has strict readOnlyHint=true and its current "
                    "fingerprint exactly matches this encrypted approval; missing or stale "
                    "entries fail closed to serialized mutation semantics."
                ),
            },
        },
        "additionalProperties": False,
        "example": {
            "name": "search",
            "url": "https://mcp.example.com/mcp",
            "headers": {"Authorization": "Bearer sk-..."},
            "enabled": True,
            "resident": False,
        },
    },
    "McpServerPatchRequest": {
        "type": "object",
        "minProperties": 1,
        "properties": {
            "enabled": {"type": "boolean"},
            "resident": {
                "type": "boolean",
                "description": (
                    "Toggle whether complete tool schemas remain resident. Changing "
                    "this field updates the MCP fingerprint and rematerializes the "
                    "runtime tool surface."
                ),
            },
            "read_only_tool_fingerprints": {
                "type": "object",
                "maxProperties": 64,
                "propertyNames": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "pattern": r"^[^\u0000-\u001f\u007f]{1,256}$",
                },
                "additionalProperties": {
                    "type": "string",
                    "pattern": "^[a-f0-9]{64}$",
                },
                "description": (
                    "Replace the encrypted read-only approval map without resending URL, "
                    "headers, or CA. Send {} to revoke every approval."
                ),
            },
        },
        "additionalProperties": False,
        "example": {
            "read_only_tool_fingerprints": {
                "search": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            }
        },
    },
    "McpServerRuntimeStatus": {
        "type": "object",
        "required": ["last_kind", "last_at", "recent_ok", "recent_total"],
        "properties": {
            "last_kind": {
                "type": "string",
                "pattern": "^[a-z0-9_]{1,48}$",
                "description": (
                    "Latest content-free Hosted Runtime V2 load outcome. "
                    "available means the server catalog loaded successfully; "
                    "other stable kinds describe why it was not ready. Clients "
                    "must tolerate future kinds."
                ),
            },
            "last_at": {
                "type": "number",
                "exclusiveMinimum": 0,
                "description": "Unix epoch seconds of the latest observation.",
            },
            "recent_ok": {
                "type": "integer",
                "minimum": 0,
                "maximum": 10,
                "description": (
                    "Number of recent observed chat turns whose kind was available."
                ),
            },
            "recent_total": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": (
                    "Number of recent observed chat turns in the bounded window."
                ),
            },
        },
        "additionalProperties": False,
        "example": {
            "last_kind": "available",
            "last_at": 1786370000.0,
            "recent_ok": 9,
            "recent_total": 10,
        },
    },
    "McpServerRecord": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "name": {"type": "string"},
            "enabled": {"type": "boolean"},
            "resident": {
                "type": "boolean",
                "description": (
                    "Whether complete remote tool schemas are resident at turn start. "
                    "False uses name/description discovery plus mcp_tool_search."
                ),
            },
            "url_hint": {"type": "string", "description": "Hostname only (no scheme, path, or credentials), for display."},
            "header_names": {"type": "array", "items": {"type": "string"}, "description": "Header names only; values are never returned."},
            "has_ca": {"type": "boolean", "description": "Whether a CA certificate is currently stored for this server."},
            "transport": {"type": "string", "enum": ["http", "sse"], "description": "MCP transport: 'http' (streamable HTTP) or 'sse' (legacy HTTP+SSE). Guessed from the URL path at save time ('.../sse' => 'sse') and corrected to the server's actual transport the first time a connectivity test succeeds."},
            "created_at": {"type": "string"},
            "updated_at": {"type": "string"},
            "runtime": {
                "$ref": "#/components/schemas/McpServerRuntimeStatus",
                "description": (
                    "Present on GET /v1/mcp/servers only when Hosted Runtime V2 "
                    "has observed this server. Omitted when no observation exists; "
                    "absence is not evidence of success."
                ),
            },
        },
        "additionalProperties": True,
        "example": {
            "id": "srv_1a2b3c4d",
            "name": "search",
            "enabled": True,
            "resident": False,
            "url_hint": "mcp.example.com",
            "header_names": ["Authorization"],
            "has_ca": False,
            "transport": "http",
            "created_at": "2026-07-16T00:00:00Z",
            "updated_at": "2026-07-16T00:00:00Z",
        },
    },
    "McpServerListResponse": {
        "type": "object",
        "required": ["servers"],
        "properties": {"servers": {"type": "array", "items": {"$ref": "#/components/schemas/McpServerRecord"}}},
        "additionalProperties": True,
    },
    "McpServerDeleteResponse": {
        "type": "object",
        "required": ["deleted"],
        "properties": {"deleted": {"type": "string", "description": "Name of the deleted server."}},
        "additionalProperties": True,
    },
    "McpServerTestResponse": {
        "type": "object",
        "required": [
            "ok",
            "tool_count",
            "tool_names",
            "read_only_tool_fingerprints",
            "transport",
        ],
        "properties": {
            "ok": {"type": "boolean", "const": True},
            "tool_count": {"type": "integer", "minimum": 0},
            "tool_names": {"type": "array", "items": {"type": "string"}},
            "read_only_tool_fingerprints": {
                "type": "object",
                "maxProperties": 64,
                "propertyNames": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "pattern": r"^[^\u0000-\u001f\u007f]{1,256}$",
                },
                "additionalProperties": {
                    "type": "string",
                    "pattern": "^[a-f0-9]{64}$",
                },
                "description": (
                    "Approval candidates for tools whose current MCP catalog has strict "
                    "readOnlyHint=true. The response contains at most 64 candidates and omits "
                    "names that PATCH cannot accept. These are not active until the user "
                    "explicitly sends selected entries to PATCH /v1/mcp/servers/{name}."
                ),
            },
            "transport": {"type": "string", "enum": ["http", "sse"], "description": "The MCP transport this probe actually spoke to the server: 'http' (streamable HTTP) or 'sse' (legacy HTTP+SSE). This is the authoritative detection that gets persisted to the server record's transport field."},
        },
        "additionalProperties": True,
        "example": {
            "ok": True,
            "tool_count": 2,
            "tool_names": ["search", "fetch"],
            "read_only_tool_fingerprints": {
                "search": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            },
            "transport": "http",
        },
    },
    "NotifyRelayRegisterRequest": {
        "type": "object",
        "required": ["device_token"],
        "properties": {
            "device_token": {
                "type": "string",
                "pattern": "^[0-9a-fA-F]{32,200}$",
                "description": "APNs device token (hex) of the enrolling device.",
            },
            "apns_env": {
                "type": "string",
                "enum": ["sandbox", "production"],
                "default": "production",
                "description": "APNs environment the device token belongs to.",
            },
            "auth_token": {
                "type": "string",
                "description": (
                    "Previously issued relay token (nrt_…). Pass it to re-enroll "
                    "after a device-token rotation or device change; the same "
                    "token is returned. Unknown tokens are ignored and the "
                    "request falls back to matching by device_token."
                ),
            },
            "user_id": {
                "type": "string",
                "description": "Optional official-account user id, if the caller has one.",
            },
        },
        "additionalProperties": True,
        "example": {"device_token": "ab12…ef90", "apns_env": "production"},
    },
    "NotifyRelayPushRequest": {
        "type": "object",
        "required": ["type", "content"],
        "properties": {
            "type": {
                "type": "integer",
                "enum": [1, 2, 3, 4],
                "description": (
                    "1 = alert notification, 2 = Live Activity update (the "
                    "Dynamic Island is the same Live Activity), 3 = Live "
                    "Activity start (push-to-start), 4 = Live Activity end."
                ),
            },
            "token": {
                "type": "string",
                "pattern": "^[0-9a-fA-F]{32,200}$",
                "description": (
                    "Target APNs token. For type 1 it may be omitted — the "
                    "enrolled device token is always used, and an explicit "
                    "value must match it (alert pushes are scoped to the "
                    "enrolled device; anything else is rejected with 400). "
                    "Required for types 2-4: Live Activity tokens rotate per "
                    "activity, so always pass the latest one your backend "
                    "received from the app."
                ),
            },
            "apns_env": {
                "type": "string",
                "enum": ["sandbox", "production"],
                "description": "Defaults to the environment stored at enrollment.",
            },
            "content": {
                "type": "object",
                "additionalProperties": True,
                "description": (
                    "Type-specific payload. type 1: title, body, subtitle?, "
                    "sound?, badge?, data? (custom keys merged into the push "
                    "payload). types 2-4: visualState? (default|sharing|reply), "
                    "name?, desc?, aiStart?, alert_title?, alert_body?; type 2 "
                    "also stale_date?; type 3 also attributes? "
                    "({\"activityId\": …}) and attributes_type? (defaults to "
                    "ScreenActivityAttributes); type 4 also dismissal_date? "
                    "(unix seconds, defaults to now)."
                ),
            },
        },
        "additionalProperties": True,
        "example": {
            "type": 1,
            "content": {"title": "IO", "body": "Your agent replied."},
        },
    },
    "NotifyRelayRegisterResponse": {
        "type": "object",
        # Only device_token and created are always present. auth_token/apns_env
        # are omitted on the already_enrolled response (an already-enrolled
        # device matched by its non-secret device_token gets no token back), so
        # they must NOT be required or a generated client rejects that valid 200.
        "required": ["device_token", "created"],
        "properties": {
            "auth_token": {"type": "string",
                           "description": "Relay token (nrt_…) for X-Relay-Token. Present only "
                                          "on first enrollment or a re-enroll that presented a "
                                          "valid token; omitted when already_enrolled is true."},
            "device_token": {"type": "string"},
            "apns_env": {"type": "string", "enum": ["sandbox", "production"],
                         "description": "Omitted on the already_enrolled response."},
            "created": {"type": "boolean", "description": "False when the device was already enrolled."},
            "already_enrolled": {"type": "boolean",
                                 "description": "True when the device was already enrolled and no "
                                                "token is disclosed; recover the token via a "
                                                "re-enroll that presents it."},
        },
        "additionalProperties": True,
        "example": {"auth_token": "nrt_0123…", "device_token": "ab12…ef90",
                    "apns_env": "production", "created": True},
    },
    "NotifyRelayPushResponse": {
        "type": "object",
        "required": ["status", "apns_env"],
        "properties": {
            "status": {"type": "string", "enum": ["delivered", "error"]},
            "apns_env": {"type": "string", "description": "Environment actually delivered to (after automatic sandbox/production fallback)."},
            "log_id": {"type": "integer", "description": "Server-side delivery-log row id."},
            "reason": {"type": "string", "description": "APNs rejection reason when status is error (e.g. BadDeviceToken — refresh the token)."},
            "error_code": {"type": "integer", "description": "APNs HTTP status when status is error."},
        },
        "additionalProperties": True,
        "example": {"status": "delivered", "apns_env": "production", "log_id": 42},
    },
}


PRECISE_JSON_BODIES: dict[Operation, str] = {
    ("post", "/v1/voice/cancel"): "VoiceCallCancelRequest",
    ("post", "/v1/web/settings"): "WebSettingsUpdateRequest",
    ("post", "/v1/agent/web/search"): "WebSearchRequest",
    ("post", "/v1/agent/web/fetch"): "WebFetchRequest",
    ("post", "/v1/users/register"): "RegisterRequest",
    ("post", "/v1/access/link-token"): "LinkTokenRequest",
    ("post", "/v1/access/claim-token"): "ClaimTokenRequest",
    ("post", "/v1/account/recover/challenge"): "RecoverChallengeRequest",
    ("post", "/v1/account/recover/verify"): "RecoverVerifyRequest",
    ("post", "/v1/account/reset"): "AccountResetRequest",
    ("post", "/v1/model_api/chat/send"): "HostedChatSendRequest",
    ("post", "/v1/model_api/models"): "ModelApiModelsRequest",
    ("post", "/v1/model_api/runtime_error"): "ModelApiRuntimeErrorRequest",
    ("put", "/v1/image-generation/config"): "ImageGenerationConfigUpdateRequest",
    ("post", "/v1/image-generation/config"): "ImageGenerationRouteCreateRequest",
    ("post", "/v1/image-generation/generate"): "ImageGenerationRequest",
    ("put", "/v1/vision/config"): "VisionConfigUpdateRequest",
    ("post", "/v1/vision/config"): "VisionRouteCreateRequest",
    ("post", "/v1/vision/observe"): "VisionObserveRequest",
    ("post", "/v1/chat/message"): "ChatTransportRequest",
    ("post", "/v1/chat/response"): "ChatResponseRequest",
    ("post", "/v1/chat/turn-activity/{turn_id}/events"): "ChatActivityEventRequest",
    ("delete", "/v1/chat/history"): "ChatHistoryClearRequest",
    ("post", "/v1/memory/index"): "MemoryIndexRequest",
    ("post", "/v1/memory/fetch"): "MemoryFetchRequest",
    ("post", "/v1/memory/actions"): "MemoryActionsRequest",
    ("post", "/v1/memory/add"): "MemoryAddRequest",
    ("post", "/v1/memory/retype"): "MemoryRetypeRequest",
    ("post", "/v1/perception/report"): "PerceptionReportRequest",
    ("post", "/v1/perception/photo/evaluate"): "PerceptionPhotoEvaluateRequest",
    ("post", "/v1/mcp/servers"): "McpServerUpsertRequest",
    ("patch", "/v1/mcp/servers/{name}"): "McpServerPatchRequest",
    ("post", "/v1/notify-relay/register"): "NotifyRelayRegisterRequest",
    ("post", "/v1/notify-relay/push"): "NotifyRelayPushRequest",
}

# These handlers deliberately accept an absent body and apply server defaults.
OPTIONAL_PRECISE_BODIES: set[Operation] = {
    ("post", "/v1/users/register"),
    ("post", "/v1/access/link-token"),
    ("post", "/v1/memory/index"),
    ("post", "/v1/model_api/runtime_error"),
}


SPECIAL_REQUEST_BODIES: dict[Operation, dict[str, Any]] = {
    ("post", "/v1/onboarding/archive"): {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {
                        "file": {"type": "string", "format": "binary", "description": "Archive file; total request size is limited to 25 MiB."},
                        "filename": {"type": "string"},
                        "content_type": {"type": "string"},
                        "client_job_id": {"type": "string"},
                    },
                }
            }
        },
    },
    ("post", "/v1/diagnostics/logs"): {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {
                        "file": {"type": "string", "format": "binary"},
                        "meta": {"type": "string", "description": "JSON-encoded diagnostic metadata."},
                    },
                }
            }
        },
    },
    ("put", "/v1/genesis/imports/{job_id}/chunks/{seq}"): {
        "required": True,
        "description": "Send either a JSON chunk document or raw ciphertext with metadata headers.",
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/FreeFormJsonObject"}},
            "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
        },
    },
    ("post", "/v1/proactive/decisions/{decision_id}/review"): {
        "required": True,
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/ProactiveDecisionReviewRequest"}},
            "application/x-www-form-urlencoded": {"schema": {"$ref": "#/components/schemas/ProactiveDecisionReviewRequest"}},
        },
    },
}


OPERATION_DESCRIPTIONS: dict[Operation, str] = {
    ("post", "/v1/voice/cancel"): (
        "Idempotently end a voice call that will not be archived. The durable "
        "tombstone suppresses late resident and Hosted Runtime V2 replies "
        "before chat insertion, removes unarchived per-turn rows and encrypted "
        "voice handoff state, and makes repeated cancellation safe. If finalize "
        "already claimed the call, cancellation returns that lifecycle state "
        "without deleting rows or downgrading the archive."
    ),
    ("post", "/v1/agent/web/search"): (
        "Run a keyless web search (DuckDuckGo HTML scrape) and return ranked "
        "results. Cloud-only: requires a hosted per-user runtime token carrying "
        "the `web` scope — long-term api-key auth is refused with 403, so "
        "self-hosted deployments cannot reach this endpoint (their model uses its "
        "own provider's web capability instead). Authorization is re-checked on "
        "every "
        "call — the user's own web switch must be on, the operator must not have "
        "halted web_search, and a per-user rate limit applies — because V1 tool "
        "authorization is static and cannot live in the prompt. Every gate and "
        "input-validation failure is reported IN-BAND as HTTP 200 with ok:false "
        "and an error.code (capability_disabled when the user's switch is off — "
        "not retryable; capability_rate_limited when over the per-user budget — "
        "retryable; capability_invalid_input for a missing or sensitive query; "
        "capability_upstream_error when the search itself fails). The aggregate "
        "result set is character-budgeted; data.truncated flags dropped items."
    ),
    ("post", "/v1/agent/web/fetch"): (
        "Fetch one absolute http(s) URL and return its readable text (HTML is "
        "reduced to the main article; JSON, plain text, and source are returned "
        "as-is), size-capped with a data.truncated flag. SSRF-guarded: "
        "private/loopback/link-local targets are refused and every redirect hop "
        "is independently re-validated (max 5). Cloud-only: requires a hosted "
        "per-user runtime token carrying the `web` scope — long-term api-key auth "
        "is refused with 403 (self-hosted deployments use their model provider's "
        "own web capability). Then the same three authorization gates as web "
        "search apply, reported in-band as HTTP "
        "200 with ok:false and an error.code (capability_disabled, "
        "capability_rate_limited, capability_invalid_input for a bad or blocked "
        "url, capability_not_found / capability_upstream_error for an upstream "
        "HTTP status)."
    ),
    ("get", "/v1/bootstrap/status"): "Return server-observed onboarding progress. Resident routes include decrypt_source_ready, decrypt_health, and decrypt_health_policy; a fresh resident poll report is required for new-account completion.",
    ("get", "/v1/onboarding/validate"): "Return ordered onboarding checks. Resident routes include a decrypt_source step between resident_consumer and live_loop, with status, checked_at_epoch, reason, policy, and remediation fields.",
    ("get", "/v1/chat/poll"): "Long-poll and optionally claim resident chat work. Official residents report their running commit and may report an intentionally skipped compatible backend target with X-Feedling-Consumer-Compat-Commit. They also report decrypt-source status and its confirmation time on every poll heartbeat with X-Feedling-Decrypt-Status and X-Feedling-Decrypt-Checked-At.",
    ("post", "/v1/chat/message"): "Store a user chat message as a v1 ciphertext envelope; the server never decrypts it. If the envelope carries a content_pk_fpr label that does not match the user's currently registered content key, the write is rejected with 409 content_pk_fpr_mismatch (re-fetch whoami and re-seal); unlabeled envelopes are accepted for compatibility.",
    ("post", "/v1/chat/response"): "Store an agent reply as a v1 ciphertext envelope plus optional thinking and encrypted file/image followups. A text primary and its attachment rows commit as one ordered transaction; generated images are returned as native content_type=image Chat messages. Replies carrying reply_to_message_id are finalized atomically across backend workers: exactly one request inserts the reply and marks the parent answered, while a losing contender returns 409 already_answered without storing its reply. A hidden source=verify_ping reply is accepted only when reply_to_message_id identifies an outstanding verify ping exactly. role=system notices bypass reply exclusivity. A bootstrap_incomplete 409 always includes retryable: needs_resident_consumer is true so an official identity that previously polled may retry the same reply with bounded backoff; other stages are false, and a missing field from an old server must be treated as false. Labeled envelopes sealed to a key that is no longer the user's registered content key are rejected with 409 content_pk_fpr_mismatch — the writer should re-fetch whoami, re-seal, and retry once.",
    ("post", "/v1/chat/verify_loop"): "Insert a hidden liveness ping and wait for its exact hidden reply (source=verify_ping and reply_to_message_id equal to this ping). loop_alive reports whether the reply arrived; passing additionally requires resident decrypt health to satisfy the onboarding policy before sticky live-loop verification is recorded.",
    ("post", "/v1/model_api/chat/send"): "Queue an asynchronous hosted-agent turn. A successful response is always 202 and never contains a plaintext assistant reply.",
    ("post", "/v1/model_api/setup"): (
        "Create or update the active hosted model route. context_window_tokens "
        "is treated as route metadata: a value below Runtime V2's "
        "deployment-tunable trust floor (32,768 by default) falls through to "
        "the audited-family or unaudited-route default rather than becoming "
        "the prompt budget. The resolved value is returned and persisted."
    ),
    ("post", "/v1/model_api/models"): "列出某 provider 在该凭据下可见的模型清单（实时拉取，非 io 兼容性保证）。unsupported / partial 时客户端退回手填。",
    ("post", "/v1/model_api/runtime_error"): "Record or clear the resident runtime's latest provider error. provider_result=success refreshes provider health immediately; provider_result=failure applies error_class to the provider-health policy.",
    ("get", "/v1/image-generation/config"): "Return the effective image-generation route, candidate routes, and validation state. The response mirrors vision routing semantics: follow_main uses the active chat route and dedicated pins a separately validated route.",
    ("put", "/v1/image-generation/config"): "Select follow_main or a dedicated image-generation route. The selected route is validated for real image output before the change is committed.",
    ("post", "/v1/image-generation/config"): "Create, validate, and select a dedicated image-generation route using an existing credential or a newly supplied provider key.",
    ("post", "/v1/image-generation/main/test"): "Validate the active main route for real image generation and return the refreshed effective configuration.",
    ("post", "/v1/image-generation/routes/{route_id}/test"): "Validate one saved route for real image generation and persist its capability result.",
    ("post", "/v1/vision/main/test"): "Validate the effective main model's image-input capability. Model API routes use explicit catalog metadata when available and otherwise perform a real two-image probe, returning a terminal 200 status. Resident runtimes start a hidden isolated two-image probe and return 202 testing; poll GET /v1/vision/config until effective_status leaves testing. Residents without the hidden-probe capability receive 409 vision_resident_update_required.",
    ("get", "/v1/model_api/usage"): (
        "Query the caller's active model_api provider for balance/usage, live on every call — "
        "never cached and never stored. Uses the same decrypted provider key as chat, decrypted "
        "server-side just for this request. Which of the five fixed metrics (balance, remaining, "
        "usage_total, usage_today, usage_month) actually populate depends on the provider/adapter: "
        "DeepSeek and OpenRouter are queried through their official APIs; an openai_compatible "
        "relay/中转站 is queried through its dashboard-style endpoints when reachable, and is "
        "skipped with error=usage_blocked_url if it resolves to a private/loopback address. "
        "Metrics the adapter cannot report are status=\"unsupported\", not omitted."
    ),
    ("get", "/v1/chat/history"): "Read encrypted chat history. Use oldest_seq as before_seq for lossless older paging and latest_seq as after_seq for lossless forward paging; timestamp watermarks remain for compatibility.",
    ("get", "/v1/chat/turn-activity/{turn_id}"): "Read display-safe activity for one V1 resident or Runtime V2 chat turn. V2 events come from backend jobs and tool dispatch; V1 events come from the authenticated resident io_cli boundary and are durably scoped to an existing user message. Both runtimes expose only bounded identifiers, state, timing, and result classification. Successful memory_search/memory_fetch events include the confirmed returned-item count and, only when every item uses the canonical bucket taxonomy, a complete category-count breakdown. Tool arguments, result bodies, assistant prose, reasoning, and custom bucket labels are never returned.",
    ("post", "/v1/chat/turn-activity/{turn_id}/events"): "Append one authenticated V1 resident tool transition. This endpoint is used by the shipped resident io_cli runtime, accepts only running/success/failure plus display-safe fixed metadata, rejects V2-owned users, and never accepts tool arguments, model prose, or result bodies.",
    ("post", "/v1/memory/index"): "Return lightweight memory cards. This is selection, not full-content retrieval; query is intentionally not exposed because it is not a search filter today.",
    ("post", "/v1/memory/fetch"): "Fetch full records for selected memory IDs. Sensitive fetch behavior is not part of the current public contract.",
    ("post", "/v1/memory/actions"): "Apply up to 20 memory actions independently and in order. Full or partial applied success returns HTTP 200. When no action is applied and at least one fails, HTTP 400 promotes the first failed item's error/detail while preserving every result and all counts. An all-skipped batch remains 200. The batch is not transactional and Idempotency-Key is not supported.",
    ("post", "/v1/perception/report"): "Submit device context. Sensitive signals must use encrypted envelopes; inspect each results entry even when HTTP status is 200.",
    ("get", "/v1/perception/app_open"): "Legacy iOS Shortcut compatibility endpoint. This GET records an event and therefore has side effects.",
    ("get", "/v1/perception/app_close"): "iOS Shortcut compatibility endpoint for the automation's \"is closed\" trigger. This GET records an event and therefore has side effects.",
    ("post", "/v1/users/register"): "Create a Feedling user and issue its first API key. The key is returned once and this operation is not idempotent.",
    ("get", "/v1/users/whoami"): "Return the authenticated user's identifiers, registered content public key, attested decrypt-service material, and first-class preferences. content_encryption is the user's stated content-encryption preference; content_encryption_effective is the shape a client must actually write and is the only one a write path may act on. Treat an absent, empty, or unrecognized effective value as \"on\" — a deployment that has not enabled plaintext storage reports \"on\" regardless of the preference.",
    ("post", "/v1/users/preferences"): "Update first-class user preferences. Accepts archive_language, timezone, and/or content_encryption (\"on\", \"off\", or null to clear). Setting content_encryption records intent only; it neither converts existing records nor changes what a client must write until content_encryption_effective follows. Use /v1/content/swap to convert existing records between shapes.",
    ("post", "/v1/access/claim-token"): "Consume a one-time link token and issue an additional API key. Existing keys remain active.",
    ("post", "/v1/account/recover/verify"): "Verify keypair possession and issue an additional API key for the existing account. Existing keys remain active.",
    ("post", "/v1/account/reset"): "Permanently delete the account, its data, and all of its API keys. This is not a per-key revocation endpoint.",
    ("post", "/v1/genesis/imports/plaintext"): "Queue an asynchronous plaintext import and immediately publish every material as queued with its total window count. Every processing frame preserves the complete material list while item progress advances. Identity may become ready while processing continues; done is published only after every material window completes. A non-empty keep-all memory import that produces zero cards fails as distill_empty_output; its job output may include up to six discarded-map diagnostics, each with a reason and a model-output excerpt capped at 500 characters. Only one plaintext import can process per account; a concurrent submission returns 409 import_job_active with active_job_id. A same-host abandoned worker is detected by its exited process and failed immediately as worker_restarted; remote or legacy owners use a bounded heartbeat lease so a live rolling-deploy worker is not killed. A failed matching client_job_id or input is resumed from its encrypted per-window checkpoint, while a completed match returns the existing job. In update_identity mode, the uploaded role card creates an identity when absent or updates the existing card while preserving its relationship anchor by default.",
    ("post", "/v1/genesis/imports/plaintext/estimate"): "Parse and encrypt-stage plaintext onboarding material without calling an LLM. Returns per-material window and conservative token estimates plus an optional fast-model recommendation for Anthropic, DeepSeek, Gemini, OpenAI, OpenRouter, or compatible relay configurations. Compatible-relay recommendations exclude Gemini Flash model IDs while leaving Gemini Pro and non-Gemini Flash names eligible. The staged payload expires and must be committed by staged_id.",
    ("post", "/v1/genesis/imports/plaintext/commit"): "Commit one encrypted staged plaintext import and start asynchronous processing. An optional distill_model overrides only this job's model; provider, base URL, credential, and the account chat model remain unchanged.",
    ("get", "/v1/mcp/servers"): (
        "List the caller's user-configured MCP servers. Secrets (url, headers, "
        "ca_pem) are never returned; url_hint is the hostname only and "
        "header_names lists header keys only. resident reports whether complete "
        "tool schemas are sent at turn start; false uses mcp_tool_search on demand. "
        "When Hosted Runtime V2 has "
        "observed a server during recent chat turns, the record also carries a "
        "bounded, content-free runtime summary. The runtime field is omitted "
        "when no observation exists; absence is not evidence that the server is "
        "healthy. Runtime V1 does not currently write this status."
    ),
    ("post", "/v1/mcp/servers"): (
        "Create or update a user-configured MCP server (matched by name). http:// and https:// URLs "
        "are both accepted, including loopback and private-network hosts and IP literals — this "
        "backend does not dial the URL at save time. POST .../test dials public endpoints from the "
        "API backend on request. Hosted Runtime V2 contacts each enabled server at the start of "
        "every turn for tools/list, before any later tool calls. resident defaults to false: tool "
        "names and short descriptions stay visible while full argument schemas load on demand via "
        "mcp_tool_search. Set resident=true only when every schema should be sent at turn start. "
        "Hosted (cloud-run) agents cannot reach machines on your home or office network; expose a "
        "local MCP server through a tunnel (e.g. Cloudflare Tunnel, Tailscale Funnel, ngrok) to a "
        "public address first. To use a self-signed or private-CA server, paste the issuing CA "
        "certificate (PEM, max 32768 bytes) into ca_pem — this adds the CA to the trust store used "
        "for this server; it does not disable certificate verification, and the server still has to "
        "present a certificate chaining to that CA. http:// sends all configured headers, including "
        "API keys, in plaintext over the network; this is an explicit, informed choice. Possible 400 "
        "error kinds: invalid_request, invalid_name, invalid_url, invalid_headers, invalid_enabled, "
        "invalid_resident, "
        "too_many_headers, headers_too_large, forbidden_header, invalid_ca, ca_too_large, "
        "invalid_read_only_tool_fingerprints, invalid_read_only_tool_name, "
        "invalid_read_only_tool_fingerprint, too_many_read_only_tool_fingerprints, and "
        "too_many_servers. A 409 "
        "cannot_encrypt means the server-side envelope could not be built."
    ),
    ("patch", "/v1/mcp/servers/{name}"): (
        "Toggle a user-configured MCP server, change whether its full schemas are resident, "
        "and/or replace its encrypted read-only tool "
        "fingerprint approvals without resending URL, headers, or CA. Send an empty approval "
        "object to revoke all read-only approvals. Unknown fields are rejected; enabled and "
        "resident must be JSON booleans. Possible 400 errors include invalid_patch, "
        "invalid_enabled, invalid_resident, "
        "invalid_read_only_tool_fingerprints, invalid_read_only_tool_name, "
        "invalid_read_only_tool_fingerprint, too_many_read_only_tool_fingerprints, and "
        "decrypt_failed. A 409 cannot_encrypt leaves the old config unchanged. 404 not_found "
        "when no server with that name exists."
    ),
    ("delete", "/v1/mcp/servers/{name}"): "Delete a user-configured MCP server. 404 not_found when no server with that name exists.",
    ("post", "/v1/mcp/servers/{name}/test"): (
        "Best-effort connectivity probe (MCP initialize -> tools/list) run from this backend's own "
        "network, offered purely as a convenience for validating publicly reachable http:// or "
        "https:// "
        "servers. A 400 unreachable_from_backend response does NOT mean the server failed to save — "
        "it was already saved successfully by POST /v1/mcp/servers. It only means this backend "
        "process could not reach the host, typically because it is a loopback/private-network "
        "address or a tunnel endpoint that only your agent's environment can reach. For those "
        "servers, skip this probe and instead ask your agent to call the MCP server directly in "
        "chat to verify it. Other 400 kinds: dns (hostname did not resolve), dns_busy (resolver "
        "capacity exhausted), tls (certificate/TLS handshake failure — check ca_pem), "
        "codex_cert_chain_required (a hosted codex agent uses a rustls TLS stack that rejects a CA "
        "certificate presented as the server's own leaf/end-entity certificate; reissue the server "
        "with a proper CA+leaf chain — a pasted ca_pem does not fix this, since rustls rejects the "
        "leaf's shape regardless of what is trusted), transport (connection failure), timeout "
        "(connect/read timeout, or the whole probe exceeded its 45-second wall-clock ceiling — "
        "e.g. a legacy HTTP+SSE endpoint answering with a never-ending event stream), "
        "response_too_large, "
        "http_401/http_403/http_404/http_4xx/http_5xx (server responded with an HTTP error), "
        "protocol (malformed MCP handshake), decrypt_failed. "
        "404 not_found when no server with that name exists. A successful response also returns "
        "at most 64 PATCH-compatible read_only_tool_fingerprints for strict readOnlyHint=true "
        "tools; those candidates are inactive until explicitly approved through PATCH."
    ),
    ("post", "/v1/notify-relay/register"): (
        "Enroll a device for the self-hosted push relay and mint (or return) its "
        "relay auth token. Anonymous and idempotent: re-enrolling the same "
        "device_token returns the existing token (created=false); passing a "
        "previously issued auth_token re-binds it to a new device_token. The "
        "token authorizes POST /v1/notify-relay/push via the X-Relay-Token "
        "header. Per-IP rate limited; 429 carries Retry-After."
    ),
    ("post", "/v1/notify-relay/push"): (
        "Push through the official APNs relay using the X-Relay-Token header "
        "(issued by POST /v1/notify-relay/register). Types: 1 alert, 2 Live "
        "Activity update (Dynamic Island included), 3 Live Activity start "
        "(push-to-start token), 4 Live Activity end. Type 1 always delivers "
        "to the enrolled device (a mismatching explicit token is rejected). "
        "Types 2-4 must pass the target token — Live Activity tokens rotate "
        "per activity. APNs "
        "rejections return HTTP 200 with status=error plus the APNs reason; "
        "handle BadDeviceToken/Unregistered by refreshing tokens on your side. "
        "Per-token rate limited (429 + Retry-After); every call is logged "
        "server-side with the content truncated for privacy. 503 means the "
        "relay's APNs key is unavailable — retry later."
    ),
}


RESPONSE_OVERRIDES: dict[Operation, dict[str, Any]] = {
    ("post", "/v1/chat/response"): {
        "409": {
            "description": (
                "The reply conflicted with an existing answer/key, or Chat bootstrap "
                "is incomplete. bootstrap_incomplete responses include a required "
                "retryable boolean; clients must treat a missing field from an older "
                "server as false."
            ),
            "content": {
                "application/json": {
                    "schema": {
                        "anyOf": [
                            {"$ref": "#/components/schemas/ChatBootstrapIncompleteResponse"},
                            {"$ref": "#/components/schemas/ErrorResponse"},
                        ]
                    }
                }
            },
        },
    },
    ("get", "/v1/dream/status"): {
        "200": {
            "description": "Current Dream and Capture banner status.",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/DreamStatusResponse"}
                }
            },
        },
    },
    ("get", "/v1/memory/list"): {
        "200": {
            "description": "One deterministic page of encrypted memory records.",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/MemoryListResponse"}
                }
            },
        },
        "400": {
            "description": "The limit or opaque cursor is invalid.",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                }
            },
        },
    },
    ("post", "/v1/memory/actions"): {
        "200": {
            "description": (
                "The batch was processed item by item and either had no failures "
                "or applied at least one action. Inspect status, counts, and every "
                "results entry for partial outcomes."
            ),
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/MemoryActionsResponse"}
                }
            },
        },
        "400": {
            "description": (
                "No action was applied and at least one item failed. The first "
                "failed item's error and optional detail are promoted, while the "
                "complete independent results, effects, and counts are preserved."
            ),
            "content": {
                "application/json": {
                    "schema": {
                        "oneOf": [
                            {"$ref": "#/components/schemas/MemoryActionsResponse"},
                            {"$ref": "#/components/schemas/ErrorResponse"},
                        ]
                    }
                }
            },
        },
    },
    ("get", "/v1/web/settings"): {
        "200": {
            "description": "Current web-search state for the authenticated user.",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/WebSettingsResponse"}
                }
            },
        },
    },
    ("post", "/v1/agent/web/search"): {
        "200": {
            "description": (
                "The capability envelope. Always HTTP 200 — inspect `ok`. On "
                "success `data` holds the search results; otherwise `error.code` "
                "explains the refusal (disabled, rate-limited, invalid input, "
                "operator halt, or upstream failure)."
            ),
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/WebCapabilityResult"}
                }
            },
        },
    },
    ("post", "/v1/agent/web/fetch"): {
        "200": {
            "description": (
                "The capability envelope. Always HTTP 200 — inspect `ok`. On "
                "success `data` holds `{url, text, truncated}`; otherwise "
                "`error.code` explains the refusal (disabled, rate-limited, "
                "invalid/blocked url, operator halt, or upstream status)."
            ),
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/WebCapabilityResult"}
                }
            },
        },
    },
    ("post", "/v1/web/settings"): {
        "200": {
            "description": "The state after applying the update.",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/WebSettingsResponse"}
                }
            },
        },
    },
    ("get", "/healthz/runner"): {
        "503": {
            "description": (
                "The aggregate runner fleet is unhealthy, unavailable, or "
                "misconfigured, or its isolated probe exceeded its three-second "
                "health-check deadline. "
                "The body has the same shape as the 200 response, with top-level "
                "\"status\": \"unhealthy\" and a down \"runner_fleet\" entry "
                "under \"checks\"."
            ),
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/GenericJsonResponse"}}},
        },
    },
    ("get", "/v1/screen/frames/{frame_id}/image"): {
        "200": {
            "description": "Complete decrypted frame image.",
            "headers": {
                "Accept-Ranges": {"schema": {"type": "string", "const": "bytes"}},
                "ETag": {"schema": {"type": "string"}},
            },
            "content": {"image/jpeg": {"schema": {"type": "string", "format": "binary"}}},
        },
        "206": {
            "description": "Requested byte range of the decrypted frame image.",
            "headers": {
                "Accept-Ranges": {"schema": {"type": "string", "const": "bytes"}},
                "Content-Range": {"required": True, "schema": {"type": "string"}},
                "ETag": {"schema": {"type": "string"}},
            },
            "content": {"image/jpeg": {"schema": {"type": "string", "format": "binary"}}},
        },
        "416": {
            "description": "Requested byte range is outside the image.",
            "headers": {
                "Accept-Ranges": {"schema": {"type": "string", "const": "bytes"}},
                "Content-Range": {"required": True, "schema": {"type": "string"}},
                "ETag": {"schema": {"type": "string"}},
            },
        },
    },
    ("get", "/v1/copytext"): {
        "200": {
            "description": "Current copy-text document.",
            "headers": {"ETag": {"required": True, "schema": {"type": "string"}}},
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/GenericJsonResponse"}}},
        },
        "304": {
            "description": "The supplied If-None-Match value still identifies the current document.",
            "headers": {"ETag": {"required": True, "schema": {"type": "string"}}},
        },
    },
    ("post", "/v1/model_api/chat/send"): {
        "202": {
            "description": "Turn accepted for asynchronous processing.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/HostedChatAcceptedResponse"}}},
        }
    },
    ("post", "/v1/model_api/models"): {
        "200": {
            "description": "The account's visible model catalog for this provider.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ModelApiModelsResponse"}}},
        }
    },
    ("post", "/v1/vision/main/test"): {
        "200": {
            "description": "Main-model image capability reached a terminal verdict.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/VisionMainTestResponse"}}},
        },
        "202": {
            "description": "A hidden resident image-capability probe is in progress.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/VisionMainTestResponse"}}},
        },
        "409": {
            "description": "The resident must be updated before hidden validation is available.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/GenericJsonResponse"}}},
        },
    },
    ("post", "/v1/notify-relay/register"): {
        "200": {
            "description": "Relay enrollment created or refreshed; the relay auth token is returned.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/NotifyRelayRegisterResponse"}}},
        }
    },
    ("post", "/v1/notify-relay/push"): {
        "200": {
            "description": "Relay attempt completed; status reports the APNs outcome (delivered or error).",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/NotifyRelayPushResponse"}}},
        }
    },
    ("post", "/v1/users/register"): {
        "201": {
            "description": "User created; the plaintext API key is returned once.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/RegisterResponse"}}},
        }
    },
    ("post", "/v1/access/link-token"): {
        "201": {
            "description": "One-time link token created. It expires after the returned TTL.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/LinkTokenResponse"}}},
        }
    },
    ("post", "/v1/access/claim-token"): {
        "201": {
            "description": "Link token consumed and an additional API key issued.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/IssuedApiKeyResponse"}}},
        }
    },
    ("post", "/v1/account/recover/challenge"): {
        "200": {
            "description": "Single-use encrypted recovery challenge created.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/RecoverChallengeResponse"}}},
        }
    },
    ("post", "/v1/account/recover/verify"): {
        "200": {
            "description": "Challenge verified and an additional API key issued.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/IssuedApiKeyResponse"}}},
        }
    },
    ("post", "/v1/memory/add"): {
        "201": {
            "description": "Encrypted memory created.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/GenericJsonResponse"}}},
        }
    },
    ("post", "/v1/onboarding/archive"): {
        "201": {
            "description": "Archive uploaded.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/GenericJsonResponse"}}},
        }
    },
    ("post", "/v1/diagnostics/logs"): {
        "201": {
            "description": "Diagnostic log uploaded.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/GenericJsonResponse"}}},
        }
    },
    ("post", "/v1/perception/report"): {
        "200": {
            "description": "Report processed; inspect each per-signal result.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/PerceptionReportResponse"}}},
        }
    },
    ("post", "/v1/identity/init"): {
        "201": {
            "description": "Encrypted identity created.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/GenericJsonResponse"}}},
        }
    },
    ("post", "/v1/genesis/imports"): {
        "201": {
            "description": "Import job created.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/GenericJsonResponse"}}},
        }
    },
    ("post", "/v1/genesis/imports/plaintext"): {
        "200": {
            "description": "Previously completed matching plaintext import reused.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/GenericJsonResponse"}}},
        },
        "202": {
            "description": "Plaintext import accepted for asynchronous processing.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/GenericJsonResponse"}}},
        },
        "409": {
            "description": "Another plaintext import is processing for this account. The response error is import_job_active and active_job_id identifies it.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
        },
    },
    ("post", "/v1/genesis/imports/plaintext/estimate"): {
        "201": {
            "description": "Material parsed and encrypted-staged; no LLM call was made.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/GenericJsonResponse"}}},
        },
    },
    ("post", "/v1/genesis/imports/plaintext/commit"): {
        "200": {
            "description": "A previously completed matching import was reused and the stage consumed.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/GenericJsonResponse"}}},
        },
        "202": {
            "description": "The staged import was committed and processing started.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/GenericJsonResponse"}}},
        },
        "404": {
            "description": "staged_import_not_found: the staged_id does not exist for this account.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
        },
        "409": {
            "description": "The stage was already consumed, or another plaintext import is processing.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
        },
        "410": {
            "description": "staged_import_expired: the encrypted staged payload has expired.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
        },
    },
    ("post", "/v1/genesis/imports/{job_id}/finalize"): {
        "202": {
            "description": "Upload finalized and awaiting reducer output.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/GenericJsonResponse"}}},
        }
    },
    ("post", "/v1/genesis/persona_backfill"): {
        "202": {
            "description": "Persona backfill enqueued.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/GenericJsonResponse"}}},
        }
    },
    ("post", "/v1/history_import/upload"): {
        "202": {
            "description": "History import queued or resumed.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/GenericJsonResponse"}}},
        }
    },
    ("post", "/v1/model_api/memory/repair"): {
        "202": {
            "description": "Memory repair job queued for asynchronous processing.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/GenericJsonResponse"}}},
        }
    },
    ("get", "/v1/model_api/usage"): {
        "200": {
            "description": "Live provider balance/usage snapshot. HTTP 200 does not imply every metric succeeded — check top-level `status` and each metric's own `status`.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ModelApiUsageResponse"}}},
        },
        "400": {
            "description": (
                "No usable model_api route to query: model_api_not_configured (no active route), "
                "model_api_not_tested (route saved but never passed a connection test), "
                "model_api_key_envelope_missing, model_api_key_decrypt_failed, or "
                "model_api_config_invalid."
            ),
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
        },
    },
    ("post", "/v1/proactive/decisions/{decision_id}/review"): {
        "200": {
            "description": "Review recorded. Form submissions requesting HTML receive a small confirmation page.",
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/GenericJsonResponse"}},
                "text/html": {"schema": {"type": "string"}},
            },
        }
    },
    ("get", "/v1/mcp/servers"): {
        "200": {
            "description": "The caller's user-configured MCP servers.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/McpServerListResponse"}}},
        }
    },
    ("post", "/v1/mcp/servers"): {
        "200": {
            "description": "Server created or updated.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/McpServerRecord"}}},
        },
        "409": {
            "description": "cannot_encrypt — the server-side envelope could not be built; retry.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
        },
    },
    ("patch", "/v1/mcp/servers/{name}"): {
        "200": {
            "description": "Updated server record.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/McpServerRecord"}}},
        },
        "409": {
            "description": "cannot_encrypt — the old config is unchanged; retry.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
        },
        "404": {
            "description": "not_found — no server with that name exists.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
        },
    },
    ("delete", "/v1/mcp/servers/{name}"): {
        "200": {
            "description": "Server deleted.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/McpServerDeleteResponse"}}},
        },
        "404": {
            "description": "not_found — no server with that name exists.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
        },
    },
    ("post", "/v1/mcp/servers/{name}/test"): {
        "200": {
            "description": "Probe succeeded; the server answered initialize and tools/list.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/McpServerTestResponse"}}},
        },
        "404": {
            "description": "not_found — no server with that name exists.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
        },
    },
}


def _json_request_body(schema_name: str, *, required: bool) -> dict[str, Any]:
    return {
        "required": required,
        "content": {
            "application/json": {
                "schema": {"$ref": f"#/components/schemas/{schema_name}"}
            }
        },
    }


def _append_parameters(operation: dict[str, Any], additions: list[dict[str, Any]]) -> None:
    parameters = list(operation.get("parameters") or [])
    seen = {
        (str(parameter.get("in")), str(parameter.get("name")).lower())
        for parameter in parameters
        if isinstance(parameter, dict)
    }
    for parameter in additions:
        key = (parameter["in"], parameter["name"].lower())
        if key not in seen:
            parameters.append(deepcopy(parameter))
            seen.add(key)
    operation["parameters"] = parameters


def _ensure_response_contract(operation: dict[str, Any]) -> None:
    responses = operation.setdefault("responses", {})
    # FastAPI infers 422 for typed path parameters, but the application-level
    # validation handler deliberately normalizes those failures to HTTP 400.
    if responses.pop("422", None) is not None:
        responses.setdefault(
            "400",
            {
                "description": "Invalid path or request parameter.",
                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
            },
        )
    for status, response in responses.items():
        if not str(status).startswith("2") or not isinstance(response, dict):
            continue
        content = response.setdefault("content", {})
        json_content = content.setdefault("application/json", {})
        schema = json_content.get("schema")
        if not isinstance(schema, dict) or not schema:
            json_content["schema"] = {"$ref": "#/components/schemas/GenericJsonResponse"}
    responses.setdefault(
        "default",
        {
            "description": "Error response.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
        },
    )


def apply_public_contracts(schema: dict[str, Any]) -> dict[str, Any]:
    """Mutate and return an already-filtered public OpenAPI document."""
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    for name, component in COMPONENT_SCHEMAS.items():
        schemas[name] = deepcopy(component)

    for path, path_item in schema.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            key = (method.lower(), path)
            if method.lower() not in {"get", "post", "put", "patch", "delete"} or not isinstance(operation, dict):
                continue

            if key in OPERATION_PARAMETERS:
                _append_parameters(operation, OPERATION_PARAMETERS[key])

            if key in SPECIAL_REQUEST_BODIES:
                operation["requestBody"] = deepcopy(SPECIAL_REQUEST_BODIES[key])
                operation["x-feedling-contract-level"] = "documented"
            elif key in PRECISE_JSON_BODIES:
                operation["requestBody"] = _json_request_body(
                    PRECISE_JSON_BODIES[key],
                    required=key not in OPTIONAL_PRECISE_BODIES,
                )
                operation["x-feedling-contract-level"] = "documented"
            elif method.lower() in {"post", "put", "patch"} and key not in BODYLESS_OPERATIONS:
                operation["requestBody"] = _json_request_body(
                    "FreeFormJsonObject", required=False
                )
                operation["x-feedling-contract-level"] = "compatibility"
            else:
                operation["x-feedling-contract-level"] = "documented"

            if key in OPERATION_DESCRIPTIONS:
                operation["description"] = OPERATION_DESCRIPTIONS[key]

            _ensure_response_contract(operation)
            if key in RESPONSE_OVERRIDES:
                operation["responses"].update(deepcopy(RESPONSE_OVERRIDES[key]))
                # FastAPI inferred 200 from JSONResponse routes whose actual
                # success status is 201/202. Keep only the real success code.
                if key in {
                    ("post", "/v1/model_api/chat/send"),
                    ("post", "/v1/users/register"),
                    ("post", "/v1/access/link-token"),
                    ("post", "/v1/access/claim-token"),
                    ("post", "/v1/memory/add"),
                    ("post", "/v1/onboarding/archive"),
                    ("post", "/v1/diagnostics/logs"),
                    ("post", "/v1/identity/init"),
                }:
                    operation["responses"].pop("200", None)

    validate_public_contract(schema)
    return schema


def validate_public_contract(schema: dict[str, Any]) -> None:
    """Fail export when the public request contract regresses."""
    errors: list[str] = []
    operations: dict[Operation, dict[str, Any]] = {}
    for path, path_item in schema.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() in {"get", "post", "put", "patch", "delete"} and isinstance(operation, dict):
                operations[(method.lower(), path)] = operation

    for key, operation in operations.items():
        method, path = key
        has_body = isinstance(operation.get("requestBody"), dict)
        expects_body = (
            key in PRECISE_JSON_BODIES
            or key in SPECIAL_REQUEST_BODIES
            or (method in {"post", "put", "patch"} and key not in BODYLESS_OPERATIONS)
        )
        if expects_body and not has_body:
            errors.append(f"{method.upper()} {path} is missing requestBody")
        if key in BODYLESS_OPERATIONS and has_body:
            errors.append(f"{method.upper()} {path} must remain explicitly bodyless")

        parameter_keys: list[tuple[str, str]] = []
        for parameter in operation.get("parameters") or []:
            if isinstance(parameter, dict) and "$ref" not in parameter:
                parameter_keys.append((str(parameter.get("in")), str(parameter.get("name")).lower()))
        if len(parameter_keys) != len(set(parameter_keys)):
            errors.append(f"{method.upper()} {path} has duplicate parameters")

        success_schemas = []
        for status, response in (operation.get("responses") or {}).items():
            if str(status).startswith("2") and isinstance(response, dict):
                for media in (response.get("content") or {}).values():
                    if isinstance(media, dict):
                        success_schemas.append(media.get("schema"))
        if not success_schemas or not any(isinstance(item, dict) and item for item in success_schemas):
            errors.append(f"{method.upper()} {path} has no non-empty success schema")

    for key in OPERATION_PARAMETERS:
        if key not in operations:
            errors.append(f"parameter contract targets missing operation {key[0].upper()} {key[1]}")

    if ("post", "/v1/copytext") in operations:
        errors.append("operator-only POST /v1/copytext leaked into the public schema")
    copytext_get = operations.get(("get", "/v1/copytext"))
    if copytext_get is None or copytext_get.get("security") != []:
        errors.append("public GET /v1/copytext must have security=[]")

    if errors:
        raise ValueError("invalid public OpenAPI contract:\n- " + "\n- ".join(errors))
