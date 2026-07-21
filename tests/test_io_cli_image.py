"""io_cli `--include-image` must hand a CLI vision agent a FILE PATH, not base64.

Emitting the decrypted `image_b64` inline on stdout is useless to a vision model
(it cannot decode a base64 string in its head) and bloats the tool output. The
consumer already writes decrypted chat/screen images to IMAGE_TEMP_DIR and lets
claude Read them; `screen-read`/`photo-read --include-image` must do the same so
the agent can actually SEE the screen frame / photo it fetched.
"""
import base64
import json
import os
import sys
from pathlib import Path

TOOLS = Path(__file__).parent.parent / "tools"
sys.path.insert(0, str(TOOLS))

import io_cli  # noqa: E402

_PIXELS = b"\xff\xd8\xff\xe0FAKEJPEGBYTES\xff\xd9"


def test_materialize_writes_file_and_drops_base64(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_TEMP_DIR", str(tmp_path))
    body = {
        "ocr_text": "hello",
        "image_b64": base64.b64encode(_PIXELS).decode(),
        "image_mime": "image/jpeg",
    }
    out = io_cli._materialize_decrypted_image("screen_frame42", body)

    # base64 is gone from the emitted dict (no giant useless text blob)
    assert "image_b64" not in out
    # a file path is handed back instead
    path = out["image_file"]
    assert path.endswith(".jpg")
    # the file exists inside IMAGE_TEMP_DIR and holds the real decoded bytes
    assert Path(path).parent == tmp_path
    assert Path(path).read_bytes() == _PIXELS
    # non-image metadata is preserved
    assert out["ocr_text"] == "hello"


def test_materialize_png_extension(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_TEMP_DIR", str(tmp_path))
    body = {"image_b64": base64.b64encode(_PIXELS).decode(), "image_mime": "image/png"}
    out = io_cli._materialize_decrypted_image("photo_1", body)
    assert out["image_file"].endswith(".png")


def test_materialize_noop_without_image(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_TEMP_DIR", str(tmp_path))
    body = {"ocr_text": "only text, pixels gated off"}
    out = io_cli._materialize_decrypted_image("screen_x", body)
    assert out == body  # unchanged; no image_file invented


def test_materialize_handles_data_url_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_TEMP_DIR", str(tmp_path))
    b64 = base64.b64encode(_PIXELS).decode()
    body = {"image_b64": f"data:image/jpeg;base64,{b64}", "image_mime": "image/jpeg"}
    out = io_cli._materialize_decrypted_image("screen_y", body)
    assert Path(out["image_file"]).read_bytes() == _PIXELS


# ---------------------------------------------------------------------------
# Hardening (added with the cross-user-leak sweep): decrypted images must not be
# world-readable on a shared host. The dir is 0700 and the file 0600, parity
# with chat_resident_consumer's _write_scratch_file.
# ---------------------------------------------------------------------------


def test_materialize_image_is_0600(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_TEMP_DIR", str(tmp_path / "imgs"))
    monkeypatch.setenv("FEEDLING_API_KEY", "k")
    out = io_cli._materialize_decrypted_image(
        "pfx", {"image_b64": base64.b64encode(_PIXELS).decode(), "image_mime": "image/png"})
    path = out["image_file"]
    assert Path(path).read_bytes() == _PIXELS
    assert (os.stat(path).st_mode & 0o777) == 0o600
    assert (os.stat(tmp_path / "imgs").st_mode & 0o777) == 0o700


# ---------------------------------------------------------------------------
# chat-image: pull ONE past chat message's image by id.
#
# Chat-history images are not reachable via photo-read (that's the perception
# photo library). The transcript injected into a turn shows historical image
# messages only as an `[image]` placeholder + this command, so the agent can
# lazily fetch the pixels of a specific past chat image when it actually needs
# them — without eagerly decrypting every history image every turn.
# ---------------------------------------------------------------------------


class _Emitted(Exception):
    def __init__(self, obj, code):
        self.obj = obj
        self.code = code


def _capture_chat_image(monkeypatch, tmp_path, *, history_status=200, history_body=None):
    """Invoke io_cli.cmd_chat_image with a stubbed enclave call; return (obj, code)."""
    import types as _types

    monkeypatch.setenv("IMAGE_TEMP_DIR", str(tmp_path))
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.local")
    monkeypatch.setattr(io_cli, "_auth_headers", lambda: {"X-API-Key": "k"})

    calls = {}

    def _fake_http(method, url, auth, **kw):
        calls["url"] = url
        calls["insecure"] = kw.get("insecure")
        return history_status, (history_body or {})

    monkeypatch.setattr(io_cli, "_http_json", _fake_http)

    def _fake_emit(obj, code=0):
        raise _Emitted(obj, code)

    monkeypatch.setattr(io_cli, "_emit", _fake_emit)

    args = _types.SimpleNamespace(message_id="msg_abc", limit=20)
    try:
        io_cli.cmd_chat_image(args)
    except _Emitted as e:
        return e.obj, e.code, calls
    raise AssertionError("cmd_chat_image did not emit")


def test_chat_image_materializes_image_by_id(tmp_path, monkeypatch):
    history = {
        "messages": [
            {"id": "other", "role": "user", "content_type": "text", "content": "hi"},
            {
                "id": "msg_abc",
                "role": "user",
                "content_type": "image",
                "content": "什么颜色?",
                "image_b64": base64.b64encode(_PIXELS).decode(),
                "image_mime": "image/jpeg",
            },
        ]
    }
    obj, code, calls = _capture_chat_image(tmp_path=tmp_path, monkeypatch=monkeypatch, history_body=history)

    assert code == 0 and obj["ok"] is True
    assert obj["message_id"] == "msg_abc"
    # pixels handed back as a Read-able file, not inline base64
    assert "image_b64" not in obj
    assert Path(obj["image_file"]).read_bytes() == _PIXELS
    # caption carried so the agent sees the user's question alongside the image
    assert obj["content"] == "什么颜色?"
    # decrypt is fetched from the enclave (TEE cert → insecure), not the backend
    assert "/v1/chat/history" in calls["url"]
    assert calls["insecure"] is True


def test_chat_image_message_not_found(tmp_path, monkeypatch):
    history = {"messages": [{"id": "someone_else", "content_type": "text", "content": "hi"}]}
    obj, code, _ = _capture_chat_image(tmp_path=tmp_path, monkeypatch=monkeypatch, history_body=history)
    assert code == 1 and obj["ok"] is False
    assert "not found" in obj["error"].lower()


def test_chat_image_message_without_image(tmp_path, monkeypatch):
    history = {"messages": [{"id": "msg_abc", "role": "user", "content_type": "text", "content": "just text"}]}
    obj, code, _ = _capture_chat_image(tmp_path=tmp_path, monkeypatch=monkeypatch, history_body=history)
    assert obj["ok"] is True
    assert "image_file" not in obj
    assert "no image" in obj["note"].lower()
