"""A runtime-token hosted send enters V2 without decrypting BYOK in HTTP.

Provider-key decryption belongs to the claimed V2 turn, where failure is
durably recorded on the job.  The request path only validates ownership and
atomically commits the encrypted message plus job.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import runtime_token  # noqa: E402
from core import store as core_store  # noqa: E402
from hosted import chat_send_core  # noqa: E402
from hosted import config_store as hosted_config_store  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402

_SECRET = "test-runtime-secret"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # This send-path happy-path relies on setup's startup materialization
    # landing V2 with no explicit flip — the v2_only fleet contract (see
    # test_asgi_hosted_chat_send.py's ``env`` fixture for the full
    # rationale). Pin it here so the default "dual" policy (Task 5) doesn't
    # leave the fresh user on the still-resident per-user fence.
    monkeypatch.setenv(hosted_config_store.HOSTED_RUNTIME_POLICY_ENV, "v2_only")
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    monkeypatch.setattr(
        core_enclave,
        "_get_enclave_info",
        lambda: {"content_pk_hex": ("22" * 32), "compose_hash": "test"},
    )
    monkeypatch.setattr(jobs_store, "workers_alive", lambda **kw: True)
    monkeypatch.setattr(jobs_store, "live_worker_capacity", lambda **kw: 4)
    monkeypatch.setattr(jobs_store, "inflight_job_count", lambda: 0)
    monkeypatch.setattr(jobs_store, "recent_mean_service_sec", lambda **kw: None)
    monkeypatch.setattr(chat_send_core.kill_switch, "turns_halted", lambda **kw: False)
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    with make_client() as c:
        yield c


def _register(client) -> tuple[str, str]:
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _mint(user_id: str) -> str:
    return runtime_token.mint(
        _SECRET.encode("utf-8"),
        user_id=user_id,
        runtime_instance_id="ri_test",
        scope=["chat", "memory", "identity"],
    )


def test_runtime_token_turn_defers_provider_key_decrypt_to_v2_worker(client, monkeypatch):
    user_id, api_key = _register(client)

    # 1) Configure + test the provider with the user's real api_key (works today).
    monkeypatch.setattr(
        provider_client, "test_provider_key",
        lambda cfg: {"reply": "ok", "usage": {"total_tokens": 1}},
    )
    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "gemini", "model": "gemini-2.5-flash", "api_key": "AQ.fake-gemini-key"},
        headers={"X-API-Key": api_key},
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    # The HTTP path must not unwrap the provider key at all. The worker will use
    # its own runtime token after it claims the durable job.
    decrypt_calls: list[dict] = []

    def reject_send_time_decrypt(*args, **kwargs):
        decrypt_calls.append(kwargs)
        raise AssertionError("provider BYOK decrypt belongs to the V2 worker")

    monkeypatch.setattr(
        core_enclave, "_decrypt_envelope_via_enclave", reject_send_time_decrypt
    )

    # Send the turn authenticated with ONLY the runtime token (no X-API-Key).
    tok = _mint(user_id)
    chat = client.post(
        "/v1/model_api/chat/send",
        json={"message": "你好"},
        headers={"X-Feedling-Runtime-Token": tok},
    )

    assert chat.status_code == 202, chat.get_data(as_text=True)
    assert chat.get_json()["status"] == "processing"
    assert decrypt_calls == []
