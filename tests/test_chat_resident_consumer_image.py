"""
Image turn pipeline tests for tools/chat_resident_consumer.py
=============================================================

Covers:
  - payload decode from image_b64
  - file write to IMAGE_TEMP_DIR
  - CLI template injection of image paths

Run with:
    cd backend && PYTHONPATH=. /path/to/venv/python -m pytest \
        ../tests/test_chat_resident_consumer_image.py -v
"""

import base64
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Module bootstrap — set required env vars BEFORE importing consumer.
# consumer reads env at module scope; these must exist first.
# ---------------------------------------------------------------------------

_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_image_checkpoint.json",
}

for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

# Ensure repo root + backend on path (mirrors existing test suite).
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

# Stub content_encryption when backend tree is absent.
try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules.setdefault("content_encryption", _fake_enc)

import tools.chat_resident_consumer as crc  # noqa: E402  (after env setup)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_JPEG_MAGIC = b"\xff\xd8\xff\xe0" + b"\x00" * 12  # minimal fake JPEG header
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8   # minimal fake PNG header


def _make_image_msg(
    ts: float = 1.0,
    image_bytes: bytes = _JPEG_MAGIC,
    mime: str = "image/jpeg",
    role: str = "user",
    msg_id: str = "test-img-01",
    vision_route_id: str = "",
) -> dict:
    message = {
        "id": msg_id,
        "role": role,
        "content": "",
        "content_type": "image",
        "ts": ts,
        "image_b64": base64.b64encode(image_bytes).decode("ascii"),
        "image_mime": mime,
    }
    if vision_route_id:
        message["vision_route_id"] = vision_route_id
    return message


# ---------------------------------------------------------------------------
# Test 1: payload decode from image_b64
# ---------------------------------------------------------------------------

def test_image_payloads_decoded_from_msg():
    """_image_payloads_from_msg returns at least one payload with mime + data."""
    msg = _make_image_msg(image_bytes=_JPEG_MAGIC, mime="image/jpeg")
    payloads = crc._image_payloads_from_msg(msg)

    assert payloads, "expected at least one payload for an image message"
    p = payloads[0]
    assert p.get("mime_type") == "image/jpeg", f"unexpected mime_type: {p}"
    assert p.get("data"), "payload data (base64) must be non-empty"
    assert p.get("data_url", "").startswith("data:image/jpeg;base64,"), (
        f"data_url malformed: {p.get('data_url', '')[:80]}"
    )

    # Round-trip: decoded bytes must equal the original image bytes.
    decoded = base64.b64decode(p["data"])
    assert decoded == _JPEG_MAGIC, "round-trip bytes mismatch"


def test_image_payloads_empty_when_no_image_b64():
    """_image_payloads_from_msg returns [] when image_b64 is absent."""
    msg = {"role": "user", "content": "no image here", "ts": 2.0}
    payloads = crc._image_payloads_from_msg(msg)
    assert payloads == []


def test_image_payloads_handles_data_url_prefix():
    """image_b64 sent as a data-URL (data:image/png;base64,...) is stripped cleanly."""
    raw = _PNG_MAGIC
    b64 = base64.b64encode(raw).decode("ascii")
    data_url = f"data:image/png;base64,{b64}"
    msg = {
        "role": "user",
        "content": "",
        "content_type": "image",
        "ts": 3.0,
        "image_b64": data_url,
        "image_mime": "image/png",
    }
    payloads = crc._image_payloads_from_msg(msg)
    assert payloads, "expected payload from data-URL-prefixed image_b64"
    assert payloads[0]["mime_type"] == "image/png"
    assert base64.b64decode(payloads[0]["data"]) == raw


# ---------------------------------------------------------------------------
# Test 2: file write to IMAGE_TEMP_DIR
# ---------------------------------------------------------------------------

def test_image_file_paths_written(tmp_path):
    """_image_file_paths_for_msg writes image bytes to IMAGE_TEMP_DIR.
    The returned paths must exist and contain the original image bytes.
    """
    msg = _make_image_msg(image_bytes=_JPEG_MAGIC, mime="image/jpeg", msg_id="img-write-01")

    with patch.object(crc, "IMAGE_TEMP_DIR", tmp_path):
        paths = crc._image_file_paths_for_msg(msg)

    assert paths, "expected at least one file path to be returned"
    for p in paths:
        path = Path(p)
        assert path.exists(), f"image file not written: {p}"
        assert path.stat().st_size > 0, f"image file is empty: {p}"
        assert path.read_bytes() == _JPEG_MAGIC, f"file content mismatch: {p}"


def test_image_file_paths_written_png(tmp_path):
    """PNG images get a .png extension."""
    msg = _make_image_msg(image_bytes=_PNG_MAGIC, mime="image/png", msg_id="img-png-01")

    with patch.object(crc, "IMAGE_TEMP_DIR", tmp_path):
        paths = crc._image_file_paths_for_msg(msg)

    assert paths, "expected at least one file path"
    assert all(p.endswith(".png") for p in paths), (
        f"expected .png extension for PNG mime; got: {paths}"
    )


def test_image_file_paths_empty_when_no_image(tmp_path):
    """_image_file_paths_for_msg returns [] when msg has no image_b64."""
    msg = {"role": "user", "content": "no image", "ts": 5.0}
    with patch.object(crc, "IMAGE_TEMP_DIR", tmp_path):
        paths = crc._image_file_paths_for_msg(msg)
    assert paths == []


# ---------------------------------------------------------------------------
# Test 3: CLI template injects image paths into argv
# ---------------------------------------------------------------------------

def test_cli_template_includes_image_paths():
    """_render_cli_template injects {image_path} / {image_paths} into argv.

    When AGENT_CLI_CMD contains the tokens, the rendered command must
    include the actual image file paths at the token positions.
    """
    fake_cmd = "claude --print {message} --session {session_id} --image {image_path}"
    image_paths = ["/tmp/feedling_chat_images/test-img-01_0.jpg"]

    with patch.object(crc, "AGENT_CLI_CMD", fake_cmd):
        argv, _ = crc._render_cli_template(
            message="describe this image",
            sid="sid-001",
            image_paths=image_paths,
        )

    # argv must be a list
    assert isinstance(argv, list) and argv, "rendered CLI must be a non-empty list"
    joined = " ".join(argv)
    assert image_paths[0] in joined, (
        f"image path not found in rendered CLI argv: {joined}"
    )


def test_cli_template_includes_all_image_paths():
    """_render_cli_template expands {image_paths} (plural) to all paths."""
    fake_cmd = "myagent --files {image_paths} -- {message}"
    image_paths = [
        "/tmp/feedling_chat_images/img_0.jpg",
        "/tmp/feedling_chat_images/img_1.jpg",
    ]

    with patch.object(crc, "AGENT_CLI_CMD", fake_cmd):
        argv, _ = crc._render_cli_template(
            message="two images",
            sid="",
            image_paths=image_paths,
        )

    joined = " ".join(argv)
    for p in image_paths:
        assert p in joined, f"path {p!r} missing from argv: {joined}"


def test_cli_template_empty_image_path_when_no_images():
    """When no images are provided, {image_path} expands to empty string."""
    fake_cmd = "agent {message} --img {image_path}"

    with patch.object(crc, "AGENT_CLI_CMD", fake_cmd):
        argv, _ = crc._render_cli_template(
            message="hello",
            sid="",
            image_paths=[],
        )

    # The --img arg will be present but its value will be empty string.
    # This ensures no crash and no residual token literal.
    joined = " ".join(argv)
    assert "__FEEDLING_IMAGE_PATH__" not in joined
    assert "{image_path}" not in joined


# ---------------------------------------------------------------------------
# Test 4: process_messages routes image turn to call_agent with payloads
# ---------------------------------------------------------------------------

def test_process_messages_image_turn_calls_agent_with_payloads(tmp_path):
    """_process_messages passes image_payloads + image_paths to call_agent
    when content_type == 'image'.
    """
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    msg = _make_image_msg(ts=9000.0, image_bytes=_JPEG_MAGIC, msg_id="img-proc-01")
    captured = {}

    def fake_call(message, images=None, image_paths=None, trace_id=None, **kwargs):
        captured["message"] = message
        captured["images"] = images
        captured["image_paths"] = image_paths
        return {"messages": ["I see the image."]}

    with patch.object(crc, "IMAGE_TEMP_DIR", tmp_path), \
         patch.object(crc, "call_agent", side_effect=fake_call), \
         patch.object(crc, "post_reply", return_value={"id": "reply-img-01"}):
        result_ts = crc._process_messages([msg])

    assert result_ts == pytest.approx(9000.0)
    assert captured.get("images"), "call_agent must receive non-empty images list"
    assert captured.get("image_paths"), "call_agent must receive non-empty image_paths list"
    # Verify the image payload has the expected fields
    payload = captured["images"][0]
    assert "mime_type" in payload
    assert "data" in payload
    assert "data_url" in payload


def test_process_messages_image_turn_preserves_caption(tmp_path):
    """带文字说明的图片 turn：content（caption）不被 IMAGE_PLACEHOLDER 覆盖，
    agent 收到用户的真实问题而非占位符（Codex P2 修复）。"""
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    msg = _make_image_msg(ts=9100.0, image_bytes=_JPEG_MAGIC, msg_id="img-cap-01")
    msg["content"] = "what is wrong here?"  # enclave history 解出的 caption
    captured = {}

    def fake_call(message, images=None, image_paths=None, trace_id=None, **kwargs):
        captured["message"] = message
        return {"messages": ["I see the image."]}

    with patch.object(crc, "IMAGE_TEMP_DIR", tmp_path), \
         patch.object(crc, "call_agent", side_effect=fake_call), \
         patch.object(crc, "post_reply", return_value={"id": "reply-cap-01"}):
        crc._process_messages([msg])

    assert "what is wrong here?" in captured.get("message", ""), (
        f"caption 应随图片传给 agent，实际 message={captured.get('message')!r}"
    )
    assert captured["message"] != crc.IMAGE_PLACEHOLDER, "content 不应被占位符整体覆盖"


def test_process_messages_image_turn_no_caption_uses_placeholder(tmp_path):
    """无文字的图片 turn 仍回退到 IMAGE_PLACEHOLDER（向后兼容）。"""
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    msg = _make_image_msg(ts=9200.0, image_bytes=_JPEG_MAGIC, msg_id="img-nocap-01")
    msg["content"] = ""  # 没有 caption
    captured = {}

    def fake_call(message, images=None, image_paths=None, trace_id=None, **kwargs):
        captured["message"] = message
        return {"messages": ["I see the image."]}

    with patch.object(crc, "IMAGE_TEMP_DIR", tmp_path), \
         patch.object(crc, "call_agent", side_effect=fake_call), \
         patch.object(crc, "post_reply", return_value={"id": "reply-nocap-01"}):
        crc._process_messages([msg])

    assert crc.IMAGE_PLACEHOLDER in captured.get("message", ""), (
        f"无 caption 时应含占位符，实际 {captured.get('message')!r}"
    )


# ── 已删除:四条测「runtime 抢先生图」的用例 ──────────────────────────────
# test_dedicated_image_route_failure_keeps_stable_settings_guidance
# test_native_agent_fallback_only_applies_when_dedicated_route_is_absent
# test_dedicated_image_route_stages_generated_media
# test_dedicated_image_success_delivers_without_waiting_for_main_model
#
# 最后一条的名字就是被废除的那个反模式本身:"delivers without waiting for
# main model" —— 图不经过主模型就直接发给用户,回复是系统写死的一句话。
# 现在生图由伴侣自己调用 generate-image 发起,它的话和图一起送达。
# 新行为的用例见 test_resident_image_autonomy.py。

def test_dedicated_vision_sends_only_observation_to_main_model(tmp_path):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    msg = _make_image_msg(
        ts=9300.0,
        image_bytes=_JPEG_MAGIC,
        msg_id="img-observe-01",
        vision_route_id="vision-route-01",
    )
    captured = {}

    def fake_call(message, images=None, image_paths=None, trace_id=None, **kwargs):
        captured.update(message=message, images=images, image_paths=image_paths)
        return {"messages": ["The screenshot shows a settings page."]}

    with patch.object(crc, "IMAGE_TEMP_DIR", tmp_path), \
         patch.object(crc, "_vision_observation", return_value="A settings page is visible."), \
         patch.object(crc, "call_agent", side_effect=fake_call), \
         patch.object(crc, "post_reply", return_value={"id": "reply-observe-01"}):
        result_ts = crc._process_messages([msg])

    assert result_ts == pytest.approx(9300.0)
    assert "UNTRUSTED VISUAL OBSERVATION" in captured["message"]
    assert "A settings page is visible." in captured["message"]
    assert not captured["images"]
    assert not captured["image_paths"]


def test_pi_vision_rejection_rotates_session_before_showing_model_guidance(
    tmp_path, monkeypatch
):
    """A rejected raw image must not poison later text turns in the Pi session."""
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    crc._agent_session_id_cache.clear()
    crc._agent_session_meta_cache.clear()
    msg = _make_image_msg(
        ts=9350.0,
        image_bytes=_JPEG_MAGIC,
        msg_id="img-pi-rejected-01",
    )

    monkeypatch.setattr(crc, "AGENT_MODE", "cli")
    monkeypatch.setattr(
        crc, "AGENT_CLI_CMD", "pi --mode json --session-id {session_id}"
    )
    monkeypatch.setattr(
        crc,
        "AGENT_SESSION_FILE_TEMPLATE",
        str(tmp_path / "session-{user_id}.json"),
    )
    monkeypatch.setitem(crc._whoami_cache, "user_id", "pi-vision-rejection")
    monkeypatch.setattr(crc, "_resident_chat_runtime_v2_enabled", lambda: False)
    monkeypatch.setattr(crc, "_prepend_io_cli_capability_catalog", lambda text: text)
    monkeypatch.setattr(
        crc, "_foreground_agent_message", lambda text, current_ts: text
    )
    monkeypatch.setattr(crc, "_worldbook_context_for_foreground", lambda _text: "")
    monkeypatch.setattr(crc, "_note_agent_turn_success", lambda: None)
    monkeypatch.setattr(
        crc,
        "call_agent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("unknown variant `image_url`, expected `text`")
        ),
    )
    monkeypatch.setattr(
        crc, "post_reply", lambda *_args, **_kwargs: {"id": "reply-pi-rejected"}
    )

    crc._save_agent_session_id("pi-session-with-rejected-image")
    with patch.object(crc, "IMAGE_TEMP_DIR", tmp_path / "images"):
        result_ts = crc._process_messages([msg])

    assert result_ts == pytest.approx(9350.0)
    assert crc._load_agent_session_id() == ""


@pytest.mark.parametrize(
    "resident_chat_v2_profile",
    [False, True],
    ids=["legacy-resident-profile", "v2-resident-profile"],
)
@pytest.mark.parametrize("dedicated_image", [False, True])
def test_pi_text_only_turn_recovers_from_session_with_rejected_image(
    tmp_path, monkeypatch, dedicated_image, resident_chat_v2_profile
):
    """Both resident V1 prompt profiles retry once in a clean Pi session."""
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    crc._agent_session_id_cache.clear()
    crc._agent_session_meta_cache.clear()
    if dedicated_image:
        msg = _make_image_msg(
            ts=9360.0,
            image_bytes=_JPEG_MAGIC,
            msg_id="img-pi-observed-retry-01",
            vision_route_id="vision-route-01",
        )
    else:
        msg = {
            "id": "text-after-image-rejection-01",
            "role": "user",
            "content": "咋了",
            "content_type": "text",
            "ts": 9360.0,
        }

    monkeypatch.setattr(crc, "AGENT_MODE", "cli")
    monkeypatch.setattr(
        crc, "AGENT_CLI_CMD", "pi --mode json --session-id {session_id}"
    )
    monkeypatch.setattr(
        crc,
        "AGENT_SESSION_FILE_TEMPLATE",
        str(tmp_path / "session-{user_id}.json"),
    )
    monkeypatch.setitem(crc._whoami_cache, "user_id", "pi-session-recovery")
    monkeypatch.setattr(
        crc,
        "_resident_chat_runtime_v2_enabled",
        lambda: resident_chat_v2_profile,
    )
    monkeypatch.setattr(crc, "_prepend_io_cli_capability_catalog", lambda text: text)
    monkeypatch.setattr(crc, "_worldbook_context_for_foreground", lambda _text: "")
    monkeypatch.setattr(crc, "_note_agent_turn_success", lambda: None)
    monkeypatch.setattr(
        crc, "_vision_observation", lambda *_args: "A blue chart is visible."
    )

    def bridge_clean_session(text, *, current_ts):
        if crc._load_agent_session_id():
            return text
        return f"{crc.FOREGROUND_CHAT_CONTEXT_HEADER}\n[image]\n---\n{text}"

    monkeypatch.setattr(crc, "_foreground_agent_message", bridge_clean_session)
    calls = []

    def fake_call(message, images=None, image_paths=None, **_kwargs):
        calls.append(
            {
                "message": message,
                "images": images,
                "image_paths": image_paths,
                "session_id": crc._load_agent_session_id(),
            }
        )
        if len(calls) == 1:
            raise RuntimeError("unknown variant `image_url`, expected `text`")
        return {"messages": ["现在能正常回复文字了。"]}

    replies = []
    monkeypatch.setattr(crc, "call_agent", fake_call)
    monkeypatch.setattr(
        crc,
        "post_reply",
        lambda reply, **kwargs: replies.append((reply, kwargs))
        or {"id": "reply-pi-recovered"},
    )

    crc._save_agent_session_id("pi-session-with-rejected-image")
    with patch.object(crc, "IMAGE_TEMP_DIR", tmp_path / "images"):
        result_ts = crc._process_messages([msg])

    assert result_ts == pytest.approx(9360.0)
    assert len(calls) == 2
    assert calls[0]["session_id"] == "pi-session-with-rejected-image"
    assert calls[1]["session_id"] == ""
    assert calls[1]["message"].startswith(crc.FOREGROUND_CHAT_CONTEXT_HEADER)
    assert not calls[1]["images"]
    assert not calls[1]["image_paths"]
    if dedicated_image:
        assert "UNTRUSTED VISUAL OBSERVATION" in calls[1]["message"]
        assert "A blue chart is visible." in calls[1]["message"]
    else:
        assert "咋了" in calls[1]["message"]
    assert replies[0][0] == "现在能正常回复文字了。"
    assert not replies[0][1].get("turn_failure_error_class")


def test_dedicated_vision_observer_failure_never_calls_main_model(tmp_path):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    msg = _make_image_msg(
        ts=9400.0,
        image_bytes=_JPEG_MAGIC,
        msg_id="img-observe-fail-01",
        vision_route_id="vision-route-01",
    )
    replies = []

    with patch.object(crc, "IMAGE_TEMP_DIR", tmp_path), \
         patch.object(
             crc,
             "_vision_observation",
             side_effect=crc.VisionObserverFailure(
                 "vision_model_unavailable",
                 status_code=502,
                 detail="ProviderError",
                 model="vision/model-1",
                 provider="openrouter",
             ),
         ), \
         patch.object(crc, "call_agent") as mock_call, \
         patch.object(
             crc,
             "post_reply",
             side_effect=lambda reply, **kwargs: replies.append((reply, kwargs)) or {"id": "reply-fail-01"},
         ):
        result_ts = crc._process_messages([msg])

    assert result_ts == pytest.approx(9400.0)
    mock_call.assert_not_called()
    assert len(replies) == 2
    carrier_text, carrier_kwargs = replies[0]
    assert "视觉模型" in carrier_text or "vision model" in carrier_text.lower()
    assert carrier_kwargs["turn_failure_error_class"] == "vision_model_unavailable"
    assert carrier_kwargs["turn_failure_blame"] == "provider_transient"
    assert carrier_kwargs["reply_to_message_id"] == "img-observe-fail-01"
    assert carrier_kwargs["turn_failure_model"] == "vision/model-1"
    assert carrier_kwargs["turn_failure_provider"] == "openrouter"
    notice_text, notice_kwargs = replies[1]
    assert "vision_model_unavailable" in notice_text
    assert "HTTP 502" in notice_text
    assert notice_kwargs["role"] == "system"
    assert notice_kwargs["notice_kind"] == "upstream_error"


# ---------------------------------------------------------------------------
# Bounded image transport (B)
#
# History used to come back with EVERY image body in the window inlined. Five
# stuck 1.4MB photos meant a 4.4MB response; the CVM egress cut it off mid-body
# ("peer closed connection ... received 196608, expected 4433378"), the resident
# skipped the whole cycle, the cursor never advanced — so the next window held
# the same five images, forever. Pixels now arrive one message at a time.
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def test_history_fetch_can_opt_out_of_image_bodies(monkeypatch):
    """include_image_body=false must reach the enclave — otherwise the caller is
    forced to take every image body in the window."""
    seen = {}

    class _Client:
        def get(self, url, params=None, headers=None):
            seen["url"] = url
            seen["params"] = params or {}
            return _FakeResp({"messages": []})

    monkeypatch.setattr(crc, "FEEDLING_ENCLAVE_URL", "http://enclave")
    monkeypatch.setattr(crc, "_ENCLAVE_CLIENT", _Client())

    crc.get_decrypted_history(since=1.0, limit=20, include_image_body=False)

    assert seen["params"]["include_image_body"] == "false"
    assert seen["url"].endswith("/v1/chat/history")


def test_history_fetch_includes_bodies_by_default(monkeypatch):
    seen = {}

    class _Client:
        def get(self, url, params=None, headers=None):
            seen["params"] = params or {}
            return _FakeResp({"messages": []})

    monkeypatch.setattr(crc, "FEEDLING_ENCLAVE_URL", "http://enclave")
    monkeypatch.setattr(crc, "_ENCLAVE_CLIENT", _Client())

    crc.get_decrypted_history(since=1.0, limit=20)

    assert "include_image_body" not in seen["params"]


def test_hydrate_pulls_each_omitted_body_by_id(monkeypatch):
    """One request per omitted body — a response can never exceed one image."""
    calls = []

    def fake_fetch(message_id):
        calls.append(message_id)
        return {
            "id": message_id,
            "role": "user",
            "ts": 1.0,
            "content_type": "image",
            "content": "what is wrong here?",
            "image_b64": base64.b64encode(_JPEG_MAGIC).decode("ascii"),
            "image_mime": "image/jpeg",
        }

    monkeypatch.setattr(crc, "_fetch_message_body_from_enclave", fake_fetch)

    out = crc._hydrate_omitted_bodies([
        {"id": "t1", "role": "user", "ts": 0.5, "content_type": "text",
         "content": "hi"},
        {"id": "i1", "role": "user", "ts": 1.0, "content_type": "image",
         "content": "what is wrong here?", "body_omitted": True,
         "image_omitted": True, "image_mime": "image/jpeg"},
    ])

    assert calls == ["i1"]                      # text row never fetched
    assert out[0]["content"] == "hi"
    assert crc._image_payloads_from_msg(out[1])  # pixels landed
    assert not out[1].get("body_omitted")
    assert not out[1].get("image_omitted")
    assert out[1]["content"] == "what is wrong here?"


def test_hydrate_failure_degrades_one_turn_not_the_cursor(monkeypatch):
    """A body that will not come back must leave the OTHER messages intact and
    the row still processable — the image branch routes its honest
    image-unavailable prompt. This is what keeps a bad body from wedging the
    cursor the way the batched window did."""
    monkeypatch.setattr(crc, "_fetch_message_body_from_enclave", lambda mid: None)

    out = crc._hydrate_omitted_bodies([
        {"id": "i1", "role": "user", "ts": 1.0, "content_type": "image",
         "content": "look", "body_omitted": True, "image_omitted": True},
        {"id": "t2", "role": "user", "ts": 2.0, "content_type": "text",
         "content": "still here"},
    ])

    assert len(out) == 2
    assert out[1]["content"] == "still here"        # unaffected
    assert out[0]["content"] == "look"              # caption survived
    assert crc._image_payloads_from_msg(out[0]) == []   # no pixels → honest prompt


def test_fetch_message_body_calls_the_enclave_single_body_route(monkeypatch):
    """Exercises the real function (not a stub) so a missing import or a bad URL
    can't hide behind a monkeypatched fetch."""
    seen = {}

    class _Client:
        def get(self, url, headers=None):
            seen["url"] = url
            return _FakeResp({"message": {"id": "i 1", "image_b64": "AAAA"}})

    monkeypatch.setattr(crc, "FEEDLING_ENCLAVE_URL", "http://enclave")
    monkeypatch.setattr(crc, "_ENCLAVE_CLIENT", _Client())

    msg = crc._fetch_message_body_from_enclave("i 1")

    assert msg == {"id": "i 1", "image_b64": "AAAA"}
    # id is percent-encoded into the path — a space must not split the URL
    assert seen["url"] == "http://enclave/v1/chat/messages/i%201/body"


def test_fetch_message_body_returns_none_on_transport_error(monkeypatch):
    class _Client:
        def get(self, url, headers=None):
            raise RuntimeError("peer closed connection")

    monkeypatch.setattr(crc, "FEEDLING_ENCLAVE_URL", "http://enclave")
    monkeypatch.setattr(crc, "_ENCLAVE_CLIENT", _Client())

    assert crc._fetch_message_body_from_enclave("i1") is None


# ---------------------------------------------------------------------------
# Codex P2: include_image_body=false omits ANY body over
# CHAT_HISTORY_INLINE_BODY_CT_MAX — not just images. A large TEXT message whose
# per-message body fetch fails would arrive content-less, fall into the
# "genuinely empty text" branch, get skipped, and advance the cursor — silently
# dropping the user's turn for good. Images have an honest-unavailable path;
# text needs one too.
# ---------------------------------------------------------------------------


def test_hydrate_marks_failed_body_as_unavailable(monkeypatch):
    monkeypatch.setattr(crc, "_fetch_message_body_from_enclave", lambda mid: None)

    out = crc._hydrate_omitted_bodies([
        {"id": "t1", "role": "user", "ts": 1.0, "content_type": "text",
         "body_omitted": True, "body_omitted_reason": "large_body_ct"},
    ])

    assert out[0]["body_unavailable"] is True


def test_large_text_turn_with_unfetchable_body_still_replies(tmp_path):
    """The turn must NOT be silently skipped: a dropped user message is
    unrecoverable, while an honest 'I couldn't read that' reply is not."""
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    msg = {
        "id": "txt-big-01", "role": "user", "ts": 9500.0, "content_type": "text",
        "content": "",                 # body never arrived
        "body_omitted": True, "body_omitted_reason": "large_body_ct",
        "body_unavailable": True,
    }
    captured = {}

    def fake_call(message, images=None, image_paths=None, trace_id=None, **kwargs):
        captured["message"] = message
        return {"messages": ["Sorry — I couldn't read that message."]}

    with patch.object(crc, "call_agent", side_effect=fake_call), \
         patch.object(crc, "post_reply", return_value={"id": "r-1"}) as mock_post:
        result_ts = crc._process_messages([msg])

    assert mock_post.called, "an unreadable-but-known message must still get a reply"
    assert captured.get("message"), "agent must be told the body is unavailable"
    assert result_ts == pytest.approx(9500.0)


def test_genuinely_empty_text_is_still_skipped(tmp_path):
    """The pre-existing contract holds: a message with no plaintext and no
    body_unavailable marker (no decrypt source at all) is still skipped — we do
    not invent a reply for content we never asked for."""
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    msg = {"id": "txt-empty-01", "role": "user", "ts": 9600.0,
           "content_type": "text", "content": ""}

    with patch.object(crc, "call_agent") as mock_agent, \
         patch.object(crc, "post_reply") as mock_post:
        crc._process_messages([msg])

    assert not mock_agent.called
    assert not mock_post.called


def test_file_turn_with_unfetchable_body_does_not_land_an_empty_file(tmp_path):
    """Same exposure as the text case, quieter: _prepare_file_for_agent decodes a
    missing file_b64 to b"" and would hand the agent a 0-byte document to describe.
    Say the bytes didn't arrive instead."""
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    msg = {
        "id": "file-gone-01", "role": "user", "ts": 9700.0, "content_type": "file",
        "content": "看看这个报告", "file_name": "report.docx",
        "file_mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "body_omitted": True, "body_unavailable": True,
    }
    captured = {}

    def fake_call(message, images=None, image_paths=None, trace_id=None, **kwargs):
        captured["message"] = message
        return {"messages": ["That file didn't come through."]}

    with patch.object(crc, "FILE_TEMP_DIR", tmp_path), \
         patch.object(crc, "_prepare_file_for_agent") as mock_prep, \
         patch.object(crc, "call_agent", side_effect=fake_call), \
         patch.object(crc, "post_reply", return_value={"id": "r-f"}) as mock_post:
        crc._process_messages([msg])

    assert not mock_prep.called, "must not prepare a file whose bytes never arrived"
    assert mock_post.called
    assert "看看这个报告" in captured["message"]           # caption preserved
    assert crc.BODY_UNAVAILABLE_PLACEHOLDER in captured["message"]
