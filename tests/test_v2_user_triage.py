from __future__ import annotations

import datetime as dt
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))

from tools import v2_user_triage as triage


def test_generic_responder_error_is_not_counted_as_prompt_coverage_failure(
    monkeypatch,
):
    def fake_fetch(_conn, sql: str, _params=()):
        if "SELECT status, count(*) AS n" in sql:
            return [{"status": "failed", "n": 1}]
        if "last_error IS NOT NULL" in sql:
            return [{"e": "turn_failed:responder_error", "n": 1}]
        if "ORDER BY id DESC" in sql:
            return [
                {
                    "id": 7,
                    "reason": "chat",
                    "status": "failed",
                    "created_at": dt.datetime(2026, 8, 29, 0, 0),
                    "finished_at": dt.datetime(2026, 8, 29, 0, 1),
                    "e": "turn_failed:responder_error",
                }
            ]
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(triage, "_fetch", fake_fetch)

    failed, coverage_failures = triage.section_jobs(object(), "usr_abcd", 8)

    assert failed == 1
    assert coverage_failures == 0


@pytest.mark.parametrize(
    ("error", "expected_count", "expected_annotation"),
    [
        ("turn_failed:prompt_coverage_incomplete", 2, True),
        ("turn_failed:prompt_coverage_incomplete:tail_gap", 2, True),
        ("turn_failed:not_prompt_coverage_incomplete_but_other", 0, False),
    ],
)
def test_jobs_recognizes_only_exact_prompt_coverage_codes(
    monkeypatch,
    capsys,
    error,
    expected_count,
    expected_annotation,
):
    def fake_fetch(_conn, sql: str, _params=()):
        if "SELECT status, count(*) AS n" in sql:
            return [{"status": "failed", "n": 2}]
        if "last_error IS NOT NULL" in sql:
            return [{"e": error, "n": 2}]
        if "ORDER BY id DESC" in sql:
            return []
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(triage, "_fetch", fake_fetch)

    _failed, coverage_failures = triage.section_jobs(object(), "usr_abcd", 8)

    output = capsys.readouterr().out
    assert coverage_failures == expected_count
    assert ("prompt coverage incomplete" in output) is expected_annotation


def test_summary_reports_exact_coverage_signal_without_compaction_diagnosis(
    monkeypatch,
    capsys,
):
    def fake_one(_conn, sql: str, _params=()):
        if "FROM v2_conversation_summary" in sql:
            return {
                "watermark_seq": 100,
                "updated_at": dt.datetime(2026, 8, 28, 0, 0),
                "version": 4,
                "stalled_h": 12,
            }
        if "FROM chat_messages" in sql:
            return {"total": 130, "backlog": 30}
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(triage, "_one", fake_one)
    monkeypatch.setattr(triage, "_fetch", lambda *_args, **_kwargs: [])

    triage.section_summary(
        object(), "usr_abcd", failed_jobs=1, coverage_failures=1
    )

    output = capsys.readouterr().out
    assert "1 exact prompt coverage failure" in output
    assert "compaction is wedged" not in output


def test_trajectory_reports_equal_sizes_without_inferring_same_batch(
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(triage, "_one", lambda *_args, **_kwargs: {"id": 9})
    monkeypatch.setattr(
        triage,
        "_fetch",
        lambda *_args, **_kwargs: [
            {
                "event_index": 1,
                "event_kind": "provider_request",
                "payload_bytes": 1024,
                "created_at": dt.datetime(2026, 8, 29, 0, 0),
            },
            {
                "event_index": 2,
                "event_kind": "provider_request",
                "payload_bytes": 1024,
                "created_at": dt.datetime(2026, 8, 29, 0, 1),
            },
        ],
    )

    triage.section_trajectory(object(), "usr_abcd")

    output = capsys.readouterr().out
    assert "same recorded size" in output
    assert "same batch" not in output
    assert "self-locking" not in output


def test_removed_backlog_option_is_rejected_before_database_access(monkeypatch):
    def unexpected_dsn(_env: str) -> str:
        pytest.fail("removed --backlog option reached database setup")

    monkeypatch.setattr(triage, "_dsn", unexpected_dsn)

    with pytest.raises(SystemExit) as exc_info:
        triage.main(
            [
                "--env",
                "test",
                "--user-id",
                "usr_abcd",
                "--backlog",
                "4",
            ]
        )

    assert exc_info.value.code == 2
