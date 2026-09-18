"""Memory-lane aggregation moved from the daily-report tests with its backend code.

Pure unit tests: no database, no network. Every DB/HTTP call goes through an
injected fake, so nothing here can reach the admin API or post a real
Lark message.

The fixture ``tests/fixtures/memory_pipeline_daily_report/sources_2026-09-14.json``
uses the exact row projection of ``db.admin_lane_rollup``;
``tests/test_lane_rollup.py`` locks those key sets (and the outcome columns'
meaning) against the real DB-backed producer so the fixture cannot silently
drift from the endpoint.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

from admin import lane_rollup_summary as summary
from notices import error_contract
from tools import memory_pipeline_daily_report as report_tool

FIXTURE = ROOT / "tests/fixtures/memory_pipeline_daily_report/sources_2026-09-14.json"
DAY = "2026-09-14"
PREV = "2026-09-13"


def _sources() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _row(uid, *, day=DAY, route="model_api", lane="capture", completed=0,
         failed=0, expired=0, codes=None, frozen=True, operational=None,
         control=0, user=0, silent_declared=0):
    # V1 cells carry the freezer's outcome split; by default everything failed
    # is operational (V2 cells always store 0 there, like the real freezer).
    if route == "resident" and operational is None:
        operational = failed - control - user
    return {"user_id": uid, "day": day, "route": route, "lane": lane,
            "enqueue_source": "", "access_path": "apikey_v2",
            "mode_source": "explicit", "completed": completed, "failed": failed,
            "expired": expired, "superseded": 0, "failure_codes": codes or {},
            "frozen": frozen,
            "operational_failures": (operational or 0) if frozen else None,
            "control_outcomes": control if frozen else None,
            "user_unavailable": user if frozen else None,
            "spoke": 0, "spoke_completed": 0,
            "silent_declared": silent_declared, "silent_undeclared": 0}


def _payload(rows, *, through=DAY, stuck_total=0, today_partial=None,
             outcomes_from="2026-08-25", stuck_rows=None):
    cov = {"backfill_from": "2026-08-01", "through_day": through,
           "partial_before": "2026-08-01", "voice_from": None,
           "outcomes_from": outcomes_from, "access_path_from": None}
    return {"rows": rows, "today_partial": today_partial or [],
            "stuck": {"rows": stuck_rows or [], "total": stuck_total,
                      "stuck_after_hours": 6.0, "resident_recent_hours": 24.0,
                      "note": ""},
            "coverage": {"resident": dict(cov), "model_api": dict(cov)},
            "pagination": {"limit": 500, "offset": 0, "returned": len(rows),
                           "total": len(rows)},
            "filters": {}}


def _report(capture_rows=(), dream_rows=(), **payload_kwargs):
    sources = {"lane_rollup": {"capture": _payload(list(capture_rows), **payload_kwargs),
                               "dream": _payload(list(dream_rows))}}
    return summary.build_summary(sources, day=DAY)


def test_cause_breakdown_always_sums_to_operational_failures():
    report = summary.build_summary(_sources(), day=DAY)
    for stats in (stats for cell in report["cells"].values() for stats in cell.values()):
        assert sum(c["attempts"] for c in stats['causes'].values()) == stats['operational']
        assert stats['operational'] + stats['control'] + stats['user_unavailable'] == stats['failed_raw']


# --------------------------------------------------------------------------- #
# Failure-code grouping
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("code,group", [
    # user's own account / configuration
    ("auth_invalid", "user_account"),
    ("extraction_failed:quota_insufficient", "user_account"),
    ("capture_agent_call_failed:model_not_found", "user_account"),
    ("dream_agent_call_failed:provider_account_expired", "user_account"),
    # a local agent timeout is registry-blamed on the system, like chat
    ("capture_agent_call_failed:turn_timeout", "our_side"),
    ("capture_agent_call_failed:resident_agent_cli_logged_out", "user_account"),
    ("provider_setup:model_api_not_tested", "user_account"),
    ("provider_setup:model_api_not_configured", "user_account"),
    # model service
    ("extraction_failed:upstream_unavailable", "model_service"),
    ("extraction_failed:rate_limited", "model_service"),
    ("provider_timeout", "model_service"),
    ("extraction_failed:output_truncated", "model_service"),
    # our side
    ("lease_timeout", "our_side"),
    ("slot_watchdog_timeout", "our_side"),
    ("watchdog_requeue_exhausted", "our_side"),
    ("extraction_failed:database_pool_timeout", "our_side"),
    ("extraction_failed:json_decode_error", "our_side"),
    ("extraction_failed:no_json_object", "our_side"),
    ("capture_memory_actions_failed", "our_side"),
    ("dream_context_unavailable", "our_side"),
    ("queue_timeout", "our_side"),
    ("provider_setup:model_api_key_decrypt_failed", "our_side"),
    ("turns_halted", "our_side"),
    # unknown
    ("runtime_failed", "unknown"),
    ("capture_agent_call_failed:unknown", "unknown"),
    # a bare prefix names no cause — not "our side" via the capture_ prefix
    ("capture_agent_call_failed", "unknown"),
    ("dream_agent_call_failed", "unknown"),
    ("migrate_agent_call_failed", "unknown"),
    ("no_code", "unknown"),
    ("", "unknown"),
    ("something_brand_new", "unknown"),
])
def test_failure_codes_group_by_who_has_to_act(code, group):
    assert summary.classify_failure_code(code) == group


def test_registered_codes_follow_the_registry_blame():
    blame_to_group = {"user_provider": "user_account",
                      "user_environment": "user_account",
                      "provider_transient": "model_service",
                      "system": "our_side"}
    unknown = {"unknown", error_contract.UNREGISTERED_ERROR_CLASS}
    checked = 0
    for spec in error_contract.all_specs():
        expected = "unknown" if spec.code in unknown else blame_to_group[spec.blame]
        # A registered code must not be captured by a memory-lane prefix first
        # unless that prefix is the explicit our-side lease/watchdog list.
        assert summary.classify_failure_code(spec.code) == expected, spec.code
        checked += 1
    assert checked > 30


def test_user_with_one_success_is_not_counted_as_stuck():
    rows = [_row("usr_a", failed=5, codes={"lease_timeout": 5}),
            _row("usr_b", failed=5, completed=1, codes={"lease_timeout": 5}),
            _row("usr_c", completed=3)]
    stats = summary.aggregate_day(rows, lane="capture", route="model_api", day=DAY)
    assert stats.stuck_users == 1
    assert len(stats.users) == 3


def test_affected_users_count_each_failing_user_once_regardless_of_retries():
    # T646: one broken user retried 4x in a night is 4 operational failures
    # but one affected user; a user who failed then succeeded is affected
    # but not stuck; a clean user is neither. The per-attempt rate moves with
    # retry policy, the two user counts do not.
    rows = [_row("usr_a", lane="dream", failed=4, codes={"lease_timeout": 4}),
            _row("usr_b", lane="dream", failed=1, completed=2, codes={"lease_timeout": 1}),
            _row("usr_c", lane="dream", completed=3)]
    stats = summary.aggregate_day(rows, lane="dream", route="model_api", day=DAY)
    assert stats.operational == 5
    assert (stats.failed_users, stats.stuck_users, len(stats.users)) == (2, 1, 3)
    serialized = summary.serialize_stats(stats)
    assert (serialized["failed_users"], serialized["stuck_users"]) == (2, 1)


def test_affected_users_ignore_control_and_user_unavailable_outcomes():
    # A skipped/disabled job or the user's own dead key is not our failure,
    # so it must not make the user "affected" either.
    rows = [_row("usr_ctl", lane="dream", failed=2, codes={"dream_disabled": 2}),
            _row("usr_key", lane="dream", failed=3, codes={"extraction_failed:auth_invalid": 3})]
    stats = summary.aggregate_day(rows, lane="dream", route="model_api", day=DAY)
    assert (stats.operational, stats.failed_users, stats.stuck_users) == (0, 0, 0)


def test_split_rows_of_one_user_are_summed_before_stuck_check():
    # heartbeat-style enqueue_source splits produce several rows per user.
    rows = [_row("usr_a", failed=2, codes={"lease_timeout": 2}),
            dict(_row("usr_a", completed=1), enqueue_source="clock")]
    stats = summary.aggregate_day(rows, lane="capture", route="model_api", day=DAY)
    assert stats.stuck_users == 0


def test_unfrozen_day_is_flagged_not_reported_as_healthy():
    live = [_row("usr_a", completed=1, failed=1, frozen=False)]
    report = _report(through=PREV, today_partial=live)
    text = report_tool.render_message(report)
    assert text.startswith("[需要关注]")
    assert "当天统计还没冻结" in text
    stats = report['cells']['capture/model_api']['day']
    assert stats['operational'] == 1 and stats['causes']['unknown']['codes'] == {"no_code": 1}


def test_lagging_freezer_is_flagged_even_without_live_rows():
    # A stalled freezer with no live tail must not read as "当天没有任务".
    report = _report(through=PREV)
    assert any("数据不完整" in r for r in report_tool.evaluate_attention(report))
    assert "落卡 capture · V2 model_api 当天统计还没冻结" in report_tool.render_message(report)


def test_missing_lane_payload_is_flagged():
    sources = _sources()
    sources["lane_rollup"].pop("dream")
    text = report_tool.render_message(summary.build_summary(sources, day=DAY))
    assert text.startswith("[需要关注]")
    assert "做梦 dream 没取到数据" in text


def test_dream_skips_come_from_silent_declared_and_are_not_successes():
    rows = [_row("usr_a", lane="dream", completed=5, silent_declared=5),
            _row("usr_b", lane="dream", completed=3, silent_declared=1, failed=1,
                 codes={"extraction_failed:output_truncated": 1}),
            _row("usr_c", lane="dream", completed=0, failed=1,
                 codes={"extraction_failed:upstream_unavailable": 1}),
            _row("usr_d", lane="dream", completed=4, silent_declared=4, failed=2,
                 codes={"extraction_failed:upstream_unavailable": 2})]
    stats = summary.aggregate_day(rows, lane="dream", route="model_api", day=DAY)
    assert (stats.completed, stats.skipped, stats.operational) == (2, 10, 4)
    # A user whose only completions were skips never succeeded that day.
    assert stats.stuck_users == 2
    # Capture has no declared skips: silent_declared there is not subtracted.
    capture = summary.aggregate_day(
        [_row("usr_a", completed=5, silent_declared=5)],
        lane="capture", route="model_api", day=DAY)
    assert (capture.completed, capture.skipped) == (5, 0)


def test_v1_control_and_user_unavailable_do_not_page():
    """Review 09-15: V1 skipped jobs and users' own account failures inflated the
    failure rate, "stuck users" and "我们这边" every day."""
    rows = []
    for i in range(12):  # skipped by the scheduler, reason recorded as a code
        rows.append(_row(f"usr_skip{i:012x}", route="resident", completed=1, failed=4,
                         codes={"capture_window_unavailable": 4}, control=4))
    for i in range(12):  # the user's own empty balance
        rows.append(_row(f"usr_bal{i:013x}", route="resident", failed=3,
                         codes={"capture_agent_call_failed:quota_insufficient": 3}, user=3))
    report = _report(rows)
    stats = report['cells']['capture/resident']['day']
    assert (stats['operational'], stats['control'], stats['user_unavailable']) == (0, 48, 36)
    assert stats['stuck_users'] == 0
    assert stats['failure_rate'] == 0
    assert report_tool.evaluate_attention(report) == []
    text = report_tool.render_message(report)
    assert text.startswith("[正常]")
    assert ("不算失败：确认是用户自己账号问题 36 次/12 人（capture_agent_call_failed:quota_insufficient 36）；"
            "跳过/关闭等控制结果 48 次") in text


def test_v1_codes_shared_with_skips_are_not_guessed_into_a_group():
    # One cell: 2 skips + 3 real failures, all recorded under capture_* reasons.
    rows = [_row("usr_mix", route="resident", completed=1, failed=5, control=2,
                 codes={"capture_window_unavailable": 2, "capture_memory_write_failed": 3})]
    stats = summary.aggregate_day(rows, lane="capture", route="resident", day=DAY)
    assert stats.operational == 3
    assert stats.causes["unknown"].codes == {"unattributed": 3}
    assert not stats.causes["our_side"].users
    # Fewer codes than failures, but a skip shares them: still not guessed.
    rows = [_row("usr_mix2", route="resident", completed=1, failed=5, control=2,
                 codes={"capture_window_unavailable": 2})]
    stats = summary.aggregate_day(rows, lane="capture", route="resident", day=DAY)
    assert stats.causes["unknown"].codes == {"unattributed": 3}
    assert not stats.causes["our_side"].users
    # Without control outcomes the leftover codes are exactly the failures.
    rows = [_row("usr_ours", route="resident", completed=1, failed=4,
                 codes={"capture_memory_write_failed": 3})]
    stats = summary.aggregate_day(rows, lane="capture", route="resident", day=DAY)
    assert stats.causes["our_side"].codes == {"capture_memory_write_failed": 3}
    assert stats.causes["unknown"].codes == {"no_code": 1}


def test_v2_control_and_user_unavailable_codes_follow_the_v2_classifier():
    rows = [_row(f"usr_{i:016x}", completed=0, failed=3,
                 codes={"capture_disabled": 1, "turns_halted": 1,
                        "provider_setup:model_api_not_configured": 1})
            for i in range(12)]
    report = _report(rows)
    stats = report['cells']['capture/model_api']['day']
    assert (stats['operational'], stats['control'], stats['user_unavailable']) == (0, 24, 12)
    assert stats['stuck_users'] == 0 and report_tool.evaluate_attention(report) == []


def test_unmeasured_or_unbalanced_v1_outcomes_count_raw_and_say_so():
    rows = [_row(f"usr_{i:016x}", route="resident", failed=2,
                 codes={"capture_window_unavailable": 2}, control=2) for i in range(3)]
    measured = _report(rows)
    assert measured['cells']['capture/resident']['day']['operational'] == 0
    unmeasured = _report(rows, outcomes_from=None)
    stats = unmeasured['cells']['capture/resident']['day']
    assert stats['operational'] == 6 and stats['unclassified']
    assert any("失败分类缺失" in note for note in report_tool.incomplete_notes(unmeasured))
    assert report_tool.evaluate_attention(unmeasured)
    broken = [_row("usr_x", route="resident", failed=5, operational=1, control=1)]
    stats = _report(broken)['cells']['capture/resident']['day']
    assert stats['operational'] == 5 and stats['unclassified']


def test_live_stuck_counts_only_recent_v1_jobs():
    stuck_rows = [
        {"route": "resident", "lane": "capture", "count": 40, "recent_count": 2},
        {"route": "model_api", "lane": "capture", "count": 19},
    ]
    report = _report(stuck_total=59, stuck_rows=stuck_rows)
    assert report['live_stuck']["capture"] == 21
    assert any("此刻有 21 个任务" in r for r in report_tool.evaluate_attention(report))
    # An older backend without recent_count keeps the full count.
    legacy = [{"route": "resident", "lane": "capture", "count": 40}]
    assert _report(stuck_total=40, stuck_rows=legacy)['live_stuck']["capture"] == 40


def test_golden_message_matches_old_tool_exactly():
    actual = summary.build_summary(_sources(), day=DAY)
    expected = FIXTURE.with_name("expected_message_2026-09-14.txt").read_text()
    assert report_tool.render_message(actual) == expected
    assert actual == json.loads(FIXTURE.with_name("summary_2026-09-14.json").read_text())


class _PagedRollup:
    def __init__(self, sources):
        self.sources, self.calls = sources, []

    def __call__(self, **query):
        self.calls.append(query)
        full = self.sources['lane_rollup'][query['lane']]
        start, size = query['offset'], query['limit']
        rows = full['rows'][start:start + size]
        return dict(copy.deepcopy(full), rows=rows, pagination={
            'returned': len(rows), 'total': len(full['rows'])})


def test_internal_pagination_and_truncation(monkeypatch):
    fake = _PagedRollup(_sources())
    monkeypatch.setattr(summary.db, 'admin_lane_rollup', fake)
    monkeypatch.setattr(summary, 'LANE_ROLLUP_PAGE_LIMIT', 7)
    assert summary.read_summary(day=DAY) == summary.build_summary(_sources(), day=DAY)
    assert len(fake.calls) > 2
    assert all(q['since_day'] == PREV and q['until_day'] == DAY for q in fake.calls)
    monkeypatch.setattr(summary, 'LANE_ROLLUP_MAX_PAGES', 1)
    actual = summary.read_summary(day=DAY)
    assert {'lane': 'capture', 'kind': 'truncated'} in actual['incomplete']
    assert '行数超过翻页上限，只统计了一部分' in report_tool.render_message(actual)


def test_pagination_deduplicates_full_cell_key(monkeypatch):
    """An older backend orders pages without ``route``: a cell can slide across
    a page boundary and come back on the next page."""
    rows = _sources()['lane_rollup']['capture']['rows'][:4]
    pages = [[rows[0], rows[1]], [rows[1], rows[2]], [rows[3]]]
    calls = []
    def read(**query):
        page = pages[len(calls)]
        calls.append(query['offset'])
        return {'rows': page, 'pagination': {
            'returned': len(page), 'total': sum(map(len, pages))}}
    monkeypatch.setattr(summary.db, 'admin_lane_rollup', read)
    monkeypatch.setattr(summary, 'LANE_ROLLUP_PAGE_LIMIT', 2)
    assert summary.fetch_lane_rollup(lane='capture', since_day=PREV, until_day=DAY)['rows'] == rows
    assert calls == [0, 2, 4]
    # Same user/day/lane on the other route is a different cell, not a duplicate.
    other = dict(rows[0], route='model_api')
    pages[:] = [[rows[0], other]]
    calls.clear()
    assert summary.fetch_lane_rollup(lane='capture', since_day=PREV, until_day=DAY)['rows'] == [rows[0], other]


@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from admin import routes_asgi
    from asgi import middleware
    app = FastAPI()
    middleware.register_exception_handlers(app)
    routes_asgi.register_asgi(app)
    monkeypatch.setenv('FEEDLING_ADMIN_TOKEN', 'summary-admin')
    with TestClient(app) as client:
        yield client


def test_endpoint_summary_is_content_free_and_uses_previous_beijing_day(client, monkeypatch):
    from datetime import datetime, timezone
    assert summary.default_day(datetime(2026, 9, 14, 17, tzinfo=timezone.utc)) == DAY
    sources = _sources()
    for payload in sources['lane_rollup'].values():
        payload['stuck']['rows'].append({'route': 'model_api', 'count': 0,
            'user_id': 'usr_private', 'job_ids': [123], 'note': 'free text secret'})
    fake = _PagedRollup(sources)
    monkeypatch.setattr(summary.db, 'admin_lane_rollup', fake)
    monkeypatch.setattr(summary, 'default_day', lambda: DAY)
    for params in ({'day': DAY}, {}):
        response = client.get('/v1/admin/lane-rollup/summary', params=params,
                              headers={'X-Admin-Token': 'summary-admin'})
        assert response.status_code == 200
        actual = response.json()
        assert actual == summary.build_summary(sources, day=DAY)
        assert actual['coverage'] == {lane: payload['coverage'] for lane, payload in sources['lane_rollup'].items()}
        assert set(actual['cells']) == {'capture/resident', 'capture/model_api', 'dream/resident', 'dream/model_api'}
        text = response.text + report_tool.render_message(actual)
        ids = {row['user_id'] for payload in sources['lane_rollup'].values() for row in payload['rows']}
        assert ids and not any(uid in text for uid in ids)
        assert not any(word in text for word in ('usr_', 'user_id', 'job_ids', 'free text secret'))


@pytest.mark.parametrize('params,error', [
    ({'day': '2026-02-30'}, 'invalid_day'), ({'day': '20260914'}, 'invalid_day'),
    ({'day': '2026-9-14'}, 'invalid_day'), ({'day': ''}, 'invalid_day'),
    ({'day': '0001-01-01'}, 'invalid_day'),
    ({'uid': 'x', 'bogus': '1'}, 'unknown_query_params'),
])
def test_endpoint_rejects_bad_parameters_before_db(client, monkeypatch, params, error):
    def never(**kw):
        pytest.fail('invalid request reached DB')
    monkeypatch.setattr(summary.db, 'admin_lane_rollup', never)
    response = client.get('/v1/admin/lane-rollup/summary', params=params,
                          headers={'X-Admin-Token': 'summary-admin'})
    assert response.status_code == 400 and response.json()['error'] == error
    if error == 'unknown_query_params':
        assert response.json() == {'error': error, 'params': ['bogus', 'uid'], 'supported': ['day']}


def test_endpoint_requires_admin_and_accepts_legacy_admin_key(client, monkeypatch):
    fake = _PagedRollup(_sources())
    monkeypatch.setattr(summary.db, 'admin_lane_rollup', fake)
    path = '/v1/admin/lane-rollup/summary'
    assert client.get(path, params={'day': DAY}).status_code == 401
    assert client.get(path, headers={'X-Admin-Token': 'wrong'}).status_code == 401
    assert not fake.calls
    assert client.get(path, params={'day': DAY, 'admin_key': 'summary-admin'}).status_code == 200
