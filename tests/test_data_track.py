from __future__ import annotations

import base64
import contextlib
from datetime import datetime, timedelta
import itertools
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from memory import service as memory_service  # noqa: E402
from proactive import service as proactive_service  # noqa: E402
from tracking import tracking_core  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setenv("FEEDLING_ADMIN_TOKEN", "admin-test-token")
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    with make_client() as c:
        yield c


_pk_counter = itertools.count(1)


def _register(client) -> tuple[str, str]:
    # Distinct public_key per call: the register endpoint now refuses duplicate
    # content keys (orphan backstop), and these tests need many distinct users.
    raw = next(_pk_counter).to_bytes(32, "big")
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(raw), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def _admin_headers() -> dict[str, str]:
    return {"X-Admin-Token": "admin-test-token"}


def _env(msg_id: str, user_id: str) -> dict:
    return {
        "id": msg_id,
        "v": 1,
        "body_ct": "ciphertext-that-must-not-leak",
        "nonce": "nonce-that-must-not-leak",
        "K_user": "wrapped-user-key",
        "K_enclave": "wrapped-enclave-key",
        "visibility": "shared",
        "owner_user_id": user_id,
    }


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _append_chat_at(user_id: str, msg_id: str, role: str, source: str, ts: float) -> None:
    doc = {
        **_env(msg_id, user_id),
        "role": role,
        "source": source,
        "ts": ts,
    }
    db.chat_append(user_id, msg_id, ts, doc, core_store.MAX_CHAT_MESSAGES)


def test_track_event_scrubs_sensitive_payload(client):
    user_id, api_key = _register(client)

    res = client.post(
        "/v1/track/event",
        headers=_headers(api_key),
        json={
            "type": "onboarding_skill_copied",
            "route": "resident",
            "app_version": "1.0",
            "build": "42",
            "payload": {
                "screen": "chat_empty",
                "characters": 123,
                "prompt": "private prompt",
                "api_key": "sk-private",
                "file_name": "private.txt",
                "nested": {"step": "skill", "token": "private-token"},
            },
        },
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    events = core_store.get_store(user_id).list_tracking_events(limit=0)
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["screen"] == "chat_empty"
    assert payload["characters"] == 123
    assert payload["nested"] == {"step": "skill"}
    assert "prompt" not in payload
    assert "api_key" not in payload
    assert "file_name" not in payload
    assert "private" not in json.dumps(events[0])


def test_admin_data_track_requires_admin_token(client, monkeypatch):
    _register(client)

    no_token = client.get("/v1/admin/data-track/users")
    assert no_token.status_code == 401

    good = client.get("/v1/admin/data-track/users", headers=_admin_headers())
    assert good.status_code == 200

    monkeypatch.delenv("FEEDLING_ADMIN_TOKEN")
    disabled = client.get("/v1/admin/data-track/users", headers=_admin_headers())
    assert disabled.status_code == 503


def test_admin_data_track_aggregates_counts_without_content(client):
    user_id, api_key = _register(client)
    store = core_store.get_store(user_id)

    store.append_chat("user", "chat", _env("msg_user_1", user_id))
    store.append_chat("openclaw", "chat", _env("msg_agent_1", user_id))
    store.append_chat(
        "openclaw",
        proactive_service.PROACTIVE_JOB_SOURCE,
        _env("msg_proactive_1", user_id),
        extra={
            "proactive_job_id": "pj_1",
            "live_activity_status": "delivered",
            "alert_status": "delivered",
            "alert_preview": "private alert preview",
        },
    )
    memory_service._save_moments(
        store,
        [
            {"id": "mem_1", "type": "moment", "source": "bootstrap", "created_at": "2026-06-01T01:00:00"},
            {"id": "mem_2", "type": "fact", "source": "chat", "created_at": "2026-06-01T02:00:00"},
        ],
    )
    db.set_blob(store.user_id, "identity", {
        "updated_at": "2026-06-01T03:00:00",
        "relationship_started_at": "2026-06-01",
        "relationship_anchor_evidence": "private evidence",
    })
    store.append_tracking_event(tracking_core._make_tracking_event(
        store,
        "onboarding_connection_copied",
        {"payload": {"screen": "chat_empty", "prompt": "private copied prompt"}},
    ))
    store.append_proactive_job({
        "job_id": "pj_failed_timeout",
        "status": "failed",
        "status_reason": "model_timeout",
        "job_kind": "presence",
    })
    store.append_proactive_job({
        "job_id": "pj_failed_unknown",
        "status": "failed",
        "job_kind": "scheduled_wake",
    })

    res = client.get("/v1/admin/data-track/users", headers=_admin_headers())

    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["summary"]["users_total"] == 1
    assert body["summary"]["chat_messages_total"] == 3
    assert body["summary"]["memory_total"] == 2
    row = body["users"][0]
    assert row["chat"]["total"] == 3
    assert row["chat"]["user_messages"] == 1
    assert row["chat"]["agent_messages"] == 2
    assert row["memory"]["by_tab"]["story"] == 1
    assert row["memory"]["by_tab"]["about_me"] == 1
    assert row["proactive"]["proactive_messages"] == 1
    # The users index intentionally skips the whole-history background
    # breakdown; that legacy evidence remains available on the one-user detail.
    detail = client.get(
        f"/v1/admin/data-track/users/{user_id}", headers=_admin_headers()
    ).get_json()["user"]
    assert detail["proactive"]["job_failed_reasons"] == {
        "model_timeout": 1,
        "unknown": 1,
    }
    dumped = json.dumps(body)
    assert "ciphertext-that-must-not-leak" not in dumped
    assert "private alert preview" not in dumped
    assert "private copied prompt" not in dumped
    assert "private evidence" not in dumped


def test_users_surface_separates_v1_failure_control_and_user_unavailable(client):
    user_id, _ = _register(client)
    store = core_store.get_store(user_id)
    for job in (
        {
            "job_id": "pj_ok",
            "status": "completed",
            "status_reason": "agent_sleep",
            "job_kind": "heartbeat",
        },
        {
            "job_id": "pj_throttled",
            "status": "skipped",
            "status_reason": "heartbeat_throttled",
            "job_kind": "heartbeat",
        },
        {
            "job_id": "pj_unknown",
            "status": "failed",
            "status_reason": "unknown",
            "job_kind": "heartbeat",
        },
        {
            "job_id": "pj_user",
            "status": "failed",
            "status_reason": "quota_insufficient",
            "job_kind": "heartbeat",
        },
    ):
        store.append_proactive_job(job)

    response = client.get(
        f"/v1/admin/data-track/users/{user_id}", headers=_admin_headers()
    )
    assert response.status_code == 200
    proactive = response.get_json()["user"]["proactive"]
    assert proactive["lens"] == "v1_proactive_jobs_log"
    assert proactive["failure_definition"]["window"] == "all_history"
    assert proactive["heartbeat_jobs"] == 4
    assert proactive["heartbeat_failed"] == 1
    assert proactive["heartbeat_control"] == 1
    assert proactive["heartbeat_user_unavailable"] == 1
    assert proactive["job_failed_reasons"] == {"unknown": 1}
    assert proactive["job_control_reasons"] == {"heartbeat_throttled": 1}
    assert proactive["job_user_unavailable_reasons"] == {
        "quota_insufficient": 1
    }

    page = client.get("/admin/data-track?view=users", headers=_admin_headers())
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "后台道失败率（按用户）" in body
    assert "冻结" in body
    assert "chat.last_user_at" in body


def test_users_background_lanes_filter_by_last_user_message_and_skip_full_history(
        client, monkeypatch):
    active_user, _ = _register(client)
    inactive_user, _ = _register(client)
    active_store = core_store.get_store(active_user)
    active_store.append_chat(
        "user", "chat", _env("human-active-now", active_user)
    )
    active_store.append_proactive_job({
        "job_id": "legacy-breakdown-must-not-be-read",
        "status": "failed",
        "status_reason": "legacy_provider_timeout",
        "job_kind": "heartbeat",
    })
    active_store.append_gate_decision({
        "decision_id": "legacy-gate-json-must-not-be-read",
        "should_reach_out": True,
    })
    active_store.append_tracking_event(tracking_core._make_tracking_event(
        active_store,
        "legacy-tracking-json-must-not-be-read",
        {"payload": {}},
    ))
    yesterday = (
        datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
    ).isoformat()
    with db.get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO lane_daily_rollup
              (user_id, day, route, lane, completed, failed,
               operational_failures, control_outcomes, user_unavailable,
               failure_codes)
            VALUES
              (%s, %s, 'resident', 'heartbeat', 3, 4, 1, 2, 1,
               '{"wake_failed:providererror":1}'::jsonb),
              (%s, %s, 'resident', 'capture', 0, 2, 2, 0, 0,
               '{"extraction_failed:pooltimeout":2}'::jsonb),
              (%s, %s, 'resident', 'heartbeat', 0, 5, 5, 0, 0,
               '{"wake_failed:providererror":5}'::jsonb)
            """,
            (
                active_user, yesterday,
                active_user, yesterday,
                inactive_user, yesterday,
            ),
        )
        conn.execute(
            """
            INSERT INTO lane_rollup_watermark
              (route, backfill_from, through_day, outcomes_from)
            VALUES ('resident', %s, %s, %s)
            ON CONFLICT (route) DO UPDATE SET
              backfill_from=EXCLUDED.backfill_from,
              through_day=EXCLUDED.through_day,
              outcomes_from=EXCLUDED.outcomes_from
            """,
            (yesterday, yesterday, yesterday),
        )

    real_snapshot = db.admin_data_track_snapshot
    snapshot_modes = []

    def observed_snapshot(user_ids, **kwargs):
        snapshot_modes.append(kwargs.get("include_legacy_background"))
        return real_snapshot(user_ids, **kwargs)

    monkeypatch.setattr(db, "admin_data_track_snapshot", observed_snapshot)
    active = client.get(
        "/v1/admin/data-track/users?human_activity=active&human_days=7&lane_days=1",
        headers=_admin_headers(),
    )
    assert active.status_code == 200, active.get_data(as_text=True)
    active_body = active.get_json()
    assert [row["user_id"] for row in active_body["users"]] == [active_user]
    assert active_body["filters"]["human_activity"] == "active"
    assert active_body["filters"]["human_days"] == 7
    assert active_body["filters"]["lane_days"] == 1
    assert snapshot_modes[-1] is False, (
        "users index must not reopen the legacy whole-history background scans"
    )

    row = active_body["users"][0]
    assert row["human_activity"] == {
        "state": "active",
        "basis": "chat.last_user_at",
        "days": 7,
        "last_user_at": row["chat"]["last_user_at"],
    }
    heartbeat = row["background_lanes"]["lanes"]["heartbeat"]
    capture = row["background_lanes"]["lanes"]["capture"]
    assert row["proactive"]["breakdowns_status"] == "omitted"
    assert row["proactive"]["job_failed_reasons"] == {}
    assert row["proactive"]["decisions"] == 1
    assert row["proactive"]["decision_true"] == 0
    assert row["memory"]["capture_breakdowns_status"] == "omitted"
    assert row["tracking"]["events"] == 1
    assert row["tracking"]["by_type"] == {}
    assert row["tracking"]["breakdowns_status"] == "omitted"
    assert row["background_lanes"]["coverage_route"] == "resident"
    assert row["background_lanes"]["coverage_routes"] == ["resident"]
    assert row["background_lanes"]["coverage"]["level"] == "green"
    assert heartbeat["terminal_attempts"] == 4
    assert heartbeat["failure_rate"] == pytest.approx(0.25)
    assert capture["terminal_attempts"] == 2
    assert capture["failure_rate"] == 1.0

    inactive = client.get(
        "/v1/admin/data-track/users?human_activity=inactive&human_days=7&lane_days=1",
        headers=_admin_headers(),
    ).get_json()
    assert [row["user_id"] for row in inactive["users"]] == [inactive_user]
    assert inactive["users"][0]["human_activity"]["state"] == "inactive"
    assert inactive["users"][0]["background_lanes"]["lanes"]["heartbeat"][
        "failure_rate"
    ] == 1.0

    page = client.get(
        "/admin/data-track?view=users&human_activity=active&human_days=7&lane_days=1",
        headers=_admin_headers(),
    )
    rendered = page.get_data(as_text=True)
    assert page.status_code == 200, rendered
    assert "后台道失败率（按用户）" in rendered
    assert "chat.last_user_at" in rendered
    assert "心跳 25%（成3/故1/分母4）" in rendered
    assert "capture 100%（成0/故2/分母2）" in rendered
    assert inactive_user not in rendered

    summary = client.get(
        "/v1/admin/data-track/summary", headers=_admin_headers()
    )
    assert summary.status_code == 200, summary.get_data(as_text=True)
    summary_body = summary.get_json()["summary"]
    assert snapshot_modes[-1] is False, (
        "fleet-wide summary must not reopen legacy JSON breakdown scans"
    )
    assert summary_body["proactive_breakdowns_status"] == "omitted"
    assert summary_body["proactive_failed_total"] is None


def _seed_lane_users(client, count: int) -> list[str]:
    """Register ``count`` users with distinct chat volume and lane counts.

    The Nth seeded user gets N+1 chat messages and a lane cell with
    completed=(N+1)*10, so ``sort=chat`` is deterministic and a row carrying a
    neighbour's lane data cannot coincidentally look right.
    """
    yesterday = (
        datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
    ).isoformat()
    user_ids = []
    for index in range(count):
        uid, _ = _register(client)
        store = core_store.get_store(uid)
        for turn in range(index + 1):
            store.append_chat("user", "chat", _env(f"human-{index}-{turn}", uid))
        user_ids.append(uid)
    with db.get_pool().connection() as conn:
        for index, uid in enumerate(user_ids):
            conn.execute(
                """
                INSERT INTO lane_daily_rollup
                  (user_id, day, route, lane, completed, failed,
                   operational_failures, control_outcomes, user_unavailable,
                   failure_codes)
                VALUES (%s, %s, 'resident', 'heartbeat', %s, 0, 0, 0, 0,
                        '{}'::jsonb)
                """,
                (uid, yesterday, (index + 1) * 10),
            )
        conn.execute(
            """
            INSERT INTO lane_rollup_watermark
              (route, backfill_from, through_day, outcomes_from)
            VALUES ('resident', %s, %s, %s)
            ON CONFLICT (route) DO UPDATE SET
              backfill_from=EXCLUDED.backfill_from,
              through_day=EXCLUDED.through_day,
              outcomes_from=EXCLUDED.outcomes_from
            """,
            (yesterday, yesterday, yesterday),
        )
    return user_ids


def _observe_lane_reader(monkeypatch):
    """Record every user_ids list handed to the per-user lane reader."""
    real = db.admin_background_lane_users
    seen: list[list[str]] = []

    def observed(user_ids, **kwargs):
        seen.append(list(user_ids))
        return real(user_ids, **kwargs)

    monkeypatch.setattr(db, "admin_background_lane_users", observed)
    return seen


def test_lane_reader_receives_exactly_the_page_ids_in_order(client, monkeypatch):
    seeded = _seed_lane_users(client, 5)
    seen = _observe_lane_reader(monkeypatch)

    # Non-zero offset with a sort the production allowlist actually accepts
    # (_data_track_request_filters keeps only chat/memory/proactive), so the
    # ordering asserted here is the ordering the endpoint really applies. The
    # correct page is neither the first N ids nor the previous page, so a
    # length-only assertion would not catch handing the reader the wrong slice.
    body = client.get(
        "/v1/admin/data-track/users"
        "?sort=chat&dir=asc&limit=2&offset=2&lane_days=1",
        headers=_admin_headers(),
    ).get_json()

    page_ids = [row["user_id"] for row in body["users"]]
    assert len(page_ids) == 2
    assert body["pagination"] == {
        "limit": 2, "offset": 2, "returned": 2, "total": 5,
        "next_offset": 4, "prev_offset": 0,
    }
    assert seen == [page_ids], (
        "the per-user lane scan must receive exactly the returned page, "
        f"in order; got {seen} for page {page_ids}"
    )

    # sort=chat&dir=asc orders by chat volume, which _seed_lane_users made
    # strictly increasing in seed order, so page[offset=2] is seeds 2 and 3.
    assert page_ids == seeded[2:4]

    # Each returned row must carry its own lane data, not a neighbour's:
    # the Nth seeded user has completed=(N+1)*10, a different scale from the
    # chat counts so the two cannot be confused for each other.
    for row in body["users"]:
        expected = (seeded.index(row["user_id"]) + 1) * 10
        assert row["background_lanes"]["lanes"]["heartbeat"]["completed"] == expected


def test_empty_page_keeps_fleet_level_lane_window(client, monkeypatch):
    _seed_lane_users(client, 3)
    seen = _observe_lane_reader(monkeypatch)

    full = client.get(
        "/v1/admin/data-track/users?limit=100&lane_days=1",
        headers=_admin_headers(),
    ).get_json()
    empty = client.get(
        "/v1/admin/data-track/users?limit=2&offset=99&lane_days=1",
        headers=_admin_headers(),
    ).get_json()

    assert empty["users"] == []
    # This only observes the argument handed to the reader. Whether the reader
    # then issues the lane SQL is a separate claim, guarded at the DB seam by
    # test_empty_ids_reads_watermark_without_scanning_lane_rows.
    assert seen[-1] == [], "an empty page must hand the reader no user ids"
    # window / read_status / coverage come from the ids-independent watermark
    # read, so they must survive a page that selects nobody.
    for field in ("window", "read_status", "coverage_by_route"):
        assert empty["background_lane_window"][field] == \
            full["background_lane_window"][field], field
    assert empty["background_lane_window"]["read_status"]["level"] == "ok"
    assert empty["background_lane_window"]["coverage_by_route"]["resident"][
        "level"
    ] == "green"


class _RecordingConn:
    """Delegates to a real connection while recording every SQL statement."""

    def __init__(self, inner, executed: list[str]):
        self._inner = inner
        self._executed = executed

    def execute(self, sql, params=None, *args, **kwargs):
        self._executed.append(str(sql))
        if params is None:
            return self._inner.execute(sql, *args, **kwargs)
        return self._inner.execute(sql, params, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_empty_ids_reads_watermark_without_scanning_lane_rows(client, monkeypatch):
    _seed_lane_users(client, 2)
    executed: list[str] = []
    real_connection = db._admin_data_track_connection

    @contextlib.contextmanager
    def recording_connection(*args, **kwargs):
        with real_connection(*args, **kwargs) as conn:
            yield _RecordingConn(conn, executed)

    monkeypatch.setattr(db, "_admin_data_track_connection", recording_connection)
    report = db.admin_background_lane_users([], days=1)

    lane_sql = [sql for sql in executed if "lane_daily_rollup" in sql]
    watermark_sql = [sql for sql in executed if "lane_rollup_watermark" in sql]
    assert lane_sql == [], (
        "no page ids means nothing to scan: the per-user lane query must not "
        "be issued at all"
    )
    # ...and the ids-independent watermark read must still happen, otherwise
    # coverage silently degrades to 'unavailable' on an empty page.
    assert len(watermark_sql) == 1
    assert report["users"] == {}
    assert report["read_status"]["level"] == "ok"
    assert report["coverage_by_route"]["resident"]["level"] == "green"


def test_summary_aggregates_ignore_pagination_window(client):
    _seed_lane_users(client, 4)

    def _summary(query: str) -> dict:
        body = client.get(
            f"/v1/admin/data-track/users?{query}", headers=_admin_headers()
        ).get_json()
        summary = dict(body["summary"])
        # generated_at is wall-clock; everything else must be pagination-blind.
        summary.pop("generated_at", None)
        return summary

    whole = _summary("limit=100&lane_days=1")
    single = _summary("limit=1&lane_days=1")
    tail = _summary("limit=1&offset=3&lane_days=1")
    past_end = _summary("limit=1&offset=99&lane_days=1")

    assert single == whole
    assert tail == whole
    assert past_end == whole
    assert whole["activation_funnel"]["registered"] == 4


def test_background_lane_coverage_uses_weakest_observed_and_current_route():
    from admin import data_track

    report = {
        "window": {"days": 7},
        "read_status": {"level": "ok", "message": ""},
        "coverage_by_route": {
            "resident": {"level": "green", "message": ""},
            "model_api": {"level": "partial", "message": "late backfill"},
        },
        "users": {
            "usr_switching": {
                "lanes": {
                    "heartbeat": {
                        **data_track._background_lane_empty(),
                        "completed": 3,
                        "terminal_attempts": 3,
                        "failure_rate": 0.0,
                    },
                },
                "routes": {
                    "resident": {"heartbeat": {"completed": 2}},
                    "model_api": {"heartbeat": {"completed": 1}},
                },
            },
            "usr_just_switched": {
                "lanes": {
                    "heartbeat": {
                        **data_track._background_lane_empty(),
                        "completed": 2,
                        "terminal_attempts": 2,
                        "failure_rate": 0.0,
                    },
                },
                "routes": {"resident": {"heartbeat": {"completed": 2}}},
            },
        },
    }

    mixed = data_track._background_lanes_for_user(
        report, user_id="usr_switching", route="model_api"
    )
    assert mixed["coverage_routes"] == ["model_api", "resident"]
    assert mixed["coverage"]["level"] == "partial"
    assert "model_api=partial" in mixed["coverage"]["message"]
    assert "resident=green" in mixed["coverage"]["message"]

    report["coverage_by_route"]["model_api"] = {
        "level": "unavailable", "message": "no watermark"
    }
    switched = data_track._background_lanes_for_user(
        report, user_id="usr_just_switched", route="model_api"
    )
    assert switched["coverage_routes"] == ["model_api", "resident"]
    assert switched["coverage"]["level"] == "unavailable"
    assert "model_api=unavailable" in switched["coverage"]["message"]


def test_data_track_admin_connection_sets_session_timeout_and_resets(
        monkeypatch):
    executed = []

    class FakeConnection:
        def execute(self, sql):
            executed.append(sql)

    class FakeLease:
        def __init__(self):
            self.connection = FakeConnection()

        def __enter__(self):
            return self.connection

        def __exit__(self, *_args):
            return False

    class FakePool:
        def connection(self, *, timeout):
            assert timeout == 5
            return FakeLease()

    monkeypatch.setattr(db, "get_pool", lambda: FakePool())
    with pytest.raises(RuntimeError, match="probe"):
        with db._admin_data_track_connection():
            raise RuntimeError("probe")

    assert executed == [
        "SET statement_timeout = '5000ms'",
        "RESET statement_timeout",
    ]


def test_admin_data_track_reports_screen_frame_storage_and_freshness(client):
    user_id, _ = _register(client)
    with db.get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO frame_envelopes
                (user_id, frame_id, ts, doc, env_meta, body_key)
            VALUES
                (%s, 'frame_inline', extract(epoch FROM now()) - 5,
                 %s::jsonb, NULL, NULL),
                (%s, 'frame_r2', extract(epoch FROM now()),
                 NULL, %s::jsonb, 'frames/test/body')
            """,
            (
                user_id,
                json.dumps({"v": 1, "body_ct": "must-not-leak"}),
                user_id,
                json.dumps({"v": 1, "owner_user_id": user_id}),
            ),
        )

    body = client.get(
        "/v1/admin/data-track/users", headers=_admin_headers()
    ).get_json()
    frames = body["users"][0]["screen_frames"]

    assert frames["total"] == 2
    assert frames["inline_count"] == 1
    assert frames["r2_count"] == 1
    assert frames["latest_at"]
    assert 0 <= frames["latest_age_sec"] < 30
    assert "must-not-leak" not in json.dumps(body)

    page = client.get(
        f"/admin/data-track/users/{user_id}", headers=_admin_headers()
    )
    html_body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "屏幕帧" in html_body
    assert "frame count" in html_body
    assert "latest age" in html_body
    assert "inline frames" in html_body
    assert "R2 frames" in html_body


def test_admin_data_track_warns_when_broadcast_is_on_but_frames_are_stale(client):
    user_id, _ = _register(client)
    with db.get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO frame_envelopes
                (user_id, frame_id, ts, doc, env_meta, body_key)
            VALUES
                (%s, 'frame_stale', extract(epoch FROM now()) - 600,
                 %s::jsonb, NULL, NULL)
            """,
            (user_id, json.dumps({"v": 1, "body_ct": "must-not-leak"})),
        )
        conn.execute(
            """
            INSERT INTO user_blobs (user_id, kind, doc)
            VALUES (
                %s,
                'perception_state',
                jsonb_build_object(
                    'broadcast_state', jsonb_build_object(
                        'v', 'on', 'ts', extract(epoch FROM now())
                    ),
                    'broadcast_active', jsonb_build_object(
                        'v', true, 'ts', extract(epoch FROM now())
                    )
                )
            )
            ON CONFLICT (user_id, kind) DO UPDATE SET doc = EXCLUDED.doc
            """,
            (user_id,),
        )

    body = client.get(
        "/v1/admin/data-track/users", headers=_admin_headers()
    ).get_json()
    frames = body["users"][0]["screen_frames"]

    assert frames["broadcast_report_active"] is True
    assert frames["broadcast_stalled"] is True
    assert frames["latest_age_sec"] >= 599
    assert "must-not-leak" not in json.dumps(body)

    page = client.get(
        f"/admin/data-track/users/{user_id}", headers=_admin_headers()
    )
    html_body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "屏幕共享连接可能已断开" in html_body
    assert "停止后重新开启" in html_body


def test_admin_data_track_surfaces_provider_health(client):
    user_id, _ = _register(client)
    with db.get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO provider_health (
              user_id, provider_state, last_provider_success_at,
              last_provider_failure_at, last_provider_error_class,
              last_provider_error_blame, last_probe_at
            )
            VALUES (%s, 'needs_user_action', now() - interval '72 hours',
                    now() - interval '1 hour', 'auth_invalid',
                    'user_provider', now())
            """,
            (user_id,),
        )

    body = client.get(
        "/v1/admin/data-track/users",
        headers=_admin_headers(),
    ).get_json()

    assert body["summary"]["provider_needs_user_action"] == 1
    row = body["users"][0]
    assert row["provider_state"] == "needs_user_action"
    assert row["last_provider_success_at"]
    assert row["last_provider_failure_at"]
    assert row["last_provider_error_class"] == "auth_invalid"


def test_admin_data_track_dau_counts_user_activity_by_beijing_day(client):
    user_a, _ = _register(client)
    user_b, _ = _register(client)
    user_c, _ = _register(client)

    day2_chat_ts = _epoch("2030-06-01T17:30:00Z")  # 2030-06-02 01:30 Beijing
    day2_tracking_ts = _epoch("2030-06-01T18:00:00Z")
    day3_chat_ts = _epoch("2030-06-02T16:30:00Z")  # 2030-06-03 00:30 Beijing

    _append_chat_at(user_a, "dau_user_chat", "user", "chat", day2_chat_ts)
    _append_chat_at(user_a, "dau_agent_reply", "openclaw", "chat", day2_chat_ts + 1)
    _append_chat_at(user_c, "dau_verify_ping", "user", "verify_ping", day2_chat_ts + 2)
    _append_chat_at(user_b, "dau_next_day_chat", "user", "chat", day3_chat_ts)

    db.log_append(
        user_a,
        "tracking_events",
        {"event_id": "trk_day2_a", "type": "app_open", "ts": day2_tracking_ts},
        ts=day2_tracking_ts,
    )
    db.log_append(
        user_b,
        "tracking_events",
        {"event_id": "trk_day2_b", "type": "onboarding_view", "ts": day2_tracking_ts + 10},
        ts=day2_tracking_ts + 10,
    )

    res = client.get(
        "/v1/admin/data-track/dau?since=2030-06-01T17:00:00Z&days=10",
        headers=_admin_headers(),
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    by_day = {row["day"]: row for row in body["rows"]}
    assert body["definition"]["timezone"] == "Asia/Shanghai"
    assert body["summary"]["snapshot_first_day"] == ""
    assert body["summary"]["snapshot_last_day"] == ""
    assert body["summary"]["snapshot_days"] == 0
    assert all(row["frozen"] is False for row in body["rows"])
    assert by_day["2030-06-03"]["dau"] == 1
    assert by_day["2030-06-03"]["chat_dau"] == 1
    assert by_day["2030-06-03"]["tracking_dau"] == 0
    assert by_day["2030-06-02"]["dau"] == 2
    assert by_day["2030-06-02"]["chat_dau"] == 1
    assert by_day["2030-06-02"]["tracking_dau"] == 2
    assert by_day["2030-06-02"]["user_messages"] == 1
    assert by_day["2030-06-02"]["tracking_events"] == 2
    assert by_day["2030-06-02"]["active_events"] == 3

    page = client.get(
        "/admin/data-track?view=dau&since=2030-06-01T17:00:00Z&days=10",
        headers=_admin_headers(),
    )
    assert page.status_code == 200, page.get_data(as_text=True)
    html = page.get_data(as_text=True)
    assert "Daily Active Users" in html
    assert "Chat DAU" in html
    assert "2030-06-02" in html


def test_admin_data_track_dau_histogram_json_and_page(client):
    users = [_register(client)[0] for _ in range(3)]
    event_ts = _epoch("2030-06-01T18:00:00Z")  # 2030-06-02 Beijing
    for uid, duration in zip(users, (59, 60, 3600)):
        db.log_append(
            uid,
            "tracking_events",
            {"type": "app_session_end", "payload": {"duration_sec": duration}},
            ts=event_ts,
        )

    res = client.get(
        "/v1/admin/data-track/dau?day=2030-06-02&days=10",
        headers=_admin_headers(),
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    histogram = body["usage_histogram"]
    assert body["filters"]["day"] == "2030-06-02"
    assert histogram["day"] == "2030-06-02"
    assert histogram["total_users"] == 3
    assert [bucket["users"] for bucket in histogram["buckets"]] == [
        1, 1, 0, 0, 0, 1, 0, 0,
    ]
    assert histogram["median_sec"] == 60.0

    page = client.get(
        "/admin/data-track?view=dau&day=2030-06-02&days=10",
        headers=_admin_headers(),
    )
    assert page.status_code == 200, page.get_data(as_text=True)
    page_html = page.get_data(as_text=True)
    assert "使用时长分布 · 2030-06-02" in page_html
    assert "样本 3 人" in page_html
    assert "样本=当天有上报的 3 位用户" in page_html


def test_user_detail_daily_usage_json_page_and_events_limit(client):
    user_id, _ = _register(client)
    zone = ZoneInfo("Asia/Shanghai")
    today = datetime.now(zone).date()
    first_day = today - timedelta(days=2)
    first_midnight = datetime.combine(
        first_day, datetime.min.time(), tzinfo=zone
    ).timestamp()
    today_midnight = datetime.combine(
        today, datetime.min.time(), tzinfo=zone
    ).timestamp()
    for index, (ts, duration) in enumerate((
        (first_midnight, 40),
        (today_midnight, 80),
        (today_midnight + 1, "bad"),
        (today_midnight + 2, 20),
        (today_midnight + 3, 30),
    )):
        db.log_append(
            user_id,
            "tracking_events",
            {
                "event_id": f"daily_{index}",
                "type": "app_session_end",
                "ts": ts,
                "payload": {"duration_sec": duration},
            },
            ts=ts,
        )

    res = client.get(
        f"/v1/admin/data-track/users/{user_id}?days=3&events_limit=2",
        headers=_admin_headers(),
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    user = res.get_json()["user"]
    assert user["daily_usage_days"] == 3
    assert len(user["daily_usage"]) == 3
    assert user["daily_usage"][0]["foreground_sec"] == 40
    assert user["daily_usage"][1]["foreground_sec"] == 0
    assert user["daily_usage"][1]["sessions"] == 0
    assert user["daily_usage"][2]["foreground_sec"] == 130
    assert user["daily_usage"][2]["sessions"] == 4
    assert sum(row["foreground_sec"] for row in user["daily_usage"]) == user["app_usage"]["foreground_sec"] == 170
    assert sum(row["sessions"] for row in user["daily_usage"]) == user["app_usage"]["sessions"] == 5
    assert user["tracking"]["events_limit"] == 2
    assert len(user["tracking"]["latest"]) == 2

    capped = client.get(
        f"/v1/admin/data-track/users/{user_id}?days=999&events_limit=999",
        headers=_admin_headers(),
    ).get_json()["user"]
    assert capped["daily_usage_days"] == 90
    assert len(capped["daily_usage"]) == 90
    assert capped["tracking"]["events_limit"] == 500
    assert len(capped["tracking"]["latest"]) == 5

    page = client.get(
        f"/admin/data-track/users/{user_id}?days=3&events_limit=2",
        headers=_admin_headers(),
    )
    assert page.status_code == 200, page.get_data(as_text=True)
    body = page.get_data(as_text=True)
    assert "最近 3 天使用时长" in body
    assert "窗口合计" in body and "全时段合计" in body
    assert "未打开" in body
    assert first_day.isoformat() in body
    assert (first_day + timedelta(days=1)).isoformat() in body


def test_uid_lookup_form_strip_validation_and_admin_key_passthrough(client):
    user_id, _ = _register(client)
    # view=users is now explicit: the bare /admin/data-track default is the
    # home page (dashboard IA v2), and the uid-lookup form lives on Users.
    page = client.get(
        "/admin/data-track?admin_key=admin-test-token&view=users",
        headers=_admin_headers(),
    )
    body = page.get_data(as_text=True)
    assert 'action="/admin/data-track/users"' in body
    assert 'name="uid"' in body
    assert 'name="admin_key" type="hidden" value="admin-test-token"' in body

    lookup = client.get(
        f"/admin/data-track/users?uid=%20%0A{user_id}%09&admin_key=admin-test-token",
        headers=_admin_headers(),
    )
    assert lookup.status_code == 303
    assert lookup.headers["location"] == (
        f"/admin/data-track/users/{user_id}?admin_key=admin-test-token"
    )

    invalid_json = client.get(
        "/v1/admin/data-track/users/not-a-user",
        headers=_admin_headers(),
    )
    assert invalid_json.status_code == 400
    assert invalid_json.get_json() == {"error": "invalid_user_id"}

    invalid_page = client.get(
        "/admin/data-track/users?uid=%3Cscript%3E",
        headers=_admin_headers(),
    )
    assert invalid_page.status_code == 400
    invalid_body = invalid_page.get_data(as_text=True)
    assert "UID 格式不正确" in invalid_body
    assert "<script>" not in invalid_body
    assert "&lt;script&gt;" in invalid_body

    invalid_path = client.get(
        f"/admin/data-track/users/{quote('<script>', safe='')}",
        headers=_admin_headers(),
    )
    assert invalid_path.status_code == 400
    assert "UID 格式不正确" in invalid_path.get_data(as_text=True)


def test_tracking_stats_events_limit_clamps_to_500():
    events = [
        {"event_id": str(index), "type": "app_open", "ts": float(index)}
        for index in range(600)
    ]
    store = SimpleNamespace(list_tracking_events=lambda limit=0: events)

    short = _dt._tracking_stats(store, include_events=True, events_limit=3)
    assert short["events_limit"] == 3
    assert [row["event_id"] for row in short["latest"]] == ["599", "598", "597"]

    capped = _dt._tracking_stats(store, include_events=True, events_limit=999)
    assert capped["events_limit"] == 500
    assert len(capped["latest"]) == 500


def test_admin_data_track_supports_since_filter_and_pagination(client):
    old_user, _ = _register(client)
    new_user, _ = _register(client)

    with registry._users_lock:
        for entry in registry._users:
            if entry["user_id"] == old_user:
                entry["created_at"] = "2026-06-01T17:00:00+00:00"
            elif entry["user_id"] == new_user:
                entry["created_at"] = "2026-06-01T19:00:00+00:00"
        registry._save_users()

    summary = client.get(
        "/v1/admin/data-track/summary?since=2026-06-01T18:00:00Z",
        headers=_admin_headers(),
    )
    assert summary.status_code == 200, summary.get_data(as_text=True)
    summary_body = summary.get_json()
    assert summary_body["summary"]["users_total"] == 1
    assert "users" not in summary_body

    users = client.get(
        "/v1/admin/data-track/users?since=2026-06-01T18:00:00Z&limit=1",
        headers=_admin_headers(),
    )
    assert users.status_code == 200, users.get_data(as_text=True)
    body = users.get_json()
    assert body["pagination"] == {
        "limit": 1,
        "offset": 0,
        "returned": 1,
        "total": 1,
        "next_offset": None,
        "prev_offset": None,
    }
    assert [row["user_id"] for row in body["users"]] == [new_user]


def test_admin_data_track_sorts_before_pagination(client):
    low_chat_high_memory, _ = _register(client)
    mid_chat_mid_memory, _ = _register(client)
    high_chat_low_memory, _ = _register(client)

    def add_chat(user_id: str, *, regular: int, proactive: int) -> None:
        store = core_store.get_store(user_id)
        for idx in range(regular):
            role = "user" if idx % 2 == 0 else "openclaw"
            store.append_chat(role, "chat", _env(f"{user_id}_chat_{idx}", user_id))
        for idx in range(proactive):
            store.append_chat(
                "openclaw",
                proactive_service.PROACTIVE_JOB_SOURCE,
                _env(f"{user_id}_proactive_{idx}", user_id),
            )

    def add_memories(user_id: str, count: int) -> None:
        store = core_store.get_store(user_id)
        memory_service._save_moments(
            store,
            [
                {
                    "id": f"{user_id}_mem_{idx}",
                    "type": "moment" if idx % 2 == 0 else "fact",
                    "source": "test",
                    "created_at": f"2026-06-01T00:{idx:02d}:00",
                }
                for idx in range(count)
            ],
        )

    add_chat(low_chat_high_memory, regular=1, proactive=0)
    add_memories(low_chat_high_memory, 5)
    add_chat(mid_chat_mid_memory, regular=2, proactive=1)
    add_memories(mid_chat_mid_memory, 3)
    add_chat(high_chat_low_memory, regular=3, proactive=2)
    add_memories(high_chat_low_memory, 1)

    def sorted_ids(sort: str, direction: str) -> list[str]:
        res = client.get(
            f"/v1/admin/data-track/users?sort={sort}&dir={direction}&limit=10",
            headers=_admin_headers(),
        )
        assert res.status_code == 200, res.get_data(as_text=True)
        return [row["user_id"] for row in res.get_json()["users"]]

    assert sorted_ids("chat", "desc") == [
        high_chat_low_memory,
        mid_chat_mid_memory,
        low_chat_high_memory,
    ]
    assert sorted_ids("chat", "asc") == [
        low_chat_high_memory,
        mid_chat_mid_memory,
        high_chat_low_memory,
    ]
    assert sorted_ids("memory", "desc") == [
        low_chat_high_memory,
        mid_chat_mid_memory,
        high_chat_low_memory,
    ]
    assert sorted_ids("proactive", "desc") == [
        high_chat_low_memory,
        mid_chat_mid_memory,
        low_chat_high_memory,
    ]

    # view=users is now explicit (bare /admin/data-track renders the home page);
    # the users-page primary nav is 首页/产品健康/用户/诊断 — the 11 legacy
    # views (日活与时长 etc.) moved behind the 诊断 hub.
    page = client.get(
        "/admin/data-track?view=users&sort=chat&dir=desc", headers=_admin_headers()
    )
    assert page.status_code == 200, page.get_data(as_text=True)
    html = page.get_data(as_text=True)
    assert "Chat desc" in html
    assert "诊断" in html
    assert "产品健康" in html
    assert "Memory asc" in html
    assert "Proactive desc" in html


# --- 2026-07 data-track redo: genesis-aware stage + activation funnel ---------
# Regression guards for the fix that stopped counting genesis (bucket-based)
# users as stuck at memgarden. Pure-function tests on _data_track_fast_validation.
from admin import data_track as _dt  # noqa: E402


def test_user_detail_page_displays_existing_agent_job_attempt_count(
    client, monkeypatch
):
    user_id, _ = _register(client)
    monkeypatch.setattr(
        _dt,
        "_v2_recent_jobs_detail",
        lambda _uid: {
            "window_hours": 72,
            "has_more": False,
            "jobs": [
                {
                    "job_id": 18801,
                    "lane": "dream",
                    "status": "expired",
                    "attempt_count": 4,
                    "created_at": "2026-08-21T00:00:00Z",
                    "finished_at": "2026-08-21T00:05:00Z",
                }
            ],
        },
    )

    page = client.get(
        f"/admin/data-track/users/{user_id}", headers=_admin_headers()
    )
    body = page.get_data(as_text=True)

    assert page.status_code == 200
    assert "Runtime V2 最近任务" in body
    assert "<th>attempt_count</th>" in body
    assert "<td class='job-attempt-count'>4</td>" in body
    assert "agent_jobs.attempt_count" in body


def _genesis_model_api_memory():
    # Genesis writes by bucket, so the retired by_tab counters are all zero even
    # though the garden has cards. This is exactly the shape that used to break.
    return {"total": 7, "by_tab": {"story": 0, "about_me": 0, "ta_thinking": 0, "unknown": 7},
            "by_source": {"genesis_import": 7}}


def test_fast_validation_genesis_user_is_complete_despite_empty_tabs():
    v = _dt._data_track_fast_validation(
        route="model_api",
        chat={"model_api_greetings": 1, "model_api_user_messages": 2, "model_api_agent_messages": 2,
              "user_messages": 2, "agent_messages": 2},
        memory=_genesis_model_api_memory(),
        identity={"relationship_started_at": "2026-06-25", "relationship_anchor_evidence": "x",
                  "relationship_anchor_source": "genesis_import", "updated_at": "2026-06-30"},
        history_import={"has_job": True, "status": "completed", "chat_ready": True},
        model_api_config={"test_status": "ok"},
        consumer_state=None,
        bootstrap_events={"by_type": {}},
    )
    assert v["passing"] is True
    assert v["stage"] == "complete"
    mg = next(s for s in v["steps"] if s["id"] == "memory")
    assert mg["passing"] is True  # cards exist -> garden satisfied (bucket-agnostic)


def test_effective_responder_uses_current_fences_before_poll_samples():
    now = 1_000.0
    result = _dt._effective_responder(
        route="resident",
        consumer_state={
            "poll_consumers": {
                "resident-vps": {
                    "responder": "resident",
                    "last_poll_epoch": 999.0,
                    "last_poll_at": "recent",
                },
                "agent-runner:u": {
                    "responder": "hosted_v1",
                    "last_poll_epoch": 998.0,
                    "last_poll_at": "also recent",
                },
            }
        },
        runtime={
            "hosted_runtime_state": "resident",
            "model_api_route": {"is_active": True, "test_status": "ok"},
            "runner_lease": {"active": True},
        },
        now_epoch=now,
    )
    assert result["effective_responder"] == "hosted_v1"
    assert result["basis"] == "live_agent_runtime_lease"
    assert result["mismatch"] is True
    assert "non_model_api_route_with_hosted_responder" in result["mismatch_reasons"]
    assert "multiple_responder_classes_detected" in result["mismatch_reasons"]
    assert {item["responder"] for item in result["recent_poll_observations"]} == {
        "hosted_v1",
        "resident",
    }


def test_effective_responder_covers_v2_resident_and_none_states():
    hosted_v2 = _dt._effective_responder(
        route="model_api",
        consumer_state=None,
        runtime={
            "hosted_runtime_state": "v2",
            "model_api_route": {"is_active": True, "test_status": "ok"},
            "runner_lease": {"active": False},
        },
        now_epoch=1_000.0,
    )
    assert hosted_v2["effective_responder"] == "hosted_v2"
    assert hosted_v2["runtime_state"] == "v2"
    assert hosted_v2["mismatch"] is False

    draining = _dt._effective_responder(
        route="model_api",
        consumer_state=None,
        runtime={
            "hosted_runtime_state": "draining",
            "model_api_route": {"is_active": True, "test_status": "ok"},
            "runner_lease": {"active": False},
        },
        now_epoch=1_000.0,
    )
    assert draining["effective_responder"] == "hosted_v2"
    assert draining["runtime_state"] == "draining"
    assert draining["mismatch"] is False

    resident = _dt._effective_responder(
        route="resident",
        consumer_state={
            "poll_consumers": {
                "resident-vps": {
                    "responder": "resident",
                    "last_poll_epoch": 999.0,
                }
            }
        },
        runtime={"hosted_runtime_state": "resident", "runner_lease": {"active": False}},
        now_epoch=1_000.0,
    )
    assert resident["effective_responder"] == "resident"
    assert resident["runtime_state"] == "resident"
    assert resident["mismatch"] is False

    none = _dt._effective_responder(
        route="resident",
        consumer_state=None,
        runtime=None,
        now_epoch=1_000.0,
    )
    assert none["effective_responder"] == "none"
    assert none["mismatch"] is False

    official_import = _dt._effective_responder(
        route="official_import",
        consumer_state={
            "poll_consumers": {
                "resident-vps": {
                    "responder": "resident",
                    "last_poll_epoch": 999.0,
                }
            }
        },
        runtime=None,
        now_epoch=1_000.0,
    )
    assert official_import["effective_responder"] == "resident"
    assert official_import["mismatch"] is True
    assert (
        "official_import_route_with_resident_poll"
        in official_import["mismatch_reasons"]
    )

    stale_hosted = _dt._effective_responder(
        route="resident",
        consumer_state={
            "poll_consumers": {
                "agent-runner:u": {
                    "responder": "hosted_v1",
                    "last_poll_epoch": 1.0,
                }
            }
        },
        runtime=None,
        now_epoch=1_000.0,
    )
    assert stale_hosted["effective_responder"] == "none"
    assert stale_hosted["mismatch"] is False
    assert stale_hosted["poll_observations"][0]["recent"] is False
    assert stale_hosted["recent_poll_observations"] == []


def test_admin_runtime_state_filter_is_strict_and_links_preserve_query(client):
    v2_user, _ = _register(client)
    draining_user, _ = _register(client)
    resident_user, _ = _register(client)
    _append_chat_at(
        v2_user,
        "msg-v2-activated",
        "user",
        "chat",
        _epoch("2030-06-01T12:00:00Z"),
    )
    with db.get_pool().connection() as conn:
        for user_id, runtime_state in (
            (v2_user, "v2"),
            (draining_user, "draining"),
            (resident_user, "resident"),
        ):
            conn.execute(
                "INSERT INTO v2_runtime_state (user_id,hosted_runtime_state) "
                "VALUES (%s,%s) ON CONFLICT (user_id) DO UPDATE SET "
                "hosted_runtime_state=EXCLUDED.hosted_runtime_state",
                (user_id, runtime_state),
            )

    # view=users is explicit now that the bare page defaults to home; the JSON
    # API ignores it, and the rendered links must keep carrying it.
    query = (
        "runtime_state=v2&q=usr_&sort=chat&dir=asc&limit=1&offset=0&view=users"
    )
    response = client.get(
        f"/v1/admin/data-track/users?{query}",
        headers=_admin_headers(),
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert [row["user_id"] for row in body["users"]] == [v2_user]
    assert body["users"][0]["responder"]["runtime_state"] == "v2"
    assert body["summary"]["runtime_state_counts"] == {"v2": 1}
    assert body["summary"]["activated_runtime_state_counts"] == {"v2": 1}
    assert body["filters"]["runtime_state"] == "v2"

    page = client.get(
        "/admin/data-track?admin_key=admin-test-token&" + query,
        headers=_admin_headers(),
    )
    assert page.status_code == 200, page.get_data(as_text=True)
    rendered = page.get_data(as_text=True).replace("&amp;", "&")
    assert v2_user in rendered
    assert draining_user not in rendered
    assert resident_user not in rendered
    assert "Hosted V2 账号行（当前筛选）" in rendered
    assert "已激活 Hosted V2 账号行（当前筛选）" in rendered
    assert '<div class="table-wrap"><table id="users">' in rendered
    assert 'lang="zh-CN"' in rendered
    assert (
        f"/admin/data-track/users/{v2_user}?admin_key=admin-test-token"
        "&q=usr_&limit=1&offset=0&sort=chat&dir=asc&view=users&runtime_state=v2"
    ) in rendered
    assert (
        "/admin/data-track?admin_key=admin-test-token&q=usr_&limit=1&offset=0"
        "&sort=chat&dir=asc&view=users&runtime_state=draining"
    ) in rendered


# App usage-duration rendering (app_session_end aggregation surfaced in the overview).
def test_fmt_duration_sec_compact_human_readable():
    assert _dt._fmt_duration_sec(0) == "0s"
    assert _dt._fmt_duration_sec(45) == "45s"
    assert _dt._fmt_duration_sec(137) == "2m17s"
    assert _dt._fmt_duration_sec(120) == "2m"
    assert _dt._fmt_duration_sec(5400) == "1h30m"
    assert _dt._fmt_duration_sec(3600) == "1h"
    assert _dt._fmt_duration_sec(None) == "—"
    assert _dt._fmt_duration_sec("nope") == "—"
    # rounds and never negative
    assert _dt._fmt_duration_sec(59.6) == "1m"


def test_beijing_time_display_helpers():
    # Display-only Beijing (UTC+8) conversion; storage stays UTC. Inputs are an
    # epoch or an explicit-UTC value so the assertion is host-timezone-independent.
    import calendar
    utc_midnight = calendar.timegm((2026, 7, 13, 0, 0, 0, 0, 0, 0))  # 2026-07-13 00:00:00 UTC
    # epoch -> Beijing wall clock is 08:00 the same day
    assert _dt._debug_time(utc_midnight) == "07-13 08:00:00"
    assert _dt._bj_iso(utc_midnight) == "2026-07-13 08:00:00"
    # a stored (naive) UTC ISO string is treated as UTC, then shifted +8
    assert _dt._bj_iso("2026-07-13T00:00:00") == "2026-07-13 08:00:00"
    assert _dt._bj_iso("2026-07-12T20:30:00") == "2026-07-13 04:30:00"  # crosses the day
    # empties stay empty; zero epoch is the "no time" sentinel
    assert _dt._bj_iso("") == ""
    assert _dt._bj_iso(None) == ""
    assert _dt._debug_time(0) == "—"
    # fail-soft: a wildly out-of-range epoch must not raise (would 500 the page)
    assert isinstance(_dt._bj_iso(10 ** 30), str)
    assert _dt._debug_time(10 ** 30) == "—"


def test_dau_page_marks_frozen_vs_live_and_cutover_note():
    # Each day shows 🔒已冻结 (snapshot, immutable) or ⏱实时 (live, can shrink on
    # deletion); the history note names the snapshot cutover day.
    summary = {
        "latest_dau": 2, "latest_day": "2026-07-14", "max_dau": 5, "avg_dau": 3.5,
        "user_messages": 10, "tracking_events": 20, "days_returned": 2,
        "timezone": "Asia/Shanghai", "generated_at": "2026-07-14T00:00:00",
        "snapshot_first_day": "2026-07-13", "snapshot_last_day": "2026-07-13", "snapshot_days": 1,
    }
    rows = [
        {"day": "2026-07-14", "frozen": False, "dau": 2, "chat_dau": 1, "tracking_dau": 2,
         "active_events": 3, "user_messages": 4, "tracking_events": 5, "session_dau": 1,
         "avg_session_sec": 60, "session_count": 3, "last_at": "2026-07-14T00:00:00"},
        {"day": "2026-07-13", "frozen": True, "dau": 5, "chat_dau": 3, "tracking_dau": 5,
         "active_events": 9, "user_messages": 6, "tracking_events": 7, "session_dau": 2,
         "avg_session_sec": 90, "session_count": 8, "last_at": "2026-07-13T10:00:00"},
    ]
    out = _dt._render_data_track_dau_page(
        {"summary": summary, "filters": {}, "definition": {"dau": "", "excluded": ""}, "rows": rows}
    )
    assert "🔒 已冻结" in out          # the frozen day
    assert "⏱ 实时" in out            # today (live)
    assert "首个冻结日是 <b>2026-07-13</b>" in out  # cutover named in the note
    assert "<b>今天</b>仍是实时数据" in out
    assert "<th>状态</th>" in out       # status column present


def test_dau_page_renders_usage_histogram_with_day_links_and_sample_copy():
    summary = {
        "latest_dau": 5, "latest_day": "2026-07-24", "max_dau": 5, "avg_dau": 4.0,
        "user_messages": 10, "tracking_events": 20, "days_returned": 2,
        "timezone": "Asia/Shanghai", "generated_at": "2026-07-25T00:00:00",
        "snapshot_first_day": "", "snapshot_last_day": "", "snapshot_days": 0,
    }
    rows = [
        {"day": "2026-07-25", "frozen": False},
        {"day": "2026-07-24", "frozen": True},
    ]
    histogram = {
        "day": "2026-07-24",
        "buckets": [
            {"label": "0-1min", "lo_sec": 0, "hi_sec": 60, "users": 2},
            {"label": "1-5min", "lo_sec": 60, "hi_sec": 300, "users": 3},
        ],
        "total_users": 5,
        "median_sec": 120,
        "mean_sec": 180,
        "p90_sec": 300,
        "max_sec": 600,
    }
    out = _dt._render_data_track_dau_page({
        "summary": summary,
        "filters": {"day": "2026-07-24"},
        "definition": {"dau": "", "excluded": ""},
        "rows": rows,
        "usage_histogram": histogram,
    })

    assert "使用时长分布 · 2026-07-24" in out
    assert "width:66.67%" in out
    assert "width:100.00%" in out
    assert "2 人 · 40.0%" in out
    assert "样本 5 人 · 中位数 2m · 均值 3m · P90 5m · 最大值 10m" in out
    assert "day=2026-07-25" in out
    assert "day=2026-07-24" in out
    assert "样本=当天有上报的 5 位用户" in out
    assert "没有 app_session_end 上报的用户不计入" in out


def test_bj_deep_converts_only_iso_datetime_strings():
    # The user-detail <pre> JSON clone shows every timestamp in Beijing; non-time
    # strings and other types are untouched. Display-only — JSON API stays UTC.
    src = {
        "user_id": "usr_x",
        "registered_at": "2026-07-13T00:00:00",          # -> +8
        "route": "model_api",                             # not a time
        "nested": {"last_activity_at": "2026-07-12T20:30:00.500000"},  # crosses day
        "list": ["2026-07-13T00:00:00", "not-a-time", 42],
        "count": 7,
    }
    out = _dt._bj_deep(src)
    assert out["registered_at"] == "2026-07-13 08:00:00"
    assert out["route"] == "model_api"
    assert out["nested"]["last_activity_at"] == "2026-07-13 04:30:00"
    assert out["list"][0] == "2026-07-13 08:00:00"
    assert out["list"][1] == "not-a-time"
    assert out["list"][2] == 42
    assert out["count"] == 7
    # source dict is not mutated (clone semantics)
    assert src["registered_at"] == "2026-07-13T00:00:00"


def _post_session_end(client, api_key: str, duration_sec: int) -> None:
    res = client.post(
        "/v1/track/event",
        headers=_headers(api_key),
        json={
            "type": "app_session_end",
            "source": "ios",
            "platform": "ios",
            "route": "model_api",
            "app_version": "1.4.0",
            "build": "312",
            "payload": {"duration_sec": duration_sec},
        },
    )
    assert res.status_code == 200, res.get_data(as_text=True)


def test_admin_data_track_app_usage_rollup(client):
    # No app_session_end events yet -> app_usage present but zeroed.
    u1, k1 = _register(client)
    empty = client.get("/v1/admin/data-track/users", headers=_admin_headers()).get_json()
    au0 = empty["summary"]["app_usage"]
    assert au0 == {
        "foreground_sec_total": 0, "sessions_total": 0,
        "avg_session_sec": 0, "users_active": 0, "dau_today": 0,
    }

    # Two sessions for u1 (137 + 63 = 200s), one for u2 (100s).
    _post_session_end(client, k1, 137)
    _post_session_end(client, k1, 63)
    u2, k2 = _register(client)
    _post_session_end(client, k2, 100)

    body = client.get("/v1/admin/data-track/users", headers=_admin_headers()).get_json()
    au = body["summary"]["app_usage"]
    assert au["foreground_sec_total"] == 300
    assert au["sessions_total"] == 3
    assert au["avg_session_sec"] == 100
    assert au["users_active"] == 2        # both users had >=1 session
    assert au["dau_today"] == 2           # events ingested "now" -> today in Shanghai

    # Per-user app_usage contract: active users carry their totals; a fresh
    # user with no app_session_end events defaults to zeros.
    rows = {r["user_id"]: r for r in body["users"]}
    assert rows[u1]["app_usage"]["sessions"] == 2
    assert rows[u1]["app_usage"]["foreground_sec"] == 200
    assert rows[u1]["app_usage"]["last_at"]  # non-empty iso
    assert rows[u2]["app_usage"]["sessions"] == 1
    u_none, _ = _register(client)
    rows2 = {
        r["user_id"]: r
        for r in client.get("/v1/admin/data-track/users", headers=_admin_headers()).get_json()["users"]
    }
    assert rows2[u_none]["app_usage"] == {
        "foreground_sec": 0, "sessions": 0,
        "last_at_epoch": 0.0, "last_at": "",
        "fields_status": "ok", "invalid_fields": [],
    }


def test_admin_data_track_page_uses_plain_language(client, monkeypatch):
    # The overview HTML leads with 激活用户, de-emphasizes 注册, explains both,
    # and renders the App-usage section — no more "已激活 / 原始行" jargon.
    user_id, _ = _register(client)
    core_store.get_store(user_id).append_chat(
        "user", "chat", _env("msg_dashboard_active", user_id)
    )
    monkeypatch.setattr(
        _dt,
        "_runtime_token_usage_summary",
        lambda **_kw: {
            "window_days": 30,
            "sampled_turns": 7,
            "users": 1,
            "model_calls": 8,
            "usage_reported_calls": 6,
            "cache_reported_calls": 6,
            "usage_telemetry_coverage": 0.75,
            "cache_telemetry_coverage": 0.75,
            "prompt_tokens": 12_000,
            "completion_tokens": 345,
            "total_tokens": 12_345,
            "cache_read_tokens": 6_000,
            "cache_write_tokens": 1_000,
            "cache_miss_tokens": 4_000,
        },
    )
    # view=users is now explicit — the bare /admin/data-track default is home.
    page = client.get(
        "/admin/data-track?view=users", headers=_admin_headers()
    ).get_data(as_text=True)
    assert "激活用户（真正用起来的人）" in page
    assert "累计注册行（含重装孤儿·非人数）" in page
    assert "怎么读这些数" in page          # the explainer note-box
    assert "没有、也无法有「已删除账户数」" in page
    assert "App 使用时长" in page
    assert "Runtime 人群" in page
    assert "实际 Runtime" in page
    assert "Resident / V1" in page
    assert "Token 与模型用量已移到独立页面" in page
    assert "打开 Token 与模型" in page
    assert "运营 Telemetry" not in page
    assert "全站 V2 token 总量" not in page
    assert "12,345" not in page
    assert "自托管激活账号（当前筛选）" not in page
    assert "已激活 / 原始行" not in page    # old jargon gone


def test_admin_data_track_app_usage_dau_is_shanghai_day(client, monkeypatch):
    # now = 2030-06-01T16:30Z == Asia/Shanghai 2030-06-02 00:30 (just past midnight).
    # A session at 16:10Z is Shanghai 06-02 00:10 (today); 15:50Z is 06-01 23:50
    # (yesterday) — dau_today must count only the Shanghai-today one.
    now = _epoch("2030-06-01T16:30:00Z")
    monkeypatch.setattr(_dt.time, "time", lambda: now)

    u_today, _ = _register(client)
    u_yesterday, _ = _register(client)
    for uid, ts_iso, dur in [
        (u_today, "2030-06-01T16:10:00Z", 40),
        (u_yesterday, "2030-06-01T15:50:00Z", 50),
    ]:
        ev = {"type": "app_session_end", "payload": {"duration_sec": dur}, "ts": _epoch(ts_iso)}
        db.log_append(uid, "tracking_events", ev, ts=_epoch(ts_iso))

    au = client.get("/v1/admin/data-track/users", headers=_admin_headers()).get_json()["summary"]["app_usage"]
    assert au["sessions_total"] == 2
    assert au["users_active"] == 2
    assert au["dau_today"] == 1  # only the Shanghai-today session, not the day-boundary one


def test_fast_validation_no_memories_still_blocks_memgarden():
    v = _dt._data_track_fast_validation(
        route="model_api",
        chat={},
        memory={"total": 0, "by_tab": {}, "by_source": {}},
        identity=None,
        history_import={"has_job": True, "status": "processing", "chat_ready": False},
        model_api_config={"test_status": "ok"},
        consumer_state=None,
        bootstrap_events={"by_type": {}},
    )
    assert v["passing"] is False
    mg = next(s for s in v["steps"] if s["id"] == "memory")
    assert mg["passing"] is False  # genuinely empty garden must still flag


def test_detail_payload_runtime_includes_reasoning_effort(client):
    from admin import data_track as data_track

    user_id, _api_key = _register(client)
    # Config lives in the routes/credentials tables now (was a model_api blob).
    from conftest import configure_model_api_route
    configure_model_api_route(
        user_id, provider="openrouter", model="anthropic/claude-sonnet-4.6",
        reasoning_effort="medium", test_status="ok")
    user_entry = next(u for u in registry._users if u["user_id"] == user_id)

    row = data_track._build_data_track_user(user_entry, include_detail=True)

    assert row["runtime"]["reasoning_effort"] == "medium"


def test_detail_runtime_reports_the_prompt_budget_turns_are_planned_against(client):
    """Support's first screen must show the window V2 actually budgets against.

    The budget decides how much of the tool catalog survives prompt assembly
    (``prompt_frontier`` admits optional tool schemas one at a time, keeping a
    floor of user MCP plus the core memory/reply tools), so a window that is too
    small silently trims the surface the user is counting on.
    usr_90184ac4cc0896e5 (2026-08-14) reported that with a healthy 3/3 MCP
    surface, and this page held no field that could confirm or refute it.
    (Before T019 the component was atomic and a tight budget dropped every tool;
    support must not explain a new report with that older mechanism.)

    ``configured`` and the resolved value are asserted separately so an
    inherited default can never read as something the user chose.
    """
    from admin import data_track as data_track
    from model_api_runtime.v2 import prompt_frontier
    from conftest import configure_model_api_route

    supplied = 24576
    user_id, _api_key = _register(client)
    configure_model_api_route(
        user_id, provider="openai_compatible", model="gpt-5.5",
        base_url="https://relay.example.com/v1",
        context_window_tokens=supplied, test_status="ok")
    user_entry = next(u for u in registry._users if u["user_id"] == user_id)

    runtime = data_track._build_data_track_user(
        user_entry, include_detail=True)["runtime"]

    assert runtime["context_window_configured"] == supplied
    # Route storage has no provenance bit. A configured value below the metadata
    # trust floor is intentionally visible as configured but not used as the
    # runtime budget; the warning trace makes this accepted tradeoff observable.
    assert supplied < prompt_frontier.provider_metadata_context_window_floor()
    assert runtime["context_window_tokens"] == (
        prompt_frontier.unaudited_default_context_window()
    )
    assert runtime["context_window_source"] == "unaudited_default"


def test_detail_runtime_marks_an_inherited_window_as_not_user_chosen(client):
    """A relay user who never supplied a window inherits the deployment default.

    The expectation is read from the module, not written as a literal: a
    hardcoded number would keep passing while quietly no longer describing the
    deployment.
    """
    from admin import data_track as data_track
    from model_api_runtime.v2 import prompt_frontier
    from conftest import configure_model_api_route

    inherited = prompt_frontier.unaudited_default_context_window()
    assert inherited > 0, "deployment is in fail-closed mode; this case cannot arise"
    user_id, _api_key = _register(client)
    configure_model_api_route(
        user_id, provider="openai_compatible", model="relay-only-model",
        base_url="https://relay.example.com/v1",
        context_window_tokens=None, test_status="ok")
    user_entry = next(u for u in registry._users if u["user_id"] == user_id)

    runtime = data_track._build_data_track_user(
        user_entry, include_detail=True)["runtime"]

    assert runtime["context_window_configured"] == 0
    assert runtime["context_window_tokens"] == inherited
    assert runtime["context_window_source"] == "unaudited_default"


def test_detail_runtime_reports_no_window_when_there_is_no_model_route(client):
    """A user with no model_api route must not be given a window at all.

    Resolution with an empty provider/model still falls through to the
    deployment default, so an unconditional call printed a concrete number for
    users who have no route — support reads that as fact. The local admin E2E
    caught this; every unit case above configures a route, so none could.
    """
    from admin import data_track as data_track

    user_id, _api_key = _register(client)  # deliberately no route configured
    user_entry = next(u for u in registry._users if u["user_id"] == user_id)

    runtime = data_track._build_data_track_user(
        user_entry, include_detail=True)["runtime"]

    assert runtime["provider"] == ""
    assert runtime["context_window_configured"] == 0
    assert runtime["context_window_tokens"] == 0
    assert runtime["context_window_source"] == ""


def test_detail_runtime_driver_carries_its_v1_lens(client):
    """``driver`` is a provider-derived V1 label and selects no V2 path.

    Support — and I, on 2026-08-14 — read a V2 user's ``driver: pi`` as "this
    user runs on pi". The lens has to travel with the value, the way the
    proactive block's already does, or the next reader repeats it.
    """
    from admin import data_track as data_track
    from conftest import configure_model_api_route

    user_id, _api_key = _register(client)
    configure_model_api_route(
        user_id, provider="openai_compatible", model="gpt-5.5",
        base_url="https://relay.example.com/v1", test_status="ok")
    user_entry = next(u for u in registry._users if u["user_id"] == user_id)

    runtime = data_track._build_data_track_user(
        user_entry, include_detail=True)["runtime"]

    assert runtime["driver"] == "pi"  # the historical label itself is unchanged
    assert "selects no V2 execution path" in runtime["driver_lens"]


def test_admin_route_and_notice_summary_helpers_are_explicit_allowlists(monkeypatch):
    monkeypatch.setattr(
        _dt.db,
        "model_api_routes_list",
        lambda _uid: [{
            "is_active": True,
            "is_vision": True,
            "provider": "openrouter",
            "model": "vision/model-test",
            "vision_test_status": "unsupported",
            "image_generation_test_status": "untested",
            "last_image_generation_test_error": "",
            "last_vision_test_error": "v" * 350,
            "last_vision_test_at": "2026-07-31T19:59:00Z",
            "last_runtime_error_class": "provider_transient",
            "last_runtime_error": "vision_model_unavailable",
            "updated_at": "2026-07-31T20:00:00Z",
            "base_url": "https://private-relay.example/secret-path",
            "api_key_hint": "private-key-hint",
        }],
    )
    monkeypatch.setattr(
        _dt.db,
        "log_read_all",
        lambda _uid, _stream: [{
            "error_class": "vision_model_unavailable",
            "blame": "provider_transient",
            "severity": "warning",
            "occurrences": 3,
            "last_ts": 2.0,
            "user_text": "private user text",
            "detail": "private free-form detail",
        }, {
            "error_class": "older_error",
            "blame": "system",
            "severity": "error",
            "occurrences": 1,
            "last_ts": 1.0,
            "copyable_prompt": "private prompt",
        }],
    )

    routes = _dt._model_api_route_summaries("usr_test")
    notices = _dt._notice_summaries("usr_test", limit=1)

    assert routes == [{
        # image_generation 与 vision 同性质:状态是枚举、错误是我们自己的错误码
        # (写入端 mark_image_generation_test(error=code)),都不含用户内容。
        # 补上它们之前,生图路由在 admin 上渲染成 purpose: []，和"没有任何用途"
        # 长得一模一样 —— 2026-08-10 查 usr_7001b1df80e2024d 的生图问题时,
        # 决定整条分支的那个事实恰恰是唯一没被投影的那个。
        "purpose": ["chat", "vision"],
        "provider": "openrouter",
        "model": "vision/model-test",
        "vision_test_status": "unsupported",
        "image_generation_test_status": "untested",
        "last_image_generation_test_error": "",
        "last_vision_test_error": "v" * 300,
        "last_vision_test_at": "2026-07-31T19:59:00Z",
        "last_runtime_error_class": "provider_transient",
        "last_runtime_error": "vision_model_unavailable",
        "updated_at": "2026-07-31T20:00:00Z",
    }]
    assert notices == [{
        "error_class": "vision_model_unavailable",
        "blame": "provider_transient",
        "severity": "warning",
        "occurrences": 3,
        "occurrences_status": "ok",
        "last_ts": 2.0,
    }]
    serialized = json.dumps({"routes": routes, "notices": notices})
    assert "private-relay.example" not in serialized
    assert "private-key-hint" not in serialized
    assert "private user text" not in serialized
    assert "private free-form detail" not in serialized
    assert "private prompt" not in serialized


def test_detail_payload_exposes_content_free_route_errors_and_notice_summaries(client):
    from conftest import configure_model_api_route
    from notices import core as notices_core

    user_id, _api_key = _register(client)
    _credential_id, route_id = configure_model_api_route(
        user_id,
        provider="openrouter",
        model="vision/model-test",
        base_url="https://private-relay.example/secret-path",
        test_status="ok",
    )
    assert db.model_api_route_set_vision(user_id, route_id)
    assert db.model_api_route_mark_vision_test(
        user_id,
        route_id,
        status="unsupported",
        error="v" * 350,
    )
    assert db.model_api_route_mark_runtime_error(
        user_id,
        error="vision_model_unavailable",
        error_class="provider_transient",
    )
    for index in range(22):
        db.log_append(
            user_id,
            notices_core.NOTICES_STREAM,
            {
                "error_class": f"error_{index:02d}",
                "blame": "provider_transient",
                "severity": "warning",
                "occurrences": index + 1,
                "last_ts": float(index + 1),
                "user_text": f"private-user-text-{index}",
                "detail": f"private-free-form-detail-{index}",
                "copyable_prompt": f"private-prompt-{index}",
                "dedupe_key": f"private-dedupe-{index}",
            },
            ts=float(index + 1),
        )

    response = client.get(
        f"/v1/admin/data-track/users/{user_id}",
        headers=_admin_headers(),
    )
    assert response.status_code == 200
    row = response.get_json()["user"]

    assert row["model_api_routes"] == [{
        "purpose": ["chat", "vision"],
        "provider": "openrouter",
        "model": "vision/model-test",
        "vision_test_status": "unsupported",
        "image_generation_test_status": "untested",
        "last_image_generation_test_error": "",
        "last_vision_test_error": "v" * 300,
        "last_vision_test_at": row["model_api_routes"][0]["last_vision_test_at"],
        "last_runtime_error_class": "provider_transient",
        "last_runtime_error": "vision_model_unavailable",
        "updated_at": row["model_api_routes"][0]["updated_at"],
    }]
    assert set(row["model_api_routes"][0]) == {
        "purpose",
        "provider",
        "model",
        "vision_test_status",
        "image_generation_test_status",
        "last_image_generation_test_error",
        "last_vision_test_error",
        "last_vision_test_at",
        "last_runtime_error_class",
        "last_runtime_error",
        "updated_at",
    }
    assert len(row["notice_summaries"]) == 20
    assert row["notice_summaries"][0] == {
        "error_class": "error_21",
        "blame": "provider_transient",
        "severity": "warning",
        "occurrences": 22,
        "occurrences_status": "ok",
        "last_ts": 22.0,
    }
    assert all(set(item) == {
        "error_class", "blame", "severity", "occurrences",
        "occurrences_status", "last_ts"
    } for item in row["notice_summaries"])
    serialized = json.dumps(row)
    assert "private-relay.example" not in serialized
    assert "private-user-text" not in serialized
    assert "private-free-form-detail" not in serialized
    assert "private-prompt" not in serialized
    assert "private-dedupe" not in serialized


def test_detail_payload_exposes_content_free_v2_profile_status_true_pg(client):
    from model_api_runtime.v2 import profile_store

    user_id, _api_key = _register(client)
    endpoint = f"/v1/admin/data-track/users/{user_id}"

    missing = client.get(endpoint, headers=_admin_headers())
    assert missing.status_code == 200
    assert missing.get_json()["user"]["v2_profile"] == {
        "state": "missing",
        "document_status": "missing",
    }

    pending_doc = profile_store.build_profile_document(
        user_id,
        state="pending",
        source={
            "card_count": 7,
            "max_updated_at": "2026-08-01T10:00:00Z",
            "generated_at": "2026-08-01T10:01:00Z",
        },
        last_attempt={
            "at": "2026-08-01T10:01:00Z",
            "reject_code": "provider_retry",
            "attempts": 2,
            "retry_not_before": 1_785_580_860,
        },
        disabled=False,
    )
    db.set_blob(user_id, profile_store.PROFILE_BLOB_KIND, pending_doc)

    pending = client.get(endpoint, headers=_admin_headers())
    assert pending.status_code == 200
    assert pending.get_json()["user"]["v2_profile"] == {
        "state": "pending",
        "document_status": "ok",
        "memory_chars": 0,
        "style_chars": 0,
        "user_chars": 0,
        "source": {
            "card_count": 7,
            "max_updated_at": "2026-08-01T10:00:00Z",
            "generated_at": "2026-08-01T10:01:00Z",
        },
        "last_attempt": {
            "reject_code": "provider_retry",
            "attempts": 2,
            "retry_not_before": 1_785_580_860.0,
        },
        "disabled": False,
    }

    ok_doc = profile_store.build_profile_document(
        user_id,
        state="ok",
        source={
            "card_count": 8,
            "max_updated_at": "2026-08-02T10:00:00Z",
            "generated_at": "2026-08-02T10:01:00Z",
        },
        last_attempt={
            "at": "2026-08-02T10:01:00Z",
            "reject_code": "",
            "attempts": 3,
            "retry_not_before": 0,
        },
        memory_text="private memory profile",
        style_text="private style profile",
        seal_text=lambda _uid, text: {
            "body_ct": f"private-ciphertext-{len(text)}",
            "nonce": "private-nonce",
        },
        disabled=True,
    )
    db.set_blob(user_id, profile_store.PROFILE_BLOB_KIND, ok_doc)

    ok = client.get(endpoint, headers=_admin_headers())
    assert ok.status_code == 200
    user_detail = ok.get_json()["user"]
    profile = user_detail["v2_profile"]
    assert profile == {
        "state": "ok",
        "document_status": "ok",
        "memory_chars": len("private memory profile"),
        "style_chars": len("private style profile"),
        "user_chars": len("private style profile"),
        "source": {
            "card_count": 8,
            "max_updated_at": "2026-08-02T10:00:00Z",
            "generated_at": "2026-08-02T10:01:00Z",
        },
        "last_attempt": {
            "reject_code": "",
            "attempts": 3,
            "retry_not_before": 0.0,
        },
        "disabled": True,
    }
    serialized = json.dumps(user_detail, sort_keys=True)
    assert "envelope" not in serialized
    assert "body_ct" not in serialized
    assert "private" not in serialized


def test_v2_profile_detail_is_allowlisted_and_read_safe(monkeypatch):
    from admin import data_track as data_track

    monkeypatch.setattr(
        data_track.db,
        "get_blob_strict",
        lambda *_args: {
            "v": 1,
            "state": "degraded",
            "memory": {"chars": 4, "envelope": {"body_ct": "secret"}},
            "user": {"chars": 5, "envelope": {"body_ct": "secret"}},
            "source": {
                "card_count": 6,
                "max_updated_at": "latest",
                "generated_at": "generated",
                "private_source": "secret",
            },
            "last_attempt": {
                "at": "2026-08-01T10:01:00Z",
                "reject_code": "provider_retry",
                "attempts": 7,
                "retry_disposition": "scheduled",
                "retry_family": "transient",
                "retry_attempts": 1,
                "retry_not_before": 8,
                "private_error": "secret",
            },
            "disabled": False,
            "plaintext": "secret",
        },
    )

    detail = data_track._v2_profile_detail("usr_test")
    assert detail["last_attempt"]["reject_code"] == "provider_retry"
    assert set(detail) == {
        "state", "document_status", "memory_chars", "style_chars",
        "user_chars", "source", "last_attempt", "disabled"
    }
    assert set(detail["source"]) == {
        "card_count", "max_updated_at", "generated_at"
    }
    assert set(detail["last_attempt"]) == {
        "reject_code", "attempts", "retry_not_before"
    }
    assert "secret" not in json.dumps(detail, sort_keys=True)

    def _fail(*_args):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(data_track.db, "get_blob_strict", _fail)
    assert data_track._v2_profile_detail("usr_test") == {
        "state": "read_error",
        "document_status": "unavailable",
    }


def test_detail_payload_exposes_capture_validation_decisions(client):
    from admin import data_track as data_track

    user_id, _api_key = _register(client)
    store = core_store.get_store(user_id)
    store.append_proactive_job({
        "job_id": "cap_validation",
        "job_kind": "memory_capture",
        "status": "completed",
        "status_reason": "supersede_without_target",
        "capture_result": {
            "status": "noop",
            "job_kind": "memory_capture",
            "applied": {"added": 0, "superseded": 0},
            "skipped": {
                "supersede_without_target": 1,
                "duplicate_active": 2,
            },
        },
        "memory_action_status": {"status": "ok", "results": 2, "effects": 0},
    })
    user_entry = next(u for u in registry._users if u["user_id"] == user_id)

    row = data_track._build_data_track_user(user_entry, include_detail=True)

    detail = row["memory_capture_validation"]
    assert detail["jobs_total"] == 1
    assert detail["skipped"] == {
        "supersede_without_target": 1,
        "duplicate_active": 2,
    }
    assert detail["jobs"][0]["job_id"] == "cap_validation"
    assert detail["jobs"][0]["capture_result"]["status"] == "noop"


def test_provider_attempts_detail_is_bounded_and_reports_more(monkeypatch):
    rows = [{"attempt_n": n} for n in range(1, 202)]
    calls = []

    def fake_log_read(user_id, stream, limit):
        calls.append((user_id, stream, limit))
        return rows

    monkeypatch.setattr(_dt.db, "log_read", fake_log_read)

    detail = _dt._provider_attempts_detail(SimpleNamespace(user_id="usr_ledger"))

    assert calls == [("usr_ledger", "provider_attempts", 201)]
    assert detail["coverage"] == "provider_runtime_and_model_api_probes"
    assert detail["has_more"] is True
    assert len(detail["attempts"]) == 200
    assert detail["attempts"][0]["attempt_n"] == 2
    assert detail["attempts"][-1]["attempt_n"] == 201


def test_perception_permissions_block_renders_granted_denied_and_switches():
    # The user-detail page shows a readable 感知授权 & 主动开关 block so "can't use
    # album/screen" is answerable on sight (granted vs not vs unknown).
    user = {
        "perception_permissions": {
            "permission_states": {"photos": "authorized", "screen": "denied", "location": "notDetermined"},
            "switches": {"photo_wake_照片唤醒": True, "screen_watch_屏幕观察": False},
            "wake_directive_configured": True,
            "wake_interval_sec": 7200,
        }
    }
    out = _dt._render_perception_permissions(user)
    assert "photos" in out and "已授权" in out          # granted
    assert "screen" in out and "未授权" in out           # denied
    assert "location" in out and "notDetermined" in out  # unknown -> raw state shown
    assert "photo_wake_照片唤醒" in out and "开" in out
    assert "screen_watch_屏幕观察" in out and "关" in out
    assert "自定义 wake 指令：已配置" in out
    assert "间隔：7200 秒" in out
    # empty permission_states -> explicit "no report" hint, not silence
    empty = _dt._render_perception_permissions({"perception_permissions": {"permission_states": {}, "switches": {}}})
    assert "permission_states 为空" in empty
    # no block at all when the user has no perception_permissions key
    assert _dt._render_perception_permissions({}) == ""


def test_perception_freshness_renders_separate_content_free_app_event_feeds():
    out = _dt._render_perception_freshness({
        "perception_freshness": {
            "fields": [{
                "field": "app_state",
                "capability": "app",
                "reported": True,
                "fresh": True,
                "last_report_ts": 1700000000.0,
                "age_sec": 5,
                "ttl_sec": 900,
            }],
            "recent_app_open_ts": 1700000001.0,
            "recent_app_open_age_sec": 4,
            "recent_app_close_ts": 1700000002.0,
            "recent_app_close_age_sec": 3,
        }
    })

    assert "最近 app_open 上报" in out
    assert "最近 app_close 上报" in out
    assert "private-app-name" not in out


def test_detail_payload_exposes_permission_metadata_without_private_directive(client):
    user_id, _api_key = _register(client)
    store = core_store.get_store(user_id)
    store.save_proactive_settings({
        "permission_states": {"photos": "authorized", "<script>": "<private>"},
        "photo_wake_enabled": False,
        "wake_directive": "private user-authored instruction",
        "wake_interval_sec": 3600,
    })
    user_entry = next(u for u in registry._users if u["user_id"] == user_id)

    row = _dt._build_data_track_user(user_entry, include_detail=True)
    pp = row["perception_permissions"]
    assert pp["permission_states"]["photos"] == "authorized"
    assert pp["switches"]["ambient_心跳"] is True
    assert "ambient_陪伴" not in pp["switches"]
    assert pp["switches"]["photo_wake_照片唤醒"] is False
    assert pp["wake_directive_configured"] is True
    assert pp["wake_interval_sec"] == 3600
    assert "private user-authored instruction" not in json.dumps(row)

    rendered = _dt._render_perception_permissions(row)
    assert "<script>" not in rendered
    assert "<private>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "&lt;private&gt;" in rendered


def test_connection_health_separates_never_connected_from_went_offline():
    """`connected` means "a binding row exists", not "a consumer is alive".

    Access bindings are append-only: the backend upserts one for the active
    route on every whoami and never clears the old ones, so the flag stays
    true forever once a user has merely *selected* the resident route. Reading
    it as liveness labelled 103 of 278 prod resident users 掉线 — "it broke, go
    debug their consumer" — when the truth was 未连接, "they never ran one".
    Support acts differently on those two, so the panel must not merge them.
    """
    from admin import data_track as _dt

    def health(last_seen_at: str, *, connected: bool = True) -> dict:
        return _dt._connection_health(
            "resident",
            [{"access_mode": "resident", "connected": connected,
              "last_seen_at": last_seen_at}],
            {},
        )

    never = health("")
    assert (never["status"], never["label"]) == ("idle", "未连接")
    assert never["stale_h"] is None, "no sighting means no age to report"

    # Same append-only binding, but this consumer really did phone home once.
    long_ago = (datetime.now() - timedelta(hours=_dt._CONN_STALE_H + 3)).isoformat()
    gone = health(long_ago)
    assert (gone["status"], gone["label"]) == ("offline", "掉线")
    assert gone["stale_h"] > _dt._CONN_STALE_H

    fresh = health(datetime.now().isoformat())
    assert (fresh["status"], fresh["label"]) == ("ok", "在线")

    # A live sighting outranks a missing/false binding flag: liveness is the
    # heartbeat, and the flag is the thing we stopped trusting.
    assert health(datetime.now().isoformat(), connected=False)["status"] == "ok"


def test_admin_data_track_reports_user_mcp_counts_without_secrets(client):
    """A saved-but-switched-off MCP server must be distinguishable from none.

    Support cannot answer "did this user actually configure a server?" today:
    the app's connection test is a control-plane probe that dials the server
    directly and passes without storing anything, so "the test was green" is
    not evidence. Worse, a server that is saved but toggled OFF still produces
    a NON-empty fingerprint and materializes cleanly on the consumer, then
    reaches the agent as zero servers — outwardly identical to a broken apply
    chain. ``enabled_count`` is what separates those two.
    """
    from hosted import mcp_core

    user_id, _ = _register(client)
    servers = [
        {"name": "alpha", "enabled": True,
         "config_envelope": {"id": "env_alpha",
                             "ciphertext": "ciphertext-that-must-not-leak"},
         "url_hint": "alpha.example.com", "header_names": ["authorization"]},
        {"name": "beta", "enabled": False,
         "config_envelope": {"id": "env_beta",
                             "ciphertext": "ciphertext-that-must-not-leak"},
         "url_hint": "beta.example.com", "header_names": ["x-api-key"]},
    ]
    db.set_blob(user_id, mcp_core.USER_MCP_BLOB, {
        "fingerprint": mcp_core.compute_fingerprint(servers),
        "servers": servers,
    })

    res = client.get("/v1/admin/data-track/users", headers=_admin_headers())
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    row = body["users"][0]

    assert row["user_mcp"]["configured"] is True
    assert row["user_mcp"]["configured_count"] == 2
    assert row["user_mcp"]["enabled_count"] == 1, (
        "the disabled server must not be counted as reaching the agent")
    # Derived, never hardcoded: this is the exact string the consumer compares
    # its applied fingerprint against, so a change to the basis must show up
    # here rather than silently passing against a stale literal.
    assert row["user_mcp"]["fingerprint"] == mcp_core.compute_fingerprint(servers)
    assert row["user_mcp"]["fingerprint"], (
        "servers exist, so the fingerprint is non-empty even with one disabled")

    # The envelope holds the url + auth headers. Counting happens in SQL
    # precisely so none of it enters the admin process.
    dumped = json.dumps(body)
    assert "ciphertext-that-must-not-leak" not in dumped
    assert "alpha.example.com" not in dumped
    assert "x-api-key" not in dumped


def test_admin_data_track_user_mcp_absent_reads_as_not_configured(client):
    """No blob at all is the third state, and must not look like a failure."""
    _register(client)
    res = client.get("/v1/admin/data-track/users", headers=_admin_headers())
    assert res.status_code == 200, res.get_data(as_text=True)
    row = res.get_json()["users"][0]
    assert row["user_mcp"] == {
        "configured": False, "configured_count": 0,
        "enabled_count": 0, "fingerprint": "",
    }


# --- T337 B5: memory breakdowns pushed down to the current page ------------
#
# memory_moments.doc is an encrypted card large enough to be stored out of
# line, so every extra ``doc->`` expression in a fleet-wide aggregate costs
# another detoast pass over every card in the fleet. by_type/by_source and the
# first/earliest/latest timestamps are rendered on the user's own row only, so
# they moved to a page-scoped read. ``total`` and ``last_created_at`` did NOT
# move: total feeds the summary and the memory sort key, and last_created_at is
# laundered through _latest_epoch into last_activity_at and then active_1d/3d.


def _seed_memory_users(client, count: int) -> list[str]:
    """Register ``count`` users, each with a distinct memory fingerprint.

    The Nth user gets N+1 cards, all of type/source scaled to N so no two users
    share a breakdown, and created_at/occurred_at windows that are disjoint
    across users — a row carrying a neighbour's breakdown cannot coincidentally
    look right.
    """
    user_ids: list[str] = []
    for index in range(count):
        uid, _ = _register(client)
        for card in range(index + 1):
            db.memory_upsert(
                uid,
                f"mom-{index}-{card}",
                f"2020-{index + 1:02d}-{card + 1:02d}T00:00:00Z",
                {
                    "type": f"type_{index}",
                    "source": f"source_{index}",
                    "created_at": f"2021-{index + 1:02d}-{card + 1:02d}T00:00:00Z",
                    "occurred_at": f"2020-{index + 1:02d}-{card + 1:02d}T00:00:00Z",
                },
            )
        user_ids.append(uid)
    return user_ids


def test_memory_breakdowns_on_nonzero_offset_equal_the_fleet_wide_read(client):
    """Field-equality against the implementation this replaced.

    The old fleet-wide read is still reachable via the default
    include_memory_breakdowns=True, so the expected value is computed by the
    code being replaced rather than restated by hand — restating it would let
    the same misunderstanding be written twice and agree with itself.
    """
    from admin import data_track as data_track_mod

    seeded = _seed_memory_users(client, 5)
    body = client.get(
        "/v1/admin/data-track/users?sort=memory&dir=asc&limit=2&offset=2",
        headers=_admin_headers(),
    ).get_json()
    page_ids = [row["user_id"] for row in body["users"]]
    assert page_ids == seeded[2:4], (
        "sort=memory&dir=asc orders by card count, which _seed_memory_users "
        f"made strictly increasing; expected {seeded[2:4]} got {page_ids}"
    )

    old = db.admin_data_track_snapshot(page_ids, include_legacy_background=False)
    for row in body["users"]:
        expected = data_track_mod._data_track_memory_from_snapshot(
            old[row["user_id"]]
        )
        assert row["memory"] == expected, (
            f"pushed-down memory for {row['user_id']} diverged from the "
            "fleet-wide read it replaced"
        )

    # The equality above shares _memory_breakdowns_into with the read it is
    # compared against, so a mutation inside that SQL moves both sides together
    # and stays green. These expectations are computed from the seed instead —
    # an oracle the implementation cannot follow. The Nth user's cards live on
    # a key space no other user uses, so a neighbour's data cannot pass either.
    for row in body["users"]:
        index = seeded.index(row["user_id"])
        cards = index + 1
        month = f"{index + 1:02d}"
        memory = row["memory"]
        assert memory["by_source"] == {f"source_{index}": cards}
        assert memory["by_type"][f"type_{index}"] == cards
        # first/earliest are MIN (day 01), latest is MAX (day == card count).
        assert memory["first_created_at"] == f"2021-{month}-01T00:00:00Z"
        assert memory["earliest_occurred_at"] == f"2020-{month}-01T00:00:00Z"
        assert memory["latest_occurred_at"] == f"2020-{month}-{cards:02d}T00:00:00Z"


def test_fleet_wide_read_does_not_scan_memory_docs_for_breakdowns(client, monkeypatch):
    """The scan that was removed must not come back alongside the page read.

    Restoring include_memory_breakdowns=True on the fleet call would leave
    every assertion about page correctness green — the page read still runs and
    still returns the right rows — while quietly reinstating the whole cost
    this batch exists to remove. The only place it is visible is the ids handed
    to the doc-scanning SQL.
    """
    seeded = _seed_memory_users(client, 5)
    calls: list[tuple[str, object]] = []
    real_connection = db._admin_data_track_connection

    class _Recorder:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=None, *args, **kwargs):
            calls.append((str(sql), params))
            if params is None:
                return self._inner.execute(sql, *args, **kwargs)
            return self._inner.execute(sql, params, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    @contextlib.contextmanager
    def recording_connection(*args, **kwargs):
        with real_connection(*args, **kwargs) as conn:
            yield _Recorder(conn)

    monkeypatch.setattr(db, "_admin_data_track_connection", recording_connection)
    body = client.get(
        "/v1/admin/data-track/users?sort=memory&dir=asc&limit=2&offset=2",
        headers=_admin_headers(),
    ).get_json()
    page_ids = [row["user_id"] for row in body["users"]]
    assert page_ids == seeded[2:4]

    breakdown_ids = [
        params[1]
        for sql, params in calls
        if "COALESCE(NULLIF(doc->>%s" in sql and "memory_moments" in sql
    ]
    assert breakdown_ids, (
        "no by_type/by_source SQL ran at all — this guard would pass "
        "vacuously; the search string no longer matches the query"
    )
    assert all(ids == page_ids for ids in breakdown_ids), (
        "the by_type/by_source scan must only ever see the current page; got "
        f"{[len(ids) for ids in breakdown_ids]} ids per call for a page of "
        f"{len(page_ids)} out of {len(seeded)} seeded users"
    )

    fleet_memory_sql = [
        sql for sql, _ in calls
        if "FROM memory_moments" in sql and "COUNT(*)::int AS total" in sql
    ]
    assert len(fleet_memory_sql) == 1
    assert "occurred_at" not in fleet_memory_sql[0], (
        "the fleet-wide memory aggregate must carry no occurred_at expression: "
        "each extra doc-> expression is another detoast pass of every card"
    )
    assert "MIN(" not in fleet_memory_sql[0]


def test_memory_breakdown_reader_receives_exactly_the_page_ids(client, monkeypatch):
    """The pushdown itself, not just its result.

    Field-equality alone cannot see this: handing the reader every fleet id
    still produces correct rows, which is exactly the bug being fixed.
    """
    seeded = _seed_memory_users(client, 5)
    seen: list[list[str]] = []
    real = db.admin_memory_breakdowns

    def observed(user_ids, *args, **kwargs):
        seen.append(list(user_ids))
        return real(user_ids, *args, **kwargs)

    monkeypatch.setattr(db, "admin_memory_breakdowns", observed)
    body = client.get(
        "/v1/admin/data-track/users?sort=memory&dir=asc&limit=2&offset=2",
        headers=_admin_headers(),
    ).get_json()

    page_ids = [row["user_id"] for row in body["users"]]
    assert page_ids == seeded[2:4]
    assert seen == [page_ids], (
        "the memory breakdown read must receive exactly the returned page, in "
        f"order; got {seen} for page {page_ids} out of {len(seeded)} users"
    )


def test_last_created_at_still_covers_users_off_the_current_page(client):
    """last_created_at must NOT be pushed down.

    It reaches the fleet summary indirectly — _latest_epoch folds it into
    last_activity_at, which drives active_1d/3d — so a user whose only recent
    activity is a memory card must still be counted while off the page.
    """
    _seed_memory_users(client, 3)
    recent_uid, _ = _register(client)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    db.memory_upsert(
        recent_uid,
        "mom-recent",
        now.isoformat(),
        {"type": "fact", "source": "chat", "created_at": now.isoformat()},
    )

    # recent_uid holds a single card, so ordering by card count descending and
    # keeping one row puts it off the page.
    body = client.get(
        "/v1/admin/data-track/users?sort=memory&dir=desc&limit=1&offset=0",
        headers=_admin_headers(),
    ).get_json()
    assert recent_uid not in [row["user_id"] for row in body["users"]]

    # Asserting on active_1d here would prove nothing: every user in this test
    # registered seconds ago, and registered_at is itself a _latest_epoch input,
    # so the count stays put whatever memory does. Anchor on the value instead.
    fleet = db.admin_data_track_snapshot(
        [recent_uid],
        include_legacy_background=False,
        include_memory_breakdowns=False,
    )
    assert fleet[recent_uid]["memory"]["last_created_at"] == now.isoformat(), (
        "the fleet-wide read must still carry last_created_at with the "
        "breakdowns pushed down — it is the only memory field that reaches "
        "active_1d/3d, and off-page users are never re-read"
    )

    # ...and that value really is what drives last_activity_at. Registration
    # happened microseconds before the card, so the card is the newest input
    # and last_activity_at must equal it exactly.
    own_page = client.get(
        f"/v1/admin/data-track/users?q={quote(recent_uid)}&limit=10",
        headers=_admin_headers(),
    ).get_json()
    own_row = next(r for r in own_page["users"] if r["user_id"] == recent_uid)
    # Compared as instants, not strings: the two fields are rendered by
    # different helpers (memory keeps the offset, last_activity_at does not),
    # and it is the instant _latest_epoch propagates that matters here.
    from core import util as core_util

    assert own_row["memory"]["last_created_at"]
    assert core_util._to_epoch(own_row["last_activity_at"]) == pytest.approx(
        core_util._to_epoch(own_row["memory"]["last_created_at"])
    ), (
        "last_activity_at stopped tracking the newest memory card; the "
        "_latest_epoch chain into active_1d/3d is broken"
    )


def test_memory_totals_are_fleet_wide_not_page_wide(client):
    """total feeds the summary and the memory sort key, so it cannot be paged."""
    _seed_memory_users(client, 4)

    def _memory_total(query: str) -> int:
        body = client.get(
            f"/v1/admin/data-track/users?{query}", headers=_admin_headers()
        ).get_json()
        return int(body["summary"]["memory_total"])

    full = _memory_total("limit=100&offset=0")
    paged = _memory_total("limit=1&offset=2")
    assert full == paged == 1 + 2 + 3 + 4, (
        "memory.total is a fleet aggregate; it must not follow the page "
        f"window (full={full} paged={paged})"
    )


def test_breakdown_read_failure_is_reported_not_zeroed(client, monkeypatch):
    """A failed page read must not look like a user with no memory cards.

    Before the pushdown these queries lived inside admin_data_track_snapshot,
    whose except block marks every row's snapshot_read_status. Moving them out
    created a path where the same failure could return an empty dict instead —
    indistinguishable, on the rendered page, from a real absence of data.
    The failure is injected where it actually occurs: the DB read.
    """
    _seed_memory_users(client, 3)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated breakdown read failure")

    monkeypatch.setattr(db, "_memory_breakdowns_into", boom)

    breakdowns, read_status = db.admin_memory_breakdowns(["usr_whatever"])
    assert breakdowns == {}
    assert read_status["level"] != "ok", (
        "a failed breakdown read reported level=ok; the caller cannot tell it "
        "apart from a user who genuinely has no cards"
    )
    assert read_status["message"]

    body = client.get(
        "/v1/admin/data-track/users?sort=memory&dir=asc&limit=2",
        headers=_admin_headers(),
    ).get_json()
    assert body["users"], "the page must still render, degraded"
    for row in body["users"]:
        assert row["snapshot_read_status"]["level"] != "ok", (
            "the degraded read never reached the row the admin actually sees"
        )


def _seed_log_stream_users(client, count: int) -> list[str]:
    """Users from ``_seed_memory_users``, each with a distinct log fingerprint.

    The Nth user gets N+1 bootstrap_events, written through db.log_append with
    no ts — the same way both production writers do, which is what makes
    bootstrap_events.last_at structurally empty.

    The N+1 memory_capture_jobs rows are seeded on purpose even though nothing
    reads them: they are what makes "no query names memory_capture_jobs" a
    statement about the SQL rather than about an empty table.
    """
    user_ids = _seed_memory_users(client, count)
    for index, uid in enumerate(user_ids):
        for entry in range(index + 1):
            db.log_append(uid, "bootstrap_events", {
                "event_type": f"boot_{index}", "success": True,
            })
            db.log_append(uid, "memory_capture_jobs", {
                "status": f"status_{index}", "mode": f"mode_{index}",
            })
    return user_ids


def test_bootstrap_events_on_nonzero_offset_equal_the_fleet_wide_read(client):
    """Field-equality against the implementation this replaced."""
    from admin import data_track as data_track_mod

    seeded = _seed_log_stream_users(client, 5)
    body = client.get(
        "/v1/admin/data-track/users?sort=memory&dir=asc&limit=2&offset=2",
        headers=_admin_headers(),
    ).get_json()
    page_ids = [row["user_id"] for row in body["users"]]
    assert page_ids == seeded[2:4]

    old = db.admin_data_track_snapshot(page_ids, include_legacy_background=False)
    for row in body["users"]:
        expected = data_track_mod._data_track_bootstrap_from_snapshot(
            old[row["user_id"]]
        )
        assert row["bootstrap_events"] == expected, (
            f"pushed-down bootstrap_events for {row['user_id']} diverged from "
            "the fleet-wide read it replaced"
        )

    # Seed-derived, so a mutation inside the shared helper cannot move both
    # sides of the comparison above together.
    for row in body["users"]:
        index = seeded.index(row["user_id"])
        assert row["bootstrap_events"]["events"] == index + 1
        assert row["bootstrap_events"]["last_at"] == "", (
            "bootstrap_events rows are written without a ts, so last_at must "
            "stay empty — that is the premise this whole pushdown rests on"
        )


def test_fleet_wide_read_does_not_scan_the_paged_log_streams(client, monkeypatch):
    """The fleet log aggregate must stop naming bootstrap_events, the page read
    must only ever see the page, and memory_capture_jobs must not be scanned by
    either one.

    memory_capture_jobs is the sharper half: it has no consumer anywhere and no
    log_trim, so re-reading it per page would keep an unbounded GROUP BY alive
    for nothing — and it would share a query with bootstrap_events, so a heavy
    user could time that read out and mark a readable row degraded. The seed
    writes rows to the stream precisely so this stays a claim about the query
    rather than about an empty table.
    """
    seeded = _seed_log_stream_users(client, 5)
    calls: list[tuple[str, object]] = []
    real_connection = db._admin_data_track_connection

    class _Recorder:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=None, *args, **kwargs):
            calls.append((str(sql), params))
            if params is None:
                return self._inner.execute(sql, *args, **kwargs)
            return self._inner.execute(sql, params, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    @contextlib.contextmanager
    def recording_connection(*args, **kwargs):
        with real_connection(*args, **kwargs) as conn:
            yield _Recorder(conn)

    monkeypatch.setattr(db, "_admin_data_track_connection", recording_connection)
    body = client.get(
        "/v1/admin/data-track/users?sort=memory&dir=asc&limit=2&offset=2",
        headers=_admin_headers(),
    ).get_json()
    page_ids = [row["user_id"] for row in body["users"]]
    assert page_ids == seeded[2:4]

    fleet_log_sql = [
        sql for sql, _ in calls
        if "FROM user_logs" in sql and "'memory_changes'" in sql
    ]
    assert len(fleet_log_sql) == 1, (
        "no fleet-wide user_logs aggregate ran — this guard would pass "
        "vacuously; the search string no longer matches the query"
    )
    assert "bootstrap_events" not in fleet_log_sql[0], (
        "bootstrap_events is back in the fleet-wide user_logs aggregate"
    )
    assert "'memory_changes'" in fleet_log_sql[0], (
        "memory_changes must stay fleet-wide: it is the second element of the "
        "memory sort tuple, so it takes part in a full-set ordering"
    )

    paged_calls = [
        (sql, params)
        for sql, params in calls
        if "FROM user_logs" in sql and "stream = ANY(%s)" in sql
    ]
    assert paged_calls, "the page-scoped log read never ran"
    assert all(params[0] == page_ids for _, params in paged_calls), (
        "the page-scoped log read must only ever see the current page; got "
        f"{[len(params[0]) for _, params in paged_calls]} ids per call for a "
        f"page of {len(page_ids)} out of {len(seeded)} seeded users"
    )
    assert all(list(params[1]) == ["bootstrap_events"] for _, params in paged_calls), (
        "the page-scoped log read must ask for exactly bootstrap_events; got "
        f"{[list(params[1]) for _, params in paged_calls]}"
    )

    # And nowhere else either — not the fleet aggregate, not the page read, not
    # a query someone adds later that happens to run on this request.
    scanned_capture_jobs = [sql for sql, _ in calls if "memory_capture_jobs" in sql]
    assert not scanned_capture_jobs, (
        "memory_capture_jobs is untrimmed and has no consumer; something is "
        f"scanning it again: {scanned_capture_jobs}"
    )


def test_paged_log_reader_receives_exactly_the_page_ids(client, monkeypatch):
    seeded = _seed_log_stream_users(client, 5)
    seen: list[list[str]] = []
    real = db.admin_paged_log_streams

    def observed(user_ids, *args, **kwargs):
        seen.append(list(user_ids))
        return real(user_ids, *args, **kwargs)

    monkeypatch.setattr(db, "admin_paged_log_streams", observed)
    body = client.get(
        "/v1/admin/data-track/users?sort=memory&dir=asc&limit=2&offset=2",
        headers=_admin_headers(),
    ).get_json()

    page_ids = [row["user_id"] for row in body["users"]]
    assert page_ids == seeded[2:4]
    assert seen == [page_ids], (
        "the paged log read must receive exactly the returned page, in order; "
        f"got {seen} for page {page_ids} out of {len(seeded)} users"
    )


def test_memory_changes_stays_fleet_wide_for_the_memory_sort(client):
    """memory_changes must NOT follow bootstrap_events onto the page.

    It is the second element of the memory sort tuple, so it breaks ties across
    the whole fleet — before any page window exists. Three users hold one card
    each, so total ties and only ``changes`` can order them; the change counts
    run opposite to registration order, which is what the sort falls back to
    when the tie-break reads zero.
    """
    user_ids: list[str] = []
    for index in range(3):
        uid, _ = _register(client)
        db.memory_upsert(
            uid, f"mom-tie-{index}", "2020-01-01T00:00:00Z",
            {"type": "fact", "source": "chat", "created_at": "2021-01-01T00:00:00Z"},
        )
        for change in range(3 - index):
            db.log_append(uid, "memory_changes", {"action": "add", "n": change})
        user_ids.append(uid)

    body = client.get(
        "/v1/admin/data-track/users?sort=memory&dir=desc&limit=1&offset=0",
        headers=_admin_headers(),
    ).get_json()
    top = body["users"][0]
    assert top["user_id"] == user_ids[0], (
        "the memory sort stopped tie-breaking on changes; it fell back to "
        f"registration order (would give {user_ids[-1]}), which is what "
        "happens the moment memory_changes is read per page instead of "
        "fleet-wide"
    )
    assert top["memory"]["changes"] == 3


@pytest.mark.parametrize("reader_name", [
    "admin_memory_breakdowns",
    "admin_screen_frames",
    "admin_paged_log_streams",
])
def test_page_readers_lease_no_connection_for_an_empty_page(monkeypatch, reader_name):
    """An empty page must cost zero database work, not one connection each.

    The `if not ids` early return is the cheap part; the part worth a test is
    that it happens *before* the pool is touched. Asserting on the return value
    alone would pass just as happily if the reader opened a connection, ran
    three aggregates over no ids and mapped the empty result — which is the
    shape B1 shipped once already.
    """
    leases: list[object] = []

    def refuse(*args, **kwargs):
        leases.append(args)
        raise AssertionError(
            f"{reader_name} leased a connection for an empty page"
        )

    monkeypatch.setattr(db, "_admin_data_track_connection", refuse)
    result, read_status = getattr(db, reader_name)([])

    assert result == {}
    assert read_status == {"level": "ok", "message": ""}, (
        "an empty page is not a read failure; it must report ok so the row "
        "does not pick up a snapshot_read_status it did not earn"
    )
    assert leases == []


def test_paged_log_read_failure_is_reported_not_zeroed(client, monkeypatch):
    """A failed page read must not look like a user with no bootstrap events."""
    _seed_log_stream_users(client, 3)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated paged log read failure")

    monkeypatch.setattr(db, "_paged_log_streams_into", boom)

    logs, read_status = db.admin_paged_log_streams(["usr_whatever"])
    assert logs == {}
    assert read_status["level"] != "ok", (
        "a failed paged log read reported level=ok; the caller cannot tell it "
        "apart from a user who genuinely has no bootstrap events"
    )
    assert read_status["message"]

    body = client.get(
        "/v1/admin/data-track/users?sort=memory&dir=asc&limit=2",
        headers=_admin_headers(),
    ).get_json()
    assert body["users"], "the page must still render, degraded"
    for row in body["users"]:
        assert row["snapshot_read_status"]["level"] != "ok", (
            "the degraded read never reached the row the admin actually sees"
        )


def _seed_frame_users(client, count: int) -> list[str]:
    """Users from ``_seed_memory_users``, each also given a frame fingerprint.

    The memory cards are only there to make the page order deterministic —
    screen frames have no sort key of their own. The Nth user gets N+1 frames,
    exactly one of them in R2, and a newest frame ``600 - 60N`` seconds old, so
    total / inline_count / r2_count / latest_age_sec are all distinct per user
    and a row carrying a neighbour's counters cannot look right. A user's own
    frames are spread an hour apart so that the newest and the oldest cannot be
    confused for each other inside the tolerance latest_age_sec is read with.
    """
    user_ids = _seed_memory_users(client, count)
    with db.get_pool().connection() as conn:
        for index, uid in enumerate(user_ids):
            newest_age = 600 - 60 * index
            for frame in range(index + 1):
                conn.execute(
                    """
                    INSERT INTO frame_envelopes
                        (user_id, frame_id, ts, doc, env_meta, body_key)
                    VALUES (%s, %s, extract(epoch FROM now()) - %s,
                            %s::jsonb, NULL, %s)
                    """,
                    (
                        uid,
                        f"frame-{index}-{frame}",
                        newest_age + frame * 3600,
                        json.dumps({"v": 1, "body_ct": "must-not-leak"}),
                        "frames/test/body" if frame == index else None,
                    ),
                )
    return user_ids


def _seed_active_broadcast(user_id: str) -> None:
    with db.get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO user_blobs (user_id, kind, doc)
            VALUES (
                %s,
                'perception_state',
                jsonb_build_object(
                    'broadcast_state', jsonb_build_object(
                        'v', 'on', 'ts', extract(epoch FROM now())
                    ),
                    'broadcast_active', jsonb_build_object(
                        'v', true, 'ts', extract(epoch FROM now())
                    )
                )
            )
            ON CONFLICT (user_id, kind) DO UPDATE SET doc = EXCLUDED.doc
            """,
            (user_id,),
        )


def test_screen_frames_on_nonzero_offset_equal_the_fleet_wide_read(client):
    """Field-equality against the implementation this replaced."""
    from admin import data_track as data_track_mod

    seeded = _seed_frame_users(client, 5)
    body = client.get(
        "/v1/admin/data-track/users?sort=memory&dir=asc&limit=2&offset=2",
        headers=_admin_headers(),
    ).get_json()
    page_ids = [row["user_id"] for row in body["users"]]
    assert page_ids == seeded[2:4]

    old = db.admin_data_track_snapshot(page_ids, include_legacy_background=False)
    for row in body["users"]:
        expected = data_track_mod._data_track_screen_frames_from_snapshot(
            old[row["user_id"]]
        )
        got = dict(row["screen_frames"])
        # latest_age_sec is derived from wall-clock at read time, so the two
        # reads legitimately differ by the seconds between them.
        assert got.pop("latest_age_sec") == pytest.approx(
            expected.pop("latest_age_sec"), abs=5
        )
        assert got == expected, (
            f"pushed-down screen_frames for {row['user_id']} diverged from the "
            "fleet-wide read it replaced"
        )

    # The equality above shares _screen_frames_into with the read it compares
    # against, so a mutation inside that SQL moves both sides together and stays
    # green. These expectations come from the seed instead.
    for row in body["users"]:
        index = seeded.index(row["user_id"])
        frames = row["screen_frames"]
        assert frames["total"] == index + 1
        assert frames["inline_count"] == index
        assert frames["r2_count"] == 1
        newest_age = 600 - 60 * index
        assert newest_age <= frames["latest_age_sec"] < newest_age + 30, (
            "latest_at is not the newest frame for this user"
        )
    assert "must-not-leak" not in json.dumps(body)


def test_fleet_wide_read_does_not_scan_frame_envelopes(client, monkeypatch):
    """Restoring include_screen_frames=True would leave every page assertion
    green while reinstating the fleet-wide scan this batch removes. The ids
    handed to the SQL are the only place it is visible."""
    seeded = _seed_frame_users(client, 5)
    calls: list[tuple[str, object]] = []
    real_connection = db._admin_data_track_connection

    class _Recorder:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=None, *args, **kwargs):
            calls.append((str(sql), params))
            if params is None:
                return self._inner.execute(sql, *args, **kwargs)
            return self._inner.execute(sql, params, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    @contextlib.contextmanager
    def recording_connection(*args, **kwargs):
        with real_connection(*args, **kwargs) as conn:
            yield _Recorder(conn)

    monkeypatch.setattr(db, "_admin_data_track_connection", recording_connection)
    body = client.get(
        "/v1/admin/data-track/users?sort=memory&dir=asc&limit=2&offset=2",
        headers=_admin_headers(),
    ).get_json()
    page_ids = [row["user_id"] for row in body["users"]]
    assert page_ids == seeded[2:4]

    frame_ids = [
        params[0]
        for sql, params in calls
        if "FROM frame_envelopes" in sql and "r2_count" in sql
    ]
    assert frame_ids, (
        "no frame_envelopes aggregate ran at all — this guard would pass "
        "vacuously; the search string no longer matches the query"
    )
    assert all(ids == page_ids for ids in frame_ids), (
        "the frame aggregate must only ever see the current page; got "
        f"{[len(ids) for ids in frame_ids]} ids per call for a page of "
        f"{len(page_ids)} out of {len(seeded)} seeded users"
    )


def test_screen_frame_reader_receives_exactly_the_page_ids(client, monkeypatch):
    """The pushdown itself, not just its result: handing the reader every fleet
    id still produces correct rows, which is exactly the bug being fixed."""
    seeded = _seed_frame_users(client, 5)
    seen: list[list[str]] = []
    real = db.admin_screen_frames

    def observed(user_ids, *args, **kwargs):
        seen.append(list(user_ids))
        return real(user_ids, *args, **kwargs)

    monkeypatch.setattr(db, "admin_screen_frames", observed)
    body = client.get(
        "/v1/admin/data-track/users?sort=memory&dir=asc&limit=2&offset=2",
        headers=_admin_headers(),
    ).get_json()

    page_ids = [row["user_id"] for row in body["users"]]
    assert page_ids == seeded[2:4]
    assert seen == [page_ids], (
        "the screen-frame read must receive exactly the returned page, in "
        f"order; got {seen} for page {page_ids} out of {len(seeded)} users"
    )


def test_broadcast_stalled_still_derives_from_both_halves_on_a_paged_row(client):
    """broadcast_stalled needs the page-scoped frame recency *and* the still
    fleet-wide broadcast report. Patching the counters onto the row instead of
    re-running the mapper would leave every count right and this flag wrong."""
    seeded = _seed_frame_users(client, 5)
    _seed_active_broadcast(seeded[2])

    body = client.get(
        "/v1/admin/data-track/users?sort=memory&dir=asc&limit=1&offset=2",
        headers=_admin_headers(),
    ).get_json()
    row = body["users"][0]
    assert row["user_id"] == seeded[2]
    frames = row["screen_frames"]
    assert frames["broadcast_report_active"] is True
    assert frames["broadcast_stalled"] is True, (
        "the device still reports broadcast=on and its newest frame is "
        f"{frames['latest_age_sec']}s old, but the paged row does not raise "
        "the stalled alert the admin page renders"
    )


def test_screen_frame_read_failure_is_reported_not_zeroed(client, monkeypatch):
    """A failed page read must not look like a user who never shared a screen.

    Before the pushdown this query lived inside admin_data_track_snapshot,
    whose except block marks every row's snapshot_read_status. Moving it out
    created a path where the same failure could return an empty dict instead.
    The failure is injected where it actually occurs: the DB read.
    """
    _seed_frame_users(client, 3)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated frame read failure")

    monkeypatch.setattr(db, "_screen_frames_into", boom)

    frames, read_status = db.admin_screen_frames(["usr_whatever"])
    assert frames == {}
    assert read_status["level"] != "ok", (
        "a failed frame read reported level=ok; the caller cannot tell it "
        "apart from a user who genuinely has no frames"
    )
    assert read_status["message"]

    body = client.get(
        "/v1/admin/data-track/users?sort=memory&dir=asc&limit=2",
        headers=_admin_headers(),
    ).get_json()
    assert body["users"], "the page must still render, degraded"
    for row in body["users"]:
        assert row["snapshot_read_status"]["level"] != "ok", (
            "the degraded read never reached the row the admin actually sees"
        )


def test_frame_recency_is_not_a_last_activity_input(client):
    """The premise of this pushdown, guarded.

    Pushing screen_frames onto the page is sound only because latest_ts is not
    one of _latest_epoch's inputs — off-page users are never re-read, so if it
    ever became one, last_activity_at (and active_1d/3d behind it) would
    silently stop seeing frames. The frame is dated in the future so that a
    regression is visible: a past frame could never move a MAX that already
    contains a registration timestamp from seconds ago.
    """
    from core import util as core_util

    user_id, _ = _register(client)
    with db.get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO frame_envelopes
                (user_id, frame_id, ts, doc, env_meta, body_key)
            VALUES (%s, 'frame_future', extract(epoch FROM now()) + 3600,
                    %s::jsonb, NULL, NULL)
            """,
            (user_id, json.dumps({"v": 1, "body_ct": "must-not-leak"})),
        )

    body = client.get(
        f"/v1/admin/data-track/users?q={quote(user_id)}&limit=10",
        headers=_admin_headers(),
    ).get_json()
    row = next(r for r in body["users"] if r["user_id"] == user_id)
    assert row["screen_frames"]["total"] == 1, "the frame was not seeded"
    assert core_util._to_epoch(row["last_activity_at"]) == pytest.approx(
        core_util._to_epoch(row["registered_at"]), abs=5
    ), (
        "last_activity_at moved with a screen frame; latest_ts has become a "
        "_latest_epoch input, and the page-scoped screen-frame read now hides "
        "activity for every user off the current page"
    )
