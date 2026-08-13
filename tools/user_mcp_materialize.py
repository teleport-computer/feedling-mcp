"""Pure materialization helpers for user-configured MCP servers.

The independently operated resident consumer (for example a user's VPS) turns
the decrypted server list into on-disk agent config:
  - claude: an ``--mcp-config`` JSON file + ``settings.json`` permission rules
  - codex:  a marker-delimited ``[mcp_servers.*]`` block merged into
    ``config.toml`` WITHOUT disturbing unrelated runtime configuration
  - any other runtime: the claude-shaped JSON doubles as the generic
    ``user-mcp.json`` documented for VPS agents (io-onboarding skill).

Pure functions only — no I/O, no env — so the whole surface unit-tests without
importing the consumer.
"""

from __future__ import annotations

import json
import re

MARKER_BEGIN = "# --- feedling user_mcp (managed) — do not edit ---"
MARKER_END = "# --- end feedling user_mcp ---"

# Defense in depth: upstream mcp_core already validates server names against
# this same pattern before persisting, but this module is a standalone pure
# function surface and must not assume a well-formed caller. `name` is spliced
# directly into TOML table headers (`[mcp_servers.{name}]`) and Claude
# permission rules (`mcp__{name}__*`); a name that doesn't match here is
# dropped by `_enabled` rather than allowed to corrupt/escape the config.
_SAFE_NAME = re.compile(r"^[a-z0-9_-]{1,32}$")

# See hermes_config_merged for the full reasoning and the cost of this number.
HERMES_MCP_DISCOVERY_TIMEOUT_SEC = 10.0


def _enabled(servers: list[dict]) -> list[dict]:
    return sorted(
        (s for s in servers
         if s.get("enabled") and _SAFE_NAME.match(s.get("name") or "")),
        key=lambda s: s["name"])


def effective_transport(server: dict) -> str:
    """"http" (streamable HTTP) or "sse" (legacy HTTP+SSE) for one server.

    An explicit stored value wins (mcp_core stamps it at save time and the
    probe corrects it on detection). Envelopes written before the transport
    field existed fall back to the same URL-path heuristic mcp_core uses —
    providers that still run the legacy transport advertise it as an ``/sse``
    URL (Tencent/AMap docs, 2026-07-19).
    """
    t = str(server.get("transport") or "").strip().lower()
    if t in ("http", "sse"):
        return t
    path = (str(server.get("url") or "").split("?", 1)[0]).rstrip("/")
    return "sse" if path.lower().endswith("/sse") else "http"


def claude_mcp_json(servers: list[dict]) -> str:
    # claude natively speaks both transports; the type field selects it.
    # This file doubles as the generic VPS user-mcp.json, so the type field
    # is part of the documented shape.
    doc = {"mcpServers": {
        s["name"]: {"type": effective_transport(s), "url": s["url"],
                    "headers": dict(s.get("headers") or {})}
        for s in _enabled(servers)
    }}
    return json.dumps(doc, indent=2, ensure_ascii=False)


def claude_allow_rules(servers: list[dict]) -> list[str]:
    return [f"mcp__{s['name']}__*" for s in _enabled(servers)]


def merge_settings_allow(
    settings_text: str | None, rules: list[str], managed_names,
) -> str:
    """Merge this feature's ``mcp__<name>__*`` allow rules into settings.json
    without disturbing allow rules for MCP servers configured by some other
    means (e.g. the user's own ``.mcp.json``). Only rules whose server name is
    in ``managed_names`` are pruned before ``rules`` is appended; every other
    allow rule (including unrelated ``mcp__`` rules) passes through untouched.
    """
    try:
        settings = json.loads(settings_text) if settings_text else {}
    except json.JSONDecodeError:
        settings = {}
    if not isinstance(settings, dict):
        # Legal JSON but the wrong shape (e.g. "[]" / "null") — settings.setdefault
        # below would crash on a list/None/etc; fall back to an empty document.
        settings = {}
    perms = settings.setdefault("permissions", {})
    existing = perms.get("allow")
    # A string "allow" value would otherwise be iterated character-by-character
    # by the list comprehension below.
    existing = existing if isinstance(existing, list) else []
    # Prefix (not substring) match on "mcp__<name>__" so a managed name that is
    # itself a prefix of, or contains, another server's name can't cross-match
    # (e.g. managed "foo" must not prune "mcp__foobar__*").
    managed_prefixes = tuple(f"mcp__{n}__" for n in {str(x) for x in managed_names})
    allow = [
        r for r in existing
        if not (managed_prefixes and str(r).startswith(managed_prefixes))
    ]
    perms["allow"] = allow + list(rules)
    return json.dumps(settings, indent=2)


def _toml_str(value: str) -> str:
    # json string escaping is valid TOML basic-string escaping for our inputs,
    # with one gap: json.dumps escapes U+0000-U+001F but NOT U+007F (DEL),
    # while TOML basic-strings require it escaped. Patch that one codepoint;
    # U+0080 and above are legal unescaped in both JSON and TOML.
    return json.dumps(str(value), ensure_ascii=False).replace("\x7f", "\\u007f")


def _strip_managed_block(text: str) -> str:
    if MARKER_BEGIN not in text:
        return text
    head, _, rest = text.partition(MARKER_BEGIN)
    _, _, tail = rest.partition(MARKER_END)
    return head.rstrip("\n") + ("\n" if head.strip() else "") + tail.lstrip("\n")


def ca_bundle_pem(servers: list[dict]) -> str | None:
    """Concatenated PEM of every enabled server's user-supplied CA, or None.

    None (not "") is the "no CA at all" signal: the caller must then write no
    file and set no env var. Handing a runtime an empty/partial bundle is worse
    than handing it nothing — codex's SSL_CERT_FILE REPLACES the trust store.
    """
    blocks = [s["ca_pem"].strip() for s in _enabled(servers) if s.get("ca_pem")]
    if not blocks:
        return None
    return "\n".join(blocks) + "\n"


def codex_config_merged(existing_text: str | None, servers: list[dict]) -> str:
    base = _strip_managed_block(existing_text or "")
    enabled = _enabled(servers)
    if not enabled:
        return base
    lines = [MARKER_BEGIN]
    for s in enabled:
        if effective_transport(s) == "sse":
            # codex only speaks stdio + streamable HTTP (verified against
            # codex-cli 0.144.6: --url is documented as "streamable HTTP");
            # a legacy HTTP+SSE table entry would just burn its 20s startup
            # timeout every turn. Leave a comment instead of a broken table.
            lines.append(
                f"# server {_toml_str(s['name'])} uses the legacy HTTP+SSE "
                "transport — codex supports streamable HTTP only; "
                "use the provider's /mcp URL instead")
            lines.append("")
            continue
        lines.append(f"[mcp_servers.{s['name']}]")
        lines.append(f"url = {_toml_str(s['url'])}")
        headers = s.get("headers") or {}
        if headers:
            pairs = ", ".join(f"{_toml_str(k)} = {_toml_str(v)}"
                              for k, v in sorted(headers.items()))
            lines.append(f"http_headers = {{ {pairs} }}")
        lines.append("startup_timeout_sec = 20")
        lines.append("")
    lines.append(MARKER_END)
    block = "\n".join(lines)
    if base.strip():
        return base.rstrip("\n") + "\n\n" + block + "\n"
    return block + "\n"


def hermes_config_merged(existing_text: str | None, servers: list[dict],
                         managed_names) -> str:
    """Merge enabled servers into config.yaml's ``mcp_servers`` map, preserving
    every other top-level key and any mcp_servers entry the user added by hand.

    hermes reads ``mcp_servers`` as ONE top-level YAML map key (native-mcp.md),
    so — unlike codex's multi-table TOML — we cannot append a managed text
    block; a second ``mcp_servers:`` would be a duplicate key. We parse, splice
    the map, and re-dump. pyyaml drops comments and reflows formatting; the
    caller backs the file up first (``_materialize_hermes_config``).

    ``managed_names`` scopes the prune to server names this feature owns
    (current + previously-applied), so a server the user configured by hand in
    config.yaml is never deleted — same contract as the codex/claude targets.
    """
    import yaml  # noqa: PLC0415 — sibling dep via backend/requirements
    doc = yaml.safe_load(existing_text) if existing_text else None
    if not isinstance(doc, dict):
        doc = {}
    mcp = doc.get("mcp_servers")
    if not isinstance(mcp, dict):
        mcp = {}
    for name in list(mcp):
        if name in managed_names:
            del mcp[name]
    for s in _enabled(servers):
        entry: dict = {"url": s["url"]}
        headers = s.get("headers") or {}
        if headers:
            entry["headers"] = {str(k): str(v) for k, v in headers.items()}
        mcp[s["name"]] = entry
    if mcp:
        doc["mcp_servers"] = mcp
        # hermes's own default is 1.5s (hermes_cli/config.py DEFAULT_CONFIG,
        # read 2026-08-13 against 0.18.2) — TIGHTER than the ~2.5s claude
        # allows, and measured real public MCP servers need 1.3~2.5s for the
        # full 3-round-trip handshake from a cold process. So a server that the
        # app's own connection test passes routinely contributes zero tools to
        # the turn, and the user is told the capability does not exist.
        #
        # Unlike claude's MCP_TIMEOUT (measured: does nothing), this value is a
        # real bound: ``wait_for_mcp_discovery`` is a ``thread.join(timeout)``
        # that returns THE INSTANT discovery completes, so healthy servers pay
        # ~0s and only a still-connecting one costs anything.
        #
        # 10.0 matches mcp_probe._CONNECT_TIMEOUT — the same servers, the same
        # patience the control-plane probe already grants them. The cost of that
        # choice, stated plainly: a server that is genuinely unreachable makes
        # every turn wait the full 10s before starting. That is the price of a
        # hard guarantee here; the claude path deliberately refuses to pay it
        # (see the spec's §6) because it has no equivalent knob to bound.
        #
        # Written ONLY alongside enabled servers, and only when the user has not
        # chosen a value: a hand-set number is the operator's call, and a user
        # with no MCP configured must not have their startup touched at all.
        doc.setdefault("mcp_discovery_timeout", HERMES_MCP_DISCOVERY_TIMEOUT_SEC)
    elif "mcp_servers" in doc:
        del doc["mcp_servers"]
    return yaml.safe_dump(
        doc, sort_keys=False, allow_unicode=True, default_flow_style=False)


def openclaw_config_merged(existing_text: str | None, servers: list[dict],
                           managed_names) -> str:
    """Merge enabled servers into openclaw.json's nested ``mcp.servers`` map,
    preserving every other top-level key and any server the user added by hand.

    OpenClaw is a separate Node runtime (not hermes): its config is JSON with a
    NESTED ``mcp.servers`` key (verified against openclaw@2026.7.1-2), and each
    HTTP entry needs an explicit ``transport: "streamable-http"``. ``managed_names``
    scopes the prune to server names this feature owns, same contract as the
    hermes/codex/claude targets.
    """
    doc = json.loads(existing_text) if existing_text else {}
    if not isinstance(doc, dict):
        doc = {}
    mcp = doc.get("mcp")
    if not isinstance(mcp, dict):
        mcp = {}
    servers_map = mcp.get("servers")
    if not isinstance(servers_map, dict):
        servers_map = {}
    for name in list(servers_map):
        if name in managed_names:
            del servers_map[name]
    for s in _enabled(servers):
        entry: dict = {"url": s["url"], "transport": "streamable-http"}
        headers = s.get("headers") or {}
        if headers:
            entry["headers"] = {str(k): str(v) for k, v in headers.items()}
        servers_map[s["name"]] = entry
    if servers_map:
        mcp["servers"] = servers_map
        doc["mcp"] = mcp
    else:
        mcp.pop("servers", None)
        if mcp:
            doc["mcp"] = mcp
        elif "mcp" in doc:
            del doc["mcp"]
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
