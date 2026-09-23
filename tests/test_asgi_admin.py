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
import ast
import base64
import inspect
import itertools
import json
import re
import sys
import textwrap
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest
from psycopg.errors import QueryCanceled
from psycopg_pool import PoolTimeout

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
import debug_trace  # noqa: E402
from accounts import registry  # noqa: E402
from admin import data_track  # noqa: E402
from admin import routes_asgi as admin_asgi  # noqa: E402
from admin import memory_metadata  # noqa: E402
from asgi import middleware  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from content import content_core  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from conftest import configure_model_api_route  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from memory import actions as memory_actions  # noqa: E402

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


def test_debug_query_timeout_and_pool_busy_have_distinct_503s(env, monkeypatch):
    assert admin_asgi.DEBUG_TRACE_REQUEST_TIMEOUT_SEC == 3.0
    calls = []

    def query_cancelled(_query):
        raise QueryCanceled("debug statement deadline")

    monkeypatch.setattr(admin_asgi.admin_core, "debug_payload", query_cancelled)
    started = time.monotonic()
    status, payload = _asgi_json(
        "GET", "/v1/admin/data-track/debug", headers=_admin()
    )
    assert time.monotonic() - started < admin_asgi.DEBUG_TRACE_REQUEST_TIMEOUT_SEC
    assert (status, payload) == (503, {"error": "debug_query_timeout"})

    def pool_busy(_query):
        calls.append("busy")
        raise PoolTimeout("debug pool acquire deadline")

    monkeypatch.setattr(admin_asgi.admin_core, "debug_payload", pool_busy)
    status, payload = _asgi_json(
        "GET", "/v1/admin/data-track/debug", headers=_admin()
    )
    assert calls == ["busy"]
    assert (status, payload) == (503, {"error": "service_busy"})


def test_debug_trace_flag_read_timeout_is_a_503(env, monkeypatch):
    """The deliberate flag-read fail-closed contract reaches the HTTP route."""
    registry._users[:] = [
        {"user_id": "user_a", "principal_id": "p_a"},
    ]
    event = {
        "ts": 100,
        "user_id": "user_a",
        "subsystem": "agent",
        "type": "agent.reply",
        "actor": "backend",
        "status": "ok",
        "summary": "",
        "explain": "agent.reply",
        "trace_id": "t-flag-timeout",
        "turn_id": "t-flag-timeout",
        "job_id": "",
        "dur_ms": None,
        "detail": {},
        "content_excerpt": {},
    }
    monkeypatch.setattr(
        data_track.db,
        "query_trace_events_flat_page",
        lambda **_kwargs: {
            "events_total": 1,
            "turns_total": 1,
            "stalled_turns": 0,
            "error_turns": 0,
            "scan_truncated": False,
            "users": [{"user_id": "user_a", "events": 1, "last_ts": 100}],
            "subsystems": ["agent"],
            "statuses": ["ok"],
            "rows": [event],
        },
    )
    monkeypatch.setattr(
        data_track.db,
        "query_trace_event_turn_rows",
        lambda **_kwargs: ([event], False),
    )
    calls = []

    def fail_flag_read(_user_ids, _kinds, **kwargs):
        calls.append(kwargs)
        raise TimeoutError("trace flag read timed out")

    monkeypatch.setattr(data_track.db, "get_blobs_for_users", fail_flag_read)

    status, payload = _asgi_json(
        "GET",
        "/v1/admin/data-track/debug?mode=flat&user_id=user_a",
        headers=_admin(),
    )

    assert (status, payload) == (503, {"error": "debug_query_timeout"})
    assert len(calls) == 1
    assert calls[0]["raise_on_error"] is True
    assert calls[0]["connection_timeout"] > 0
    assert calls[0]["statement_timeout_ms"] > 0


def test_debug_route_deadline_abandons_a_slow_sync_worker(env, monkeypatch):
    monkeypatch.setattr(admin_asgi, "DEBUG_TRACE_REQUEST_TIMEOUT_SEC", 0.05)

    def slow_debug_payload(_query):
        time.sleep(1.0)
        return {"too_late": True}

    monkeypatch.setattr(admin_asgi.admin_core, "debug_payload", slow_debug_payload)
    started = time.monotonic()
    status, payload = _asgi_json(
        "GET", "/v1/admin/data-track/debug", headers=_admin()
    )
    elapsed = time.monotonic() - started

    assert (status, payload) == (503, {"error": "debug_query_timeout"})
    assert elapsed < 1.0


def test_t428_all_ten_data_track_routes_use_bounded_db_bridge():
    handlers = (
        admin_asgi.data_track_summary,
        admin_asgi.data_track_users,
        admin_asgi.data_track_dau,
        admin_asgi.data_track_events,
        admin_asgi.data_track_growth,
        admin_asgi.data_track_verdicts,
        admin_asgi.data_track_user,
        admin_asgi.data_track_page,
        admin_asgi.data_track_user_lookup,
        admin_asgi.data_track_user_page,
    )

    assert admin_asgi.DATA_TRACK_REQUEST_TIMEOUT_SEC == (
        db._ADMIN_DATA_TRACK_READ_TIMEOUT_MS / 1000
    )
    assert admin_asgi.DATA_TRACK_DETAIL_REQUEST_TIMEOUT_SEC == (
        db._ADMIN_DATA_TRACK_DETAIL_READ_TIMEOUT_MS / 1000
    )
    for handler in handlers:
        tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        bounded_calls = [
            node
            for node in calls
            if isinstance(node.func, ast.Name)
            and node.func.id == "_run_data_track_db"
        ]
        raw_calls = [
            node
            for node in calls
            if isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "threadpool"
            and node.func.attr == "run_db"
        ]
        assert len(bounded_calls) == 1, handler.__name__
        assert raw_calls == [], handler.__name__
        timeout_keywords = [
            keyword
            for keyword in bounded_calls[0].keywords
            if keyword.arg == "timeout_seconds"
        ]
        lease_keywords = [
            keyword
            for keyword in bounded_calls[0].keywords
            if keyword.arg == "statement_timeout_ms"
        ]
        if handler in {
            admin_asgi.data_track_user,
            admin_asgi.data_track_user_page,
        }:
            assert len(timeout_keywords) == 1, handler.__name__
            assert isinstance(timeout_keywords[0].value, ast.Name)
            assert timeout_keywords[0].value.id == (
                "DATA_TRACK_DETAIL_REQUEST_TIMEOUT_SEC"
            )
            assert lease_keywords == [], handler.__name__
        elif handler is admin_asgi.data_track_users:
            # T653: the fleet users list is the one endpoint with a 15 s
            # fallback budget, and that budget must reach the SQL lease too —
            # otherwise statement_timeout still cancels the snapshot at 5 s.
            assert admin_asgi.DATA_TRACK_USERS_REQUEST_TIMEOUT_SEC == 15.0
            assert len(timeout_keywords) == 1, handler.__name__
            assert isinstance(timeout_keywords[0].value, ast.Name)
            assert timeout_keywords[0].value.id == (
                "DATA_TRACK_USERS_REQUEST_TIMEOUT_SEC"
            )
            assert len(lease_keywords) == 1, handler.__name__
            lease_src = ast.unparse(lease_keywords[0].value)
            assert lease_src == "int(DATA_TRACK_USERS_REQUEST_TIMEOUT_SEC * 1000)", lease_src
        else:
            assert timeout_keywords == [], handler.__name__
            assert lease_keywords == [], handler.__name__


def test_t428_data_track_deadline_returns_503_without_waiting_for_db(
    env, monkeypatch
):
    release = threading.Event()
    safety_release = threading.Timer(1.0, release.set)

    def slow_summary(_query):
        release.wait(1.0)
        return {"too_late": True}

    monkeypatch.setattr(admin_asgi, "DATA_TRACK_REQUEST_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(admin_asgi.admin_core, "summary_payload", slow_summary)
    safety_release.start()
    started = time.monotonic()
    try:
        status, payload = _asgi_json(
            "GET", "/v1/admin/data-track/summary", headers=_admin()
        )
        elapsed = time.monotonic() - started
    finally:
        release.set()
        safety_release.cancel()

    assert (status, payload) == (503, {"error": "data_track_query_timeout"})
    assert elapsed < 0.5


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


def test_memory_length_rejection_is_queryable_through_admin_data_track(
    env,
    tee_primary,
    monkeypatch,
):
    user_id, api_key = _register()
    client = make_client()
    enabled = client.post(
        "/v1/debug/trace/enable",
        headers={"X-API-Key": api_key},
        json={"enabled": True},
    )
    assert enabled.status_code == 200

    def fake_envelope(store, _inner, *, item_id=None):
        memory_id = item_id or "memory_t074"
        return {
            "id": memory_id,
            "body_ct": "ciphertext-only",
            "nonce": "nonce",
            "K_user": "wrapped-user-key",
            "K_enclave": "wrapped-enclave-key",
            "visibility": "shared",
            "owner_user_id": store.user_id,
        }, ""

    monkeypatch.setattr(
        memory_actions,
        "_build_memory_envelope_for_store",
        fake_envelope,
    )
    secret = "T074_REAL_ACTION_SECRET_MUST_NOT_REACH_ADMIN"
    raw_content = ("z" * 5011) + secret
    written = client.post(
        "/v1/memory/actions",
        headers={"X-API-Key": api_key},
        json={"actions": [{
            "type": "memory.add",
            "memory": {
                "summary": "Long action card",
                "content": raw_content,
                "source": "chat",
            },
        }]},
    )
    assert written.status_code == 400, written.get_data(as_text=True)
    assert written.get_json()["error"] == "memory_content_too_long"
    assert db.memory_load_strict(user_id) == []

    debug_trace._flush_pending_for_user(user_id)
    status, payload = _asgi_json(
        "GET",
        "/v1/admin/data-track/debug"
        "?q=memory.content.rejected&mode=flat&limit=10",
        headers=_admin(),
    )

    assert status == 200
    assert payload["summary"]["events_total"] == 1
    event = payload["events"][0]
    assert event["type"] == "memory.content.rejected"
    assert event["detail"] == {
        "route": "memory_actions",
        "counts": {
            "max_chars": 5000,
        },
        db.TRACE_OUTCOME_PROVENANCE_FIELD: "missing",
    }
    assert secret not in json.dumps(payload, ensure_ascii=False)


def test_rds_primary_trace_write_recovers_and_debug_endpoint_returns_200(
    env,
    monkeypatch,
):
    """The production-selected RDS pool must carry both trace write and read."""
    assert db.database_schema() == "rds"
    user_id = "usr_t306_" + uuid.uuid4().hex[:16]
    event = {
        "ts": time.time(),
        "subsystem": "agent",
        "type": "agent.t306.rds",
        "status": "ok",
        "actor": "backend",
        "trace_id": "trace-t306-rds",
        "summary": "RDS trace migration regression",
    }
    health_names = (
        "_write_failures_total",
        "_write_consecutive_failures",
        "_write_last_failure_at",
        "_write_last_success_at",
        "_write_last_error",
        "_write_last_error_log_at",
    )
    with debug_trace._write_failure_lock:
        health_before = {
            name: getattr(debug_trace, name) for name in health_names
        }
        debug_trace._write_consecutive_failures = 3
        debug_trace._write_last_error = "undefinedtable"

    monkeypatch.setattr(debug_trace, "is_enabled", lambda _store: True)
    monkeypatch.setattr(debug_trace, "_record_trace_stats", lambda *_a, **_kw: None)
    try:
        debug_trace._append_events(user_id, [event])
        health = debug_trace.trace_storage_health()
        assert health["healthy"] is True
        assert health["consecutive_failures"] == 0
        assert health["last_error"] == ""

        status, payload = _asgi_json(
            "GET",
            "/v1/admin/data-track/debug?user_id=",
            headers=_admin(),
        )
        assert status == 200
        assert payload["summary"]["events_total"] >= 1
        assert any(
            row["trace_id"] == "trace-t306-rds"
            for row in payload["events"]
        )
    finally:
        db.delete_trace_events_for_user(user_id)
        with debug_trace._write_failure_lock:
            for name, value in health_before.items():
                setattr(debug_trace, name, value)


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


def test_events_day_selector_shape_and_invalid_day(env, monkeypatch):
    raw = {
        "proactive": [{
            "route": "model_api", "lane": "heartbeat", "total": 3,
            "success": 2, "failed": 1, "pending": 0, "median_dur": 4.5,
        }],
        "capture": [{
            "route": "resident", "total": 7, "success": 5,
            "failed": 2, "median_dur": 8.0,
        }],
        "genesis": [{
            "route": "resident", "distill": "first", "total": 1,
            "success": 1, "failed": 0,
        }],
        "reply": [{
            "route": "model_api", "user_msgs": 4, "real_replies": 3,
            "fallback_replies": 1, "median_latency": 2.0,
        }],
    }
    seen_days = []

    def fake_overview(*, day, tz="Asia/Shanghai"):
        seen_days.append((day, tz))
        return raw

    monkeypatch.setattr(db, "admin_events_overview", fake_overview)
    frozen = {
        "timezone": "Asia/Shanghai", "closed_through_day": "2035-05-05",
        "windows": [],
    }
    import_overall = {
        "calculated_at": "2035-05-06T00:00:00+00:00",
        "coverage": "red", "reason": "test", "windows": [],
    }
    monkeypatch.setattr(db, "admin_event_path_rollup_windows", lambda **_kw: frozen)
    monkeypatch.setattr(db, "admin_history_import_job_rolling_windows",
                        lambda: import_overall)
    path = "/v1/admin/data-track/events?day=2035-05-06"
    expected = {
        "filters": {"day": "2035-05-06", "timezone": "Asia/Shanghai"},
        "event_path_master": data_track._event_path_master_payload(frozen),
        "history_import_overall": import_overall,
        **raw,
    }
    assert _flask_get_json(path, headers=_admin()) == (200, expected)
    assert _asgi_json("GET", path, headers=_admin()) == (200, expected)
    assert seen_days == [
        ("2035-05-06", "Asia/Shanghai"),
        ("2035-05-06", "Asia/Shanghai"),
    ]

    invalid_path = "/v1/admin/data-track/events?day=2035-02-30"
    assert _flask_get_json(invalid_path, headers=_admin()) == (
        400,
        {"error": "invalid_day"},
    )
    assert _asgi_json("GET", invalid_path, headers=_admin()) == (
        400,
        {"error": "invalid_day"},
    )
    assert len(seen_days) == 2


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
        "failure_code_provenance": "explicit",
        "outcome": "",
        "outcome_reason": "",
        "duration_ms": 289000,
        "provider": "openai",
        "model": "gpt-5.5",
        "memory_card_count_now": 2,
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
    assert second["jobs"][0]["failure_code_provenance"] == "normalized_invalid"
    assert second["jobs"][0]["duration_ms"] == 25000
    rendered = json.dumps([first, second])
    for forbidden in ("prompt", "reply", "content", "body", "NEVER RETURN"):
        assert forbidden not in rendered


def test_dream_job_metadata_separates_skipped_runs_from_real_consolidations(env):
    """A dream that ran but found the garden too small is completed-but-skipped."""
    uid, _key = _register()
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs "
            "(user_id,lane,status,wake_result,wake_result_reason,created_at,finished_at) "
            "VALUES (%s,'dream','completed','skipped','not_enough_new_cards',"
            "'2026-08-13T10:00:00Z','2026-08-13T10:00:01Z'),"
            "(%s,'dream','completed',NULL,NULL,"
            "'2026-08-13T09:00:00Z','2026-08-13T09:00:01Z'),"
            "(%s,'dream','completed','skipped','PRIVATE FREE TEXT',"
            "'2026-08-13T08:00:00Z','2026-08-13T08:00:01Z')",
            (uid, uid, uid),
        )

    status, body = _asgi_json(
        "GET",
        f"/v1/admin/memory-dream-jobs?user_id={uid}&status=completed",
        headers=_admin(),
    )

    assert status == 200
    assert [
        (job["status"], job["outcome"], job["outcome_reason"])
        for job in body["jobs"]
    ] == [
        ("completed", "skipped", "not_enough_new_cards"),
        ("completed", "", ""),
        ("completed", "skipped", ""),
    ]
    assert "PRIVATE FREE TEXT" not in json.dumps(body)


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
            "failure_code_provenance": "explicit",
            "duration_ms": 42,
            "provider": "openai",
            "model": "gpt-5.5",
            "memory_card_count_now": 3,
            "finished_at": "2026-08-13T00:00:01Z",
        }
    )
    assert set(card) == memory_metadata.CARD_FIELDS
    assert set(job) == memory_metadata.DREAM_JOB_FIELDS
    assert "SECRET" not in json.dumps([card, job])

    unmeasured_job = memory_metadata.dream_job_metadata_from_row(
        {
            "job_id": 8,
            "user_id": "usr_safe",
            "status": "failed",
            "failure_code": "runtime_failed",
            "duration_ms": 1,
            "provider": "openai",
            "model": "gpt-5.5",
            "memory_card_count_now": 0,
        }
    )
    assert unmeasured_job["failure_code_provenance"] == "unmeasured"


def test_admin_failure_code_fallback_has_provenance_without_new_public_code(
    env,
    caplog,
):
    uid, _key = _register()
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs "
            "(user_id,lane,status,last_error,created_at,finished_at) VALUES "
            "(%s,'dream','failed','runtime_failed',now()-interval '2 seconds',now()),"
            "(%s,'dream','failed','PRIVATE FREE TEXT',now()-interval '1 second',now())",
            (uid, uid),
        )

    status, body = _asgi_json(
        "GET",
        f"/v1/admin/memory-dream-jobs?user_id={uid}&status=failed&limit=10",
        headers=_admin(),
    )

    assert status == 200
    assert {
        (job["failure_code"], job["failure_code_provenance"])
        for job in body["jobs"]
    } == {
        ("runtime_failed", "explicit"),
        ("runtime_failed", "normalized_invalid"),
    }
    assert "PRIVATE FREE TEXT" not in json.dumps(body)

    with caplog.at_level("WARNING", logger=memory_metadata.log.name):
        assert memory_metadata._safe_failure_code("PRIVATE FREE TEXT") == (
            "runtime_failed"
        )
    assert "field=admin_failure_code fallback=runtime_failed" in caplog.text

    caplog.clear()
    with caplog.at_level("WARNING", logger=memory_metadata.log.name):
        assert memory_metadata._safe_failure_code("runtime_failed") == (
            "runtime_failed"
        )
    assert "field=admin_failure_code" not in caplog.text


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
        ("/v1/admin/data-track/events", "GET"),
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


# --- T653: only the users list gets the 15 s fallback budget; >5 s is never silent ---

def test_users_list_budget_is_15s_and_reaches_the_sql_lease(env, monkeypatch):
    from admin import admin_core, routes_asgi
    assert routes_asgi.DATA_TRACK_USERS_REQUEST_TIMEOUT_SEC == 15.0
    assert routes_asgi.DATA_TRACK_REQUEST_TIMEOUT_SEC == 5.0
    seen = []

    def fake_snapshot(user_ids, **kwargs):
        seen.append(kwargs.get("statement_timeout_ms"))
        return {}

    monkeypatch.setattr(db, "admin_data_track_snapshot", fake_snapshot)
    budgets = []
    real = routes_asgi._run_data_track_db

    async def spy(fn, *args, timeout_seconds=None, **kwargs):
        budgets.append((fn.__name__, timeout_seconds))
        return await real(fn, *args, timeout_seconds=timeout_seconds, **kwargs)

    monkeypatch.setattr(routes_asgi, "_run_data_track_db", spy)
    status, body = _asgi_json("GET", "/v1/admin/data-track/users?limit=5", headers=_admin())
    assert status == 200, body
    status, body = _asgi_json("GET", "/v1/admin/data-track/summary", headers=_admin())
    assert status == 200, body
    assert seen == [15000, None]
    assert budgets == [("users_payload", 15.0), ("summary_payload", None)]
    assert admin_core is not None


def test_users_list_over_5s_is_marked_slow_not_silent(env, monkeypatch, caplog):
    from admin import admin_core

    class _ClockModule:
        """admin_core's own ``time`` binding: every attribute is the real
        module's, except monotonic(), which replays the two readings
        users_payload takes (start, end). The global time module is untouched."""

        def __init__(self, readings):
            self._readings = iter(readings)

        def monotonic(self):
            return next(self._readings)

        def __getattr__(self, name):
            return getattr(time, name)

    monkeypatch.setattr(admin_asgi, "time", _ClockModule([100.0]))
    monkeypatch.setattr(admin_core, "time", _ClockModule([100.0, 106.2]))
    with caplog.at_level("WARNING"):
        status, body = _asgi_json("GET", "/v1/admin/data-track/users?limit=20", headers=_admin())
    assert status == 200, body
    assert body["slow"]["elapsed_ms"] == 6200
    assert body["slow"]["soft_budget_ms"] == 5000
    assert sum(body["slow"]["stages_ms"].values()) == 6200
    assert "[data-track] users slow elapsed_ms=6200 budget_ms=5000 limit=20 total_ms=6200 stages_ms=" in caplog.text
    assert "admin_key" not in caplog.text

    monkeypatch.setattr(admin_asgi, "time", _ClockModule([100.0]))
    monkeypatch.setattr(admin_core, "time", _ClockModule([100.0, 100.1]))
    status, body = _asgi_json("GET", "/v1/admin/data-track/users?limit=20", headers=_admin())
    assert status == 200 and "slow" not in body
    assert time.monotonic is not None and admin_core.time is not time  # global module never patched


class _StageClock:
    """Only the three timing modules see this clock; driver/ASGI clocks stay real."""
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        value = self.now
        self.now += 0.125  # exact binary intervals, independent of machine speed
        return value

    def __getattr__(self, name):
        return getattr(time, name)


def test_users_stages_cover_real_snapshot_page_and_cursor_reads(env, monkeypatch, caplog):
    import admin_read_timing
    import psycopg
    from admin import admin_core
    from model_api_runtime.v2 import jobs_store

    uid, _ = _register()
    # Use the same injection as asgi_app, including its explicit cursor path.
    monkeypatch.setattr(data_track, "_runtime_token_usage_summary", jobs_store.recent_token_usage_summary)
    clock = _StageClock()
    for module in (admin_core, admin_asgi, admin_read_timing):
        monkeypatch.setattr(module, "time", clock)
    executed = []
    fetched = []
    real_execute = psycopg.Cursor.execute
    real_fetchone, real_fetchall = psycopg.Cursor.fetchone, psycopg.Cursor.fetchall

    def execute(cur, query, *args, **kwargs):
        if admin_read_timing._current.get() is not None:
            executed.append(str(query))
        return real_execute(cur, query, *args, **kwargs)

    def fetchone(cur):
        if admin_read_timing._current.get() is not None:
            fetched.append("one")
        return real_fetchone(cur)

    def fetchall(cur):
        if admin_read_timing._current.get() is not None:
            fetched.append("all")
        return real_fetchall(cur)

    monkeypatch.setattr(psycopg.Cursor, "execute", execute)
    monkeypatch.setattr(psycopg.Cursor, "fetchone", fetchone)
    monkeypatch.setattr(psycopg.Cursor, "fetchall", fetchall)
    # The driver spy must exclude calls outside this request's collection.
    with db.get_pool().connection() as outside:
        assert outside.execute("SELECT 42").fetchone() == (42,)
    assert executed == [] and fetched == []
    with caplog.at_level("WARNING"):
        status, body = _asgi_json(
            "GET", "/v1/admin/data-track/users?limit=5&admin_key=credential-canary",
            headers=_admin(),
        )
    assert status == 200, body
    assert [row["user_id"] for row in body["users"]] == [uid]
    assert body["users"][0]["snapshot_read_status"]["level"] == "ok"
    stages = body["slow"]["stages_ms"]
    assert set(stages) == {
        "queue_ms", "pool_wait_ms", "sql_ms", "python_assembly_ms", "unaccounted_ms",
    }
    assert all(isinstance(value, int) and value > 0 for value in stages.values())
    assert sum(stages.values()) == body["slow"]["total_ms"]
    # Each real execute/fetch takes one controlled 125 ms interval. Includes
    # SET/RESET, token cursor, watermarks, fleet and all paged SQL statements.
    assert stages["sql_ms"] == 125 * (len(executed) + len(fetched))
    assert any("memory_moments" in sql for sql in executed)
    assert any("v2_turn_metrics" in sql for sql in executed)
    assert any("lane_rollup_watermark" in sql for sql in executed)
    assert "credential-canary" not in caplog.text and uid not in caplog.text
    assert "SELECT" not in caplog.text and "admin_key" not in caplog.text
    assert str(stages) in caplog.text
    assert admin_read_timing._current.get() is None
    assert db.get_pool() is db._pool  # other requests retain the real pool


def test_users_stages_preserve_pool_exit_and_exception_context(env):
    import admin_read_timing
    from contextlib import contextmanager

    error = ValueError("sentinel")
    seen = []

    class DriverPool:
        @contextmanager
        def connection(self):
            try:
                yield object()
            except ValueError as exc:
                seen.append(exc)
                raise
            finally:
                seen.append("returned")

    with pytest.raises(ValueError, match="sentinel"):
        with admin_read_timing.collect():
            with admin_read_timing.wrap_pool(DriverPool()).connection():
                raise error
    assert seen == [error, "returned"]
    assert admin_read_timing._current.get() is None


def test_users_stages_are_isolated_across_nested_collections(env, monkeypatch):
    import admin_read_timing
    monkeypatch.setattr(admin_read_timing, "time", _StageClock())
    with admin_read_timing.collect() as outer:
        with outer.stage("sql_ms"):
            pass
        with admin_read_timing.collect() as inner:
            with inner.stage("pool_wait_ms"):
                pass
        assert admin_read_timing._current.get() is outer
    assert outer.seconds == {"sql_ms": 0.125, "pool_wait_ms": 0, "python_assembly_ms": 0}
    assert inner.seconds == {"sql_ms": 0, "pool_wait_ms": 0.125, "python_assembly_ms": 0}
    assert admin_read_timing._current.get() is None


@pytest.mark.parametrize("queue_s,total_s,slow", [(0, 5, False), (0, 5.125, True), (6, 6.25, True)])
def test_users_slow_threshold_and_pre_thread_wait(env, monkeypatch, queue_s, total_s, slow):
    import admin_read_timing
    from admin import admin_core
    from types import SimpleNamespace

    readings = iter((100 + queue_s, 100 + total_s))
    monkeypatch.setattr(admin_core, "time", SimpleNamespace(monotonic=lambda: next(readings)))
    monkeypatch.setattr(admin_asgi, "time", SimpleNamespace(monotonic=lambda: 100))
    # No elapsed time inside the payload in this boundary test; its real SQL
    # path is separately timed against PostgreSQL above.
    monkeypatch.setattr(admin_read_timing, "time", SimpleNamespace(monotonic=lambda: 100 + queue_s))
    status, body = _asgi_json("GET", "/v1/admin/data-track/users?limit=5", headers=_admin())
    assert status == 200, body
    assert ("slow" in body) is slow
    if slow:
        assert body["slow"]["total_ms"] == int(total_s * 1000)
        assert body["slow"]["elapsed_ms"] == int((total_s - queue_s) * 1000)
        assert body["slow"]["stages_ms"]["queue_ms"] == int(queue_s * 1000)
        assert sum(body["slow"]["stages_ms"].values()) == int(total_s * 1000)


def test_users_timing_collectors_do_not_cross_threads(env):
    import admin_read_timing
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(2)
    raw_pool = db.get_pool()

    def work():
        assert admin_read_timing._current.get() is None
        with admin_read_timing.collect() as timings:
            barrier.wait(timeout=5)
            pool = db.get_pool()
            assert pool._pool is raw_pool
            assert pool._timings is timings
            with pool.connection() as conn:
                assert conn.execute("SELECT 1").fetchone() == (1,)
            barrier.wait(timeout=5)
            assert admin_read_timing._current.get() is timings
        assert admin_read_timing._current.get() is None
        return timings

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(work)
        second = executor.submit(work)
        left, right = first.result(timeout=10), second.result(timeout=10)
    assert left is not right
    assert left.seconds["sql_ms"] > 0 and right.seconds["sql_ms"] > 0


def test_users_timing_does_not_leak_when_worker_thread_is_reused(env, monkeypatch):
    import admin_read_timing
    from admin import admin_core
    from concurrent.futures import ThreadPoolExecutor

    raw_pool = db.get_pool()
    real_get_pool = db.get_pool
    seen = []

    def observe_pool():
        pool = real_get_pool()
        seen.append(pool)
        return pool

    monkeypatch.setattr(db, "get_pool", observe_pool)

    def users():
        admin_core.users_payload("limit=5")
        assert admin_read_timing._current.get() is None
        return threading.get_ident()

    def summary():
        assert db.get_pool() is raw_pool
        admin_core.summary_payload("")
        assert admin_read_timing._current.get() is None
        return threading.get_ident()

    with ThreadPoolExecutor(max_workers=1) as executor:
        users_thread = executor.submit(users).result(timeout=5)
        assert seen and all(pool is not raw_pool for pool in seen)
        seen.clear()
        summary_thread = executor.submit(summary).result(timeout=5)
    assert summary_thread == users_thread
    assert seen and all(pool is raw_pool for pool in seen)


def test_users_sql_timing_rethrows_same_driver_exception(env, monkeypatch):
    import admin_read_timing
    import psycopg
    error = QueryCanceled("driver-error-canary")
    monkeypatch.setattr(admin_read_timing, "time", _StageClock())

    def fail(_conn, *args, **kwargs):
        raise error

    monkeypatch.setattr(psycopg.Connection, "execute", fail)
    with admin_read_timing.collect() as timings:
        with pytest.raises(QueryCanceled) as caught:
            with db.get_pool().connection() as conn:
                conn.execute("SELECT 1")
        assert caught.value is error
        assert timings.seconds["sql_ms"] == 0.125
        assert timings.seconds["pool_wait_ms"] == 0.125
        assert timings.active is None
    assert admin_read_timing._current.get() is None


@pytest.mark.parametrize("api", ["connection_iterator", "cursor_iterator", "fetchmany"])
def test_users_timed_cursor_iteration_and_batches_use_real_driver(env, monkeypatch, api):
    import admin_read_timing

    clock = _StageClock()
    monkeypatch.setattr(admin_read_timing, "time", clock)
    with admin_read_timing.collect() as timings:
        with db.get_pool().connection() as conn:
            with conn.cursor() as explicit_cursor:
                if api == "connection_iterator":
                    cursor = conn.execute("SELECT generate_series(1, 3)")
                else:
                    cursor = explicit_cursor.execute("SELECT generate_series(1, 3)")
                before = timings.seconds["sql_ms"]
                assert before == 0.125
                if api == "fetchmany":
                    assert cursor.fetchmany(size=2) == [(1,), (2,)]
                    assert cursor.fetchmany() == [(3,)]
                    assert cursor.fetchmany(2) == []
                    expected_steps = 3
                else:
                    rows = []
                    with timings.stage("python_assembly_ms"):
                        for row in cursor:
                            rows.append(row)
                            clock.now += 1  # consumer work must not count as SQL
                    assert rows == [(1,), (2,), (3,)]
                    expected_steps = 4  # three rows + StopIteration
                assert timings.seconds["sql_ms"] - before == 0.125 * expected_steps
        assert timings.active is None
    assert admin_read_timing._current.get() is None
