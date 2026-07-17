from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from nacl.public import PrivateKey

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.e2e import config, hosted, p0
from tools.e2e.client import E2EClient, TEST_API, _refuse_prod


class FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _client() -> E2EClient:
    return E2EClient(
        TEST_API,
        "e2e-user",
        "e2e-key",
        PrivateKey.generate(),
        bytes(PrivateKey.generate().public_key),
    )


def test_refuse_prod_allows_test_api() -> None:
    _refuse_prod(TEST_API)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.feedling.app",
        "https://mcp.feedling.app",
        "https://example.test",
    ],
)
def test_refuse_prod_rejects_every_non_test_host(url: str) -> None:
    with pytest.raises(RuntimeError, match="only permits the test API"):
        _refuse_prod(url)


def test_envelope_seal_open_roundtrip() -> None:
    client = _client()
    try:
        envelope = client._seal("青色 continuity")
        assert client.open_envelope(envelope) == "青色 continuity"
    finally:
        client._http.close()


def test_message_text_accepts_three_history_shapes() -> None:
    client = _client()
    try:
        envelope = client._seal("sealed reply")
        assert client.message_text({"content": "plain reply"}) == "plain reply"
        assert client.message_text(envelope) == "sealed reply"
        assert client.message_text({"envelope": envelope}) == "sealed reply"
    finally:
        client._http.close()


def test_teardown_failure_is_a_hard_failure() -> None:
    class FailingHTTP:
        def __init__(self):
            self.closed = False

        def post(self, *_args, **_kwargs) -> FakeResponse:
            return FakeResponse(500, {"error": "reset_failed"})

        def close(self) -> None:
            self.closed = True

    client = _client()
    client._http.close()
    failing_http = FailingHTTP()
    client._http = failing_http  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="HTTP 500"):
        client.__exit__(None, None, None)
    assert failing_http.closed


def test_hosted_503_retry_reuses_client_message_id(monkeypatch) -> None:
    responses = [
        FakeResponse(503, {"error": "hosting_runtime_unavailable"}),
        FakeResponse(202, {"user_message": {"id": "m1", "ts": 123.5}}),
    ]

    class FakeClient:
        def __init__(self):
            self.payloads: list[dict] = []

        def post(self, _path: str, *, json: dict) -> FakeResponse:
            self.payloads.append(json)
            return responses.pop(0)

    client = FakeClient()
    monkeypatch.setattr(hosted.time, "sleep", lambda _seconds: None)

    sent_at, error = hosted._hosted_send(client, "hello")  # type: ignore[arg-type]

    assert error == ""
    assert sent_at == 123.5
    assert len(client.payloads) == 2
    first_id, second_id = [payload["client_msg_id"] for payload in client.payloads]
    assert first_id == second_id
    assert str(uuid.UUID(first_id)) == first_id


def test_load_keys_parses_pool_file(monkeypatch, tmp_path) -> None:
    keys_file = tmp_path / "keys.env"
    keys_file.write_text(
        "# release key pool\n"
        "E2E_KEY_ANTHROPIC='sk-ant-test'\n"
        'E2E_RELAY_BASE="https://relay.test/v1"\n'
        "E2E_RELAY_MODEL=model=tagged\n"
        "ignored-line\n"
    )
    monkeypatch.setattr(config, "KEYS_FILE", keys_file)

    assert config.load_keys() == {
        "E2E_KEY_ANTHROPIC": "sk-ant-test",
        "E2E_RELAY_BASE": "https://relay.test/v1",
        "E2E_RELAY_MODEL": "model=tagged",
    }


def test_p0_blocking_verdict() -> None:
    assert not p0.p0_blocks_release([])
    assert not p0.p0_blocks_release([
        {"cell": "missing-key", "result": "skip"},
        {"cell": "memory-late", "result": "ok"},
    ])
    assert p0.p0_blocks_release([
        {"cell": "healthy", "result": "ok"},
        {"cell": "chat-failed", "result": "fail"},
    ])
