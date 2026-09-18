"""Daily memory-pipeline Lark report (tools/memory_pipeline_daily_report.py).

Pure unit tests: no database, no network. Every HTTP call goes through an
injected fake opener, so nothing here can reach the admin API or post a real
Lark message.

The summary fixture is pinned to the raw lane-rollup fixture by
``tests/test_admin_lane_rollup_summary.py``. ``tests/test_lane_rollup.py``
locks the raw fixture to the real DB-backed producer, so it cannot silently
drift from the endpoint. Aggregation tests moved with the backend code;
this file keeps report policy, presentation and delivery contracts.
"""
from __future__ import annotations

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

from tests.test_admin_lane_rollup_summary import _row, _report
from tools import memory_pipeline_daily_report as report_tool  # noqa: E402
from tools.strict_yaml import load_yaml_strict  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "memory_pipeline_daily_report" / "summary_2026-09-14.json"
WORKFLOW = ROOT / ".github" / "workflows" / "memory-pipeline-daily.yml"
DAY = "2026-09-14"
PREV = "2026-09-13"


def _sources() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Attention thresholds
# --------------------------------------------------------------------------- #

def _stuck_rows(n, code="lease_timeout"):
    return [_row(f"usr_{i:016x}", failed=2, codes={code: 2}) for i in range(n)]


def test_stuck_users_threshold_boundary():
    below = _report(_stuck_rows(report_tool.STUCK_USERS_ATTENTION - 1))
    at = _report(_stuck_rows(report_tool.STUCK_USERS_ATTENTION))
    assert not any("完全卡死" in r for r in report_tool.evaluate_attention(below))
    assert any("完全卡死 10 人" in r for r in report_tool.evaluate_attention(at))


def test_our_side_users_threshold_boundary():
    healthy = [_row(f"usr_ok{i:013x}", completed=50) for i in range(10)]
    def rows(n):
        return healthy + [_row(f"usr_{i:016x}", completed=1, failed=1,
                               codes={"lease_timeout": 1}) for i in range(n)]
    assert not any("我们这边" in r for r in report_tool.evaluate_attention(_report(rows(4))))
    assert any("我们这边的故障影响 5 人" in r for r in report_tool.evaluate_attention(_report(rows(5))))


def test_failure_rate_jump_boundary_and_minimum_attempts():
    def rows(fail_today, *, attempts=40):
        prev = [_row("usr_p", day=PREV, completed=36, failed=4,
                     codes={"extraction_failed:rate_limited": 4})]  # 10%
        today = [_row("usr_t", completed=attempts - fail_today, failed=fail_today,
                      codes={"extraction_failed:rate_limited": fail_today})]
        return prev + today
    no_jump = _report(rows(9, attempts=40))   # 22.5% → +12.5pp
    jump = _report(rows(10, attempts=40))     # 25% → +15pp
    assert not any("失败率" in r for r in report_tool.evaluate_attention(no_jump))
    assert any("失败率 25%，前一天 10%" in r for r in report_tool.evaluate_attention(jump))
    # Same jump on too few attempts stays quiet.
    few = [_row("usr_p", day=PREV, completed=9, failed=1, codes={"lease_timeout": 1}),
           _row("usr_t", completed=6, failed=4, codes={"lease_timeout": 4})]
    assert not any("失败率" in r for r in report_tool.evaluate_attention(_report(few)))


def test_high_failure_rate_fires_without_previous_day():
    rows = [_row("usr_t", completed=10, failed=10,
                 codes={"extraction_failed:upstream_unavailable": 10})]
    assert any("失败率 50%" in r for r in report_tool.evaluate_attention(_report(rows)))
    rows = [_row("usr_t", completed=11, failed=9,
                 codes={"extraction_failed:upstream_unavailable": 9})]
    assert not any("失败率" in r for r in report_tool.evaluate_attention(_report(rows)))
    # A 60% day on 5 attempts is noise, not an incident.
    rows = [_row("usr_t", completed=2, failed=3,
                 codes={"extraction_failed:upstream_unavailable": 3})]
    assert not any("失败率" in r for r in report_tool.evaluate_attention(_report(rows)))


def test_silent_stop_is_detected_by_active_user_drop():
    prev = [_row(f"usr_{i:016x}", day=PREV, completed=3) for i in range(12)]
    kept = [_row(f"usr_{i:016x}", completed=3) for i in range(6)]
    dropped = [_row(f"usr_{i:016x}", completed=3) for i in range(5)]
    assert not any("活跃用户" in r for r in report_tool.evaluate_attention(_report(prev + kept)))
    fired = report_tool.evaluate_attention(_report(prev + dropped))
    assert any("活跃用户 5 人，前一天 12 人" in r for r in fired)


def test_live_stuck_jobs_threshold():
    def stuck(n):
        return _report(stuck_total=n, stuck_rows=[{"route": "model_api", "count": n}])
    assert not any("卡着没结束" in r for r in report_tool.evaluate_attention(stuck(19)))
    assert any("此刻有 20 个任务" in r for r in report_tool.evaluate_attention(stuck(20)))


def test_quiet_healthy_day_is_marked_normal():
    rows = [_row(f"usr_{i:016x}", completed=5) for i in range(3)]
    text = report_tool.render_message(_report(rows))
    assert text.splitlines()[0].startswith("[正常]")
    assert "V1 resident：当天没有任务" in text


def test_each_route_line_shows_affected_and_zero_success_users():
    rows = [_row("usr_a", failed=4, codes={"lease_timeout": 4}),
            _row("usr_b", failed=1, completed=2, codes={"lease_timeout": 1}),
            _row("usr_c", completed=3)]
    text = report_tool.render_message(_report(rows))
    line = next(l for l in text.splitlines() if l.strip().startswith("V2 model_api：活跃"))
    assert "失败 5（" in line
    assert "受影响 2 人（前一天 0）｜零成功 1 人（前一天 0）" in line


def test_rate_caveat_is_printed_once_on_every_message():
    # Seven 2026-09-18: keep the per-attempt rate, but say on the message
    # itself that retries inflate it (dream retries up to 4x since 09-16).
    quiet = report_tool.render_message(_report([_row("usr_a", completed=5)]))
    busy = report_tool.render_message(_report(_stuck_rows(3)))
    for text in (quiet, busy):
        assert text.count(report_tool.RATE_CAVEAT) == 1
    assert "2026-09-16" in report_tool.RATE_CAVEAT
    assert "受影响" in report_tool.RATE_CAVEAT and "零成功" in report_tool.RATE_CAVEAT


def test_backend_without_failed_users_renders_dash_not_zero():
    # The tool reads whatever prod serves; an older backend must not read as
    # "nobody affected". stuck_users predates this change and stays numeric.
    report = _report(_stuck_rows(2))
    for cell in report["cells"].values():
        for side in ("day", "previous"):
            cell[side].pop("failed_users")
    text = report_tool.render_message(report)
    line = next(l for l in text.splitlines() if l.strip().startswith("V2 model_api：活跃"))
    assert "受影响 - 人（前一天 -）｜零成功 2 人（前一天 0）" in line


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
        if url.path == "/v1/admin/lane-rollup/summary":
            return _Resp(json.dumps(self.sources).encode())
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
    assert fake.admin_calls == [("/v1/admin/lane-rollup/summary", {"day": DAY})]
    (post,) = fake.posts
    assert post["sign"] == report_tool.lark_sign(post["timestamp"], "lark-secret")
    expected = report_tool.render_message(_sources())
    assert post["content"]["text"] == expected
    assert "admin-secret" not in json.dumps(post)


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
    assert "/v1/admin/lane-rollup/summary HTTP 503" in text
    assert "body with secrets" not in text and "admin-secret" not in text


@pytest.mark.parametrize("stage", ["build", "fetch"])
def test_unexpected_error_still_posts_a_content_free_notice(monkeypatch, stage):
    """Review 09-15: only FetchError was caught, so a response shape change
    crashed the run with nothing posted."""
    posts = []

    def boom(*_args, **_kwargs):
        raise TypeError("usr_leaky_secret_value")

    if stage == "build":
        monkeypatch.setattr(report_tool, "render_message", boom)
        argv, environ = ["--fixture", str(FIXTURE), "--day", DAY], {}
    else:
        monkeypatch.setattr(report_tool, "fetch_summary", boom)
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
    assert "生成报告出错：KeyError" in posts[0]["content"]["text"]


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
        assert any("完全卡死 3 人" in r for r in report_tool.evaluate_attention(_report(_stuck_rows(3))))

        # nan compares False with everything (the rule would silently never
        # fire) and inf is just as unreachable: both keep the default.
        monkeypatch.setenv("MEMORY_REPORT_FAILURE_RATE", "nan")
        monkeypatch.setenv("MEMORY_REPORT_FAILURE_RATE_JUMP_PP", "inf")
        monkeypatch.setenv("MEMORY_REPORT_ACTIVE_DROP_RATIO", "-inf")
        importlib.reload(report_tool)
        assert report_tool.FAILURE_RATE_ATTENTION == 0.5
        assert report_tool.FAILURE_RATE_JUMP_PP == 15.0
        assert report_tool.ACTIVE_DROP_RATIO == 0.5
    finally:
        for name in report_tool.THRESHOLD_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        importlib.reload(report_tool)


def test_standalone_script_needs_only_stdlib_and_defaults_the_day(tmp_path):
    # No repository on sys.path and site-packages disabled, as in Actions.
    import shutil
    script = tmp_path / 'report.py'
    shutil.copyfile(ROOT / 'tools/memory_pipeline_daily_report.py', script)
    result = subprocess.run([sys.executable, '-I', '-S', str(script),
        '--fixture', str(FIXTURE), '--dry-run'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip('\n') == FIXTURE.with_name('expected_message_2026-09-14.txt').read_text()
