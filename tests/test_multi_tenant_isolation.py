"""
Multi-tenant data-isolation test suite.

Written after the 2026-05-11 P0 incident where MCP server's peer-IP-based
session-key fallback caused user A's tool calls to authenticate as user B,
resulting in cross-tenant identity / memory / chat writes. The Explore-agent
+ manual audit that followed found two more latent bugs (`time` not imported
in mcp_server.py, identity_init / identity_replace not checking
envelope.owner_user_id against authenticated user). This file exists so that
class of bug can be CAUGHT BY CI before it reaches users again.

What this exercises:

1. N=8 users register concurrently — every register must mint a unique
   user_id + api_key (no collisions, no race-induced overwrite).

2. Each user runs a full real-shaped flow in its own thread, concurrently
   with every other user:
     register → identity_init → memory_add x5 → chat_message (text) x5 →
     chat_message (image content_type) x3 → identity_replace → reads back.
   Each step's envelope body_ct embeds the user_id verbatim so we can
   detect cross-tenant contamination by string match alone.

3. After the concurrent phase, every user reads back THEIR OWN history
   via four different auth paths (X-API-Key header, Authorization: Bearer,
   ?key= query, X-API-Key with case variation) and asserts:
     - identity_get returns the envelope they wrote (and ONLY that one)
     - memory_list returns exactly 5 moments, all owned by them
     - chat_history returns the 8 messages they sent (and zero owned
       by anyone else)
     - whoami returns their own user_id (not a stranger's)

4. Cross-tenant probes: user A tries to use user B's api_key. Verify the
   response is user B's data (correct routing) and NOT user A's; then
   user A goes back to their own key and verifies they still see only
   their own data (no key-rotation poisoning of the in-memory cache).

5. Spot-checks for the bugs we already fixed:
     - identity_init with envelope.owner_user_id != caller → 403
     - identity_replace with envelope.owner_user_id != caller → 403
     - memory_add with envelope.owner_user_id != caller → 403

6. A small registration-race stress: 50 parallel /v1/users/register calls,
   all of them must succeed AND all returned user_ids must be distinct.

Run:
    pytest tests/test_multi_tenant_isolation.py -v

The fixture spawns a fresh Flask backend in a subprocess against a temp
data dir, so the test is hermetic — it does NOT hit prod or the deployed
CVM. CI uses the same pattern; see .github/workflows/ci.yml python-tests.
"""

from __future__ import annotations

import base64
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest
import requests

# ---------------------------------------------------------------------------
# Test config
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"

N_USERS = 8                  # concurrent users in full-flow test
REGISTER_RACE_N = 50         # concurrent register stress
TIMEOUT = 8                  # per-request HTTP timeout


# ---------------------------------------------------------------------------
# Fixture — spawn fresh Flask backend on a free port, against a temp dir
# ---------------------------------------------------------------------------

def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def backend():
    """Start a fresh Flask backend on a random local port with an isolated
    data dir. Tear it down at the end of the module. Each test module thus
    gets a clean slate; no leakage from prior test runs."""
    port = _pick_free_port()
    ws_port = _pick_free_port()
    tmp_data = tempfile.mkdtemp(prefix="feedling-isolation-test-")
    env = {
        **os.environ,
        "FEEDLING_DATA_DIR": tmp_data,
        "FEEDLING_WS_PORT": str(ws_port),
        # Force the Flask app to bind the picked port. The app reads
        # FEEDLING_PORT in some branches; passing both is harmless.
        "FEEDLING_PORT": str(port),
        "PORT": str(port),
    }

    log_path = Path(tmp_data) / "backend.log"
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-u", str(BACKEND_DIR / "serve_dev.py")],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=str(BACKEND_DIR),
    )

    base_url = f"http://127.0.0.1:{port}"
    # Wait for /healthz
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            r = requests.get(f"{base_url}/healthz", timeout=1)
            if r.status_code == 200:
                break
        except Exception:
            pass
        if proc.poll() is not None:
            log.close()
            raise RuntimeError(f"backend died early; log:\n{log_path.read_text()}")
        time.sleep(0.2)
    else:
        proc.kill()
        log.close()
        raise RuntimeError(f"backend never came up; log:\n{log_path.read_text()}")

    yield {"base_url": base_url, "data_dir": tmp_data}

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _stub_envelope(owner_uid: str, marker: str, content_type: str = "text",
                   visibility: str = "shared") -> dict:
    """Build a v1-shaped envelope with stub ciphertext. body_ct embeds the
    user_id + marker verbatim so cross-tenant contamination is detectable
    by simple string match in test assertions."""
    payload = f"{owner_uid}|{marker}".encode("utf-8")
    env = {
        "v": 1,
        "id": uuid.uuid4().hex,
        "body_ct": _b64(payload),
        "nonce": _b64(b"\x00" * 12),
        "K_user": _b64(b"\x00" * 32),
        "visibility": visibility,
        "owner_user_id": owner_uid,
    }
    if visibility == "shared":
        env["K_enclave"] = _b64(b"\x00" * 32)
    if content_type != "text":
        env["content_type"] = content_type
    return env


def _register(base_url: str) -> tuple[str, str]:
    r = requests.post(f"{base_url}/v1/users/register", json={}, timeout=TIMEOUT)
    assert r.status_code == 201, f"register failed: {r.status_code} {r.text}"
    body = r.json()
    return body["user_id"], body["api_key"]


def _user_flow(base_url: str, slot_idx: int) -> dict:
    """Full-flow exercise for one user. Runs in its own thread. Returns
    the dict the test asserts against — caller checks isolation across
    every user's result simultaneously.

    Order matches the skill protocol: memories are written FIRST (Pass 1-3),
    then identity (Step 5 derivation), then chat (Step 6+). The backend
    enforces this with `bootstrap_incomplete` 409s on identity_init /
    chat_response if prerequisites aren't satisfied — see
    tests/test_bootstrap_gates.py for the gate behavior, and
    backend/app.py `_gate_bootstrap_for_*`.
    """
    user_id, api_key = _register(base_url)
    H = {"X-API-Key": api_key}

    # --- memories FIRST (bootstrap gate requires per-tab floors satisfied) ---
    # occurred_at=today → relationship_age=0 → tier <2 days → story=1, about_me=1
    # floor. Write 1 moment (Story) + 4 facts (About me) so we have 5 cards
    # total (preserves the multi-tenant isolation surface) while passing the
    # per-tab gate. type is now mandatory plaintext metadata on every write
    # (post-2026-05-22 typed-memory rewrite).
    memory_markers = []
    today_iso = datetime.now().isoformat()
    seed_types = ["moment", "fact", "fact", "fact", "fact"]
    for i, mem_type in enumerate(seed_types):
        m_marker = f"memory-{slot_idx}-{i}"
        m_env = _stub_envelope(user_id, m_marker)
        m_env["occurred_at"] = today_iso
        m_env["type"] = mem_type
        r = requests.post(
            f"{base_url}/v1/memory/add",
            json={"envelope": m_env},
            headers=H, timeout=TIMEOUT,
        )
        assert r.status_code in (200, 201), f"memory_add failed: {r.status_code} {r.text}"
        memory_markers.append(m_marker)

    # --- identity init (now allowed; memory floor satisfied) ---
    iv_marker = f"identity-{slot_idx}"
    iv_env = _stub_envelope(user_id, iv_marker)
    r = requests.post(
        f"{base_url}/v1/identity/init",
        json={
            "envelope": iv_env,
            "days_with_user": 0,
            "relationship_anchor_evidence": f"tenant-test-anchor-{slot_idx}",
        },
        headers=H, timeout=TIMEOUT,
    )
    assert r.status_code == 201, f"identity_init failed for {user_id}: {r.text}"

    # --- 5 text chat messages + 3 image messages ---
    chat_markers = []
    for i in range(5):
        c_marker = f"chat-text-{slot_idx}-{i}"
        c_env = _stub_envelope(user_id, c_marker, content_type="text")
        r = requests.post(
            f"{base_url}/v1/chat/message",
            json={"envelope": c_env},
            headers=H, timeout=TIMEOUT,
        )
        assert r.status_code == 200, f"chat_message text failed: {r.text}"
        chat_markers.append(c_marker)
    for i in range(3):
        c_marker = f"chat-image-{slot_idx}-{i}"
        c_env = _stub_envelope(user_id, c_marker, content_type="image")
        r = requests.post(
            f"{base_url}/v1/chat/message",
            json={"envelope": c_env},
            headers=H, timeout=TIMEOUT,
        )
        assert r.status_code == 200, f"chat_message image failed: {r.text}"
        chat_markers.append(c_marker)

    # --- identity_replace ---
    iv2_marker = f"identity-replaced-{slot_idx}"
    iv2_env = _stub_envelope(user_id, iv2_marker)
    r = requests.post(
        f"{base_url}/v1/identity/replace",
        json={"envelope": iv2_env},
        headers=H, timeout=TIMEOUT,
    )
    assert r.status_code == 200, f"identity_replace failed: {r.text}"

    return {
        "slot_idx": slot_idx,
        "user_id": user_id,
        "api_key": api_key,
        "identity_marker_final": iv2_marker,
        "memory_markers": memory_markers,
        "chat_markers": chat_markers,
    }


def _decode_body_ct(b64_str: str) -> str:
    try:
        return base64.b64decode(b64_str).decode("utf-8", errors="replace")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_concurrent_full_flow_per_user(backend):
    """Drive N users through a full flow concurrently and verify, at the
    end, that every user sees ONLY their own data. Any cross-tenant write
    or read shows up as a marker for slot X appearing in slot Y's results.
    """
    base_url = backend["base_url"]

    # Phase 1 — concurrent full flow.
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=N_USERS) as ex:
        futures = [ex.submit(_user_flow, base_url, i) for i in range(N_USERS)]
        for f in as_completed(futures):
            results.append(f.result())

    # Sanity: N distinct user_ids and api_keys.
    user_ids = [r["user_id"] for r in results]
    api_keys = [r["api_key"] for r in results]
    assert len(set(user_ids)) == N_USERS, f"user_ids not unique: {user_ids}"
    assert len(set(api_keys)) == N_USERS, "api_keys not unique"

    # Phase 2 — every user reads back via 4 different auth methods and
    # asserts isolation.
    for me in results:
        my_uid = me["user_id"]
        my_key = me["api_key"]
        my_markers = set(me["memory_markers"]) | set(me["chat_markers"]) | {me["identity_marker_final"]}
        other_markers = set()
        for other in results:
            if other["user_id"] == my_uid:
                continue
            other_markers |= set(other["memory_markers"]) | set(other["chat_markers"]) | {other["identity_marker_final"]}

        for auth_idx, auth_headers in enumerate([
            {"X-API-Key": my_key},
            {"Authorization": f"Bearer {my_key}"},
            {"x-api-key": my_key},  # case variation
            None,  # ?key= variant
        ]):
            params = {"key": my_key} if auth_headers is None else None
            url = f"{base_url}/v1/chat/history?limit=200"

            r = requests.get(url, headers=auth_headers or {}, params=params, timeout=TIMEOUT)
            assert r.status_code == 200, f"history failed for {my_uid} auth_idx={auth_idx}: {r.text}"
            msgs = r.json().get("messages", [])

            seen = {_decode_body_ct(m.get("body_ct", "")).split("|", 1)[-1] for m in msgs}
            # Must see every marker we wrote
            for ck in me["chat_markers"]:
                assert ck in seen, f"{my_uid} missing own chat marker {ck} via auth {auth_idx}"
            # Must NOT see anything from other users
            leaked = other_markers & seen
            assert not leaked, (
                f"DATA LEAK detected: user {my_uid} (auth_idx={auth_idx}) "
                f"saw markers belonging to other users: {leaked}"
            )

        # identity_get — must return the LATEST envelope (the replace),
        # never an init envelope from another user
        r = requests.get(
            f"{base_url}/v1/identity/get",
            headers={"X-API-Key": my_key}, timeout=TIMEOUT,
        )
        assert r.status_code == 200
        env = r.json().get("identity") or {}
        body = _decode_body_ct(env.get("body_ct", ""))
        assert me["identity_marker_final"] in body, (
            f"{my_uid} identity_get returned wrong envelope: body={body!r}"
        )
        for other in results:
            if other["user_id"] == my_uid:
                continue
            assert other["identity_marker_final"] not in body, (
                f"{my_uid} identity_get leaked {other['user_id']}'s envelope"
            )

        # memory_list — must return exactly 5 memories, all our markers
        r = requests.get(
            f"{base_url}/v1/memory/list?limit=200",
            headers={"X-API-Key": my_key}, timeout=TIMEOUT,
        )
        assert r.status_code == 200
        moments = r.json().get("moments", [])
        # Exactly the 5 we wrote
        assert len(moments) == 5, (
            f"{my_uid} memory_list returned {len(moments)} != 5"
        )
        body_markers = {_decode_body_ct(m.get("body_ct", "")).split("|", 1)[-1] for m in moments}
        assert body_markers == set(me["memory_markers"]), (
            f"{my_uid} memory_list returned wrong markers: "
            f"got={body_markers} expected={set(me['memory_markers'])}"
        )

        # whoami — must return MY user_id
        r = requests.get(
            f"{base_url}/v1/users/whoami",
            headers={"X-API-Key": my_key}, timeout=TIMEOUT,
        )
        assert r.status_code == 200
        assert r.json().get("user_id") == my_uid, (
            f"whoami returned wrong user_id for {my_uid}: {r.json()}"
        )


def test_cross_tenant_key_swap_returns_correct_users_data(backend):
    """User A uses user B's key — server must respond with user B's data
    (correct routing). Then user A switches back to their own key and
    must again see ONLY their own data (no in-memory cache poisoning)."""
    base_url = backend["base_url"]

    a_uid, a_key = _register(base_url)
    b_uid, b_key = _register(base_url)

    # A writes a marker only A should see.
    a_marker = "isolation-probe-A"
    r = requests.post(
        f"{base_url}/v1/chat/message",
        json={"envelope": _stub_envelope(a_uid, a_marker)},
        headers={"X-API-Key": a_key}, timeout=TIMEOUT,
    )
    assert r.status_code == 200

    # B writes a marker only B should see.
    b_marker = "isolation-probe-B"
    r = requests.post(
        f"{base_url}/v1/chat/message",
        json={"envelope": _stub_envelope(b_uid, b_marker)},
        headers={"X-API-Key": b_key}, timeout=TIMEOUT,
    )
    assert r.status_code == 200

    # A uses B's key: server returns B's data (correct routing).
    r = requests.get(
        f"{base_url}/v1/chat/history?limit=50",
        headers={"X-API-Key": b_key}, timeout=TIMEOUT,
    )
    msgs = r.json().get("messages", [])
    bodies = {_decode_body_ct(m.get("body_ct", "")) for m in msgs}
    assert any(b_marker in body for body in bodies), "B's marker missing when querying with B's key"
    assert not any(a_marker in body for body in bodies), "A's marker leaked when querying with B's key"

    # A goes back to their own key: must NOT see B's marker.
    r = requests.get(
        f"{base_url}/v1/chat/history?limit=50",
        headers={"X-API-Key": a_key}, timeout=TIMEOUT,
    )
    msgs = r.json().get("messages", [])
    bodies = {_decode_body_ct(m.get("body_ct", "")) for m in msgs}
    assert any(a_marker in body for body in bodies), "A's marker missing under own key"
    assert not any(b_marker in body for body in bodies), "B's marker visible under A's key — cache poisoning?"


def test_envelope_owner_user_id_mismatch_rejected(backend):
    """All v1 envelope-accepting endpoints must reject envelopes whose
    owner_user_id ≠ authenticated caller. Catches the latent
    defense-in-depth bug we fixed alongside the time-import bug.

    Order note: P1 added a bootstrap-stage gate (identity_init 409s
    until memory_count ≥ floor). The owner_user_id check runs AFTER
    that gate, so this test seeds A's memories first to reach the
    owner-check code path.
    """
    base_url = backend["base_url"]
    a_uid, a_key = _register(base_url)
    b_uid, _ = _register(base_url)

    # Seed A's memories so identity_init reaches the owner check, not the
    # bootstrap gate. occurred_at = today puts the user in the <2-days
    # tier (Story=1 / About me=1 floor); seed 1 moment + 2 facts. type is
    # now mandatory plaintext metadata.
    today_iso = datetime.now().isoformat()
    seed_types = ["moment", "fact", "fact"]
    for i, mem_type in enumerate(seed_types):
        m_env = _stub_envelope(a_uid, f"a-mem-{i}")
        m_env["occurred_at"] = today_iso
        m_env["type"] = mem_type
        r = requests.post(
            f"{base_url}/v1/memory/add",
            json={"envelope": m_env},
            headers={"X-API-Key": a_key}, timeout=TIMEOUT,
        )
        assert r.status_code in (200, 201), f"seed memory_add failed: {r.text}"

    # identity_init with B's owner_user_id while authenticated as A → 403
    bad_env = _stub_envelope(b_uid, "bad-init")  # claims owner=B
    r = requests.post(
        f"{base_url}/v1/identity/init",
        json={"envelope": bad_env, "days_with_user": 1},
        headers={"X-API-Key": a_key}, timeout=TIMEOUT,
    )
    assert r.status_code == 403, (
        f"identity_init accepted mismatched owner_user_id (regression): {r.status_code} {r.text}"
    )

    # Now legitimately init A's identity so identity_replace has something to act on.
    good_env = _stub_envelope(a_uid, "good-init")
    r = requests.post(
        f"{base_url}/v1/identity/init",
        json={
            "envelope": good_env,
            "days_with_user": 0,
            "relationship_anchor_evidence": "owner-mismatch-test-anchor",
        },
        headers={"X-API-Key": a_key}, timeout=TIMEOUT,
    )
    assert r.status_code == 201

    # identity_replace with B's owner_user_id → 403
    bad_env = _stub_envelope(b_uid, "bad-replace")
    r = requests.post(
        f"{base_url}/v1/identity/replace",
        json={"envelope": bad_env},
        headers={"X-API-Key": a_key}, timeout=TIMEOUT,
    )
    assert r.status_code == 403, (
        f"identity_replace accepted mismatched owner_user_id (regression): {r.status_code} {r.text}"
    )

    # memory_add with B's owner_user_id → 403 (this one already existed).
    # Set type so the request gets past the type-required check and reaches
    # the owner-mismatch check (which is the one this test is exercising).
    bad_env = _stub_envelope(b_uid, "bad-memory")
    bad_env["occurred_at"] = "2026-05-01T00:00:00"
    bad_env["type"] = "fact"
    r = requests.post(
        f"{base_url}/v1/memory/add",
        json={"envelope": bad_env},
        headers={"X-API-Key": a_key}, timeout=TIMEOUT,
    )
    assert r.status_code == 403, (
        f"memory_add accepted mismatched owner_user_id: {r.status_code} {r.text}"
    )


def test_no_auth_rejected_on_protected_endpoints(backend):
    """Defence-in-depth: any request without a key gets 401, never
    silently routed to a default tenant. Catches a class of bug where
    a future code path forgets `require_user()` and falls through to
    a global state."""
    base_url = backend["base_url"]

    for path, method in [
        ("/v1/chat/history", "GET"),
        ("/v1/chat/message", "POST"),
        ("/v1/identity/get", "GET"),
        ("/v1/identity/init", "POST"),
        ("/v1/memory/list", "GET"),
        ("/v1/memory/add", "POST"),
        ("/v1/users/whoami", "GET"),
        ("/v1/bootstrap/status", "GET"),
    ]:
        r = (requests.get if method == "GET" else requests.post)(
            f"{base_url}{path}", json={}, timeout=TIMEOUT
        )
        assert r.status_code == 401, f"{method} {path} no-auth → expected 401, got {r.status_code}"


def test_register_race_no_duplicates(backend):
    """Fire 50 parallel /v1/users/register and assert every user_id is
    unique. Catches a hypothetical lock-skip in `_register_user` that
    would let two concurrent calls overwrite each other in `_users`."""
    base_url = backend["base_url"]

    def reg_one(_idx):
        r = requests.post(f"{base_url}/v1/users/register", json={}, timeout=TIMEOUT)
        assert r.status_code == 201, r.text
        return r.json()

    with ThreadPoolExecutor(max_workers=REGISTER_RACE_N) as ex:
        results = list(ex.map(reg_one, range(REGISTER_RACE_N)))

    user_ids = [r["user_id"] for r in results]
    api_keys = [r["api_key"] for r in results]
    assert len(set(user_ids)) == REGISTER_RACE_N, (
        f"register race produced duplicate user_ids: "
        f"{len(set(user_ids))} distinct out of {REGISTER_RACE_N}"
    )
    assert len(set(api_keys)) == REGISTER_RACE_N, "register race produced duplicate api_keys"


def test_concurrent_writes_dont_corrupt_others(backend):
    """While 4 users are simultaneously hammering chat_message, each
    concurrent reader must see only their own messages. Catches a class
    of race where, e.g., a shared file write or in-memory list mutation
    causes a temporary cross-tenant view."""
    base_url = backend["base_url"]

    users = [_register(base_url) for _ in range(4)]

    def writer(uid_key, n_msgs):
        uid, key = uid_key
        markers = []
        for i in range(n_msgs):
            mk = f"concurrent-{uid}-{i}"
            r = requests.post(
                f"{base_url}/v1/chat/message",
                json={"envelope": _stub_envelope(uid, mk)},
                headers={"X-API-Key": key}, timeout=TIMEOUT,
            )
            assert r.status_code == 200
            markers.append(mk)
        return uid, markers

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda uk: writer(uk, 15), users))

    # Each user reads their own and asserts they don't see anyone else's.
    own = {uid: set(markers) for uid, markers in results}
    for uid, key in users:
        r = requests.get(
            f"{base_url}/v1/chat/history?limit=200",
            headers={"X-API-Key": key}, timeout=TIMEOUT,
        )
        bodies = {_decode_body_ct(m.get("body_ct", "")) for m in r.json().get("messages", [])}
        # Every marker WE wrote must appear
        for m in own[uid]:
            assert any(m in b for b in bodies), f"{uid} missing own marker {m}"
        # No marker from any other user may appear
        for other_uid, other_markers in own.items():
            if other_uid == uid:
                continue
            for m in other_markers:
                assert not any(m in b for b in bodies), (
                    f"{uid} leaked {other_uid}'s marker {m}"
                )
