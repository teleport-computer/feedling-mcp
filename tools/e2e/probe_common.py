"""Shared scaffolding for the pre Runtime-V2 deep functional probes.

Result taxonomy is adopted from sxysun qa/ SOP §5 (see
docs/testing/RELEASE_TESTING_PROTOCOL.md §8): ordered severity, NO `SKIP`
(missing coverage / key / deployment is a `BLOCKED_*`), a green retry never
rescues a `PRODUCT_FAIL`, and missing evidence is `BLOCKED_EVIDENCE`, never a
silent pass.

Each probe exports ``run_<area>_probe(c, cfg) -> {"area", "cases": [...]}`` where
a case is ``{"name", "result", "detail"}`` and result is one of the constants
below. ``cfg`` is a plain dict:
    {"cell", "provider", "model", "key", "base_url", "run_invariants": bool}
``run_invariants`` is True on exactly one provider so provider-independent
backend invariants run once (deep.py owns that flag).
"""
from __future__ import annotations

import uuid

# -- result taxonomy (ordered most→least severe) ----------------------------
SECURITY_FAIL = "SECURITY_FAIL"
BLOCKED_CREDENTIAL = "BLOCKED_CREDENTIAL"
BLOCKED_DEPLOYMENT = "BLOCKED_DEPLOYMENT"
BLOCKED_EVIDENCE = "BLOCKED_EVIDENCE"
PRODUCT_FAIL = "PRODUCT_FAIL"
AGENT_ERROR = "AGENT_ERROR"
PASS = "PASS"

SEVERITY = [
    SECURITY_FAIL, BLOCKED_CREDENTIAL, BLOCKED_DEPLOYMENT,
    BLOCKED_EVIDENCE, PRODUCT_FAIL, AGENT_ERROR, PASS,
]
RESULTS = set(SEVERITY)
# A run is release-blocked when any case lands on one of these. Per qa SOP §5,
# a broken harness (AGENT_ERROR) and a missing key/deployment (BLOCKED_CREDENTIAL/
# _DEPLOYMENT) are NOT green — only BLOCKED_EVIDENCE (an honest "cannot observe on
# this surface") is release-tolerable but still surfaced in the report.
BLOCKING = {SECURITY_FAIL, BLOCKED_CREDENTIAL, BLOCKED_DEPLOYMENT, PRODUCT_FAIL, AGENT_ERROR}


def worst(results: list[str]) -> str:
    """Highest-severity status in a set (empty → PASS)."""
    for status in SEVERITY:
        if status in results:
            return status
    return PASS


class Probe:
    """Accumulates cases for one area. Never raises out of a case: an unexpected
    exception is recorded as AGENT_ERROR so one broken probe never aborts the run."""

    def __init__(self, area: str):
        self.area = area
        self.cases: list[dict] = []

    def add(self, name: str, result: str, detail: str = "") -> str:
        if result not in RESULTS:
            # A typo'd result would be absent from SEVERITY/BLOCKING and could let
            # worst() collapse to PASS — treat an unknown status as a harness bug.
            detail = f"[invalid result {result!r}] {detail}"
            result = AGENT_ERROR
        self.cases.append({"name": name, "result": result, "detail": str(detail)[:400]})
        return result

    def ok(self, name: str, cond: bool, detail: str = "", *, fail: str = PRODUCT_FAIL) -> str:
        """PASS when cond is truthy, else `fail` (default PRODUCT_FAIL)."""
        return self.add(name, PASS if cond else fail, detail)

    def blocked(self, name: str, why: str, kind: str = BLOCKED_EVIDENCE) -> str:
        return self.add(name, kind, why)

    def guard(self, name: str, fn):
        """Run fn() (which returns (result, detail) or raises); record it. An
        exception becomes AGENT_ERROR — the probe itself misbehaved, distinct
        from a product failure."""
        try:
            result, detail = fn()
            return self.add(name, result, detail)
        except Exception as e:  # noqa: BLE001
            return self.add(name, AGENT_ERROR, f"{type(e).__name__}: {e}")

    def result(self) -> dict:
        return {"area": self.area, "cases": self.cases}


# -- memory action helpers (plaintext write path; server encrypts) ----------
# The write path enforces a source allowlist (memory/actions.py). "e2e_probe"
# was never on it, so every probe write started failing 400 source_invalid once
# the allowlist landed — the whole deep memory area went 7/8 red for a reason
# that had nothing to do with the product (observed 2026-08-06). Probes must
# write with a real product source; widening the allowlist for tests would
# weaken the very validation it guards.
_PROBE_SOURCE = "memory_capture"


def mem_add(c, *, summary: str, content: str = "", mem_type: str = "fact",
            bucket: str = "", threads=None, importance=None, pulse=None,
            source: str = _PROBE_SOURCE, occurred_at: str = "",
            visibility: str = "") -> tuple[int, dict]:
    memory = {
        "type": mem_type, "summary": summary, "title": summary,
        "content": content or summary, "description": content or summary,
        "source": source,
    }
    if bucket:
        memory["bucket"] = bucket
    if threads:
        memory["threads"] = list(threads)
    if importance is not None:
        memory["importance"] = float(importance)
    if pulse is not None:
        memory["pulse"] = float(pulse)
    if occurred_at:
        memory["occurred_at"] = occurred_at
    if visibility:
        memory["visibility"] = visibility
    r = c.post("/v1/memory/actions", json={"actions": [
        {"type": "memory.add", "memory": memory, "reason": "e2e probe write"}]})
    body = {}
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {"_text": r.text[:200]}
    return r.status_code, body


def mem_supersede(c, supersedes: str, *, summary: str, content: str = "",
                  bucket: str = "", threads=None) -> tuple[int, dict]:
    memory = {"type": "fact", "summary": summary, "title": summary,
              "content": content or summary, "description": content or summary,
              "source": _PROBE_SOURCE}
    if bucket:
        memory["bucket"] = bucket
    if threads:
        memory["threads"] = list(threads)
    r = c.post("/v1/memory/actions", json={"actions": [
        {"type": "memory.supersede", "supersedes": supersedes, "memory": memory,
         "reason": "e2e probe supersede"}]})
    try:
        return r.status_code, r.json()
    except Exception:  # noqa: BLE001
        return r.status_code, {"_text": r.text[:200]}


def mem_index(c, *, limit: int = 100, bucket: str = "", query: str = "") -> list[dict]:
    payload: dict = {"limit": limit}
    if bucket:
        payload["bucket"] = bucket
    if query:
        payload["query"] = query
    r = c.post("/v1/memory/index", json=payload)
    r.raise_for_status()
    return [it for it in (r.json().get("items") or []) if isinstance(it, dict)]


def mem_fetch(c, ids: list[str], *, limit: int = 20) -> list[dict]:
    r = c.post("/v1/memory/fetch", json={"ids": list(ids), "limit": limit})
    r.raise_for_status()
    return [it for it in (r.json().get("items") or []) if isinstance(it, dict)]


def find_card(items: list[dict], needle: str) -> dict | None:
    for it in items:
        if needle in str(it.get("summary") or "") or needle in str(it.get("content") or ""):
            return it
    return None


def new_marker() -> str:
    """A short unique token to tag a card/message so we can find exactly it."""
    return uuid.uuid4().hex[:8]


def install_identity(c, identity: dict) -> tuple[int, dict]:
    """Install an identity card on a fresh account. Handles the V2
    days_with_user_mismatch guard (retry once with the server-computed value) that
    codex2's proactive probe found. Returns (status, body) of the final attempt."""
    payload = {"identity": identity, "days_with_user": 0,
               "relationship_anchor_evidence": "deep-e2e fresh synthetic account"}
    r = c.post("/v1/identity/init", json=payload)
    if r.status_code == 400:
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            body = {}
        if body.get("error") == "days_with_user_mismatch" and isinstance(
                body.get("computed_from_earliest_memory"), int):
            payload["days_with_user"] = body["computed_from_earliest_memory"]
            payload["relationship_anchor_evidence"] = (
                "server-confirmed earliest memory " + str(body.get("earliest_memory_date") or ""))
            r = c.post("/v1/identity/init", json=payload)
    try:
        return r.status_code, r.json()
    except Exception:  # noqa: BLE001
        return r.status_code, {"_text": r.text[:200]}
