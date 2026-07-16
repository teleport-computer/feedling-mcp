"""_model_api_file_payload classification + validation. Pure unit."""
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from hosted import turn  # noqa: E402


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def test_absent_file_returns_none_none():
    assert turn._model_api_file_payload({}) == (None, None)


def test_png_file_repipes_as_image():
    parse, err = turn._model_api_file_payload(
        {"file_b64": _b64(b"\x89PNG\r\n\x1a\n..."), "file_name": "shot.png", "file_mime": "image/png"}
    )
    assert err is None
    assert parse["kind"] == "image"
    assert parse["mime"] == "image/png"


def test_gif_allowed_as_image():
    parse, err = turn._model_api_file_payload(
        {"file_b64": _b64(b"GIF89a..."), "file_name": "a.gif", "file_mime": "image/gif"}
    )
    assert err is None and parse["kind"] == "image" and parse["mime"] == "image/gif"


def test_heic_rejected_with_hint():
    parse, (body, status) = turn._model_api_file_payload(
        {"file_b64": _b64(b"\x00\x00\x00 ftypheic"), "file_name": "p.heic", "file_mime": "image/heic"}
    )
    assert parse is None and status == 400
    assert body["error"] == "unsupported_file_type" and "hint" in body


def test_docx_by_extension_is_file():
    parse, err = turn._model_api_file_payload(
        {"file_b64": _b64(b"PK\x03\x04binary-zip"), "file_name": "报告.docx", "file_mime": ""}
    )
    assert err is None and parse["kind"] == "file"
    assert parse["name"] == "报告.docx"  # unicode display name preserved


def test_plain_text_sniff_accepts_source_code():
    parse, err = turn._model_api_file_payload(
        {"file_b64": _b64("def f():\n    return 1\n".encode()), "file_name": "s.py", "file_mime": ""}
    )
    assert err is None and parse["kind"] == "file"


def test_binary_without_known_ext_rejected():
    parse, (body, status) = turn._model_api_file_payload(
        {"file_b64": _b64(b"\x00\x01\x02\x03NUL-inside"), "file_name": "blob.bin", "file_mime": ""}
    )
    assert parse is None and status == 400 and body["error"] == "unsupported_file_type"


def test_doc_old_binary_rejected():
    parse, (body, status) = turn._model_api_file_payload(
        {"file_b64": _b64(b"\xd0\xcf\x11\xe0old-ole"), "file_name": "old.doc", "file_mime": ""}
    )
    assert parse is None and status == 400 and body["error"] == "unsupported_file_type"


def test_invalid_base64_rejected():
    parse, (body, status) = turn._model_api_file_payload(
        {"file_b64": "!!!not-base64!!!", "file_name": "x.txt"}
    )
    assert parse is None and status == 400 and body["error"] == "invalid_file"


def test_oversize_rejected_413():
    big = b"a" * (turn.MODEL_API_MAX_FILE_BYTES + 1)
    parse, (body, status) = turn._model_api_file_payload(
        {"file_b64": _b64(big), "file_name": "big.txt", "file_mime": "text/plain"}
    )
    assert parse is None and status == 413 and body["error"] == "payload_too_large"
    assert body["max_bytes"] == turn.MODEL_API_MAX_FILE_BYTES


def test_display_name_strips_path_but_keeps_unicode():
    parse, err = turn._model_api_file_payload(
        {"file_b64": _b64(b"hello"), "file_name": "../../离职协议.txt", "file_mime": "text/plain"}
    )
    assert err is None and parse["name"] == "离职协议.txt"


def _big_jpeg():
    import io
    import os as _os
    pytest = __import__("pytest")
    pytest.importorskip("PIL")
    from PIL import Image
    buf = io.BytesIO()
    Image.frombytes("RGB", (4000, 3000), _os.urandom(4000 * 3000 * 3)).save(
        buf, format="JPEG", quality=95)
    return buf.getvalue()


def test_oversized_direct_image_is_accepted_and_downscaled():
    # A >2MB phone photo used to 400 here; now it is bounded to <= the stored cap.
    big = _big_jpeg()
    assert len(big) > turn.MODEL_API_MAX_IMAGE_BYTES
    data, mime, err = turn._model_api_image_payload(
        {"image_b64": _b64(big), "image_mime": "image/jpeg"})
    assert err is None                                           # no longer rejected
    assert len(data) <= turn.MODEL_API_MAX_IMAGE_BYTES           # bounded
    assert mime == "image/jpeg"


def test_oversized_file_picker_image_is_downscaled_before_repipe():
    big = _big_jpeg()
    parse, err = turn._model_api_file_payload(
        {"file_b64": _b64(big), "file_name": "photo.jpg", "file_mime": "image/jpeg"})
    assert err is None and parse["kind"] == "image"
    assert len(parse["bytes"]) <= turn.MODEL_API_MAX_IMAGE_BYTES  # bounded, not 25MB


def test_beyond_upload_ceiling_is_still_rejected():
    huge = b"\xff\xd8\xff" + b"x" * (turn.MODEL_API_MAX_IMAGE_UPLOAD_BYTES + 1)
    data, mime, err = turn._model_api_image_payload(
        {"image_b64": _b64(huge), "image_mime": "image/jpeg"})
    assert data is None and "too large" in (err or "")
