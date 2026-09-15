"""Daily memory-pipeline Lark report (tools/memory_pipeline_daily_report.py).

Pure unit tests: no database, no network. Every HTTP call goes through an
injected fake opener, so nothing here can reach the admin API or post a real
Lark message.

The fixture ``tests/fixtures/memory_pipeline_daily_report/sources_2026-09-14.json``
uses the exact row projection of ``db.admin_lane_rollup``;
``tests/test_lane_rollup.py`` locks those key sets (and the outcome columns'
meaning) against the real DB-backed producer so the fixture cannot silently
drift from the endpoint.
"""
from __future__ import annotations

import copy
import io
import json
import os
import re
import subprocess
import sys
import textwrap
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

from notices import error_contract  # noqa: E402
from tools import memory_pipeline_daily_report as report_tool  # noqa: E402
from tools.strict_yaml import load_yaml_strict  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "memory_pipeline_daily_report" / "sources_2026-09-14.json"
WORKFLOW = ROOT / ".github" / "workflows" / "memory-pipeline-daily.yml"
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
    return report_tool.build_report(sources, day=DAY)


# --------------------------------------------------------------------------- #
# Message from the realistic fixture
# --------------------------------------------------------------------------- #

def test_fixture_message_summarizes_each_lane_route_and_marks_attention():
    text = report_tool.render_message(report_tool.build_report(_sources(), day=DAY))

    assert text.splitlines()[0] == "[需要关注] 记忆管线日报 2026-09-14（北京时间）"
    # Attention block names what fired, with the threshold.
    assert "- 落卡 · V2 model_api：我们这边的故障影响 8 人（阈值 5）" in text
    assert "- 落卡 · V2 model_api：失败率 30%，前一天 3%" in text
    # Only V2 capture fires: the V1 users out of balance and the scheduler
    # skips are real, but they are not our failures (review 09-15).
    assert sum(1 for line in text.splitlines() if line.startswith("- ")) == 2
    # Failure counts are operational failures (V2 includes expired jobs).
    assert ("  V1 resident：活跃 26 人｜成功 139｜失败 14（9%，前一天 0%）｜完全卡死 2 人"
            in text)
    assert ("  V2 model_api：活跃 35 人｜成功 172｜失败 72（30%，前一天 3%）｜完全卡死 8 人"
            in text)
    assert ("失败原因：账号/配置类 8 次/2 人（capture_agent_call_failed:resident_agent_cli_logged_out 8）；"
            "我们这边 3 次/3 人（capture_memory_actions_failed 3）；未知 3 次/3 人（runtime_failed 3）") in text
    assert ("不算失败：确认是用户自己账号问题 24 次/4 人（capture_agent_call_failed:quota_insufficient 24）；"
            "跳过/关闭等控制结果 9 次") in text
    assert "模型服务 18 次/6 人（extraction_failed:upstream_unavailable 12、extraction_failed:rate_limited 6）" in text
    assert "我们这边 48 次/8 人（extraction_failed:json_decode_error 24、lease_timeout 24）" in text
    assert "未知 6 次/6 人（no_code 6）" in text
    assert ("不算失败：确认是用户自己账号问题 24 次/4 人（extraction_failed:auth_invalid 24）；"
            "跳过/关闭等控制结果 10 次") in text
    # Dream skips come from the rollup's silent_declared and are not successes.
    assert "  V2 model_api：活跃 14 人｜成功 3｜失败 0（0%，前一天 0%）｜完全卡死 0 人" in text
    assert "花园太小跳过 8 次" in text
    # V1 live stuck counts only jobs created in the last 24h (2 of the orphan's 40).
    assert "此刻卡着没结束的任务：落卡 5 · 做梦 0" in text
    assert "数据说明" not in text


def test_message_is_content_free():
    sources = _sources()
    text = report_tool.render_message(report_tool.build_report(sources, day=DAY))
    user_ids = {r["user_id"] for lane in sources["lane_rollup"].values()
                for r in lane["rows"]}
    assert user_ids  # the fixture really carries ids that must not leak
    assert not any(uid in text for uid in user_ids)
    assert "usr_" not in text
    assert "job" not in text.lower()


def test_cause_breakdown_always_sums_to_operational_failures():
    report = report_tool.build_report(_sources(), day=DAY)
    for stats in list(report.today.values()) + list(report.previous.values()):
        assert sum(c.attempts for c in stats.causes.values()) == stats.operational
        assert stats.operational + stats.control + stats.user_unavailable == stats.failed_raw


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
    assert report_tool.classify_failure_code(code) == group


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
        assert report_tool.classify_failure_code(spec.code) == expected, spec.code
        checked += 1
    assert checked > 30


# --------------------------------------------------------------------------- #
# Attention thresholds
# --------------------------------------------------------------------------- #

def _stuck_rows(n, code="lease_timeout"):
    return [_row(f"usr_{i:016x}", failed=2, codes={code: 2}) for i in range(n)]


def test_stuck_users_threshold_boundary():
    below = _report(_stuck_rows(report_tool.STUCK_USERS_ATTENTION - 1))
    at = _report(_stuck_rows(report_tool.STUCK_USERS_ATTENTION))
    assert not any("完全卡死" in r for r in below.attention)
    assert any("完全卡死 10 人" in r for r in at.attention)


def test_user_with_one_success_is_not_counted_as_stuck():
    rows = [_row("usr_a", failed=5, codes={"lease_timeout": 5}),
            _row("usr_b", failed=5, completed=1, codes={"lease_timeout": 5}),
            _row("usr_c", completed=3)]
    stats = report_tool.aggregate_day(rows, lane="capture", route="model_api", day=DAY)
    assert stats.stuck_users == 1
    assert len(stats.users) == 3


def test_split_rows_of_one_user_are_summed_before_stuck_check():
    # heartbeat-style enqueue_source splits produce several rows per user.
    rows = [_row("usr_a", failed=2, codes={"lease_timeout": 2}),
            dict(_row("usr_a", completed=1), enqueue_source="clock")]
    stats = report_tool.aggregate_day(rows, lane="capture", route="model_api", day=DAY)
    assert stats.stuck_users == 0


def test_our_side_users_threshold_boundary():
    healthy = [_row(f"usr_ok{i:013x}", completed=50) for i in range(10)]
    def rows(n):
        return healthy + [_row(f"usr_{i:016x}", completed=1, failed=1,
                               codes={"lease_timeout": 1}) for i in range(n)]
    assert not any("我们这边" in r for r in _report(rows(4)).attention)
    assert any("我们这边的故障影响 5 人" in r for r in _report(rows(5)).attention)


def test_failure_rate_jump_boundary_and_minimum_attempts():
    def rows(fail_today, *, attempts=40):
        prev = [_row("usr_p", day=PREV, completed=36, failed=4,
                     codes={"extraction_failed:rate_limited": 4})]  # 10%
        today = [_row("usr_t", completed=attempts - fail_today, failed=fail_today,
                      codes={"extraction_failed:rate_limited": fail_today})]
        return prev + today
    no_jump = _report(rows(9, attempts=40))   # 22.5% → +12.5pp
    jump = _report(rows(10, attempts=40))     # 25% → +15pp
    assert not any("失败率" in r for r in no_jump.attention)
    assert any("失败率 25%，前一天 10%" in r for r in jump.attention)
    # Same jump on too few attempts stays quiet.
    few = [_row("usr_p", day=PREV, completed=9, failed=1, codes={"lease_timeout": 1}),
           _row("usr_t", completed=6, failed=4, codes={"lease_timeout": 4})]
    assert not any("失败率" in r for r in _report(few).attention)


def test_high_failure_rate_fires_without_previous_day():
    rows = [_row("usr_t", completed=10, failed=10,
                 codes={"extraction_failed:upstream_unavailable": 10})]
    assert any("失败率 50%" in r for r in _report(rows).attention)
    rows = [_row("usr_t", completed=11, failed=9,
                 codes={"extraction_failed:upstream_unavailable": 9})]
    assert not any("失败率" in r for r in _report(rows).attention)
    # A 60% day on 5 attempts is noise, not an incident.
    rows = [_row("usr_t", completed=2, failed=3,
                 codes={"extraction_failed:upstream_unavailable": 3})]
    assert not any("失败率" in r for r in _report(rows).attention)


def test_silent_stop_is_detected_by_active_user_drop():
    prev = [_row(f"usr_{i:016x}", day=PREV, completed=3) for i in range(12)]
    kept = [_row(f"usr_{i:016x}", completed=3) for i in range(6)]
    dropped = [_row(f"usr_{i:016x}", completed=3) for i in range(5)]
    assert not any("活跃用户" in r for r in _report(prev + kept).attention)
    fired = _report(prev + dropped).attention
    assert any("活跃用户 5 人，前一天 12 人" in r for r in fired)


def test_live_stuck_jobs_threshold():
    def stuck(n):
        return _report(stuck_total=n, stuck_rows=[{"route": "model_api", "count": n}])
    assert not any("卡着没结束" in r for r in stuck(19).attention)
    assert any("此刻有 20 个任务" in r for r in stuck(20).attention)


def test_unfrozen_day_is_flagged_not_reported_as_healthy():
    live = [_row("usr_a", completed=1, failed=1, frozen=False)]
    report = _report(through=PREV, today_partial=live)
    text = report_tool.render_message(report)
    assert text.startswith("[需要关注]")
    assert "当天统计还没冻结" in text
    stats = report.today[("capture", "model_api")]
    assert stats.operational == 1 and stats.causes["unknown"].codes == {"no_code": 1}


def test_lagging_freezer_is_flagged_even_without_live_rows():
    # A stalled freezer with no live tail must not read as "当天没有任务".
    report = _report(through=PREV)
    assert any("数据不完整" in r for r in report.attention)
    assert "落卡 capture · V2 model_api 当天统计还没冻结" in report_tool.render_message(report)


def test_missing_lane_payload_is_flagged():
    sources = _sources()
    sources["lane_rollup"].pop("dream")
    text = report_tool.render_message(report_tool.build_report(sources, day=DAY))
    assert text.startswith("[需要关注]")
    assert "做梦 dream 没取到数据" in text


def test_quiet_healthy_day_is_marked_normal():
    rows = [_row(f"usr_{i:016x}", completed=5) for i in range(3)]
    text = report_tool.render_message(_report(rows))
    assert text.splitlines()[0].startswith("[正常]")
    assert "V1 resident：当天没有任务" in text


def test_dream_skips_come_from_silent_declared_and_are_not_successes():
    rows = [_row("usr_a", lane="dream", completed=5, silent_declared=5),
            _row("usr_b", lane="dream", completed=3, silent_declared=1, failed=1,
                 codes={"extraction_failed:output_truncated": 1}),
            _row("usr_c", lane="dream", completed=0, failed=1,
                 codes={"extraction_failed:upstream_unavailable": 1}),
            _row("usr_d", lane="dream", completed=4, silent_declared=4, failed=2,
                 codes={"extraction_failed:upstream_unavailable": 2})]
    stats = report_tool.aggregate_day(rows, lane="dream", route="model_api", day=DAY)
    assert (stats.completed, stats.skipped, stats.operational) == (2, 10, 4)
    # A user whose only completions were skips never succeeded that day.
    assert stats.stuck_users == 2
    # Capture has no declared skips: silent_declared there is not subtracted.
    capture = report_tool.aggregate_day(
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
    stats = report.today[("capture", "resident")]
    assert (stats.operational, stats.control, stats.user_unavailable) == (0, 48, 36)
    assert stats.stuck_users == 0
    assert stats.failure_rate == 0
    assert report.attention == []
    text = report_tool.render_message(report)
    assert text.startswith("[正常]")
    assert ("不算失败：确认是用户自己账号问题 36 次/12 人（capture_agent_call_failed:quota_insufficient 36）；"
            "跳过/关闭等控制结果 48 次") in text


def test_v1_codes_shared_with_skips_are_not_guessed_into_a_group():
    # One cell: 2 skips + 3 real failures, all recorded under capture_* reasons.
    rows = [_row("usr_mix", route="resident", completed=1, failed=5, control=2,
                 codes={"capture_window_unavailable": 2, "capture_memory_write_failed": 3})]
    stats = report_tool.aggregate_day(rows, lane="capture", route="resident", day=DAY)
    assert stats.operational == 3
    assert stats.causes["unknown"].codes == {"unattributed": 3}
    assert not stats.causes["our_side"].users
    # Fewer codes than failures, but a skip shares them: still not guessed.
    rows = [_row("usr_mix2", route="resident", completed=1, failed=5, control=2,
                 codes={"capture_window_unavailable": 2})]
    stats = report_tool.aggregate_day(rows, lane="capture", route="resident", day=DAY)
    assert stats.causes["unknown"].codes == {"unattributed": 3}
    assert not stats.causes["our_side"].users
    # Without control outcomes the leftover codes are exactly the failures.
    rows = [_row("usr_ours", route="resident", completed=1, failed=4,
                 codes={"capture_memory_write_failed": 3})]
    stats = report_tool.aggregate_day(rows, lane="capture", route="resident", day=DAY)
    assert stats.causes["our_side"].codes == {"capture_memory_write_failed": 3}
    assert stats.causes["unknown"].codes == {"no_code": 1}


def test_v2_control_and_user_unavailable_codes_follow_the_v2_classifier():
    rows = [_row(f"usr_{i:016x}", completed=0, failed=3,
                 codes={"capture_disabled": 1, "turns_halted": 1,
                        "provider_setup:model_api_not_configured": 1})
            for i in range(12)]
    report = _report(rows)
    stats = report.today[("capture", "model_api")]
    assert (stats.operational, stats.control, stats.user_unavailable) == (0, 24, 12)
    assert stats.stuck_users == 0 and report.attention == []


def test_v2_classifier_literal_matches_jobs_store():
    from model_api_runtime.v2 import jobs_store
    import db
    assert report_tool.v2_control_outcome_codes() == jobs_store.CONTROL_OUTCOME_CODES
    assert report_tool.skip_declared_lanes() == db.LANE_ROLLUP_SKIP_DECLARED_LANES
    for code in ("capture_disabled", "extraction_failed:auth_invalid", "lease_timeout",
                 "queue_timeout", "extraction_failed:upstream_unavailable"):
        stats = report_tool.aggregate_day(
            [_row("usr_a", failed=1, codes={code: 1})],
            lane="capture", route="model_api", day=DAY)
        expected = jobs_store.terminal_outcome_class(code)
        got = ("control" if stats.control else "user_unavailable" if stats.user_unavailable
               else "operational_failure")
        assert got == ("operational_failure" if expected == "timeout" else expected), code


def test_unmeasured_or_unbalanced_v1_outcomes_count_raw_and_say_so():
    rows = [_row(f"usr_{i:016x}", route="resident", failed=2,
                 codes={"capture_window_unavailable": 2}, control=2) for i in range(3)]
    measured = _report(rows)
    assert measured.today[("capture", "resident")].operational == 0
    unmeasured = _report(rows, outcomes_from=None)
    stats = unmeasured.today[("capture", "resident")]
    assert stats.operational == 6 and stats.unclassified
    assert any("失败分类缺失" in note for note in unmeasured.incomplete)
    assert unmeasured.attention
    broken = [_row("usr_x", route="resident", failed=5, operational=1, control=1)]
    stats = _report(broken).today[("capture", "resident")]
    assert stats.operational == 5 and stats.unclassified


def test_live_stuck_counts_only_recent_v1_jobs():
    stuck_rows = [
        {"route": "resident", "lane": "capture", "count": 40, "recent_count": 2},
        {"route": "model_api", "lane": "capture", "count": 19},
    ]
    report = _report(stuck_total=59, stuck_rows=stuck_rows)
    assert report.live_stuck["capture"] == 21
    assert any("此刻有 21 个任务" in r for r in report.attention)
    # An older backend without recent_count keeps the full count.
    legacy = [{"route": "resident", "lane": "capture", "count": 40}]
    assert _report(stuck_total=40, stuck_rows=legacy).live_stuck["capture"] == 40


# --------------------------------------------------------------------------- #
# Lark signature and payload
# --------------------------------------------------------------------------- #

def _ci_sign_snippet() -> str:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    snippets = re.findall(r"SIGN=\$\(python3 - <<'PY'\n(.*?)\n\s*PY\n", workflow, re.S)
    assert len(snippets) == 2, "expected the deploy-start and deploy-finish signers"
    assert len({textwrap.dedent(s) for s in snippets}) == 1
    return textwrap.dedent(snippets[0])


@pytest.mark.parametrize("timestamp,secret", [
    ("1757900000", "lark-test-secret"),
    ("1600000000", "含中文的 secret\twith tab"),
])
def test_signature_matches_the_ci_yml_signer(timestamp, secret):
    result = subprocess.run(
        [sys.executable, "-c", _ci_sign_snippet()],
        capture_output=True, text=True, check=True,
        env={**os.environ, "SIGN_TS": timestamp, "LARK_BOT_SECRET": secret},
    )
    assert report_tool.lark_sign(timestamp, secret) == result.stdout.strip()


def test_signature_known_vector():
    # Pinned so the tool and ci.yml cannot drift together to a wrong scheme:
    # HMAC-SHA256 keyed by "timestamp\nsecret" over an empty message.
    assert report_tool.lark_sign("1757900000", "lark-test-secret") == \
        "rWW3ygiAdSmKEIysYTVjbdbv4Xnqjn35pAGLgGJZsNg="


def test_payload_shape_matches_ci_yml_jq():
    assert report_tool.lark_payload("hi") == {"msg_type": "text", "content": {"text": "hi"}}
    signed = report_tool.lark_payload("hi", secret="s", timestamp="123")
    assert signed == {"timestamp": "123", "sign": report_tool.lark_sign("123", "s"),
                      "msg_type": "text", "content": {"text": "hi"}}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_post_requires_lark_success_code():
    def ok(request, timeout):
        return _Resp(b'{"code":0,"msg":"success"}')

    def bad_sign(request, timeout):
        return _Resp(b'{"code":19021,"msg":"sign match fail or timestamp is not within one hour"}')

    report_tool.post_to_lark("https://lark.invalid/hook", {"x": 1}, opener=ok)
    with pytest.raises(RuntimeError, match="lark_rejected:code=19021"):
        report_tool.post_to_lark("https://lark.invalid/hook", {"x": 1}, opener=bad_sign)


def test_post_accepts_both_documented_success_shapes():
    for body in (b'{"code":0,"msg":"success","data":{}}',
                 b'{"StatusCode":0,"StatusMessage":"success"}',
                 b'{"Extra":null,"StatusCode":0,"StatusMessage":"success","code":0,"msg":"success"}'):
        report_tool.post_to_lark("https://lark.invalid/hook", {"x": 1},
                                 opener=lambda request, timeout, b=body: _Resp(b))


@pytest.mark.parametrize("body, error", [
    (b"{}", "lark_invalid_response"),
    (b"[]", "lark_invalid_response"),
    (b'{"msg":"success"}', "lark_invalid_response"),
    (b'{"code":"0","msg":"success"}', "lark_invalid_response"),
    (b'{"code":null}', "lark_invalid_response"),
    (b'{"code":false}', "lark_invalid_response"),
    (b'{"code":0,"StatusCode":"0"}', "lark_invalid_response"),
    (b"", "lark_response_not_json"),
    (b'{"code":19021,"msg":"sign match fail"}', "lark_rejected:code=19021"),
    (b'{"code":0,"StatusCode":9499}', "lark_rejected:code=9499"),
])
def test_post_rejects_anything_but_an_explicit_zero_code(body, error):
    """Codex review 2026-09-15 (I2). Before: HTTP 200 ``{}`` counted as posted
    and ``[]`` crashed with AttributeError. After: both are post failures."""
    with pytest.raises(RuntimeError) as exc_info:
        report_tool.post_to_lark("https://lark.invalid/hook", {"x": 1},
                                 opener=lambda request, timeout: _Resp(body))
    assert str(exc_info.value) == error


@pytest.mark.parametrize("body", [b"{}", b"[]", b'{"code":"0"}'])
def test_malformed_lark_success_fails_the_run_instead_of_reporting_posted(body):
    posts = []

    def opener(request, timeout):
        posts.append(request.full_url)
        return _Resp(body)

    out, err = io.StringIO(), io.StringIO()
    old_err = sys.stderr
    sys.stderr = err
    try:
        code = report_tool.main(
            ["--fixture", str(FIXTURE), "--day", DAY], opener=opener,
            environ={"LARK_BOT_WEBHOOK": "https://lark.invalid/hook"}, out=out)
    finally:
        sys.stderr = old_err
    assert posts == ["https://lark.invalid/hook"]
    assert code == 3
    assert "posted memory pipeline daily report" not in out.getvalue()
    assert "lark post failed: lark_invalid_response" in err.getvalue()


# --------------------------------------------------------------------------- #
# CLI: dry-run, live read with fakes, fetch failure
# --------------------------------------------------------------------------- #

def _never(request, timeout):
    raise AssertionError(f"unexpected network call: {request.full_url}")


def test_dry_run_prints_and_never_posts():
    out = io.StringIO()
    code = report_tool.main(
        ["--fixture", str(FIXTURE), "--day", DAY, "--dry-run"],
        opener=_never,
        environ={"LARK_BOT_WEBHOOK": "https://lark.invalid/hook", "LARK_BOT_SECRET": "s"},
        out=out,
    )
    assert code == 0
    assert out.getvalue().startswith("[需要关注] 记忆管线日报 2026-09-14")


def test_dry_run_script_entrypoint():
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "memory_pipeline_daily_report.py"),
         "--fixture", str(FIXTURE), "--day", DAY, "--dry-run"],
        capture_output=True, text=True,
        env={k: v for k, v in os.environ.items()
             if k not in {"LARK_BOT_WEBHOOK", "FEEDLING_ADMIN_TOKEN"}},
    )
    assert result.returncode == 0, result.stderr
    assert "记忆管线日报 2026-09-14" in result.stdout


class _FakeAdminAndLark:
    """Serves the fixture as the admin API (one row per page) and records Lark posts."""

    def __init__(self, sources, *, token="admin-secret", page_limit=None):
        self.sources = sources
        self.token = token
        self.posts = []
        self.admin_calls = []
        self.page_limit = page_limit

    def __call__(self, request, timeout):
        url = urllib.parse.urlsplit(request.full_url)
        if url.netloc == "lark.invalid":
            self.posts.append(json.loads(request.data))
            return _Resp(b'{"code":0}')
        assert request.get_header("X-admin-token") == self.token
        query = dict(urllib.parse.parse_qsl(url.query))
        self.admin_calls.append((url.path, query))
        if url.path == "/v1/admin/lane-rollup":
            full = self.sources["lane_rollup"][query["lane"]]
            limit = self.page_limit or int(query["limit"])
            offset = int(query["offset"])
            rows = full["rows"][offset:offset + limit]
            page = dict(copy.deepcopy(full), rows=rows)
            page["pagination"] = {"limit": limit, "offset": offset,
                                  "returned": len(rows), "total": len(full["rows"])}
            return _Resp(json.dumps(page).encode())
        raise AssertionError(url.path)


def test_live_mode_reads_admin_api_and_posts_signed_message(monkeypatch):
    fake = _FakeAdminAndLark(_sources())
    code = report_tool.main(
        ["--day", DAY], opener=fake,
        environ={"FEEDLING_API_URL": "https://api.invalid",
                 "FEEDLING_ADMIN_TOKEN": "admin-secret",
                 "LARK_BOT_WEBHOOK": "https://lark.invalid/hook",
                 "LARK_BOT_SECRET": "lark-secret"},
        out=io.StringIO(),
    )
    assert code == 0
    rollup_calls = [q for p, q in fake.admin_calls if p == "/v1/admin/lane-rollup"]
    assert {q["lane"] for q in rollup_calls} == {"capture", "dream"}
    assert all(q["since_day"] == PREV and q["until_day"] == DAY for q in rollup_calls)
    assert {p for p, _ in fake.admin_calls} == {"/v1/admin/lane-rollup"}
    (post,) = fake.posts
    assert post["sign"] == report_tool.lark_sign(post["timestamp"], "lark-secret")
    expected = report_tool.render_message(report_tool.build_report(_sources(), day=DAY))
    assert post["content"]["text"] == expected
    assert "admin-secret" not in json.dumps(post)


def test_lane_rollup_pages_are_merged(monkeypatch):
    monkeypatch.setattr(report_tool, "LANE_ROLLUP_PAGE_LIMIT", 7)
    fake = _FakeAdminAndLark(_sources(), page_limit=7)
    merged = report_tool.fetch_lane_rollup("https://api.invalid", "admin-secret",
                                           lane="capture", since_day=PREV,
                                           until_day=DAY, opener=fake)
    assert len(merged["rows"]) == len(_sources()["lane_rollup"]["capture"]["rows"])
    assert len(fake.admin_calls) > 1
    assert "truncated" not in merged


def test_lane_rollup_pages_are_deduplicated_by_full_cell_key(monkeypatch):
    """An older backend orders pages without ``route``: a cell can slide across
    a page boundary and come back on the next page."""
    rows = _sources()["lane_rollup"]["capture"]["rows"][:4]
    pages = [[rows[0], rows[1]], [rows[1], rows[2]], [rows[3]]]
    calls = []

    def opener(request, timeout):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(request.full_url).query))
        page = pages[len(calls)]
        calls.append(int(query["offset"]))
        return _Resp(json.dumps({"rows": page, "pagination": {
            "returned": len(page), "total": sum(len(p) for p in pages)}}).encode())

    monkeypatch.setattr(report_tool, "LANE_ROLLUP_PAGE_LIMIT", 2)
    merged = report_tool.fetch_lane_rollup("https://api.invalid", "t", lane="capture",
                                           since_day=PREV, until_day=DAY, opener=opener)
    assert merged["rows"] == rows
    # Same user/day/lane on the other route is a different cell, not a duplicate.
    other_route = dict(rows[0], route="model_api")
    pages[:] = [[rows[0], other_route]]
    calls.clear()
    merged = report_tool.fetch_lane_rollup("https://api.invalid", "t", lane="capture",
                                           since_day=PREV, until_day=DAY, opener=opener)
    assert merged["rows"] == [rows[0], other_route]


def test_lane_rollup_page_cap_is_declared(monkeypatch):
    monkeypatch.setattr(report_tool, "LANE_ROLLUP_PAGE_LIMIT", 1)
    monkeypatch.setattr(report_tool, "LANE_ROLLUP_MAX_PAGES", 3)
    fake = _FakeAdminAndLark(_sources(), page_limit=1)
    merged = report_tool.fetch_lane_rollup("https://api.invalid", "admin-secret",
                                           lane="capture", since_day=PREV,
                                           until_day=DAY, opener=fake)
    assert merged["truncated"] is True and len(merged["rows"]) == 3


def test_fetch_failure_still_posts_a_content_free_notice():
    posts = []

    def opener(request, timeout):
        if "lark.invalid" in request.full_url:
            posts.append(json.loads(request.data))
            return _Resp(b'{"code":0}')
        raise urllib.error.HTTPError(request.full_url, 503, "body with secrets", {}, None)

    code = report_tool.main(
        ["--day", DAY], opener=opener,
        environ={"FEEDLING_API_URL": "https://api.invalid",
                 "FEEDLING_ADMIN_TOKEN": "admin-secret",
                 "LARK_BOT_WEBHOOK": "https://lark.invalid/hook"},
        out=io.StringIO(),
    )
    assert code == 1
    (post,) = posts
    text = post["content"]["text"]
    assert text.startswith("[需要关注] 记忆管线日报 2026-09-14（北京时间）没生成出来")
    assert "/v1/admin/lane-rollup HTTP 503" in text
    assert "body with secrets" not in text and "admin-secret" not in text


@pytest.mark.parametrize("stage", ["build", "fetch"])
def test_unexpected_error_still_posts_a_content_free_notice(monkeypatch, stage):
    """Review 09-15: only FetchError was caught, so a response shape change
    crashed the run with nothing posted."""
    posts = []

    def boom(*_args, **_kwargs):
        raise TypeError("usr_leaky_secret_value")

    if stage == "build":
        monkeypatch.setattr(report_tool, "build_report", boom)
        argv, environ = ["--fixture", str(FIXTURE), "--day", DAY], {}
    else:
        monkeypatch.setattr(report_tool, "fetch_sources", boom)
        argv = ["--day", DAY]
        environ = {"FEEDLING_API_URL": "https://api.invalid",
                   "FEEDLING_ADMIN_TOKEN": "admin-secret"}

    def opener(request, timeout):
        posts.append(json.loads(request.data))
        return _Resp(b'{"code":0}')

    code = report_tool.main(argv, opener=opener,
                            environ={**environ, "LARK_BOT_WEBHOOK": "https://lark.invalid/hook"},
                            out=io.StringIO())
    assert code == 1
    (post,) = posts
    text = post["content"]["text"]
    assert text.startswith("[需要关注] 记忆管线日报 2026-09-14（北京时间）没生成出来")
    assert "TypeError" in text
    assert "usr_leaky_secret_value" not in text and "admin-secret" not in text


def test_changed_response_shape_is_reported_not_crashed():
    posts = []

    def opener(request, timeout):
        if "lark.invalid" in request.full_url:
            posts.append(json.loads(request.data))
            return _Resp(b'{"code":0}')
        return _Resp(b'{"rows": [{"route": "model_api", "lane": "capture", "day": "2026-09-14", '
                     b'"completed": 1, "failure_codes": {"x": 1}, "failed": 1}], '
                     b'"stuck": {"rows": "not-a-list", "total": 0}, '
                     b'"coverage": {"resident": "renamed"}, '
                     b'"pagination": {"returned": 1, "total": 1}}')

    code = report_tool.main(["--day", DAY], opener=opener,
                            environ={"FEEDLING_API_URL": "https://api.invalid",
                                     "FEEDLING_ADMIN_TOKEN": "admin-secret",
                                     "LARK_BOT_WEBHOOK": "https://lark.invalid/hook"},
                            out=io.StringIO())
    assert code == 1
    assert "生成报告出错：AttributeError" in posts[0]["content"]["text"]


def test_missing_admin_config_is_a_fetch_failure_not_silence():
    posts = []

    def opener(request, timeout):
        posts.append(json.loads(request.data))
        return _Resp(b'{"code":0}')

    code = report_tool.main(["--day", DAY], opener=opener,
                            environ={"LARK_BOT_WEBHOOK": "https://lark.invalid/hook"},
                            out=io.StringIO())
    assert code == 1
    assert "未配置" in posts[0]["content"]["text"]


def test_default_day_is_previous_beijing_day():
    from datetime import datetime, timezone
    # 2026-09-15 01:30 UTC = 09:30 Beijing → report 09-14.
    assert report_tool.default_day(datetime(2026, 9, 15, 1, 30, tzinfo=timezone.utc)) == DAY
    # 2026-09-14 17:00 UTC = 09-15 01:00 Beijing → still report 09-14.
    assert report_tool.default_day(datetime(2026, 9, 14, 17, 0, tzinfo=timezone.utc)) == DAY


# --------------------------------------------------------------------------- #
# Workflow wiring
# --------------------------------------------------------------------------- #

def test_workflow_runs_daily_against_prod_with_existing_secrets():
    text = WORKFLOW.read_text(encoding="utf-8")
    workflow = load_yaml_strict(text, source_name=str(WORKFLOW.relative_to(ROOT)))
    triggers = workflow.get("on") or workflow.get(True)
    assert triggers["schedule"] == [{"cron": "30 1 * * *"}]
    assert "workflow_dispatch" in triggers
    assert workflow["permissions"] == {"contents": "read"}
    (job,) = workflow["jobs"].values()
    run_step = next(s for s in job["steps"]
                    if "tools/memory_pipeline_daily_report.py" in str(s.get("run", "")))
    env = run_step["env"]
    assert env["FEEDLING_ADMIN_TOKEN"] == "${{ secrets.FEEDLING_ADMIN_TOKEN }}"
    assert env["LARK_BOT_WEBHOOK"] == "${{ secrets.LARK_BOT_WEBHOOK }}"
    assert env["LARK_BOT_SECRET"] == "${{ secrets.LARK_BOT_SECRET }}"
    assert "api.feedling.app" in env["FEEDLING_API_URL"]
    # Secrets must reach the tool via env only, never argv.
    assert "secrets." not in run_step["run"]
    # Attention thresholds are overridable from repository variables.
    for name in report_tool.THRESHOLD_ENV_VARS:
        assert env[name] == f"${{{{ vars.{name} }}}}"


def test_thresholds_keep_defaults_and_follow_env_overrides(monkeypatch):
    """Codex review 2026-09-15 (M1): the thresholds are unbaselined initial
    values, so they must be tunable without a code change."""
    import importlib

    for name in report_tool.THRESHOLD_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    try:
        importlib.reload(report_tool)
        assert (report_tool.STUCK_USERS_ATTENTION, report_tool.OUR_SIDE_USERS_ATTENTION,
                report_tool.MIN_ATTEMPTS_FOR_RATE, report_tool.FAILURE_RATE_ATTENTION,
                report_tool.FAILURE_RATE_JUMP_PP, report_tool.ACTIVE_DROP_RATIO,
                report_tool.ACTIVE_DROP_MIN_PREVIOUS,
                report_tool.LIVE_STUCK_JOBS_ATTENTION) == (10, 5, 20, 0.5, 15.0, 0.5, 10, 20)

        monkeypatch.setenv("MEMORY_REPORT_STUCK_USERS", "3")
        monkeypatch.setenv("MEMORY_REPORT_FAILURE_RATE", "0.8")
        monkeypatch.setenv("MEMORY_REPORT_LIVE_STUCK_JOBS", "")       # unset var in Actions
        monkeypatch.setenv("MEMORY_REPORT_OUR_SIDE_USERS", "many")    # typo keeps default
        importlib.reload(report_tool)
        assert report_tool.STUCK_USERS_ATTENTION == 3
        assert report_tool.FAILURE_RATE_ATTENTION == 0.8
        assert report_tool.LIVE_STUCK_JOBS_ATTENTION == 20
        assert report_tool.OUR_SIDE_USERS_ATTENTION == 5
        # The override reaches the rule, not just the constant.
        assert any("完全卡死 3 人" in r for r in _report(_stuck_rows(3)).attention)
    finally:
        for name in report_tool.THRESHOLD_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        importlib.reload(report_tool)
