#!/usr/bin/env python3
"""
Feedling backend API test suite.

Usage:
    python tests/test_api.py                  # needs --key or --multi-tenant (everything is auth-gated)
    python tests/test_api.py https://api.feedling.app --multi-tenant
    python tests/test_api.py http://localhost:5001 --key <shared_api_key>

NOTE: this is a live-server integration script (point it at a running
backend), NOT a pytest suite — keep it excluded from `pytest tests/`
via --ignore, same as e2e_model_api_test.py.

Post-SINGLE_USER/v0 strip (2026-04-20):
    The backend rejects plaintext chat / identity / memory writes with 400.
    These tests now (a) assert that rejection and (b) exercise the v1 envelope
    path for the same write endpoints. They do NOT encrypt anything — the
    envelopes here carry dummy ciphertext. End-to-end encryption semantics
    (enclave wrap/unwrap) live in tools/e2e_encryption_test.py.

Tests cover:
    1. Read endpoints (screen/analyze, frames, tokens)
    2. Chat: plaintext write rejected; v1 envelope stored verbatim
    3. Chat response: plaintext rejected; v1 envelope accepted
    4. Long-poll: timeout case
    5. Long-poll: wakes when user sends a v1 envelope
    6. Full round-trip: user envelope → poll wakes → openclaw envelope → history
    7. Bootstrap
    8. Identity: plaintext init rejected; get still works
    9. Memory garden: plaintext rejected; v1 envelope add/list/get/delete
   10. Multi-tenant: v1 isolation + 401 enforcement
   11. v1 envelope validation (missing fields, bad visibility, etc.)
"""

import base64
import sys
import threading
import time
import uuid

import requests

BASE_URL = "http://localhost:5001"
MULTI_TENANT = False
SHARED_KEY = ""
USER_ID = ""

for arg in sys.argv[1:]:
    if arg == "--multi-tenant":
        MULTI_TENANT = True
    elif arg.startswith("--key="):
        SHARED_KEY = arg.split("=", 1)[1]
    elif arg == "--key":
        continue
    elif arg.startswith("http"):
        BASE_URL = arg.rstrip("/")

# Late-capture: --key <value>
args = sys.argv[1:]
for i, a in enumerate(args):
    if a == "--key" and i + 1 < len(args):
        SHARED_KEY = args[i + 1]

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
SKIP = "\033[93m~\033[0m"

_failures = []


def check(name: str, condition: bool, detail: str = ""):
    if condition:
        print(f"  {PASS} {name}")
    else:
        print(f"  {FAIL} {name}" + (f" — {detail}" if detail else ""))
        _failures.append(name)


def section(title: str):
    print(f"\n{'─' * 50}")
    print(f"  {title}")
    print(f"{'─' * 50}")


# ---------------------------------------------------------------------------
# Auth setup: register a fresh multi-tenant user if requested
# ---------------------------------------------------------------------------

AUTH_HEADERS = {}

if MULTI_TENANT:
    section("0. Multi-tenant registration")
    rr = requests.post(f"{BASE_URL}/v1/users/register",
                       json={"public_key": "test-pubkey"}, timeout=5)
    check("POST /v1/users/register returns 201", rr.status_code == 201,
          f"got {rr.status_code}: {rr.text[:120]}")
    if rr.status_code == 201:
        body = rr.json()
        check("register response has api_key",
              bool(body.get("api_key")))
        check("register response has user_id (usr_*)",
              body.get("user_id", "").startswith("usr_"))
        SHARED_KEY = body["api_key"]
        USER_ID = body.get("user_id", "")

if SHARED_KEY:
    AUTH_HEADERS = {"X-API-Key": SHARED_KEY}

# If we were handed a key but not a user_id (i.e. --key mode), fetch it via whoami.
if SHARED_KEY and not USER_ID:
    wr = requests.get(f"{BASE_URL}/v1/users/whoami",
                      headers=AUTH_HEADERS, timeout=5)
    if wr.status_code == 200:
        USER_ID = wr.json().get("user_id", "")

# Monkey-patch requests.{get,post,delete} so existing test bodies auto-forward
# X-API-Key without us needing to touch every call site.
_orig_get = requests.get
_orig_post = requests.post
_orig_delete = requests.delete


def _inject(headers):
    h = dict(headers or {})
    for k, v in AUTH_HEADERS.items():
        h.setdefault(k, v)
    return h


def _auth_get(url, **kw):
    kw["headers"] = _inject(kw.get("headers"))
    return _orig_get(url, **kw)


def _auth_post(url, **kw):
    kw["headers"] = _inject(kw.get("headers"))
    return _orig_post(url, **kw)


def _auth_delete(url, **kw):
    kw["headers"] = _inject(kw.get("headers"))
    return _orig_delete(url, **kw)


requests.get = _auth_get
requests.post = _auth_post
requests.delete = _auth_delete


CONSUMER_HEADERS = {
    "X-Feedling-Consumer": "feedling-chat-resident",
    "X-Feedling-Consumer-Id": "test-api-consumer",
    "X-Feedling-Consumer-Version": "test-api",
    "X-Feedling-Consumer-Commit": "test-api",
}


def _raw_headers(*extras):
    headers = dict(AUTH_HEADERS)
    for extra in extras:
        headers.update(extra or {})
    return headers


def _seed_envelope(owner: str, *, visibility: str = "shared",
                   with_k_enclave: bool = True, **overrides) -> dict:
    body = base64.b64encode(uuid.uuid4().bytes * 4).decode()
    nonce = base64.b64encode(uuid.uuid4().bytes[:12] * 2).decode()
    k = base64.b64encode(uuid.uuid4().bytes * 3).decode()
    env = {
        "v": 1,
        "body_ct": body,
        "nonce": nonce,
        "K_user": k,
        "enclave_pk_fpr": "00" * 16,
        "visibility": visibility,
        "owner_user_id": owner,
    }
    if with_k_enclave:
        env["K_enclave"] = k
    env.update(overrides)
    return env


# ---------------------------------------------------------------------------
# Satisfy bootstrap-stage gate (added 2026-05-15)
#
# /v1/chat/response and /v1/identity/init 409 with `bootstrap_incomplete`
# until the user has written enough memories for the relationship-age floor
# AND initialized identity. The legacy linear test order (chat → identity)
# predates that gate, so we seed fresh memories + identity here before any
# chat-response assertion runs.
#
# Idempotent under --key mode: identity_init returns 409 already_initialized
# if it's been set before, which is fine — bootstrap is still satisfied.
# ---------------------------------------------------------------------------

def _seed_bootstrap_state():
    if not USER_ID:
        return
    occurred_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    # Post-2026-05-22 typed-memory model: bootstrap gate is per-tab.
    # Seed 1 moment (Story) + 2 facts (About me) at today to pass the
    # <2-days floor (story=1, about_me=1).
    seed_types = ["moment", "fact", "fact"]
    for i, mem_type in enumerate(seed_types):
        env = _seed_envelope(
            USER_ID,
            occurred_at=occurred_at,
            type=mem_type,
        )
        _orig_post(f"{BASE_URL}/v1/memory/add",
                   json={"envelope": env}, headers=AUTH_HEADERS, timeout=5)
    seed_id_env = _seed_envelope(USER_ID)
    _orig_post(f"{BASE_URL}/v1/identity/init",
               json={
                   "envelope": seed_id_env,
                   "days_with_user": 0,
                   "relationship_anchor_evidence": "test_api seeded earliest memory at current timestamp",
               },
               headers=AUTH_HEADERS, timeout=5)
    _seed_live_connection_state()


def _seed_live_connection_state():
    """Simulate the standard resident consumer acceptance path for legacy API tests."""
    if not USER_ID:
        return

    since = time.time()
    poll_rt = {}

    def poll_and_reply_verify():
        try:
            r = _orig_get(
                f"{BASE_URL}/v1/chat/poll?since={since}&timeout=10",
                headers=_raw_headers(CONSUMER_HEADERS),
                timeout=15,
            )
            poll_rt["poll_status"] = r.status_code
            if r.status_code != 200:
                poll_rt["err"] = r.text[:120]
                return
            if not r.json().get("messages", []):
                poll_rt["err"] = "no verify ping message"
                return
            verify_env = _seed_envelope(USER_ID, visibility="local_only", with_k_enclave=False)
            rr = _orig_post(
                f"{BASE_URL}/v1/chat/response",
                json={"envelope": verify_env},
                headers=_raw_headers(CONSUMER_HEADERS),
                timeout=5,
            )
            poll_rt["reply_status"] = rr.status_code
        except Exception as e:
            poll_rt["err"] = str(e)

    t = threading.Thread(target=poll_and_reply_verify, daemon=True)
    t.start()
    time.sleep(0.2)
    _orig_post(
        f"{BASE_URL}/v1/chat/verify_loop",
        json={"timeout_sec": 10},
        headers=AUTH_HEADERS,
        timeout=15,
    )
    t.join(timeout=12)


_seed_bootstrap_state()


# ---------------------------------------------------------------------------
# v1 envelope helper — dummy ciphertext; server never decrypts.
# ---------------------------------------------------------------------------

def make_envelope(owner: str, *, visibility: str = "shared",
                  with_k_enclave: bool = True, **overrides) -> dict:
    body = base64.b64encode(uuid.uuid4().bytes * 4).decode()
    nonce = base64.b64encode(uuid.uuid4().bytes[:12] * 2).decode()
    k = base64.b64encode(uuid.uuid4().bytes * 3).decode()
    env = {
        "v": 1,
        "body_ct": body,
        "nonce": nonce,
        "K_user": k,
        "enclave_pk_fpr": "00" * 16,
        "visibility": visibility,
        "owner_user_id": owner,
    }
    if with_k_enclave:
        env["K_enclave"] = k
    env.update(overrides)
    return env


# ---------------------------------------------------------------------------
# 1. Health / read endpoints
# ---------------------------------------------------------------------------

section("1. Read endpoints")

r = requests.get(f"{BASE_URL}/v1/screen/analyze", timeout=5)
check("GET /v1/screen/analyze returns 200", r.status_code == 200)
body = r.json()
check("analyze has 'active' field", "active" in body)
check("analyze has 'rate_limit_ok' field", "rate_limit_ok" in body)

r = requests.get(f"{BASE_URL}/v1/screen/frames", timeout=5)
check("GET /v1/screen/frames returns 200", r.status_code == 200)
check("frames response has 'frames' list", "frames" in r.json())

r = requests.get(f"{BASE_URL}/v1/push/tokens", timeout=5)
check("GET /v1/push/tokens returns 200", r.status_code == 200)
check("tokens response has 'tokens' list", "tokens" in r.json())

# ---------------------------------------------------------------------------
# 2. Chat — plaintext rejected, v1 envelope accepted
# ---------------------------------------------------------------------------

section("2. Chat: plaintext rejected, v1 envelope stored")

r = requests.post(f"{BASE_URL}/v1/chat/message",
                  json={"content": "plaintext should fail now"}, timeout=5)
check("POST plaintext /v1/chat/message → 400", r.status_code == 400,
      f"got {r.status_code}: {r.text[:120]}")

env = make_envelope(USER_ID)
r = requests.post(f"{BASE_URL}/v1/chat/message", json={"envelope": env}, timeout=5)
check("POST v1 envelope /v1/chat/message → 200", r.status_code == 200)
msg_id = r.json().get("id") if r.status_code == 200 else None
msg_ts = r.json().get("ts") if r.status_code == 200 else None
check("envelope response has id/ts/v=1",
      bool(msg_id) and bool(msg_ts) and (r.json().get("v") if r.status_code == 200 else None) == 1)

r = requests.get(f"{BASE_URL}/v1/chat/history?limit=10", timeout=5)
check("GET /v1/chat/history returns 200", r.status_code == 200)
msgs = r.json().get("messages", [])
check("history contains our envelope message",
      any(m.get("id") == msg_id and m.get("body_ct") == env["body_ct"] for m in msgs))

# history ?since filter still works
if msg_ts is not None:
    r = requests.get(f"{BASE_URL}/v1/chat/history?since={msg_ts + 1}", timeout=5)
    check("?since filters out older messages",
          not any(m.get("id") == msg_id for m in r.json().get("messages", [])))

r = requests.get(f"{BASE_URL}/v1/chat/history?limit=abc", timeout=5)
check("invalid limit returns 400", r.status_code == 400)

r = requests.get(f"{BASE_URL}/v1/chat/history?since=abc", timeout=5)
check("invalid since returns 400", r.status_code == 400)

# ---------------------------------------------------------------------------
# 3. Chat — openclaw response (v1 envelope only)
# ---------------------------------------------------------------------------

section("3. Chat: openclaw response via v1 envelope")

r = requests.post(f"{BASE_URL}/v1/chat/response",
                  json={"content": "plaintext reply should fail"}, timeout=5)
check("POST plaintext /v1/chat/response → 400", r.status_code == 400)

reply_env = make_envelope(USER_ID)
# Reply WITH reply_to_message_id, like a real consumer: it marks the section-2
# user message replied. Leaving it unanswered would trip the lost-turn
# redelivery backstop and make section 4's timeout poll return it instantly.
r = requests.post(f"{BASE_URL}/v1/chat/response",
                  json={"envelope": reply_env, "reply_to_message_id": msg_id}, timeout=5)
check("POST v1 envelope /v1/chat/response → 200", r.status_code == 200)
reply_id = r.json().get("id") if r.status_code == 200 else None
check("envelope response has id", bool(reply_id))

r = requests.get(f"{BASE_URL}/v1/chat/history?limit=20", timeout=5)
msgs = r.json().get("messages", [])
oc = [m for m in msgs if m.get("role") == "openclaw"]
check("openclaw envelope reply appears in history",
      any(m.get("id") == reply_id and m.get("body_ct") == reply_env["body_ct"] for m in oc))

r = requests.post(f"{BASE_URL}/v1/chat/response", json={}, timeout=5)
check("empty body returns 400", r.status_code == 400)

# ---------------------------------------------------------------------------
# 4. Long-poll — timeout case
# ---------------------------------------------------------------------------

section("4. Long-poll: timeout")

recent_ts = time.time()
t0 = time.time()
r = requests.get(f"{BASE_URL}/v1/chat/poll?since={recent_ts}&timeout=2", timeout=10)
elapsed = time.time() - t0
check("poll returns 200", r.status_code == 200, f"got {r.status_code}: {r.text[:80]}")
if r.status_code == 200:
    body = r.json()
    check("timed_out is true when no message", body.get("timed_out") is True)
    check("messages is empty on timeout", body.get("messages") == [])
    check("timeout respected (~2s)", 1.5 <= elapsed <= 4.0, f"elapsed={elapsed:.2f}s")
else:
    _failures += ["timed_out is true when no message", "messages is empty on timeout", "timeout respected (~2s)"]

# ---------------------------------------------------------------------------
# 5. Long-poll — wakes when user posts v1 envelope
# ---------------------------------------------------------------------------

section("5. Long-poll: wakes when user sends v1 envelope")

poll_result = {}
poll_error = {}
poll_since = time.time()


def do_poll():
    try:
        r = requests.get(f"{BASE_URL}/v1/chat/poll?since={poll_since}&timeout=10", timeout=15)
        poll_result["status"] = r.status_code
        if r.status_code == 200:
            poll_result["body"] = r.json()
        else:
            poll_error["err"] = f"HTTP {r.status_code}: {r.text[:80]}"
    except Exception as e:
        poll_error["err"] = str(e)


t = threading.Thread(target=do_poll, daemon=True)
t.start()

time.sleep(0.3)
wake_env = make_envelope(USER_ID)
wake_r = requests.post(f"{BASE_URL}/v1/chat/message", json={"envelope": wake_env}, timeout=5)
wake_id = wake_r.json().get("id") if wake_r.status_code == 200 else None

t.join(timeout=8)

check("poll thread completed", not t.is_alive(), "poll hung")
if poll_error:
    check("no poll error", False, poll_error.get("err"))
else:
    check("poll returned 200", poll_result.get("status") == 200)
    body = poll_result.get("body", {})
    check("timed_out is false", body.get("timed_out") is False)
    msgs = body.get("messages", [])
    check("wake envelope is in poll response",
          any(m.get("id") == wake_id for m in msgs))

# Answer the wake message so it doesn't linger unanswered: the redelivery
# backstop would hand it straight to section 6's poll, short-circuiting the
# wake it means to test.
if wake_id:
    requests.post(f"{BASE_URL}/v1/chat/response",
                  json={"envelope": make_envelope(USER_ID), "reply_to_message_id": wake_id},
                  timeout=5)

# ---------------------------------------------------------------------------
# 6. Full round-trip: user envelope → poll wakes → openclaw envelope → history
# ---------------------------------------------------------------------------

section("6. Full round-trip (envelopes)")

round_ts = time.time()
user_env = make_envelope(USER_ID)
oc_env = make_envelope(USER_ID)

poll_rt = {}


def poll_and_reply():
    try:
        r = requests.get(f"{BASE_URL}/v1/chat/poll?since={round_ts}&timeout=10", timeout=15)
        poll_rt["poll_status"] = r.status_code
        if r.status_code == 200:
            poll_rt["poll_body"] = r.json()
        else:
            poll_rt["err"] = f"HTTP {r.status_code}: {r.text[:80]}"
            return
        # Reply to the polled message like a real consumer (marks it replied,
        # keeping the transcript free of unanswered turns the redelivery
        # backstop would resurrect in later polls).
        polled = (poll_rt["poll_body"].get("messages") or [{}])[0].get("id")
        rr = requests.post(f"{BASE_URL}/v1/chat/response",
                           json={"envelope": oc_env, "reply_to_message_id": polled}, timeout=5)
        poll_rt["reply_status"] = rr.status_code
        if rr.status_code == 200:
            poll_rt["reply_id"] = rr.json().get("id")
    except Exception as e:
        poll_rt["err"] = str(e)


t = threading.Thread(target=poll_and_reply, daemon=True)
t.start()

time.sleep(0.3)
user_r = requests.post(f"{BASE_URL}/v1/chat/message", json={"envelope": user_env}, timeout=5)
user_id_rt = user_r.json().get("id") if user_r.status_code == 200 else None
t.join(timeout=10)

check("round-trip poll completed", not t.is_alive())
if "err" not in poll_rt:
    check("poll woke up with user envelope",
          poll_rt.get("poll_body", {}).get("timed_out") is False)
    check("openclaw envelope reply posted", poll_rt.get("reply_status") == 200)

    r = requests.get(f"{BASE_URL}/v1/chat/history?since={round_ts}&limit=20", timeout=5)
    all_msgs = r.json().get("messages", [])
    user_present = any(m.get("id") == user_id_rt and m.get("role") == "user" for m in all_msgs)
    oc_present = any(m.get("id") == poll_rt.get("reply_id") and m.get("role") == "openclaw" for m in all_msgs)
    check("user envelope visible in history", user_present)
    check("openclaw envelope reply visible in history", oc_present)
else:
    check("no round-trip error", False, poll_rt["err"])

# ---------------------------------------------------------------------------
# 7. Bootstrap
# ---------------------------------------------------------------------------

section("7. Bootstrap")

r = requests.post(f"{BASE_URL}/v1/bootstrap", timeout=5)
check("POST /v1/bootstrap returns 200", r.status_code == 200)
if r.status_code == 200:
    body = r.json()
    check("bootstrap has 'status' field", "status" in body)
    check("status is 'first_time' or 'already_bootstrapped'",
          body.get("status") in ("first_time", "already_bootstrapped"))
    if body.get("status") == "first_time":
        check("first_time response has 'instructions'", "instructions" in body)

# ---------------------------------------------------------------------------
# 8. Identity card
# ---------------------------------------------------------------------------

section("8. Identity card: plaintext rejected, get works")

# plaintext → 400 (post-v0 strip)
plaintext_identity = {
    "agent_name": "TestAgent",
    "self_introduction": "plaintext should fail",
    "dimensions": [{"name": "x", "value": 50, "description": "y"}],
}
r = requests.post(f"{BASE_URL}/v1/identity/init", json=plaintext_identity, timeout=5)
# Server returns 400 for a missing envelope *only if* no identity exists yet; a
# freshly-registered user hits the 400 branch. For --key mode where identity
# might already exist, 409 is also valid.
check("plaintext /v1/identity/init → 400 or 409",
      r.status_code in (400, 409), f"got {r.status_code}: {r.text[:120]}")

# v1 envelope init (only if none exists yet). days_with_user is mandatory
# at init — it sets the server-side relationship anchor.
env_id = make_envelope(USER_ID)
r = requests.post(f"{BASE_URL}/v1/identity/init",
                  json={
                      "envelope": env_id,
                      "days_with_user": 0,
                      "relationship_anchor_evidence": "test_api identity init evidence",
                  }, timeout=5)
check("v1 envelope /v1/identity/init → 201 or 409",
      r.status_code in (201, 409), f"got {r.status_code}: {r.text[:120]}")

# missing days_with_user must 400
r = requests.post(f"{BASE_URL}/v1/identity/init",
                  json={"envelope": env_id}, timeout=5)
check("/v1/identity/init without days_with_user → 400 or 409",
      r.status_code in (400, 409), f"got {r.status_code}: {r.text[:120]}")

r = requests.get(f"{BASE_URL}/v1/identity/get", timeout=5)
check("GET /v1/identity/get returns 200 (or 404 if none)",
      r.status_code in (200, 404))
if r.status_code == 200:
    body = r.json()
    check("identity response has 'identity' key", "identity" in body)
    if "identity" in body:
        identity = body["identity"]
        check("v1 identity has body_ct",
              identity.get("v") == 1 and bool(identity.get("body_ct")))
        # The relationship anchor must be present and the live days_with_user
        # must be injected at the top level of the response.
        check("identity has relationship_started_at anchor",
              bool(identity.get("relationship_started_at")))
        check("identity response has live days_with_user",
              "days_with_user" in identity and isinstance(identity["days_with_user"], int))

# ---------------------------------------------------------------------------
# 9. Memory garden: plaintext rejected, v1 envelope add/list/get/delete
# ---------------------------------------------------------------------------

section("9. Memory garden: v1 envelope round-trip")

# plaintext add → 400
r = requests.post(
    f"{BASE_URL}/v1/memory/add",
    json={"title": "plaintext", "description": "should fail",
          "occurred_at": "2025-01-01T12:00:00", "type": "测试", "source": "bootstrap"},
    timeout=5,
)
check("plaintext /v1/memory/add → 400", r.status_code == 400,
      f"got {r.status_code}: {r.text[:120]}")

# v1 envelope add — needs plaintext `occurred_at` alongside the envelope for
# server-side ordering; the backend reads it off the envelope dict (see
# app.py:memory_add).
mem_id = f"mom_{uuid.uuid4().hex[:12]}"
env_mem = make_envelope(
    USER_ID,
    id=mem_id,
    occurred_at="2025-01-01T12:00:00",
    source="bootstrap",
    type="moment",  # mandatory plaintext metadata post-2026-05-22
)
r = requests.post(f"{BASE_URL}/v1/memory/add", json={"envelope": env_mem}, timeout=5)
check("POST v1 envelope /v1/memory/add → 201", r.status_code == 201,
      f"got {r.status_code}: {r.text[:120]}")
if r.status_code == 201:
    mem = r.json().get("moment", {})
    check("add response has moment.id == our id", mem.get("id") == mem_id)
    check("add response preserves body_ct", mem.get("body_ct") == env_mem["body_ct"])

# list
r = requests.get(f"{BASE_URL}/v1/memory/list?limit=20", timeout=5)
check("GET /v1/memory/list returns 200", r.status_code == 200)
if r.status_code == 200:
    moments = r.json().get("moments", [])
    check("our envelope moment appears in list",
          any(m.get("id") == mem_id for m in moments))

# get
r = requests.get(f"{BASE_URL}/v1/memory/get?id={mem_id}", timeout=5)
check("GET /v1/memory/get returns 200", r.status_code == 200)
if r.status_code == 200:
    check("get preserves body_ct",
          r.json().get("moment", {}).get("body_ct") == env_mem["body_ct"])

# delete
r = requests.delete(f"{BASE_URL}/v1/memory/delete?id={mem_id}", timeout=5)
check("DELETE /v1/memory/delete returns 200", r.status_code == 200)

r = requests.get(f"{BASE_URL}/v1/memory/get?id={mem_id}", timeout=5)
check("get after delete returns 404", r.status_code == 404)

# owner_user_id mismatch → 403
env_wrong_owner = make_envelope(
    "usr_somebody_else",
    occurred_at="2025-01-01T12:00:00",
)
r = requests.post(f"{BASE_URL}/v1/memory/add", json={"envelope": env_wrong_owner}, timeout=5)
check("memory envelope with wrong owner_user_id → 403",
      r.status_code == 403, f"got {r.status_code}")

# ---------------------------------------------------------------------------
# 10. Multi-tenant: isolation + 401 enforcement (envelopes)
# ---------------------------------------------------------------------------

if MULTI_TENANT:
    section("10. Multi-tenant: v1 isolation + 401 enforcement")

    rr2 = _orig_post(f"{BASE_URL}/v1/users/register", json={}, timeout=5)
    check("second register returns 201", rr2.status_code == 201)
    if rr2.status_code == 201:
        user2 = rr2.json()
        key2 = user2["api_key"]
        uid2 = user2["user_id"]

        # user2 sends a v1 envelope with a distinctive body_ct
        user2_env = make_envelope(uid2)
        r = _orig_post(f"{BASE_URL}/v1/chat/message",
                       json={"envelope": user2_env},
                       headers={"X-API-Key": key2}, timeout=5)
        check("user2 send envelope 200", r.status_code == 200)

        # user1 must not see user2's ciphertext
        r = _orig_get(f"{BASE_URL}/v1/chat/history?limit=50",
                      headers={"X-API-Key": SHARED_KEY}, timeout=5)
        msgs = r.json().get("messages", [])
        check("user1 does NOT see user2's envelope",
              not any(m.get("body_ct") == user2_env["body_ct"] for m in msgs))

        # user2 does see their own
        r = _orig_get(f"{BASE_URL}/v1/chat/history?limit=50",
                      headers={"X-API-Key": key2}, timeout=5)
        msgs = r.json().get("messages", [])
        check("user2 sees their own envelope",
              any(m.get("body_ct") == user2_env["body_ct"] for m in msgs))

    # No key at all → 401
    r = _orig_get(f"{BASE_URL}/v1/screen/analyze", timeout=5)
    check("no-auth → 401", r.status_code == 401, f"got {r.status_code}")

    # Bogus key → 401
    r = _orig_get(f"{BASE_URL}/v1/screen/analyze",
                  headers={"X-API-Key": "nope"}, timeout=5)
    check("bogus key → 401", r.status_code == 401, f"got {r.status_code}")

    # ?key= query param works
    r = _orig_get(f"{BASE_URL}/v1/screen/analyze?key={SHARED_KEY}", timeout=5)
    check("?key= query param works", r.status_code == 200)

    # Bearer header works
    r = _orig_get(f"{BASE_URL}/v1/screen/analyze",
                  headers={"Authorization": f"Bearer {SHARED_KEY}"}, timeout=5)
    check("Authorization: Bearer works", r.status_code == 200)

    # whoami returns our user_id
    r = _orig_get(f"{BASE_URL}/v1/users/whoami",
                  headers={"X-API-Key": SHARED_KEY}, timeout=5)
    check("whoami returns 200", r.status_code == 200)
    if r.status_code == 200:
        check("whoami user_id matches", r.json().get("user_id", "").startswith("usr_"))


# ---------------------------------------------------------------------------
# 11. v1 envelope validation (shape errors)
# ---------------------------------------------------------------------------

section("11. v1 envelope validation")

# local_only — K_enclave optional
env_lo = make_envelope(USER_ID, visibility="local_only", with_k_enclave=False)
r = requests.post(f"{BASE_URL}/v1/chat/message", json={"envelope": env_lo}, timeout=5)
check("local_only envelope without K_enclave → 200", r.status_code == 200)

# shared without K_enclave → 400
env_bad = make_envelope(USER_ID, with_k_enclave=False)
r = requests.post(f"{BASE_URL}/v1/chat/message", json={"envelope": env_bad}, timeout=5)
check("shared without K_enclave → 400", r.status_code == 400)

# missing required field → 400
env_missing = make_envelope(USER_ID)
env_missing.pop("body_ct")
r = requests.post(f"{BASE_URL}/v1/chat/message", json={"envelope": env_missing}, timeout=5)
check("envelope missing body_ct → 400", r.status_code == 400)

# invalid visibility → 400
env_badvis = make_envelope(USER_ID)
env_badvis["visibility"] = "public"
r = requests.post(f"{BASE_URL}/v1/chat/message", json={"envelope": env_badvis}, timeout=5)
check("envelope with bad visibility → 400", r.status_code == 400)

# empty body → 400
r = requests.post(f"{BASE_URL}/v1/chat/message", json={}, timeout=5)
check("no envelope → 400", r.status_code == 400)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print(f"\n{'═' * 50}")
if _failures:
    print(f"  {FAIL} {len(_failures)} test(s) failed:")
    for f in _failures:
        print(f"     • {f}")
else:
    print(f"  {PASS} All tests passed")
print(f"{'═' * 50}\n")

sys.exit(1 if _failures else 0)
