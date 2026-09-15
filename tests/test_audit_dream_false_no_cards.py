"""Read-only audit of users stuck by the 09-10 / 09-13 false "no cards" Dreams.

The database test seeds the incident through the production path — a real
``/v1/dream/tick`` enqueue and a real ``/v1/proactive/jobs/<id>/status`` report
from an old consumer — with only the new backend reclassification switched off
(that is exactly the pre-fix backend that produced the stuck ledgers), then runs
the tool's read against that database.
"""
import importlib.util
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import psycopg  # noqa: E402
import pytest  # noqa: E402

import db  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from conftest import seed_user  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from proactive import dream_ledger_audit as audit  # noqa: E402
from proactive import dream_scheduler  # noqa: E402

MODULE_PATH = Path(__file__).parent.parent / "tools" / "audit_dream_false_no_cards.py"
SPEC = importlib.util.spec_from_file_location("audit_dream_false_no_cards", MODULE_PATH)
cli = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(cli)

WINDOW = audit.parse_window("2026-09-10T18:00:00Z/2026-09-10T20:00:00Z")


def _job(job_id, completed, *, reason="dream_no_cards_available", cards_read=None,
         card_count=5, signature="sig_incident", status="completed", **extra):
    result = {"status": "noop", "reason": reason, "job_kind": "memory_dream"}
    if cards_read is not None:
        result["cards_read"] = cards_read
    return {
        "job_id": job_id,
        "job_kind": "memory_dream",
        "status": status,
        "status_reason": reason,
        "noop_reason": reason,
        "completed_at": completed,
        "dream_result": result,
        "dream_stats": {"card_count": card_count, "seed_card_count": card_count,
                        "turn_count": 7, "signature": signature},
        "dream_until": {"signature": signature, "last_until": "u"},
        **extra,
    }


def _ledger_for(job):
    return {**audit.ledger_after(job), "pending_dream_key": "", "dream_fail_streak": 0}


INCIDENT = _job("dream_incident", "2026-09-10T18:30:00.123456")
PREVIOUS = _job("dream_previous", "2026-09-01T03:00:00", reason="dream_memory_actions_applied",
                card_count=3, signature="sig_previous", organized_count=2)


def _select(jobs, ledger, **kwargs):
    return audit.select_user("usr_x", jobs, ledger, windows=[WINDOW], **kwargs)


def test_incident_completion_pointed_at_by_the_ledger_is_a_candidate():
    verdict, candidate = _select([PREVIOUS, INCIDENT], _ledger_for(INCIDENT))

    assert verdict == "candidate"
    assert candidate["job_id"] == "dream_incident"
    assert candidate["enqueue_card_count"] == 5
    assert candidate["restore_from_job_id"] == "dream_previous"
    assert candidate["restore_ledger"]["last_dream_signature"] == "sig_previous"
    assert candidate["restore_ledger"]["last_dreamed_seed_card_count"] == 3
    assert candidate["expected_ledger"]["last_dream_signature"] == "sig_incident"


def test_user_who_never_dreamed_before_restores_to_the_zero_ledger():
    verdict, candidate = _select([INCIDENT], _ledger_for(INCIDENT))

    assert verdict == "candidate"
    assert candidate["restore_from_job_id"] == ""
    assert candidate["restore_ledger"] == audit.ledger_after(None)


@pytest.mark.parametrize(
    "jobs, ledger, v2_last, verdict",
    [
        pytest.param(
            [_job("dream_incident", "2026-09-10T21:00:00")],
            None, None, "no_incident_completion", id="outside-window",
        ),
        pytest.param(
            [_job("dream_incident", "2026-09-10T18:30:00", cards_read="empty")],
            None, None, "no_incident_completion", id="verified-empty-marker",
        ),
        pytest.param(
            [_job("dream_incident", "2026-09-10T18:30:00", card_count=0)],
            None, None, "no_incident_completion", id="enqueued-with-no-cards",
        ),
        pytest.param(
            [_job("dream_incident", "2026-09-10T18:30:00", status="failed")],
            None, None, "no_incident_completion", id="not-completed",
        ),
        pytest.param(
            [INCIDENT, _job("dream_later", "2026-09-11T03:00:00",
                            reason="dream_nothing_to_consolidate", signature="sig_later")],
            _ledger_for(INCIDENT), None, "later_verified_dream", id="later-resident-dream",
        ),
        pytest.param(
            [INCIDENT], _ledger_for(INCIDENT),
            datetime(2026, 9, 12, 3, tzinfo=timezone.utc),
            "later_verified_dream", id="later-v2-dream",
        ),
        pytest.param([INCIDENT], None, None, "ledger_missing", id="no-ledger"),
        pytest.param(
            [INCIDENT], {**_ledger_for(INCIDENT), "last_dream_signature": "sig_other"},
            None, "ledger_moved", id="ledger-signature-differs",
        ),
        pytest.param(
            [INCIDENT],
            {**_ledger_for(INCIDENT),
             "last_dream_completed_at": _ledger_for(INCIDENT)["last_dream_completed_at"] + 3600},
            None, "ledger_moved", id="ledger-stamped-at-another-time",
        ),
    ],
)
def test_non_candidates_are_excluded_with_a_reason(jobs, ledger, v2_last, verdict):
    assert _select(jobs, ledger, v2_last_completed=v2_last) == (verdict, None)


def test_a_later_night_that_failed_the_same_way_is_the_completion_to_match():
    later = _job("dream_again", "2026-09-12T03:00:00")
    verdict, candidate = _select([PREVIOUS, INCIDENT, later], _ledger_for(later))

    assert verdict == "candidate"
    assert candidate["job_id"] == "dream_again"
    assert candidate["incident_completions_in_window"] == 1
    assert candidate["restore_from_job_id"] == "dream_previous"


def test_report_counts_verdicts_and_carries_no_card_content():
    report = audit.build_report(
        {"usr_a": [INCIDENT], "usr_b": [PREVIOUS]},
        {"usr_a": _ledger_for(INCIDENT)},
        windows=[WINDOW],
    )

    assert report["verdicts"] == {"candidate": 1, "no_incident_completion": 1}
    assert [row["user_id"] for row in report["candidates"]] == ["usr_a"]
    assert set(report["candidates"][0]) == {
        "user_id", "job_id", "completed_at", "enqueue_card_count",
        "incident_completions_in_window", "expected_ledger", "ledger_fingerprint",
        "restore_from_job_id", "restore_ledger", "rewound_job_ids",
        "unreclassified_job_ids",
    }
    assert report["already_repaired"] == []


def test_a_ledger_already_rewound_to_the_restore_target_is_already_repaired():
    verdict, row = _select([PREVIOUS, INCIDENT], _ledger_for(PREVIOUS))

    assert verdict == "already_repaired"
    assert row["job_id"] == "dream_incident"
    assert row["ledger_fingerprint"] == audit.ledger_fingerprint(row["restore_ledger"])
    report = audit.build_report(
        {"usr_x": [PREVIOUS, INCIDENT]}, {"usr_x": _ledger_for(PREVIOUS)}, windows=[WINDOW],
    )
    assert report["already_repaired"] == [{
        "user_id": "usr_x", "job_id": "dream_incident",
        "unreclassified_job_ids": ["dream_incident"],
    }]
    assert report["candidates"] == []


def test_a_job_the_repair_reclassified_still_reads_as_the_false_completion():
    repaired = {
        **INCIDENT,
        "status": "failed",
        "status_reason": "dream_context_unavailable",
        "noop_reason": "dream_context_unavailable",
        "dream_result": {**INCIDENT["dream_result"], "status": "failed",
                         "reason": "dream_context_unavailable"},
        audit.REPAIR_MARKER_KEY: {"original_status": "completed",
                                  "original_completed_at": INCIDENT["completed_at"]},
    }
    assert _select([PREVIOUS, INCIDENT], _ledger_for(INCIDENT))[0] == "candidate"
    verdict, row = _select([PREVIOUS, repaired], _ledger_for(PREVIOUS))
    assert verdict == "already_repaired"
    assert row["rewound_job_ids"] == ["dream_incident"]
    assert row["unreclassified_job_ids"] == []
    verdict, row = _select([PREVIOUS, INCIDENT], _ledger_for(PREVIOUS))
    assert row["unreclassified_job_ids"] == ["dream_incident"]
    # A job the fixed backend failed on its own (no marker) is not an incident.
    unmarked = {key: value for key, value in repaired.items() if key != audit.REPAIR_MARKER_KEY}
    assert _select([PREVIOUS, unmarked], _ledger_for(PREVIOUS)) == ("no_incident_completion", None)


def test_ledger_fingerprint_covers_only_ledger_fields_and_tells_missing_from_zero():
    ledger = _ledger_for(INCIDENT)
    fingerprint = audit.ledger_fingerprint(ledger)
    # Non-ledger siblings (pending/backoff/trace) do not move the fingerprint.
    assert audit.ledger_fingerprint({**ledger, "pending_dream_key": "k",
                                     "last_dream_trace_at": 9.0}) == fingerprint
    for key in audit.LEDGER_FIELDS:
        changed = {**ledger, key: ledger[key] + 1 if isinstance(ledger[key], (int, float))
                   else ledger[key] + "x"}
        assert audit.ledger_fingerprint(changed) != fingerprint, key
    zero = audit.ledger_after(None)
    assert audit.ledger_fingerprint({}) != audit.ledger_fingerprint(zero)
    # jsonb may hand an integral float back for an int; that is the same ledger.
    assert audit.ledger_fingerprint({**zero, "last_dreamed_card_count": 0.0}) == \
        audit.ledger_fingerprint(zero)


def test_window_dates_cover_every_utc_day_a_window_touches():
    """A 3-day window must load its middle day (Codex r2 I3)."""
    window = audit.parse_window("2026-09-10T18:00:00Z/2026-09-12T02:00:00Z")
    assert audit.window_utc_dates([window]) == ["2026-09-10", "2026-09-11", "2026-09-12"]
    # Non-UTC offsets are bucketed by UTC day, overlapping windows deduplicate.
    shifted = audit.parse_window("2026-09-11T07:00:00+08:00/2026-09-11T09:00:00+08:00")
    assert audit.window_utc_dates([window, shifted]) == [
        "2026-09-10", "2026-09-11", "2026-09-12",
    ]


def test_window_argument_must_be_an_ordered_pair():
    import argparse

    with pytest.raises(audit.InvalidWindow):
        audit.parse_window("2026-09-10T20:00:00Z/2026-09-10T18:00:00Z")
    with pytest.raises(argparse.ArgumentTypeError):
        cli.parse_window("2026-09-10T20:00:00Z/2026-09-10T18:00:00Z")
    with pytest.raises(argparse.ArgumentTypeError):
        cli.parse_window("2026-09-10T18:00:00Z")
    with pytest.raises(SystemExit):
        cli.main(["--env", "test", "--window", "2026-09-10T18:00:00Z"],
                 connect=lambda: pytest.fail("must not connect"))


def _memory(user_id, memory_id):
    ts = "2026-06-20T00:00:00Z"
    return {
        "v": 1, "id": memory_id, "type": "fact", "owner_user_id": user_id,
        "visibility": "shared", "body_ct": f"ct_{memory_id}", "nonce": f"n_{memory_id}",
        "K_user": f"ku_{memory_id}", "K_enclave": f"ke_{memory_id}",
        "occurred_at": ts, "created_at": ts, "updated_at": ts, "status": "active",
        "importance": 0.6, "pulse": 0.3,
    }


def _stuck_user_via_pre_fix_backend(monkeypatch, tmp_path, user_id, *, payload):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setenv("FEEDLING_DREAM_NIGHT_ONLY", "false")
    monkeypatch.setenv("FEEDLING_DREAM_MIN_NEW_CARDS", "1")
    monkeypatch.setenv("FEEDLING_DREAM_MIN_INTERVAL_SEC", "0")
    core_store._stores.clear()
    api_key = f"test_key_{user_id}"
    registry._key_to_user[registry._hash_api_key(api_key)] = user_id
    seed_user(user_id)
    db.memory_replace_all(user_id, [_memory(user_id, f"mem_{i}") for i in range(4)])
    client = make_client()
    headers = {"X-API-Key": api_key}
    # Real clock: the tool bounds its scan on the job's enqueue time (``ts``).
    now = time.time()
    job = client.post("/v1/dream/tick", headers=headers, json={"now": now}).get_json()["job"]
    # The backend before this fix recorded whatever the consumer reported.
    with monkeypatch.context() as pre_fix:
        pre_fix.setattr(
            dream_scheduler, "reclassify_unverified_no_cards_completion",
            lambda _store, _job, patch: patch,
        )
        done = client.post(
            f"/v1/proactive/jobs/{job['job_id']}/status", headers=headers, json=payload,
        )
    assert done.status_code == 200
    stuck = client.post("/v1/dream/tick", headers=headers, json={"now": now + 100.0}).get_json()
    return job, stuck


def _legacy_payload(**dream_result_extra):
    return {
        "status": "completed",
        "reason": "dream_no_cards_available",
        "dream_result": {"status": "noop", "reason": "dream_no_cards_available",
                         "job_kind": "memory_dream", **dream_result_extra},
        "cards_merged": 0, "cards_superseded": 0, "questions": [],
        "noop_reason": "dream_no_cards_available",
    }


def _run_tool_read(user_ids, *, window=None, **kwargs):
    now = datetime.now(timezone.utc)
    window = window or (now - timedelta(hours=1), now + timedelta(hours=1))
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        with conn.transaction():
            report = audit.collect(conn, windows=[window], user_ids=user_ids, **kwargs)
            timeout = conn.execute("SHOW statement_timeout").fetchone()[0]
            # Savepoint, so the refused write does not abort (and roll back)
            # the outer read transaction: it commits normally below.
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                with conn.transaction():
                    conn.execute(
                        "DELETE FROM user_blobs WHERE user_id = %s", (user_ids[0],)
                    )
        # Transaction-local: the session default is back after a committed read.
        assert conn.execute("SHOW statement_timeout").fetchone()[0] != timeout
    report["_statement_timeout"] = timeout
    return report


def _run_cli(user_ids):
    import contextlib
    import io
    import json

    now = datetime.now(timezone.utc)
    window = f"{(now - timedelta(hours=1)).isoformat()}/{(now + timedelta(hours=1)).isoformat()}"
    argv = ["--env", "test", "--window", window]
    for user_id in user_ids:
        argv += ["--user-id", user_id]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert cli.main(argv, connect=lambda: psycopg.connect(os.environ["DATABASE_URL"])) == 0
    return json.loads(out.getvalue())


@pytest.fixture
def utc_server_clock():
    """``completed_at`` is written with the naive server clock; CVMs run in UTC."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


def test_tool_finds_a_user_stuck_by_the_real_pre_fix_flow(
    tmp_path, monkeypatch, utc_server_clock,
):
    stuck_user, verified_user = "usr_audit_stuck_0915", "usr_audit_verified_0915"
    job, stuck = _stuck_user_via_pre_fix_backend(
        monkeypatch, tmp_path, stuck_user, payload=_legacy_payload()
    )
    assert stuck["reason"] == "already_dreamed"  # the incident, reproduced
    _stuck_user_via_pre_fix_backend(
        monkeypatch, tmp_path, verified_user, payload=_legacy_payload(cards_read="empty")
    )

    report = _run_tool_read([stuck_user, verified_user])
    cli_report = _run_cli([stuck_user, verified_user])
    assert cli_report["candidates"] == report["candidates"]
    assert cli_report["environment"] == "test"

    assert report["verdicts"] == {"candidate": 1, "no_incident_completion": 1}
    [candidate] = report["candidates"]
    assert candidate["user_id"] == stuck_user
    assert candidate["job_id"] == job["job_id"]
    assert candidate["enqueue_card_count"] == 4
    assert candidate["restore_ledger"] == audit.ledger_after(None)
    ledger = dream_scheduler.load_dream_state(core_store.get_store(stuck_user))
    assert candidate["expected_ledger"]["last_dream_signature"] == ledger["last_dream_signature"]
    # Read-only: the tool left the stuck ledger exactly as it found it.
    assert dream_scheduler.load_dream_state(core_store.get_store(stuck_user)) == ledger


def test_tool_finds_a_completion_on_the_middle_day_of_a_multi_day_window(
    tmp_path, monkeypatch, utc_server_clock,
):
    """The incident completion is today; the window runs from before yesterday
    to after tomorrow, so today is neither its start nor its end UTC date."""
    user = "usr_audit_middle_day_0915"
    job, stuck = _stuck_user_via_pre_fix_backend(
        monkeypatch, tmp_path, user, payload=_legacy_payload()
    )
    assert stuck["reason"] == "already_dreamed"
    now = datetime.now(timezone.utc)
    window = (now - timedelta(hours=25), now + timedelta(hours=25))
    assert now.date().isoformat() not in {
        window[0].date().isoformat(), window[1].date().isoformat()
    }

    report = _run_tool_read([user], window=window, statement_timeout_sec=7)

    assert report["verdicts"] == {"candidate": 1}
    assert report["candidates"][0]["job_id"] == job["job_id"]
    assert now.date().isoformat() in report["prefilter"]["completed_on_utc_dates"]
    assert report["_statement_timeout"] == "7s"


def test_tool_scan_is_bounded_on_the_job_enqueue_time(
    tmp_path, monkeypatch, utc_server_clock,
):
    """Jobs enqueued more than ``max_job_age_days`` before the earliest window
    start are not scanned (index-friendly bound on ``user_logs.ts``, partial
    index ``ix_user_logs_proactive_jobs_ts``), and the bound is reported."""
    user = "usr_audit_old_enqueue_0915"
    _stuck_user_via_pre_fix_backend(monkeypatch, tmp_path, user, payload=_legacy_payload())
    # Test setup only: pretend the job sat pending for 10 days before the
    # completion that lands in today's window.
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute(
            "UPDATE user_logs SET ts = ts - %s WHERE user_id = %s AND stream = 'proactive_jobs'",
            (10 * 86400, user),
        )

    bounded = _run_tool_read([user], max_job_age_days=1)
    assert bounded["users_scanned"] == 0
    assert bounded["prefilter"]["enqueued_after"] < bounded["windows"][0][0]

    widened = _run_tool_read([user], max_job_age_days=11)
    assert widened["verdicts"] == {"candidate": 1}
    assert widened["_statement_timeout"] == "1min"  # the default
