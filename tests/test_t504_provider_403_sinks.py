"""T504: every provider-403 auth sink shares one raw-input boundary."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import types

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

for key, value in {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_t504_checkpoint.json",
}.items():
    os.environ.setdefault(key, value)

import provider_client  # noqa: E402
import provider_health  # noqa: E402
from genesis import service as genesis_service  # noqa: E402
from hosted import image_generator, setup_core, vision_observer  # noqa: E402
from model_api_runtime.v2 import extraction, slot_protocol, worker  # noqa: E402
from notices import catalog, error_contract  # noqa: E402
import tools.chat_resident_consumer as resident  # noqa: E402


GENERIC_MESSAGE = "Request failed. Please try again later."


def _body(message: str, *, error_type: str = "api_error", **extra) -> str:
    return json.dumps(
        {"error": {"message": message, "type": error_type, **extra}},
        ensure_ascii=False,
    )


GENERIC_BODY = _body(GENERIC_MESSAGE)
GENERIC_CASE_VARIANT_BODY = _body(
    "request failed. please try again later.", error_type="API_ERROR"
)
AUTH_BODY = _body("Unauthorized: invalid API key")


def _provider_error(status: int, raw_body: str) -> provider_client.ProviderError:
    with pytest.raises(provider_client.ProviderError) as caught:
        provider_client._raise_for_provider_status(
            httpx.Response(status, text=raw_body)
        )
    return caught.value


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    (
        (403, GENERIC_BODY, "upstream_unavailable"),
        (403, AUTH_BODY, "auth_invalid"),
        (401, GENERIC_BODY, "auth_invalid"),
    ),
    ids=("generic-403", "auth-403", "generic-message-401"),
)
def test_catalog_and_wake_health_sink_three_grid(status, body, expected):
    exc = _provider_error(status, body)
    assert catalog.classify_upstream(str(exc)) == expected
    assert provider_health.error_class_for_exception(exc) == expected


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    (
        (403, GENERIC_BODY, "image_generation_unavailable"),
        (403, AUTH_BODY, "image_generation_auth_invalid"),
        (401, GENERIC_BODY, "image_generation_auth_invalid"),
    ),
    ids=("generic-403", "auth-403", "generic-message-401"),
)
def test_image_generation_sink_three_grid(status, body, expected):
    exc = _provider_error(status, body)
    assert image_generator.classify_image_generation_error(exc) == expected
    # The setup probe is a consumer of this classifier, not a separate sink.
    assert setup_core._image_generation_error_code(
        exc, dedicated=True
    ) == expected


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    (
        (403, GENERIC_BODY, "vision_model_unavailable"),
        (403, AUTH_BODY, "vision_model_auth_invalid"),
        (401, GENERIC_BODY, "vision_model_auth_invalid"),
    ),
    ids=("generic-403", "auth-403", "generic-message-401"),
)
def test_vision_sink_three_grid(status, body, expected):
    exc = _provider_error(status, body)
    assert vision_observer.classify_vision_error(exc).error_code == expected
    # Catalog-aware setup remapping must preserve auth/unavailable outcomes.
    assert setup_core._classify_catalog_route_vision_error(
        exc, catalog_model_found=True
    ).error_code == expected


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    (
        (403, GENERIC_BODY, "upstream_unavailable"),
        (403, AUTH_BODY, "auth_invalid"),
        (401, GENERIC_BODY, "auth_invalid"),
    ),
    ids=("generic-403", "auth-403", "generic-message-401"),
)
def test_v2_turn_sink_three_grid(status, body, expected):
    assert worker._turn_failure_error_class(
        _provider_error(status, body)
    ) == expected


def test_v2_turn_and_wake_sinks_use_raw_body_not_reduced_detail():
    exc = provider_client.ProviderError(
        "provider_http_403: forbidden",
        status_code=403,
        response_detail="forbidden",
        raw_response_body=GENERIC_BODY,
    )
    assert worker._turn_failure_error_class(exc) == "upstream_unavailable"
    assert provider_health.error_class_for_exception(exc) == "upstream_unavailable"


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    (
        (403, GENERIC_BODY, "internal"),
        (403, AUTH_BODY, "bad_api_key"),
        (401, GENERIC_BODY, "bad_api_key"),
    ),
    ids=("generic-403", "auth-403", "generic-message-401"),
)
def test_genesis_sink_three_grid(status, body, expected):
    exc = _provider_error(status, body)
    assert genesis_service.classify_genesis_error(str(exc), exc) == expected
    # Persisted jobs retain only the production exception string, never raw body.
    assert genesis_service.classify_genesis_error(str(exc)) == expected


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    (
        (403, GENERIC_BODY, "provider_error"),
        (403, AUTH_BODY, "provider_auth"),
        (401, GENERIC_BODY, "provider_auth"),
    ),
    ids=("generic-403", "auth-403", "generic-message-401"),
)
def test_resident_cli_attempt_sink_three_grid(status, body, expected):
    production_text = f"pi agent produced no reply: {status}: {body}"
    assert resident._provider_attempt_error_class(production_text) == expected


def test_resident_cli_passes_unmodified_text_to_shared_boundary(monkeypatch):
    production_text = (
        "Pi Agent Produced No Reply: 403: " + GENERIC_CASE_VARIANT_BODY
    )
    seen = []

    def classify(status, raw_body):
        seen.append((status, raw_body))
        return False

    monkeypatch.setattr(
        resident._error_contract,
        "provider_response_is_auth_failure",
        classify,
    )

    assert resident._provider_attempt_error_class(production_text) == "provider_error"
    assert seen == [(403, production_text)]


def test_extraction_status_only_contract_is_unchanged_pending_product_ruling():
    exc = _provider_error(403, GENERIC_BODY)
    assert extraction._provider_failure_code(exc) == "auth_invalid"


def test_shared_boundary_accepts_case_variants_without_pre_lowering():
    assert error_contract.provider_response_is_auth_failure(
        403, GENERIC_CASE_VARIANT_BODY
    ) is False


def test_raw_body_cannot_cross_slot_protocol_encoding():
    sentinel = "PRIVATE_RAW_PROVIDER_SLOT_BODY"
    exc = provider_client.ProviderError(
        "provider_http_403: forbidden",
        status_code=403,
        raw_response_body=_body(GENERIC_MESSAGE, debug=sentinel),
    )
    assert sentinel in exc.raw_response_body
    with pytest.raises(TypeError, match="unsupported slot message"):
        slot_protocol.encode_message(exc)


def test_raw_body_stays_out_of_image_trace_ledger_and_stderr(monkeypatch, capsys):
    sentinel = "PRIVATE_RAW_PROVIDER_IMAGE_BODY"
    exc = provider_client.ProviderError(
        "provider_http_403: Request failed. Please try again later.",
        status_code=403,
        response_detail=GENERIC_MESSAGE,
        raw_response_body=_body(GENERIC_MESSAGE, debug=sentinel),
    )
    assert sentinel in exc.raw_response_body
    traces = []
    ledger_rows = []
    monkeypatch.setattr(
        image_generator.debug_trace,
        "trace_event",
        lambda _store, **kwargs: traces.append(kwargs),
    )
    monkeypatch.setattr(
        image_generator.db,
        "log_append",
        lambda *args, **kwargs: ledger_rows.append((args, kwargs)) or False,
    )

    image_generator.observe_attempt(
        types.SimpleNamespace(user_id="usr_t504"),
        attempt_id="image_generation:t504",
        operation="setup_test",
        provider="openai_compatible",
        model="relay-image",
        outcome="failed",
        provider_called=True,
        error_category=image_generator.classify_image_generation_error(exc),
        status_code=exc.status_code,
    )

    emitted = repr((traces, ledger_rows)) + capsys.readouterr().err
    assert sentinel not in repr(traces)
    assert sentinel not in repr(ledger_rows)
    assert sentinel not in emitted


def test_raw_body_stays_out_of_setup_provider_attempt_ledger(monkeypatch):
    sentinel = "PRIVATE_RAW_PROVIDER_SETUP_BODY"
    exc = provider_client.ProviderError(
        "provider_http_403: Request failed. Please try again later.",
        status_code=403,
        response_detail=GENERIC_MESSAGE,
        raw_response_body=_body(GENERIC_MESSAGE, debug=sentinel),
    )
    assert sentinel in exc.raw_response_body
    traces = []
    ledger_rows = []
    monkeypatch.setattr(
        setup_core.provider_client,
        "test_provider_key",
        lambda _config: (_ for _ in ()).throw(exc),
    )
    monkeypatch.setattr(
        setup_core,
        "_emit_model_api_probe_trace",
        lambda _store, **kwargs: traces.append(kwargs),
    )
    monkeypatch.setattr(
        setup_core.provider_attempt_ledger,
        "record_runtime_attempt",
        lambda *args, **kwargs: ledger_rows.append((args, kwargs)) or True,
    )

    with pytest.raises(provider_client.ProviderError):
        setup_core._test_provider_key_observed(
            types.SimpleNamespace(user_id="usr_t504"),
            provider_client.ProviderConfig(
                "openai_compatible",
                "relay-model",
                "secret-key",
                "https://relay.example/v1",
            ),
            operation="setup",
            probe_trace_id="model_api_probe:t504",
        )

    assert sentinel not in repr(traces)
    assert sentinel not in repr(ledger_rows)
