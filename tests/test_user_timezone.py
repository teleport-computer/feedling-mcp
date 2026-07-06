from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import store as core_store  # noqa: E402
from accounts import registry  # noqa: E402


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


def _register(client) -> tuple[str, str, str]:
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["principal_id"], body["api_key"]


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def test_preferences_sets_valid_timezone(client):
    user_id, _pid, api_key = _register(client)
    res = client.post(
        "/v1/users/preferences",
        headers=_headers(api_key),
        json={"timezone": "Asia/Shanghai"},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    assert res.get_json()["timezone"] == "Asia/Shanghai"
    assert registry._get_user_timezone(user_id) == "Asia/Shanghai"


def test_preferences_rejects_invalid_timezone(client):
    user_id, _pid, api_key = _register(client)
    res = client.post(
        "/v1/users/preferences",
        headers=_headers(api_key),
        json={"timezone": "Mars/Olympus"},
    )
    assert res.status_code == 400
    assert registry._get_user_timezone(user_id) is None


def test_preferences_clears_timezone_on_null(client):
    user_id, _pid, api_key = _register(client)
    client.post("/v1/users/preferences", headers=_headers(api_key),
                json={"timezone": "Europe/Paris"})
    res = client.post("/v1/users/preferences", headers=_headers(api_key),
                      json={"timezone": None})
    assert res.status_code == 200
    assert res.get_json()["timezone"] is None
    assert registry._get_user_timezone(user_id) is None


def test_invalid_timezone_does_not_apply_archive_language(client):
    user_id, _pid, api_key = _register(client)
    res = client.post(
        "/v1/users/preferences",
        headers=_headers(api_key),
        json={"archive_language": "fr", "timezone": "Mars/Olympus"},
    )
    assert res.status_code == 400
    # archive_language must NOT have been written (no partial apply)
    from accounts import registry
    assert registry._get_user_archive_language(user_id) != "fr"


def test_whoami_returns_record_timezone(client):
    _uid, _pid, api_key = _register(client)
    client.post("/v1/users/preferences", headers=_headers(api_key),
                json={"timezone": "Asia/Tokyo"})
    who = client.get("/v1/users/whoami", headers=_headers(api_key)).get_json()
    assert who["timezone"] == "Asia/Tokyo"


def test_whoami_falls_back_to_perception_snapshot(client):
    user_id, _pid, api_key = _register(client)
    # No record timezone; seed the perception snapshot state directly.
    from perception import store as perc_store
    perc_store.merge_state(user_id, {"timezone": {"v": "Europe/Berlin", "ts": 1.0}})
    who = client.get("/v1/users/whoami", headers=_headers(api_key)).get_json()
    assert who["timezone"] == "Europe/Berlin"


def test_whoami_omits_timezone_when_unknown(client):
    _uid, _pid, api_key = _register(client)
    who = client.get("/v1/users/whoami", headers=_headers(api_key)).get_json()
    assert "timezone" not in who


def test_device_events_timezone_populates_record(client):
    user_id, _pid, api_key = _register(client)
    res = client.post(
        "/v1/device/events",
        headers=_headers(api_key),
        json={"type": "app_presence", "source": "ios",
              "payload": {"timezone": "America/New_York", "is_foreground": True}},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    assert registry._get_user_timezone(user_id) == "America/New_York"
    who = client.get("/v1/users/whoami", headers=_headers(api_key)).get_json()
    assert who["timezone"] == "America/New_York"


def test_record_context_timezone_rejects_invalid(client):
    user_id, _pid, _api_key = _register(client)
    from perception import service as perception_service
    assert perception_service.record_context_timezone(user_id, "Not/AZone") is False
    assert registry._get_user_timezone(user_id) is None
