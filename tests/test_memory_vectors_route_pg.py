"""Real PostgreSQL + real ASGI auth for POST /v1/memory/vectors (T523).

Two users hold a card with the SAME moment id but different text, so their
stored vectors differ. Each credential must see only its own row; nothing in
the request body can switch the user; missing/invalid credentials are refused;
a runtime token alone (no API key, the hosted enclave→backend forward) works.
"""
from __future__ import annotations

import asyncio
import base64
import json
import struct
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from accounts import registry  # noqa: E402
from asgi import middleware  # noqa: E402
from core import runtime_token  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from memory import routes_asgi as memory_asgi  # noqa: E402
from memory.embedding import fake, sweep  # noqa: E402
from model_api_runtime.v2 import serve_worker  # noqa: E402

_SECRET = "test-runtime-secret"
SHARED_ID = "mom_same_id"


def _app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    memory_asgi.register_asgi(app)
    return app


_ASGI = _app()


def _post(payload, headers):
    async def go():
        transport = httpx.ASGITransport(app=_ASGI)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.post("/v1/memory/vectors", json=payload, headers=headers)
            return resp.status_code, (resp.json() if resp.content else None)
    return asyncio.run(go())


def _card(uid, mid, text, **extra):
    return {"id": mid, "owner_user_id": uid, "visibility": "shared", "status": "active",
            "body": json.dumps({"summary": text, "content": text}), **extra}


@pytest.fixture()
def two_users(monkeypatch):
    registry.load_users()
    a = registry._register_user()
    b = registry._register_user()
    encoder = fake.FakeEmbedder()
    monkeypatch.setenv("FEEDLING_MEMORY_EMBEDDING_ENABLED", "1")
    monkeypatch.setattr(sweep, "_embedder", encoder)
    monkeypatch.setattr(sweep, "_unavailable_logged", False)
    for user, text in ((a, "alpha likes green tea"), (b, "bravo rides a red bike")):
        assert db.memory_upsert(user["user_id"], SHARED_ID, "2026-09-26",
                                _card(user["user_id"], SHARED_ID, text))
        assert serve_worker._tick_embedding_for_user(user["user_id"]) == 1
    return a, b, encoder


def _vector(body):
    rows = body["vectors"]
    assert [r["id"] for r in rows] == [SHARED_ID]
    blob = base64.b64decode(rows[0]["vector_b64"])
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _stored(user, encoder):
    return db.memory_vectors_load(user["user_id"], encoder.model_id)[SHARED_ID][1]


def test_each_api_key_sees_only_its_own_row_for_the_same_moment_id(two_users):
    a, b, encoder = two_users
    payload = {"model_id": encoder.model_id, "ids": [SHARED_ID]}
    status_a, body_a = _post(payload, {"X-API-Key": a["api_key"]})
    status_b, body_b = _post(payload, {"X-API-Key": b["api_key"]})
    assert status_a == status_b == 200
    va, vb = _vector(body_a), _vector(body_b)
    assert va == pytest.approx(_stored(a, encoder)) and vb == pytest.approx(_stored(b, encoder))
    assert va != pytest.approx(vb)


def test_body_identity_fields_cannot_switch_the_user(two_users):
    a, b, encoder = two_users
    status, body = _post({"model_id": encoder.model_id, "ids": [SHARED_ID],
                          "user_id": b["user_id"], "owner_user_id": b["user_id"]},
                         {"X-API-Key": a["api_key"]})
    assert status == 200 and _vector(body) == pytest.approx(_stored(a, encoder))


def test_runtime_token_alone_is_the_hosted_forward_and_stays_scoped(two_users, monkeypatch):
    a, b, encoder = two_users
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    tok = runtime_token.mint(_SECRET.encode(), user_id=a["user_id"], runtime_instance_id="ri_t",
                             scope=["memory"], ttl=900.0)
    status, body = _post({"model_id": encoder.model_id, "ids": [SHARED_ID], "user_id": b["user_id"]},
                         {"X-Feedling-Runtime-Token": tok})
    assert status == 200 and _vector(body) == pytest.approx(_stored(a, encoder))


@pytest.mark.parametrize("headers", [
    {}, {"X-API-Key": "0" * 64}, {"X-API-Key": ""},
    {"X-Feedling-Runtime-Token": "not-a-token"},
])
def test_missing_or_invalid_credentials_are_refused(two_users, headers, monkeypatch):
    _, _, encoder = two_users
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    status, body = _post({"model_id": encoder.model_id, "ids": [SHARED_ID]}, headers)
    assert status == 401 and "vectors" not in (body or {})


def test_a_deleted_card_is_not_served_even_before_the_sweep_prunes(two_users):
    a, _, encoder = two_users
    assert db.memory_delete(a["user_id"], SHARED_ID)
    assert SHARED_ID in db.memory_vectors_load(a["user_id"], encoder.model_id)  # row still there
    status, body = _post({"model_id": encoder.model_id, "ids": [SHARED_ID]}, {"X-API-Key": a["api_key"]})
    assert status == 200 and body["vectors"] == []


def test_a_card_made_local_only_is_not_served_before_the_sweep_prunes(two_users):
    a, _, encoder = two_users
    assert db.memory_upsert(a["user_id"], SHARED_ID, "2026-09-26",
                            _card(a["user_id"], SHARED_ID, "alpha likes green tea",
                                  visibility="local_only"))
    status, body = _post({"model_id": encoder.model_id, "ids": [SHARED_ID]}, {"X-API-Key": a["api_key"]})
    assert status == 200 and body["vectors"] == []
