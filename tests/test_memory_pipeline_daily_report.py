"""Daily memory-pipeline Lark report (tools/memory_pipeline_daily_report.py).

Pure unit tests: no database, no network. Every HTTP call goes through an
injected fake opener, so nothing here can reach the admin API or post a real
Lark message.

The fixture ``tests/fixtures/memory_pipeline_daily_report/sources_2026-09-14.json``
uses the exact row projection of ``db.admin_lane_rollup`` and the dream-job
projection of ``admin.memory_metadata``; ``tests/test_lane_rollup.py`` locks
those key sets against the real DB-backed producers so the fixture cannot
silently drift from the endpoint.
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
         failed=0, expired=0, codes=None, frozen=True):
    return {"user_id": uid, "day": day, "route": route, "lane": lane,
            "enqueue_source": "", "access_path": "apikey_v2",
            "mode_source": "explicit", "completed": completed, "failed": failed,
            "expired": expired, "superseded": 0, "failure_codes": codes or {},
            "frozen": frozen, "operational_failures": 0, "control_outcomes": 0,
            "user_unavailable": 0, "spoke": 0, "spoke_completed": 0,
            "silent_declared": 0, "silent_undeclared": 0}


def _payload(rows, *, through=DAY, stuck_total=0, today_partial=None):
    cov = {"backfill_from": "2026-08-01", "through_day": through,
           "partial_before": "2026-08-01", "voice_from": None,
           "outcomes_from": None, "access_path_from": None}
    return {"rows": rows, "today_partial": today_partial or [],
            "stuck": {"rows": [], "total": stuck_total, "stuck_after_hours": 6.0,
                      "note": ""},
            "coverage": {"resident": dict(cov), "model_api": dict(cov)},
            "pagination": {"limit": 500, "offset": 0, "returned": len(rows),
                           "total": len(rows)},
            "filters": {}}


def _report(capture_rows=(), dream_rows=(), **payload_kwargs):
    sources = {"lane_rollup": {"capture": _payload(list(capture_rows), **payload_kwargs),
                               "dream": _payload(list(dream_rows))},
               "dream_jobs": {"jobs": []}}
    return report_tool.build_report(sources, day=DAY)


# --------------------------------------------------------------------------- #
# Message from the realistic fixture
# --------------------------------------------------------------------------- #

def test_fixture_message_summarizes_each_lane_route_and_marks_attention():
    text = report_tool.render_message(report_tool.build_report(_sources(), day=DAY))

    assert text.splitlines()[0] == "[需要关注] 记忆管线日报 2026-09-14（北京时间）"
    # Attention block names what fired, with the threshold.
    assert "- 落卡 · V2 model_api：完全卡死 12 人（有失败、零成功，阈值 10）" in text
    assert "- 落卡 · V2 model_api：我们这边的故障影响 8 人（阈值 5）" in text
    assert "- 落卡 · V2 model_api：失败率 37%，前一天 5%" in text
    # Per lane/route counts. V2 failed includes expired jobs (90 failed + 6 expired).
    assert ("  V1 resident：活跃 23 人｜成功 127｜失败 38（23%，前一天 12%）｜完全卡死 6 人"
            in text)
    assert ("  V2 model_api：活跃 30 人｜成功 162｜失败 96（37%，前一天 5%）｜完全卡死 12 人"
            in text)
    # Cause groups with top codes; V2 dream skip count excludes the job that
    # finished on the previous Beijing day.
    assert ("失败原因：用户自己的账号/配置 32 次/6 人（capture_agent_call_failed:quota_insufficient 24、"
            "capture_agent_call_failed:resident_agent_cli_logged_out 8）；我们这边 3 次/3 人"
            "（capture_memory_actions_failed 3）；未知 3 次/3 人（runtime_failed 3）") in text
    assert "模型服务 18 次/6 人（extraction_failed:upstream_unavailable 12、extraction_failed:rate_limited 6）" in text
    assert "我们这边 48 次/8 人（extraction_failed:json_decode_error 24、lease_timeout 24）" in text
    assert "未知 6 次/6 人（no_code 6）" in text
    assert "｜成功里跳过 8 次（花园太小）" in text
    assert "此刻卡着没结束的任务：落卡 3 · 做梦 0" in text
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


def test_cause_breakdown_always_sums_to_failed():
    report = report_tool.build_report(_sources(), day=DAY)
    for stats in list(report.today.values()) + list(report.previous.values()):
        assert sum(c.attempts for c in stats.causes.values()) == stats.failed


# --------------------------------------------------------------------------- #
# Failure-code grouping
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("code,group", [
    # user's own account / configuration
    ("auth_invalid", "user_account"),
    ("extraction_failed:quota_insufficient", "user_account"),
    ("capture_agent_call_failed:model_not_found", "user_account"),
    ("dream_agent_call_failed:provider_account_expired", "user_account"),
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

def _stuck_rows(n, code="extraction_failed:auth_invalid"):
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
    assert not any("卡着没结束" in r for r in _report(stuck_total=19).attention)
    assert any("此刻有 20 个任务" in r for r in _report(stuck_total=20).attention)


def test_unfrozen_day_is_flagged_not_reported_as_healthy():
    live = [_row("usr_a", completed=1, failed=1, frozen=False)]
    report = _report(through=PREV, today_partial=live)
    text = report_tool.render_message(report)
    assert text.startswith("[需要关注]")
    assert "当天统计还没冻结" in text
    stats = report.today[("capture", "model_api")]
    assert stats.failed == 1 and stats.causes["unknown"].codes == {"no_code": 1}


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


def test_dream_skips_use_beijing_day_bounds():
    jobs = [
        {"status": "completed", "outcome": "skipped", "finished_at": "2026-09-13T15:59:59Z"},
        {"status": "completed", "outcome": "skipped", "finished_at": "2026-09-13T16:00:00Z"},
        {"status": "completed", "outcome": "skipped", "finished_at": "2026-09-14T15:59:59Z"},
        {"status": "completed", "outcome": "skipped", "finished_at": "2026-09-14T16:00:00Z"},
        {"status": "completed", "outcome": "", "finished_at": "2026-09-14T03:00:00Z"},
        {"status": "failed", "outcome": "skipped", "finished_at": "2026-09-14T03:00:00Z"},
    ]
    assert report_tool.count_dream_skips(jobs, DAY) == 2
    # Each edge on its own, so a UTC-day window cannot pass by coincidence.
    assert report_tool.count_dream_skips(jobs[1:2], DAY) == 1
    assert report_tool.count_dream_skips(jobs[3:4], DAY) == 0


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
        if url.path == "/v1/admin/memory-dream-jobs":
            assert query["status"] == "completed"
            return _Resp(json.dumps(self.sources["dream_jobs"]).encode())
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
