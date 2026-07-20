#!/usr/bin/env python3
"""Live e2e smoke: stale resident-consumer maintenance injection (test env only).

Simulates a pre-06-26 resident consumer (official name, NO Consumer-Commit
header) and asserts the full loop:
  a. maintenance message injected (user-role, source=resident_maintenance),
     decryptable with the USER's own key, plaintext content non-empty (direct-poll)
  b. notice resident_consumer_stale / blame=user_environment / copyable_prompt
  c. reply with source=resident_maintenance accepted
  d. rate-limit: no duplicate injection
  e. convergence: correct commit header -> notice resolved
Account is provisioned fresh and hard-deleted in teardown.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx

from tools.e2e.client import E2EClient, TEST_API

GRACE_WAIT_SEC = 17 * 60   # server missing-commit grace 15min + margin
POLL_EVERY_SEC = 60

CONSUMER_HEADERS = {
    "X-Feedling-Consumer": "feedling-chat-resident",
    "X-Feedling-Consumer-Id": "e2e-stale-sim:1",
    "X-Feedling-Consumer-Version": "resident-v1",
    # deliberately NO X-Feedling-Consumer-Commit -> "史前版本" simulation
}


def step(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}", flush=True)
    if not ok:
        raise SystemExit(f"e2e FAIL at step: {name}")


def poll(c: E2EClient, *, commit: str = "") -> dict:
    headers = {"X-API-Key": c.api_key, **CONSUMER_HEADERS}
    if commit:
        headers["X-Feedling-Consumer-Commit"] = commit
    r = httpx.get(
        f"{c.api_url}/v1/chat/poll",
        params={"since": 0, "timeout": 0},
        headers=headers,
        timeout=30,
        verify=False,
    )
    r.raise_for_status()
    return r.json()


def maintenance_rows(c: E2EClient) -> list[dict]:
    r = c.get("/v1/chat/history")
    r.raise_for_status()
    msgs = r.json().get("messages") or r.json().get("history") or []
    return [
        m for m in msgs
        if isinstance(m, dict) and m.get("source") == "resident_maintenance"
        and m.get("role") == "user"
    ]


def stale_notices(c: E2EClient) -> list[dict]:
    r = c.get("/v1/notices")
    r.raise_for_status()
    body = r.json()
    items = body.get("notices") if isinstance(body, dict) else body
    return [
        n for n in (items or [])
        if isinstance(n, dict) and n.get("error_class") == "resident_consumer_stale"
    ]


def main() -> None:
    print(f"[e2e] start {time.strftime('%H:%M:%S')}", flush=True)
    with E2EClient.provision(route="resident", api_url=TEST_API) as c:
        print(f"[e2e] provisioned {c.user_id}", flush=True)

        # t0: first stale poll starts the missing-commit timer
        body = poll(c)
        expected_commit = str(
            (body.get("client_release") or {}).get("expected_consumer_commit") or ""
        )
        step("t0-poll", True, f"expected_commit={expected_commit or '<none>'}")
        step("t0-no-early-injection", not maintenance_rows(c))

        # wait out the grace window, polling periodically like a real consumer
        deadline = time.time() + GRACE_WAIT_SEC
        injected: list[dict] = []
        while time.time() < deadline:
            time.sleep(POLL_EVERY_SEC)
            poll(c)
            injected = maintenance_rows(c)
            if injected:
                break
        step("a1-injected", bool(injected),
             f"after ~{int(GRACE_WAIT_SEC - (deadline - time.time()))}s")

        row = injected[0]
        # a. decryptable with the user's own key (hard assertion, no fallback)
        text = c.decrypt_reply(row)
        step("a2-user-key-decrypt", bool(text.strip()), f"len={len(text)}")
        step("a3-content-is-maintenance",
             "维护" in text or "resident" in text, text[:60].replace("\n", " "))
        # direct-poll plaintext fallback present on the row
        step("a4-plaintext-content", bool(str(row.get("content") or "").strip()))

        # b. notice with copyable prompt
        notices = stale_notices(c)
        step("b1-notice-emitted", bool(notices))
        n = notices[0]
        step("b2-blame-user-environment", n.get("blame") == "user_environment",
             str(n.get("blame")))
        step("b3-copyable-prompt", bool(str(n.get("copyable_prompt") or "").strip()))

        # c. reply as the (old) consumer would after fixing — v1 sealed envelope,
        # suppress_push semantics = empty alert_body (post_reply parity)
        msg_id = str(row.get("id") or "")
        r = c.post("/v1/chat/response", json={
            "envelope": c._seal("e2e: 已按维护提示更新并重启 resident。"),
            "reply_to_message_id": msg_id,
            "source": "resident_maintenance",
            "alert_body": "",
        })
        step("c1-reply-accepted", r.status_code in (200, 202),
             f"HTTP {r.status_code} {r.text[:120]}")

        # d. rate limit — another stale poll must not inject a second prompt
        poll(c)
        time.sleep(3)
        step("d1-no-duplicate", len(maintenance_rows(c)) == 1)

        # e. convergence — correct commit header, then notice resolves
        if expected_commit and expected_commit != "dev":
            poll(c, commit=expected_commit)
            time.sleep(3)
            resolved = [x for x in stale_notices(c) if x.get("resolved")]
            step("e1-converged-resolved", bool(resolved))
        else:
            print("[e2e] SKIP e1 — server did not advertise an expected commit",
                  flush=True)

    print("[e2e] teardown complete — account deleted", flush=True)
    print("[e2e] ALL PASS", flush=True)


if __name__ == "__main__":
    main()
