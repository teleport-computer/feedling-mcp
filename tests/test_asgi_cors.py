"""Browser CORS contract for the public API documentation playground."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import asgi_app  # noqa: E402
from asgi.settings import _origins  # noqa: E402


DOCS_ORIGIN = "https://docs.feedling.app"
UNTRUSTED_ORIGIN = "https://malicious.example"
PLAYGROUND_REQUEST_HEADERS = (
    "authorization",
    "content-type",
    "if-none-match",
    "range",
    "x-api-key",
    "x-byte-end",
    "x-byte-start",
    "x-ciphertext-sha256",
    "x-content-sha256",
    "x-envelope-meta",
    "x-feedling-consumer",
    "x-feedling-consumer-compat-commit",
    "x-feedling-consumer-commit",
    "x-feedling-consumer-id",
    "x-feedling-consumer-version",
    "x-feedling-runtime-token",
)


def _request(method: str, path: str, *, headers: dict[str, str] | None = None):
    async def go():
        transport = httpx.ASGITransport(app=asgi_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, headers=headers)

    return asyncio.run(go())


def _preflight(origin: str, method: str, request_headers: str):
    return _request(
        "OPTIONS",
        "/v1/users/whoami",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": request_headers,
        },
    )


def test_docs_origin_preflight_allows_playground_methods_and_contract_headers():
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        response = _preflight(
            DOCS_ORIGIN,
            method,
            ", ".join(PLAYGROUND_REQUEST_HEADERS),
        )

        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == DOCS_ORIGIN
        assert method in response.headers["access-control-allow-methods"]
        allowed_headers = response.headers["access-control-allow-headers"].lower()
        for header in PLAYGROUND_REQUEST_HEADERS:
            assert header in allowed_headers
        assert "access-control-allow-credentials" not in response.headers


def test_untrusted_origin_preflight_is_rejected():
    response = _preflight(
        UNTRUSTED_ORIGIN,
        "GET",
        ", ".join(PLAYGROUND_REQUEST_HEADERS),
    )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_auth_error_is_readable_by_docs_origin_without_bypassing_auth():
    response = _request(
        "GET",
        "/v1/users/whoami",
        headers={"Origin": DOCS_ORIGIN},
    )

    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == DOCS_ORIGIN
    exposed_headers = response.headers["access-control-expose-headers"].lower()
    assert "accept-ranges" in exposed_headers
    assert "content-range" in exposed_headers


def test_non_browser_request_is_unchanged():
    response = _request("GET", "/healthz")

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_cors_configuration_rejects_wildcard_origin(monkeypatch):
    monkeypatch.setenv("TEST_CORS_ORIGINS", "*")

    with pytest.raises(ValueError, match="exact origins"):
        _origins("TEST_CORS_ORIGINS", ())
