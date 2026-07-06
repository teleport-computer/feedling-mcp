from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from worldbook import worldbook_core  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
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


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def _env(user_id: str, entry_id: str = "wb1", *, body_ct: str = "ct") -> dict:
    return {
        "v": 1,
        "id": entry_id,
        "body_ct": body_ct,
        "nonce": "nonce",
        "K_user": "k-user",
        "K_enclave": "k-enclave",
        "visibility": "shared",
        "owner_user_id": user_id,
        "enclave_pk_fpr": "fpr",
    }


def test_worldbook_routes_require_auth(client):
    assert client.get("/v1/worldbook/list").status_code == 401
    assert client.post("/v1/worldbook/upsert", json={}).status_code == 401
    assert client.delete("/v1/worldbook/delete?id=wb1").status_code == 401


def test_worldbook_upsert_list_delete_round_trips_ciphertext(client):
    user_id, api_key = _register(client)
    env = _env(user_id, body_ct="body-1")

    upsert = client.post("/v1/worldbook/upsert", json=env, headers=_headers(api_key))
    assert upsert.status_code == 200, upsert.get_data(as_text=True)
    assert upsert.get_json() == {"id": "wb1"}

    listed = client.get("/v1/worldbook/list", headers=_headers(api_key))
    assert listed.status_code == 200, listed.get_data(as_text=True)
    envelopes = listed.get_json()["envelopes"]
    assert len(envelopes) == 1
    assert {key: envelopes[0][key] for key in env} == env

    deleted = client.delete("/v1/worldbook/delete?id=wb1", headers=_headers(api_key))
    assert deleted.status_code == 200, deleted.get_data(as_text=True)
    assert deleted.get_json() == {"ok": True}
    assert client.get("/v1/worldbook/list", headers=_headers(api_key)).get_json() == {"envelopes": []}


def test_worldbook_upsert_rejects_outer_id_mismatch(client):
    user_id, api_key = _register(client)
    res = client.post(
        "/v1/worldbook/upsert",
        json={"id": "outer", "envelope": _env(user_id, "inner")},
        headers=_headers(api_key),
    )
    assert res.status_code == 400
    assert "id" in res.get_json()["error"]


def test_worldbook_upsert_rejects_wrong_owner(client):
    _user_id, api_key = _register(client)
    res = client.post(
        "/v1/worldbook/upsert",
        json=_env("other-user", "wb1"),
        headers=_headers(api_key),
    )
    assert res.status_code == 400
    assert res.get_json()["error"] == "owner_user_id does not match caller"


def test_worldbook_upsert_rejects_over_cap_content_reported_by_enclave(client, monkeypatch):
    user_id, api_key = _register(client)

    def fake_validate(api_key_arg, world_books, messages, *, runtime_token=None):
        assert api_key_arg == api_key
        assert [item["id"] for item in world_books] == ["too-big"]
        assert messages == []
        return {"block": "", "matched_names": [], "rejected_over_cap": ["too-big"]}

    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "http://enclave.test")
    monkeypatch.setattr(
        worldbook_core.worldbook_readside_core,
        "post_enclave_worldbook_match",
        fake_validate,
    )

    res = client.post(
        "/v1/worldbook/upsert",
        json=_env(user_id, "too-big"),
        headers=_headers(api_key),
    )

    assert res.status_code == 400
    assert res.get_json() == {"error": "content_too_long", "id": "too-big", "max_chars": 20000}
    assert client.get("/v1/worldbook/list", headers=_headers(api_key)).get_json() == {"envelopes": []}


def test_worldbook_upsert_rejects_when_enclave_cannot_validate_envelope(client, monkeypatch):
    user_id, api_key = _register(client)

    def fake_validate(api_key_arg, world_books, messages, *, runtime_token=None):
        return {"block": "", "matched_names": [], "rejected_over_cap": [], "unavailable_ids": ["bad-env"]}

    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "http://enclave.test")
    monkeypatch.setattr(
        worldbook_core.worldbook_readside_core,
        "post_enclave_worldbook_match",
        fake_validate,
    )

    res = client.post(
        "/v1/worldbook/upsert",
        json=_env(user_id, "bad-env"),
        headers=_headers(api_key),
    )

    assert res.status_code == 400
    assert res.get_json() == {"error": "worldbook_validate_failed", "id": "bad-env"}
    assert client.get("/v1/worldbook/list", headers=_headers(api_key)).get_json() == {"envelopes": []}


def test_worldbook_match_calls_enclave_with_stored_envelopes(client, monkeypatch):
    user_id, api_key = _register(client)
    upsert = client.post("/v1/worldbook/upsert", json=_env(user_id, "wb-match"), headers=_headers(api_key))
    assert upsert.status_code == 200

    def fake_match(api_key_arg, world_books, messages, *, runtime_token=None):
        assert api_key_arg == api_key
        assert [item["id"] for item in world_books] == ["wb-match"]
        assert messages == [{"role": "user", "content": "hello trigger"}]
        return {
            "block": "<world_book>\nhello\n</world_book>",
            "matched_names": ["Match"],
            "rejected_over_cap": [],
            "unavailable_ids": [],
        }

    monkeypatch.setattr(
        worldbook_core.worldbook_readside_core,
        "post_enclave_worldbook_match",
        fake_match,
    )

    res = client.post(
        "/v1/worldbook/match",
        json={"message": "hello trigger"},
        headers=_headers(api_key),
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["block"] == "<world_book>\nhello\n</world_book>"
    assert body["matched_names"] == ["Match"]
