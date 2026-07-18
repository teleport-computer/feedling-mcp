from __future__ import annotations

import urllib.parse

import pytest

from tools import prompt_cache_canary as canary


def _proof(*, retries: int = 0, cache_read: int = 800, route: str = "route-opaque"):
    return {
        "sampled_turns": 2,
        "model_calls": 2,
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
                "cache_read_tokens": 0,
            },
            {
                "job_id": 2,
                "failed": False,
                "model_calls": 1,
                "retries": retries,
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

    assert result["second_turn_cache_read_tokens"] == 800
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
