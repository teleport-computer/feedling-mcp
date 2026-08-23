import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from hosted import vision_observer, visual_transport
import provider_client


class _Store:
    user_id = "usr_trace_test"

    def __init__(self, rows=None, error=None):
        self._rows = rows or []
        self._error = error

    def reload_chat_strict(self):
        if self._error:
            raise self._error
        return self._rows


def _image_row():
    return {
        "id": "msg-image",
        "content_type": "image",
        "vision_route_id": "route-vision",
    }


def test_observe_image_uses_bounded_reliable_provider_call(monkeypatch):
    captured = {}

    def fake_reliable(config, messages, **kwargs):
        captured.update(config=config, messages=messages, kwargs=kwargs)
        return {"reply": "A highway guardrail is visible."}

    monkeypatch.setattr(
        vision_observer.provider_client,
        "reliable_chat_completion_isolated",
        fake_reliable,
    )
    config = object()

    result = vision_observer.observe_image(
        config,
        image_mime="image/jpeg",
        image_b64="encoded-image",
    )

    assert result == "A highway guardrail is visible."
    assert captured["config"] is config
    assert captured["kwargs"]["max_attempts"] == 2
    assert captured["kwargs"]["base_delay_sec"] == 0.5
    assert captured["kwargs"]["timeout"] == 45.0
    assert captured["kwargs"]["max_tokens"] == (
        visual_transport.VISUAL_OUTPUT_MAX_TOKENS
    )
    assert captured["messages"][0]["content"][1]["image_url"]["url"] == (
        "data:image/jpeg;base64,encoded-image"
    )


def test_observe_image_rejects_nonempty_token_limited_reply(monkeypatch):
    monkeypatch.setattr(
        vision_observer.provider_client,
        "reliable_chat_completion_isolated",
        lambda *_args, **_kwargs: {
            "reply": "A partial but nonempty observation.",
            "stop_reason": "MAX_TOKENS",
        },
    )

    with pytest.raises(vision_observer.VisionObserverError) as caught:
        vision_observer.observe_image(
            object(), image_mime="image/png", image_b64="encoded-image"
        )

    assert caught.value.error_code == "vision_model_failed"
    assert caught.value.reason == "output_truncated"
    assert caught.value.detail == "output_truncated"
    assert caught.value.retryable is False


@pytest.mark.parametrize("stop_reason", [None, "", "provider_new_reason"])
def test_observe_image_accepts_nonempty_reply_with_unknown_stop_reason(
    monkeypatch, stop_reason
):
    monkeypatch.setattr(
        vision_observer.provider_client,
        "reliable_chat_completion_isolated",
        lambda *_args, **_kwargs: {
            "reply": "A complete observation.",
            "stop_reason": stop_reason,
        },
    )

    assert vision_observer.observe_image(
        object(), image_mime="image/png", image_b64="encoded-image"
    ) == "A complete observation."


def test_observe_image_exposes_safe_auth_failure(monkeypatch):
    def fail(*_args, **_kwargs):
        raise provider_client.ProviderError("raw provider body", status_code=401)

    monkeypatch.setattr(
        vision_observer.provider_client,
        "reliable_chat_completion_isolated",
        fail,
    )

    try:
        vision_observer.observe_image(
            object(),
            image_mime="image/jpeg",
            image_b64="encoded-image",
        )
    except vision_observer.VisionObserverError as exc:
        assert exc.error_code == "vision_model_auth_invalid"
        assert exc.status_code == 401
        assert exc.retryable is False
        assert "raw provider body" not in exc.detail
    else:
        raise AssertionError("expected a classified visual observer failure")


def test_observe_image_exposes_transient_exhaustion(monkeypatch):
    def fail(*_args, **_kwargs):
        exc = provider_client.ProviderError("temporary upstream", status_code=502)
        exc.feedling_error_class = "transient_exhausted"
        raise exc

    monkeypatch.setattr(
        vision_observer.provider_client,
        "reliable_chat_completion_isolated",
        fail,
    )

    try:
        vision_observer.observe_image(
            object(),
            image_mime="image/jpeg",
            image_b64="encoded-image",
        )
    except vision_observer.VisionObserverError as exc:
        assert exc.error_code == "vision_model_unavailable"
        assert exc.status_code == 502
        assert exc.retryable is True
    else:
        raise AssertionError("expected a classified visual observer failure")


@pytest.mark.parametrize("status_code", [408, 500])
def test_vision_classifier_keeps_internal_upstream_detail_separate(status_code):
    upstream_detail = f"UPSTREAM_{status_code}_DIAGNOSTIC"
    try:
        provider_client._raise_for_provider_status(
            provider_client.httpx.Response(status_code, text=upstream_detail)
        )
    except provider_client.ProviderError as exc:
        failure = vision_observer.classify_vision_error(exc)
    else:
        raise AssertionError("expected provider status failure")

    assert failure.error_code == "vision_model_unavailable"
    assert failure.status_code == status_code
    assert failure.upstream_detail == upstream_detail
    assert failure.detail == "ProviderError"
    assert upstream_detail not in str(failure)
    assert upstream_detail not in failure.detail


def test_observe_pinned_message_traces_three_image_failures_without_content(monkeypatch):
    events = []
    monkeypatch.setattr(
        vision_observer.debug_trace,
        "trace_event",
        lambda _store, **kwargs: events.append(kwargs),
    )
    monkeypatch.setattr(
        vision_observer.db,
        "model_api_route_get",
        lambda *_args: {
            "provider": "openrouter",
            "model": "vision/model",
            "base_url": "https://private-relay.example/secret",
        },
    )

    body, status = vision_observer.observe_pinned_message(
        _Store(error=RuntimeError("private reload failure")),
        {"message_id": "msg-image", "route_id": "route-vision"},
        caller_api_key=None,
    )
    assert (body["error"], status) == ("vision_image_unavailable", 502)
    assert events[-1]["summary"] == "chat_reload_failed"

    monkeypatch.setattr(
        vision_observer.cap_registry,
        "run_capability",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=False,
            error={"code": "capability_forbidden", "retryable": False},
            data=None,
        ),
    )
    body, status = vision_observer.observe_pinned_message(
        _Store([_image_row()]),
        {"message_id": "msg-image", "route_id": "route-vision"},
        caller_api_key=None,
    )
    assert (body["detail"], status) == ("capability_forbidden", 502)
    assert events[-1]["summary"] == "image_read_failed"

    monkeypatch.setattr(
        vision_observer.cap_registry,
        "run_capability",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=True,
            error=None,
            data={"image_b64": "", "user_text": "private image caption"},
        ),
    )
    body, status = vision_observer.observe_pinned_message(
        _Store([_image_row()]),
        {"message_id": "msg-image", "route_id": "route-vision"},
        caller_api_key=None,
    )
    assert (body["detail"], status) == ("image_body_missing", 502)
    assert events[-1]["summary"] == "image_body_missing"

    assert [event["type"] for event in events] == [
        "vision.observe.failed",
        "vision.observe.failed",
        "vision.observe.failed",
    ]
    serialized = json.dumps(events)
    assert "private reload failure" not in serialized
    assert "private image caption" not in serialized
    assert "private-relay.example" not in serialized


def test_observe_pinned_message_traces_provider_call_without_image_or_reply(monkeypatch):
    events = []
    monkeypatch.setattr(
        vision_observer.debug_trace,
        "trace_event",
        lambda _store, **kwargs: events.append(kwargs),
    )
    monkeypatch.setattr(
        vision_observer.db,
        "model_api_route_get",
        lambda *_args: {
            "provider": "openrouter",
            "model": "vision/model",
            "base_url": "https://private-relay.example/secret",
        },
    )
    monkeypatch.setattr(
        vision_observer.cap_registry,
        "run_capability",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=True,
            error=None,
            data={
                "image_b64": "private-image-bytes",
                "image_mime": "image/png",
            },
        ),
    )
    monkeypatch.setattr(
        vision_observer,
        "load_provider_config",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        vision_observer,
        "observe_image",
        lambda *_args, **_kwargs: "private observation reply",
    )

    body, status = vision_observer.observe_pinned_message(
        _Store([_image_row()]),
        {"message_id": "msg-image", "route_id": "route-vision"},
        caller_api_key=None,
    )

    assert status == 200
    assert body["observation"] == "private observation reply"
    assert [event["type"] for event in events] == [
        "vision.provider.called",
        "vision.provider.completed",
    ]
    assert events[0]["status"] == "started"
    assert events[1]["status"] == "ok"
    assert events[0]["detail"] == {
        "provider": "openrouter",
        "model": "vision/model",
    }
    serialized = json.dumps(events)
    assert "private-image-bytes" not in serialized
    assert "private observation reply" not in serialized
    assert "private-relay.example" not in serialized


def test_observe_pinned_failure_body_stays_out_of_tenant_response_and_trace(
    monkeypatch, caplog
):
    upstream_detail = "UPSTREAM_SECRET_NOT_TENANT_VISIBLE"
    events = []
    monkeypatch.setattr(
        vision_observer.debug_trace,
        "trace_event",
        lambda _store, **kwargs: events.append(kwargs),
    )
    monkeypatch.setattr(
        vision_observer.db,
        "model_api_route_get",
        lambda *_args: {
            "provider": "openrouter",
            "model": "vision/model",
            "base_url": "https://private-relay.example/secret",
        },
    )
    monkeypatch.setattr(
        vision_observer.cap_registry,
        "run_capability",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=True,
            error=None,
            data={"image_b64": "private-image-bytes", "image_mime": "image/png"},
        ),
    )
    monkeypatch.setattr(
        vision_observer,
        "load_provider_config",
        lambda *_args, **_kwargs: object(),
    )
    failure = vision_observer.VisionObserverError(
        "vision_model_unavailable",
        status_code=500,
        retryable=True,
        detail="ProviderError",
        upstream_detail=upstream_detail,
    )
    monkeypatch.setattr(
        vision_observer,
        "observe_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )

    with caplog.at_level("WARNING", logger=vision_observer.log.name):
        body, status = vision_observer.observe_pinned_message(
            _Store([_image_row()]),
            {"message_id": "msg-image", "route_id": "route-vision"},
            caller_api_key=None,
        )

    assert status == 502
    assert body == {
        "error": "vision_observer_failed",
        "detail": "ProviderError",
        "error_class": "vision_model_unavailable",
        "status_code": 500,
        "retryable": True,
        "provider": "openrouter",
        "model": "vision/model",
    }
    assert events[-1]["detail"]["status_code"] == 500
    assert upstream_detail not in json.dumps({"body": body, "events": events})
    assert upstream_detail in caplog.text


def test_observe_pinned_truncation_returns_stable_nonretryable_reason(monkeypatch):
    events = []
    monkeypatch.setattr(
        vision_observer.debug_trace,
        "trace_event",
        lambda _store, **kwargs: events.append(kwargs),
    )
    monkeypatch.setattr(
        vision_observer.db,
        "model_api_route_get",
        lambda *_args: {"provider": "openrouter", "model": "vision/model"},
    )
    monkeypatch.setattr(
        vision_observer.cap_registry,
        "run_capability",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=True,
            error=None,
            data={"image_b64": "private-image-bytes", "image_mime": "image/png"},
        ),
    )
    monkeypatch.setattr(
        vision_observer,
        "load_provider_config",
        lambda *_args, **_kwargs: object(),
    )
    failure = vision_observer.classify_vision_error(
        visual_transport.VisualOutputTruncated()
    )
    monkeypatch.setattr(
        vision_observer,
        "observe_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )

    body, status = vision_observer.observe_pinned_message(
        _Store([_image_row()]),
        {"message_id": "msg-image", "route_id": "route-vision"},
        caller_api_key=None,
    )

    assert status == 502
    assert body["error_class"] == "vision_model_failed"
    assert body["reason"] == "output_truncated"
    assert body["retryable"] is False
    assert events[-1]["detail"]["reason"] == "output_truncated"
