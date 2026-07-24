"""Export must ship the user's world book.

World book entries are content the user typed by hand (Settings → 世界书 in the
iOS app), stored as v1 envelopes exactly like chat/memory/identity. Account
deletion drops them (``db.delete_user_data`` lists ``world_book_entries``), so
an export that omits them hands the user an incomplete copy of what they are
about to lose — the same class of silent gap as an offloaded chat body that
exports with no ciphertext in it (see test_content_offloaded_bodies.py).

The server never decrypts: these assertions check the ciphertext is shipped
verbatim for client-side decrypt, never that plaintext appears.
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
    # No FEEDLING_ENCLAVE_URL -> worldbook upsert skips the enclave content cap
    # check and stores the envelope directly (worldbook_core §_validate_content_cap).
    monkeypatch.delenv("FEEDLING_ENCLAVE_URL", raising=False)
    with make_client() as c:
        yield c


def _headers(api_key: str) -> dict:
    return {"X-API-Key": api_key}


def _register(client) -> tuple[str, str]:
    res = client.post("/v1/users/register",
                      json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"})
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _envelope(user_id: str, entry_id: str, body: bytes,
              visibility: str = "shared") -> dict:
    env = {
        "v": 1,
        "id": entry_id,
        "body_ct": _b64(body),
        "nonce": _b64(b"\x00" * 12),
        "K_user": _b64(b"\x01" * 48),
        "visibility": visibility,
        "owner_user_id": user_id,
    }
    if visibility == "shared":
        env["K_enclave"] = _b64(b"\x02" * 48)
    return env


def _upsert(client, api_key: str, envelope: dict) -> None:
    res = client.post("/v1/worldbook/upsert", headers=_headers(api_key),
                      json={"envelope": envelope})
    assert res.status_code == 200, res.get_data(as_text=True)


def test_export_includes_world_book_entries(client):
    user_id, api_key = _register(client)
    _upsert(client, api_key, _envelope(user_id, "wb1", b"world-book-ciphertext-1"))

    res = client.get("/v1/content/export", headers=_headers(api_key))
    assert res.status_code == 200, res.get_data(as_text=True)

    entries = res.get_json()["world_book"]
    row = next(e for e in entries if e.get("id") == "wb1")
    assert base64.b64decode(row["body_ct"]) == b"world-book-ciphertext-1"


def test_export_does_not_filter_world_book_by_visibility(client):
    # Export must hand back whatever is stored, not a filtered projection: the
    # user owns every row and holds the only key that reads it.
    #
    # local_only is currently UNREACHABLE for world book in production — the
    # enclave deliberately refuses to read such an entry
    # (enclave/routes/worldbook.py: `visibility == "local_only" or not
    # K_enclave` -> unavailable_ids), so /v1/worldbook/upsert answers 400
    # worldbook_validate_failed, and iOS seals world book entries as `shared`
    # unconditionally (FeedlingAPI.sealForCurrentUser). That is by design: a
    # world book entry exists to be injected into the agent's prompt, which
    # requires enclave-readable ciphertext. This test reaches the state
    # directly (no FEEDLING_ENCLAVE_URL -> the cap check is skipped) purely to
    # pin the export side: should such a row ever exist — legacy data, a
    # self-hosted deploy with no enclave configured, or a future feature — the
    # export must still ship it rather than silently drop it.
    user_id, api_key = _register(client)
    _upsert(client, api_key,
            _envelope(user_id, "wb-local", b"private-entry", visibility="local_only"))

    res = client.get("/v1/content/export", headers=_headers(api_key))
    entries = res.get_json()["world_book"]
    row = next(e for e in entries if e.get("id") == "wb-local")
    assert row["visibility"] == "local_only"
    assert not row.get("K_enclave")
    assert base64.b64decode(row["body_ct"]) == b"private-entry"


def test_exported_world_book_entry_keeps_its_envelope_fields(client):
    # Without nonce + K_user the ciphertext is undecryptable, and without
    # updated_at the user cannot tell revisions apart. Export is the user's only
    # copy, so it has to carry the whole envelope, not a display projection.
    user_id, api_key = _register(client)
    _upsert(client, api_key, _envelope(user_id, "wb2", b"body"))

    res = client.get("/v1/content/export", headers=_headers(api_key))
    row = next(e for e in res.get_json()["world_book"] if e.get("id") == "wb2")
    for field in ("nonce", "K_user", "visibility", "owner_user_id", "updated_at"):
        assert row.get(field), f"exported world book entry lost {field}"


def test_export_schema_version_announces_world_book_support(client):
    # An empty world_book and a pre-world-book export both read as "no entries"
    # to anything walking the JSON. The version is the only way a later reader
    # can tell a complete export from one taken before world book was covered.
    _user_id, api_key = _register(client)
    res = client.get("/v1/content/export", headers=_headers(api_key))
    assert res.get_json()["schema_version"] >= 3


def test_export_world_book_is_empty_list_when_user_has_none(client):
    # Absent vs empty matters to the client: a missing key reads as "this export
    # predates world book support", an empty list as "you had none".
    _register_out = _register(client)
    res = client.get("/v1/content/export", headers=_headers(_register_out[1]))
    assert res.get_json()["world_book"] == []
