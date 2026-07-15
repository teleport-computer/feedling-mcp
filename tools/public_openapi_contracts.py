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
]


OPERATION_PARAMETERS: dict[Operation, list[dict[str, Any]]] = {
    ("get", "/v1/chat/poll"): [
        _query("since", TIMESTAMP, "Return messages newer than this Unix timestamp.", example=1783962000.0),
        _query("timeout", _schema("number", minimum=0, maximum=60, default=30), "Long-poll wait in seconds; the server caps it at 60.", example=30),
        _query("consumer_id", _schema("string", maxLength=160), "Stable resident consumer identifier.", example="resident_mbp_01"),
        _query("claim", _schema("boolean", default=True), "Whether this poll may claim queued work.", example=True),
        *CONSUMER_HEADERS,
    ],
    ("post", "/v1/chat/response"): [
        _query("consumer_id", _schema("string", maxLength=160), "Stable resident consumer identifier.", example="resident_mbp_01"),
        *CONSUMER_HEADERS,
    ],
    ("get", "/v1/chat/history"): [
        _query("limit", _schema("integer", minimum=1, maximum=200, default=200), "Maximum messages in this page.", example=50),
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
        _query("limit", _schema("integer", minimum=1, maximum=200, default=50), "Recommended page size; values above 200 are capped.", example=50),
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
                "enum": ["user_provider", "provider_transient", "system"],
                "description": "Stable responsibility classification when the endpoint can identify it.",
            },
            "request_id": {"type": "string", "description": "Support correlation identifier when available."},
        },
        "additionalProperties": True,
        "example": {"error": "invalid_payload", "detail": "The request body is invalid", "request_id": "req_abc123"},
    },
    "EncryptedEnvelope": {
        "type": "object",
        "required": ["body_ct", "nonce", "K_user", "visibility", "owner_user_id"],
        "properties": {
            "v": {"type": "integer", "const": 1, "default": 1},
            "id": {"type": "string"},
            "body_ct": {"type": "string", "description": "Base64 ciphertext."},
            "nonce": {"type": "string", "description": "Base64 nonce."},
            "K_user": {"type": "string", "description": "Content key wrapped for the user."},
            "K_enclave": {"type": "string", "description": "Content key wrapped for the enclave; required for shared visibility."},
            "visibility": {"type": "string", "enum": ["shared", "local_only"]},
            "owner_user_id": {"type": "string"},
            "enclave_pk_fpr": {"type": "string"},
        },
        "if": {
            "properties": {"visibility": {"const": "shared"}},
            "required": ["visibility"],
        },
        "then": {"required": ["K_enclave"]},
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
        "required": ["body_ct", "nonce", "K_user", "visibility", "owner_user_id", "type", "occurred_at"],
        "properties": {
            "v": {"type": "integer", "const": 1, "default": 1},
            "id": {"type": "string"},
            "body_ct": {"type": "string", "description": "Base64 ciphertext."},
            "nonce": {"type": "string", "description": "Base64 nonce."},
            "K_user": {"type": "string", "description": "Content key wrapped for the user."},
            "K_enclave": {"type": "string", "description": "Content key wrapped for the enclave; required for shared visibility."},
            "visibility": {"type": "string", "enum": ["shared", "local_only"]},
            "owner_user_id": {"type": "string"},
            "enclave_pk_fpr": {"type": "string"},
            "type": {"type": "string", "enum": ["moment", "quote", "fact", "event", "insight", "reflection"]},
            "occurred_at": {"type": "string", "minLength": 1, "description": "Plaintext ISO-8601 ordering metadata."},
            "source": {"type": "string", "default": "live_conversation"},
            "anchor_memory_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
        },
        "if": {
            "properties": {"visibility": {"const": "shared"}},
            "required": ["visibility"],
        },
        "then": {"required": ["K_enclave"]},
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
            "message": {"type": "string", "maxLength": 12000, "example": "Help me plan tomorrow."},
            "content": {"type": "string", "maxLength": 12000, "deprecated": True, "description": "Compatibility alias for message."},
            "context_refs": {"type": "array", "maxItems": 8, "items": {"$ref": "#/components/schemas/ChatContextReference"}},
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
            "envelope": {"$ref": "#/components/schemas/EncryptedEnvelope"},
            "content_type": {"type": "string", "enum": ["text", "image", "file"], "default": "text"},
            "file_name": {"type": "string"},
            "file_mime": {"type": "string"},
        },
        "additionalProperties": True,
    },
    "ChatResponseRequest": {
        "type": "object",
        "required": ["envelope"],
        "properties": {
            "envelope": {"$ref": "#/components/schemas/EncryptedEnvelope"},
            "source": {
                "type": "string",
                "enum": ["chat", "live_activity", "heartbeat", "verify_ping", "agent_initiated_proactive"],
                "default": "chat",
                "description": "Resident protocol source. Ordinary replies should use chat.",
            },
            "reply_to_message_id": {"type": "string", "description": "Parent user message; strongly recommended for duplicate-reply protection."},
            "content_type": {"type": "string", "enum": ["text", "image"], "default": "text"},
        },
        "additionalProperties": True,
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
        },
        "additionalProperties": True,
    },
    "MemoryAction": {
        "type": "object",
        "required": ["type"],
        "properties": {
            "type": {"type": "string", "enum": ["memory.add", "memory.supersede", "memory.delete", "memory.retype"]},
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
            "content_envelope": {"$ref": "#/components/schemas/EncryptedEnvelope"},
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
            "meta_envelope": {"$ref": "#/components/schemas/EncryptedEnvelope"},
            "exif_gps": {"type": "object", "deprecated": True, "description": "Legacy compatibility only; clients should not upload raw EXIF GPS."},
        },
        "additionalProperties": False,
    },
}


PRECISE_JSON_BODIES: dict[Operation, str] = {
    ("post", "/v1/users/register"): "RegisterRequest",
    ("post", "/v1/access/link-token"): "LinkTokenRequest",
    ("post", "/v1/access/claim-token"): "ClaimTokenRequest",
    ("post", "/v1/account/recover/challenge"): "RecoverChallengeRequest",
    ("post", "/v1/account/recover/verify"): "RecoverVerifyRequest",
    ("post", "/v1/account/reset"): "AccountResetRequest",
    ("post", "/v1/model_api/chat/send"): "HostedChatSendRequest",
    ("post", "/v1/chat/message"): "ChatTransportRequest",
    ("post", "/v1/chat/response"): "ChatResponseRequest",
    ("delete", "/v1/chat/history"): "ChatHistoryClearRequest",
    ("post", "/v1/memory/index"): "MemoryIndexRequest",
    ("post", "/v1/memory/fetch"): "MemoryFetchRequest",
    ("post", "/v1/memory/actions"): "MemoryActionsRequest",
    ("post", "/v1/memory/add"): "MemoryAddRequest",
    ("post", "/v1/memory/retype"): "MemoryRetypeRequest",
    ("post", "/v1/perception/report"): "PerceptionReportRequest",
    ("post", "/v1/perception/photo/evaluate"): "PerceptionPhotoEvaluateRequest",
}

# These handlers deliberately accept an absent body and apply server defaults.
OPTIONAL_PRECISE_BODIES: set[Operation] = {
    ("post", "/v1/users/register"),
    ("post", "/v1/access/link-token"),
    ("post", "/v1/memory/index"),
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
    ("post", "/v1/model_api/chat/send"): "Queue an asynchronous hosted-agent turn. A successful response is always 202 and never contains a plaintext assistant reply.",
    ("get", "/v1/chat/history"): "Read encrypted chat history using timestamp watermarks. Use oldest_ts as before for older pages and latest_ts as since for newer pages.",
    ("post", "/v1/memory/index"): "Return lightweight memory cards. This is selection, not full-content retrieval; query is intentionally not exposed because it is not a search filter today.",
    ("post", "/v1/memory/fetch"): "Fetch full records for selected memory IDs. Sensitive fetch behavior is not part of the current public contract.",
    ("post", "/v1/memory/actions"): "Apply up to 20 memory actions in order. The batch is not transactional and Idempotency-Key is not supported.",
    ("post", "/v1/perception/report"): "Submit device context. Sensitive signals must use encrypted envelopes; inspect each results entry even when HTTP status is 200.",
    ("get", "/v1/perception/app_open"): "Legacy iOS Shortcut compatibility endpoint. This GET records an event and therefore has side effects.",
    ("post", "/v1/users/register"): "Create a Feedling user and issue its first API key. The key is returned once and this operation is not idempotent.",
    ("post", "/v1/access/claim-token"): "Consume a one-time link token and issue an additional API key. Existing keys remain active.",
    ("post", "/v1/account/recover/verify"): "Verify keypair possession and issue an additional API key for the existing account. Existing keys remain active.",
    ("post", "/v1/account/reset"): "Permanently delete the account, its data, and all of its API keys. This is not a per-key revocation endpoint.",
    ("post", "/v1/genesis/imports/plaintext"): "Queue an asynchronous plaintext import. In update_identity mode, the uploaded role card creates an identity when absent or updates the existing card while preserving its relationship anchor by default. Reusing a completed client_job_id returns the existing job.",
}


RESPONSE_OVERRIDES: dict[Operation, dict[str, Any]] = {
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
        }
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
    ("post", "/v1/proactive/decisions/{decision_id}/review"): {
        "200": {
            "description": "Review recorded. Form submissions requesting HTML receive a small confirmation page.",
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/GenericJsonResponse"}},
                "text/html": {"schema": {"type": "string"}},
            },
        }
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
