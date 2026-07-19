from __future__ import annotations

import urllib.parse

import pytest

from tools import prompt_cache_canary as canary


def _proof(
    *,
    followup_reads: tuple[int | None, ...] = (0, 1500, 1600),
    retry_turn: int | None = None,
    first_prompt_tokens: int = 1800,
    route: str = "route-opaque",
):
    turns = [{
        "job_id": 1,
        "failed": False,
        "model_calls": 1,
        "retries": 0,
        "usage_reported_calls": 1,
        "cache_reported_calls": 1,
        "prompt_tokens": first_prompt_tokens,
        "cache_read_tokens": 0,
        "cache_write_tokens": None,
        "cache_miss_tokens": first_prompt_tokens,
        "status": "ok",
    }]
    for index, cache_read in enumerate(followup_reads, start=2):
        turns.append({
            "job_id": index,
            "failed": False,
            "model_calls": 1,
            "retries": 1 if retry_turn == index else 0,
            "usage_reported_calls": 1,
            "cache_reported_calls": 1,
            "prompt_tokens": first_prompt_tokens + 50 * (index - 1),
            "cache_read_tokens": cache_read,
            "cache_write_tokens": None,
            "cache_miss_tokens": (
                None if cache_read is None else first_prompt_tokens - cache_read
            ),
            "status": "ok",
        })
    return {
        "sampled_turns": len(turns),
        "model_calls": len(turns),
        "usage_telemetry_coverage": 1.0,
        "cache_telemetry_coverage": 1.0,
        "route_identity_coverage": 1.0,
        "route_fingerprint_count": 1,
        "route_fingerprint": route,
        "hit_ratio": 0.5,
        "turns": turns,
    }


def _config() -> canary.Config:
    return canary.Config(
        api_url="https://pre-api.feedling.app",
        admin_token="admin-secret",
        provider_api_key="provider-secret",
        timeout_sec=60,
        prefix_chars=4096,
    )


def test_validate_cache_proof_accepts_exact_route_bound_hit() -> None:
    proof = canary.validate_cache_proof(
        _proof(), expected_route="route-opaque")

    assert proof.route == "route-opaque"
    assert proof.qualifying_hit_turns == (3, 4)
    assert proof.best_cache_read_tokens == 1600


@pytest.mark.parametrize(
    ("proof", "error"),
    [
        (
            _proof(followup_reads=(0, 0, 0)),
            "all follow-up turns reported zero cache-read",
        ),
        (
            _proof(followup_reads=(None, None, None)),
            "all follow-up turns omitted cache-read",
        ),
        (
            _proof(
                followup_reads=(0, 1000, 900),
                first_prompt_tokens=2000,
            ),
            "no follow-up cache read covered the synthetic conversation prefix",
        ),
        (_proof(retry_turn=3), "hidden provider retry"),
        ({**_proof(), "route_fingerprint_count": 2}, "crossed provider routes"),
        ({**_proof(), "cache_telemetry_coverage": 0.5}, "not 100%"),
    ],
)
def test_validate_cache_proof_rejects_false_green(proof, error) -> None:
    with pytest.raises(canary.CanaryFailure, match=error):
        canary.validate_cache_proof(proof)


def test_probe_nonce_prevents_cross_run_warm_cache_false_green() -> None:
    first = canary._long_first_prompt(4096, "nonce-a")
    repeated = canary._long_first_prompt(4096, "nonce-a")
    other_run = canary._long_first_prompt(4096, "nonce-b")

    assert first == repeated
    assert first != other_run
    assert "nonce-a" in first and "nonce-b" in other_run
    assert len(first) == len(other_run) == 4096


def test_canary_refuses_production_host() -> None:
    config = canary.Config(
        api_url="https://api.feedling.app",
        admin_token="admin",
        provider_api_key="provider",
    )
    with pytest.raises(canary.CanaryFailure, match="refuses non-Pre host"):
        canary.run(config, request=lambda *_args, **_kwargs: (500, {}))


def test_canary_refuses_plaintext_pre_transport() -> None:
    config = canary.Config(
        api_url="http://pre-api.feedling.app",
        admin_token="admin",
        provider_api_key="provider",
    )
    with pytest.raises(canary.CanaryFailure, match="requires HTTPS"):
        canary.run(config, request=lambda *_args, **_kwargs: (500, {}))


def test_http_refuses_plaintext_before_sending_credentials() -> None:
    with pytest.raises(canary.CanaryFailure, match="requires HTTPS"):
        canary._http(
            "GET",
            "http://pre-api.feedling.app/v1/admin/v2-metrics",
            admin_token="admin-secret",
        )


def test_redirect_handler_never_forwards_request() -> None:
    request = canary.urllib.request.Request(
        "https://pre-api.feedling.app/v1/admin/v2-metrics",
        headers={"X-Admin-Token": "admin-secret"},
    )
    assert canary._NoRedirectHandler().redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://example.test/steal",
    ) is None


def test_loopback_http_remains_available_for_local_canary() -> None:
    canary._validate_config(canary.Config(
        api_url="http://127.0.0.1:8000",
        admin_token="admin",
        provider_api_key="provider",
    ))


def test_env_configs_probe_automatic_and_anthropic_cache_paths(monkeypatch) -> None:
    monkeypatch.setenv("FEEDLING_ADMIN_TOKEN", "admin")
    monkeypatch.setenv("OPENROUTER_API_KEY", "provider")
    monkeypatch.setenv(
        "FEEDLING_PROMPT_CACHE_CANARY_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.setenv(
        "FEEDLING_PROMPT_CACHE_ANTHROPIC_CANARY_MODEL",
        "anthropic/claude-sonnet-4.6",
    )

    assert [config.model for config in canary._env_configs()] == [
        "openai/gpt-4.1-mini",
        "anthropic/claude-sonnet-4.6",
    ]


def test_wait_for_reply_fails_fast_on_terminal_runtime_metric() -> None:
    metrics_calls = 0

    def request(method, url, **kwargs):
        nonlocal metrics_calls
        path = urllib.parse.urlparse(url).path
        if path == "/v1/chat/history":
            return 200, {"messages": []}
        if path == "/v1/admin/v2-metrics":
            metrics_calls += 1
            proof = _proof()
            proof["sampled_turns"] = 1
            proof["model_calls"] = 1
            proof["turns"] = [{
                "job_id": 1,
                "failed": True,
                "status": "turn_failed:providererror",
            }]
            return 200, {"prompt_cache": proof}
        raise AssertionError(f"unexpected request: {method} {url}")

    with pytest.raises(
        canary.CanaryFailure,
        match=r"runtime turn failed before reply \(turn_failed:providererror\)",
    ):
        canary._wait_for_reply(
            _config(),
            request,
            "user-secret",
            10.0,
            "u-canary",
            9.0,
        )

    assert metrics_calls == 1


def test_wait_for_reply_ignores_transient_metrics_outage(monkeypatch) -> None:
    history_calls = 0
    metrics_calls = 0

    def request(method, url, **kwargs):
        nonlocal history_calls, metrics_calls
        path = urllib.parse.urlparse(url).path
        if path == "/v1/chat/history":
            history_calls += 1
            if history_calls == 1:
                return 200, {"messages": []}
            return 200, {"messages": [{"role": "agent", "ts": 12.0}]}
        if path == "/v1/admin/v2-metrics":
            metrics_calls += 1
            return 503, {"error": "temporarily unavailable"}
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(canary.time, "sleep", lambda _seconds: None)

    canary._wait_for_reply(
        _config(),
        request,
        "user-secret",
        10.0,
        "u-canary",
        9.0,
    )

    assert history_calls == 2
    assert metrics_calls == 1


def test_end_to_end_orchestration_resets_throwaway_account(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []
    sends = 0

    def request(method, url, **kwargs):
        nonlocal sends
        calls.append((method, url, kwargs))
        path = urllib.parse.urlparse(url).path
        if path == "/v1/users/register":
            return 201, {"user_id": "u-canary", "api_key": "user-secret"}
        if path == "/v1/model_api/setup":
            assert kwargs["body"]["api_key"] == "provider-secret"
            return 200, {"config": {"test_status": "ok"}}
        if path == "/v1/model_api/chat/send":
            sends += 1
            return 202, {"user_message": {"ts": float(sends * 10)}}
        if path == "/v1/chat/history":
            since = float(urllib.parse.parse_qs(
                urllib.parse.urlparse(url).query)["since"][0])
            return 200, {"messages": [{
                "role": "agent",
                "ts": since + 2,
            }]}
        if path == "/v1/admin/v2-metrics":
            return 200, {"prompt_cache": _proof()}
        if path == "/v1/account/reset":
            return 200, {"status": "deleted"}
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(canary, "_FOLLOWUP_SETTLE_SEC", 0)

    result = canary.run(_config(), request=request)

    assert result["best_cache_read_tokens"] == 1600
    assert result["measurement_turns"] == 3
    assert result["qualifying_cache_hit_turns"] == 2
    assert result["cache_hit_rate"] == pytest.approx(2 / 3)
    assert sends == 4
    assert sum(
        1 for method, url, _ in calls
        if method == "POST" and url.endswith("/v1/account/reset")
    ) == 1
    metrics_calls = [kwargs for method, url, kwargs in calls
                     if method == "GET" and "/v1/admin/v2-metrics" in url]
    assert len(metrics_calls) == 2
    assert all(call["admin_token"] == "admin-secret" for call in metrics_calls)


def test_failure_path_still_resets_account(monkeypatch) -> None:
    reset = []
    sends = 0

    def request(method, url, **kwargs):
        nonlocal sends
        path = urllib.parse.urlparse(url).path
        if path == "/v1/users/register":
            return 201, {"user_id": "u-canary", "api_key": "user-secret"}
        if path == "/v1/model_api/setup":
            return 200, {"config": {"test_status": "ok"}}
        if path == "/v1/model_api/chat/send":
            sends += 1
            return 202, {"user_message": {"ts": float(sends * 10)}}
        if path == "/v1/chat/history":
            return 200, {"messages": [{"role": "agent", "ts": 999.0}]}
        if path == "/v1/admin/v2-metrics":
            return 200, {
                "prompt_cache": _proof(followup_reads=(0, 0, 0))}
        if path == "/v1/account/reset":
            reset.append(True)
            return 200, {}
        raise AssertionError(path)

    monkeypatch.setattr(canary, "_FOLLOWUP_SETTLE_SEC", 0)
    with pytest.raises(
        canary.CanaryFailure,
        match="all follow-up turns reported zero cache-read",
    ):
        canary.run(_config(), request=request)

    assert reset == [True]


def test_main_probes_every_model_before_reporting_failures(monkeypatch, capsys) -> None:
    first = canary.Config(
        api_url="https://pre-api.feedling.app",
        admin_token="admin",
        provider_api_key="provider",
        model="openai/gpt-4.1-mini",
    )
    second = canary.replace(first, model="anthropic/claude-sonnet-4.6")
    seen = []

    def fake_run(config):
        seen.append(config.model)
        if config is first:
            raise canary.CanaryFailure("cold miss")
        return {"model": config.model}

    monkeypatch.setattr(canary, "_env_configs", lambda: [first, second])
    monkeypatch.setattr(canary, "run", fake_run)

    assert canary.main() == 1
    assert seen == [first.model, second.model]
    captured = capsys.readouterr()
    assert first.model in captured.err
    assert "cold miss" in captured.err


def test_failure_diagnostics_preserve_null_without_exposing_route_or_jobs() -> None:
    proof = _proof(followup_reads=(None, 0, 1600))

    diagnostic = canary._proof_diagnostics(proof)

    assert '"cache_read_tokens":null' in diagnostic
    assert '"same_feedling_route":true' in diagnostic
    assert "route-opaque" not in diagnostic
    assert '"job_id"' not in diagnostic
