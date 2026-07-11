"""Regression: a hosted (host-all) turn authenticated with a RUNTIME TOKEN must
be able to decrypt the user's Model API provider-key envelope.

This closes the follow-on gap explicitly deferred in
``tests/test_runtime_token_auth.py`` ("routes that forward the user's API key to
the enclave (content decrypt) still need the enclave side to accept runtime
tokens — out of scope here"). The enclave's ``/v1/envelope/decrypt`` already
accepts a runtime token, but ``model_api_chat_send`` only ever extracted the
api_key (``None`` under runtime-token auth) and never forwarded the token, so the
provider-key unwrap failed with ``model_api_key_decrypt_failed`` and the turn
returned 400. In prod (``host_all=true``) this left hosted users unable to chat
despite a valid, tested provider key.
"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
import provider_client  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import runtime_token  # noqa: E402
from core import store as core_store  # noqa: E402
from hosted import agent_runtime_cutover  # noqa: E402

_SECRET = "test-runtime-secret"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    monkeypatch.setattr(
        core_enclave,
        "_get_enclave_info",
        lambda: {"content_pk_hex": ("22" * 32), "compose_hash": "test"},
    )
    # A live supervisor heartbeat so the send wedge guard lets the turn route.
    db.set_supervisor_heartbeat({"ts": time.time(), "owner": "test",
                                 "host_all": True, "gateway": True})
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    with make_client() as c:
        yield c


def _register(client) -> tuple[str, str]:
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _mint(user_id: str) -> str:
    return runtime_token.mint(
        _SECRET.encode("utf-8"),
        user_id=user_id,
        runtime_instance_id="ri_test",
        scope=["chat", "memory", "identity"],
    )


def test_runtime_token_turn_decrypts_provider_key_and_routes(client, monkeypatch):
    user_id, api_key = _register(client)

    # 1) Configure + test the provider with the user's real api_key (works today).
    monkeypatch.setattr(
        provider_client, "test_provider_key",
        lambda cfg: {"reply": "ok", "usage": {"total_tokens": 1}},
    )
    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "gemini", "model": "gemini-2.5-flash", "api_key": "AQ.fake-gemini-key"},
        headers={"X-API-Key": api_key},
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    # 2) The enclave decrypt only yields the provider key when a runtime token is
    #    forwarded — mirrors prod where the hosted turn carries no api_key.
    forwarded: dict = {}

    def fake_decrypt(envelope, api_key, *, purpose, runtime_token=""):
        forwarded["runtime_token"] = runtime_token
        forwarded["api_key"] = api_key
        if not runtime_token:
            raise RuntimeError("api_key_unavailable")
        return b"AQ.fake-gemini-key"

    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", fake_decrypt)

    # 3) Stub the downstream routing so the test isolates the decrypt/auth wiring.
    monkeypatch.setattr(agent_runtime_cutover, "check_supervisor_live",
                        lambda **kw: (True, "ok"))
    monkeypatch.setattr(agent_runtime_cutover, "handle_send",
                        lambda store, user_row, driver, **kw: ({"status": "processing"}, 202))

    # 4) Send the turn authenticated with ONLY the runtime token (no X-API-Key).
    tok = _mint(user_id)
    chat = client.post(
        "/v1/model_api/chat/send",
        json={"message": "你好"},
        headers={"X-Feedling-Runtime-Token": tok},
    )

    assert chat.status_code == 202, chat.get_data(as_text=True)
    assert chat.get_json()["status"] == "processing"
    # The route must have forwarded the runtime token (api_key is absent here).
    assert forwarded.get("runtime_token") == tok
    assert not forwarded.get("api_key")
