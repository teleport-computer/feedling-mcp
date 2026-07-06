"""Regression: /v1/memory/legacy_batch must thread the runtime token into the
per-card decrypt under HOST_ALL / token-only (X-API-Key dropped).

This exact pit was hit before (gate③ P0: persona decrypt under token-only). If the
route forgot to pass runtime_token, the migrator would silently decrypt nothing and
report "no legacy cards" for every token-only user. Nail it with a test.
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import store as core_store  # noqa: E402
from core import runtime_token  # noqa: E402
from memory import actions as memory_actions_mod  # noqa: E402
from memory import service as memory_service  # noqa: E402

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


def test_legacy_batch_threads_runtime_token_under_token_only(client, monkeypatch):
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    user_id, _api_key = _register(client)
    tok = runtime_token.mint(
        _SECRET.encode("utf-8"),
        user_id=user_id,
        runtime_instance_id="ri_test",
        scope=["memory"],
        ttl=900.0,
    )

    legacy_moment = {
        "id": "m1", "body_ct": "ct1", "nonce": "n", "K_user": "k", "K_enclave": "ke",
        "visibility": "shared", "owner_user_id": user_id, "status": "active",
        "occurred_at": "2020-01-01",
    }
    monkeypatch.setattr(memory_service, "_load_moments", lambda store: [dict(legacy_moment)])

    seen: dict = {}

    def _fake_plain(moment, api_key, runtime_token=""):
        seen["api_key"] = api_key
        seen["runtime_token"] = runtime_token
        return {"title": "去西湖", "description": "上周一起去了西湖"}, ""

    monkeypatch.setattr(memory_actions_mod, "_memory_plain_from_envelope", _fake_plain)

    res = client.post(
        "/v1/memory/legacy_batch",
        json={"batch_size": 8},
        headers={"X-Feedling-Runtime-Token": tok},  # token-only: NO X-API-Key
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    # the legacy card was decrypted + detected
    assert [r["id"] for r in body["batch"]] == ["m1"]
    assert body["legacy_remaining"] == 1
    # THE PIT: the route pulled the runtime token (no api key) and threaded it in
    assert seen.get("runtime_token") == tok
    assert not seen.get("api_key")
