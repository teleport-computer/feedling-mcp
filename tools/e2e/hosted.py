"""Hosted (model_api) P0 cell: provision → setup → chat → continuity →
memory(warn) → bubbles(sanity) → teardown. One call per provider-key class.

No unlock step: hosted accounts are gate-exempt by design (see unlock.py
docstring). Send rides the canonical hosted entry POST /v1/model_api/chat/send
(202 + async reply via history); a 503 hosting_runtime_unavailable right after
setup is the supervisor's discovery lag and is retried with backoff — it fires
BEFORE the message is written, so retrying cannot double-send
(backend/hosted/chat_send_core.py:107-126).

Pass criteria mirror docs/testing/RELEASE_TESTING_PROTOCOL.md §3. Memory is a
WARN (capture is async by design); everything else is pass/fail.
"""
from __future__ import annotations

import time
import uuid

from .client import E2EClient
from .config import HostedCell

FIRST_REPLY_TIMEOUT = 300.0   # includes the hosted runner's per-user spawn
NEXT_REPLY_TIMEOUT = 180.0
MEMORY_POLL_SEC = 300.0
SEND_RETRY_SEC = 90.0         # 503 hosting_runtime_unavailable backoff window

FACT_MSG = "你好呀。顺便记住一件小事：我最喜欢的颜色是青色。"
CONTINUITY_MSG = "我刚才说我最喜欢的颜色是什么来着？"


def _hosted_send(c: E2EClient, text: str) -> tuple[float, str]:
    """POST /v1/model_api/chat/send with discovery-lag retry.
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
            server_ts = float((body.get("user_message") or {}).get("ts") or sent_at)
            return server_ts, ""
        last = f"{r.status_code} {r.text[:120]}"
        if r.status_code == 503 and "hosting_runtime_unavailable" in r.text:
            time.sleep(8)     # supervisor discovery lag after setup — safe retry
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

    def step(name: str, ok: bool, detail: str = "", *, warn: bool = False) -> bool:
        steps.append((name, "ok" if ok else ("warn" if warn else "fail"), detail))
        return ok

    models = cell.models or [m for m in [pool.get("E2E_RELAY_MODEL", "")] if m]
    if not models:
        return {"cell": cell.name, "result": "skip",
                "steps": [("model", "skip", "no model candidates configured")]}

    with E2EClient.provision(route="model_api") as c:
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

        # -- first chat roundtrip (spawn included) ----------------------------
        sent, err = _hosted_send(c, FACT_MSG)
        if err:
            step("chat-send", False, err)
            return {"cell": cell.name, "result": "fail", "steps": steps,
                    "user_id": c.user_id}
        reply = c.wait_reply(sent, timeout=FIRST_REPLY_TIMEOUT)
        text = c.message_text(reply) if reply else ""
        if not step("chat", reply is not None and bool(text.strip()),
                    f"{time.time() - sent:.0f}s; head={text[:40]!r}"):
            return {"cell": cell.name, "result": "fail", "steps": steps,
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

        hard_fail = any(s[1] == "fail" for s in steps)
        return {"cell": cell.name, "result": "fail" if hard_fail else "ok",
                "steps": steps, "user_id": c.user_id}
