"""Native ASGI parity for the remaining accounts routes (plan §9.4 / §5.3).

Covers the nine routes migrated in this slice — access modes / link-token /
claim-token, users/register, account recover challenge+verify, users/preferences,
onboarding/route — asserting for each:

  * body + status parity vs the Flask oracle (both delegate to
    ``accounts.accounts_core``), on the deterministic paths;
  * the EXACT per-route auth: access-modes / onboarding / preferences require an
    authenticated user (fixed-body 401 like Flask's ``auth.require_user()``),
    while register / recover(challenge+verify) / claim-token are PUBLIC /
    PRE-AUTH and must succeed with NO credential on both backends;
  * the register orphan-lineage backstop (a re-register of a known content
    public key is a 409, never a second minted account) holds on the ASGI path;
  * validation error bodies match Flask byte-for-byte.

accounts.routes_asgi is already in asgi_app._ASGI_PACKAGES, so the routes are
live on the shared app without any manual register here.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import sys
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import asgi_app  # noqa: E402
from accounts import recover as accounts_recover  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def clean(tmp_path, monkeypatch):
    """Fresh, isolated registry/store state per test (shared by both backends)."""
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    accounts_recover._recover_challenges.clear()
    registry._save_users()
    return tmp_path


@pytest.fixture()
def user(clean):
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


# --------------------------------------------------------------------------- #
# transport helpers
# --------------------------------------------------------------------------- #


def _asgi(method: str, path: str, json_body=None, headers: dict | None = None):
    async def go():
        transport = httpx.ASGITransport(app=asgi_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.request(method, path, json=json_body, headers=headers or {})
            try:
                return resp.status_code, resp.json()
            except Exception:
                return resp.status_code, None

    return asyncio.run(go())


def _asgi_get(path, headers=None):
    return _asgi("GET", path, None, headers)


def _asgi_post(path, json_body=None, headers=None):
    return _asgi("POST", path, json_body, headers)


def _flask(method: str, path: str, json_body=None, headers: dict | None = None):
    c = make_client()
    res = c.open(path, method=method, json=json_body, headers=headers or {})
    return res.status_code, res.get_json()


def _flask_get(path, headers=None):
    return _flask("GET", path, None, headers)


def _flask_post(path, json_body=None, headers=None):
    return _flask("POST", path, json_body, headers)


def _norm_modes(body: dict) -> dict:
    """Blank the volatile per-binding check-in timestamps (see whoami test)."""
    out = dict(body)
    modes = []
    for m in out.get("access_modes", []) or []:
        m = dict(m)
        m["updated_at"] = "<ts>"
        m["last_seen_at"] = "<ts>"
        modes.append(m)
    if modes:
        out["access_modes"] = modes
    return out


# --------------------------------------------------------------------------- #
# GET /v1/access/modes — requires auth; idempotent read → body parity
# --------------------------------------------------------------------------- #


def test_access_modes_parity(user):
    _uid, api_key = user
    h = {"X-API-Key": api_key}
    f_status, f_body = _flask_get("/v1/access/modes", h)
    a_status, a_body = _asgi_get("/v1/access/modes", h)
    assert f_status == a_status == 200
    assert _norm_modes(a_body) == _norm_modes(f_body)


def test_access_modes_requires_auth(user):
    f_status, _ = _flask_get("/v1/access/modes")
    a_status, a_body = _asgi_get("/v1/access/modes")
    assert f_status == a_status == 401
    assert a_body == {"error": "unauthorized"}
    assert _asgi_get("/v1/access/modes", {"X-API-Key": "nope"}) == (401, {"error": "unauthorized"})


# --------------------------------------------------------------------------- #
# POST /v1/access/modes/switch — requires auth; idempotent on same mode
# --------------------------------------------------------------------------- #


def test_access_modes_switch_parity(user):
    _uid, api_key = user
    h = {"X-API-Key": api_key}
    f_status, f_body = _flask_post("/v1/access/modes/switch", {"access_mode": "model_api"}, h)
    a_status, a_body = _asgi_post("/v1/access/modes/switch", {"access_mode": "model_api"}, h)
    assert f_status == a_status == 200
    assert a_body["active_route"] == "model_api"
    assert _norm_modes(a_body) == _norm_modes(f_body)


def test_access_modes_switch_invalid_mode_parity(user):
    _uid, api_key = user
    h = {"X-API-Key": api_key}
    f = _flask_post("/v1/access/modes/switch", {"access_mode": "bogus"}, h)
    a = _asgi_post("/v1/access/modes/switch", {"access_mode": "bogus"}, h)
    assert f == a
    assert a[0] == 400


def test_access_modes_switch_requires_auth(user):
    assert _asgi_post("/v1/access/modes/switch", {"access_mode": "model_api"}) == (
        401, {"error": "unauthorized"})


# --------------------------------------------------------------------------- #
# POST /v1/access/link-token (auth) + POST /v1/access/claim-token (PUBLIC)
# --------------------------------------------------------------------------- #


def test_link_token_create_shape_parity(user):
    _uid, api_key = user
    h = {"X-API-Key": api_key}
    f_status, f_body = _flask_post("/v1/access/link-token", {"access_mode": "model_api"}, h)
    a_status, a_body = _asgi_post("/v1/access/link-token", {"access_mode": "model_api"}, h)
    assert f_status == a_status == 201

    def _fixed(b):
        return {k: b[k] for k in ("access_mode", "route", "label",
                                  "expires_in_seconds", "claim_endpoint")}

    assert _fixed(a_body) == _fixed(f_body)
    # The random parts still have the right shape.
    assert a_body["token"].startswith("flt_")
    assert a_body["token_id"].startswith("flt_")


def test_link_token_requires_auth(user):
    assert _asgi_post("/v1/access/link-token", {"access_mode": "model_api"}) == (
        401, {"error": "unauthorized"})


def test_claim_token_full_flow_is_public(user):
    """Create a link token (authed), then claim it over ASGI with NO auth — the
    claim is a bearer flow, not user-authed. The issued api_key must work."""
    _uid, api_key = user
    created = _asgi_post("/v1/access/link-token", {"access_mode": "model_api"},
                         {"X-API-Key": api_key})
    assert created[0] == 201
    raw_token = created[1]["token"]

    claim_status, claim_body = _asgi_post("/v1/access/claim-token", {"token": raw_token})
    assert claim_status == 201, claim_body
    assert claim_body["status"] == "connected"
    assert claim_body["access_mode"] == "model_api"
    new_key = claim_body["api_key"]
    assert new_key

    # The claimed key authenticates as the same account.
    who = _asgi_get("/v1/access/modes", {"X-API-Key": new_key})
    assert who[0] == 200
    assert who[1]["user_id"] == claim_body["user_id"]


def test_claim_token_missing_token_parity(clean):
    # No auth header on either backend — a public route that validates its body.
    f = _flask_post("/v1/access/claim-token", {})
    a = _asgi_post("/v1/access/claim-token", {})
    assert f == a == (400, {"error": "token required"})


def test_claim_token_invalid_token_parity(clean):
    f = _flask_post("/v1/access/claim-token", {"token": "flt_nope"})
    a = _asgi_post("/v1/access/claim-token", {"token": "flt_nope"})
    assert f == a == (404, {"error": "invalid_token"})


def test_claim_token_single_use(user):
    _uid, api_key = user
    created = _asgi_post("/v1/access/link-token", {"access_mode": "model_api"},
                         {"X-API-Key": api_key})
    raw_token = created[1]["token"]
    first = _asgi_post("/v1/access/claim-token", {"token": raw_token})
    assert first[0] == 201
    second = _asgi_post("/v1/access/claim-token", {"token": raw_token})
    assert second == (409, {"error": "token_already_used"})


# --------------------------------------------------------------------------- #
# POST /v1/users/register — PUBLIC (no auth) + orphan-lineage backstop
# --------------------------------------------------------------------------- #


def test_register_is_public_and_creates_account(clean):
    """Register must succeed over ASGI with NO auth (there is no user yet)."""
    status, body = _asgi_post("/v1/users/register",
                              {"public_key": _b64(b"\x22" * 32), "archive_language": "en"})
    assert status == 201
    assert body["user_id"] and body["api_key"]
    assert len(registry._users) == 1


def test_register_duplicate_pubkey_is_409_no_orphan_parity(clean):
    """Orphan-lineage backstop preserved on ASGI: a re-register of a known content
    public key is a 409 (recover instead), never a second minted account."""
    pub = _b64(b"\x33" * 32)
    first = _asgi_post("/v1/users/register", {"public_key": pub})
    assert first[0] == 201
    assert len(registry._users) == 1

    # Second register of the same pubkey -> 409, and NO orphan account minted.
    second = _asgi_post("/v1/users/register", {"public_key": pub})
    assert second[0] == 409
    assert second[1]["error"] == "account_exists_for_key"
    assert second[1]["recover_endpoint"] == "/v1/account/recover/challenge"
    assert len(registry._users) == 1  # backstop held

    # The 409 body matches the Flask oracle byte-for-byte.
    f = _flask_post("/v1/users/register", {"public_key": pub})
    assert f == second


def test_register_no_pubkey_allowed_over_asgi(clean):
    # Legacy clients without a public_key can't be deduped — must still register.
    one = _asgi_post("/v1/users/register", {"archive_language": "en"})
    two = _asgi_post("/v1/users/register", {"archive_language": "en"})
    assert one[0] == two[0] == 201
    assert len(registry._users) == 2


# --------------------------------------------------------------------------- #
# POST /v1/account/recover/{challenge,verify} — PRE-AUTH proof-of-possession
# --------------------------------------------------------------------------- #


def _new_keypair():
    priv = X25519PrivateKey.generate()
    pub_bytes = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    return priv, pub_bytes, _b64(pub_bytes)


def _solve_challenge(env: dict, priv, pub_bytes: bytes) -> str:
    """Decrypt a local_only envelope (mirror of content_encryption.box_seal)."""
    k_user = base64.b64decode(env["K_user"])
    ek_pub, sealed = k_user[:32], k_user[32:]
    shared = priv.exchange(X25519PublicKey.from_public_bytes(ek_pub))
    k_wrap = HKDF(algorithm=SHA256(), length=32, salt=None,
                  info=b"feedling-box-seal-v1").derive(shared)
    seal_nonce = hashlib.sha256(ek_pub + pub_bytes).digest()[:12]
    K = ChaCha20Poly1305(k_wrap).decrypt(seal_nonce, sealed, None)
    aad = f'{env["owner_user_id"]}|{env["v"]}|{env["id"]}'.encode("utf-8")
    body = ChaCha20Poly1305(K).decrypt(
        base64.b64decode(env["nonce"]), base64.b64decode(env["body_ct"]), aad)
    return body.decode("utf-8")


def test_recover_full_flow_public_over_asgi(clean):
    """The whole challenge->verify recovery runs over ASGI with NO auth and
    issues a working api_key for the EXISTING account (no new user)."""
    priv, pub_bytes, pub_b64 = _new_keypair()
    reg = _asgi_post("/v1/users/register", {"public_key": pub_b64, "archive_language": "en"})
    assert reg[0] == 201
    user_id = reg[1]["user_id"]

    ch_status, ch = _asgi_post("/v1/account/recover/challenge", {"public_key": pub_b64})
    assert ch_status == 200, ch
    answer = _solve_challenge(ch["envelope"], priv, pub_bytes)

    vr_status, vr = _asgi_post("/v1/account/recover/verify",
                               {"challenge_id": ch["challenge_id"], "answer": answer})
    assert vr_status == 200, vr
    assert vr["user_id"] == user_id
    recovered_key = vr["api_key"]
    assert recovered_key

    # No new account minted; the recovered key authenticates as the same account.
    assert len(registry._users) == 1
    who = _asgi_get("/v1/access/modes", {"X-API-Key": recovered_key})
    assert who[0] == 200 and who[1]["user_id"] == user_id


def test_recover_challenge_unknown_pubkey_parity(clean):
    _, _, pub_b64 = _new_keypair()  # never registered
    f = _flask_post("/v1/account/recover/challenge", {"public_key": pub_b64})
    a = _asgi_post("/v1/account/recover/challenge", {"public_key": pub_b64})
    assert f == a == (404, {"error": "no_recoverable_account"})


def test_recover_challenge_bad_pubkey_parity(clean):
    f = _flask_post("/v1/account/recover/challenge", {"public_key": "!!not-base64!!"})
    a = _asgi_post("/v1/account/recover/challenge", {"public_key": "!!not-base64!!"})
    assert f[0] == a[0] == 400
    assert f[1] == a[1]  # same decode-error body


def test_recover_verify_bad_challenge_parity(clean):
    f = _flask_post("/v1/account/recover/verify", {"challenge_id": "rec_x", "answer": "y"})
    a = _asgi_post("/v1/account/recover/verify", {"challenge_id": "rec_x", "answer": "y"})
    assert f == a == (401, {"error": "invalid_or_expired_challenge"})


def test_recover_verify_wrong_answer(clean):
    priv, pub_bytes, pub_b64 = _new_keypair()
    _asgi_post("/v1/users/register", {"public_key": pub_b64})
    ch_status, ch = _asgi_post("/v1/account/recover/challenge", {"public_key": pub_b64})
    assert ch_status == 200
    vr = _asgi_post("/v1/account/recover/verify",
                    {"challenge_id": ch["challenge_id"], "answer": "wrong-answer"})
    assert vr == (401, {"error": "challenge_failed"})


# --------------------------------------------------------------------------- #
# POST /v1/users/preferences — requires auth
# --------------------------------------------------------------------------- #


def test_preferences_set_language_and_timezone_parity(user):
    _uid, api_key = user
    h = {"X-API-Key": api_key}
    payload = {"archive_language": "zh-Hans", "timezone": "Asia/Shanghai"}
    f_status, f_body = _flask_post("/v1/users/preferences", payload, h)
    a_status, a_body = _asgi_post("/v1/users/preferences", payload, h)
    assert f_status == a_status == 200
    assert a_body == f_body == {
        "status": "updated", "archive_language": "zh-Hans", "timezone": "Asia/Shanghai"}


def test_preferences_empty_body_parity(user):
    _uid, api_key = user
    h = {"X-API-Key": api_key}
    f = _flask_post("/v1/users/preferences", {}, h)
    a = _asgi_post("/v1/users/preferences", {}, h)
    assert f == a == (400, {"error": "provide archive_language and/or timezone (string or null)"})


def test_preferences_bad_timezone_parity(user):
    _uid, api_key = user
    h = {"X-API-Key": api_key}
    payload = {"timezone": "Not/AZone"}
    f = _flask_post("/v1/users/preferences", payload, h)
    a = _asgi_post("/v1/users/preferences", payload, h)
    assert f == a == (400, {"error": "timezone must be a valid IANA zone or null"})


def test_preferences_requires_auth(user):
    assert _asgi_post("/v1/users/preferences", {"archive_language": "en"}) == (
        401, {"error": "unauthorized"})


# --------------------------------------------------------------------------- #
# GET/POST /v1/onboarding/route — requires auth
# --------------------------------------------------------------------------- #


def test_onboarding_route_get_parity(user):
    _uid, api_key = user
    h = {"X-API-Key": api_key}
    f = _flask_get("/v1/onboarding/route", h)
    a = _asgi_get("/v1/onboarding/route", h)
    assert f == a
    assert a[0] == 200 and "route" in a[1] and "allowed" in a[1]


def test_onboarding_route_post_parity(user):
    _uid, api_key = user
    h = {"X-API-Key": api_key}
    f = _flask_post("/v1/onboarding/route", {"route": "model_api"}, h)
    a = _asgi_post("/v1/onboarding/route", {"route": "model_api"}, h)
    # selected_at is util._now_iso() — a fresh timestamp per call; blank it.
    assert a[0] == f[0] == 200
    assert a[1]["route"] == f[1]["route"] == "model_api"
    assert set(a[1]) == set(f[1]) == {"route", "selected_at"}


def test_onboarding_route_post_invalid_parity(user):
    _uid, api_key = user
    h = {"X-API-Key": api_key}
    f = _flask_post("/v1/onboarding/route", {"route": "bogus"}, h)
    a = _asgi_post("/v1/onboarding/route", {"route": "bogus"}, h)
    assert f == a
    assert a[0] == 400


def test_onboarding_route_requires_auth(user):
    assert _asgi_get("/v1/onboarding/route") == (401, {"error": "unauthorized"})
    assert _asgi_post("/v1/onboarding/route", {"route": "model_api"}) == (
        401, {"error": "unauthorized"})
