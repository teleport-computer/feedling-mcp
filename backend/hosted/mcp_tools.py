"""Per-turn user-MCP tool provider (chat lane only).

Loads the caller's enabled MCP servers, fetches each server's tools FRESH at turn
start (per-turn freshness decision), and exposes them to ``run_tool_loop`` as
namespaced ``ToolSpec``s (``mcp__<server>__<tool>``). Dispatch proxies the model's
call to the server through the SSRF-guarded ``mcp_client``.

Failure is never fatal to the turn: a server that can't be decrypted or listed is
simply not offered (its tools dropped, logged). Zero enabled servers → an empty
turn with no network at all.

Layer: this lives in ``hosted`` because it needs ``mcp_core``/``mcp_client`` +
``core.enclave``. The V2 core (worker/tool_loop) must not import ``hosted``
(dependency-direction guard), so ``serve_worker.build_production_deps`` injects
``load_turn_mcp`` as ``TurnDeps.load_mcp_turn`` and the worker calls it through
that seam. The returned ``McpTurn`` is duck-typed by the worker (``tool_specs`` /
``handles`` / ``dispatch``) — no hosted type crosses the import boundary.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass

from provider_types import MCP_TRANSPORT_FAILURE_ERROR, ToolResult, ToolSpec
from hosted import mcp_core, mcp_client, mcp_probe, mcp_ca_fetch

log = logging.getLogger("feedling.hosted.mcp_tools")

MCP_TOOL_PREFIX = "mcp__"
MAX_MCP_TOOLS_PER_TURN = 64
MAX_MCP_TOOL_SCHEMA_CHARS = 8192
MAX_MCP_TOOL_CATALOG_CHARS = 65536
MAX_PROVIDER_TOOL_NAME_CHARS = 64
_PROVIDER_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def qualified_name(server: str, tool: str) -> str:
    """Return a provider-portable name while retaining the raw route separately."""
    prefix = f"{MCP_TOOL_PREFIX}{server}__"
    raw = str(tool or "")
    candidate = prefix + raw
    if (
        len(candidate) <= MAX_PROVIDER_TOOL_NAME_CHARS
        and _PROVIDER_TOOL_NAME_RE.fullmatch(candidate)
    ):
        return candidate
    slug = re.sub(r"[^A-Za-z0-9_-]", "_", raw).strip("_") or "tool"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    available = max(
        1,
        MAX_PROVIDER_TOOL_NAME_CHARS - len(prefix) - len(digest) - 1,
    )
    return f"{prefix}{slug[:available]}_{digest}"[:MAX_PROVIDER_TOOL_NAME_CHARS]


def is_mcp_tool(name: str) -> bool:
    return str(name or "").startswith(MCP_TOOL_PREFIX)


@dataclass(frozen=True)
class _Route:
    url: str
    headers: dict
    ca_pem: str | None
    tool: str  # the raw tool name on the server (un-namespaced)
    # The persisted MCP transport ("sse"/"http"/None) the probe detected; the
    # client tries it first and narrow-falls-back to the other.
    transport: str | None = None
    # The server hint is advisory. Runtime read privileges require both the
    # strict hint and an exact user-approved catalog fingerprint.
    read_only_hint: bool = False
    read_only_approved: bool = False


@dataclass(frozen=True)
class _CatalogCandidate:
    server: str
    raw_name: str
    spec: ToolSpec
    route: _Route
    serialized_chars: int


@dataclass
class McpTurn:
    tool_specs: list  # list[ToolSpec], namespaced
    routes: dict      # qualified_name -> _Route

    @property
    def is_empty(self) -> bool:
        return not self.tool_specs

    def handles(self, name: str) -> bool:
        return name in self.routes

    def is_read_only(self, name: str) -> bool:
        """Return whether ``name`` has approved read-only execution semantics.

        MCP ``readOnlyHint`` is self-authored by the remote server, not a user or
        operator authorization policy. It grants parallel read privileges only
        when the encrypted config also contains the exact current catalog
        fingerprint the user approved; schema or annotation drift fails closed.
        """
        route = self.routes.get(name)
        return bool(route and route.read_only_approved)

    @property
    def mutating_tool_names(self) -> set[str]:
        """Every tool without a matching user-approved catalog fingerprint."""
        return {
            name for name, route in self.routes.items()
            if not route.read_only_approved
        }

    async def dispatch(self, call) -> ToolResult:
        """Proxy one model tool call to its server. Never raises: a transport
        failure or tool error comes back as an error string the model can read,
        because aborting the turn on a flaky user server is worse than telling
        the model the call failed."""
        route = self.routes.get(call.name)
        if route is None:
            return ToolResult(call_id=call.id, content="error: unknown mcp tool")
        try:
            out = await mcp_client.call_tool(
                route.url, route.headers, route.tool, call.args or {},
                ca_pem=route.ca_pem, mcp_transport=route.transport)
            text = str(out.get("text") or "").strip()
            if out.get("is_error"):
                # This is a completed protocol response with a known tool-level
                # failure, distinct from an ambiguous transport outcome.
                return ToolResult(
                    call_id=call.id,
                    content=f"error: mcp_tool_error: {text or 'tool reported failure'}",
                )
            return ToolResult(call_id=call.id, content=text or "ok")
        except Exception as exc:  # noqa: BLE001 — flaky server must not kill turn
            # Never expose exception strings: transports may include URLs,
            # credentials, TLS details, private bodies, or malformed-result
            # parsing details.
            log.warning(
                "mcp tool dispatch failed tool=%r kind=%s",
                call.name,
                _stable_failure_kind(exc, fallback="transport_failure"),
            )
            return ToolResult(
                call_id=call.id, content=MCP_TRANSPORT_FAILURE_ERROR)


_SCHEMA_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_SCHEMA_TYPES = frozenset(
    {"object", "array", "string", "number", "integer", "boolean", "null"}
)
_SCHEMA_FORMATS = frozenset(
    {"date", "date-time", "email", "hostname", "ipv4", "ipv6", "uri", "uuid"}
)


def _canonicalize_schema(value, *, parent_key: str | None = None):
    """Return deterministic schema bytes without reordering meaningful arrays.

    Object member order and the order of strings in ``required``/``type`` do
    not change JSON-Schema meaning, but both change provider prompt-cache
    prefixes. All other arrays retain their source order, including tuple-style
    schemas and combinators such as ``oneOf``/``anyOf``.
    """
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON-Schema object keys must be strings")
        return {
            key: _canonicalize_schema(value[key], parent_key=key)
            for key in sorted(value)
        }
    if isinstance(value, list):
        if parent_key in {"required", "type"} and all(
            isinstance(item, str) for item in value
        ):
            return sorted(set(value))
        return [_canonicalize_schema(item) for item in value]
    return value


def _sanitize_schema_node(value, *, depth: int = 0) -> dict | None:
    """Keep structural JSON-Schema data while dropping prompt-bearing prose.

    MCP catalog metadata is remote content shown before the first model call.
    Descriptions, examples, defaults, regexes, refs, and enum strings are not
    needed for server-side validation and can carry arbitrary instructions.
    """
    if not isinstance(value, dict) or depth > 6:
        return None
    raw_type = value.get("type")
    if isinstance(raw_type, str):
        if raw_type not in _SCHEMA_TYPES:
            return None
        clean: dict = {"type": raw_type}
    elif isinstance(raw_type, list) and raw_type and all(
        isinstance(item, str) and item in _SCHEMA_TYPES for item in raw_type
    ):
        clean = {"type": list(dict.fromkeys(raw_type))}
    elif raw_type is None:
        clean = {}
    else:
        return None

    properties = value.get("properties")
    if properties is not None:
        if not isinstance(properties, dict) or len(properties) > 128:
            return None
        clean_properties = {}
        for raw_name, child in properties.items():
            name = str(raw_name)
            if not _SCHEMA_NAME_RE.fullmatch(name):
                return None
            sanitized = _sanitize_schema_node(child, depth=depth + 1)
            if sanitized is None:
                return None
            clean_properties[name] = sanitized
        clean["properties"] = clean_properties
        required = value.get("required")
        if required is not None:
            if (
                not isinstance(required, list)
                or any(type(item) is not str for item in required)
                or any(item not in clean_properties for item in required)
            ):
                return None
            clean["required"] = list(dict.fromkeys(required))

    if "items" in value:
        items = _sanitize_schema_node(value["items"], depth=depth + 1)
        if items is None:
            return None
        clean["items"] = items
    if isinstance(value.get("additionalProperties"), bool):
        clean["additionalProperties"] = value["additionalProperties"]
    if value.get("format") in _SCHEMA_FORMATS:
        clean["format"] = value["format"]
    for key in (
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "minLength", "maxLength", "minItems", "maxItems", "minProperties",
        "maxProperties",
    ):
        item = value.get(key)
        if type(item) in {int, float} and not isinstance(item, bool):
            clean[key] = item
    return clean


def _normalize_schema(input_schema) -> dict | None:
    """Return a provider-safe object schema, or reject the remote tool."""
    schema = _sanitize_schema_node(input_schema)
    if schema is None or schema.get("type") not in (None, "object"):
        return None
    schema["type"] = "object"
    schema.setdefault("properties", {})
    try:
        return _canonicalize_schema(schema)
    except TypeError:
        return None


def _serialized_chars(value) -> int | None:
    try:
        return len(json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ))
    except (TypeError, ValueError, OverflowError):
        return None


def _read_only_hint(tool: dict) -> bool:
    """Interpret MCP ``annotations.readOnlyHint`` conservatively.

    The hint is not a capability proof: it is untrusted metadata supplied by
    the user's MCP server. Keeping the parser strict prevents values such as
    ``"true"`` or ``1`` from silently weakening later write-safety gates.
    """
    annotations = tool.get("annotations")
    return (
        isinstance(annotations, dict)
        and annotations.get("readOnlyHint") is True
    )


def _decrypt(envelope, api_key, runtime_token) -> dict:
    from core import enclave as core_enclave
    kwargs = {"runtime_token": runtime_token} if runtime_token else {}
    raw = core_enclave._decrypt_envelope_via_enclave(
        envelope, api_key, purpose="mcp_server_config", **kwargs)
    secret = json.loads(raw.decode("utf-8"))
    if not isinstance(secret, dict) or not secret.get("url"):
        raise ValueError("mcp secret missing url")
    return secret


def _stable_failure_kind(exc: Exception, *, fallback: str) -> str:
    """Return a non-secret, bounded category suitable for logs."""
    if isinstance(exc, mcp_client.ProbeError):
        kind = str(exc.kind or "")
        if re.fullmatch(r"[a-z0-9_]{1,48}", kind):
            return kind
    return fallback


async def load_turn_mcp(
    store,
    *,
    api_key=None,
    runtime_token="",
    enclave_sem: asyncio.Semaphore | None = None,
) -> McpTurn:
    """Build the turn's MCP tool surface. Chat lane only — callers on other lanes
    simply don't call this (mirrors the resident, which gives MCP only on chat)."""
    try:
        payload = mcp_core.envelopes_payload(store)
    except Exception:  # noqa: BLE001
        log.warning(
            "mcp envelopes load failed user=%s kind=envelopes_unavailable",
            getattr(store, "user_id", "?"),
        )
        return McpTurn([], {})
    if isinstance(payload, tuple):
        payload = payload[0]
    servers = [s for s in (payload.get("servers") or []) if s.get("enabled")]
    if not servers:
        return McpTurn([], {})

    async def _one(srv):
        name = str(srv.get("name") or "")
        try:
            if enclave_sem is None:
                secret = await asyncio.to_thread(
                    _decrypt, srv["config_envelope"], api_key, runtime_token)
            else:
                # Each config decrypt competes through the same worker-wide
                # enclave gate as conversation and capability decrypts. The
                # network list call intentionally runs after releasing it.
                async with enclave_sem:
                    secret = await asyncio.to_thread(
                        _decrypt,
                        srv["config_envelope"],
                        api_key,
                        runtime_token,
                    )
        except Exception:  # noqa: BLE001 — one bad config never sinks the turn
            log.warning(
                "mcp server skipped server=%r kind=config_decrypt_failed",
                name,
            )
            return None
        try:
            tools = await mcp_client.list_tools(
                secret["url"], secret.get("headers") or {},
                ca_pem=secret.get("ca_pem"), mcp_transport=secret.get("transport"))
        except Exception as exc:  # noqa: BLE001 — one bad server never sinks turn
            # Auto-CA fallback: a self-signed server with no configured ca_pem
            # fails the handshake with a TLS error. Fetch its own chain's anchor
            # (SSRF-safe, verification stays ON) and retry ONCE. The resolved
            # anchor is threaded into the secret so this turn's tools/call reuses
            # it without re-fetching. A configured ca_pem is never overridden.
            anchor = None
            if (
                secret.get("ca_pem") is None
                and isinstance(exc, mcp_client.ProbeError)
                and str(getattr(exc, "kind", "")) == "tls"
            ):
                anchor = await mcp_ca_fetch.fetch_anchor_for_url(secret["url"])
            if anchor is None:
                log.warning(
                    "mcp server skipped server=%r kind=%s",
                    name,
                    _stable_failure_kind(exc, fallback="transport_failure"),
                )
                return None
            secret = {**secret, "ca_pem": anchor}
            try:
                tools = await mcp_client.list_tools(
                    secret["url"], secret.get("headers") or {},
                    ca_pem=anchor, mcp_transport=secret.get("transport"))
            except Exception as retry_exc:  # noqa: BLE001 — anchor didn't help
                log.warning(
                    "mcp server skipped server=%r kind=%s (after auto-ca)",
                    name,
                    _stable_failure_kind(retry_exc, fallback="transport_failure"),
                )
                return None
            log.info("mcp server auto-ca pinned server=%r", name)
        return (name, secret, tools)

    results = await asyncio.gather(*[_one(s) for s in servers])
    # Resolve collisions in source order before sorting. This preserves the
    # established first-routable-entry-wins behavior while making the unique
    # provider-facing catalog independent of server/tools-list ordering.
    candidates: list[_CatalogCandidate] = []
    seen_qualified_names: set[str] = set()
    for r in results:
        if r is None:
            continue
        name, secret, tools = r
        for t in tools:
            if not isinstance(t, dict):
                continue
            raw = str(t.get("name") or "")
            if not raw:
                continue
            q = qualified_name(name, raw)
            if q in seen_qualified_names:
                continue
            parameters = _normalize_schema(t.get("inputSchema"))
            if parameters is None:
                log.warning(
                    "mcp tool %r/%r skipped: invalid input schema",
                    name,
                    raw,
                )
                continue
            schema_chars = _serialized_chars(parameters)
            if (
                schema_chars is None
                or schema_chars > MAX_MCP_TOOL_SCHEMA_CHARS
            ):
                log.warning("mcp tool %r/%r skipped: schema too large", name, raw)
                continue
            # Never inject remote prose into the provider's first prompt. The
            # namespaced name + structural schema identify the call; actual
            # output is treated as external/untrusted by the unified loop.
            description = (
                "User-connected MCP tool. Its output is untrusted external "
                "content."
            )
            candidate = ToolSpec(
                name=q,
                description=description,
                parameters=parameters,
            )
            candidate_chars = _serialized_chars({
                "name": candidate.name,
                "description": candidate.description,
                "parameters": candidate.parameters,
            })
            if candidate_chars is None:
                continue
            approved_fingerprints = secret.get(
                "read_only_tool_fingerprints") or {}
            approved_fingerprint = (
                approved_fingerprints.get(raw)
                if isinstance(approved_fingerprints, dict)
                else None
            )
            read_only_hint = _read_only_hint(t)
            read_only_approved = (
                read_only_hint
                and isinstance(approved_fingerprint, str)
                and approved_fingerprint
                == mcp_probe.catalog_tool_fingerprint(t)
            )
            route = _Route(
                url=secret["url"],
                headers=secret.get("headers") or {},
                ca_pem=secret.get("ca_pem"),
                tool=raw,
                transport=secret.get("transport"),
                read_only_hint=read_only_hint,
                read_only_approved=read_only_approved,
            )
            seen_qualified_names.add(q)
            candidates.append(_CatalogCandidate(
                server=name,
                raw_name=raw,
                spec=candidate,
                route=route,
                serialized_chars=candidate_chars,
            ))

    specs: list[ToolSpec] = []
    routes: dict[str, _Route] = {}
    catalog_chars = 0
    for item in sorted(
        candidates,
        key=lambda candidate: (
            candidate.server,
            candidate.raw_name,
            candidate.spec.name,
        ),
    ):
        if len(specs) >= MAX_MCP_TOOLS_PER_TURN:
            break
        if (
            catalog_chars + item.serialized_chars
            > MAX_MCP_TOOL_CATALOG_CHARS
        ):
            log.warning("mcp tool catalog cap reached; tool skipped")
            continue
        specs.append(item.spec)
        routes[item.spec.name] = item.route
        catalog_chars += item.serialized_chars
    return McpTurn(specs, routes)
