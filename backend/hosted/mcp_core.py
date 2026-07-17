"""User-configured remote HTTP MCP servers (spec: 2026-07-08-user-mcp-servers-design).

Storage: one per-user blob (kind ``user_mcp``). Secrets (url+headers) live ONLY
inside a shared X25519 envelope (purpose label ``mcp_server_config``); plaintext
metadata is what the iOS list screen shows. ``fingerprint`` is advertised on
every ``/v1/chat/poll`` so the resident consumer knows when to re-materialize.
"""

from __future__ import annotations

import hashlib
import json
import re
import ssl
import uuid
from urllib.parse import urlparse

import db
from core import envelope as core_envelope
from core import util as core_util
from core import wake_bus
from core.store import UserStore

USER_MCP_BLOB = "user_mcp"
MAX_SERVERS = 10
MAX_HEADERS = 20
MAX_HEADERS_BYTES = 8192
MAX_CA_BYTES = 32768
_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
# Host header is forged by the client stack; MCP session headers are owned by it.
_FORBIDDEN_HEADERS = {"host"}


def _err(kind: str, detail: str = "") -> dict:
    return {"error": {"kind": kind, "detail": detail}}


def _load(store: UserStore) -> dict:
    data = db.get_blob(store.user_id, USER_MCP_BLOB)
    if not isinstance(data, dict):
        return {"fingerprint": "", "servers": []}
    data.setdefault("fingerprint", "")
    data.setdefault("servers", [])
    return data


def compute_fingerprint(servers: list[dict]) -> str:
    if not servers:
        return ""
    basis = [
        {"name": s["name"], "enabled": bool(s.get("enabled")),
         "envelope_id": (s.get("config_envelope") or {}).get("id", "")}
        for s in sorted(servers, key=lambda s: s["name"])
    ]
    return "sha256:" + hashlib.sha256(
        json.dumps(basis, sort_keys=True).encode()).hexdigest()


def _save(store: UserStore, servers: list[dict]) -> dict:
    data = {"fingerprint": compute_fingerprint(servers), "servers": servers}
    db.set_blob(store.user_id, USER_MCP_BLOB, data)
    # Wake any parked chat poller so an idle resident consumer picks up the new
    # fingerprint immediately instead of waiting out the long-poll timeout.
    # Same double-call as chat_core (local waiter + cross-worker wake_bus); safe
    # from the run_db threadpool because the registry hops back to the loop via
    # loop.call_soon_threadsafe.
    store.notify_chat_waiters()
    wake_bus.notify("chat", store.user_id)
    return data


def fingerprint_for_store(store: UserStore) -> str:
    return str(_load(store).get("fingerprint") or "")


def validate_url_syntax(url: str) -> str | None:
    # Malformed inputs (e.g. an unterminated IPv6 literal ``https://[::1``) can
    # raise ValueError either at ``urlparse`` time or on ``.scheme``/``.hostname``
    # access depending on the Python version — pull both out inside the guard so
    # any parse failure resolves to a clean ``invalid_url`` instead of a 500.
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme
        hostname = parsed.hostname
    except ValueError:
        return "invalid_url"
    # http is deliberately allowed: the agent — not the backend — makes the
    # actual MCP call, and v2 spec §6 already judged that path not to be a new
    # attack surface. Plaintext is the user's informed choice (§8).
    if scheme not in ("http", "https"):
        return "invalid_url"
    if not hostname:
        return "invalid_url"
    return None


def _validate_ca_pem(ca_pem: str) -> dict | None:
    """Hard-validate at write time: a bad bundle would break the user's WHOLE
    agent TLS (codex's SSL_CERT_FILE replaces the trust store, spec §5), not
    just this one server. Fail here, not inside the agent."""
    if len(ca_pem.encode("utf-8")) > MAX_CA_BYTES:
        return _err("ca_too_large", f"max {MAX_CA_BYTES} bytes")
    try:
        ssl.create_default_context().load_verify_locations(cadata=ca_pem)
    except (ssl.SSLError, ValueError, TypeError) as e:
        return _err("invalid_ca", str(e)[:160])
    return None


def _validate_payload(name: str, url: str, headers: dict, ca_pem: str = "") -> dict | None:
    if not _NAME_RE.match(name or ""):
        return _err("invalid_name", "name must match ^[a-z0-9_-]{1,32}$")
    kind = validate_url_syntax(url)
    if kind:
        # Do NOT re-parse ``url`` here for a detail string: it may be malformed
        # (that's why validation failed) and ``urlparse`` would raise again → 500.
        return _err(kind)
    if not isinstance(headers, dict):
        return _err("invalid_headers", "headers must be an object")
    if len(headers) > MAX_HEADERS:
        return _err("too_many_headers", f"max {MAX_HEADERS}")
    total = sum(len(str(k)) + len(str(v)) for k, v in headers.items())
    if total > MAX_HEADERS_BYTES:
        return _err("headers_too_large", f"max {MAX_HEADERS_BYTES} bytes")
    for k in headers:
        if str(k).strip().lower() in _FORBIDDEN_HEADERS:
            return _err("forbidden_header", str(k))
    if ca_pem:
        err = _validate_ca_pem(ca_pem)
        if err:
            return err
    return None


def _public(srv: dict) -> dict:
    out = {k: srv.get(k) for k in
           ("id", "name", "enabled", "url_hint", "header_names", "has_ca",
            "created_at", "updated_at")}
    # v2 (2026-07-08) records predate the "has_ca" field entirely — .get()
    # returns None for them, which violates the boolean type this endpoint
    # declares in the OpenAPI contract. Coerce, don't KeyError: missing means
    # "no CA was ever set", i.e. False.
    out["has_ca"] = bool(out["has_ca"])
    return out


def list_servers(store: UserStore) -> tuple[dict, int]:
    servers = _load(store)["servers"]
    return {"servers": [_public(s) for s in servers]}, 200


def upsert_server(store: UserStore, payload: dict) -> tuple[dict, int]:
    name = str(payload.get("name") or "").strip()
    url = str(payload.get("url") or "").strip()
    headers = payload.get("headers") or {}
    ca_pem = str(payload.get("ca_pem") or "").strip()
    err = _validate_payload(name, url, headers, ca_pem)
    if err:
        return err, 400
    servers = _load(store)["servers"]
    existing = next((s for s in servers if s["name"] == name), None)
    if existing is None and len(servers) >= MAX_SERVERS:
        return _err("too_many_servers", f"max {MAX_SERVERS}"), 400
    secret_doc = {"url": url,
                  "headers": {str(k): str(v) for k, v in headers.items()}}
    if ca_pem:
        secret_doc["ca_pem"] = ca_pem
    secret = json.dumps(secret_doc)
    envelope, enc_err = core_envelope._build_shared_envelope_for_store(
        store, secret.encode("utf-8"), item_id=f"user_mcp_{uuid.uuid4().hex}")
    if envelope is None:
        return _err("cannot_encrypt", str(enc_err or "")), 409
    now = core_util._now_iso()
    record = {
        "id": existing["id"] if existing else f"srv_{uuid.uuid4().hex[:8]}",
        "name": name,
        "enabled": bool(payload.get("enabled", True)),
        "config_envelope": envelope,
        "url_hint": urlparse(url).hostname or "",
        "header_names": sorted(str(k) for k in headers),
        "has_ca": bool(ca_pem),
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
    }
    servers = [s for s in servers if s["name"] != name] + [record]
    _save(store, servers)
    return _public(record), 200


def set_enabled(store: UserStore, name: str, payload: dict) -> tuple[dict, int]:
    servers = _load(store)["servers"]
    srv = next((s for s in servers if s["name"] == name), None)
    if srv is None:
        return _err("not_found", name), 404
    srv["enabled"] = bool(payload.get("enabled"))
    srv["updated_at"] = core_util._now_iso()
    _save(store, servers)
    return _public(srv), 200


def delete_server(store: UserStore, name: str) -> tuple[dict, int]:
    servers = _load(store)["servers"]
    if not any(s["name"] == name for s in servers):
        return _err("not_found", name), 404
    _save(store, [s for s in servers if s["name"] != name])
    return {"deleted": name}, 200


def test_server(store: UserStore, name: str, caller_api_key: str | None) -> tuple[dict, int]:
    from core import enclave as core_enclave
    from hosted import mcp_probe
    servers = _load(store)["servers"]
    srv = next((s for s in servers if s["name"] == name), None)
    if srv is None:
        return _err("not_found", name), 404
    try:
        secret = json.loads(core_enclave._decrypt_envelope_via_enclave(
            srv["config_envelope"], caller_api_key,
            purpose="mcp_server_config").decode("utf-8"))
    except Exception as e:
        return _err("decrypt_failed", str(e)[:160]), 400
    try:
        out = mcp_probe.probe(secret["url"], secret.get("headers") or {},
                              ca_pem=secret.get("ca_pem"))
    except mcp_probe.ProbeError as e:
        return _err(e.kind, e.detail), 400
    return out, 200


def envelopes_payload(store: UserStore) -> tuple[dict, int]:
    data = _load(store)
    return {
        "fingerprint": data["fingerprint"],
        "servers": [
            {"name": s["name"], "enabled": bool(s.get("enabled")),
             "config_envelope": s["config_envelope"]}
            for s in data["servers"]
        ],
    }, 200
