"""T627: real PostgreSQL aggregation and the admin ASGI contract."""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import contextmanager
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from psycopg.errors import QueryCanceled

sys.path.insert(0, str(Path(__file__).parents[1] / "backend"))
import db
import enclave_health_contract as contract
from admin import routes_asgi
from asgi import middleware

NOW = datetime.now(timezone.utc).replace(minute=45, second=0, microsecond=0)
TOKEN = "test-admin-enclave"
PATH = "/v1/admin/enclave-decrypt-health"


@pytest.fixture(params=["rds", "tee"])
def clean_traces(request, monkeypatch):
    # conftest owns disposable databases, never live environments.
    original_url = os.environ["DATABASE_URL"]
    original_schema = os.environ.get("FEEDLING_DATABASE_SCHEMA", "rds")
    if request.param == "tee":
        db.close_pool()
        monkeypatch.setenv("DATABASE_URL", os.environ["TEE_DATABASE_URL"])
        monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "tee")
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM trace_events")
    yield
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM trace_events")
    if request.param == "tee":
        db.close_pool()
        monkeypatch.setenv("DATABASE_URL", original_url)
        monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", original_schema)


def insert(kind, *, when=None, uid="usr_private_a", purpose="memory_action", detail=None, subsystem="enclave", count=1):
    event = {"ts": (when or NOW - timedelta(minutes=1)).timestamp(),
             "subsystem": subsystem, "type": f"enclave.call.{kind}",
             "summary": "DO_NOT_EXPORT_USER_CONTENT", "detail": {
                 "purpose": purpose, "raw_secret": "DO_NOT_EXPORT_TOKEN", **(detail or {})}}
    assert db.insert_trace_events_strict(uid, [event] * count) == count


@pytest.mark.parametrize("minute,second,end_minute", [(14,59,0),(15,0,15),(29,59,15),(30,0,30),(45,1,45)])
def test_wall_clock_complete_buckets(minute, second, end_minute):
    now = NOW.replace(minute=minute, second=second)
    previous, current, end = db.enclave_decrypt_health_windows(15, now=now)
    assert end == now.replace(minute=end_minute, second=0)
    assert current == end - timedelta(minutes=15)
    assert previous == end - timedelta(minutes=30)


@pytest.mark.parametrize("window", [0,1441,-1,1.5,True,"15"])
def test_db_rejects_invalid_windows(window):
    with pytest.raises(ValueError):
        db.admin_enclave_decrypt_health(window, now=NOW)


def test_db_counts_both_windows_with_closed_projection(clean_traces):
    for minute in (1,16):
        when = NOW - timedelta(minutes=minute)
        insert("done", when=when, count=10)
        insert("timeout", when=when, count=3)
        insert("error", when=when, uid="usr_private_b", detail={"failure_class":"enclave_transport_error"}, count=2)
        insert("error", when=when, detail={"status_code":401}, count=4)
        insert("error", when=when, detail={"status_code":"403"}, count=5)
        insert("error", when=when, detail={"status_code":500}, count=6)
        insert("error", when=when, detail={"failure_class":"enclave_invalid_response"})
        insert("start", when=when, count=50)
        insert("batch", when=when, count=50)
        insert("done", when=when, subsystem="another", count=50)
    result = db.admin_enclave_decrypt_health(15, now=NOW)
    assert set(result) == contract.PAYLOAD_KEYS
    assert result["calculated_at"] == NOW.isoformat()
    for period in ("current", "previous"):
        row = result[period]
        assert set(row) == contract.WINDOW_KEYS
        assert {key: row[key] for key in contract.COUNT_KEYS} == {
            "done":10,"timeout":3,"transport_error":2,"http_401":4,"http_403":5,
            "http_other":7,"calls":31,"unavailable":5,"users_affected":2,
        }
        assert row["unavailable_rate"] == 5 / 15
        assert row["top_purposes"] == [{"purpose":"memory_action","count":21}]
    rendered = json.dumps(result)
    for secret in ("usr_private", "DO_NOT_EXPORT", "raw_secret", "summary", "detail"):
        assert secret not in rendered


def test_exact_boundaries_belong_to_the_completed_bucket(clean_traces):
    insert("timeout", when=NOW)
    insert("done", when=NOW - timedelta(minutes=15))
    insert("error", when=NOW - timedelta(minutes=30))
    insert("error", when=NOW + timedelta(microseconds=1))
    result = db.admin_enclave_decrypt_health(15, now=NOW + timedelta(minutes=4))
    assert result["current"]["timeout"] == 1
    assert result["current"]["calls"] == 1
    assert result["previous"]["done"] == 1
    assert result["previous"]["calls"] == 1


def test_six_hour_403_spike_does_not_raise_unavailability(clean_traces):
    insert("done", count=24)
    # Explicit HTTP status wins even if a malformed producer also labels transport.
    insert("error", detail={"status_code":403,"failure_class":"enclave_transport_error"}, count=56)
    insert("error", detail={"status_code":401}, count=56)
    row = db.admin_enclave_decrypt_health(15, now=NOW)["current"]
    assert row["calls"] == 136
    assert row["http_401"] == row["http_403"] == 56
    assert row["unavailable"] == 0
    assert row["unavailable_rate"] == 0


def test_http_only_windows_have_unknown_rate_and_count_affected_users(clean_traces):
    # Unlike an empty GROUP BY result, these rows exercise the computed-rate
    # branch: the observed HTTP errors leave its availability denominator zero.
    for minute in (1, 16):
        when = NOW - timedelta(minutes=minute)
        insert("error", when=when, uid="usr_http_a", detail={"status_code": 401}, count=4)
        insert("error", when=when, uid="usr_http_b", detail={"status_code": 403}, count=56)
        insert("error", when=when, uid="usr_http_b", detail={"status_code": 500}, count=2)
    result = db.admin_enclave_decrypt_health(15, now=NOW)
    for period in ("current", "previous"):
        row = result[period]
        assert {key: row[key] for key in contract.COUNT_KEYS} == {
            "done": 0, "timeout": 0, "transport_error": 0,
            "http_401": 4, "http_403": 56, "http_other": 2,
            "calls": 62, "unavailable": 0, "users_affected": 2,
        }
        assert row["unavailable_rate"] is None
        assert row["top_purposes"] == [{"purpose": "memory_action", "count": 62}]
    # This exact DB output must also be consumable without a false unmeasured
    # alert; a numeric zero here would be rejected by the reporter contract.
    from tools import enclave_decrypt_alert
    enclave_decrypt_alert.validate_health(result, 15)
    assert enclave_decrypt_alert.alert_state(result, run_minute=0) == "静默"


def test_top_five_failure_purposes_are_sorted_safe_labels(clean_traces):
    for count, purpose in enumerate(("memory_action","perception:weather","screen_frame_image",
                                     "identity_get","genesis_chunk","genesis_voice"), 1):
        insert("timeout", purpose=purpose, count=count)
    for purpose in ("usr_secret_123", "sk-secret", "private text", {"token":"private"}):
        insert("error", purpose=purpose, count=2)
    insert("done", purpose="model_api_provider_key", count=500)
    row = db.admin_enclave_decrypt_health(15, now=NOW)["current"]
    assert row["top_purposes"] == [
        {"purpose":"other","count":8}, {"purpose":"genesis_voice","count":6},
        {"purpose":"genesis_chunk","count":5}, {"purpose":"identity_get","count":4},
        {"purpose":"screen_frame_image","count":3},
    ]
    assert row["users_affected"] == 1


def test_empty_windows_have_unknown_rate(clean_traces):
    result = db.admin_enclave_decrypt_health(15, now=NOW)
    for row in (result["current"],result["previous"]):
        assert all(row[k] == 0 for k in contract.COUNT_KEYS)
        assert row["unavailable_rate"] is None
        assert row["top_purposes"] == []


def test_perception_label_projection_covers_current_encrypted_signal_vocabulary():
    from perception import ios_contract_v2
    assert {f"perception:{key}" for key in ios_contract_v2.ENCRYPTED_SIGNAL_KEYS_V2} <= contract.PURPOSE_LABELS


app = FastAPI()
middleware.register_exception_handlers(app)
routes_asgi.register_asgi(app)


def get(params=None, token=TOKEN):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            return await client.get(PATH, params=params, headers={"X-Admin-Token":token} if token else {})
    return asyncio.run(run())


@pytest.fixture
def admin_env(monkeypatch):
    monkeypatch.setenv("FEEDLING_ADMIN_TOKEN", TOKEN)


def test_route_requires_configured_admin(admin_env, monkeypatch):
    assert get(token=None).status_code == 401
    assert get(token="wrong").status_code == 401
    monkeypatch.delenv("FEEDLING_ADMIN_TOKEN")
    assert get().status_code == 503


@pytest.mark.parametrize("params", [
    {"user_id":"someone"}, {"window_minutes":"0"}, {"window_minutes":"1441"},
    {"window_minutes":"NaN"}, {"window_minutes":""}, {"window_minutes":"1.5"},
    {"end_epoch":"nan"}, {"end_epoch":"inf"}, {"end_epoch":"-1"},
    {"end_epoch":"1e100"}, {"end_epoch":"bad"},
    [("window_minutes","15"),("window_minutes","30")],
    [("end_epoch","1"),("end_epoch","2")],
])
def test_route_rejects_invalid_params_before_db(admin_env, monkeypatch, params):
    def forbidden(*a, **kw):
        pytest.fail("invalid request reached DB")
    monkeypatch.setattr(db, "admin_enclave_decrypt_health", forbidden)
    assert get(params).status_code == 400


def test_route_real_db_response_has_exact_keys(admin_env, clean_traces):
    insert("timeout")
    response = get({"end_epoch":NOW.timestamp()})
    assert response.status_code == 200
    assert response.json() == db.admin_enclave_decrypt_health(15, now=NOW)
    assert set(response.json()) == {"window_minutes","calculated_at","current","previous"}
    assert response.json()["current"]["timeout"] == 1
    from tools import enclave_decrypt_alert
    enclave_decrypt_alert.validate_health(response.json(), 15)


@pytest.mark.parametrize("window", [1,1440])
def test_route_passes_valid_limits_and_end_epoch(admin_env, monkeypatch, window):
    seen = []
    monkeypatch.setattr(db, "admin_enclave_decrypt_health", lambda *a, **kw: seen.append((a,kw)) or {})
    assert get({"window_minutes":window,"end_epoch":NOW.timestamp()}).status_code == 200
    assert seen == [((window,), {"now":NOW})]


def test_query_timeout_is_unavailable_not_zero(admin_env, monkeypatch):
    def timeout(*a, **kw):
        raise QueryCanceled("private query detail")
    monkeypatch.setattr(db, "admin_enclave_decrypt_health", timeout)
    response = get()
    assert response.status_code == 503
    assert "private query" not in response.text


def test_statement_timeout_is_live_and_transaction_local(clean_traces, monkeypatch):
    pool = db.get_pool()
    observed = []

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def transaction(self):
            return self.conn.transaction()

        def execute(self, query, *args):
            if "WITH events AS" in query:
                observed.append(self.conn.execute("SHOW statement_timeout").fetchone()[0])
            return self.conn.execute(query, *args)

    class Pool:
        @contextmanager
        def connection(self, **kwargs):
            with pool.connection(**kwargs) as conn:
                original = conn.execute("SHOW statement_timeout").fetchone()[0]
                yield Connection(conn)
                assert conn.execute("SHOW statement_timeout").fetchone()[0] == original

    with monkeypatch.context() as patch:
        patch.setattr(db, "get_pool", lambda: Pool())
        db.admin_enclave_decrypt_health(15, now=NOW)
    assert observed == ["5s"]
