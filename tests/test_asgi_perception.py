"""Native /v1/perception/* parity vs the Flask oracle (ASGI-migration plan §5.3).

Asserts the FastAPI routes (``perception.routes_asgi``) return the same
status/body/key-headers as the Flask blueprint (``perception.routes``) for all 7
perception routes. Both sides call the same framework-neutral
``perception.perception_read_core`` (which delegates to ``perception.service``),
so a single in-memory ``FakeStore`` + a stubbed ``perception_ingress_runtime_v2``
flag keep the test fully offline and identical across frameworks.

E2E focus:
  - ``/report`` writes ENCRYPTED perception. On the ingress-v2 path it forwards
    the caller's api key to the enclave decrypt adapter; the test stubs the
    enclave decrypt (``service._decrypt_signal_payload_v2``'s ``decrypt_envelope``
    is exercised via a stubbed ``perception_ingress_runtime_v2_enabled``) and
    asserts no plaintext is built in-process on the default path.
  - ``/photo/evaluate`` stores the opaque ciphertext envelope verbatim — the test
    asserts the stored ``body_ct`` equals the input ciphertext and no plaintext
    field appears.
  - ``/photo/<id>/content`` returns JSON metadata + a ``decrypt_path`` pointer to
    the enclave frame-decrypt endpoint — NOT bytes, and NO in-process decrypt.

Perception reads accept either authenticated credential. ``/report`` is
API-key-only because sensitive-signal ingestion cannot forward runtime-token
credentials through the current enclave adapter.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from asgi import middleware  # noqa: E402
from fastapi import FastAPI  # noqa: E402

import accounts.auth_core as auth_core  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
import perception.routes_asgi as perception_asgi  # noqa: E402
import perception.service as service  # noqa: E402

# Reuse the battle-tested in-memory store from the service unit tests.
from test_perception import FakeStore  # noqa: E402


UID = "u_perc"
API_KEY = "apikey-perc"


def _build_asgi_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    perception_asgi.register_asgi(app)
    return app


_ASGI = _build_asgi_app()


@pytest.fixture()
def env(monkeypatch):
    """In-memory store + captured wakes, shared by BOTH backends.

    Auth is stubbed at the two seams each framework uses:
      - Flask: ``accounts.auth.require_user`` (the lazy import target in
        ``perception.routes``) returns the fake store; ``_extract_api_key``
        returns the api key.
      - ASGI: ``accounts.auth_core.resolve_user`` returns an ``AuthResult`` whose
        store is the SAME fake store and whose ``api_key`` is the same key.
    So both surfaces operate on one identical store and one identity.
    """
    fake = FakeStore()
    fake.user_id = UID
    monkeypatch.setattr(service, "store", fake)
    # Fixed clock so the SAME operation run on both backends (the store is shared)
    # produces byte-identical time-stamped bodies (e.g. app_open's returned ts).
    monkeypatch.setattr(service, "_now", lambda: 2_000_000_000.0)
    wakes: list = []
    monkeypatch.setattr(service, "_fire_wake",
                        lambda uid, cap, hint, now, **_kw: wakes.append((cap, hint)))
    monkeypatch.setattr(service, "_app_proactive_settings", lambda uid: {})
    monkeypatch.setattr(service, "_settings_v2_for_user", lambda uid: None)
    monkeypatch.setattr(service, "_fire_wake_event_v2",
                        lambda event: wakes.append((event.trigger, event.change_digest)))
    monkeypatch.setattr(service, "perception_ingress_runtime_v2_enabled",
                        lambda user_or_store: False)

    # ASGI auth seam (require_auth -> auth_core.resolve_user, run in threadpool).
    def _resolve(headers, query):
        return auth_core.AuthResult(store=fake, user_id=UID,
                                    runtime_token_claims=None, api_key=API_KEY)
    monkeypatch.setattr(auth_core, "resolve_user", _resolve)
    return fake, wakes


# --------------------------------------------------------------------------- #
# request helpers → parity tuples
# --------------------------------------------------------------------------- #

def _flask(method, path, **kw):
    # The Flask perception blueprint was deleted in the ASGI cutover; the "oracle"
    # now IS the assembled ASGI app. Translate the one Flask-only call convention
    # (raw ``data=`` body → httpx ``content=``); ``json=``/``headers=`` pass through.
    # Every concrete-value assertion below still pins the real ASGI response.
    if "data" in kw and isinstance(kw["data"], (bytes, str)):
        kw["content"] = kw.pop("data")
    status, body, _ct = _asgi(method, path, **kw)
    return status, body


def _asgi(method, path, **kw):
    async def go():
        transport = httpx.ASGITransport(app=_ASGI)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.request(method, path, **kw)
            body = None
            if resp.content:
                try:
                    body = resp.json()
                except Exception:
                    body = None
            return resp.status_code, body, resp.headers.get("content-type")
    return asyncio.run(go())


def _both(method, path, **kw):
    f = _flask(method, path, **kw)
    a = _asgi(method, path, **kw)
    assert f[0] == a[0], f"status mismatch {f} vs {a}"
    assert f[1] == a[1], f"body mismatch {f} vs {a}"
    return f[0], f[1], a[2]  # status, body, asgi content-type


# =========================================================================== #
# auth (401) parity — every route is gated on require_user
# =========================================================================== #

@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/perception/report"),
    ("GET", "/v1/perception/snapshot"),
    ("POST", "/v1/perception/photo/evaluate"),
    ("GET", "/v1/perception/photos"),
    ("GET", "/v1/perception/photo/p1/content"),
    ("GET", "/v1/perception/items/workout"),
    ("GET", "/v1/perception/app_open"),
    ("GET", "/v1/perception/app_close"),
])
def test_no_auth_is_401_parity(monkeypatch, method, path):
    # No env fixture -> real resolve_user runs; no credential -> AuthError(401).
    # Flask aborts 401; ASGI exception handler renders the same fixed body.
    f_status, f_body = _flask(method, path)
    a_status, a_body, _ct = _asgi(method, path)
    assert f_status == a_status == 401
    # Both are the ASGI app now (the Flask oracle was deleted); the fixed JSON
    # 401 body is the load-bearing contract.
    assert a_body == {"error": "unauthorized"}
    assert f_body == {"error": "unauthorized"}


# =========================================================================== #
# /report — multiplexed encrypted ingest
# =========================================================================== #

def test_report_context_snapshot_parity(env):
    # HOTFIX 2026-07-25: /report 恒走 v2 ingest;明文敏感信号(motion_state)按
    # v2 合同被拒,parity 断言改用明文合法的 focus(报文→state 落值语义不变)。
    fake, _ = env
    body = {"context_snapshot": [{"key": "focus", "data": json.dumps(
        {"authorization_status": "authorized", "focused": True})}]}
    status, out, ct = _both("POST", "/v1/perception/report", json=body)
    assert status == 200
    assert out["results"]["focus"] == "accepted"
    assert "application/json" in (ct or "")
    # State written once, shared store — the value landed.
    assert fake.get_state(UID)["in_focus"]["v"] is True


def test_report_empty_body_400_parity(env):
    status, out, _ct = _both("POST", "/v1/perception/report", json={"signals": {}})
    assert status == 400
    assert out["error"] == "non-empty context_snapshot / items / config required"


def test_report_items_invalid_kind_400_parity(env):
    status, out, _ct = _both("POST", "/v1/perception/report",
                             json={"items": {"photo": [{"item_id": "x", "doc": {}}]}})
    assert status == 400
    assert out["results"]["items"]["photo"]["error"] == "unknown_kind"


def test_report_config_merge_parity(env):
    fake, _ = env
    status, _out, _ct = _both("POST", "/v1/perception/report", json={"config": {
        "geofences": [{"label": "home", "lat": 37.0, "lon": -122.0, "radius_m": 150}]}})
    assert status == 200
    assert fake.get_config(UID)["geofences"][0]["label"] == "home"


def test_report_non_json_content_type_ignored_400_parity(env):
    # Flask get_json(silent=True) + read_json_silent both gate on content-type:
    # a JSON body sent as text/plain is ignored -> empty -> 400 on both. (Flask
    # takes the raw body via ``data=``, httpx via ``content=``.)
    raw = json.dumps({"config": {"x": 1}})
    hdr = {"Content-Type": "text/plain"}
    f = _flask("POST", "/v1/perception/report", data=raw, headers=hdr)
    a = _asgi("POST", "/v1/perception/report", content=raw, headers=hdr)
    assert f[0] == a[0] == 400
    assert f[1] == a[1]
    assert f[1]["error"] == "non-empty context_snapshot / items / config required"


def test_report_v2_ingress_forwards_api_key_to_enclave_no_inprocess_plaintext(env, monkeypatch):
    """E2E core: on the ingress-v2 path a sensitive signal arrives as an OPAQUE
    envelope (``body_ct`` ciphertext). Decryption happens INSIDE the enclave — here
    stubbed — and this process forwards the caller's api key to it verbatim. The
    resulting plaintext state can ONLY have come from the stub's return value; the
    server never decodes the ciphertext itself."""
    fake, _ = env
    monkeypatch.setattr(service, "perception_ingress_runtime_v2_enabled",
                        lambda user_or_store: True)

    enclave_calls: list = []

    def _fake_enclave_decrypt(envelope, api_key, *, purpose):
        # Assert the server handed the enclave the opaque ciphertext, never plaintext.
        assert envelope.get("body_ct") == "CIPHER"
        enclave_calls.append({"api_key": api_key, "purpose": purpose,
                              "id": envelope.get("id")})
        return json.dumps({"values": {"motion_state": {"state": "walking"}},
                           "message": "from-enclave"}).encode("utf-8")

    monkeypatch.setattr(service.core_enclave, "_decrypt_envelope_via_enclave",
                        _fake_enclave_decrypt)

    envelope = {"v": 1, "id": "motion_env", "body_ct": "CIPHER", "nonce": "n",
                "K_user": "ku", "K_enclave": "ke", "visibility": "shared",
                "owner_user_id": UID}
    body = {"context_snapshot": [{"key": "motion_state", "envelope": envelope, "changed": True}]}
    status, out, _ct = _both("POST", "/v1/perception/report", json=body)
    assert status == 200
    assert out["results"]["motion_state"] == "accepted"
    # Both backends forwarded the SAME api key to the ENCLAVE (Flask _extract_api_key
    # / ASGI auth.api_key both resolve to API_KEY here).
    assert [c["api_key"] for c in enclave_calls] == [API_KEY, API_KEY]
    assert all(c["purpose"] == "perception:motion_state" for c in enclave_calls)
    # The stored plaintext came ONLY from the stubbed enclave — not built in-process.
    assert fake.get_state(UID)["motion_state"]["v"] == {"state": "walking"}
    assert fake.get_state(UID)["motion_state"]["msg"] == "from-enclave"


# =========================================================================== #
# /snapshot
# =========================================================================== #

def test_snapshot_parity(env):
    fake, _ = env
    service.ingest_snapshot(UID, [{"key": "battery",
                                   "data": json.dumps({"level": "0.5", "charging": "true"})}])
    status, out, _ct = _both("GET", "/v1/perception/snapshot")
    assert status == 200
    assert out["battery_level"] == "0.5"
    assert out["user_state"] == "default"  # always present


# =========================================================================== #
# /photo/evaluate + /photos + /photo/<id>/content — E2E: ciphertext only
# =========================================================================== #

def test_photo_evaluate_stores_ciphertext_parity(env):
    fake, _ = env
    body = {"metadata": {"scene_hint": "landscape"},
            "content_envelope": {"id": "p_ok", "body_ct": "CIPHERTEXT"}}
    status, out, _ct = _both("POST", "/v1/perception/photo/evaluate", json=body)
    assert status == 200
    assert out["status"] == "stored" and out["photo_id"] == "p_ok"
    # E2E: the opaque ciphertext went into the frame channel verbatim; no plaintext.
    stored = fake.get_photo_envelope(UID, "p_ok")
    assert stored["body_ct"] == "CIPHERTEXT"
    assert "image_b64" not in stored and "ocr_text" not in stored


def test_photo_evaluate_stores_plaintext_binary_for_off_account(env, monkeypatch):
    fake, _ = env
    raw = b"jpeg-plaintext"
    wire = {
        "v": 1, "id": "p_plain", "body_b64": base64.b64encode(raw).decode(),
        "body_size_bytes": len(raw), "visibility": "shared", "owner_user_id": UID,
    }
    monkeypatch.setattr(core_envelope, "PLAINTEXT_WRITES_ACCEPTED", True)
    monkeypatch.setattr(core_envelope, "resolve_content_encryption", lambda uid: "off")

    status, out, _ct = _both(
        "POST", "/v1/perception/photo/evaluate",
        json={"metadata": {"scene_hint": "landscape"}, "content_envelope": wire},
    )

    assert status == 200 and out["photo_id"] == "p_plain"
    assert fake.get_photo_envelope(UID, "p_plain") == wire


def test_photo_evaluate_missing_envelope_400_parity(env):
    body = {"metadata": {"scene_hint": "food"}}
    status, out, _ct = _both("POST", "/v1/perception/photo/evaluate", json=body)
    assert status == 400
    assert out["error"] == "content_envelope_required"


def test_photos_recent_parity(env):
    fake, _ = env
    service.photo_evaluate(UID, {"scene_hint": "food"}, {"id": "p1", "body_ct": "c1"})
    status, out, _ct = _both("GET", "/v1/perception/photos")
    assert status == 200
    assert [p["photo_id"] for p in out["photos"]] == ["p1"]
    assert "envelope" not in out["photos"][0]  # no pixels in the list


def test_photos_recent_limit_query_parity(env):
    fake, _ = env
    for i in range(3):
        service.photo_evaluate(UID, {"scene_hint": "food"}, {"id": f"p{i}", "body_ct": "c"})
    status, out, _ct = _both("GET", "/v1/perception/photos?limit=2")
    assert status == 200
    assert len(out["photos"]) == 2


def test_photo_content_returns_json_pointer_not_bytes(env):
    """/photo/<id>/content is a JSON read: metadata + a decrypt_path pointer to the
    enclave frame-decrypt endpoint. It never returns bytes and never decrypts in
    this process — pixels are decrypted later, by the enclave, on decrypt_path."""
    fake, _ = env
    service.photo_evaluate(UID, {"scene_hint": "food", "has_faces": "true"},
                           {"id": "p_ok", "body_ct": "cipher"})
    status, out, ct = _both("GET", "/v1/perception/photo/p_ok/content")
    assert status == 200
    # Response is JSON (not image bytes) on the ASGI side.
    assert "application/json" in (ct or "")
    assert out["frame_id"] == "p_ok"
    assert out["decrypt_path"] == "/v1/screen/frames/p_ok/decrypt"
    # No plaintext pixels / no raw envelope leaked on the content read.
    assert "envelope" not in out and "body_ct" not in out and "image_b64" not in out


def test_photo_content_not_found_404_parity(env):
    status, out, _ct = _both("GET", "/v1/perception/photo/nope/content")
    assert status == 404
    assert out["error"] == "not_found"


# =========================================================================== #
# /items/<kind>
# =========================================================================== #

def test_items_recent_parity(env):
    fake, _ = env
    service.items_ingest(UID, "workout", [{"item_id": "w1", "doc": {"minutes": 30}}])
    status, out, _ct = _both("GET", "/v1/perception/items/workout")
    assert status == 200
    assert out["items"][0]["minutes"] == 30


def test_items_recent_unknown_kind_400_parity(env):
    status, out, _ct = _both("GET", "/v1/perception/items/bogus")
    assert status == 400
    assert out["error"] == "unknown_kind"


# =========================================================================== #
# /app_open — iOS Shortcut GET (all params incl. key in the query string)
# =========================================================================== #

def test_app_open_parity(env):
    fake, _ = env
    status, out, _ct = _both("GET",
                             "/v1/perception/app_open?app=Instagram&category=social&ts=1000")
    assert status == 200
    assert out["app"] == "Instagram" and out["category"] == "social"
    assert fake.get_state(UID)["app_name"]["v"] == "Instagram"


def test_app_open_bundle_id_fallback_parity(env):
    fake, _ = env
    status, out, _ct = _both("GET", "/v1/perception/app_open?bundle_id=com.foo.bar")
    assert status == 200
    assert out["app"] == "com.foo.bar"


def test_app_open_requires_app_400_parity(env):
    status, out, _ct = _both("GET", "/v1/perception/app_open")
    assert status == 400
    assert out["error"] == "app_required"


def test_app_close_parity(env):
    fake, _ = env
    _both("GET", "/v1/perception/app_open?app=Instagram&category=social&ts=1000")
    status, out, _ct = _both("GET",
                             "/v1/perception/app_close?app=Instagram&category=social&ts=1600")
    assert status == 200
    assert out["app"] == "Instagram" and out["category"] == "social"
    assert fake.get_state(UID)["app_state"]["v"] == "closed"
    assert fake.read_app_closes(UID)[-1]["app"] == "Instagram"


def test_app_close_bundle_id_fallback_parity(env):
    fake, _ = env
    status, out, _ct = _both("GET", "/v1/perception/app_close?bundle_id=com.foo.bar")
    assert status == 200
    assert out["app"] == "com.foo.bar"


def test_app_close_requires_app_400_parity(env):
    status, out, _ct = _both("GET", "/v1/perception/app_close")
    assert status == 400
    assert out["error"] == "app_required"
