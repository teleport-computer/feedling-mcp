"""Local full-stack probe for serialized upstream response-envelope leakage.

The backend, dev-seed enclave, PostgreSQL queue, V2 worker, and encrypted chat
round-trip are real.  Only the external OpenAI-compatible provider boundary is
replaced by a local HTTP stub: its first foreground turn serializes the Gemini
relay wrapper from usr_90184 into ``message.content``; the bounded correction
turn returns a normal answer.

Run this against the branch's local stack (see ``tools/e2e/README.md``).  The
probe refuses non-loopback API targets through :class:`E2EClient`.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.e2e.client import E2EClient  # noqa: E402


RECOVERED_REPLY = "T027_RECOVERED_NORMAL_REPLY"
RAW_GEMINI_RESPONSE_ENVELOPE = json.dumps(
    {
        "response": {
            "candidates": [{"content": {}}],
            "usageMetadata": {
                "promptTokenCount": 23894,
                "totalTokenCount": 24515,
                "thoughtsTokenCount": 621,
            },
            "modelVersion": "gemini-3-flash",
            "responseId": "OTV9aoyAI9ronsEPuOK1kAM",
        },
        "traceId": "491379ee31653ba7",
        "metadata": {},
    },
    ensure_ascii=False,
)


class _ProviderState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.health_calls = 0
        self.foreground_calls = 0

    def reply_for(self, payload: dict) -> str:
        messages = payload.get("messages") or []
        is_health = (
            len(messages) == 2
            and isinstance(messages[0], dict)
            and messages[0].get("content")
            == "You are a health check endpoint. Reply with exactly: ok"
        )
        with self._lock:
            if is_health:
                self.health_calls += 1
                return "ok"
            self.foreground_calls += 1
            if self.foreground_calls == 1:
                return RAW_GEMINI_RESPONSE_ENVELOPE
            return RECOVERED_REPLY


def _handler(state: _ProviderState):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, body: dict) -> None:
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802 — stdlib HTTP handler contract
            if self.path.rstrip("/").endswith("/models"):
                self._json(
                    200,
                    {"object": "list", "data": [{"id": "stub/gemini-3-flash"}]},
                )
                return
            self._json(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802 — stdlib HTTP handler contract
            if not self.path.rstrip("/").endswith("/chat/completions"):
                self._json(404, {"error": "not_found"})
                return
            length = int(self.headers.get("Content-Length") or "0")
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json(400, {"error": "invalid_json"})
                return
            reply = state.reply_for(payload)
            self._json(
                200,
                {
                    "id": f"chatcmpl-t027-{state.foreground_calls}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "stub/gemini-3-flash",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": reply},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 10,
                        "total_tokens": 110,
                    },
                },
            )

        def log_message(self, _format: str, *_args) -> None:
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:5001")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    state = _ProviderState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    provider_port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        with E2EClient.provision(route="model_api", api_url=args.api) as client:
            setup = client.post(
                "/v1/model_api/setup",
                json={
                    "provider": "openai_compatible",
                    "model": "stub/gemini-3-flash",
                    "api_key": "t027-local-probe",
                    "base_url": f"http://127.0.0.1:{provider_port}/v1",
                    "context_window_tokens": 65536,
                },
            )
            setup.raise_for_status()
            setup_body = setup.json()
            test_status = str(
                (setup_body.get("config") or {}).get("test_status")
                or setup_body.get("status")
                or ""
            )
            if test_status != "ok":
                raise AssertionError(f"setup did not test provider: {setup_body}")

            sent_at = client.send_chat("请正常回复，不要解释测试。")
            message = client.wait_reply(sent_at, timeout=args.timeout)
            if message is None:
                raise AssertionError("timed out waiting for V2 reply")
            plaintext = client.decrypt_reply(message)
            if plaintext != RECOVERED_REPLY:
                raise AssertionError(f"unexpected visible reply: {plaintext!r}")
            if "traceId" in plaintext or "usageMetadata" in plaintext:
                raise AssertionError("provider envelope leaked into visible reply")
            if state.foreground_calls != 2:
                raise AssertionError(
                    "expected one envelope plus one bounded correction, "
                    f"got {state.foreground_calls} foreground calls"
                )
            system_bubbles = client.system_bubbles_since(sent_at)
            if system_bubbles:
                raise AssertionError(f"unexpected system bubbles: {system_bubbles}")

            print(f"setup test_status={test_status} health_calls={state.health_calls}")
            print(
                "provider foreground_calls=2 "
                "(serialized_envelope=1 bounded_correction=1)"
            )
            print(f"visible_reply={plaintext}")
            print("raw_envelope_visible=false system_bubbles=0 teardown=automatic")
        return 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
