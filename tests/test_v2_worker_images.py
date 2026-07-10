import sys
from pathlib import Path

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
