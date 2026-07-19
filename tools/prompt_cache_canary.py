#!/usr/bin/env python3
"""Pre deployment gate proving Runtime V2 performs a real prompt-cache read.

The canary uses only public user paths plus the read-only admin metrics endpoint:
register a throwaway model_api account, configure OpenRouter, complete two simple
chat turns with a long shared prefix, prove the second turn reports cached input
tokens with zero hidden provider retries, then delete the account.

Secrets are read from environment variables and are never printed. Synthetic
conversation text is deleted in ``finally``; ``v2_turn_metrics`` intentionally
has no user FK, so the non-content proof remains available after cleanup.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable


class CanaryFailure(RuntimeError):
    pass


class TransientCanaryFailure(CanaryFailure):
    """A temporary transport/service failure that a bounded poll may retry."""


@dataclass(frozen=True)
class Config:
    api_url: str
    admin_token: str
    provider_api_key: str
    provider: str = "openrouter"
    model: str = "openai/gpt-4.1-mini"
    timeout_sec: float = 360.0
    prefix_chars: int = 9000


RequestFn = Callable[..., tuple[int, dict[str, Any]]]
_MAX_UNCACHED_INPUT_TOKENS = 768
_FOLLOWUP_MESSAGES = (
    "Invoke no tools or functions. Return ordinary assistant text "
    "containing exactly CACHE_CANARY_TWO.",
    "Invoke no tools or functions. Return ordinary assistant text "
    "containing exactly CACHE_CANARY_THREE.",
    "Invoke no tools or functions. Return ordinary assistant text "
    "containing exactly CACHE_CANARY_FOUR.",
)
_EXPECTED_TURNS = 1 + len(_FOLLOWUP_MESSAGES)
_FOLLOWUP_SETTLE_SEC = 1.0


@dataclass(frozen=True)
class CacheProof:
    route: str
    first_prompt_tokens: int
    minimum_cache_read_tokens: int
    followup_cache_read_tokens: tuple[int | None, ...]
    qualifying_hit_turns: tuple[int, ...]

    @property
    def best_cache_read_tokens(self) -> int:
        return max(
            (value or 0 for value in self.followup_cache_read_tokens),
            default=0,
        )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep credentials bound to the exact origin selected by the canary."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_transport_url(url: str, *, require_pre_host: bool) -> None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if require_pre_host and host != "pre-api.feedling.app" and not _is_loopback(host):
        raise CanaryFailure(
            f"prompt-cache canary refuses non-Pre host: {host or '<empty>'}")
    if parsed.username or parsed.password or parsed.fragment:
        raise CanaryFailure("prompt-cache canary refuses credentialed or fragment URLs")
    if parsed.scheme not in {"http", "https"}:
        raise CanaryFailure("prompt-cache canary requires an HTTP(S) URL")
    if parsed.scheme != "https" and not _is_loopback(host):
        raise CanaryFailure("prompt-cache canary requires HTTPS outside loopback")


def _env_config() -> Config:
    return Config(
        api_url=os.environ.get(
            "FEEDLING_API_URL", "https://pre-api.feedling.app").rstrip("/"),
        admin_token=os.environ.get("FEEDLING_ADMIN_TOKEN", "").strip(),
        provider_api_key=os.environ.get("OPENROUTER_API_KEY", "").strip(),
        model=os.environ.get(
            "FEEDLING_PROMPT_CACHE_CANARY_MODEL", "openai/gpt-4.1-mini").strip(),
        timeout_sec=float(os.environ.get(
            "FEEDLING_PROMPT_CACHE_CANARY_TIMEOUT_SEC", "360")),
        prefix_chars=int(os.environ.get(
            "FEEDLING_PROMPT_CACHE_CANARY_PREFIX_CHARS", "9000")),
    )


def _env_configs() -> list[Config]:
    """Return the automatic-cache and explicit-boundary live probes."""
    primary = _env_config()
    anthropic_model = os.environ.get(
        "FEEDLING_PROMPT_CACHE_ANTHROPIC_CANARY_MODEL",
        "anthropic/claude-sonnet-4.6",
    ).strip()
    configs = [primary]
    if anthropic_model and anthropic_model != primary.model:
        configs.append(replace(primary, model=anthropic_model))
    return configs


def _validate_config(config: Config) -> None:
    _validate_transport_url(config.api_url, require_pre_host=True)
    if not config.admin_token:
        raise CanaryFailure("FEEDLING_ADMIN_TOKEN is required")
    if not config.provider_api_key:
        raise CanaryFailure("OPENROUTER_API_KEY is required")
    if config.provider != "openrouter":
        raise CanaryFailure("prompt-cache canary currently requires openrouter")
    if not config.model:
        raise CanaryFailure("FEEDLING_PROMPT_CACHE_CANARY_MODEL is required")
    if not 60 <= float(config.timeout_sec) <= 900:
        raise CanaryFailure("canary timeout must be between 60 and 900 seconds")
    if not 4096 <= int(config.prefix_chars) <= 10_500:
        raise CanaryFailure("canary prefix must be between 4096 and 10500 chars")


def _http(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    api_key: str = "",
    admin_token: str = "",
    timeout: float = 30.0,
) -> tuple[int, dict[str, Any]]:
    # Validate every request independently as defense in depth. The custom
    # opener below refuses redirects, so secrets cannot cross origins after
    # this exact URL has been accepted.
    _validate_transport_url(url, require_pre_host=False)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if api_key:
        headers["X-API-Key"] = api_key
    if admin_token:
        headers["X-Admin-Token"] = admin_token
    request = urllib.request.Request(
        url, data=data, headers=headers, method=method)
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
            raw = response.read()
            payload = json.loads(raw) if raw else {}
            return int(response.status), payload if isinstance(payload, dict) else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:  # noqa: BLE001 — diagnostic shape only
            payload = {}
        return int(exc.code), payload if isinstance(payload, dict) else {}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransientCanaryFailure(
            f"transport failure during {method}: {type(exc).__name__}") from exc


def _require_status(status: int, expected: int, step: str, body: dict) -> None:
    if status == expected:
        return
    error = str(body.get("error") or "unknown")[:80]
    raise CanaryFailure(f"{step} returned {status} ({error})")


def _long_first_prompt(prefix_chars: int, probe_nonce: str) -> str:
    instruction = (
        "Runtime prompt-cache deployment canary. Invoke no tools or functions. "
        "Return ordinary assistant text containing exactly CACHE_CANARY_ONE.\n"
        f"Unique probe nonce: {probe_nonce}\n"
        "The following neutral sequence is a stable cache prefix:\n"
    )
    repeated = ("stable-cache-prefix-token " * (prefix_chars // 26 + 1))
    return (instruction + repeated)[:prefix_chars]


def _send_until_accepted(
    config: Config,
    request: RequestFn,
    api_key: str,
    message: str,
) -> float:
    deadline = time.time() + min(90.0, config.timeout_sec / 2)
    client_msg_id = str(uuid.uuid4())
    last = "not attempted"
    while time.time() < deadline:
        status, body = request(
            "POST",
            f"{config.api_url}/v1/model_api/chat/send",
            body={"message": message, "client_msg_id": client_msg_id},
            api_key=api_key,
            timeout=45.0,
        )
        if status == 202:
            return float((body.get("user_message") or {}).get("ts") or time.time())
        last = f"status={status} error={str(body.get('error') or 'unknown')[:60]}"
        # These responses occur before message persistence. Reusing the same
        # client_msg_id also preserves idempotency if readiness changes mid-loop.
        if status == 503 and body.get("error") in {
            "hosting_runtime_unavailable",
            "runtime_policy_not_ready",
            "runtime_control_unavailable",
        }:
            time.sleep(5)
            continue
        raise CanaryFailure(f"chat send rejected: {last}")
    raise CanaryFailure(f"chat send readiness timeout: {last}")


def _wait_for_reply(
    config: Config,
    request: RequestFn,
    api_key: str,
    since_ts: float,
    user_id: str,
    metrics_since_ts: float,
) -> None:
    deadline = time.time() + config.timeout_sec
    query = urllib.parse.urlencode({"since": since_ts - 1, "limit": 50})
    polls = 0
    while time.time() < deadline:
        polls += 1
        status, body = request(
            "GET",
            f"{config.api_url}/v1/chat/history?{query}",
            api_key=api_key,
            timeout=30.0,
        )
        _require_status(status, 200, "chat history", body)
        for message in body.get("messages") or []:
            if (
                isinstance(message, dict)
                and message.get("role") in {"agent", "openclaw"}
                and float(message.get("ts") or 0) > since_ts
            ):
                return
        # A terminal provider/runtime failure does not create an assistant chat
        # row. Inspect the content-free whole-turn metric periodically so the
        # deployment gate fails promptly instead of waiting the full reply
        # timeout. Status is fixed-vocabulary/sanitized before printing.
        if polls == 1 or polls % 5 == 0:
            try:
                cache = _metrics(
                    config,
                    request,
                    user_id=user_id,
                    since_ts=metrics_since_ts,
                    until_ts=time.time() + 1.0,
                )
            except TransientCanaryFailure:
                # History is the primary success signal. A brief admin-metrics
                # outage must not reject an otherwise healthy deployment.
                cache = {}
            for turn in cache.get("turns") or []:
                if isinstance(turn, dict) and turn.get("failed") is True:
                    raw_status = str(turn.get("status") or "runtime_failed")
                    safe_status = "".join(
                        char for char in raw_status[:80]
                        if char.isalnum() or char in "_.:-"
                    ) or "runtime_failed"
                    raise CanaryFailure(
                        f"runtime turn failed before reply ({safe_status})")
        time.sleep(3)
    raise CanaryFailure("assistant reply did not arrive before timeout")


def _metrics(
    config: Config,
    request: RequestFn,
    *,
    user_id: str,
    since_ts: float,
    until_ts: float,
    route_fingerprint: str = "",
) -> dict[str, Any]:
    params = {
        "cache_provider": config.provider,
        "cache_model": config.model,
        "cache_user_id": user_id,
        "cache_since_ts": f"{since_ts:.6f}",
        "cache_until_ts": f"{until_ts:.6f}",
    }
    if route_fingerprint:
        params["cache_route_fingerprint"] = route_fingerprint
    status, body = request(
        "GET",
        f"{config.api_url}/v1/admin/v2-metrics?{urllib.parse.urlencode(params)}",
        admin_token=config.admin_token,
        timeout=30.0,
    )
    if status in {429, 500, 502, 503, 504}:
        raise TransientCanaryFailure(
            f"v2 metrics temporarily unavailable ({status})")
    _require_status(status, 200, "v2 metrics", body)
    cache = body.get("prompt_cache")
    if not isinstance(cache, dict):
        raise CanaryFailure("v2 metrics omitted prompt_cache")
    return cache


def _reset_account(
    config: Config,
    request: RequestFn,
    api_key: str,
) -> None:
    last = "not attempted"
    for attempt in range(1, 4):
        status, body = request(
            "POST",
            f"{config.api_url}/v1/account/reset",
            body={"confirm": "delete-all-data"},
            api_key=api_key,
            timeout=45.0,
        )
        if status == 200:
            print("[prompt-cache-canary] account reset -> 200")
            return
        last = f"status={status} error={str(body.get('error') or 'unknown')[:60]}"
        if status in {502, 503, 504} and attempt < 3:
            time.sleep(2 * attempt)
            continue
        break
    raise CanaryFailure(f"throwaway account cleanup failed: {last}")


def validate_cache_proof(
    cache: dict[str, Any],
    *,
    expected_route: str = "",
    expected_turns: int = _EXPECTED_TURNS,
) -> CacheProof:
    """Validate an isolated warm-up plus bounded follow-up cache evidence.

    Automatic provider caches are allowed to cold-miss or move to a fallback
    endpoint once.  Requiring the very first follow-up to hit made the deploy
    gate stochastic.  The probe now uses a per-run nonce and accepts only a
    *full-prefix* hit from one of several sequential follow-ups on the same
    account/session.  Every turn must still be a single logical provider call
    with complete usage/cache telemetry, zero hidden retries, and one Feedling
    route fingerprint.
    """
    expected_turns = int(expected_turns)
    if expected_turns < 2:
        raise ValueError("cache proof needs a warm-up and at least one follow-up")
    if int(cache.get("sampled_turns") or 0) != expected_turns:
        raise CanaryFailure(
            f"cache proof requires exactly {expected_turns} sampled turns")
    if int(cache.get("model_calls") or 0) != expected_turns:
        raise CanaryFailure(
            f"cache proof requires exactly {expected_turns} logical model calls")
    if float(cache.get("usage_telemetry_coverage") or 0.0) != 1.0:
        raise CanaryFailure("usage telemetry coverage is not 100%")
    if float(cache.get("cache_telemetry_coverage") or 0.0) != 1.0:
        raise CanaryFailure("cache telemetry coverage is not 100%")
    if float(cache.get("route_identity_coverage") or 0.0) != 1.0:
        raise CanaryFailure("cache route identity coverage is not 100%")
    if int(cache.get("route_fingerprint_count") or 0) != 1:
        raise CanaryFailure("cache proof crossed provider routes")
    route = str(cache.get("route_fingerprint") or "")
    if not route:
        raise CanaryFailure("cache proof omitted route fingerprint")
    if expected_route and route != expected_route:
        raise CanaryFailure("route-bound cache proof changed route fingerprint")
    turns = cache.get("turns")
    if not isinstance(turns, list) or len(turns) != expected_turns:
        raise CanaryFailure(
            f"cache proof requires {expected_turns} chronological turn rows")
    for index, turn in enumerate(turns, start=1):
        if not isinstance(turn, dict) or turn.get("failed") is not False:
            raise CanaryFailure(f"cache canary turn {index} failed")
        if int(turn.get("model_calls") or 0) != 1:
            raise CanaryFailure(f"cache canary turn {index} used multiple model calls")
        if "retries" not in turn:
            raise CanaryFailure(f"cache canary turn {index} omitted retry telemetry")
        if int(turn.get("retries") or 0) != 0:
            raise CanaryFailure(
                f"cache canary turn {index} had a hidden provider retry")
    first_prompt_tokens = int(turns[0].get("prompt_tokens") or 0)
    if first_prompt_tokens <= 0:
        raise CanaryFailure("first turn omitted prompt-token telemetry")
    minimum_cache_read = max(
        1024,
        first_prompt_tokens - _MAX_UNCACHED_INPUT_TOKENS,
    )

    followup_reads: list[int | None] = []
    for turn in turns[1:]:
        raw = turn.get("cache_read_tokens")
        if raw is None or isinstance(raw, bool):
            followup_reads.append(None)
            continue
        try:
            parsed = int(raw)
        except (TypeError, ValueError, OverflowError):
            followup_reads.append(None)
            continue
        followup_reads.append(max(0, parsed))

    if all(value is None for value in followup_reads):
        raise CanaryFailure(
            "all follow-up turns omitted cache-read token telemetry")
    best_cache_read = max((value or 0 for value in followup_reads), default=0)
    if best_cache_read <= 0:
        raise CanaryFailure(
            "all follow-up turns reported zero cache-read tokens")
    if best_cache_read < minimum_cache_read:
        raise CanaryFailure(
            "no follow-up cache read covered the synthetic conversation "
            f"prefix ({best_cache_read} < {minimum_cache_read})"
        )
    qualifying = tuple(
        index
        for index, value in enumerate(followup_reads, start=2)
        if value is not None and value >= minimum_cache_read
    )
    return CacheProof(
        route=route,
        first_prompt_tokens=first_prompt_tokens,
        minimum_cache_read_tokens=minimum_cache_read,
        followup_cache_read_tokens=tuple(followup_reads),
        qualifying_hit_turns=qualifying,
    )


def _proof_diagnostics(cache: dict[str, Any]) -> str:
    """Return content-free, secret-free evidence suitable for CI logs."""

    def _number(value: Any) -> int | float | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return value
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return None

    rows = []
    turns = cache.get("turns")
    for index, turn in enumerate(turns if isinstance(turns, list) else [], start=1):
        if not isinstance(turn, dict):
            rows.append({"turn": index, "malformed": True})
            continue
        raw_status = str(turn.get("status") or "")[:80]
        safe_status = "".join(
            char for char in raw_status if char.isalnum() or char in "_.:-"
        )
        rows.append({
            "turn": index,
            "prompt_tokens": _number(turn.get("prompt_tokens")),
            "cache_read_tokens": _number(turn.get("cache_read_tokens")),
            "cache_write_tokens": _number(turn.get("cache_write_tokens")),
            "cache_miss_tokens": _number(turn.get("cache_miss_tokens")),
            "usage_reported_calls": _number(turn.get("usage_reported_calls")),
            "cache_reported_calls": _number(turn.get("cache_reported_calls")),
            "model_calls": _number(turn.get("model_calls")),
            "retries": _number(turn.get("retries")),
            "failed": turn.get("failed") if isinstance(turn.get("failed"), bool) else None,
            "status": safe_status,
        })
    diagnostic = {
        "sampled_turns": _number(cache.get("sampled_turns")),
        "model_calls": _number(cache.get("model_calls")),
        "usage_telemetry_coverage": _number(
            cache.get("usage_telemetry_coverage")),
        "cache_telemetry_coverage": _number(
            cache.get("cache_telemetry_coverage")),
        "route_identity_coverage": _number(
            cache.get("route_identity_coverage")),
        "same_feedling_route": (
            int(cache.get("route_fingerprint_count") or 0) == 1
            and float(cache.get("route_identity_coverage") or 0.0) == 1.0
        ),
        "turns": rows,
    }
    return json.dumps(diagnostic, sort_keys=True, separators=(",", ":"))


def run(config: Config, *, request: RequestFn = _http) -> dict[str, Any]:
    _validate_config(config)
    api_key = ""
    try:
        # Any 32-byte value is a syntactically valid X25519 public key. The
        # canary never decrypts replies, and its account is deleted in finally.
        public_key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
        status, registration = request(
            "POST",
            f"{config.api_url}/v1/users/register",
            body={
                "public_key": public_key,
                "platform": "prompt-cache-canary",
                "access_mode": "model_api",
                "label": f"prompt-cache-canary-{os.environ.get('GITHUB_SHA', 'local')[:12]}",
            },
            timeout=30.0,
        )
        _require_status(status, 201, "register", registration)
        user_id = str(registration.get("user_id") or "")
        api_key = str(registration.get("api_key") or "")
        if not user_id or not api_key:
            raise CanaryFailure("register omitted throwaway credentials")
        print("[prompt-cache-canary] throwaway account registered")

        status, setup = request(
            "POST",
            f"{config.api_url}/v1/model_api/setup",
            body={
                "provider": config.provider,
                "model": config.model,
                "api_key": config.provider_api_key,
            },
            api_key=api_key,
            timeout=60.0,
        )
        _require_status(status, 200, "model_api setup", setup)
        test_status = str(
            (setup.get("config") or {}).get("test_status")
            or setup.get("test_status")
            or "")
        if test_status != "ok":
            raise CanaryFailure("model_api setup did not reach test_status=ok")

        # Put a fresh nonce near the front of the long user prefix.  It is never
        # printed, but remains verbatim in later history turns.  This prevents a
        # previous CI run from warming the same synthetic conversation and
        # producing a false green on this run's first request.
        probe_nonce = secrets.token_hex(16)
        window_start = time.time() - 1.0
        first_sent = _send_until_accepted(
            config,
            request,
            api_key,
            _long_first_prompt(config.prefix_chars, probe_nonce),
        )
        _wait_for_reply(
            config,
            request,
            api_key,
            first_sent,
            user_id,
            window_start,
        )
        for followup in _FOLLOWUP_MESSAGES:
            time.sleep(_FOLLOWUP_SETTLE_SEC)
            sent_at = _send_until_accepted(
                config,
                request,
                api_key,
                followup,
            )
            _wait_for_reply(
                config,
                request,
                api_key,
                sent_at,
                user_id,
                window_start,
            )

        # Metrics are flushed at the terminal turn boundary. History can become
        # visible a moment earlier, so poll the bounded proof briefly.
        proof_deadline = time.time() + 30.0
        last_cache: dict[str, Any] = {}
        while time.time() < proof_deadline:
            window_end = time.time() + 1.0
            try:
                last_cache = _metrics(
                    config,
                    request,
                    user_id=user_id,
                    since_ts=window_start,
                    until_ts=window_end,
                )
            except TransientCanaryFailure:
                time.sleep(2)
                continue
            if int(last_cache.get("sampled_turns") or 0) >= _EXPECTED_TURNS:
                break
            time.sleep(2)
        try:
            proof = validate_cache_proof(last_cache)
        except CanaryFailure:
            print(
                "[prompt-cache-canary] proof diagnostics: "
                + _proof_diagnostics(last_cache)
            )
            raise
        route_bound: dict[str, Any] | None = None
        route_deadline = time.time() + 30.0
        while time.time() < route_deadline:
            try:
                route_bound = _metrics(
                    config,
                    request,
                    user_id=user_id,
                    since_ts=window_start,
                    until_ts=time.time() + 1.0,
                    route_fingerprint=proof.route,
                )
                break
            except TransientCanaryFailure:
                time.sleep(2)
        if route_bound is None:
            raise CanaryFailure(
                "route-bound v2 metrics unavailable before proof timeout")
        try:
            route_proof = validate_cache_proof(
                route_bound,
                expected_route=proof.route,
            )
        except CanaryFailure:
            print(
                "[prompt-cache-canary] route-bound proof diagnostics: "
                + _proof_diagnostics(route_bound)
            )
            raise
        measurement_turns = len(route_proof.followup_cache_read_tokens)
        hit_turns = len(route_proof.qualifying_hit_turns)
        result = {
            "model": config.model,
            "route_fingerprint": route_proof.route,
            "first_turn_prompt_tokens": route_proof.first_prompt_tokens,
            "measurement_turns": measurement_turns,
            "qualifying_cache_hit_turns": hit_turns,
            "cache_hit_rate": hit_turns / measurement_turns,
            "best_cache_read_tokens": route_proof.best_cache_read_tokens,
            "hit_ratio": route_bound.get("hit_ratio"),
        }
        print(
            "[prompt-cache-canary] CACHE HIT PROVEN: "
            f"model={config.model} "
            f"qualifying_followup_hits={hit_turns}/{measurement_turns} "
            f"best_cache_read_tokens={route_proof.best_cache_read_tokens} "
            f"token_hit_ratio={result['hit_ratio']}"
        )
        return result
    finally:
        if api_key:
            primary = sys.exc_info()[1]
            try:
                _reset_account(config, request, api_key)
            except CanaryFailure as cleanup_error:
                if primary is not None:
                    raise CanaryFailure(
                        f"{primary}; additionally {cleanup_error}") from primary
                raise


def main() -> int:
    failures: list[str] = []
    for config in _env_configs():
        print(f"[prompt-cache-canary] probing model={config.model}")
        try:
            run(config)
        except CanaryFailure as exc:
            failures.append(f"{config.model}: {exc}")
            print(
                f"[prompt-cache-canary] MODEL FAIL: model={config.model} "
                f"reason={exc}",
                file=sys.stderr,
            )
    if failures:
        print(
            "PROMPT CACHE CANARY FAIL: " + "; ".join(failures),
            file=sys.stderr,
        )
        return 1
    print("PROMPT CACHE CANARY OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
