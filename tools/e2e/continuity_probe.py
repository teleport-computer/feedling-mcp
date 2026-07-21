"""Message-continuity deep probe for pre Runtime V2 (model-touching, per provider).

Every case asserts a coherence invariant, never "a reply arrived": multi-turn
context recall, the HARD decrypt-continuity assertion (usr_f13f922a), exactly one
reply per turn, client_msg_id idempotency, seq total order, cold-start first
reply, long-message cap, rapid double-send, and CoT thinking-bubble delivery.

Fault-injection (stream cut / fake model name / slow first token / intermittent
5xx) needs the mock relay — see RELEASE_TESTING_PROTOCOL §4.6/§10; deferred here.
"""
from __future__ import annotations

import time
import uuid

from .client import E2EClient
from .hosted import FIRST_REPLY_TIMEOUT, NEXT_REPLY_TIMEOUT
from .probe_common import BLOCKED_EVIDENCE, PASS, PRODUCT_FAIL, Probe, new_marker


def run_continuity_probe(c: E2EClient, cfg: dict) -> dict:
    p = Probe("continuity")
    # first send on a fresh account IS the cold start
    p.guard("multi_turn_context_and_coldstart", lambda: _multi_turn(c, p))
    p.guard("idempotent_double_post", lambda: _idempotent(c))
    p.guard("seq_total_order", lambda: _seq_order(c))
    p.guard("long_message_cap", lambda: _long_message(c))
    p.guard("rapid_double_send_no_dup", lambda: _rapid_double(c))
    p.guard("cot_thinking_bubble", lambda: _cot(c))
    return p.result()


_AGENT_ROLES = ("agent", "openclaw", "assistant")


def _user_row_count(c: E2EClient) -> int:
    r = c.get("/v1/chat/history", params={"since": 0, "limit": 500})
    r.raise_for_status()
    return sum(1 for m in (r.json().get("messages") or []) if m.get("role") == "user")


def _send(c: E2EClient, text: str) -> tuple[float, str]:
    """Send one hosted message; return (server_ts, user_message_id). The id lets us
    correlate the reply by reply_message_id/reply_to_message_id instead of a fuzzy
    timestamp window (a late memory-turn reply must never be mistaken for this turn's)."""
    cmid = str(uuid.uuid4())
    deadline = time.time() + 90
    last = ""
    while time.time() < deadline:
        r = c.post("/v1/model_api/chat/send", json={"message": text, "client_msg_id": cmid})
        if r.status_code == 202:
            u = r.json().get("user_message") or {}
            uid = str(u.get("id") or "")
            ts = u.get("ts")
            if not uid or ts is None:
                # an empty id would make _replies_for match every unrelated agent row
                # ('' == ''), so a malformed 202 must fail loud, not fail open.
                raise RuntimeError(f"202 missing user_message id/ts: {r.text[:120]}")
            return float(ts), uid
        last = f"{r.status_code} {r.text[:100]}"
        if r.status_code == 503 and any(x in r.text for x in ("workers_unavailable", "runtime_policy_not_ready")):
            time.sleep(5)
            continue
        break
    raise RuntimeError(f"hosted send failed: {last}")


def _rows_since(c: E2EClient, since: float) -> list[dict]:
    r = c.get("/v1/chat/history", params={"since": max(0, since - 1), "limit": 200})
    r.raise_for_status()
    return [m for m in (r.json().get("messages") or []) if isinstance(m, dict)]


def _replies_for(rows: list[dict], user_id: str) -> list[dict]:
    """Agent rows that are THE correlated reply to user_id (by the user row's
    reply_message_id, or the agent row's reply_to_message_id)."""
    if not user_id:
        return []                       # never correlate against an empty id
    reply_id = ""
    for m in rows:
        if str(m.get("id") or "") == user_id:
            reply_id = str(m.get("reply_message_id") or "")
            break
    return [m for m in rows
            if str(m.get("role") or "") in _AGENT_ROLES
            and ((reply_id and str(m.get("id") or "") == reply_id)
                 or str(m.get("reply_to_message_id") or "") == user_id)]


def _reply_for(c: E2EClient, user_id: str, since: float, *, timeout: float) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = _replies_for(_rows_since(c, since), user_id)
        if found:
            return min(found, key=lambda m: float(m.get("ts") or 0))
        time.sleep(3)
    return None


def _multi_turn(c: E2EClient, p: Probe):
    """Turn 1 plants a fact + proves decrypt continuity + exactly-one-reply (by
    correlated id, not timestamp); Turn 2 proves the model recalls it."""
    mk = new_marker()
    t0 = time.time()
    # phrase it as conversational context, NOT "remember this" — the latter makes the
    # model answer "saved!" on recall instead of reciting the value.
    sent, uid1 = _send(c, f"随口跟你说个代号：{mk}。等会我会问你它是什么。")
    reply = _reply_for(c, uid1, sent, timeout=FIRST_REPLY_TIMEOUT)
    if reply is None:
        return PRODUCT_FAIL, f"no first reply within {FIRST_REPLY_TIMEOUT:.0f}s (cold start)"
    p.add("coldstart_first_reply", PASS, f"{time.time()-t0:.0f}s")

    # HARD decrypt continuity
    try:
        dec = c.decrypt_reply(reply)
        p.add("decrypt_continuity_HARD", PASS if dec.strip() else PRODUCT_FAIL,
              f"len={len(dec)}" if dec.strip() else "empty plaintext")
    except Exception as e:  # noqa: BLE001
        p.add("decrypt_continuity_HARD", PRODUCT_FAIL, f"{type(e).__name__}: {e}")

    # exactly ONE reply correlated to turn 1's user id (wait for a possible dup)
    time.sleep(12)
    replies = _replies_for(_rows_since(c, sent), uid1)
    p.add("exactly_one_reply", PASS if len(replies) == 1 else PRODUCT_FAIL,
          f"{len(replies)} reply/replies correlated to one user turn (id={uid1[:8]})")

    # turn 2: recall
    sent2, uid2 = _send(c, "我刚才说的那个代号是什么？把它原样念给我。")
    reply2 = _reply_for(c, uid2, sent2, timeout=NEXT_REPLY_TIMEOUT)
    text2 = c.message_text(reply2) if reply2 else ""
    return (PASS if (reply2 is not None and mk in text2) else PRODUCT_FAIL,
            f"recall {'ok' if mk in text2 else 'MISSING'}; head={text2[:48]!r}")


def _idempotent(c: E2EClient):
    """Same client_msg_id posted twice must ingest ONE user message (dedup)."""
    before = _user_row_count(c)
    cmid = str(uuid.uuid4())
    msg = f"幂等测试 {new_marker()}"
    codes = []
    for _ in range(2):
        r = c.post("/v1/model_api/chat/send", json={"message": msg, "client_msg_id": cmid})
        codes.append(r.status_code)
        time.sleep(1)
    time.sleep(4)
    delta = _user_row_count(c) - before
    # both POSTs must have satisfied the send contract (202), AND only one row lands
    if any(cd != 202 for cd in codes):
        return PRODUCT_FAIL, f"idempotent send had a non-202 attempt (codes={codes})"
    return (PASS if delta == 1 else PRODUCT_FAIL,
            f"same client_msg_id ×2 (codes={codes}) → {delta} user row(s); must be 1")


def _seq_order(c: E2EClient):
    r = c.get("/v1/chat/history", params={"since": 0, "limit": 500})
    r.raise_for_status()
    rows = r.json().get("messages") or []
    seqs = [m.get("seq") for m in rows if m.get("seq") is not None]
    if not seqs:
        return BLOCKED_EVIDENCE, "history rows expose no seq field"
    strictly_increasing = all(b > a for a, b in zip(seqs, seqs[1:]))
    unique = len(set(seqs)) == len(seqs)
    return (PASS if (strictly_increasing and unique) else PRODUCT_FAIL,
            f"n={len(seqs)} strictly_increasing={strictly_increasing} unique={unique}")


def _long_message(c: E2EClient):
    """Contract (chat_send_core.py:70-71): a >12000-char message is rejected with
    EXACTLY 413 and a length error — not any 4xx, and never a 5xx."""
    big = "长消息压力测试。" * 2000  # ~16k chars, well over the 12000 cap
    r = c.post("/v1/model_api/chat/send", json={"message": big, "client_msg_id": str(uuid.uuid4())})
    if r.status_code != 413:
        return PRODUCT_FAIL, f"oversized → {r.status_code} (expected exact 413 length cap): {r.text[:80]}"
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        return PRODUCT_FAIL, f"413 body is not JSON: {r.text[:80]}"
    if body.get("error") != "message too long" or int(body.get("max_chars") or 0) != 12000:
        return PRODUCT_FAIL, f"413 body off-contract: {body}"
    return PASS, "oversized rejected 413 error='message too long' max_chars=12000"


def _rapid_double(c: E2EClient):
    """Two DIFFERENT messages sent back-to-back. Runtime V2 intentionally
    COALESCES unreplied user messages into one turn (worker.py gathers all
    unreplied inputs since the cursor), so the invariant is NOT "two replies" —
    it is: no flood (1–2 replies, never a storm) AND neither message is silently
    dropped (both markers acknowledged across the reply text)."""
    a, b = new_marker(), new_marker()
    s1, uid1 = _send(c, f"第一件事，暗号 {a}，请记住。")
    s2, uid2 = _send(c, f"第二件事，暗号 {b}，也请记住。")
    _reply_for(c, uid2, s2, timeout=FIRST_REPLY_TIMEOUT)
    time.sleep(12)
    rows = _rows_since(c, min(s1, s2))
    r1, r2 = _replies_for(rows, uid1), _replies_for(rows, uid2)
    # no user turn may have MORE than one correlated reply (that would be a dup/flood)
    if len(r1) > 1 or len(r2) > 1:
        return PRODUCT_FAIL, f"duplicate reply on a turn: uid1={len(r1)} uid2={len(r2)}"
    total = len({str(m.get('id')) for m in r1 + r2})
    if total == 0:
        return PRODUCT_FAIL, "both rapid turns went unanswered (dropped)"
    # prove neither message was dropped: a recall turn must surface BOTH markers
    s3, uid3 = _send(c, "我刚说的两个暗号分别是什么？都告诉我。")
    reply3 = _reply_for(c, uid3, s3, timeout=NEXT_REPLY_TIMEOUT)
    text = c.message_text(reply3) if reply3 else ""
    both = a in text and b in text
    return (PASS if both else PRODUCT_FAIL,
            f"{total} distinct reply(ies), no per-turn dup; both markers acknowledged={both}")


def _cot(c: E2EClient):
    """CoT thinking bubble: V2 surfaces provider reasoning as the reply's thinking
    envelope. Provider/model-dependent — absent reasoning is BLOCKED_EVIDENCE, not fail."""
    sent, uid = _send(c, "请一步步想清楚再回答：13 和 17 哪个更接近 15？为什么？")
    reply = _reply_for(c, uid, sent, timeout=FIRST_REPLY_TIMEOUT)
    if reply is None:
        return PRODUCT_FAIL, "no reply"
    thinking = (reply.get("thinking") or reply.get("thinking_envelope")
                or (reply.get("extra") or {}).get("thinking_envelope")
                or (reply.get("extra") or {}).get("thinking"))
    if not thinking:
        return BLOCKED_EVIDENCE, "reply carries no thinking bubble (model may not emit reasoning)"
    if isinstance(thinking, dict) and thinking.get("body_ct"):
        try:
            txt = c.open_envelope(thinking)
            return (PASS if txt.strip() else PRODUCT_FAIL,
                    f"thinking bubble decrypts len={len(txt)}")
        except Exception as e:  # noqa: BLE001
            return PRODUCT_FAIL, f"thinking bubble present but undecryptable: {e}"
    return PASS, "thinking bubble present"
