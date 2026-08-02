import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from hosted import vision_observer
import httpx
import provider_client
from provider_attempt_accounting import (
    AttemptLane,
    AttemptState,
    ProviderAttemptContext,
)


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
        "reliable_chat_completion",
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
    assert captured["messages"][0]["content"][1]["image_url"]["url"] == (
        "data:image/jpeg;base64,encoded-image"
    )


def test_observe_image_accounts_each_real_sync_retry(monkeypatch):
    recorded = []
    statuses = [503, 200]

    def handler(_request: httpx.Request) -> httpx.Response:
        status = statuses.pop(0)
        if status == 503:
            return httpx.Response(503, json={"error": {"message": "retry"}})
        return httpx.Response(200, json={
            "choices": [{
                "message": {"content": "A red bicycle."},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 9, "completion_tokens": 3},
        })

    monkeypatch.setattr(
        provider_client,
        "_shared_client",
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(provider_client, "record_provider_attempt", recorded.append)
    monkeypatch.setattr(provider_client.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(provider_client.random, "uniform", lambda _a, _b: 0)
    config = provider_client.ProviderConfig(
        provider="openrouter",
        model="vision-model",
        api_key="vision-key",
        base_url="https://relay.example/v1",
        provider_attempt_context=ProviderAttemptContext(
            user_id="usr_sync_vision",
            lane=AttemptLane.CHAT,
            job_id=93,
            call_id="v2job:93:vision-tool:309ad63028c1da2e",
        ),
    )

    result = vision_observer.observe_image(
        config,
        image_mime="image/jpeg",
        image_b64="encoded-image",
    )

    assert result == "A red bicycle."
    assert [event.state for event in recorded] == [
        AttemptState.STARTED,
        AttemptState.COMPLETED,
        AttemptState.STARTED,
        AttemptState.COMPLETED,
    ]
    assert [
        (event.outer_attempt_ordinal, event.inner_attempt_ordinal)
        for event in recorded
        if event.state is AttemptState.COMPLETED
    ] == [(1, 1), (2, 1)]


def test_observe_image_exposes_safe_auth_failure(monkeypatch):
    def fail(*_args, **_kwargs):
        raise provider_client.ProviderError("raw provider body", status_code=401)

    monkeypatch.setattr(
        vision_observer.provider_client,
        "reliable_chat_completion",
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
        "reliable_chat_completion",
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
