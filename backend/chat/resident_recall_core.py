"""Per-turn card selection for plaintext resident accounts (T779 step 3, T788).

The resident consumer used to ask the enclave for "the cards picked for this
message". For accounts whose effective content encryption is ``off`` the backend
now answers instead, with the lexical ranker only (Seven 2026-09-30: no vectors
for residents for now), on exactly the page the consumer's enclave read used:
``before_seq = seq + 1``, ``limit = 4``, no ``context_recent``, trace on.

Effective-off operations never call the enclave. Rollout off/shadow returns
classified optional unavailability after required content validation; on uses
isolated local candidates. Unknown-stage, ownership and required-content
failures are hard failures. Only successfully resolved encrypted/legacy modes
return the compatibility delegation response.
"""
from __future__ import annotations

import os
import threading
import time

import debug_trace
from accounts import registry as accounts_registry
from chat import chat_core
from core import enclave as core_enclave
from core import runtime_token
from core import store as core_store
from memory import memory_core, plaintext_recall, recall_contract, recall_operation

MODE_ENV = "FEEDLING_RESIDENT_PLAINTEXT_RECALL"
MODES = ("off", "shadow", "on")
PAGE_LIMIT = 4                      # the consumer's AUTO_MEMORY_TURN_PAGE
MAX_DEADLINE_MS = 4000.0            # server-side cap on the whole selection
DEFAULT_DEADLINE_MS = 3000.0
_DEADLINE_MARGIN_S = 0.15
MAX_BODY_BYTES = 1024
_RUNTIME_TOKEN_SCOPE = ["envelope_decrypt"]

# Bounded work per backend worker process. A task that outlives its wait keeps
# its permit until it really ends (plaintext_recall.run_bounded), so slow reads
# cannot pile up behind a caller that already gave up.
_LOCAL_PERMIT = threading.BoundedSemaphore(2)
_ACCOUNT_PERMIT = threading.BoundedSemaphore(2)
_ENCLAVE_PERMIT = threading.BoundedSemaphore(4)
_SHADOW_PERMIT = threading.BoundedSemaphore(1)
_TRACE_PERMIT = threading.BoundedSemaphore(1)

# Fingerprint fields that only describe the hybrid (vector) policy. A lexical
# local pick is compared with an enclave pick whose hybrid step fell back to
# the lexical ranker by blanking exactly these, never the inputs that decide
# the selection (history window, candidate cards, flags).
_HYBRID_POLICY_FIELDS = ("hybrid", "card_vectors", "query_vectors", "min_cosine",
                         "model_id", "fresh")


def mode() -> str:
    value = os.environ.get(MODE_ENV, "off").strip().lower()
    return value if value in MODES else "off"


def deadline_seconds(header_value) -> float:
    """The caller's remaining budget, clamped to the server's own bounds."""
    try:
        ms = float(header_value)
    except (TypeError, ValueError):
        ms = DEFAULT_DEADLINE_MS
    if ms != ms:  # NaN
        ms = DEFAULT_DEADLINE_MS
    ms = min(MAX_DEADLINE_MS, max(0.0, ms))
    return max(0.0, ms / 1000.0 - _DEADLINE_MARGIN_S)


def parse_request(body) -> tuple[dict | None, str]:
    if not isinstance(body, dict) or set(body) - {"message_id", "seq", "include_references"}:
        return None, "invalid_body"
    message_id = body.get("message_id")
    seq = body.get("seq")
    if not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 128:
        return None, "invalid_message_id"
    if isinstance(seq, bool) or not isinstance(seq, int) or not 0 < seq < 2**63 - 1:
        return None, "invalid_seq"
    if "include_references" in body and not isinstance(body["include_references"], bool):
        return None, "invalid_body"
    parsed = {"message_id": message_id.strip(), "seq": seq}
    if "include_references" in body:
        parsed["include_references"] = body["include_references"]
    return parsed, ""


# ------------------------------------------------------------------ reads

def _store(user_id: str):
    return core_store.get_store_per_load_mode(
        user_id, reason="resident plaintext recall reads the page the enclave reads")


def _history_page(user_id: str, through_seq: int) -> list:
    from memory.recall_attachments import HistoryPage
    raw_page = []
    body, status = chat_core.history(
        _store(user_id),
        query={"limit": str(PAGE_LIMIT), "before_seq": str(int(through_seq) + 1),
               "include_image_body": "0"},
        user_agent="resident-plaintext-recall", remote_addr="", raw_page_observer=lambda rows: raw_page.extend(json.loads(json.dumps(rows))))
    if status != 200 or not isinstance(body, dict):
        raise plaintext_recall.NotServedHere("history_unavailable")
    rows = body.get("messages")
    if not isinstance(rows, list):
        raise recall_contract.RecallFailure("history_shape_invalid")
    return HistoryPage(rows, raw_page)


def _list_moments(user_id: str, limit: int) -> list:
    body, status = memory_core.list_moments(
        _store(user_id), limit_raw=str(limit), cursor="", since="", include_archived_raw=None)
    if status != 200 or not isinstance(body, dict):
        raise plaintext_recall.NotServedHere("memory_list_unavailable")
    return list(body.get("moments") or [])


def _no_vectors(user_id, model_id, ids):  # the lexical path never reads vectors
    raise AssertionError("resident_recall_is_lexical")


def deps(page_out: list | None = None) -> plaintext_recall.Deps:
    """``page_out`` receives the exact page the selection read, so the answer's
    page view is that page (no second read)."""
    def history_page(user_id, through_seq):
        rows = _history_page(user_id, through_seq)
        if page_out is not None:
            page_out[:] = rows
        return rows
    return plaintext_recall.Deps(
        effective_mode=accounts_registry.effective_content_encryption,
        history_page=history_page,
        list_moments=_list_moments,
        stored_vectors=_no_vectors,
        encoder=None,
    )


def page_view(rows) -> list:
    """Only what the consumer's page check reads: order, id, role, seq."""
    view = []
    for m in rows if isinstance(rows, list) else []:
        if isinstance(m, dict):
            view.append({"id": str(m.get("id") or m.get("message_id") or ""),
                         "role": str(m.get("role") or ""), "seq": m.get("seq")})
    return view


def local_select(user_id: str, seq: int, page_out: list | None = None) -> plaintext_recall.LocalResult:
    return plaintext_recall.select(
        user_id, seq, deps(page_out), query_args=plaintext_recall.RESIDENT_QUERY_ARGS,
        hybrid=False)


def message_in_page(rows, message_id: str) -> bool:
    return any(item["id"] == message_id for item in page_view(rows))


def _mint(user_id: str) -> str:
    secret = os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET", "").strip().encode("utf-8")
    if not secret:
        raise RuntimeError("runtime_token_secret_missing")
    return runtime_token.mint(secret, user_id=user_id, runtime_instance_id="resident-recall",
                              scope=_RUNTIME_TOKEN_SCOPE, ttl=300.0)


def enclave_select(user_id: str, seq: int, *, input_fp: bool = False) -> dict:
    """The consumer's own enclave read, made by the backend for this user."""
    params = {"before_seq": seq + 1, "limit": PAGE_LIMIT, "include_image_body": "false",
              "context_trace": "1"}
    if input_fp:
        params["context_input_fp"] = "1"
    payload, error = core_enclave._enclave_get_json_for_gate(
        "/v1/chat/history", None, params=params, runtime_token=_mint(user_id))
    if error or not isinstance(payload, dict):
        raise RuntimeError("enclave_read_failed")
    if payload.get("user_id") != user_id:
        raise RuntimeError("enclave_user_mismatch")
    return payload


# ------------------------------------------------------------------ comparison

def compare(local: plaintext_recall.LocalResult, diagnostics: dict | None) -> dict:
    """Shadow verdict for a lexical local pick against the enclave's pick.

    An enclave turn whose hybrid step was active is a policy difference
    (vectors), recorded as ``incomparable:hybrid_policy`` and never counted as
    agreement. When the enclave's hybrid step fell back (or was disabled), only
    the hybrid-policy fields are blanked; every other input must match for the
    turn to count as ``normalized``.
    """
    diag = diagnostics if isinstance(diagnostics, dict) else {}
    remote_fp = diag.get("input_fingerprint")
    if (diag.get("error") or not isinstance(remote_fp, dict) or not remote_fp
            or not isinstance(diag.get("summary"), dict) or not diag["summary"]):
        return {"verdict": "unmeasured", "reason": "enclave_diagnostics_missing"}

    def policy(fp):
        hybrid = fp.get("hybrid")
        if hybrid is None:
            return "lexical"
        if isinstance(hybrid, str) and hybrid.startswith("active:"):
            return "active"
        if isinstance(hybrid, str) and hybrid.startswith("fallback:"):
            return "lexical"
        return "unknown"

    if policy(remote_fp) == "active":
        return {"verdict": "incomparable", "reason": "hybrid_policy"}
    if policy(remote_fp) == "unknown":
        return {"verdict": "unmeasured", "reason": "hybrid_status_unknown"}

    # A normalized candidate was produced by the enclave from the SAME frozen
    # page/cards, removing sealed cards. Never replace it with a fresh DB read.
    candidates = [(diag, False)]
    normalized = diag.get("normalized")
    if isinstance(normalized, dict):
        candidates.append((normalized, True))
    blank = dict.fromkeys(_HYBRID_POLICY_FIELDS)
    for candidate, sealed_subset in candidates:
        fp, summary = candidate.get("input_fingerprint"), candidate.get("summary")
        if not isinstance(fp, dict) or not isinstance(summary, dict) or not summary:
            continue
        if policy(fp) != "lexical":
            continue
        if fp == local.input_fingerprint:
            basis = "sealed_cards_excluded" if sealed_subset else "exact"
        elif {**fp, **blank} == {**local.input_fingerprint, **blank}:
            basis = ("sealed_cards_excluded+hybrid_fallback_blanked" if sealed_subset
                     else "hybrid_fallback_blanked")
        else:
            continue
        result = {"verdict": "comparable" if basis == "exact" else "normalized",
                  "basis": basis, "same": summary == local.summary,
                  "diff": sorted(k for k in local.summary if summary.get(k) != local.summary[k])}
        if sealed_subset:
            result["full_same"] = diag["summary"] == local.summary
        return result
    return {"verdict": "incomparable",
            "reason": ",".join(sorted(k for k in set(local.input_fingerprint) | set(remote_fp)
                                      if k not in blank and remote_fp.get(k)
                                      != local.input_fingerprint.get(k)))[:120]}


# ------------------------------------------------------------------ one turn

def _response(source: str, payload: dict, rows) -> dict:
    return {
        "source": source,
        "messages": page_view(rows),
        "context_memories": payload.get("context_memories"),
        "context_memory_trace": payload.get("context_memory_trace"),
        "context_memory_log": payload.get("context_memory_log"),
    }


def _background(fn, permit, *, name: str) -> None:
    """No queue and no unbounded waiter threads; failures cannot change a turn.

    The permit belongs to the actual worker, including diagnostics I/O, until
    it exits. A stuck worker reduces capacity instead of spawning more work.
    """
    if not permit.acquire(blocking=False):
        return

    def run():
        try:
            fn()
        except Exception:  # noqa: BLE001 — optional evidence is best effort
            pass
        finally:
            permit.release()

    try:
        threading.Thread(target=run, name=name, daemon=True).start()
    except Exception:  # noqa: BLE001 — a failure to start owns no work
        permit.release()


def _trace(store, *, source: str, outcome: str, elapsed_ms: float, reason: str = "",
           detail: dict | None = None) -> None:
    current = mode()
    _background(
        lambda: debug_trace.trace_event(
            store, subsystem="memory", type="memory.resident_recall.served",
            summary="Resident per-turn card selection",
            detail={"source": source, "outcome": outcome, "reason": reason[:80],
                    "elapsed_ms": round(elapsed_ms, 1), "mode": current, **(detail or {})}),
        _TRACE_PERMIT, name="resident-recall-trace")


def _enclave_with_budget(user_id: str, seq: int, remaining: float, *, input_fp=False):
    if remaining <= 0:
        return "timeout", None
    return plaintext_recall.run_bounded(
        lambda: enclave_select(user_id, seq, input_fp=input_fp), remaining, _ENCLAVE_PERMIT)


def _submit_shadow(store, user_id: str, seq: int, diagnostics) -> None:
    def run():
        started = time.monotonic()
        try:
            value = local_select(user_id, seq)
            if time.monotonic() - started > plaintext_recall.shadow_timeout_seconds():
                detail = {"verdict": "unmeasured", "reason": "timeout"}
            else:
                detail = compare(value, diagnostics)
                detail.update({"local_ms": value.elapsed_ms, "sealed_cards": value.sealed_cards})
        except plaintext_recall.NotServedHere as exc:
            detail = {"verdict": "not_served", "reason": exc.reason}
        except Exception as exc:  # noqa: BLE001
            detail = {"verdict": "unmeasured", "reason": f"local_{type(exc).__name__}"[:60]}
        debug_trace.trace_event(
            store, subsystem="memory", type="memory.resident_recall.shadow",
            summary="Resident plaintext recall shadow comparison", detail=detail)

    _background(run, _SHADOW_PERMIT, name="resident-recall-shadow")


def select_for_turn(store, request: dict, deadline_s: float) -> tuple[dict, int]:
    """One resident turn: ``(body, status)`` under the contract in the module doc."""
    started = time.monotonic()
    user_id = store.user_id
    current = mode()
    end = started + max(0.0, deadline_s)
    account_status, effective = plaintext_recall.run_bounded(
        lambda: accounts_registry.effective_content_encryption(user_id),
        end - time.monotonic(), _ACCOUNT_PERMIT)
    if account_status != "ok":
        return {"error": "input_read_unconfirmed", "category": "content_read_failed"}, 503
    try:
        effective = recall_contract.exact_mode(effective)
        if effective == "off":
            payload, status = recall_operation.select_off(
                user_id, request["seq"], deps(), deadline=end, permit=_LOCAL_PERMIT,
                rollout=current, message_id=request["message_id"],
                include_references=request.get("include_references", True),
                query_args=plaintext_recall.RESIDENT_QUERY_ARGS, hybrid=False)
            _trace(store, source="local", outcome="ok" if status == 200 else "unavailable",
                   elapsed_ms=(time.monotonic() - started) * 1000,
                   reason=payload.get("detail", ""))
            return payload, status
    except recall_contract.RecallFailure as exc:
        return {"error": exc.reason, "category": exc.category}, (422 if exc.reason == "message_not_in_window" else 503)
    # Only successful on/legacy unknown may delegate to encrypted compatibility.
    return {"error": "not_served", "detail": "account_encrypted"}, 409
