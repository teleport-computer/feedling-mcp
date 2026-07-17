"""Pure materialization helpers for user-configured MCP servers.

The resident consumer (hosted CVM AND self-hosted VPS — same process) turns the
decrypted server list into on-disk agent config:
  - claude: an ``--mcp-config`` JSON file + ``settings.json`` permission rules
  - codex:  a marker-delimited ``[mcp_servers.*]`` block merged into
    ``config.toml`` WITHOUT disturbing the spawner-owned gateway section
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


def _enabled(servers: list[dict]) -> list[dict]:
    return sorted(
        (s for s in servers
         if s.get("enabled") and _SAFE_NAME.match(s.get("name") or "")),
        key=lambda s: s["name"])


def claude_mcp_json(servers: list[dict]) -> str:
    doc = {"mcpServers": {
        s["name"]: {"type": "http", "url": s["url"],
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
