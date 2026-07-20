"""Hosted-runtime V2 prompt-cache affinity key safety tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import serve_worker
from provider_client import ProviderConfig


_CACHE_KEY_DOMAIN = b"feedling:v2:prompt-cache:v3\0"
_ROUTE_DOMAIN = b"feedling:v2:prompt-cache-route:v1\0"
_CREDENTIAL_DOMAIN = b"feedling:v2:prompt-cache-credential:v1\0"


def _install_provider_config(
    monkeypatch,
    *,
    provider: str = "openai",
    model: str = "gpt-test",
    api_key: str = "provider-secret",
    base_url: str = "https://provider.invalid/v1",
) -> tuple[ProviderConfig, dict[str, str]]:
    """Keep enclave/config-store mechanics out of these key-derivation tests."""
    config = ProviderConfig(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
    )
    store = {"sentinel": "unchanged"}
    monkeypatch.setattr(serve_worker.core_store, "get_store", lambda _user_id: store)
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _user_id: "runtime-token")
    monkeypatch.setattr(
        serve_worker.hosted_config_store,
        "_load_runtime_provider_config",
        lambda resolved_store, api_key, *, runtime_token: (
            config
            if resolved_store is store and api_key is None and runtime_token == "runtime-token"
            else (_ for _ in ()).throw(AssertionError("unexpected provider-config lookup"))
        ),
    )
    return config, store


def _expected_cache_key(
    secret: str,
    user_id: str,
    *,
    provider: str = "openai",
    model: str = "gpt-test",
    destination: str = "https://provider.invalid/v1",
    api_key: str = "provider-secret",
) -> str:
    scope = _expected_scope(
        secret=secret,
        provider=provider,
        model=model,
        destination=destination,
        api_key=api_key,
    )
    digest = hmac.new(
        secret.encode("utf-8"),
        _CACHE_KEY_DOMAIN
        + scope
        + b"\0"
        + user_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"feedling-v2-{digest}"


def _expected_scope(
    *, secret: str, provider: str, model: str, destination: str, api_key: str,
) -> bytes:
    parsed = urlsplit(destination)
    port = parsed.port
    if (parsed.scheme.lower(), port) in {("https", 443), ("http", 80)}:
        port = None
    return json.dumps(
        [
            provider.lower(),
            model.lower(),
            parsed.scheme.lower(),
            (parsed.hostname or "").lower(),
            port,
            parsed.path.rstrip("/"),
            hmac.new(
                secret.encode("utf-8"),
                _CREDENTIAL_DOMAIN + api_key.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest(),
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _expected_route_fingerprint(
    secret: str,
    *,
    provider: str = "openai",
    model: str = "gpt-test",
    destination: str = "https://provider.invalid/v1",
    api_key: str = "provider-secret",
) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        _ROUTE_DOMAIN
        + _expected_scope(
            secret=secret,
            provider=provider,
            model=model,
            destination=destination,
            api_key=api_key,
        ),
        hashlib.sha256,
    ).hexdigest()
    return f"feedling-v2-route-{digest}"


def test_prompt_cache_key_is_deterministic_opaque_and_non_mutating(monkeypatch):
    original_config, store = _install_provider_config(monkeypatch)
    secret = "runtime-secret-alpha"
    user_id = "user-private-123"
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", secret)

    first, first_meta = serve_worker._resolve_provider(user_id)
    second, second_meta = serve_worker._resolve_provider(user_id)

    assert first_meta == second_meta == {}
    assert first is not None and second is not None
    assert first.prompt_cache_key == second.prompt_cache_key
    assert first.prompt_cache_key == _expected_cache_key(secret, user_id)
    assert first.prompt_cache_route_fingerprint == _expected_route_fingerprint(secret)
    assert user_id not in first.prompt_cache_key

    # Affinity is attached to a replacement value only. Nothing containing the
    # user id or derived key is written back into the user's stored config.
    assert original_config.prompt_cache_key == ""
    assert original_config.prompt_cache_route_fingerprint == ""
    assert original_config.capture_attempt_trace is False
    assert first.capture_attempt_trace is True
    assert store == {"sentinel": "unchanged"}
    assert user_id not in repr(original_config)
    assert first is not original_config


def test_prompt_cache_key_is_distinct_for_different_users(monkeypatch):
    _install_provider_config(monkeypatch)
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", "runtime-secret-alpha")

    first, _ = serve_worker._resolve_provider("user-one")
    second, _ = serve_worker._resolve_provider("user-two")

    assert first is not None and second is not None
    assert first.prompt_cache_key != second.prompt_cache_key
    assert first.prompt_cache_route_fingerprint == second.prompt_cache_route_fingerprint
    assert "user-one" not in first.prompt_cache_key
    assert "user-two" not in second.prompt_cache_key


def test_prompt_cache_key_rotates_with_runtime_secret(monkeypatch):
    _install_provider_config(monkeypatch)
    user_id = "user-stable"

    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", "runtime-secret-alpha")
    before_rotation, _ = serve_worker._resolve_provider(user_id)

    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", "runtime-secret-beta")
    after_rotation, _ = serve_worker._resolve_provider(user_id)

    assert before_rotation is not None and after_rotation is not None
    assert before_rotation.prompt_cache_key == _expected_cache_key(
        "runtime-secret-alpha", user_id
    )
    assert after_rotation.prompt_cache_key == _expected_cache_key(
        "runtime-secret-beta", user_id
    )
    assert before_rotation.prompt_cache_key != after_rotation.prompt_cache_key
    assert (
        before_rotation.prompt_cache_route_fingerprint
        != after_rotation.prompt_cache_route_fingerprint
    )


def test_prompt_cache_key_is_unlinkable_across_provider_destinations(monkeypatch):
    user_id = "same-private-user"
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", "runtime-secret-alpha")

    _install_provider_config(
        monkeypatch, provider="openai", base_url="https://relay-a.invalid/v1/")
    relay_a, _ = serve_worker._resolve_provider(user_id)
    # Same normalized route (trailing slash/default port only) stays stable.
    _install_provider_config(
        monkeypatch, provider="openai", base_url="https://relay-a.invalid:443/v1")
    relay_a_equivalent, _ = serve_worker._resolve_provider(user_id)
    _install_provider_config(
        monkeypatch, provider="openai", base_url="https://relay-b.invalid/v1")
    relay_b, _ = serve_worker._resolve_provider(user_id)
    _install_provider_config(
        monkeypatch, provider="openrouter", base_url="https://relay-a.invalid/v1")
    other_provider, _ = serve_worker._resolve_provider(user_id)

    assert relay_a is not None and relay_a_equivalent is not None
    assert relay_b is not None and other_provider is not None
    assert relay_a.prompt_cache_key == relay_a_equivalent.prompt_cache_key
    assert relay_a.prompt_cache_key != relay_b.prompt_cache_key
    assert relay_a.prompt_cache_key != other_provider.prompt_cache_key
    assert (
        relay_a.prompt_cache_route_fingerprint
        == relay_a_equivalent.prompt_cache_route_fingerprint
    )
    assert (
        relay_a.prompt_cache_route_fingerprint
        != relay_b.prompt_cache_route_fingerprint
    )


def test_prompt_cache_key_uses_provider_default_destination(monkeypatch):
    user_id = "same-private-user"
    secret = "runtime-secret-alpha"
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", secret)
    _install_provider_config(monkeypatch, provider="openai", base_url="")

    resolved, _ = serve_worker._resolve_provider(user_id)

    assert resolved is not None
    assert resolved.prompt_cache_key == _expected_cache_key(
        secret,
        user_id,
        provider="openai",
        destination="https://api.openai.com/v1",
    )


def test_prompt_cache_key_is_unlinkable_across_models_on_same_route(monkeypatch):
    user_id = "same-private-user"
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", "runtime-secret-alpha")
    _install_provider_config(
        monkeypatch,
        provider="openrouter",
        model="openai/gpt-5",
        base_url="https://openrouter.ai/api/v1",
    )
    openai_model, _ = serve_worker._resolve_provider(user_id)
    _install_provider_config(
        monkeypatch,
        provider="openrouter",
        model="anthropic/claude-sonnet-4",
        base_url="https://openrouter.ai/api/v1",
    )
    anthropic_model, _ = serve_worker._resolve_provider(user_id)

    assert openai_model is not None and anthropic_model is not None
    assert openai_model.prompt_cache_key != anthropic_model.prompt_cache_key
    assert (
        openai_model.prompt_cache_route_fingerprint
        != anthropic_model.prompt_cache_route_fingerprint
    )


def test_prompt_cache_key_is_unlinkable_across_provider_credentials(monkeypatch):
    user_id = "same-private-user"
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", "runtime-secret-alpha")
    _install_provider_config(monkeypatch, api_key="provider-account-a")
    account_a, _ = serve_worker._resolve_provider(user_id)
    _install_provider_config(monkeypatch, api_key="provider-account-b")
    account_b, _ = serve_worker._resolve_provider(user_id)

    assert account_a is not None and account_b is not None
    assert account_a.prompt_cache_key != account_b.prompt_cache_key
    assert (
        account_a.prompt_cache_route_fingerprint
        != account_b.prompt_cache_route_fingerprint
    )


def test_prompt_cache_route_scope_has_no_ipv6_host_port_collision(monkeypatch):
    user_id = "same-private-user"
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", "runtime-secret-alpha")
    _install_provider_config(
        monkeypatch,
        base_url="https://[2001:db8::1]:8000/v1",
    )
    host_plus_port, _ = serve_worker._resolve_provider(user_id)
    _install_provider_config(
        monkeypatch,
        base_url="https://[2001:db8::1:8000]/v1",
    )
    longer_host, _ = serve_worker._resolve_provider(user_id)

    assert host_plus_port is not None and longer_host is not None
    assert host_plus_port.prompt_cache_key != longer_host.prompt_cache_key
    assert (
        host_plus_port.prompt_cache_route_fingerprint
        != longer_host.prompt_cache_route_fingerprint
    )
