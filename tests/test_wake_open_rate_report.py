"""T723 E: heartbeat open rate is reported per model family, exclusions itemised."""
from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

from tools import wake_open_rate_report as report  # noqa: E402


@pytest.mark.parametrize("model,family", [
    ("glm-5.3-flash", "glm"),
    ("[特价]GLM-5.3", "glm"),
    ("z-ai/glm-5.3-flash", "glm"),
    ("gemini-3-flash-preview", "gemini"),
    ("逆[Ag1-量-0.2x]gemini-3.7-flash-high", "gemini"),
    ("deepseek-v4-flash", "deepseek"),
    ("按量反重力B1/claude-sonnet-4-6", "claude"),
    ("grok-4.6", "grok"),
    ("mimo-v2.5", "other"),
    ("", "no_trace"),
    (None, "no_trace"),
])
def test_model_family_labels(model, family):
    assert report.model_family(model) == family


def test_open_rate_counts_only_model_choices_and_itemises_exclusions():
    rows = [
        ("completed", None, "", "glm-5.3-flash"),
        ("completed", "sleep", "nothing new", "glm-5.3-flash"),
        ("completed", "sleep", "nothing new", "glm-5.3-flash"),
        ("completed", "sleep", report.CIRCUIT_SLEEP_REASON, "glm-5.3-flash"),
        ("failed", None, "", "glm-5.3-flash"),
        ("completed", None, "", "gemini-2.5-flash"),
        ("superseded", None, "", None),
        ("expired", None, "", None),
    ]
    out = report.summarize(rows)
    glm = out["by_family"]["glm"]
    assert (glm["spoke"], glm["silent"]) == (1, 2)
    assert glm["open_rate"] == round(1 / 3, 4)
    assert glm["excluded"] == {"circuit": 1, "failed": 1}
    assert out["by_family"]["gemini"]["open_rate"] == 1.0
    assert out["by_family"]["no_trace"] == {
        "spoke": 0, "silent": 0, "open_rate": None,
        "excluded": {"expired": 1, "superseded": 1},
    }
    total = out["total"]
    assert (total["spoke"], total["silent"]) == (2, 2)
    assert total["excluded"] == {"circuit": 1, "expired": 1, "failed": 1, "superseded": 1}
    assert list(out["by_family"])[0] == "glm"


def test_system_written_sleep_is_excluded_not_counted_as_silence():
    rows = [
        ("completed", None, "", "glm-5.3"),
        ("completed", "sleep", "empty_visible_reply_suppressed", "glm-5.3"),
    ]
    glm = report.summarize(rows)["by_family"]["glm"]
    assert glm["open_rate"] == 1.0
    assert glm["excluded"] == {"empty_reply_suppressed": 1}


def test_system_sleep_reasons_match_every_worker_producer():
    """Every non-model sleep reason the worker writes must be excluded here."""
    import re
    from model_api_runtime.v2 import jobs_store

    source = (ROOT / "backend" / "model_api_runtime" / "v2" / "worker.py").read_text()
    literal = set(re.findall(r'wake_result="sleep",\s*wake_result_reason="([a-z0-9_]+)"', source))
    via_constant = {
        getattr(jobs_store, name)
        for name in re.findall(r"stay_silent_reason = jobs_store\.([A-Z_]+)", source)
    }
    assert literal and via_constant
    assert literal | via_constant == set(report.SYSTEM_SLEEP_REASONS)
    # The only other assignment is the model's own stay_silent reason.
    others = re.findall(r"stay_silent_reason = (?!jobs_store\.)(.+)", source)
    assert others == ['str(reason or "").strip()[:500]']


def test_render_text_has_one_line_per_family():
    text = report.render_text(report.summarize([
        ("completed", None, "", "glm-5.3"), ("completed", "sleep", "x", "deepseek-v4-flash"),
    ]))
    assert text.splitlines()[1].startswith("TOTAL")
    assert {line.split()[0] for line in text.splitlines()[2:]} == {"glm", "deepseek"}


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs the test Postgres")
def test_sql_joins_model_on_trace_id_and_respects_window():
    import conftest
    import db
    from model_api_runtime.v2 import jobs_store

    uid = "u_wake_open_rate_" + uuid.uuid4().hex[:8]
    conftest.seed_user(uid)
    now = datetime.now(timezone.utc)
    cases = [
        ("glm-5.3-flash", "completed", None, None),
        ("glm-5.3-flash", "completed", "sleep", "no"),
        ("glm-5.3-flash", "completed", "sleep", "empty_visible_reply_suppressed"),
        ("deepseek-v4-flash", "completed", None, None),
        (None, "failed", None, None),
    ]
    trace_ids = []
    try:
        for model, status, wake_result, reason in cases:
            trace_id = uuid.uuid4().hex
            trace_ids.append(trace_id)
            job_id, _ = jobs_store.enqueue_job(uid, "heartbeat", trace_id=trace_id)
            with db.get_pool().connection() as conn:
                conn.execute(
                    "UPDATE agent_jobs SET status=%s, wake_result=%s, wake_result_reason=%s WHERE id=%s",
                    (status, wake_result, reason, job_id),
                )
            if model:
                db.insert_trace_events_strict(uid, [{
                    "ts": time.time(), "subsystem": "agent", "type": "agent.model.call.done",
                    "status": "ok", "trace_id": trace_id, "lane": "heartbeat", "model": model,
                }])
        with db.get_pool().connection() as conn:
            rows = report.fetch(conn, since=(now - timedelta(minutes=5)).isoformat(),
                                until=(now + timedelta(minutes=5)).isoformat())
            outside = report.fetch(conn, since=(now - timedelta(days=2)).isoformat(),
                                   until=(now - timedelta(days=1)).isoformat())
        mine = [r for r in rows if r[3] in {"glm-5.3-flash", "deepseek-v4-flash"} or r[0] == "failed"]
        out = report.summarize(mine)
        assert out["by_family"]["glm"]["spoke"] == 1
        assert out["by_family"]["glm"]["silent"] == 1
        assert out["by_family"]["glm"]["excluded"] == {"empty_reply_suppressed": 1}
        assert out["by_family"]["deepseek"]["spoke"] == 1
        assert out["total"]["excluded"].get("failed", 0) >= 1
        assert not [r for r in outside if r[3] in {"glm-5.3-flash", "deepseek-v4-flash"}]
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
        db.delete_trace_events_for_user(uid)
