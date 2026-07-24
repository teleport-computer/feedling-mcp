import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))  # noqa: E402

from perception import perception_read_core  # noqa: E402
from screen import screen_read_core  # noqa: E402
from screen.screen_read_core import ScreenResult  # noqa: E402
from capabilities import photo as cap_photo  # noqa: E402


def test_recent_wraps(monkeypatch):
    monkeypatch.setattr(perception_read_core, "photos_recent",
                        lambda store, limit: ({"photos": [{"id": "p1"}]}, 200))
    r = cap_photo.recent("STORE", params={"limit": 3})
    assert r.ok is True and r.data == {"photos": [{"id": "p1"}]}


def test_read_requires_id():
    r = cap_photo.read("STORE", params={})
    assert r.ok is False and r.error["code"] == "capability_invalid_input"


def test_read_augments_with_image_meta_when_requested(monkeypatch):
    monkeypatch.setattr(perception_read_core, "photo_content",
                        lambda store, pid: ({"id": "p1", "frame_id": "f9"}, 200))
    monkeypatch.setattr(screen_read_core, "frame_decrypt",
                        lambda *a, **k: ScreenResult(status=200, raw_body=b"x", media_type="image/png"))
    r = cap_photo.read("STORE", params={"id": "p1", "include_image": True})
    assert r.ok is True
    assert r.data["image_media_type"] == "image/png" and r.data["has_image"] is True


def test_recent_caps_large_photo_list(monkeypatch):
    monkeypatch.setattr(perception_read_core, "photos_recent",
                        lambda store, limit: ({"photos": list(range(1000))}, 200))
    r = cap_photo.recent("STORE", params={})
    assert r.ok is True and len(r.data["photos"]) == 50
