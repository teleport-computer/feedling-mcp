import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import worker


def _img_row(mid, caption="[image]", mime="image/jpeg"):
    return {"id": mid, "ts": 1.0, "role": "user", "content": caption,
            "has_image": True, "image_mime": mime}


def _fake_reader(payload):
    def _read(user_id, message_ids):
        return {mid: payload[mid] for mid in message_ids if mid in payload}
    return _read


def test_inject_builds_openai_content_blocks_with_caption_first():
    tail = [_img_row("m1", caption="这个报告哪里有问题")]
    out = worker._inject_tail_images(
        tail, user_id="u",
        read_images=_fake_reader({"m1": {"image_mime": "image/png", "image_b64": "AAAA"}}),
        active_image_ids={"m1"})
    blocks = out[0]["content"]
    assert blocks[0] == {"type": "text", "text": "这个报告哪里有问题"}
    assert blocks[1] == {"type": "image_url",
                         "image_url": {"url": "data:image/png;base64,AAAA"}}


def test_inject_builds_one_native_block_per_image_with_one_caption():
    tail = [_img_row("m1", caption="compare")]
    out = worker._inject_tail_images(
        tail,
        user_id="u",
        read_images=_fake_reader({"m1": {"images": [
            {"image_mime": "image/png", "image_b64": "AAAA"},
            {"image_mime": "image/webp", "image_b64": "BBBB"},
        ]}}),
        active_image_ids={"m1"},
    )
    assert out[0]["content"] == [
        {"type": "text", "text": "compare"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "image_url", "image_url": {"url": "data:image/webp;base64,BBBB"}},
    ]


def test_inject_omits_text_block_for_the_bare_image_marker():
    """`[image]` is our own placeholder, not something the user wrote. Don't send it."""
    tail = [_img_row("m1")]
    out = worker._inject_tail_images(
        tail, user_id="u",
        read_images=_fake_reader({"m1": {"image_mime": "image/jpeg", "image_b64": "AAAA"}}),
        active_image_ids={"m1"})
    assert out[0]["content"] == [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}]


def test_inject_only_takes_the_most_recent_N_images():
    tail = [_img_row(f"m{i}") for i in range(5)]
    payload = {f"m{i}": {"image_mime": "image/jpeg", "image_b64": "AAAA"} for i in range(5)}
    out = worker._inject_tail_images(
        tail,
        user_id="u",
        read_images=_fake_reader(payload),
        active_image_ids={f"m{i}" for i in range(5)},
    )
    injected = [i for i, r in enumerate(out) if isinstance(r["content"], list)]
    assert injected == [3, 4]                       # newest _TAIL_IMAGE_LIMIT=2
    assert out[0]["content"] == "[image]"           # older rows stay text
    assert worker._TAIL_IMAGE_LIMIT == 2


def test_inject_skips_oversized_image_and_keeps_text():
    tail = [_img_row("m1", caption="看看这个")]
    big = "A" * (worker._IMAGE_MAX_B64_CHARS + 1)
    out = worker._inject_tail_images(
        tail, user_id="u",
        read_images=_fake_reader({"m1": {"image_mime": "image/jpeg", "image_b64": big}}),
        active_image_ids={"m1"})
    assert out[0]["content"] == "看看这个"           # degraded to text, turn still answers


def test_inject_degrades_silently_when_reader_raises():
    def _boom(user_id, message_ids):
        raise RuntimeError("enclave down")
    tail = [_img_row("m1", caption="看看这个")]
    out = worker._inject_tail_images(
        tail,
        user_id="u",
        read_images=_boom,
        active_image_ids={"m1"},
    )
    assert out[0]["content"] == "看看这个"           # no-filler: never fail the turn


def test_inject_is_a_noop_without_a_reader_or_without_images():
    tail = [{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]
    assert worker._inject_tail_images(tail, user_id="u", read_images=None) == tail
    assert worker._inject_tail_images(tail, user_id="u", read_images=_fake_reader({})) == tail


def test_inject_does_not_mutate_the_input_tail():
    """compaction shares read_tail's rows; mutating them would poison the summarizer."""
    tail = [_img_row("m1", caption="c")]
    original = dict(tail[0])
    worker._inject_tail_images(
        tail, user_id="u",
        read_images=_fake_reader({"m1": {"image_mime": "image/jpeg", "image_b64": "AAAA"}}),
        active_image_ids={"m1"})
    assert tail[0] == original


def test_injection_cap_covers_any_image_ingestion_accepts():
    """Regression: the V2 injection b64 cap must cover the LARGEST image that
    ingestion accepts, or images in the ~1.5–2.0MB raw dead zone are stored at
    send time then silently dropped to text-only at turn time — the model then
    says "没收到图片". The bug was a unit mismatch: ingestion caps RAW BYTES
    (MODEL_API_MAX_IMAGE_BYTES), injection caps B64 CHARS (~4/3 larger), and both
    used the literal 2_000_000. worker.py must not import hosted, so the default
    is a derived hardcode; this cross-module test is what keeps the two in sync."""
    from hosted.turn import MODEL_API_MAX_IMAGE_BYTES

    # base64 length of N raw bytes is ceil(N/3)*4.
    max_ingested_b64_len = ((MODEL_API_MAX_IMAGE_BYTES + 2) // 3) * 4
    assert worker._IMAGE_MAX_B64_CHARS >= max_ingested_b64_len, (
        f"injection cap {worker._IMAGE_MAX_B64_CHARS} < b64 length "
        f"{max_ingested_b64_len} of the ingestion cap "
        f"{MODEL_API_MAX_IMAGE_BYTES} bytes — images in the dead zone are "
        f"accepted at send but dropped to text-only at the turn")


def test_dedicated_image_becomes_untrusted_observation_not_raw_pixels():
    tail = [{
        **_img_row("m1", caption="这张图怎么了"),
        "vision_route_id": "vision-route",
    }]
    raw_reads = []
    observer_calls = []

    def read_images(user_id, message_ids):
        raw_reads.append((user_id, message_ids))
        return {"m1": {"image_mime": "image/png", "image_b64": "RAW"}}

    def observe(user_id, targets, **_kwargs):
        observer_calls.append((user_id, targets))
        return {"m1": "A blue chart with a rising line."}

    out = worker._inject_tail_images(
        tail,
        user_id="u",
        read_images=read_images,
        active_image_ids={"m1"},
        read_vision_observations=observe,
    )

    assert raw_reads == []
    assert observer_calls == [(
        "u",
        [{"message_id": "m1", "route_id": "vision-route"}],
    )]
    assert isinstance(out[0]["content"], str)
    assert "UNTRUSTED VISUAL OBSERVATION" in out[0]["content"]
    assert "A blue chart with a rising line." in out[0]["content"]
    assert "image_url" not in out[0]["content"]


def test_dedicated_persistent_failure_never_sends_pixels_to_main():
    tail = [{**_img_row("m1"), "vision_route_id": "vision-route"}]

    def observe(_user_id, _targets, **_kwargs):
        return worker.VisionObservationBatch(
            outcomes={
                "m1": worker.VisionObservationOutcome(
                    error_code="vision_model_auth_invalid",
                    provider="dedicated",
                    model="vision/model",
                    status_code=401,
                )
            },
            absolute_deadline=time.monotonic() + 30,
            main_vision_verified=True,
        )

    with pytest.raises(worker.DedicatedVisionUnavailable) as caught:
        worker._inject_tail_images(
            tail,
            user_id="u",
            read_images=lambda *_args: pytest.fail("raw fallback is forbidden"),
            active_image_ids={"m1"},
            read_vision_observations=observe,
        )

    assert caught.value.error_code == "vision_model_auth_invalid"
    assert caught.value.provider == "dedicated"
    assert caught.value.model == "vision/model"
    assert caught.value.status_code == 401


def test_dedicated_observer_failure_keeps_safe_reason_code():
    tail = [{**_img_row("m1"), "vision_route_id": "vision-route"}]

    class ObserverFailure(RuntimeError):
        error_code = "vision_model_auth_invalid"
        status_code = 401
        upstream_detail = "UPSTREAM_SECRET_NOT_TENANT_VISIBLE"

    with pytest.raises(worker.DedicatedVisionUnavailable) as caught:
        worker._inject_tail_images(
            tail,
            user_id="u",
            read_images=lambda *_args: pytest.fail("raw fallback is forbidden"),
            active_image_ids={"m1"},
            read_vision_observations=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                ObserverFailure("secret provider response")
            ),
        )

    assert caught.value.error_code == "vision_model_auth_invalid"
    assert caught.value.status_code == 401
    assert caught.value.upstream_detail == "UPSTREAM_SECRET_NOT_TENANT_VISIBLE"
    assert worker._safe_failure_code("turn_failed", caught.value) == (
        "turn_failed:vision_model_auth_invalid"
    )
    assert "UPSTREAM_SECRET_NOT_TENANT_VISIBLE" not in str(caught.value)


def test_historical_dedicated_image_is_not_observed_or_resent():
    tail = [{**_img_row("m1"), "vision_route_id": "vision-route"}]
    out = worker._inject_tail_images(
        tail,
        user_id="u",
        read_images=lambda *_args: pytest.fail("raw pixels must not be read"),
        active_image_ids=set(),
        read_vision_observations=lambda *_args: pytest.fail("observer must not rerun"),
    )

    assert out[0]["content"] == "[image]"


def test_historical_follow_main_image_is_not_resent():
    tail = [_img_row("m1", caption="之前那张图")]
    out = worker._inject_tail_images(
        tail,
        user_id="u",
        read_images=lambda *_args: pytest.fail("historical pixels must not be read"),
        active_image_ids=set(),
    )

    assert out[0]["content"] == "之前那张图"


def test_current_dedicated_image_does_not_rehydrate_historical_follow_main_pixels():
    tail = [
        _img_row("old", caption="旧图"),
        {
            **_img_row("current", caption="看看新图"),
            "vision_route_id": "vision-route",
        },
    ]

    out = worker._inject_tail_images(
        tail,
        user_id="u",
        read_images=lambda *_args: pytest.fail("historical pixels must not be read"),
        active_image_ids={"current"},
        read_vision_observations=lambda _user_id, _targets, **_kwargs: {
            "current": "A red square."
        },
    )

    assert out[0]["content"] == "旧图"
    assert "UNTRUSTED VISUAL OBSERVATION" in out[1]["content"]
    assert "A red square." in out[1]["content"]


def test_verified_main_fallback_reads_only_failed_target_pixels_in_mixed_batch():
    tail = [
        {**_img_row("ok"), "vision_route_id": "vision-a"},
        {**_img_row("fallback"), "vision_route_id": "vision-b"},
    ]
    deadline = time.monotonic() + 30
    reads = []
    traces = []
    state = worker.VisionFallbackState()
    observer_calls = 0

    def observe(_user_id, _targets, **kwargs):
        nonlocal observer_calls
        observer_calls += 1
        assert kwargs["main_provider_config"] == "main-config"
        return worker.VisionObservationBatch(
            outcomes={
                "ok": worker.VisionObservationOutcome(
                    observation="A green checkmark."
                ),
                "fallback": worker.VisionObservationOutcome(
                    error_code="vision_model_unavailable",
                    provider="dedicated",
                    model="vision/model",
                    status_code=502,
                ),
            },
            absolute_deadline=deadline,
            main_vision_verified=True,
        )

    def read_images(user_id, message_ids):
        reads.append((user_id, list(message_ids)))
        return {
            "fallback": {"image_mime": "image/png", "image_b64": "RAW-FALLBACK"},
            "ok": {"image_mime": "image/png", "image_b64": "RAW-MUST-NOT-SEND"},
        }

    out = worker._inject_tail_images(
        tail,
        user_id="u",
        read_images=read_images,
        active_image_ids={"ok", "fallback"},
        read_vision_observations=observe,
        main_provider_config="main-config",
        vision_fallback_state=state,
        emit_debug_trace=lambda user_id, event_type, **kwargs: traces.append(
            {"user_id": user_id, "type": event_type, **kwargs}
        ),
    )

    assert reads == [("u", ["fallback"])]
    assert isinstance(out[0]["content"], str)
    assert "A green checkmark." in out[0]["content"]
    assert "RAW-MUST-NOT-SEND" not in json.dumps(out)
    assert out[1]["content"][-1]["image_url"]["url"] == (
        "data:image/png;base64,RAW-FALLBACK"
    )
    assert state.absolute_deadline == deadline
    assert state.selected_count == 1
    assert traces[0]["type"] == "vision.fallback.evaluated"
    assert traces[0]["detail"] == {
        "eligible_failure_count": 1,
        "main_vision_verified": True,
        "remaining_budget": "positive",
        "selected_count": 1,
        "skipped_reason": "none",
    }
    serialized = json.dumps(traces)
    assert "RAW-FALLBACK" not in serialized
    assert "RAW-MUST-NOT-SEND" not in serialized
    assert "A green checkmark." not in serialized
    assert "message_id" not in serialized

    rebuilt = worker._inject_tail_images(
        tail,
        user_id="u",
        read_images=read_images,
        active_image_ids={"ok", "fallback"},
        read_vision_observations=observe,
        main_provider_config="main-config",
        vision_fallback_state=state,
        emit_debug_trace=lambda user_id, event_type, **kwargs: traces.append(
            {"user_id": user_id, "type": event_type, **kwargs}
        ),
    )

    assert rebuilt == out
    assert observer_calls == 1
    assert state.absolute_deadline == deadline
    assert state.selected_count == 1
    assert len(traces) == 1
    assert reads == [("u", ["fallback"]), ("u", ["fallback"])]


@pytest.mark.parametrize(
    ("error_code", "reason", "eligible"),
    [
        ("vision_model_rate_limited", "", True),
        ("vision_model_unavailable", "", True),
        ("vision_model_empty_response", "", True),
        ("vision_model_failed", "output_truncated", True),
        ("vision_model_failed", "", False),
        ("vision_model_auth_invalid", "", False),
        ("vision_model_quota_insufficient", "", False),
        ("vision_model_not_found", "", False),
        ("vision_model_incompatible", "", False),
        ("vision_model_not_ready", "", False),
    ],
)
def test_main_fallback_eligibility_is_exact(error_code, reason, eligible):
    import vision_policy

    assert vision_policy.is_main_fallback_eligible(error_code, reason) is eligible


def test_eligible_failure_does_not_fallback_without_exact_main_verification():
    tail = [{**_img_row("m1"), "vision_route_id": "vision-route"}]

    with pytest.raises(worker.DedicatedVisionUnavailable) as caught:
        worker._inject_tail_images(
            tail,
            user_id="u",
            read_images=lambda *_args: pytest.fail("raw fallback is forbidden"),
            active_image_ids={"m1"},
            read_vision_observations=lambda *_args, **_kwargs: (
                worker.VisionObservationBatch(
                    outcomes={
                        "m1": worker.VisionObservationOutcome(
                            error_code="vision_model_unavailable"
                        )
                    },
                    absolute_deadline=time.monotonic() + 30,
                    main_vision_verified=False,
                )
            ),
        )

    assert caught.value.error_code == "vision_model_unavailable"


def test_eligible_failure_does_not_fallback_after_shared_deadline_expires():
    tail = [{**_img_row("m1"), "vision_route_id": "vision-route"}]

    with pytest.raises(worker.DedicatedVisionUnavailable) as caught:
        worker._inject_tail_images(
            tail,
            user_id="u",
            read_images=lambda *_args: pytest.fail("raw fallback is forbidden"),
            active_image_ids={"m1"},
            read_vision_observations=lambda *_args, **_kwargs: (
                worker.VisionObservationBatch(
                    outcomes={
                        "m1": worker.VisionObservationOutcome(
                            error_code="vision_model_unavailable"
                        )
                    },
                    absolute_deadline=time.monotonic() - 1,
                    main_vision_verified=True,
                )
            ),
        )

    assert caught.value.error_code == "vision_model_unavailable"
