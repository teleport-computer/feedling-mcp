from __future__ import annotations

import urllib.parse

import pytest

from tools import prompt_cache_canary as canary


def _proof(
    *,
    retries: int = 0,
    cache_read: int = 1500,
    first_prompt_tokens: int = 1800,
    route: str = "route-opaque",
):
    return {
        "sampled_turns": 2,
        "model_calls": 2,
        "usage_telemetry_coverage": 1.0,
        "cache_telemetry_coverage": 1.0,
        "route_identity_coverage": 1.0,
        "route_fingerprint_count": 1,
        "route_fingerprint": route,
        "hit_ratio": 0.5,
        "turns": [
            {
                "job_id": 1,
                "failed": False,
                "model_calls": 1,
                "retries": 0,
                "prompt_tokens": first_prompt_tokens,
                "cache_read_tokens": 0,
            },
            {
                "job_id": 2,
                "failed": False,
                "model_calls": 1,
                "retries": retries,
                "prompt_tokens": first_prompt_tokens + 50,
                "cache_read_tokens": cache_read,
            },
        ],
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
    assert canary.validate_cache_proof(
        _proof(), expected_route="route-opaque") == "route-opaque"


@pytest.mark.parametrize(
    ("proof", "error"),
    [
        (_proof(cache_read=0), "zero cache-read"),
        (
            _proof(cache_read=1000, first_prompt_tokens=2000),
            "did not cover the synthetic conversation prefix",
        ),
        (_proof(retries=1), "hidden provider retry"),
        ({**_proof(), "route_fingerprint_count": 2}, "crossed provider routes"),
        ({**_proof(), "cache_telemetry_coverage": 0.5}, "not 100%"),
    ],
)
def test_validate_cache_proof_rejects_false_green(proof, error) -> None:
    with pytest.raises(canary.CanaryFailure, match=error):
        canary.validate_cache_proof(proof)


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
        "FEEDLING_PROMPT_CACHE_CANARY_MODEL", "openai/gpt-4o-mini")
    monkeypatch.setenv(
        "FEEDLING_PROMPT_CACHE_ANTHROPIC_CANARY_MODEL",
        "anthropic/claude-haiku-4.5",
    )

    assert [config.model for config in canary._env_configs()] == [
        "openai/gpt-4o-mini",
        "anthropic/claude-haiku-4.5",
    ]


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

    result = canary.run(_config(), request=request)

    assert result["second_turn_cache_read_tokens"] == 1500
    assert sends == 2
    assert sum(
        1 for method, url, _ in calls
        if method == "POST" and url.endswith("/v1/account/reset")
    ) == 1
    metrics_calls = [kwargs for method, url, kwargs in calls
                     if method == "GET" and "/v1/admin/v2-metrics" in url]
    assert len(metrics_calls) == 2
    assert all(call["admin_token"] == "admin-secret" for call in metrics_calls)


def test_failure_path_still_resets_account() -> None:
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
            return 200, {"prompt_cache": _proof(cache_read=0)}
        if path == "/v1/account/reset":
            reset.append(True)
            return 200, {}
        raise AssertionError(path)

    with pytest.raises(canary.CanaryFailure, match="zero cache-read"):
        canary.run(_config(), request=request)

    assert reset == [True]
