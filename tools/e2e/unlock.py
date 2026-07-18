"""Minimal programmatic onboarding unlock — get a fresh account's replies
deliverable. Recipe verified against backend code by codex2 (2026-07-17
mailbox: "最小 unlock 配方"); do NOT copy tools/provider_smoke's legacy
init_identity/open_chat_gate flow — it predates identity-never-gates.

- model_api: NO unlock needed. `_bootstrap_state` always reports main_loop
  (identity/memory are informational only, backend/bootstrap/gates.py:39-69)
  and the hosted route bypasses `_gate_bootstrap_for_chat` entirely (:137-159).
  register → setup → send. Do NOT run a fresh_start genesis job here: while it
  is `processing` it temporarily BLOCKS the runner spawn (supervisor.py:918-948)
  — the opposite of an unlock.

- resident: the ONLY chat unlock is (a) the official consumer's recent poll
  heartbeat (X-Feedling-Consumer: feedling-chat-resident, 180s window) and
  (b) a passed /v1/chat/verify_loop. Identity/memory/genesis are NOT gates
  (locked by tests/test_bootstrap_gates.py:430-474). The 2026-07-16 E2E 409
  was a missed verify_loop, nothing else.
"""
from __future__ import annotations

import time

import httpx

from .client import E2EClient


def wait_resident_consumer_passing(client: E2EClient, *, timeout: float = 90.0) -> bool:
    """Poll /v1/bootstrap/status until resident_consumer.passing (the launched
    consumer's official-header poll heartbeat has been seen)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get("/v1/bootstrap/status")
        if r.status_code == 200:
            rc = (r.json().get("resident_consumer") or {})
            if rc.get("passing"):
                return True
        time.sleep(3)
    return False


def verify_loop(client: E2EClient, *, timeout: float = 120.0) -> bool:
    """POST /v1/chat/verify_loop until passing=true. Server posts a hidden
    verify_ping; the consumer's reply is allowed through the gate and flips
    `chat_loop_verified` — after which real replies deliver."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = client.post("/v1/chat/verify_loop", json={"timeout_sec": 45}, timeout=60)
        except httpx.TransportError:
            time.sleep(5)
            continue
        if r.status_code == 200:
            body = r.json()
            if body.get("passing"):
                return True
        time.sleep(5)
    return False
