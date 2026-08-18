import base64
import json
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))  # noqa: E402

from perception import perception_read_core  # noqa: E402
from screen import screen_read_core  # noqa: E402
from screen.screen_read_core import ScreenResult  # noqa: E402
from capabilities import photo as cap_photo  # noqa: E402


def test_recent_wraps(monkeypatch):
    photo_id = "ab" * 16
    monkeypatch.setattr(perception_read_core, "photos_recent",
                        lambda store, limit: ({"photos": [{"photo_id": photo_id}]}, 200))
    r = cap_photo.recent("STORE", params={"limit": 3})
    assert r.ok is True and r.data == {"photos": [{"photo_id": photo_id}]}


def test_recent_omits_photos_the_decrypt_route_cannot_address(monkeypatch):
    readable = "cd" * 16
    legacy_uuid = "A1234567-B89C-4DEF-8123-456789ABCDEF"
    monkeypatch.setattr(
        perception_read_core,
        "photos_recent",
        lambda store, limit: ({
            "photos": [
                {"photo_id": readable},
                {"photo_id": legacy_uuid},
            ],
        }, 200),
    )

    r = cap_photo.recent("STORE", params={})

    assert r.ok is True
    assert r.data == {"photos": [{"photo_id": readable}]}


def test_read_requires_id():
    r = cap_photo.read("STORE", params={})
    assert r.ok is False and r.error["code"] == "capability_invalid_input"


def test_read_carries_decrypted_pixels_when_requested(monkeypatch):
    monkeypatch.setattr(perception_read_core, "photo_content",
                        lambda store, pid: ({"id": "p1", "frame_id": "f9"}, 200))
    monkeypatch.setattr(screen_read_core, "frame_decrypt",
                        lambda *a, **k: ScreenResult(
                            status=200,
                            raw_body=b"\xff\xd8photo-bytes",
                            media_type="image/jpeg",
                        ))
    r = cap_photo.read("STORE", params={"id": "p1", "include_image": True})
    assert r.ok is True
    assert r.data["image_media_type"] == "image/jpeg"
    assert r.data["has_image"] is True
    assert base64.b64decode(r.data["image_b64"]) == b"\xff\xd8photo-bytes"


def test_read_extracts_pixels_from_enclave_json_proxy(monkeypatch):
    pixels = b"\xff\xd8real-photo\xff\xd9"
    image_b64 = base64.b64encode(pixels).decode("ascii")
    monkeypatch.setattr(perception_read_core, "photo_content",
                        lambda store, pid: ({"id": "p1", "frame_id": "f9"}, 200))
    monkeypatch.setattr(screen_read_core, "frame_decrypt",
                        lambda *a, **k: ScreenResult(
                            status=200,
                            raw_body=json.dumps({
                                "image_b64": image_b64,
                                "image_mime": "image/jpeg",
                                "decrypt_status": "ok",
                            }).encode("utf-8"),
                            media_type="application/json",
                        ))

    r = cap_photo.read("STORE", params={"id": "p1", "include_image": True})

    assert r.ok is True
    assert r.data["image_b64"] == image_b64
    assert base64.b64decode(r.data["image_b64"]) == pixels
    assert b'"image_b64"' not in base64.b64decode(r.data["image_b64"])


def test_requested_pixels_fail_explicitly_when_decrypt_proxy_has_no_image(monkeypatch):
    monkeypatch.setattr(perception_read_core, "photo_content",
                        lambda store, pid: ({"id": "p1", "frame_id": "f9"}, 200))
    monkeypatch.setattr(screen_read_core, "frame_decrypt",
                        lambda *a, **k: ScreenResult(
                            status=200,
                            raw_body=b'{"decrypt_status":"ok","image_b64":null}',
                            media_type="application/json",
                        ))

    r = cap_photo.read("STORE", params={"id": "p1", "include_image": True})

    assert r.ok is False
    assert r.error["code"] == "capability_unavailable"


def test_requested_pixels_never_treat_malformed_json_as_an_image(monkeypatch):
    monkeypatch.setattr(perception_read_core, "photo_content",
                        lambda store, pid: ({"id": "p1", "frame_id": "f9"}, 200))
    monkeypatch.setattr(screen_read_core, "frame_decrypt",
                        lambda *a, **k: ScreenResult(
                            status=200,
                            raw_body=b"not-json-and-not-a-photo",
                            media_type="application/json",
                        ))

    r = cap_photo.read("STORE", params={"id": "p1", "include_image": True})

    assert r.ok is False
    assert r.error["code"] == "capability_upstream_error"


def test_read_without_include_image_never_decrypts(monkeypatch):
    monkeypatch.setattr(perception_read_core, "photo_content",
                        lambda store, pid: ({"id": "p1", "frame_id": "f9"}, 200))
    monkeypatch.setattr(
        screen_read_core,
        "frame_decrypt",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("metadata-only read must not decrypt pixels")
        ),
    )

    r = cap_photo.read("STORE", params={"id": "p1"})

    assert r.ok is True
    assert "image_b64" not in r.data


def test_recent_caps_large_photo_list(monkeypatch):
    monkeypatch.setattr(perception_read_core, "photos_recent",
                        lambda store, limit: ({"photos": [
                            {"photo_id": f"{index:032x}"}
                            for index in range(1000)
                        ]}, 200))
    r = cap_photo.recent("STORE", params={})
    assert r.ok is True and len(r.data["photos"]) == 50
