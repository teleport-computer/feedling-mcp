import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from hosted import vision_observer
import provider_client


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
