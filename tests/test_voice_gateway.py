"""Unit coverage for the ElevenLabs Custom LLM voice bridge."""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from core import voice_token
import db
from enclave.routes import chat as enclave_chat
from hosted import chat_send_core
from voice import results
from voice import routes_asgi
from voice.message_filter import is_meaningful_voice_message


def test_internal_voice_delta_requires_voice_reply_scope():
    dependency = inspect.signature(
        routes_asgi.internal_voice_delta
    ).parameters["auth"].default.dependency

    assert getattr(dependency, "__name__", "") == "_dep"
    assert [cell.cell_contents for cell in dependency.__closure__ or ()] == [
        "voice_reply"
    ]


def test_voice_token_is_scoped_signed_and_expires():
    token, expires_at = voice_token.mint(
        b"voice-secret", user_id="user-1", call_id="call-1", now=100.0, ttl=30.0
    )

    assert expires_at == 130.0
    claims = voice_token.verify(b"voice-secret", token, now=129.9)
    assert claims["aud"] == "io_voice_llm"
    assert claims["user_id"] == "user-1"
    assert claims["call_id"] == "call-1"

    with pytest.raises(voice_token.VoiceTokenError, match="bad_signature"):
        voice_token.verify(b"different-secret", token, now=101.0)
    with pytest.raises(voice_token.VoiceTokenError, match="token_expired"):
        voice_token.verify(b"voice-secret", token, now=130.0)


def test_gateway_uses_public_https_and_rejects_private_default(monkeypatch):
    request = SimpleNamespace(base_url="http://192.168.2.10:5101/")
    monkeypatch.delenv("FEEDLING_VOICE_GATEWAY_PUBLIC_URL", raising=False)
    monkeypatch.delenv("FEEDLING_VOICE_ALLOW_PRIVATE_GATEWAY", raising=False)
    assert routes_asgi._gateway_url(request) is None

    monkeypatch.setenv(
        "FEEDLING_VOICE_GATEWAY_PUBLIC_URL", "https://voice.example.test"
    )
    assert routes_asgi._gateway_url(request) == (
        "https://voice.example.test/v1/voice"
    )

    monkeypatch.setenv(
        "FEEDLING_VOICE_GATEWAY_PUBLIC_URL",
        "https://voice.example.test/v1/voice/chat/completions",
    )
    assert routes_asgi._gateway_url(request) == "https://voice.example.test/v1/voice"


@pytest.mark.parametrize(
    ("compose_name", "expected_origin"),
    [
        ("docker-compose.phala.yaml", "https://api.feedling.app"),
        ("docker-compose.phala.test.yaml", "https://test-api.feedling.app"),
        ("docker-compose.phala.pre.yaml", "https://pre-api.feedling.app"),
    ],
)
def test_managed_composes_pin_public_voice_gateway(
    compose_name, expected_origin
):
    compose = yaml.safe_load((ROOT / "deploy" / compose_name).read_text())

    assert (
        compose["services"]["backend"]["environment"][
            "FEEDLING_VOICE_GATEWAY_PUBLIC_URL"
        ]
        == expected_origin
    )


def test_self_host_compose_forwards_public_voice_gateway_setting():
    compose = yaml.safe_load(
        (ROOT / "deploy" / "docker-compose.yaml").read_text()
    )

    assert (
        compose["services"]["backend"]["environment"][
            "FEEDLING_VOICE_GATEWAY_PUBLIC_URL"
        ]
        == "${FEEDLING_VOICE_GATEWAY_PUBLIC_URL:-}"
    )


def test_gateway_extracts_only_latest_user_turn():
    payload = {
        "messages": [
            {"role": "system", "content": "ElevenLabs prompt"},
            {"role": "user", "content": "第一句"},
            {"role": "assistant", "content": "ignored"},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "第二句"},
                    {"type": "image_url", "image_url": "ignored"},
                ],
            },
        ]
    }

    assert routes_asgi._last_user_turn(payload) == ("第二句", 2)
    assert routes_asgi._last_user_turn({"messages": []}) is None


@pytest.mark.parametrize(
    "message",
    ["...", "……", "（背景杂音）", "[silence]", "【background noise】"],
)
def test_gateway_ignores_non_speech_voice_transcripts(message):
    assert is_meaningful_voice_message(message) is False


@pytest.mark.parametrize("message", ["嗯。", "把音量调大一点", "测试 123"])
def test_gateway_keeps_short_meaningful_voice_transcripts(message):
    assert is_meaningful_voice_message(message) is True


def test_asr_revision_keeps_the_same_logical_voice_turn():
    first = routes_asgi._voice_turn_id(2)
    corrected = routes_asgi._voice_turn_id(2)

    assert first == corrected == "2"
    assert routes_asgi._voice_turn_id(3) != first


def test_gateway_extracts_session_context_from_elevenlabs_extra_body():
    payload = {
        "model": "io-current",
        "elevenlabs_extra_body": {
            "io_voice_token": " signed-token ",
            "io_call_id": " call-1 ",
        },
    }

    assert routes_asgi._voice_session_context(payload) == (
        "signed-token",
        "call-1",
        "v3",
        ["io_call_id", "io_voice_token"],
    )


def test_gateway_accepts_legacy_top_level_session_context():
    assert routes_asgi._voice_session_context(
        {"io_voice_token": "signed-token", "io_call_id": "call-1"}
    )[:2] == ("signed-token", "call-1")


def test_sse_chunk_is_openai_compatible():
    raw = routes_asgi._sse_chunk("chatcmpl-test", content="你好")
    payload = json.loads(raw.removeprefix("data: ").strip())

    assert payload["object"] == "chat.completion.chunk"
    assert payload["model"] == "io-current"
    assert payload["choices"][0]["delta"] == {"content": "你好"}


def test_incremental_and_final_suffixes_do_not_replay_streamed_text():
    assert routes_asgi._incremental_suffix("你好", "你好呀") == "呀"
    assert routes_asgi._incremental_suffix("你好", "您好") == ""
    assert routes_asgi._final_suffix("你好", "你好呀") == "呀"
    assert routes_asgi._final_suffix("你好呀", "你好") == ""


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("flash", "flash"),
        ("v3", "v3"),
        ("unknown", "v3"),
    ],
)
def test_voice_buffer_uses_elevenlabs_slow_llm_compatibility_text(
    requested, expected
):
    context = routes_asgi._voice_session_context({
        "elevenlabs_extra_body": {
            "io_voice_token": "signed-token",
            "io_call_id": "call-1",
            "io_tts_model": requested,
        }
    })

    assert context[2] == expected
    assert routes_asgi._VOICE_BUFFER_TEXT == "... "


def test_voice_metadata_requires_a_complete_bounded_pair():
    assert chat_send_core._voice_metadata(None) == {}
    assert chat_send_core._voice_metadata({"call_id": "call"}) == {}
    assert chat_send_core._voice_metadata(
        {"call_id": " call ", "turn_id": " turn "}
    ) == {"voice_call_id": "call", "voice_turn_id": "turn"}
    assert chat_send_core._voice_metadata(
        {"call_id": "c" * 97, "turn_id": "turn"}
    ) == {}


def test_enclave_history_preserves_voice_routing_metadata(monkeypatch):
    monkeypatch.setattr(
        enclave_chat.envelope,
        "decrypt_envelope",
        lambda message, user_id, content_sk: b"spoken words",
    )

    messages, errors = enclave_chat._decrypt_history_items(
        [{
            "id": "message-1",
            "role": "user",
            "ts": 123.0,
            "source": "chat",
            "v": 1,
            "visibility": "shared",
            "content_type": "text",
            "voice_call_id": " call-1 ",
            "voice_turn_id": " turn-1 ",
        }],
        "user-1",
        object(),
    )

    assert errors == []
    assert messages[0]["voice_call_id"] == "call-1"
    assert messages[0]["voice_turn_id"] == "turn-1"


def test_failed_voice_turn_uses_the_normal_chat_failure_copy(monkeypatch):
    monkeypatch.setattr(
        db,
        "chat_get_strict",
        lambda user_id, message_id: {
            "reply_status": "failed",
            "reply_failure_code": "rate_limited",
        },
    )

    assert routes_asgi._failed_turn_text("user-1", "message-1") == (
        "模型服务限流了，稍等几分钟再试。"
    )


def test_resident_voice_turn_enters_the_normal_chat_lane(monkeypatch):
    captured = {}

    class Store:
        user_id = "user-1"

        def append_chat_idempotent(
            self, role, source, envelope, *, client_msg_id, window_sec, extra
        ):
            captured.update(
                role=role,
                source=source,
                envelope=envelope,
                client_msg_id=client_msg_id,
                window_sec=window_sec,
                extra=extra,
            )
            return {"id": "message-1", "ts": 123.0}, True

        def notify_chat_waiters(self):
            captured["notified"] = True

    monkeypatch.setattr(
        routes_asgi.core_envelope,
        "_build_shared_envelope_for_store",
        lambda store, plaintext, item_id=None: (
            {"id": item_id, "body_ct": "ct", "nonce": "n", "K_user": "ku"},
            "",
        ),
    )

    body, status = routes_asgi._resident_voice_send_core(
        Store(),
        message="你好",
        client_msg_id="00000000-0000-0000-0000-000000000001",
        call_id="call-1",
        turn_id="turn-1",
    )

    assert status == 202
    assert body["user_message"] == {"id": "message-1", "ts": 123.0}
    assert captured["role"] == "user"
    assert captured["source"] == "chat"
    assert captured["extra"] == {
        "voice_call_id": "call-1",
        "voice_turn_id": "turn-1",
    }
    assert captured["notified"] is True


def test_io_rejection_is_streamed_instead_of_dropping_the_call():
    response = routes_asgi._streaming_text_response(
        "chatcmpl-test", "连接模型服务时出了问题。"
    )

    assert response.status_code == 200
    assert response.media_type == "text/event-stream"

    async def collect() -> str:
        chunks = [chunk async for chunk in response.body_iterator]
        return "".join(chunks)

    body = asyncio.run(collect())
    assert body.count("data: [DONE]") == 1


def test_ignored_voice_turn_returns_an_empty_streaming_completion():
    response = routes_asgi._streaming_text_response("chatcmpl-test", "")

    async def collect() -> str:
        chunks = [chunk async for chunk in response.body_iterator]
        return "".join(chunks)

    body = asyncio.run(collect())
    assert '"content"' not in body
    assert body.count("data: [DONE]") == 1


def test_gateway_does_not_persist_or_run_non_speech_turn(monkeypatch):
    async def read_payload(_request):
        return {
            "elevenlabs_extra_body": {
                "io_voice_token": "signed-token",
                "io_call_id": "call-1",
            },
            "messages": [{"role": "user", "content": "..."}],
        }

    monkeypatch.setattr(routes_asgi.asgi_http, "read_json_silent", read_payload)
    monkeypatch.setattr(routes_asgi.results, "secret", lambda: b"voice-secret")
    monkeypatch.setattr(
        routes_asgi.voice_token,
        "verify",
        lambda _secret, _token: {"call_id": "call-1", "user_id": "user-1"},
    )

    response = asyncio.run(
        routes_asgi.voice_chat_completions(SimpleNamespace())
    )

    async def collect() -> str:
        chunks = [chunk async for chunk in response.body_iterator]
        return "".join(chunks)

    body = asyncio.run(collect())
    assert response.status_code == 200
    assert '"content"' not in body


class _Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.row or []


class _Connection:
    def __init__(self):
        self.rows = {}
        self.stream_rows = {}

    @contextmanager
    def transaction(self):
        yield self

    def execute(self, sql, params=None):
        if sql.startswith("DELETE FROM voice_turn_results"):
            now = time.time()
            self.rows = {
                key: row for key, row in self.rows.items() if row["expires_at"] > now
            }
            return _Result(None)
        if sql.startswith("INSERT INTO voice_turn_results"):
            call_id, turn_id, user_id, message_id, nonce, ciphertext, expires_at = params
            key = (call_id, turn_id)
            if key in self.rows:
                return _Result(None)
            self.rows[key] = {
                "user_id": user_id,
                "message_id": message_id,
                "nonce": nonce,
                "ciphertext": ciphertext,
                "expires_at": expires_at,
            }
            return _Result((call_id,))
        if sql.startswith("SELECT message_id,nonce,ciphertext"):
            user_id, call_id, turn_id = params
            row = self.rows.get((call_id, turn_id))
            if row is None or row["user_id"] != user_id or row["expires_at"] <= time.time():
                return _Result(None)
            return _Result((row["message_id"], row["nonce"], row["ciphertext"]))
        if sql.startswith("DELETE FROM voice_turn_streams"):
            now = time.time()
            self.stream_rows = {
                key: row
                for key, row in self.stream_rows.items()
                if row["expires_at"] > now
            }
            return _Result(None)
        if sql.startswith("INSERT INTO voice_turn_streams"):
            (
                call_id,
                turn_id,
                segment,
                user_id,
                text_len,
                nonce,
                ciphertext,
                is_final,
                expires_at,
            ) = params
            key = (call_id, turn_id, segment)
            previous = self.stream_rows.get(key)
            if (
                previous is not None
                and (
                    previous["user_id"] != user_id
                    or (
                        previous["text_len"] >= text_len
                        and not (
                            previous["text_len"] == text_len
                            and not previous["is_final"]
                            and is_final
                        )
                    )
                )
            ):
                return _Result(None)
            self.stream_rows[key] = {
                "user_id": user_id,
                "text_len": text_len,
                "nonce": nonce,
                "ciphertext": ciphertext,
                "is_final": previous["is_final"] or is_final if previous else is_final,
                "expires_at": expires_at,
            }
            return _Result((segment,))
        if sql.startswith("SELECT segment,nonce,ciphertext,is_final"):
            user_id, call_id, turn_id = params
            found = []
            for (stored_call, stored_turn, segment), row in self.stream_rows.items():
                if (
                    stored_call == call_id
                    and stored_turn == turn_id
                    and row["user_id"] == user_id
                    and row["expires_at"] > time.time()
                ):
                    found.append((
                        segment,
                        row["nonce"],
                        row["ciphertext"],
                        row["is_final"],
                    ))
            return _Result(sorted(found))
        raise AssertionError(f"unexpected SQL: {sql}")


class _Pool:
    def __init__(self):
        self.conn = _Connection()

    @contextmanager
    def connection(self):
        yield self.conn


def test_voice_reply_handoff_is_encrypted_and_idempotent(monkeypatch):
    pool = _Pool()
    monkeypatch.setenv("FEEDLING_VOICE_TOKEN_SECRET", "test-voice-secret")
    monkeypatch.setattr(results.db, "get_pool", lambda: pool)

    assert results.store_reply(
        "user-1",
        call_id="call-1",
        turn_id="turn-1",
        message_id="message-1",
        text="真实回答",
    )
    stored = pool.conn.rows[("call-1", "turn-1")]
    assert "真实回答".encode("utf-8") not in stored["ciphertext"]
    assert results.load_reply(
        "user-1", call_id="call-1", turn_id="turn-1"
    ) == {"message_id": "message-1", "text": "真实回答"}
    assert results.load_reply(
        "another-user", call_id="call-1", turn_id="turn-1"
    ) is None
    assert not results.store_reply(
        "user-1",
        call_id="call-1",
        turn_id="turn-1",
        message_id="message-duplicate",
        text="重复回答",
    )


def test_voice_stream_handoff_is_encrypted_monotonic_and_segmented(monkeypatch):
    pool = _Pool()
    monkeypatch.setenv("FEEDLING_VOICE_TOKEN_SECRET", "test-voice-secret")
    monkeypatch.setattr(results.db, "get_pool", lambda: pool)

    assert results.store_stream_text(
        "user-1", call_id="call-1", turn_id="turn-1", segment=0, text="你"
    )
    assert results.store_stream_text(
        "user-1", call_id="call-1", turn_id="turn-1", segment=0, text="你好"
    )
    assert not results.store_stream_text(
        "user-1", call_id="call-1", turn_id="turn-1", segment=0, text="你"
    )
    assert results.store_stream_text(
        "user-1", call_id="call-1", turn_id="turn-1", segment=1, text="第二段"
    )
    assert results.store_stream_text(
        "user-1",
        call_id="call-1",
        turn_id="turn-1",
        segment=1,
        text="第二段",
        is_final=True,
    )

    stored = pool.conn.stream_rows[("call-1", "turn-1", 0)]
    assert "你好".encode("utf-8") not in stored["ciphertext"]
    assert results.load_stream_texts(
        "user-1", call_id="call-1", turn_id="turn-1"
    ) == [
        {"segment": 0, "text": "你好", "is_final": False},
        {"segment": 1, "text": "第二段", "is_final": True},
    ]
    assert results.load_stream_texts(
        "another-user", call_id="call-1", turn_id="turn-1"
    ) == []
