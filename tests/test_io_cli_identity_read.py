"""io_cli identity-read must never pass ciphertext off as a successful read.

The decrypt source is the enclave's GET /v1/identity/get. The backend's
same-named endpoint deliberately serves the raw E2E envelope (iOS decrypts
locally), so falling back to it hands the agent `body_ct` and nothing readable.

The original fallback condition — `status != 200 or identity is not a dict` —
covered BOTH "no enclave configured" (the documented intent) and "the enclave
call failed", then emitted `ok: True` regardless. Two costs: the agent reported a
card full of "encrypted long fields", and the enclave's real status code was
discarded, so the cause of the failure could not be recovered afterwards.
"""
import sys
import types as _types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import io_cli  # noqa: E402


class _Emitted(Exception):
    def __init__(self, obj, code):
        self.obj, self.code = obj, code


_ENVELOPE = {"identity": {"v": 1, "body_ct": "CIPHERTEXT", "nonce": "n",
                          "K_user": "ku", "K_enclave": "ke"}}
_PLAINTEXT = {"identity": {"v": 1, "agent_name": "裴晟",
                           "self_introduction": "我陪你写代码",
                           "decrypt_status": "ok"}}


def _run(monkeypatch, *, enclave_url, responses):
    """Drive cmd_identity_read with a scripted {url-substring: (status, body)}."""
    if enclave_url:
        monkeypatch.setenv("FEEDLING_ENCLAVE_URL", enclave_url)
    else:
        monkeypatch.delenv("FEEDLING_ENCLAVE_URL", raising=False)
    monkeypatch.setenv("FEEDLING_API_URL", "https://api.local")
    monkeypatch.setattr(io_cli, "_auth_headers", lambda: {"X-Feedling-Runtime-Token": "rt"})

    seen = []

    def _fake_http(method, url, auth, **kw):
        seen.append(url)
        for needle, resp in responses.items():
            if needle in url:
                return resp
        raise AssertionError(f"unscripted url {url}")

    monkeypatch.setattr(io_cli, "_http_json", _fake_http)
    monkeypatch.setattr(io_cli, "_emit",
                        lambda obj, code=0: (_ for _ in ()).throw(_Emitted(obj, code)))
    try:
        io_cli.cmd_identity_read(_types.SimpleNamespace())
    except _Emitted as e:
        return e.obj, e.code, seen
    raise AssertionError("cmd_identity_read did not emit")


def test_enclave_plaintext_is_returned_without_touching_the_backend(monkeypatch):
    obj, code, seen = _run(monkeypatch, enclave_url="https://enclave.local",
                           responses={"enclave.local": (200, _PLAINTEXT)})
    assert code == 0 and obj["ok"] is True
    assert obj["identity"]["agent_name"] == "裴晟"
    assert not any("api.local" in u for u in seen)


def test_enclave_failure_is_reported_not_papered_over_with_ciphertext(monkeypatch):
    obj, code, _ = _run(
        monkeypatch, enclave_url="https://enclave.local",
        responses={"enclave.local": (503, {"error": "not_ready"}),
                   "api.local": (200, _ENVELOPE)},
    )
    assert code != 0 and obj["ok"] is False
    # the enclave's own status/body is what a human needs to diagnose this
    assert obj["enclave_status"] == 503
    assert "not_ready" in str(obj["enclave_error"])


def test_ciphertext_from_the_backend_is_never_reported_as_ok(monkeypatch):
    """Even with no enclave configured (the documented fallback case), an
    envelope is not a readable card."""
    obj, code, _ = _run(monkeypatch, enclave_url=None,
                        responses={"api.local": (200, _ENVELOPE)})
    assert code != 0 and obj["ok"] is False
    assert "body_ct" in str(obj.get("error", ""))


def test_local_only_card_is_a_successful_read(monkeypatch):
    """"You may not read this" is an answer, not a failure — the agent needs to
    know the user opted it out rather than retrying forever."""
    local_only = {"identity": {"v": 1, "visibility": "local_only",
                               "decrypt_status": "local_only_agent_cannot_read"}}
    obj, code, _ = _run(monkeypatch, enclave_url="https://enclave.local",
                        responses={"enclave.local": (200, local_only)})
    assert code == 0 and obj["ok"] is True
    assert obj["identity"]["decrypt_status"] == "local_only_agent_cannot_read"


def test_enclave_decrypt_error_is_not_a_readable_card(monkeypatch):
    """The enclave answers 200 with `decrypt_status: "error: …"` when the card is
    present but could not be opened (DecryptFailure / bad JSON). No body_ct rides
    along in that response, so a "no ciphertext ⇒ readable" test would wave it
    through and hand the agent a card with no agent_name at all.
    """
    failed = {"identity": {"v": 1, "created_at": "x", "updated_at": "y",
                           "decrypt_status": "error: aead_open_failed"}}
    obj, code, _ = _run(
        monkeypatch, enclave_url="https://enclave.local",
        responses={"enclave.local": (200, failed), "api.local": (200, _ENVELOPE)},
    )
    assert code != 0 and obj["ok"] is False
    assert "aead_open_failed" in str(obj["enclave_error"])
