"""Server-managed UI copy: bundle read (ETag/304) + admin edit (auth/validation).

The copytext tables are global (no per-user scoping), so an autouse fixture
resets them before each test for deterministic revisions.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import app as appmod  # noqa: E402  (import triggers db.init_schema → migration 0006)
import db  # noqa: E402
from copytext import service as copytext_service  # noqa: E402

ADMIN_TOKEN = "test-admin-token"


@pytest.fixture(autouse=True)
def _reset_copytext():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM copytext_strings")
        conn.execute("UPDATE copytext_meta SET revision = 0 WHERE id = TRUE")
    yield


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("FEEDLING_ADMIN_TOKEN", ADMIN_TOKEN)
    appmod.app.config.update(TESTING=True)
    with appmod.app.test_client() as c:
        yield c


def _admin(token: str = ADMIN_TOKEN) -> dict[str, str]:
    return {"X-Admin-Token": token}


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def test_empty_bundle(client):
    res = client.get("/v1/copytext")
    assert res.status_code == 200, res.get_data(as_text=True)
    assert res.get_json() == {"revision": 0, "strings": {}}
    assert res.headers["ETag"] == '"0"'


def test_get_returns_etag_and_304_on_match(client):
    client.post(
        "/v1/copytext",
        headers=_admin(),
        json={"strings": {"chat.empty.title": {"en": "Hi", "zh-Hans": "你好"}}},
    )
    res = client.get("/v1/copytext")
    assert res.status_code == 200
    etag = res.headers["ETag"]
    assert etag == '"1"'
    assert res.get_json()["strings"]["chat.empty.title"]["zh-Hans"] == "你好"

    # Same ETag → 304, no body needed.
    res2 = client.get("/v1/copytext", headers={"If-None-Match": etag})
    assert res2.status_code == 304
    assert res2.headers["ETag"] == etag

    # Stale ETag → 200 with fresh bundle.
    res3 = client.get("/v1/copytext", headers={"If-None-Match": '"0"'})
    assert res3.status_code == 200


# ---------------------------------------------------------------------------
# Service-level edits
# ---------------------------------------------------------------------------

def test_apply_edits_bumps_revision_and_persists():
    r1 = copytext_service.apply_edits(
        {"strings": {"a.b": {"en": "A", "zh-Hans": "甲"}}}
    )
    assert r1 == {"revision": 1, "upserted": 2, "deleted": 0}

    bundle = copytext_service.build_bundle()
    assert bundle["revision"] == 1
    assert bundle["strings"]["a.b"] == {"en": "A", "zh-Hans": "甲"}

    # Update one lang + delete the key in a later edit.
    r2 = copytext_service.apply_edits({"strings": {"a.b": {"en": "A2"}}})
    assert r2["revision"] == 2
    assert copytext_service.build_bundle()["strings"]["a.b"]["en"] == "A2"

    r3 = copytext_service.apply_edits({"delete": ["a.b"]})
    assert r3 == {"revision": 3, "upserted": 0, "deleted": 1}
    assert copytext_service.build_bundle()["strings"] == {}


# ---------------------------------------------------------------------------
# Write auth + validation
# ---------------------------------------------------------------------------

def test_post_requires_admin_token(client):
    body = {"strings": {"k": {"en": "v"}}}
    assert client.post("/v1/copytext", json=body).status_code == 401
    assert client.post("/v1/copytext", headers=_admin("wrong"), json=body).status_code == 401
    ok = client.post("/v1/copytext", headers=_admin(), json=body)
    assert ok.status_code == 200, ok.get_data(as_text=True)
    assert ok.get_json()["revision"] == 1


def test_post_rejects_bad_payload(client):
    bad_lang = client.post(
        "/v1/copytext", headers=_admin(), json={"strings": {"k": {"fr": "v"}}}
    )
    assert bad_lang.status_code == 400

    bad_value = client.post(
        "/v1/copytext", headers=_admin(), json={"strings": {"k": {"en": 123}}}
    )
    assert bad_value.status_code == 400

    empty = client.post("/v1/copytext", headers=_admin(), json={})
    assert empty.status_code == 400

    # Truthy non-object JSON bodies must be a controlled 400, not a 500.
    for body in ([1], "x", 5):
        res = client.post(
            "/v1/copytext",
            headers={**_admin(), "Content-Type": "application/json"},
            data=json.dumps(body),
        )
        assert res.status_code == 400, f"{body!r} → {res.status_code}"
