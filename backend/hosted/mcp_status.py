"""Content-free recent runtime health for user-configured MCP servers."""

from __future__ import annotations

import logging
import re
import time

import db

log = logging.getLogger("feedling.hosted.mcp_status")

RUNTIME_STATUS_BLOB = "user_mcp_runtime_status"
MAX_RECENT_TURNS = 10
_MAX_CAS_ATTEMPTS = 3
_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_KIND_RE = re.compile(r"^[a-z0-9_]{1,48}$")


def _clean_results(results) -> list[dict]:
    clean: dict[str, dict] = {}
    for raw in results or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "")
        kind = str(raw.get("kind") or "")
        if not _NAME_RE.fullmatch(name) or not _KIND_RE.fullmatch(kind):
            continue
        clean[name] = {"name": name, "kind": kind}
    return [clean[name] for name in sorted(clean)]


def _recent_rows(raw, *, keep: int = MAX_RECENT_TURNS) -> list[dict]:
    rows = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        try:
            at = float(item.get("at") or 0)
        except (TypeError, ValueError):
            continue
        if at > 0 and _KIND_RE.fullmatch(kind):
            rows.append({"at": at, "kind": kind})
    rows.sort(key=lambda item: item["at"])
    return rows[-max(1, keep):]


def record_runtime_results(store, results, *, now: float | None = None) -> bool:
    """Append one turn's bounded per-server outcomes using a CAS update.

    The document intentionally contains no URL, headers, exception text, remote
    response, or tool arguments. Servers absent from ``results`` are pruned:
    this call represents a successful read of the current enabled-server set.
    """
    clean = _clean_results(results)
    observed_at = round(float(time.time() if now is None else now), 3)
    user_id = str(getattr(store, "user_id", "") or "")
    if not user_id:
        return False

    for _attempt in range(_MAX_CAS_ATTEMPTS):
        raw = db.get_blob(user_id, RUNTIME_STATUS_BLOB)
        expected = raw if raw is not None else {}
        previous = raw if isinstance(raw, dict) else {}
        previous_servers = previous.get("servers")
        if not isinstance(previous_servers, dict):
            previous_servers = {}
        try:
            previous_updated_at = float(previous.get("updated_at") or 0)
        except (TypeError, ValueError):
            previous_updated_at = 0
        if not clean and not previous_servers:
            # The common case is a user with no MCP configuration. Do not add a
            # database write to every V2 chat turn merely to persist emptiness.
            return True

        servers = {}
        for result in clean:
            name = result["name"]
            kind = result["kind"]
            prior = previous_servers.get(name)
            recent = _recent_rows(
                prior.get("recent") if isinstance(prior, dict) else [],
                keep=MAX_RECENT_TURNS - 1,
            )
            recent.append({"at": observed_at, "kind": kind})
            recent.sort(key=lambda item: item["at"])
            recent = recent[-MAX_RECENT_TURNS:]
            latest = recent[-1]
            servers[name] = {
                "last_at": latest["at"],
                "last_kind": latest["kind"],
                "recent": recent,
            }

        doc = {
            "version": 1,
            "updated_at": max(observed_at, previous_updated_at),
            "servers": servers,
        }
        if db.set_blob_if_unchanged(
            user_id,
            RUNTIME_STATUS_BLOB,
            expected,
            doc,
            insert_if_missing=raw is None,
        ):
            return True

    log.warning("mcp runtime status CAS exhausted user=%s", user_id)
    return False


def runtime_status_for_store(store) -> dict:
    """Return the sanitized bounded document used by internal projections."""
    raw = db.get_blob(str(getattr(store, "user_id", "") or ""), RUNTIME_STATUS_BLOB)
    if not isinstance(raw, dict) or raw.get("version") != 1:
        return {"version": 1, "updated_at": 0, "servers": {}}
    raw_servers = raw.get("servers")
    if not isinstance(raw_servers, dict):
        raw_servers = {}
    servers = {}
    for raw_name, value in raw_servers.items():
        name = str(raw_name)
        if not _NAME_RE.fullmatch(name) or not isinstance(value, dict):
            continue
        recent = _recent_rows(value.get("recent"))
        if not recent:
            continue
        servers[name] = {
            "last_at": recent[-1]["at"],
            "last_kind": recent[-1]["kind"],
            "recent": recent,
        }
    try:
        updated_at = float(raw.get("updated_at") or 0)
    except (TypeError, ValueError):
        updated_at = 0
    return {
        "version": 1,
        "updated_at": updated_at,
        "servers": servers,
    }


def runtime_summaries_for_store(store) -> dict[str, dict]:
    """Project bounded internal history into the public per-server summary."""
    servers = runtime_status_for_store(store)["servers"]
    summaries = {}
    for name, status in servers.items():
        recent = status["recent"]
        if not recent:
            continue
        # NOT_OBSERVED rows are excluded from BOTH numerator and denominator.
        # They mean "this turn produced no evidence either way" (V1: the server
        # was still connecting at init and the model never needed it), which is
        # the ordinary case for a server the user simply did not use. Counting
        # them in the denominator would make a perfectly healthy server read as
        # mostly-failing in the app. No-op for V2, which never emits this kind.
        observed = [row for row in recent if row["kind"] != NOT_OBSERVED]
        if not observed:
            continue
        summaries[name] = {
            "last_kind": observed[-1]["kind"],
            "last_at": observed[-1]["at"],
            "recent_ok": sum(
                1 for row in observed if row["kind"] == "available"),
            "recent_total": len(observed),
        }
    return summaries


# --- V1 (resident_cli / self-hosted) ----------------------------------------
# V2 calls record_runtime_results directly from its own tool loop. V1's
# equivalent evidence arrives as the consumer's `mcp.surface.registered` debug
# trace, so it is projected here and injected from asgi_app (diagnostics must
# not import hosted — CONTRIBUTING §2 "需要向上调用时用注入").
#
# Before this, record_runtime_results had exactly ONE caller (v2/serve_worker),
# so every resident_cli user's `runtime` field in GET /v1/mcp/servers was
# permanently absent — 323 of 340 activated prod users on 2026-08-13.
NOT_OBSERVED = "not_observed"

_VERDICT_TO_KIND = {
    "ok": "available",
    # Startup-pending but a real call succeeded: available is the honest answer;
    # the user's question is "can my server be used", not "was it fast".
    "recovered": "available",
    "failed": "unavailable",
    # Deliberately its own kind, not "unavailable": "the model did not call it"
    # is not "the model could not call it" — the same distinction the consumer's
    # verdict ladder exists to preserve. Folding it into unavailable would show
    # a red dot for every server the user simply did not use this turn.
    "inconclusive": NOT_OBSERVED,
}


def record_from_registered_trace(store, detail) -> bool:
    """Project one V1 ``mcp.surface.registered`` detail into runtime status.

    Returns False (and writes nothing) when the payload carries no usable
    verdict map — an unparseable trace is not an observation, and inventing one
    is how an absent signal becomes a fake green.
    """
    if not isinstance(detail, dict):
        return False
    verdicts = detail.get("verdict")
    if not isinstance(verdicts, dict) or not verdicts:
        return False
    results = []
    for raw_name, raw_verdict in verdicts.items():
        kind = _VERDICT_TO_KIND.get(str(raw_verdict or "").strip().lower())
        if kind:
            results.append({"name": str(raw_name), "kind": kind})
    if not results:
        return False
    return record_runtime_results(store, results)
