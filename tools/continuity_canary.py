#!/usr/bin/env python3
"""§4 day-0 decrypt continuity canary.

The §3 deploy canary only proves NEW writes round-trip. THIS incident's real
question was "can the enclave still open OLD envelopes?" — which needs an old
envelope that never changes. So: take the permanent canary user's OLDEST stored
chat envelope (written once, long ago) and prove the live enclave still decrypts
it. The moment ANY old data becomes undecryptable, this goes red the same day,
and its green during a "decrypt failed" report instantly reframes triage to
"client-side".

Auth uses a runtime token (HMAC over user_id) minted from the shared
FEEDLING_RUNTIME_TOKEN_SECRET the enclave already holds — no long-lived api key
to store. Read side is the DB (the envelope, verbatim); decrypt is the enclave.

Exit 0 = old data still decrypts. 1 = decrypt failed (a real key/continuity
event). 2 = setup incomplete (canary user has no stored shared envelope yet).

Config (env):
  CANARY_USER_ID               permanent synthetic user (usr_…)
  DATABASE_URL                 prod/test Postgres (reads the stored envelope)
  FEEDLING_RUNTIME_TOKEN_SECRET  shared HMAC secret (mints the auth token)
  FEEDLING_ENCLAVE_URL         enclave base url (in-enclave TLS → no verify)
  FEEDLING_CANARY_RETRIES      decrypt retries (default 4)
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from core import runtime_token as rt  # noqa: E402

USER_ID = os.environ.get("CANARY_USER_ID", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
SECRET = os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET", "").encode("utf-8")
ENCLAVE_URL = os.environ.get("FEEDLING_ENCLAVE_URL", "http://127.0.0.1:5003").rstrip("/")
RETRIES = int(os.environ.get("FEEDLING_CANARY_RETRIES", "4"))

_INSECURE = ssl._create_unverified_context()


def _fail(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    print(f"CONTINUITY CANARY {'SETUP' if code == 2 else 'FAIL'}: {msg}", file=sys.stderr)
    sys.exit(code)


def _oldest_shared_envelope() -> dict | None:
    """The canary user's oldest chat envelope that carries K_enclave (i.e. is
    enclave-openable). Returns the reconstructed envelope, or None if none."""
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=15) as conn:
            row = conn.execute(
                "SELECT doc FROM chat_messages "
                "WHERE user_id = %s AND doc->>'K_enclave' IS NOT NULL "
                "ORDER BY seq ASC LIMIT 1",
                (USER_ID,),
            ).fetchone()
    except psycopg.Error as e:
        _fail(f"database unreachable/error: {str(e).splitlines()[0][:200]}", 1)
    if row is None:
        return None
    doc = row[0]
    return {
        "v": doc.get("v", 1),
        "id": doc["id"],
        "owner_user_id": doc.get("owner_user_id", USER_ID),
        "visibility": doc.get("visibility", "shared"),
        "body_ct": doc["body_ct"],
        "nonce": doc["nonce"],
        "K_user": doc["K_user"],
        "K_enclave": doc["K_enclave"],
    }


def _decrypt(envelope: dict, token: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{ENCLAVE_URL}/v1/envelope/decrypt",
        data=json.dumps({"envelope": envelope}).encode(),
        headers={"Content-Type": "application/json", "X-Feedling-Runtime-Token": token},
        method="POST",
    )
    ctx = _INSECURE if ENCLAVE_URL.startswith("https") else None
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"error": raw.decode("utf-8", "replace")[:300]}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {"error": f"transport: {getattr(e, 'reason', e)}"}


def main() -> None:
    if not USER_ID:
        _fail("CANARY_USER_ID unset — set the repo var to the permanent canary user", 2)
    if not DATABASE_URL or not SECRET:
        _fail("DATABASE_URL and FEEDLING_RUNTIME_TOKEN_SECRET are required", 1)

    print(f"[continuity] user={USER_ID} enclave={ENCLAVE_URL}")
    envelope = _oldest_shared_envelope()
    if envelope is None:
        _fail(f"{USER_ID} has no stored shared (K_enclave) chat envelope yet — "
              f"write ONE chat message through the normal path so this canary has "
              f"an aging envelope to verify forever", 2)
    print(f"[continuity] oldest envelope id={envelope['id']} v={envelope['v']}")

    # Runtime token: user-scoped, short TTL (this run only). The enclave verifies
    # it locally via the shared secret and resolves owner_user_id from it.
    token = rt.mint(SECRET, user_id=USER_ID, runtime_instance_id="continuity-canary",
                    scope=["chat"], ttl=300.0)

    last = ""
    for attempt in range(1, RETRIES + 1):
        st, body = _decrypt(envelope, token)
        if st == 200 and body.get("plaintext_b64"):
            print(f"[continuity] enclave decrypted the aging envelope ✓ (attempt {attempt})")
            print("CONTINUITY CANARY OK")
            return
        last = f"{st}: {body.get('error')}"
        print(f"[continuity] decrypt attempt {attempt}/{RETRIES} -> {last}")
        if attempt < RETRIES:
            time.sleep(2 ** attempt)
    # A 403 decrypt_failed here is THE alarm: old data no longer opens.
    _fail(f"enclave could not decrypt the aging envelope after {RETRIES} tries; last={last}", 1)


if __name__ == "__main__":
    main()
