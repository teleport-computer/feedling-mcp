import sys
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
        read_images=_fake_reader({"m1": {"image_mime": "image/png", "image_b64": "AAAA"}}))
    blocks = out[0]["content"]
    assert blocks[0] == {"type": "text", "text": "这个报告哪里有问题"}
    assert blocks[1] == {"type": "image_url",
                         "image_url": {"url": "data:image/png;base64,AAAA"}}


def test_inject_omits_text_block_for_the_bare_image_marker():
    """`[image]` is our own placeholder, not something the user wrote. Don't send it."""
    tail = [_img_row("m1")]
    out = worker._inject_tail_images(
        tail, user_id="u",
        read_images=_fake_reader({"m1": {"image_mime": "image/jpeg", "image_b64": "AAAA"}}))
    assert out[0]["content"] == [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}]


def test_inject_only_takes_the_most_recent_N_images():
    tail = [_img_row(f"m{i}") for i in range(5)]
    payload = {f"m{i}": {"image_mime": "image/jpeg", "image_b64": "AAAA"} for i in range(5)}
    out = worker._inject_tail_images(tail, user_id="u", read_images=_fake_reader(payload))
    injected = [i for i, r in enumerate(out) if isinstance(r["content"], list)]
    assert injected == [3, 4]                       # newest _TAIL_IMAGE_LIMIT=2
    assert out[0]["content"] == "[image]"           # older rows stay text
    assert worker._TAIL_IMAGE_LIMIT == 2


def test_inject_skips_oversized_image_and_keeps_text():
    tail = [_img_row("m1", caption="看看这个")]
    big = "A" * (worker._IMAGE_MAX_B64_CHARS + 1)
    out = worker._inject_tail_images(
        tail, user_id="u",
        read_images=_fake_reader({"m1": {"image_mime": "image/jpeg", "image_b64": big}}))
    assert out[0]["content"] == "看看这个"           # degraded to text, turn still answers


def test_inject_degrades_silently_when_reader_raises():
    def _boom(user_id, message_ids):
        raise RuntimeError("enclave down")
    tail = [_img_row("m1", caption="看看这个")]
    out = worker._inject_tail_images(tail, user_id="u", read_images=_boom)
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
        read_images=_fake_reader({"m1": {"image_mime": "image/jpeg", "image_b64": "AAAA"}}))
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

    def observe(user_id, targets):
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


def test_dedicated_observer_failure_is_explicit_and_never_falls_back():
    tail = [{**_img_row("m1"), "vision_route_id": "vision-route"}]

    def observe(_user_id, _targets):
        raise RuntimeError("provider down")

    with pytest.raises(worker.DedicatedVisionUnavailable):
        worker._inject_tail_images(
            tail,
            user_id="u",
            read_images=lambda *_args: pytest.fail("raw fallback is forbidden"),
            active_image_ids={"m1"},
            read_vision_observations=observe,
        )


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
