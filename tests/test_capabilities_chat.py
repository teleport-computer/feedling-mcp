import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))  # noqa: E402

import httpx  # noqa: E402
from capabilities import chat as cap_chat  # noqa: E402
import types  # noqa: E402


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
    def json(self):
        return self._payload


def test_image_read_requires_id():
    r = cap_chat.image_read("STORE", params={})
    assert r.ok is False and r.error["code"] == "capability_invalid_input"


def test_image_read_fetches_exact_old_message_with_runtime_token(monkeypatch):
    seen = {}

    def _get(url, **kwargs):
        seen.update(url=url, kwargs=kwargs)
        return _Resp(
            200,
            {
                "message": {
                    "id": "old image/1",
                    "image_b64": "AAAA",
                    "image_mime": "image/png",
                }
            },
        )

    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.example")
    monkeypatch.setattr(httpx, "get", _get)
    r = cap_chat.image_read(
        "STORE",
        runtime_token="rt-zero-roster",
        # A tiny legacy window must not matter: the capability uses the exact
        # single-message body route rather than recent history.
        params={"id": "old image/1", "limit": 1},
    )
    assert r.ok is True
    assert r.data == {
        "message_id": "old image/1",
        "image_mime": "image/png",
        "image_b64": "AAAA",
    }
    assert seen["url"] == (
        "https://enclave.example/v1/chat/messages/old%20image%2F1/body"
    )
    assert seen["kwargs"]["headers"] == {
        "X-Feedling-Runtime-Token": "rt-zero-roster"
    }
    assert "params" not in seen["kwargs"]


def test_image_read_missing_message(monkeypatch):
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.example")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(200, {}))
    r = cap_chat.image_read("STORE", params={"id": "nope"})
    assert r.ok is False and r.error["code"] == "capability_not_found"


def test_image_read_without_any_credential_keeps_forbidden_error(monkeypatch):
    seen = {}

    def _get(_url, **kwargs):
        seen.update(kwargs)
        return _Resp(401, {"error": "missing api_key"})

    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.example")
    monkeypatch.setattr(httpx, "get", _get)

    result = cap_chat.image_read("STORE", params={"id": "m1"})

    assert result.ok is False
    assert result.error["code"] == "capability_forbidden"
    assert seen["headers"] == {}


def test_image_read_plaintext_binary_never_calls_enclave(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_plain")
    monkeypatch.setattr(
        cap_chat.db,
        "chat_get_strict",
        lambda *_args: {
            "id": "img-plain",
            "content_type": "image",
            "body_b64": "AAAA",
            "image_mime": "image/png",
            "owner_user_id": "usr_plain",
        },
    )
    monkeypatch.setattr(
        cap_chat.httpx,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("plaintext image must not call enclave")
        ),
    )

    result = cap_chat.image_read(store, params={"id": "img-plain"})

    assert result.ok is True
    assert result.data["image_b64"] == "AAAA"
