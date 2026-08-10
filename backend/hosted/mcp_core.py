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
from hosted.mcp_approvals import (
    MAX_READ_ONLY_TOOL_APPROVALS,
    valid_fingerprint,
    valid_tool_name,
)

USER_MCP_BLOB = "user_mcp"
# ⚠️ 这是**防滥用的兜底**,不是产品限制 —— 别拿它当成本闸。
#
# 2026-07-10 首版随手定的 10,没有任何依据被记下来。而它挡的"服务器数量"和
# 真实成本几乎无关:一台服务器可以出 100 个工具,十台也可能只有 15 个。
# 真正决定死活的三样都在别处已经有闸:
#   - 每轮到模型的工具数/字节数(mcp_tools 的 MAX_MCP_TOOLS_PER_TURN / 目录上限)
#   - 握手延迟(并行 + 每个各自超时 + V2 的 MCP_TURN_WALL_BUDGET_SEC)
#   - 单台的 header/CA 体积(下面几个常量)
# 10 唯一的效果,是让想接第 11 台的用户撞一堵没道理的墙(2026-08-10 Seven 指出)。
#
# 留一道是因为「一个账号指 500 台」确实是真实滥用面:每轮就是 500 个并发出站
# 连接。所以这个数只要**明显不碍正常使用**即可,不需要精确。
MAX_SERVERS = 30
MAX_HEADERS = 20
MAX_HEADERS_BYTES = 8192
MAX_CA_BYTES = 32768
_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
# Host header is forged by the client stack; MCP session headers are owned by it.
_FORBIDDEN_HEADERS = {"host"}
_UPSERT_FIELDS = frozenset({
    "name",
    "url",
    "headers",
    "enabled",
    "ca_pem",
    "read_only_tool_fingerprints",
})
_PATCH_FIELDS = frozenset({"enabled", "read_only_tool_fingerprints"})


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
    # http is deliberately allowed for saved configs and the explicit public
    # probe. Plaintext transport is the user's informed choice (spec §8).
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


def _validate_read_only_approvals(value) -> dict | None:
    if not isinstance(value, dict):
        return _err(
            "invalid_read_only_tool_fingerprints", "must be an object")
    if len(value) > MAX_READ_ONLY_TOOL_APPROVALS:
        return _err(
            "too_many_read_only_tool_fingerprints",
            f"max {MAX_READ_ONLY_TOOL_APPROVALS}",
        )
    for tool_name, fingerprint in value.items():
        if not valid_tool_name(tool_name):
            return _err("invalid_read_only_tool_name")
        if not valid_fingerprint(fingerprint):
            return _err("invalid_read_only_tool_fingerprint")
    return None


def _validate_upsert_request(payload) -> dict | None:
    if not isinstance(payload, dict):
        return _err("invalid_request", "body must be an object")
    unknown = sorted(set(payload) - _UPSERT_FIELDS)
    if unknown:
        return _err("invalid_request", f"unknown fields: {', '.join(unknown)}")
    if not isinstance(payload.get("name"), str):
        return _err("invalid_name", "name must be a string")
    if not isinstance(payload.get("url"), str):
        return _err("invalid_url", "url must be a string")
    if "headers" in payload:
        headers = payload["headers"]
        if not isinstance(headers, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in headers.items()
        ):
            return _err(
                "invalid_headers", "header names and values must be strings")
    if "enabled" in payload and not isinstance(payload["enabled"], bool):
        return _err("invalid_enabled", "enabled must be a boolean")
    if "ca_pem" in payload and not isinstance(payload["ca_pem"], str):
        return _err("invalid_ca", "ca_pem must be a string")
    return None


def _validate_patch_request(payload) -> dict | None:
    if not isinstance(payload, dict):
        return _err("invalid_patch", "body must be an object")
    unknown = sorted(set(payload) - _PATCH_FIELDS)
    if unknown:
        return _err("invalid_patch", f"unknown fields: {', '.join(unknown)}")
    if not payload:
        return _err(
            "invalid_patch",
            "provide enabled and/or read_only_tool_fingerprints",
        )
    if "enabled" in payload and not isinstance(payload["enabled"], bool):
        return _err("invalid_enabled", "enabled must be a boolean")
    return None


def _transport_hint(url: str) -> str:
    """Save-time transport guess: providers that still run the legacy
    HTTP+SSE transport advertise it as an ``…/sse`` URL (Tencent/AMap map
    docs, 2026-07-19). The probe corrects a wrong guess on detection
    (``test_server`` persists the detected value); the materializers apply
    the same fallback for pre-transport envelopes
    (user_mcp_materialize.effective_transport)."""
    try:
        path = (urlparse(url).path or "").rstrip("/")
    except ValueError:
        return "http"
    return "sse" if path.lower().endswith("/sse") else "http"


def _validate_payload(
    name: str,
    url: str,
    headers: dict,
    ca_pem: str,
    read_only_tool_fingerprints,
) -> dict | None:
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
    approvals_error = _validate_read_only_approvals(
        read_only_tool_fingerprints)
    if approvals_error:
        return approvals_error
    return None


def _public(srv: dict) -> dict:
    out = {k: srv.get(k) for k in
           ("id", "name", "enabled", "url_hint", "header_names", "has_ca",
            "transport", "created_at", "updated_at")}
    # v2 (2026-07-08) records predate the "has_ca" field entirely — .get()
    # returns None for them, which violates the boolean type this endpoint
    # declares in the OpenAPI contract. Coerce, don't KeyError: missing means
    # "no CA was ever set", i.e. False.
    out["has_ca"] = bool(out["has_ca"])
    # Pre-transport records: the URL lives only inside the envelope, so the
    # list endpoint can't run the path heuristic — report the default. The
    # probe stamps the real value on the record at first successful test.
    out["transport"] = str(out["transport"] or "http")
    return out


def list_servers(store: UserStore) -> tuple[dict, int]:
    servers = _load(store)["servers"]
    return {"servers": [_public(s) for s in servers]}, 200


def upsert_server(store: UserStore, payload: dict) -> tuple[dict, int]:
    request_error = _validate_upsert_request(payload)
    if request_error:
        return request_error, 400
    name = payload["name"]
    url = payload["url"].strip()
    headers = payload.get("headers", {})
    ca_pem = payload.get("ca_pem", "").strip()
    read_only_tool_fingerprints = payload.get(
        "read_only_tool_fingerprints", {})
    err = _validate_payload(
        name,
        url,
        headers,
        ca_pem,
        read_only_tool_fingerprints,
    )
    if err:
        return err, 400
    servers = _load(store)["servers"]
    existing = next((s for s in servers if s["name"] == name), None)
    if existing is None and len(servers) >= MAX_SERVERS:
        return _err("too_many_servers", f"max {MAX_SERVERS}"), 400
    transport = _transport_hint(url)
    secret_doc = {"url": url,
                  "headers": {str(k): str(v) for k, v in headers.items()},
                  "transport": transport}
    if ca_pem:
        secret_doc["ca_pem"] = ca_pem
    if read_only_tool_fingerprints:
        secret_doc["read_only_tool_fingerprints"] = {
            str(tool_name): str(fingerprint)
            for tool_name, fingerprint in read_only_tool_fingerprints.items()
        }
    secret = json.dumps(secret_doc)
    envelope, enc_err = core_envelope._build_shared_envelope_for_store(
        store, secret.encode("utf-8"), item_id=f"user_mcp_{uuid.uuid4().hex}")
    if envelope is None:
        return _err("cannot_encrypt", str(enc_err or "")), 409
    now = core_util._now_iso()
    record = {
        "id": existing["id"] if existing else f"srv_{uuid.uuid4().hex[:8]}",
        "name": name,
        "enabled": payload.get("enabled", True),
        "config_envelope": envelope,
        "url_hint": urlparse(url).hostname or "",
        "header_names": sorted(str(k) for k in headers),
        "has_ca": bool(ca_pem),
        "transport": transport,
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
    }
    servers = [s for s in servers if s["name"] != name] + [record]
    _save(store, servers)
    return _public(record), 200


def set_enabled(
    store: UserStore,
    name: str,
    payload: dict,
    caller_api_key: str | None = None,
) -> tuple[dict, int]:
    """Patch public state and/or encrypted read-only approvals in place.

    ``GET /v1/mcp/servers`` intentionally never returns URL/header/CA secrets.
    Requiring clients to repeat those values merely to approve fingerprints
    discovered by ``POST .../test`` would make existing saved servers impossible
    to upgrade after the original form is gone.  The API-key-only PATCH therefore
    decrypts the existing envelope, replaces only the approval map, and seals a
    fresh envelope while preserving every connection secret.
    """
    servers = _load(store)["servers"]
    srv = next((s for s in servers if s["name"] == name), None)
    if srv is None:
        return _err("not_found", name), 404

    request_error = _validate_patch_request(payload)
    if request_error:
        return request_error, 400
    patch_enabled = "enabled" in payload
    patch_approvals = "read_only_tool_fingerprints" in payload

    replacement_envelope = None
    if patch_approvals:
        approvals = payload.get("read_only_tool_fingerprints")
        approvals_error = _validate_read_only_approvals(approvals)
        if approvals_error:
            return approvals_error, 400
        try:
            plaintext = core_envelope.read_envelope_body(
                srv["config_envelope"],
                caller_api_key,
                purpose="mcp_server_config",
            )
            secret_doc = json.loads(plaintext.decode("utf-8"))
            if not isinstance(secret_doc, dict) or not secret_doc.get("url"):
                raise ValueError("invalid MCP config")
        except Exception:  # noqa: BLE001 — never expose config/decrypt details
            return _err("decrypt_failed"), 400

        secret_doc = dict(secret_doc)
        if approvals:
            secret_doc["read_only_tool_fingerprints"] = {
                str(tool_name): str(fingerprint)
                for tool_name, fingerprint in approvals.items()
            }
        else:
            # An empty map is an explicit revocation of every parallel-read
            # approval. Omit it from ciphertext rather than storing dead state.
            secret_doc.pop("read_only_tool_fingerprints", None)
        replacement_envelope, enc_err = (
            core_envelope._build_shared_envelope_for_store(
                store,
                json.dumps(secret_doc).encode("utf-8"),
                item_id=f"user_mcp_{uuid.uuid4().hex}",
            )
        )
        if replacement_envelope is None:
            return _err("cannot_encrypt", str(enc_err or "")), 409

    # Mutate the record only after every fallible decrypt/re-encrypt step has
    # succeeded, so a failed approval patch cannot incidentally toggle enabled.
    if patch_enabled:
        srv["enabled"] = payload["enabled"]
    if replacement_envelope is not None:
        srv["config_envelope"] = replacement_envelope
    srv["updated_at"] = core_util._now_iso()
    _save(store, servers)
    return _public(srv), 200


def delete_server(store: UserStore, name: str) -> tuple[dict, int]:
    servers = _load(store)["servers"]
    if not any(s["name"] == name for s in servers):
        return _err("not_found", name), 404
    _save(store, [s for s in servers if s["name"] != name])
    return {"deleted": name}, 200


def _user_driver(store: UserStore, caller_api_key: str | None) -> str:
    """This user's agent driver, or '' when it can't be determined (VPS /
    unconfigured — then no codex-specific warning is emitted)."""
    try:
        from hosted import agent_runtime_cutover
        from hosted import config_store as hosted_config_store
        cfg = hosted_config_store._load_runtime_provider_config(store, caller_api_key)
        return agent_runtime_cutover.driver_for_provider(str((cfg or {}).get("provider") or ""))
    except Exception:  # noqa: BLE001 — driver unknown ⇒ no warning, never 500
        return ""


def test_server(store: UserStore, name: str, caller_api_key: str | None) -> tuple[dict, int]:
    from hosted import mcp_probe
    # Snapshot the WHOLE blob up front — this is the CAS expectation. The probe
    # below can take tens of seconds; any upsert/delete/toggle that lands in
    # that window must NOT be rolled back by our persistence (codex3 P0). We
    # never mutate `original`; the persist step is a conditional swap on it.
    original = _load(store)
    servers = original["servers"]
    srv = next((s for s in servers if s["name"] == name), None)
    if srv is None:
        return _err("not_found", name), 404
    try:
        secret = json.loads(core_envelope.read_envelope_body(
            srv["config_envelope"], caller_api_key,
            purpose="mcp_server_config").decode("utf-8"))
        if (
            not isinstance(secret, dict)
            or not isinstance(secret.get("url"), str)
            or not secret["url"]
            or validate_url_syntax(secret["url"]) is not None
        ):
            raise ValueError("invalid MCP config")
        headers = secret.get("headers", {})
        ca_pem = secret.get("ca_pem")
        if (
            not isinstance(headers, dict)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in headers.items()
            )
            or (ca_pem is not None and not isinstance(ca_pem, str))
        ):
            raise ValueError("invalid MCP config")
    except Exception:  # noqa: BLE001 — never expose config/decrypt details
        return _err("decrypt_failed"), 400
    driver = _user_driver(store, caller_api_key)
    hint = str(secret.get("transport") or srv.get("transport")
               or _transport_hint(secret["url"]))
    try:
        out = mcp_probe.probe(secret["url"], headers,
                              ca_pem=ca_pem, transport_hint=hint)
    except mcp_probe.ProbeError as e:
        # codex(rustls) 无法用单张自签名证书；此时 "agent 会自己处理" 的 tls 文案是错的。
        if (e.kind == "tls" and driver == "codex"
                and mcp_probe.leaf_is_ca(secret["url"]) is True):
            return _err("codex_cert_chain_required",
                        "single self-signed cert; codex needs a CA+leaf chain"), 400
        return _err(e.kind, e.detail), 400

    detected = str(out.get("transport") or "")
    updated_srv = None
    if detected in ("http", "sse") and detected != hint:
        # The hint was wrong (e.g. a legacy server on a non-/sse path, or an
        # old pre-transport record) — persist what actually worked so the
        # materialized agent config speaks the right protocol. Re-sealing bumps
        # the envelope id → fingerprint moves → the consumer re-materializes.
        # Best-effort: a failed re-seal must not fail a probe that succeeded.
        new_secret = json.dumps({**secret, "transport": detected})
        envelope, _enc_err = core_envelope._build_shared_envelope_for_store(
            store, new_secret.encode("utf-8"),
            item_id=f"user_mcp_{uuid.uuid4().hex}")
        if envelope is not None:
            updated_srv = {**srv, "config_envelope": envelope,
                           "transport": detected, "updated_at": core_util._now_iso()}
    elif detected in ("http", "sse") and not srv.get("transport"):
        # Hint matched but the record predates the field — stamp it (metadata
        # only, envelope untouched, fingerprint therefore unchanged).
        updated_srv = {**srv, "transport": detected}

    if updated_srv is not None:
        # Build the new blob from the SNAPSHOT (not a re-load) and CAS on the
        # snapshot, so a concurrent write between _load above and here is not
        # clobbered: if `original` moved, the swap no-ops and we keep the probe
        # result. `srv is s` identity holds — both come from `original`.
        new_servers = [updated_srv if s is srv else s for s in servers]
        new_doc = {"fingerprint": compute_fingerprint(new_servers),
                   "servers": new_servers}
        if db.set_blob_if_unchanged(store.user_id, USER_MCP_BLOB, original, new_doc):
            # Only the re-seal path moves the fingerprint (transport is not in
            # the fingerprint basis); wake the consumer only when it actually
            # changed, mirroring _save's intent without a spurious wake.
            if new_doc["fingerprint"] != original.get("fingerprint"):
                store.notify_chat_waiters()
                wake_bus.notify("chat", store.user_id)
    return out, 200


def envelopes_payload(store: UserStore) -> tuple[dict, int]:
    data = _load(store)
    return {
        "fingerprint": data["fingerprint"],
        "servers": [
            {"name": s["name"], "enabled": bool(s.get("enabled")),
             # convenience mirror of the encrypted secret's transport; ""
             # for pre-transport records (consumer falls back to the URL
             # heuristic after decrypting)
             "transport": str(s.get("transport") or ""),
             "config_envelope": s["config_envelope"]}
            for s in data["servers"]
        ],
    }, 200
