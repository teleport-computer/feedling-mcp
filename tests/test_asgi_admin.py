"""Native admin data-track and privileged-operation route coverage.

Asserts the FastAPI routes (admin.routes_asgi) return the same status/body as the
Flask oracle (admin.data_track) — both run the *same* admin.data_track functions,
the ASGI side via admin.admin_core (which materialises a Flask request context
from the query string). Covers:
  - JSON routes (summary / users / dau / users/{id}): status + body parity,
    with the volatile ``generated_at`` / ``stuck_for_sec`` fields normalised.
  - HTML pages (/admin/data-track [+ ?view=dau], /admin/data-track/users/{id}):
    status + Content-Type + body parity, with the embedded ``generated_at``
    ISO timestamp normalised; the 404 branch is text/plain.
  - store/evict: side-effect payload + the 400 (missing user_id) branch.
  - users/{id}/delete: confirmation guard, cascade, cache eviction, and audit.
  - admin-token auth: 401 (missing/bad) + 503 (unconfigured), mirroring copytext.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import re
import sys
import uuid
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from accounts import registry  # noqa: E402
from admin import routes_asgi as admin_asgi  # noqa: E402
from admin import memory_metadata  # noqa: E402
from asgi import middleware  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from content import content_core  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from conftest import configure_model_api_route  # noqa: E402
from fastapi import FastAPI  # noqa: E402

ADMIN_TOKEN = "admin-test-token"
ADMIN_PASSWORD = "admin-test-password"
_pk_counter = itertools.count(1)


def _build_asgi_app() -> FastAPI:
    # Standalone app: the admin router + the fixed-body exception handlers,
    # independent of asgi_app.py's package list (owned by the orchestrator).
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    admin_asgi.register_asgi(app)
    return app


_ASGI = _build_asgi_app()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setenv("FEEDLING_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("FEEDLING_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", "runtime-session-test-secret")
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    yield


def _register() -> tuple[str, str]:
    raw = next(_pk_counter).to_bytes(32, "big")
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": base64.b64encode(raw).decode("ascii"), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


# --------------------------------------------------------------------------- #
# request helpers
# --------------------------------------------------------------------------- #

def _flask_get_json(path, headers=None):
    res = make_client().get(path, headers=headers or {})
    return res.status_code, res.get_json(silent=True)


def _flask_get_raw(path, headers=None):
    res = make_client().get(path, headers=headers or {})
    return res.status_code, res.get_data(as_text=True), res.headers.get("Content-Type")


def _asgi(method, path, headers=None, **kw):
    async def go():
        transport = httpx.ASGITransport(app=_ASGI)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.request(method, path, headers=headers or {}, **kw)
            return resp

    return asyncio.run(go())


def _asgi_json(method, path, headers=None, **kw):
    resp = _asgi(method, path, headers=headers, **kw)
    body = None
    if resp.content:
        try:
            body = resp.json()
        except Exception:
            body = None
    return resp.status_code, body


def _asgi_raw(method, path, headers=None, **kw):
    resp = _asgi(method, path, headers=headers, **kw)
    return resp.status_code, resp.text, resp.headers.get("content-type")


def _admin(token=ADMIN_TOKEN):
    return {"X-Admin-Token": token}


# --------------------------------------------------------------------------- #
# normalisers for volatile fields
# --------------------------------------------------------------------------- #

_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?")
_CACHE_NOTE_RE = re.compile(
    r"<div class='cache-note'[^>]*>页面缓存 · 数据生成于 [^<]*</div>"
)


def _norm_json(obj):
    """Blank out fields that depend on wall-clock time between the two calls."""
    if isinstance(obj, dict):
        return {
            k: ("NORM" if k in ("generated_at", "stuck_for_sec") else _norm_json(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_norm_json(x) for x in obj]
    return obj


def _norm_html(text: str) -> str:
    without_cache_note = _CACHE_NOTE_RE.sub("", text)
    return _TS_RE.sub("TS", without_cache_note)


# --------------------------------------------------------------------------- #
# JSON routes — parity
# --------------------------------------------------------------------------- #


def test_route_fence_audit_is_admin_only_and_read_only(env):
    uid, _api_key = _register()
    _credential_id, route_id = configure_model_api_route(
        uid,
        provider="anthropic",
        model="claude-3-5-sonnet-latest",
    )
    # Recreate the historical mismatch without using the fixed route writer.
    db.set_blob(
        uid,
        "onboarding_route",
        {"route": "resident", "selected_at": "2026-07-27T00:00:00Z"},
    )
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_runtime_instances "
            "(user_id,driver,status,pid,lease_owner,lease_expires_at,runtime_home) "
            "VALUES (%s,'claude','running',123,'supervisor-test',"
            "now()+interval '5 minutes','/tmp/runtime') "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "status='running', lease_owner='supervisor-test', "
            "lease_expires_at=now()+interval '5 minutes'",
            (uid,),
        )

    assert _asgi_json("GET", "/v1/admin/route-fence-audit") == (
        401,
        {"error": "unauthorized"},
    )
    status, body = _asgi_json(
        "GET",
        "/v1/admin/route-fence-audit",
        headers=_admin(),
    )
    assert status == 200
    assert body["mode"] == "dry_run"
    row = next(item for item in body["rows"] if item["user_id"] == uid)
    assert row["onboarding_route"] == "resident"
    assert row["model_api_route"]["id"] == route_id
    assert row["runner_lease"]["active"] is True
    assert body["lease_source"]["cardinality"].startswith("one row per user")
    # GET cannot remediate; route stays active until the separately gated CLI
    # is explicitly invoked with --apply.
    assert db.model_api_route_get(uid, route_id)["is_active"] is True

def test_summary_parity_empty(env):
    f = _flask_get_json("/v1/admin/data-track/summary", headers=_admin())
    a = _asgi_json("GET", "/v1/admin/data-track/summary", headers=_admin())
    assert f[0] == a[0] == 200
    assert _norm_json(f[1]) == _norm_json(a[1])
    assert f[1]["summary"]["users_total"] == 0
    assert "users" not in f[1]


def test_users_parity_empty(env):
    f = _flask_get_json("/v1/admin/data-track/users", headers=_admin())
    a = _asgi_json("GET", "/v1/admin/data-track/users", headers=_admin())
    assert f[0] == a[0] == 200
    assert _norm_json(f[1]) == _norm_json(a[1])
    assert f[1]["users"] == []
    assert f[1]["pagination"]["total"] == 0


def test_users_parity_with_user(env):
    uid, _key = _register()
    f = _flask_get_json("/v1/admin/data-track/users", headers=_admin())
    a = _asgi_json("GET", "/v1/admin/data-track/users", headers=_admin())
    assert f[0] == a[0] == 200
    assert _norm_json(f[1]) == _norm_json(a[1])
    assert any(u["user_id"] == uid for u in f[1]["users"])


def test_users_query_params_parity(env):
    _register()
    qs = "?sort=chat&dir=asc&limit=10&offset=0&q=en"
    f = _flask_get_json("/v1/admin/data-track/users" + qs, headers=_admin())
    a = _asgi_json("GET", "/v1/admin/data-track/users" + qs, headers=_admin())
    assert f[0] == a[0] == 200
    assert _norm_json(f[1]) == _norm_json(a[1])
    # The filter echo must reflect the query string parsed on the ASGI side.
    assert f[1]["filters"]["sort"] == "chat"
    assert f[1]["filters"]["dir"] == "asc"


def test_dau_parity(env):
    f = _flask_get_json("/v1/admin/data-track/dau", headers=_admin())
    a = _asgi_json("GET", "/v1/admin/data-track/dau", headers=_admin())
    assert f[0] == a[0] == 200
    assert _norm_json(f[1]) == _norm_json(a[1])
    assert f[1]["summary"]["timezone"] == "Asia/Shanghai"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", f[1]["usage_histogram"]["day"])
    assert len(f[1]["usage_histogram"]["buckets"]) == 8


def test_dau_day_selector_and_invalid_day_parity(env):
    path = "/v1/admin/data-track/dau?day=2035-05-06"
    f = _flask_get_json(path, headers=_admin())
    a = _asgi_json("GET", path, headers=_admin())
    assert f[0] == a[0] == 200
    assert _norm_json(f[1]) == _norm_json(a[1])
    assert f[1]["filters"]["day"] == "2035-05-06"
    assert f[1]["usage_histogram"]["day"] == "2035-05-06"

    invalid_path = "/v1/admin/data-track/dau?day=2035-02-30"
    f_bad = _flask_get_json(invalid_path, headers=_admin())
    a_bad = _asgi_json("GET", invalid_path, headers=_admin())
    assert f_bad == a_bad == (400, {"error": "invalid_day"})


def test_user_detail_parity(env):
    uid, _key = _register()
    f = _flask_get_json(f"/v1/admin/data-track/users/{uid}", headers=_admin())
    a = _asgi_json("GET", f"/v1/admin/data-track/users/{uid}", headers=_admin())
    assert f[0] == a[0] == 200
    assert _norm_json(f[1]) == _norm_json(a[1])
    assert f[1]["user"]["user_id"] == uid


def test_user_detail_not_found_parity(env):
    missing = "usr_0000000000000000"
    f = _flask_get_json(f"/v1/admin/data-track/users/{missing}", headers=_admin())
    a = _asgi_json("GET", f"/v1/admin/data-track/users/{missing}", headers=_admin())
    assert f == a
    assert f == (404, {"error": "user_not_found"})


def test_user_detail_invalid_uid_parity(env):
    path = "/v1/admin/data-track/users/not-a-user"
    f = _flask_get_json(path, headers=_admin())
    a = _asgi_json("GET", path, headers=_admin())
    assert f == a == (400, {"error": "invalid_user_id"})


# --------------------------------------------------------------------------- #
# content-free memory metadata diagnostics
# --------------------------------------------------------------------------- #

def _seed_memory_metadata_rows(user_id: str) -> None:
    rows = [
        (
            "memory-new",
            "2026-08-13T12:00:00Z",
            {
                "created_at": "2026-08-13T12:00:01Z",
                "supersedes": ["memory-old"],
                "source": "memory_dream",
                "summary": "NEVER_RETURN_CARD_SUMMARY",
                "content": "NEVER_RETURN_CARD_CONTENT",
                "body_ct": "NEVER_RETURN_CARD_BODY_CT",
                "prompt": "NEVER_RETURN_CARD_PROMPT",
                "reply": "NEVER_RETURN_CARD_REPLY",
            },
        ),
        (
            "memory-old",
            "2026-08-12",
            {
                "created_at": "2026-08-12T08:30:00Z",
                "superseded_by": "memory-new",
                "capture_mode": "memory_capture",
                "is_archived": True,
                "archive_reason": "superseded_by:memory-new",
                "summary": "NEVER_RETURN_OLD_SUMMARY",
            },
        ),
    ]
    with db.get_pool().connection() as conn:
        for memory_id, occurred_at, doc in rows:
            conn.execute(
                "INSERT INTO memory_moments (user_id,moment_id,occurred_at,doc) "
                "VALUES (%s,%s,%s,%s)",
                (user_id, memory_id, occurred_at, json.dumps(doc)),
            )


def _seed_dream_job_rows(user_id: str) -> None:
    with db.get_pool().connection() as conn:
        first = conn.execute(
            "INSERT INTO agent_jobs "
            "(user_id,lane,status,last_error,created_at,claimed_at,started_at,finished_at) "
            "VALUES (%s,'dream','failed','upstream_unavailable',"
            "'2026-08-13T10:00:00Z','2026-08-13T10:00:05Z',"
            "'2026-08-13T10:00:10Z','2026-08-13T10:04:59Z') RETURNING id",
            (user_id,),
        ).fetchone()[0]
        second = conn.execute(
            "INSERT INTO agent_jobs "
            "(user_id,lane,status,last_error,created_at,started_at,finished_at) "
            "VALUES (%s,'dream','failed','NEVER RETURN RAW PROVIDER BODY',"
            "'2026-08-13T09:00:00Z','2026-08-13T09:00:02Z',"
            "'2026-08-13T09:00:27Z') RETURNING id",
            (user_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO agent_jobs (user_id,lane,status,created_at,finished_at) "
            "VALUES (%s,'chat','completed','2026-08-13T08:00:00Z',"
            "'2026-08-13T08:00:01Z')",
            (user_id,),
        )
        for job_id, provider, model, latency in (
            (first, "openai", "gpt-5.5", 289000),
            (second, "anthropic", "claude-sonnet-4-5", 25000),
        ):
            conn.execute(
                "INSERT INTO v2_turn_metrics "
                "(job_id,user_id,lane,provider,model,latency_ms,failed,status) "
                "VALUES (%s,%s,'dream',%s,%s,%s,true,'failed')",
                (job_id, user_id, provider, model, latency),
            )


def test_memory_card_metadata_is_paginated_and_content_free(env):
    uid, _key = _register()
    _seed_memory_metadata_rows(uid)

    status, first = _asgi_json(
        "GET",
        f"/v1/admin/users/{uid}/memory-card-metadata?limit=1&offset=0",
        headers=_admin(),
    )
    assert status == 200
    assert first["user_id"] == uid
    assert first["pagination"] == {
        "limit": 1,
        "offset": 0,
        "total": 2,
        "has_more": True,
    }
    assert first["cards"] == [
        {
            "id": "memory-new",
            "occurred_at": "2026-08-13T12:00:00Z",
            "created_at": "2026-08-13T12:00:01Z",
            "supersedes": ["memory-old"],
            "superseded_by": "",
            "source": "memory_dream",
            "archived": False,
        }
    ]

    status, second = _asgi_json(
        "GET",
        f"/v1/admin/users/{uid}/memory-card-metadata?limit=1&offset=1"
        f"&admin_key={ADMIN_TOKEN}",
    )
    assert status == 200
    assert second["pagination"]["has_more"] is False
    assert second["cards"][0]["id"] == "memory-old"
    assert second["cards"][0]["source"] == "memory_capture"
    assert second["cards"][0]["archived"] is True

    rendered = json.dumps([first, second])
    for forbidden in (
        "summary",
        "content",
        "body_ct",
        "prompt",
        "reply",
        "NEVER_RETURN",
    ):
        assert forbidden not in rendered
    assert set(first["cards"][0]) == memory_metadata.CARD_FIELDS


def test_dream_job_metadata_supports_filters_pagination_and_no_bodies(env):
    uid, _key = _register()
    _seed_memory_metadata_rows(uid)
    _seed_dream_job_rows(uid)

    path = (
        f"/v1/admin/memory-dream-jobs?user_id={uid}"
        "&status=failed&limit=1&offset=0"
    )
    status, first = _asgi_json("GET", path, headers=_admin())
    assert status == 200
    assert first["filters"] == {"user_id": uid, "status": "failed"}
    assert first["pagination"] == {
        "limit": 1,
        "offset": 0,
        "total": 2,
        "has_more": True,
    }
    assert first["jobs"][0] == {
        "job_id": first["jobs"][0]["job_id"],
        "user_id": uid,
        "lane": "dream",
        "status": "failed",
        "failure_code": "upstream_unavailable",
        "duration_ms": 289000,
        "provider": "openai",
        "model": "gpt-5.5",
        "memory_card_count": 2,
        "created_at": "2026-08-13T10:00:00Z",
        "finished_at": "2026-08-13T10:04:59Z",
    }
    assert set(first["jobs"][0]) == memory_metadata.DREAM_JOB_FIELDS

    status, second = _asgi_json(
        "GET", path.replace("offset=0", "offset=1"), headers=_admin()
    )
    assert status == 200
    assert second["pagination"]["has_more"] is False
    assert second["jobs"][0]["failure_code"] == "runtime_failed"
    assert second["jobs"][0]["duration_ms"] == 25000
    rendered = json.dumps([first, second])
    for forbidden in ("prompt", "reply", "content", "body", "NEVER RETURN"):
        assert forbidden not in rendered


@pytest.mark.parametrize(
    "path",
    [
        "/v1/admin/users/usr_example/memory-card-metadata",
        "/v1/admin/memory-dream-jobs",
    ],
)
def test_memory_metadata_routes_use_existing_admin_auth(env, path):
    assert _asgi_json("GET", path) == (401, {"error": "unauthorized"})
    assert _asgi_json("GET", path, headers=_admin("wrong")) == (
        401,
        {"error": "unauthorized"},
    )


def test_metadata_projection_rejects_unexpected_content_fields():
    hostile = {
        "id": "safe-id",
        "occurred_at": "2026-08-13",
        "created_at": "2026-08-13T00:00:00Z",
        "supersedes": [],
        "superseded_by": "",
        "source": "memory_dream",
        "archived": False,
        "summary": "SECRET SUMMARY",
        "content": "SECRET CONTENT",
        "body_ct": "SECRET CIPHERTEXT",
        "prompt": "SECRET PROMPT",
        "reply": "SECRET REPLY",
    }
    card = memory_metadata.card_metadata_from_row(hostile)
    job = memory_metadata.dream_job_metadata_from_row(
        {
            **hostile,
            "job_id": 7,
            "user_id": "usr_safe",
            "status": "failed",
            "failure_code": "no_json_object",
            "duration_ms": 42,
            "provider": "openai",
            "model": "gpt-5.5",
            "memory_card_count": 3,
            "finished_at": "2026-08-13T00:00:01Z",
        }
    )
    assert set(card) == memory_metadata.CARD_FIELDS
    assert set(job) == memory_metadata.DREAM_JOB_FIELDS
    assert "SECRET" not in json.dumps([card, job])


# --------------------------------------------------------------------------- #
# HTML pages — parity (status + Content-Type + normalised body)
# --------------------------------------------------------------------------- #

def test_data_track_page_parity(env):
    f_status, f_body, f_ct = _flask_get_raw("/admin/data-track", headers=_admin())
    a_status, a_body, a_ct = _asgi_raw("GET", "/admin/data-track", headers=_admin())
    assert f_status == a_status == 200
    assert f_ct == a_ct == "text/html; charset=utf-8"
    assert _norm_html(f_body) == _norm_html(a_body)
    assert "Feedling 值班首页" in f_body


def test_data_track_dau_page_parity(env):
    f_status, f_body, f_ct = _flask_get_raw("/admin/data-track?view=dau", headers=_admin())
    a_status, a_body, a_ct = _asgi_raw("GET", "/admin/data-track?view=dau", headers=_admin())
    assert f_status == a_status == 200
    assert f_ct == a_ct == "text/html; charset=utf-8"
    assert _norm_html(f_body) == _norm_html(a_body)
    assert "Daily Active Users" in f_body


def test_user_detail_page_existing(env):
    uid, _key = _register()
    f_status, f_body, f_ct = _flask_get_raw(f"/admin/data-track/users/{uid}", headers=_admin())
    a_status, a_body, a_ct = _asgi_raw("GET", f"/admin/data-track/users/{uid}", headers=_admin())
    assert f_status == a_status == 200
    assert f_ct == a_ct == "text/html; charset=utf-8"
    # Body embeds a volatile JSON dump (stuck_for_sec) — assert stable substrings.
    for needle in (uid, "Back to data track", "chat messages"):
        assert needle in f_body
        assert needle in a_body


def test_user_detail_page_not_found_parity(env):
    path = "/admin/data-track/users/usr_0000000000000000"
    f_status, f_body, f_ct = _flask_get_raw(path, headers=_admin())
    a_status, a_body, a_ct = _asgi_raw("GET", path, headers=_admin())
    assert f_status == a_status == 404
    assert f_ct == a_ct == "text/plain; charset=utf-8"
    assert f_body == a_body == "user not found"


def test_user_detail_page_invalid_uid_parity(env):
    path = "/admin/data-track/users/not-a-user"
    f_status, f_body, f_ct = _flask_get_raw(path, headers=_admin())
    a_status, a_body, a_ct = _asgi_raw("GET", path, headers=_admin())
    assert f_status == a_status == 400
    assert f_ct == a_ct == "text/html; charset=utf-8"
    assert "UID 格式不正确" in f_body
    assert _norm_html(f_body) == _norm_html(a_body)


# --------------------------------------------------------------------------- #
# store/evict (POST)
# --------------------------------------------------------------------------- #

def test_store_evict_missing_user_id_parity(env):
    f = make_client().post("/v1/admin/store/evict", headers=_admin(), json={})
    a = _asgi_json("POST", "/v1/admin/store/evict", headers=_admin(), json={})
    assert (f.status_code, f.get_json(silent=True)) == a
    assert a == (400, {"error": "user_id required"})


def test_store_evict_uncached_parity(env):
    # A never-cached user id evicts to False on both sides (no state consumed).
    unique = f"evict-{uuid.uuid4().hex}"
    f = make_client().post(
        "/v1/admin/store/evict", headers=_admin(), json={"user_id": unique}
    )
    a = _asgi_json("POST", "/v1/admin/store/evict", headers=_admin(), json={"user_id": unique})
    assert (f.status_code, f.get_json(silent=True)) == a
    assert a == (200, {"evicted": False, "user_id": unique})


def test_store_evict_query_param(env):
    unique = f"evict-{uuid.uuid4().hex}"
    a = _asgi_json("POST", f"/v1/admin/store/evict?user_id={unique}", headers=_admin())
    assert a == (200, {"evicted": False, "user_id": unique})


def test_store_evict_cached_true(env):
    uid, _key = _register()
    core_store.get_store(uid)  # cache it
    a = _asgi_json("POST", "/v1/admin/store/evict", headers=_admin(), json={"user_id": uid})
    assert a == (200, {"evicted": True, "user_id": uid})


# --------------------------------------------------------------------------- #
# destructive admin user deletion (POST)
# --------------------------------------------------------------------------- #

_DELETE_TABLES = (
    "users",
    "chat_messages",
    "memory_moments",
    "user_blobs",
    "agent_runtime_instances",
    "provider_health",
    "v2_user_allowlist",
)


def _seed_delete_rows(user_id: str) -> None:
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO chat_messages (user_id, msg_id, ts, doc) "
            "VALUES (%s, 'admin-delete-msg', 1, '{}'::jsonb)",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO memory_moments (user_id, moment_id, doc) "
            "VALUES (%s, 'admin-delete-memory', '{}'::jsonb)",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO user_blobs (user_id, kind, doc) "
            "VALUES (%s, 'admin-delete-blob', '{}'::jsonb)",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO agent_runtime_instances "
            "(user_id, driver, status, runtime_home) "
            "VALUES (%s, 'claude', 'idle', '/tmp/admin-delete')",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO provider_health (user_id, provider_state) "
            "VALUES (%s, 'ok')",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO v2_user_allowlist "
            "(user_id, desired, updated_by, note) "
            "VALUES (%s, 'resident', 'admin', 'admin-delete') "
            "ON CONFLICT (user_id) DO UPDATE SET desired='resident', "
            "updated_by='admin', note='admin-delete'",
            (user_id,),
        )


def _delete_row_counts(user_id: str) -> dict[str, int]:
    with db.get_pool().connection() as conn:
        return {
            table: conn.execute(
                f"SELECT count(*) FROM {table} WHERE user_id = %s", (user_id,)
            ).fetchone()[0]
            for table in _DELETE_TABLES
        }


def test_admin_delete_user_cascades_evicts_and_audits(env, monkeypatch, caplog):
    uid, _key = _register()
    _seed_delete_rows(uid)
    core_store.get_store(uid)
    assert all(count > 0 for count in _delete_row_counts(uid).values())
    assert uid in registry._key_to_user.values()
    assert uid in core_store._stores

    cleanup_calls = []
    original_delete_user_data = db.delete_user_data

    monkeypatch.setattr(
        content_core,
        "_purge_onboarding_archives_with_retry",
        lambda user_id: cleanup_calls.append(("archives-r2", user_id)),
    )

    def delete_frames(user_id):
        cleanup_calls.append(("frames-r2", user_id))

    def delete_chat_files(user_id):
        cleanup_calls.append(("chat-files-r2", user_id))

    def delete_user_data(user_id):
        cleanup_calls.append(("db-belt", user_id))
        original_delete_user_data(user_id)

    monkeypatch.setattr(db, "delete_user_frames", delete_frames)
    monkeypatch.setattr(db, "delete_user_chat_files", delete_chat_files)
    monkeypatch.setattr(db, "delete_user_data", delete_user_data)
    caplog.set_level("INFO", logger="feedling.admin")

    response = _asgi_json(
        "POST",
        f"/v1/admin/users/{uid}/delete",
        headers=_admin(),
        json={"confirm": uid},
    )

    assert response == (200, {"deleted": True, "user_id": uid})
    assert _delete_row_counts(uid) == {table: 0 for table in _DELETE_TABLES}
    assert all(entry.get("user_id") != uid for entry in registry._users)
    assert uid not in registry._key_to_user.values()
    assert uid not in core_store._stores
    assert cleanup_calls == [
        ("archives-r2", uid),
        ("frames-r2", uid),
        ("chat-files-r2", uid),
        ("db-belt", uid),
    ]
    audit_lines = [
        record.getMessage()
        for record in caplog.records
        if '"event":"admin_user_delete"' in record.getMessage()
    ]
    assert len(audit_lines) == 1
    assert '"who":"admin"' in audit_lines[0]
    assert f'"user_id":"{uid}"' in audit_lines[0]
    assert '"ts":"' in audit_lines[0]


@pytest.mark.parametrize("payload", [{}, {"confirm": "wrong-user"}, []])
def test_admin_delete_user_requires_exact_confirmation(env, payload):
    uid, _key = _register()
    response = _asgi_json(
        "POST",
        f"/v1/admin/users/{uid}/delete",
        headers=_admin(),
        json=payload,
    )
    assert response == (400, {"error": "confirmation_mismatch"})
    assert db.user_exists(uid) is True


def test_admin_delete_user_not_found(env, monkeypatch):
    monkeypatch.setattr(
        content_core,
        "_purge_onboarding_archives_with_retry",
        lambda _user_id: pytest.fail("R2 cleanup must not run for an unknown user"),
    )
    uid = "usr_admin_delete_missing"
    response = _asgi_json(
        "POST",
        f"/v1/admin/users/{uid}/delete",
        headers=_admin(),
        json={"confirm": uid},
    )
    assert response == (404, {"error": "user_not_found"})


def test_admin_delete_user_wrong_token_is_401(env):
    uid, _key = _register()
    response = _asgi_json(
        "POST",
        f"/v1/admin/users/{uid}/delete",
        headers=_admin("wrong"),
        json={"confirm": uid},
    )
    assert response == (401, {"error": "unauthorized"})
    assert db.user_exists(uid) is True


def test_admin_delete_user_archive_failure_aborts(env, monkeypatch):
    uid, _key = _register()
    monkeypatch.setattr(
        content_core,
        "_purge_onboarding_archives_with_retry",
        lambda _user_id: RuntimeError("R2 unavailable"),
    )
    response = _asgi_json(
        "POST",
        f"/v1/admin/users/{uid}/delete",
        headers=_admin(),
        json={"confirm": uid},
    )
    assert response == (503, {"error": "archive_cleanup_failed"})
    assert db.user_exists(uid) is True


# --------------------------------------------------------------------------- #
# admin-token auth parity (mirrors copytext admin tests)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "path,method",
    [
        ("/v1/admin/data-track/summary", "GET"),
        ("/admin/data-track", "GET"),
        ("/v1/admin/store/evict", "POST"),
        ("/v1/admin/users/usr_admin_delete/delete", "POST"),
    ],
)
def test_no_token_is_401_parity(env, path, method):
    f = make_client().open(path, method=method)
    a = _asgi_json(method, path)
    assert (f.status_code, f.get_json(silent=True)) == a
    assert a == (401, {"error": "unauthorized"})


def test_wrong_token_is_401_parity(env):
    f = _flask_get_json("/v1/admin/data-track/summary", headers=_admin("wrong"))
    a = _asgi_json("GET", "/v1/admin/data-track/summary", headers=_admin("wrong"))
    assert f == a
    assert a == (401, {"error": "unauthorized"})


def test_unconfigured_is_503_parity(env, monkeypatch):
    monkeypatch.delenv("FEEDLING_ADMIN_TOKEN", raising=False)
    f = _flask_get_json("/v1/admin/data-track/summary", headers=_admin())
    a = _asgi_json("GET", "/v1/admin/data-track/summary", headers=_admin())
    assert f == a
    assert a == (503, {"error": "service_unavailable", "detail": "admin token is not configured"})


# --------------------------------------------------------------------------- #
# password login + signed admin session cookie
# --------------------------------------------------------------------------- #

def test_admin_login_page_is_public(env):
    response = _asgi("GET", "/admin/login?next=/admin/data-track%3Fview%3Ddau")
    assert response.status_code == 200
    assert 'action="/admin/login"' in response.text
    assert 'name="next" value="/admin/data-track?view=dau"' in response.text


def test_admin_login_sets_signed_secure_cookie_and_cookie_authenticates(env):
    response = _asgi(
        "POST",
        "/admin/login",
        data={"password": ADMIN_PASSWORD, "next": "/admin/data-track"},
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/data-track"
    cookie_header = response.headers["set-cookie"]
    assert "admin_session=" in cookie_header
    assert "Max-Age=604800" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "Secure" in cookie_header
    assert "SameSite=lax" in cookie_header
    cookie_value = response.cookies["admin_session"]
    assert ADMIN_PASSWORD not in cookie_value
    assert ADMIN_TOKEN not in cookie_value

    protected = _asgi_json(
        "GET",
        "/v1/admin/data-track/summary",
        headers={"Cookie": f"admin_session={cookie_value}"},
    )
    assert protected[0] == 200


@pytest.mark.parametrize("supplied", ["wrong", ""])
def test_admin_login_rejects_bad_password_with_same_page(env, supplied):
    response = _asgi(
        "POST",
        "/admin/login",
        data={"password": supplied, "next": "/admin/data-track"},
    )
    assert response.status_code == 401
    assert "密码不对，再试一次。" in response.text
    assert "admin_session=" not in response.headers.get("set-cookie", "")


def test_admin_login_without_password_config_is_generic_401(env, monkeypatch):
    monkeypatch.delenv("FEEDLING_ADMIN_PASSWORD")
    response = _asgi("POST", "/admin/login", data={"password": ADMIN_PASSWORD})
    assert response.status_code == 401
    assert "密码不对，再试一次。" in response.text


def test_admin_session_rejects_tampering_and_expiry(env):
    valid = admin_asgi._sign_admin_session(expires_at=2_000_000_000)
    expired = admin_asgi._sign_admin_session(expires_at=1)
    assert valid is not None
    assert admin_asgi._valid_admin_session(valid, now=1_900_000_000) is True
    assert admin_asgi._valid_admin_session(valid + "x", now=1_900_000_000) is False
    assert admin_asgi._valid_admin_session(expired, now=2) is False

    response = _asgi_json(
        "GET",
        "/v1/admin/data-track/summary",
        headers={"Cookie": f"admin_session={valid}x"},
    )
    assert response == (401, {"error": "unauthorized"})


def test_admin_session_uses_token_fallback_and_rejects_secret_rotation(env, monkeypatch):
    monkeypatch.delenv("FEEDLING_RUNTIME_TOKEN_SECRET")
    session = admin_asgi._sign_admin_session(expires_at=2_000_000_000)
    assert session is not None
    assert admin_asgi._valid_admin_session(session, now=1_900_000_000) is True

    monkeypatch.setenv("FEEDLING_ADMIN_TOKEN", "rotated-admin-token")
    assert admin_asgi._valid_admin_session(session, now=1_900_000_000) is False


@pytest.mark.parametrize(
    "headers,path",
    [
        ({"X-Admin-Token": ADMIN_TOKEN}, "/v1/admin/data-track/summary"),
        ({"Authorization": f"Bearer {ADMIN_TOKEN}"}, "/v1/admin/data-track/summary"),
        ({}, f"/v1/admin/data-track/summary?admin_key={ADMIN_TOKEN}"),
    ],
)
def test_legacy_admin_token_channels_remain_supported(env, headers, path):
    assert _asgi_json("GET", path, headers=headers)[0] == 200


def test_admin_login_rejects_external_next_and_logout_clears_cookie(env):
    login = _asgi(
        "POST",
        "/admin/login",
        data={"password": ADMIN_PASSWORD, "next": "https://example.com/steal"},
    )
    assert login.status_code == 303
    assert login.headers["location"] == "/admin/data-track"

    logout = _asgi("GET", "/admin/logout")
    assert logout.status_code == 303
    assert logout.headers["location"] == "/admin/login"
    cookie_header = logout.headers["set-cookie"]
    assert "admin_session=" in cookie_header
    assert "Max-Age=0" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "Secure" in cookie_header
    assert "SameSite=lax" in cookie_header
