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
from dataclasses import dataclass, field

from provider_types import MCP_TRANSPORT_FAILURE_ERROR, ToolResult, ToolSpec
from hosted import mcp_core, mcp_client, mcp_probe, mcp_ca_fetch

log = logging.getLogger("feedling.hosted.mcp_tools")

MCP_TOOL_PREFIX = "mcp__"
# 每轮交给模型的 MCP 工具数上限。**这个数是实测出来的,不是拍的**
# (2026-08-10,tools/e2e/tool_count_ceiling_probe.py,可复跑)。
#
# 此前三条路三个值、且都没有依据:V2=64(2026-07-18 加,常量旁一行注释都没有)、
# pi=50→100、claude/codex 无上限 —— 同一个用户换个 driver 行为就变。
#
# 实测(每档 3 次,易混淆模式:塞入预报/历史/空气质量等 6 个近义工具,
# 逼模型真读说明才能选对;目标工具放在列表中间,防"选第一个"蒙对):
#
#   工具数   sonnet-4.6   gemini-flash   deepseek   sonnet prompt_tokens
#      16      3/3           3/3          3/3           4,267
#      64      3/3           3/3          3/3          12,475
#     128      3/3           3/3          3/3          23,419
#     300      3/3           3/3          3/3          52,830
#     500      3/3           3/3          3/3          86,035   (简单模式)
#
# 三个结论:
#   ① **没有硬墙** —— 一路到 500 个 / 225KB schema,openrouter(sonnet、
#      gemini-flash)与 deepseek 直连都没拒收。原先担心的"撞 provider 函数上限
#      导致整轮失败"在我们实际用的路上不存在。
#   ② **选择准确率不是瓶颈** —— 弱模型在 300 个 + 6 个近义干扰项下仍全对。
#   ③ 真正的代价是 **token,而且每轮都付**。所以阈值该按"愿意为工具面付多少
#      上下文"来定,不是按"会不会坏"。
#
# 选 128 的理由:功能上远在任何边界之下;成本约 23k token(200k 窗口的 12%),
# 且这是所有服务器开满的最坏情况;并且能把本月工具最多的真实用户
# (usr_1baf,6 台共 107 个)**整个装下,一个都不用裁**。
MAX_MCP_TOOLS_PER_TURN = 128
# 2026-08-10 上调:同一天开始 schema 里带上说明/enum/default,体积自然变大。
# 不上调的话,原来能用的工具会**新**撞上这道闸而被丢掉 —— 而丢弃只有一行
# log.warning,又是一次静默失败。丢弃数现在也进 summary(见 _allocate_round_robin),
# 所以真撞上了运维看得见。
MAX_MCP_TOOL_SCHEMA_CHARS = 32768
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
    # 本轮工具面的分配摘要。**不是给模型看的**,是给运维看的:
    # 「这一轮模型到底看得到哪些 MCP 工具」以前在 V2 上完全不可观测,只有一行
    # log.warning,进不了 admin。usr_1baf 那次就是因此只能靠用户报 + 猜
    # (pi 那条路已有 mcp.surface.* 埋点,V2 一直没有 —— 又是一次只覆盖一条 lane)。
    # serve_worker 拿它落 debug trace;字段与 pi 那侧对齐,方便同一套排查。
    summary: dict = field(default_factory=dict)

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
    """把远端 schema 规整成 provider 一定收得下的形状 —— 但**不再剥说明**。

    2026-08-10 Seven 定稿:参数说明也要原样给模型。原本这里只留结构
    (type/properties/required/数值边界),把 description/title/enum/default/
    examples/pattern 全剥掉——模型看到的是 `{address: 字符串, city: 字符串}`,
    不知道该填什么;`enum` 被剥掉更要命,本来只能填 celsius/fahrenheit 的参数
    变成"随便填个字符串"。而 pi 桥那条路一直是**整个原样透传**的,
    又是同一个产品两条路给模型看的东西完全不同。

    保留的仍然是**结构校验**:深度上限、属性数量上限、类型白名单、属性名正则。
    那道闸挡的是「畸形 schema 让 provider 整个拒收」——一个坏工具会连累这一轮
    **所有**工具,和注入是两回事,不能一起放开。
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
    # 说明性字段:原样带上。模型靠它们知道参数是什么、能填什么。
    for key in ("description", "title"):
        text = value.get(key)
        if isinstance(text, str) and text.strip():
            clean[key] = text
    # enum / default / examples 是**取值**信息,对"填得对"和说明同样关键。
    # 只做可 JSON 序列化的基本类型检查,不改内容。
    raw_enum = value.get("enum")
    if isinstance(raw_enum, list) and raw_enum and len(raw_enum) <= 128 and all(
        item is None or isinstance(item, (str, int, float, bool))
        for item in raw_enum
    ):
        clean["enum"] = list(raw_enum)
    if isinstance(value.get("pattern"), str):
        clean["pattern"] = value["pattern"]
    for key in ("default", "examples"):
        item = value.get(key)
        if isinstance(item, (str, int, float, bool, list, dict)):
            try:
                json.dumps(item)
            except (TypeError, ValueError):
                continue
            clean[key] = item
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
    from core import envelope as core_envelope
    kwargs = {"runtime_token": runtime_token} if runtime_token else {}
    raw = core_envelope.read_envelope_body(
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
    # 因 schema 太大 / 结构非法被整个丢掉的工具。以前只有 log.warning ——
    # 模型少了一个工具,而运维在 admin 里什么都看不到。
    schema_rejected: list[str] = []
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
                schema_rejected.append(f"{name}/{raw}")
                continue
            schema_chars = _serialized_chars(parameters)
            if (
                schema_chars is None
                or schema_chars > MAX_MCP_TOOL_SCHEMA_CHARS
            ):
                log.warning("mcp tool %r/%r skipped: schema too large", name, raw)
                schema_rejected.append(f"{name}/{raw}")
                continue
            # 原样透传服务器写的说明。
            #
            # 这里原本把每个工具的说明都换成同一句「用户连接的 MCP 工具,输出
            # 不可信」——安全上省事,代价是模型只剩名字和参数名可看:很多工具名
            # 是缩写,它不知道该什么时候用、参数该怎么填,于是要么不用、要么用错。
            # 而 pi 桥那条路一直是原样透传的,**同一个产品两条路给模型看的东西
            # 完全不同**。2026-08-10 Seven 定稿:两条路统一为原样透传,告诉模型
            # 这是一个可以使用的 MCP 工具就够,不加不可信标注、不加长度上限。
            #
            # 注入面照旧由别处兜:工具**输出**仍按外部不可信内容处理(unified
            # loop),服务器是用户自己连的,名字空间 mcp__<server>__<tool> 也让
            # 模型知道调用来源。
            description = str(t.get("description") or "").strip() or (
                f'MCP tool "{raw}" from server "{name}"'
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

    return _allocate_round_robin(candidates, schema_rejected=schema_rejected)


def _allocate_round_robin(
    candidates: list[_CatalogCandidate],
    *,
    schema_rejected: list[str] | None = None,
) -> McpTurn:
    """Fill the turn's tool budget round-robin across servers, not in name order.

    The previous allocator sorted every candidate into one list keyed by server
    name and truncated at the caps. That starves whole servers by alphabet: with
    six connected servers offering 107 tools, ``mcdonalds`` and ``tavily`` landed
    past the cut and reached the model with ZERO tools each — the user sees them
    enabled and green in the app while the agent cannot see a single tool, and
    the model then invents a reason ("I don't have a search tool"). The pi bridge
    hit the identical bug against the identical user config and fixed it this way
    (``tools/pi_mcp_bridge/tool_mapping.js``); this is the same allocation for the
    hosted V2 catalog, so both runtimes now behave the same.

    Round-robin means every server lands at least one tool before any server
    lands its second, so a small server is never starved by a large one; only
    the largest get trimmed. Determinism is preserved exactly as before —
    servers sorted by name, tools sorted within a server, fixed rounds — so the
    provider-facing catalog bytes stay stable for a given input.

    BOTH caps are enforced in the same pass, and they are NOT symmetric:

    - The count cap ends allocation — nothing more can fit, by definition.
    - The char cap must NOT. It rejects the one candidate that would overflow
      and allocation continues, because a later candidate can still be small
      enough to fit. Ending the round-robin on the first oversized schema
      starves every server still holding candidates for a reason that has
      nothing to do with them (measured on the regression case: a server drops
      from 20 tools to 12).

    The cursor advances BEFORE the char check, so a repeatedly rejected
    candidate can never spin the loop.
    """
    by_server: dict[str, list[_CatalogCandidate]] = {}
    for item in candidates:
        by_server.setdefault(item.server, []).append(item)
    for items in by_server.values():
        items.sort(key=lambda c: (c.raw_name, c.spec.name))
    names = sorted(by_server)

    specs: list[ToolSpec] = []
    routes: dict[str, _Route] = {}
    catalog_chars = 0
    kept: dict[str, int] = {name: 0 for name in names}
    dropped_chars = 0
    cursor = {name: 0 for name in names}
    progressed = True
    while progressed and len(specs) < MAX_MCP_TOOLS_PER_TURN:
        progressed = False
        for server in names:
            if len(specs) >= MAX_MCP_TOOLS_PER_TURN:
                break
            index = cursor[server]
            items = by_server[server]
            if index >= len(items):
                continue
            cursor[server] = index + 1
            progressed = True
            item = items[index]
            if (
                catalog_chars + item.serialized_chars
                > MAX_MCP_TOOL_CATALOG_CHARS
            ):
                dropped_chars += 1
                continue
            specs.append(item.spec)
            routes[item.spec.name] = item.route
            catalog_chars += item.serialized_chars
            kept[server] += 1

    summary = {
        "kept": len(specs),
        "offered": len(candidates),
        "count_cap": MAX_MCP_TOOLS_PER_TURN,
        "char_cap": MAX_MCP_TOOL_CATALOG_CHARS,
        "char_cap_skips": dropped_chars,
        "catalog_chars": catalog_chars,
        # `服务器:注册数/发现数` —— 必须是**分配后**的注册数。只报发现数的话,
        # 一台服务器的工具全被裁掉时仍会显示它有 N 个,恰好把这条埋点要回答的
        # 那个问题答错(pi 那侧栽过一次,这里不重蹈)。
        "per_server": ",".join(
            f"{name}:{kept[name]}/{len(by_server[name])}" for name in names
        ),
        "servers": len(names),
        # 连候选都没进来的:schema 太大或结构非法,整个工具消失。
        # 和「进了候选但被上限裁掉」是两种不同的失踪,分开报才诊断得动。
        "schema_rejected": len(schema_rejected or []),
        "schema_rejected_names": ",".join((schema_rejected or [])[:10]),
    }

    # Never truncate silently: the count cap used to drop whole servers with no
    # log line at all, which is exactly why this took a user report to find.
    # Report kept/offered PER SERVER and post-allocation — a total alone cannot
    # answer "which server did the model lose", and reporting the offered count
    # would name a server whose tools were all dropped as if it were fine.
    if len(specs) < len(candidates):
        log.warning(
            "mcp catalog capped: kept=%d offered=%d count_cap=%d "
            "char_cap_skips=%d detail=%s",
            len(specs),
            len(candidates),
            MAX_MCP_TOOLS_PER_TURN,
            dropped_chars,
            ",".join(
                f"{name}:{kept[name]}/{len(by_server[name])}" for name in names
            ),
        )
    return McpTurn(specs, routes, summary)
