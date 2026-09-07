from __future__ import annotations

import base64
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from hosted import image_generator, setup_core  # noqa: E402


def _store(user_id: str = "image-user"):
    return SimpleNamespace(user_id=user_id)


def _valid_png_base64() -> str:
    out = io.BytesIO()
    Image.new("RGB", (2, 2), (30, 90, 180)).save(out, format="PNG")
    return base64.b64encode(out.getvalue()).decode("ascii")


def _route(**overrides):
    route = {
        "id": "route-image",
        "credential_id": "credential-image",
        "provider": "openai",
        "model": "gpt-image-2",
        "base_url": "https://api.openai.com/v1",
        "api_key_envelope": {
            "body_ct": "sealed",
            "nonce": "nonce",
            "K_user": "user-key",
            "K_enclave": "enclave-key",
            "id": "credential-image",
            "owner_user_id": "image-user",
            "visibility": "shared",
            "v": 1,
        },
        "image_generation_test_status": "ok",
        "last_image_generation_test_error": "",
    }
    route.update(overrides)
    return route


def _capture_attempt_observations(monkeypatch):
    traces = []
    logs = []
    monkeypatch.setattr(
        image_generator.debug_trace,
        "trace_event",
        lambda _store, **event: traces.append(event),
    )

    def append(user_id, stream, doc, *, ts, item_key):
        logs.append((user_id, stream, item_key, doc, ts))
        return True

    monkeypatch.setattr(image_generator.db, "log_append", append)
    return traces, logs


@pytest.fixture(autouse=True)
def _isolate_image_observation_sinks(monkeypatch):
    """Keep these unit tests from enqueueing asynchronous DB trace writes."""
    monkeypatch.setattr(
        image_generator.debug_trace,
        "trace_event",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        image_generator.db,
        "log_append",
        lambda *_args, **_kwargs: True,
    )


@pytest.mark.parametrize(
    ("status_code", "dedicated_expected", "follow_main_expected"),
    (
        (
            400,
            "image_generation_model_incompatible",
            "image_generation_model_required",
        ),
        (401, "image_generation_auth_invalid", "image_generation_auth_invalid"),
        (
            402,
            "image_generation_quota_insufficient",
            "image_generation_quota_insufficient",
        ),
        (403, "image_generation_auth_invalid", "image_generation_auth_invalid"),
        (
            404,
            "image_generation_model_not_found",
            "image_generation_model_not_found",
        ),
        (
            415,
            "image_generation_model_incompatible",
            "image_generation_model_required",
        ),
        (
            422,
            "image_generation_model_incompatible",
            "image_generation_model_required",
        ),
    ),
)
def test_image_generation_classifier_preserves_status_specific_codes(
    status_code, dedicated_expected, follow_main_expected,
):
    exc = image_generator.provider_client.ProviderError(
        "opaque provider failure",
        status_code=status_code,
    )

    # This generic class is deliberately identical for all seven inputs. The
    # image classifier must therefore read the status itself, not the broad
    # provider_config bucket that caused T494.
    assert image_generator.provider_client.classify_provider_error(exc) == (
        "provider_config"
    )
    assert image_generator._classify_error(exc) == dedicated_expected
    assert setup_core._image_generation_error_code(
        exc,
        dedicated=True,
    ) == dedicated_expected
    assert setup_core._image_generation_error_code(
        exc,
        dedicated=False,
    ) == follow_main_expected


@pytest.mark.parametrize("status_code", (408, 429, 500))
def test_image_generation_classifier_keeps_existing_transient_fallback(
    status_code,
):
    exc = image_generator.provider_client.ProviderError(
        "opaque provider failure",
        status_code=status_code,
    )

    assert image_generator._classify_error(exc) == "image_generation_failed"
    assert setup_core._image_generation_error_code(
        exc, dedicated=True
    ) == "image_generation_test_failed"


def test_image_generation_classifier_keeps_statusless_provider_config_behavior():
    exc = RuntimeError("opaque configuration failure")
    exc.feedling_error_class = "provider_config"

    assert image_generator._classify_error(
        exc
    ) == "image_generation_model_incompatible"
    assert setup_core._image_generation_error_code(
        exc, dedicated=False
    ) == "image_generation_model_required"


def test_config_payload_follows_main_without_replacing_chat_model(monkeypatch):
    active = _route(
        id="route-main",
        provider="deepseek",
        model="deepseek-v4-flash",
        image_generation_test_status="unsupported",
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_active_route",
        lambda _uid: active,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_image_generation_route",
        lambda _uid: None,
    )
    monkeypatch.setattr(
        setup_core.vision_routing,
        "runtime_capability",
        lambda _store: {"runtime": "v2", "onboarding_route": "model_api"},
    )

    config = setup_core._image_generation_config_payload(_store())

    assert config["mode"] == "follow_main"
    assert config["main_model"]["model"] == "deepseek-v4-flash"
    assert config["effective_status"] == "unsupported"
    assert config["dedicated_route"] is None


def test_resident_config_exposes_text_only_main_and_dedicated_capability(monkeypatch):
    monkeypatch.setattr(
        setup_core.vision_routing,
        "runtime_capability",
        lambda _store: {"runtime": "vps", "onboarding_route": "resident"},
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_active_route",
        lambda _uid: None,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_image_generation_route",
        lambda _uid: None,
    )
    monkeypatch.setattr(
        setup_core.vision_routing.chat_consumer,
        "consumer_agent_runtime",
        lambda _store: {
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-flash",
        },
    )
    monkeypatch.setattr(
        setup_core.vision_routing.chat_consumer,
        "consumer_supports_capability",
        lambda _store, capability: (
            capability == setup_core.vision_routing.chat_consumer.IMAGE_GENERATION_CAPABILITY
        ),
    )

    config = setup_core._image_generation_config_payload(_store())

    assert config["available"] is True
    assert config["runtime"] == "vps"
    assert config["main_model"] == {
        "source": "resident",
        "route_id": None,
        "provider": "openrouter",
        "model": "deepseek/deepseek-v4-flash",
        "image_generation_test_status": "unsupported",
        "last_image_generation_test_error": "image_generation_model_required",
    }
    assert config["effective_status"] == "unsupported"


def test_resident_config_uses_declared_agent_image_generation_capability(monkeypatch):
    monkeypatch.setattr(
        setup_core.vision_routing,
        "runtime_capability",
        lambda _store: {"runtime": "vps", "onboarding_route": "resident"},
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_active_route",
        lambda _uid: None,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_image_generation_route",
        lambda _uid: None,
    )
    monkeypatch.setattr(
        setup_core.vision_routing.chat_consumer,
        "consumer_agent_runtime",
        lambda _store: {"provider": "openai", "model": "gpt-5.6-sol"},
    )
    monkeypatch.setattr(
        setup_core.vision_routing.chat_consumer,
        "consumer_supports_capability",
        lambda _store, capability: capability in {
            setup_core.vision_routing.chat_consumer.IMAGE_GENERATION_CAPABILITY,
            setup_core.vision_routing.chat_consumer.AGENT_IMAGE_GENERATION_CAPABILITY,
        },
    )

    config = setup_core._image_generation_config_payload(_store())

    assert config["main_model"]["image_generation_test_status"] == "ok"
    assert config["main_model"]["last_image_generation_test_error"] == ""
    assert config["effective_status"] == "ok"


def test_resident_main_test_uses_declared_agent_capability(monkeypatch):
    monkeypatch.setattr(
        setup_core.vision_routing,
        "runtime_capability",
        lambda _store: {"runtime": "vps"},
    )
    monkeypatch.setattr(
        setup_core.vision_routing.chat_consumer,
        "consumer_supports_capability",
        lambda _store, capability: (
            capability
            == setup_core.vision_routing.chat_consumer.AGENT_IMAGE_GENERATION_CAPABILITY
        ),
    )
    monkeypatch.setattr(
        setup_core,
        "_image_generation_config_payload",
        lambda _store: {"effective_status": "ok"},
    )

    body, status = setup_core.image_generation_main_test.__wrapped__(
        _store(),
        caller_api_key="caller-key",
    )

    assert status == 200
    assert body == {"config": {"effective_status": "ok"}}


def test_resident_generate_requires_a_pinned_image_route(monkeypatch):
    monkeypatch.setattr(
        image_generator.db,
        "model_api_image_generation_route",
        lambda _uid: None,
    )

    body, status = image_generator.generate_with_pinned_route(
        _store(),
        {"prompt": "draw a red robot"},
        caller_api_key="caller-key",
    )

    assert status == 409
    assert body == {
        "error": "image_generation_model_required",
        "error_class": "image_generation_model_required",
    }


def test_resident_generate_uses_only_the_pinned_image_route(monkeypatch, capsys):
    marked = []
    decrypted = []
    traces, logs = _capture_attempt_observations(monkeypatch)
    monkeypatch.setattr(
        image_generator.db,
        "model_api_image_generation_route",
        lambda _uid: _route(),
    )

    def fake_decrypt(
        envelope,
        caller_key,
        *,
        purpose,
        caller_user_id,
        runtime_token="",
    ):
        decrypted.append((
            envelope,
            caller_key,
            purpose,
            caller_user_id,
            runtime_token,
        ))
        return b"provider-key"

    monkeypatch.setattr(
        image_generator.core_enclave,
        "_decrypt_envelope_via_enclave",
        fake_decrypt,
    )

    async def generated(config, prompt):
        assert config.provider == "openai"
        assert config.model == "gpt-image-2"
        assert config.api_key == "provider-key"
        assert prompt == "draw a red robot"
        return {
            "media": [
                {
                    "mime_type": "image/png",
                    "data_base64": "aW1hZ2U=",
                    "name": "robot.png",
                }
            ]
        }

    monkeypatch.setattr(
        image_generator.provider_client,
        "generate_image_async",
        generated,
    )
    monkeypatch.setattr(
        image_generator.generated_image,
        "normalize_generated_image",
        lambda *_args, **_kwargs: SimpleNamespace(
            data=b"normalized-image",
            mime_type="image/png",
            name="robot.png",
        ),
    )
    monkeypatch.setattr(
        image_generator.db,
        "model_api_route_mark_image_generation_test",
        lambda _uid, _rid, **kwargs: marked.append(kwargs) or True,
    )

    body, status = image_generator.generate_with_pinned_route(
        _store(),
        {"prompt": "draw a red robot"},
        caller_api_key="caller-key",
        caller_runtime_token="runtime-token",
    )

    assert status == 200
    assert body["provider"] == "openai"
    assert body["model"] == "gpt-image-2"
    assert body["images"] == [
        {
            "mime_type": "image/png",
            "data_base64": "bm9ybWFsaXplZC1pbWFnZQ==",
            "name": "robot.png",
        }
    ]
    assert decrypted == [
        (
            _route()["api_key_envelope"],
            "caller-key",
            "model_api_provider_key",
            "image-user",
            "runtime-token",
        )
    ]
    assert marked == [{"status": "ok"}]
    assert len(traces) == len(logs) == 1
    assert traces[0]["detail"] == {
        key: value for key, value in logs[0][3].items() if key not in {"source", "ts"}
    }
    assert logs[0][0:3] == (
        "image-user",
        "image_generation_attempts",
        traces[0]["trace_id"],
    )
    assert {
        key: traces[0]["detail"][key]
        for key in (
            "operation",
            "provider",
            "model",
            "outcome",
            "error_category",
            "provider_called",
        )
    } == {
        "operation": "runtime_generate",
        "provider": "openai",
        "model": "gpt-image-2",
        "outcome": "ok",
        "error_category": "",
        "provider_called": True,
    }
    assert "attempt_finished" not in capsys.readouterr().err


def _wire_resident_generation_route(monkeypatch, *, generated, marked):
    monkeypatch.setattr(
        image_generator.db,
        "model_api_image_generation_route",
        lambda _uid: _route(),
    )
    monkeypatch.setattr(
        image_generator.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: b"provider-key",
    )
    monkeypatch.setattr(
        image_generator.provider_client,
        "generate_image_async",
        generated,
    )
    monkeypatch.setattr(
        image_generator.db,
        "model_api_route_mark_image_generation_test",
        lambda _uid, _rid, **kwargs: marked.append(kwargs) or True,
    )


def test_resident_provider_seam_bare_exception_still_updates_route_health(
    monkeypatch, capsys,
):
    """The narrow try is structural; provider adapters may leak raw exceptions."""
    marked = []
    traces, logs = _capture_attempt_observations(monkeypatch)

    async def provider_failure(*_args, **_kwargs):
        raise TypeError("provider adapter response parser failed")

    _wire_resident_generation_route(
        monkeypatch,
        generated=provider_failure,
        marked=marked,
    )

    body, status = image_generator.generate_with_pinned_route(
        _store(),
        {"prompt": "draw a red robot"},
        caller_api_key="caller-key",
    )

    assert status == 400
    assert body["error"] == "image_generation_failed"
    assert marked == [{"status": "failed", "error": "image_generation_failed"}]
    assert len(traces) == len(logs) == 1
    assert {
        key: logs[0][3][key]
        for key in (
            "operation",
            "outcome",
            "error_category",
            "provider_called",
        )
    } == {
        "operation": "runtime_generate",
        "outcome": "failed",
        "error_category": "image_generation_failed",
        "provider_called": True,
    }
    stderr = capsys.readouterr().err
    assert "error_category=image_generation_failed" in stderr
    assert "provider adapter response parser failed" not in stderr


def test_resident_local_decrypt_bug_does_not_poison_route_health(monkeypatch):
    marked = []
    monkeypatch.setattr(
        image_generator.db,
        "model_api_image_generation_route",
        lambda _uid: _route(),
    )
    monkeypatch.setattr(
        image_generator.core_envelope,
        "decrypt_provider_key_envelope",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            TypeError("internal decrypt helper signature drift")
        ),
    )
    monkeypatch.setattr(
        image_generator.db,
        "model_api_route_mark_image_generation_test",
        lambda _uid, _rid, **kwargs: marked.append(kwargs) or True,
    )

    with pytest.raises(TypeError, match="internal decrypt helper signature drift"):
        image_generator.generate_with_pinned_route(
            _store(),
            {"prompt": "draw a red robot"},
            caller_api_key="caller-key",
        )

    assert marked == [], "local credential bugs are not provider route evidence"


def test_resident_decrypt_contract_failure_has_specific_code_without_route_verdict(
    monkeypatch,
):
    marked = []
    monkeypatch.setattr(
        image_generator.db,
        "model_api_image_generation_route",
        lambda _uid: _route(),
    )
    monkeypatch.setattr(
        image_generator.core_envelope,
        "decrypt_provider_key_envelope",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("enclave_http_400:invalid envelope")
        ),
    )
    monkeypatch.setattr(
        image_generator.db,
        "model_api_route_mark_image_generation_test",
        lambda _uid, _rid, **kwargs: marked.append(kwargs) or True,
    )

    body, status = image_generator.generate_with_pinned_route(
        _store(),
        {"prompt": "draw a red robot"},
        caller_api_key="caller-key",
    )

    assert status == 409
    assert body["error"] == "image_generation_key_decrypt_failed"
    assert body["error_class"] == "image_generation_key_decrypt_failed"
    assert marked == [], "credential decrypt failures are not provider route evidence"


def test_resident_enclave_unavailable_stays_retryable_without_route_verdict(
    monkeypatch,
):
    marked = []
    monkeypatch.setattr(
        image_generator.db,
        "model_api_image_generation_route",
        lambda _uid: _route(),
    )
    monkeypatch.setattr(
        image_generator.core_envelope,
        "decrypt_provider_key_envelope",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("enclave_unavailable")
        ),
    )
    monkeypatch.setattr(
        image_generator.db,
        "model_api_route_mark_image_generation_test",
        lambda _uid, _rid, **kwargs: marked.append(kwargs) or True,
    )

    body, status = image_generator.generate_with_pinned_route(
        _store(),
        {"prompt": "draw a red robot"},
        caller_api_key="caller-key",
    )

    assert status == 503
    assert body["error"] == "image_generation_unavailable"
    assert marked == [], "enclave outages are not provider route evidence"


def test_resident_unknown_decrypt_runtime_error_remains_server_failure(
    monkeypatch,
):
    marked = []
    monkeypatch.setattr(
        image_generator.db,
        "model_api_image_generation_route",
        lambda _uid: _route(),
    )
    monkeypatch.setattr(
        image_generator.core_envelope,
        "decrypt_provider_key_envelope",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("enclave_brand_new_mode")
        ),
    )
    monkeypatch.setattr(
        image_generator.db,
        "model_api_route_mark_image_generation_test",
        lambda _uid, _rid, **kwargs: marked.append(kwargs) or True,
    )

    with pytest.raises(RuntimeError, match="^enclave_brand_new_mode$"):
        image_generator.generate_with_pinned_route(
            _store(),
            {"prompt": "draw a red robot"},
            caller_api_key="caller-key",
        )

    assert marked == [], "unknown local failures are not provider route evidence"


def test_resident_normalization_bug_does_not_poison_route_health(monkeypatch):
    marked = []

    async def generated(*_args, **_kwargs):
        return {
            "media": [{
                "mime_type": "image/png",
                "data_base64": "aW1hZ2U=",
                "name": "robot.png",
            }]
        }

    _wire_resident_generation_route(
        monkeypatch,
        generated=generated,
        marked=marked,
    )
    monkeypatch.setattr(
        image_generator.generated_image,
        "normalize_generated_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            TypeError("internal normalizer signature drift")
        ),
    )

    with pytest.raises(TypeError, match="internal normalizer signature drift"):
        image_generator.generate_with_pinned_route(
            _store(),
            {"prompt": "draw a red robot"},
            caller_api_key="caller-key",
        )

    assert marked == [], "our processing bug must leave the saved route status unchanged"


def test_select_dedicated_tests_before_changing_route(monkeypatch):
    route = _route(image_generation_test_status="untested")
    events = []
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_get",
        lambda _uid, _rid: route,
    )
    monkeypatch.setattr(
        setup_core,
        "_test_route_image_generation_or_error",
        lambda *_args, **_kwargs: events.append("tested") or None,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_set_image_generation",
        lambda _uid, _rid: events.append("selected") or True,
    )
    monkeypatch.setattr(
        setup_core,
        "_image_generation_config_payload",
        lambda _store: {"mode": "dedicated", "effective_status": "ok"},
    )

    body, status = setup_core.image_generation_config_set.__wrapped__(
        _store(),
        {"mode": "dedicated", "route_id": route["id"]},
        caller_api_key="caller-key",
    )

    assert status == 200
    assert body["config"]["effective_status"] == "ok"
    assert events == ["tested", "selected"]


def test_failed_follow_main_probe_preserves_existing_dedicated_route(monkeypatch):
    active = _route(id="route-main", provider="deepseek", model="deepseek-v4-flash")
    cleared = []
    monkeypatch.setattr(
        setup_core.db,
        "model_api_active_route",
        lambda _uid: active,
    )
    monkeypatch.setattr(
        setup_core,
        "_test_route_image_generation_or_error",
        lambda *_args, **_kwargs: (
            {"error": "image_generation_model_incompatible", "retryable": False},
            400,
        ),
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_clear_image_generation",
        lambda _uid: cleared.append(True) or True,
    )

    body, status = setup_core.image_generation_config_set.__wrapped__(
        _store(),
        {"mode": "follow_main"},
        caller_api_key="caller-key",
    )

    assert status == 400
    assert body["error"] == "image_generation_model_incompatible"
    assert cleared == []


def test_route_probe_marks_real_generated_media_ready(monkeypatch, capsys):
    marked = []
    traces, logs = _capture_attempt_observations(monkeypatch)
    monkeypatch.setattr(
        setup_core.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: b"provider-key",
    )

    async def generated(*_args, **_kwargs):
        return {
            "media": [
                {
                    "mime_type": "image/png",
                    "data_base64": _valid_png_base64(),
                }
            ]
        }

    monkeypatch.setattr(
        setup_core.provider_client,
        "generate_image_async",
        generated,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_mark_image_generation_test",
        lambda _uid, _rid, **kwargs: marked.append(kwargs) or True,
    )

    error = setup_core._test_route_image_generation_or_error(
        _store(),
        _route(),
        "caller-key",
    )

    assert error is None
    assert marked == [{"status": "ok"}]
    assert len(traces) == len(logs) == 1
    payload = logs[0][3]
    assert payload["operation"] == "setup_test"
    assert payload["provider"] == "openai"
    assert payload["model"] == "gpt-image-2"
    assert payload["outcome"] == "ok"
    assert payload["error_category"] == ""
    assert payload["provider_called"] is True
    assert traces[0]["detail"] == {
        key: value for key, value in payload.items() if key not in {"source", "ts"}
    }
    assert "attempt_finished" not in capsys.readouterr().err


def test_route_probe_failure_is_observable_without_provider_error_text(
    monkeypatch, capsys,
):
    marked = []
    traces, logs = _capture_attempt_observations(monkeypatch)
    monkeypatch.setattr(
        setup_core.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: b"provider-key",
    )

    async def failed(*_args, **_kwargs):
        exc = setup_core.provider_client.ProviderError(
            "upstream echoed sk-private-key and private prompt",
            status_code=401,
        )
        exc.feedling_error_class = "auth_invalid"
        raise exc

    monkeypatch.setattr(
        setup_core.provider_client,
        "generate_image_async",
        failed,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_mark_image_generation_test",
        lambda _uid, _rid, **kwargs: marked.append(kwargs) or True,
    )

    body, status = setup_core._test_route_image_generation_or_error(
        _store(),
        _route(),
        "caller-key",
    )

    assert status == 400
    assert body == {
        "error": "image_generation_auth_invalid",
        "retryable": True,
    }
    assert marked == [{
        "status": "failed",
        "error": "image_generation_auth_invalid",
    }]
    assert len(traces) == len(logs) == 1
    assert logs[0][0:2] == ("image-user", "image_generation_attempts")
    payload = logs[0][3]
    assert payload["operation"] == "setup_test"
    assert payload["provider"] == "openai"
    assert payload["model"] == "gpt-image-2"
    assert payload["outcome"] == "failed"
    assert payload["error_category"] == "image_generation_auth_invalid"
    assert payload["provider_called"] is True
    assert payload["status_code"] == 401
    assert traces[0]["detail"] == {
        key: value for key, value in payload.items() if key not in {"source", "ts"}
    }
    emitted = repr((traces, logs)) + capsys.readouterr().err
    assert "sk-private-key" not in emitted
    assert "private prompt" not in emitted


def test_local_image_capability_rejection_records_provider_not_called(
    monkeypatch, capsys,
):
    traces, logs = _capture_attempt_observations(monkeypatch)
    route = _route(provider="deepseek", model="deepseek-v4-flash")
    monkeypatch.setattr(
        setup_core.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: b"provider-key",
    )
    monkeypatch.setattr(
        image_generator.provider_client,
        "chat_completion_async",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("HTTP provider seam must not be reached")
        ),
    )
    monkeypatch.setattr(
        image_generator.db,
        "model_api_image_generation_route",
        lambda _uid: route,
    )
    monkeypatch.setattr(
        image_generator.db,
        "model_api_route_mark_image_generation_test",
        lambda *_args, **_kwargs: True,
    )

    setup_body, setup_status = setup_core._test_route_image_generation_or_error(
        _store(),
        route,
        "caller-key",
    )
    runtime_body, runtime_status = image_generator.generate_with_pinned_route(
        _store(),
        {"prompt": "draw a red robot"},
        caller_api_key="caller-key",
    )

    assert (setup_status, setup_body["error"]) == (
        400,
        "image_generation_model_incompatible",
    )
    assert (runtime_status, runtime_body["error"]) == (
        400,
        "image_generation_model_incompatible",
    )
    assert len(traces) == len(logs) == 2
    assert [record[3]["operation"] for record in logs] == [
        "setup_test",
        "runtime_generate",
    ]
    assert all(record[3]["provider_called"] is False for record in logs)
    assert all(record[3]["status_code"] is None for record in logs)
    assert "provider_called=false" in capsys.readouterr().err


def test_new_provider_family_probe_records_http_status(monkeypatch, capsys):
    calls: list[str] = []
    traces, logs = _capture_attempt_observations(monkeypatch)
    route = _route(provider="deepseek", model="qwen-image-3.0")
    monkeypatch.setattr(
        setup_core.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: b"provider-key",
    )

    class RejectedAsyncClient:
        is_closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            self.is_closed = True

        async def post(self, url: str, *, headers=None, json=None, timeout=None):
            calls.append(url)

            class Response:
                status_code = 401
                text = "test rejection"

                @staticmethod
                def json():
                    return {"error": {"message": "test rejection"}}

            return Response()

    monkeypatch.setattr(
        setup_core.provider_client,
        "_build_shared_async_client",
        lambda **_kwargs: RejectedAsyncClient(),
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_mark_image_generation_test",
        lambda *_args, **_kwargs: True,
    )

    body, status = setup_core._test_route_image_generation_or_error(
        _store(),
        route,
        "caller-key",
    )

    assert status == 400
    assert body == {
        "error": "image_generation_auth_invalid",
        "retryable": True,
    }
    assert len(calls) == 1
    assert calls[0].endswith("/chat/completions")
    assert len(traces) == len(logs) == 1
    payload = logs[0][3]
    assert payload["provider_called"] is True
    assert payload["status_code"] == 401
    assert payload["error_category"] == "image_generation_auth_invalid"
    assert "provider_called=true" in capsys.readouterr().err


def test_route_probe_observation_sink_failures_keep_content_free_stderr(
    monkeypatch, capsys,
):
    monkeypatch.setattr(
        image_generator.debug_trace,
        "trace_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("forced trace failure")
        ),
    )
    monkeypatch.setattr(image_generator.db, "log_append", lambda *_a, **_kw: False)
    monkeypatch.setattr(
        setup_core.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: b"provider-key",
    )

    async def failed(*_args, **_kwargs):
        raise setup_core.provider_client.ProviderError(
            "provider body with sk-secret-material"
        )

    monkeypatch.setattr(
        setup_core.provider_client,
        "generate_image_async",
        failed,
    )
    monkeypatch.setattr(
        setup_core,
        "_image_generation_error_code",
        lambda _exc, *, dedicated: "image_generation_test_failed",
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_mark_image_generation_test",
        lambda *_args, **_kwargs: True,
    )

    body, status = setup_core._test_route_image_generation_or_error(
        _store(),
        _route(),
        "caller-key",
    )

    assert status == 400
    assert body["error"] == "image_generation_test_failed"
    stderr = capsys.readouterr().err
    assert "attempt_finished" in stderr
    assert "operation=setup_test" in stderr
    assert "signal_failures=trace_events,user_logs" in stderr
    assert "sk-secret-material" not in stderr


def test_configure_failure_observation_survives_route_and_credential_rollback(
    monkeypatch, capsys,
):
    events = []
    traces = []
    route = _route(id="new-route", credential_id="new-credential")
    monkeypatch.setattr(
        image_generator.debug_trace,
        "trace_event",
        lambda _store, **event: traces.append(event),
    )

    def append(user_id, stream, doc, *, ts, item_key):
        events.append(("user_log", user_id, stream, item_key, doc))
        return True

    monkeypatch.setattr(image_generator.db, "log_append", append)
    monkeypatch.setattr(setup_core.db, "model_api_routes_list", lambda _uid: [])
    monkeypatch.setattr(
        setup_core.model_api_route_create,
        "__wrapped__",
        lambda _store, _payload, **_kwargs: ({"route": route}, 200),
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_get",
        lambda _uid, _rid: route,
    )
    monkeypatch.setattr(
        setup_core.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: b"provider-key",
    )

    async def failed(*_args, **_kwargs):
        raise setup_core.provider_client.ProviderError("upstream failure")

    monkeypatch.setattr(
        setup_core.provider_client,
        "generate_image_async",
        failed,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_mark_image_generation_test",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_delete",
        lambda _uid, route_id: events.append(("route_delete", route_id)) or True,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_credential_delete",
        lambda _uid, credential_id: (
            events.append(("credential_delete", credential_id)) or True
        ),
    )

    body, status = setup_core.image_generation_route_configure.__wrapped__(
        _store(),
        {"provider": "openai", "model": "gpt-image-2", "api_key": "new-key"},
        caller_api_key="caller-key",
    )

    assert status == 400
    assert body["error"] == "image_generation_test_failed"
    assert [event[0] for event in events] == [
        "user_log",
        "route_delete",
        "credential_delete",
    ]
    assert events[0][1:4] == (
        "image-user",
        "image_generation_attempts",
        traces[0]["trace_id"],
    )
    assert events[0][4]["outcome"] == "failed"
    assert events[0][4]["provider_called"] is True
    capsys.readouterr()


def test_route_probe_normalization_bug_preserves_existing_status(monkeypatch):
    marked = []
    monkeypatch.setattr(
        setup_core.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: b"provider-key",
    )

    async def generated(*_args, **_kwargs):
        return {
            "media": [{
                "mime_type": "image/png",
                "data_base64": "aW1hZ2U=",
            }]
        }

    monkeypatch.setattr(
        setup_core.provider_client,
        "generate_image_async",
        generated,
    )
    monkeypatch.setattr(
        setup_core.image_generator,
        "normalize_provider_media",
        lambda _result: (_ for _ in ()).throw(
            TypeError("internal normalizer signature drift")
        ),
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_mark_image_generation_test",
        lambda _uid, _rid, **kwargs: marked.append(kwargs) or True,
    )

    with pytest.raises(TypeError, match="internal normalizer signature drift"):
        setup_core._test_route_image_generation_or_error(
            _store(),
            _route(image_generation_test_status="ok"),
            "caller-key",
        )

    assert marked == [], "the probe must neither fail nor re-approve the route"


def test_route_probe_maps_text_only_model_to_configuration_error(monkeypatch):
    marked = []
    monkeypatch.setattr(
        setup_core.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: b"provider-key",
    )

    async def unsupported(*_args, **_kwargs):
        exc = setup_core.provider_client.ProviderError(
            "image_generation_model_unsupported"
        )
        exc.feedling_error_class = "provider_incompatible"
        raise exc

    monkeypatch.setattr(
        setup_core.provider_client,
        "generate_image_async",
        unsupported,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_mark_image_generation_test",
        lambda _uid, _rid, **kwargs: marked.append(kwargs) or True,
    )

    body, status = setup_core._test_route_image_generation_or_error(
        _store(),
        _route(provider="deepseek", model="deepseek-v4-flash"),
        "caller-key",
    )

    assert status == 400
    assert body == {
        "error": "image_generation_model_incompatible",
        "retryable": False,
    }
    assert marked == [
        {
            "status": "unsupported",
            "error": "image_generation_model_incompatible",
        }
    ]


def _configure_rollback_env(monkeypatch, *, select_status, routes_before=()):
    """Wire image_generation_route_configure so create succeeds and select
    returns select_status; record every rollback delete in order."""
    deleted = []
    monkeypatch.setattr(
        setup_core.db,
        "model_api_routes_list",
        lambda _uid: [{"id": route_id} for route_id in routes_before],
    )
    monkeypatch.setattr(
        setup_core.model_api_route_create,
        "__wrapped__",
        lambda _store, _payload, **_kwargs: ({
            "route": {"id": "new-route", "credential_id": "new-credential"}
        }, 200),
    )
    monkeypatch.setattr(
        setup_core.image_generation_config_set,
        "__wrapped__",
        lambda _store, _payload, **_kwargs: (
            ({"config": {}}, 200)
            if select_status == 200
            else ({"error": "image_generation_invalid_output"}, select_status)
        ),
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_delete",
        lambda uid, route_id: deleted.append(("route", uid, route_id)) or True,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_credential_delete",
        lambda uid, credential_id: deleted.append(
            ("credential", uid, credential_id)
        ) or True,
    )
    return deleted


def test_failed_configure_with_new_key_rolls_back_route_and_credential(monkeypatch):
    deleted = _configure_rollback_env(monkeypatch, select_status=502)

    body, status = setup_core.image_generation_route_configure.__wrapped__(
        _store(),
        {"provider": "openai", "model": "gpt-image-2", "api_key": "secret"},
        caller_api_key="caller-key",
    )

    assert (body, status) == ({"error": "image_generation_invalid_output"}, 502)
    # 路线必须显式删:tee schema 没有 credential→route 级联,只删凭据会留下
    # JOIN 不出来、用户也删不掉的僵尸路线(2026-08 生图排查实锤)。
    assert deleted == [
        ("route", "image-user", "new-route"),
        ("credential", "image-user", "new-credential"),
    ]


def test_failed_configure_with_reused_credential_keeps_credential(monkeypatch):
    deleted = _configure_rollback_env(monkeypatch, select_status=502)

    body, status = setup_core.image_generation_route_configure.__wrapped__(
        _store(),
        {"credential_id": "new-credential", "model": "gpt-image-2"},
        caller_api_key="caller-key",
    )

    assert status == 502
    assert deleted == [("route", "image-user", "new-route")]


def test_failed_configure_leaves_preexisting_route_untouched(monkeypatch):
    deleted = _configure_rollback_env(
        monkeypatch, select_status=502, routes_before=("new-route",)
    )

    body, status = setup_core.image_generation_route_configure.__wrapped__(
        _store(),
        {"provider": "openai", "model": "gpt-image-2", "api_key": "secret"},
        caller_api_key="caller-key",
    )

    assert status == 502
    assert deleted == []


def test_successful_configure_deletes_nothing(monkeypatch):
    deleted = _configure_rollback_env(monkeypatch, select_status=200)

    body, status = setup_core.image_generation_route_configure.__wrapped__(
        _store(),
        {"provider": "openai", "model": "gpt-image-2", "api_key": "secret"},
        caller_api_key="caller-key",
    )

    assert status == 200
    assert deleted == []
