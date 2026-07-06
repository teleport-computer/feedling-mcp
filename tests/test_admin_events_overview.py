"""DB-backed coverage for db.admin_events_overview().

The event-health board depends on PostgreSQL JSONB aggregation and route joins,
so this intentionally follows tests/test_db.py: it runs only when conftest has
provisioned a real test database.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

if not os.environ.get("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set - needs a real Postgres", allow_module_level=True)

import db  # noqa: E402
from conftest import seed_user  # noqa: E402

db.init_schema()


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _iso(base: datetime, seconds: int) -> str:
    return (base + timedelta(seconds=seconds)).isoformat()


def test_admin_events_overview_aggregates_routes_events_and_durations():
    now = datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc)
    u_res = _uid("events_res")
    u_api = _uid("events_api")
    u_import = _uid("events_import")
    for uid in (u_res, u_api, u_import):
        seed_user(uid)
    db.set_blob(u_res, "onboarding_route", {"route": "resident"})
    db.set_blob(u_api, "onboarding_route", {"route": "model_api"})
    db.set_blob(u_import, "onboarding_route", {"route": "official_import"})
    before = db.admin_events_overview()

    db.log_append(u_api, "proactive_jobs", {
        "job_id": "pj_screen",
        "job_kind": "screen_tick",
        "status": "delivered",
        "created_at": _iso(now, 0),
        "posted_at": _iso(now, 3),
    }, ts=now.timestamp(), item_key="pj_screen")
    db.log_append(u_import, "proactive_jobs", {
        "job_id": "pj_trigger",
        "trigger": "scheduled_wake",
        "status": "failed",
        "created_at": _iso(now, 0),
        "failed_at": _iso(now, 7),
    }, ts=now.timestamp(), item_key="pj_trigger")
    db.log_append(u_import, "proactive_jobs", {
        "job_id": "cap_resident",
        "job_kind": "memory_capture",
        "status": "completed",
        "created_at": _iso(now, 0),
        "completed_at": _iso(now, 20),
    }, ts=now.timestamp(), item_key="cap_resident")
    db.log_append(u_api, "memory_capture_jobs", {
        "job_id": "mc_api",
        "mode": "recap",
        "status": "failed",
        "created_at": _iso(now, 0),
        "completed_at": _iso(now, 30),
    }, ts=now.timestamp(), item_key="mc_api")

    with db.get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO genesis_import_jobs
              (user_id, job_id, status, source_kind, metadata, created_at, updated_at, completed_at)
            VALUES
              (%s, %s, %s, %s, %s, now(), now(), now()),
              (%s, %s, %s, %s, %s, now(), now(), NULL)
            """,
            (
                u_res, "g_first", "done", "history", Jsonb({"mode": "onboarding"}),
                u_api, "g_second", "failed", "history", Jsonb({"mode": "add_memory"}),
            ),
        )

    db.chat_append(u_api, "m_user_api", now.timestamp(), {
        "id": "m_user_api", "role": "user", "source": "chat",
    }, 5000)
    db.chat_append(u_api, "m_real_api", now.timestamp() + 1, {
        "id": "m_real_api", "role": "agent", "source": "model_api",
    }, 5000)
    db.chat_append(u_api, "m_fallback_api", now.timestamp() + 2, {
        "id": "m_fallback_api", "role": "agent", "source": "foreground_fallback",
    }, 5000)
    db.chat_append(u_import, "m_proactive_fallback", now.timestamp(), {
        "id": "m_proactive_fallback", "role": "openclaw", "source": "proactive_fallback",
    }, 5000)

    out = db.admin_events_overview()

    def rows(section: str, *keys: str) -> dict[tuple, dict]:
        return {tuple(r[k] for k in keys): r for r in out[section]}

    def before_rows(section: str, *keys: str) -> dict[tuple, dict]:
        return {tuple(r[k] for k in keys): r for r in before[section]}

    def delta(section: str, key: tuple, field: str, *keys: str) -> int:
        after_row = rows(section, *keys).get(key, {})
        before_row = before_rows(section, *keys).get(key, {})
        return int(after_row.get(field) or 0) - int(before_row.get(field) or 0)

    proactive = rows("proactive", "route", "lane")
    assert proactive[("model_api", "screen")]["success"] == 1
    if not before_rows("proactive", "route", "lane").get(("model_api", "screen")):
        assert proactive[("model_api", "screen")]["median_dur"] == pytest.approx(3.0)
    assert proactive[("official_import", "trigger")]["failed"] == 1
    if not before_rows("proactive", "route", "lane").get(("official_import", "trigger")):
        assert proactive[("official_import", "trigger")]["median_dur"] == pytest.approx(7.0)
    assert delta("proactive", ("official_import", "other"), "total", "route", "lane") == 0

    capture = rows("capture", "route")
    assert delta("capture", ("official_import",), "success", "route") == 1
    assert delta("capture", ("model_api",), "failed", "route") == 1
    if not before_rows("capture", "route").get(("official_import",)):
        assert capture[("official_import",)]["median_dur"] == pytest.approx(20.0)
    if not before_rows("capture", "route").get(("model_api",)):
        assert capture[("model_api",)]["median_dur"] == pytest.approx(30.0)

    assert delta("genesis", ("resident", "first"), "success", "route", "distill") == 1
    assert delta("genesis", ("model_api", "second"), "failed", "route", "distill") == 1

    assert delta("reply", ("model_api",), "user_msgs", "route") == 1
    assert delta("reply", ("model_api",), "real_replies", "route") == 1
    assert delta("reply", ("model_api",), "fallback_replies", "route") == 1
    assert delta("reply", ("official_import",), "real_replies", "route") == 0
    assert delta("reply", ("official_import",), "fallback_replies", "route") == 0
