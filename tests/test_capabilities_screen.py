import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))  # noqa: E402

from screen import screen_read_core  # noqa: E402
from screen.screen_read_core import ScreenResult  # noqa: E402
from capabilities import screen as cap_screen  # noqa: E402


def test_recent_wraps_json_body(monkeypatch):
    monkeypatch.setattr(screen_read_core, "list_frames",
                        lambda store, limit: ScreenResult(status=200, json_body={"frames": [], "total": 0}))
    r = cap_screen.recent("STORE", params={"limit": 5})
    assert r.ok is True and r.data == {"frames": [], "total": 0}


def test_read_resolves_latest_then_decrypts(monkeypatch):
    monkeypatch.setattr(screen_read_core, "latest_frame",
                        lambda store: ScreenResult(status=200, json_body={"id": "f1"}))
    seen = {}
    def fake_decrypt(store, frame_id, *, include_image, api_key, runtime_token):
        seen.update(frame_id=frame_id, include_image=include_image)
        return ScreenResult(status=200, json_body={"caption": "a cat"})
    monkeypatch.setattr(screen_read_core, "frame_decrypt", fake_decrypt)
    r = cap_screen.read("STORE", params={})  # no frame_id → resolve latest
    assert r.ok is True and r.data == {"caption": "a cat"}
    assert seen == {"frame_id": "f1", "include_image": "false"}


def test_read_binary_body_exposes_meta_only(monkeypatch):
    monkeypatch.setattr(screen_read_core, "frame_decrypt",
                        lambda *a, **k: ScreenResult(status=200, raw_body=b"\xff\xd8", media_type="image/jpeg"))
    r = cap_screen.read("STORE", params={"frame_id": "f2", "include_image": True})
    assert r.ok is True
    assert r.data == {"media_type": "image/jpeg", "has_binary": True}


def test_recent_caps_large_frame_list(monkeypatch):
    monkeypatch.setattr(screen_read_core, "list_frames",
                        lambda store, limit: ScreenResult(status=200, json_body={"frames": list(range(1000)), "total": 1000}))
    r = cap_screen.recent("STORE", params={})
    assert r.ok is True and len(r.data["frames"]) == 50
