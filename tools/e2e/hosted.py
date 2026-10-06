"""Hosted (model_api) P0 cell: provision → setup → chat → continuity →
memory(warn) → bubbles(sanity) → teardown. One call per provider-key class.

No unlock step: hosted accounts are gate-exempt by design (see unlock.py
docstring). Send rides the canonical Runtime V2 entry
POST /v1/model_api/chat/send (202 + async reply via history). A transient
``workers_unavailable`` or ``runtime_policy_not_ready`` response is retried
with the same client_msg_id; admission fails before the message is committed.

Pass criteria mirror docs/testing/RELEASE_TESTING_PROTOCOL.md §3. Memory is a
WARN (capture is async by design); everything else is pass/fail.
"""
from __future__ import annotations

import time
import uuid

from .client import (
    E2EClient, VERDICT_FALLBACK, VERDICT_OK,
    decrypt_verdict as _decrypt_verdict,
)
from .config import HostedCell

# ``.client`` put the repo's ``backend`` on sys.path at import time, so the
# canonical BYOK model-catalog fetcher is importable here. Reusing it (rather
# than hand-rolling an HTTP GET) inherits its contract: OpenRouter's key-scoped
# ``/models/user`` route, provider default base URLs, real TLS verification, and
# the strict parse that distinguishes a valid-empty catalogue from a malformed
# one. Imported at module top (not lazily) so tests can monkeypatch it.
from provider_client import (  # noqa: E402 — backend path set by .client
    list_provider_models as _list_provider_models,
    model_catalog_error_slug as _model_catalog_error_slug,
)

FIRST_REPLY_TIMEOUT = 300.0   # includes provider cold start and queue wait
NEXT_REPLY_TIMEOUT = 180.0
MEMORY_POLL_SEC = 300.0
SEND_RETRY_SEC = 90.0         # post-deploy worker/policy readiness window

# Relay providers carry their own base_url and expose an OpenAI-style /models
# catalogue; official providers do not need (and are not asked for) a preflight.
_RELAY_PROVIDERS = {"openai_compatible", "openrouter"}

# Cell result reserved for "the test instrument itself is stale" — the relay no
# longer sells the model the key pool names. It is neither PASS nor FAIL: a
# configured model that is off-sale self-tests as 503 "no available channel",
# which is indistinguishable from a real product failure (T544; also seen
# 2026-08-17, keys.env line 9). ``p0_blocks_release`` deliberately does not list
# it, so it is surfaced on its own line without blocking or greening a release.
RESULT_INSTRUMENT_STALE = "instrument_stale"


def relay_model_preflight(provider: str, base_url: str, api_key: str,
                          candidates: list[str]) -> tuple[str, str]:
    """Before setup, confirm a relay still sells one of the configured models.

    Returns ``(status, detail)`` with status one of:
      - ``in_list``      at least one candidate is on sale now → run setup
      - ``stale``        the relay returned a VALID catalogue (possibly empty)
                         that lists none of the candidates → INSTRUMENT_STALE
      - ``unverifiable`` the catalogue could not be read or was malformed →
                         inconclusive; do NOT gate, fall through to setup
      - ``skip``         not a relay provider, or an openai_compatible relay with
                         no base_url → no preflight

    Uses the canonical ``list_provider_models`` fetcher, so OpenRouter is queried
    on its key-scoped ``/models/user`` route (public ``/models`` ignores the
    key's privacy/ZDR/guardrail eligibility) and OpenRouter's default base URL is
    supplied when the cell carries none. ``detail`` carries only model ids /
    error slugs — never the api_key. ``unverifiable`` stays distinct from
    ``stale``: 'could not ask' must never be reported as 'confirmed off-sale',
    and a VALID empty catalogue IS off-sale (a real 200 ``data: []``), not
    unverifiable.
    """
    if provider not in _RELAY_PROVIDERS:
        return "skip", ""
    # openai_compatible has no default endpoint; without a base_url there is
    # nothing to query. OpenRouter carries its own canonical default.
    if provider == "openai_compatible" and not base_url:
        return "skip", ""
    try:
        result = _list_provider_models(provider, api_key, base_url)
    except Exception as exc:  # noqa: BLE001 — any fetch/parse error is inconclusive
        try:
            slug = _model_catalog_error_slug(exc)
        except Exception:  # noqa: BLE001
            slug = type(exc).__name__
        return "unverifiable", f"catalogue unreadable: {slug}"
    if not result.get("catalog_supported", True):
        return "unverifiable", "provider does not expose a model catalogue"
    offered = {
        str(m.get("id")) for m in result.get("models", [])
        if isinstance(m, dict) and m.get("id")
    }
    present = [m for m in candidates if m in offered]
    if present:
        # Presence is proven even from a partial prefix — the candidate is here.
        return "in_list", f"model on sale: {present[0]}"
    # Absence is only proven from a COMPLETE catalogue. list_provider_models
    # returns complete=False when a later page failed/truncated; the candidate
    # could be on a page we never fetched, so "not seen" is not "off-sale".
    # Treat that as unverifiable (do NOT gate) rather than stale (fail-open bug).
    if not result.get("complete", True):
        warns = "; ".join(str(w) for w in (result.get("warnings") or []))[:160]
        return "unverifiable", f"catalogue incomplete (truncated/partial): {warns or 'no detail'}"
    sample = ", ".join(sorted(offered)[:8]) if offered else "(empty catalogue)"
    return "stale", f"none of {candidates} on sale; relay offers: {sample}"

FACT_MSG = "你好呀。顺便记住一件小事：我最喜欢的颜色是青色。"
CONTINUITY_MSG = "我刚才说我最喜欢的颜色是什么来着？"


def _hosted_send(c: E2EClient, text: str) -> tuple[float, str]:
    """POST /v1/model_api/chat/send with bounded V2-readiness retry.
    Returns (send_epoch, error) — error=="" on accepted."""
    deadline = time.time() + SEND_RETRY_SEC
    last = ""
    client_msg_id = str(uuid.uuid4())
    while time.time() < deadline:
        sent_at = time.time()
        r = c.post("/v1/model_api/chat/send", json={
            "message": text, "client_msg_id": client_msg_id,
        })
        if r.status_code == 202:
            body = r.json()
            c.record_failure_locator("trace_id", (body.get("user_message") or {}).get("id"))
            server_ts = float((body.get("user_message") or {}).get("ts") or sent_at)
            return server_ts, ""
        last = f"{r.status_code} {r.text[:120]}"
        if r.status_code == 503 and any(
            code in r.text
            for code in ("workers_unavailable", "runtime_policy_not_ready")
        ):
            time.sleep(5)
            continue
        break
    return 0.0, last


def run_hosted_cell(cell: HostedCell, pool: dict[str, str]) -> dict:
    """Returns {cell, result: ok|fail|skip, steps: [(name, ok|fail|warn|skip, detail)]}."""
    key = cell.key(pool)
    if not key:
        return {"cell": cell.name, "result": "skip",
                "steps": [("key", "skip", f"no {cell.key_env} in pool")]}

    steps: list[tuple[str, str, str]] = []
    active_client: E2EClient | None = None

    def step(name: str, ok: bool, detail: str = "", *, warn: bool = False) -> bool:
        steps.append((name, "ok" if ok else ("warn" if warn else "fail"), detail))
        if not ok and not warn and active_client is not None:
            active_client.preserve_failure(f"{name}: {detail or 'failed'}")
        return ok

    def verdict_step(name: str, verdict: str, detail: str) -> bool:
        """三态记录:ok / fallback / fail。fallback 单列，不并进任何一边。

        并进 ok 就是我们正在修的那个 bug;并进 fail 会让「交付了失败话术」和
        「压根没回来」在报表上同形，而这两件事的排查方向完全不同。
        """
        steps.append((name, verdict, detail))
        if verdict != VERDICT_OK and active_client is not None:
            active_client.preserve_failure(f"{name}[{verdict}]: {detail or verdict}")
        return verdict == VERDICT_OK

    models = cell.models or [m for m in [pool.get("E2E_RELAY_MODEL", "")] if m]
    if not models:
        return {"cell": cell.name, "result": "skip",
                "steps": [("model", "skip", "no model candidates configured")]}

    # Model-list preflight (relay only), BEFORE provisioning an account: a
    # configured model the relay no longer sells is a stale instrument, not a
    # product failure. Doing it first avoids creating a throwaway account we
    # would only tear down. `unverifiable` is inconclusive and does not gate.
    pf_status, pf_detail = relay_model_preflight(
        cell.provider, cell.base_url(pool), key, models)
    if pf_status == "stale":
        return {"cell": cell.name, "result": RESULT_INSTRUMENT_STALE,
                "steps": [("model_preflight", RESULT_INSTRUMENT_STALE, pf_detail)]}

    with E2EClient.provision(route="model_api") as c:
        active_client = c
        c.configure_failure_evidence(cell=f"hosted:{cell.name}")
        # Record the preflight outcome as a (non-gating) step for the report.
        # `unverifiable` is a warn: we could not confirm the catalogue, so we let
        # setup proceed rather than block on our own inability to ask.
        if pf_status == "in_list":
            step("model_preflight", True, pf_detail)
        elif pf_status == "unverifiable":
            step("model_preflight", True, pf_detail, warn=True)
        # -- setup: try model candidates until the live self-test passes ------
        setup_detail, ok = "", False
        for model in models:
            payload = {"provider": cell.provider, "model": model, "api_key": key}
            base = cell.base_url(pool)
            if base:
                payload["base_url"] = base
            r = c.post("/v1/model_api/setup", json=payload)
            if r.status_code == 200:
                body = r.json()
                test_status = ((body.get("config") or {}).get("test_status")
                               or body.get("test_status") or "")
                ok = test_status == "ok"
                setup_detail = f"model={model} test_status={test_status or '?'}"
                if ok:
                    break
            else:
                setup_detail = f"{model}: {r.status_code} {r.text[:120]}"
        if not step("setup", ok, setup_detail):
            return {"cell": cell.name, "result": "fail", "steps": steps,
                    "user_id": c.user_id}

        run_start = time.time()

        # -- first chat roundtrip ---------------------------------------------
        sent, err = _hosted_send(c, FACT_MSG)
        if err:
            step("chat-send", False, err)
            return {"cell": cell.name, "result": "fail", "steps": steps,
                    "user_id": c.user_id}
        reply = c.wait_reply(sent, timeout=FIRST_REPLY_TIMEOUT)
        text = c.message_text(reply) if reply else ""
        # 闸不能只测「非空」：失效时交付给用户的兜底话术也是非空的，那样闸的
        # 盲区正对着失效方向。改问后端自己的判词，见 client.classify_reply。
        chat_verdict, chat_detail = c.classify_reply(reply, text)
        if not verdict_step("chat", chat_verdict,
                            f"{time.time() - sent:.0f}s; head={text[:40]!r}; "
                            f"{chat_detail}"):
            return {"cell": cell.name, "result": chat_verdict, "steps": steps,
                    "user_id": c.user_id}

        # -- tier/readability continuity (HARD P0) ----------------------------
        # A reply ARRIVING is not enough: encrypted-tier replies must decrypt
        # with the user's key; plaintext-tier replies must be canonical `body`
        # rows with no residual crypto fields. A shape/read failure blocks the
        # release just like a dead chat loop.
        try:
            dec = c.read_reply_strict(reply)
            dec_err = ""
        except Exception as de:  # noqa: BLE001
            dec, dec_err = "", f"{type(de).__name__}: {de}"
        # 解得开只是第一层；解出来的正文同样不能是失败话术。这一步只跑辅助闸
        # (常量比对)——主闸已在 chat 步问过后端判词，不重复那次调用。
        dec_verdict, dec_detail = _decrypt_verdict(dec, dec_err)
        if not verdict_step("decrypt", dec_verdict, dec_detail):
            return {"cell": cell.name, "result": dec_verdict, "steps": steps,
                    "user_id": c.user_id}

        # -- continuity -------------------------------------------------------
        sent2, err2 = _hosted_send(c, CONTINUITY_MSG)
        reply2 = c.wait_reply(sent2, timeout=NEXT_REPLY_TIMEOUT) if not err2 else None
        text2 = c.message_text(reply2) if reply2 else ""
        cont_ok = reply2 is not None and "青" in text2
        step("continuity", cont_ok, f"head={text2[:40]!r}" if reply2 else (err2 or "no reply"))

        # -- memory (WARN: capture is async) ---------------------------------
        deadline = time.time() + MEMORY_POLL_SEC
        found = False
        while time.time() < deadline and not found:
            found = any("青" in s for s in c.memory_summaries())
            if not found:
                time.sleep(15)
        step("memory", found, "card found" if found else
             f"no card within {MEMORY_POLL_SEC:.0f}s (async capture)", warn=not found)

        # -- error-bubble sanity ---------------------------------------------
        bubbles = c.system_bubbles_since(run_start)
        step("no-error-bubbles", not bubbles, f"{len(bubbles)} system notice(s)")

        # fallback 单列：它阻断发版(用户实际收到的是失败话术)，但不并进
        # "fail"，否则「交付了失败话术」和「压根没回来」在报表上同形。
        if any(s[1] == "fail" for s in steps):
            result = "fail"
        elif any(s[1] == VERDICT_FALLBACK for s in steps):
            result = VERDICT_FALLBACK
        else:
            result = "ok"
        return {"cell": cell.name, "result": result,
                "steps": steps, "user_id": c.user_id}
