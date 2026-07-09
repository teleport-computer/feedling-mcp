import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))  # noqa: E402

import httpx  # noqa: E402
from capabilities import chat as cap_chat  # noqa: E402


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
    def json(self):
        return self._payload


def test_image_read_requires_id():
    r = cap_chat.image_read("STORE", params={})
    assert r.ok is False and r.error["code"] == "capability_invalid_input"


def test_image_read_finds_message(monkeypatch):
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.example")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(
        200, {"messages": [{"id": "m1", "image_b64": "AAAA", "image_mime": "image/png"}]}))
    r = cap_chat.image_read("STORE", runtime_token="rt", params={"id": "m1"})
    assert r.ok is True
    assert r.data == {"message_id": "m1", "image_mime": "image/png", "image_b64": "AAAA"}


def test_image_read_missing_message(monkeypatch):
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.example")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(200, {"messages": []}))
    r = cap_chat.image_read("STORE", params={"id": "nope"})
    assert r.ok is False and r.error["code"] == "capability_not_found"
