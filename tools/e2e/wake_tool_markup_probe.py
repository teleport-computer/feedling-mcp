"""Local full-stack probe for tool-markup leakage from a Runtime V2 wake.

The backend, dev-seed enclave, PostgreSQL queue, V2 worker, manual-wake route,
and encrypted chat round-trip are real. Only the external OpenAI-compatible
provider boundary is replaced by a local HTTP stub. The wake response contains
a leaked tool marker beside useful text; the assertion is on the user-decrypted
reply. An empty-history manual wake is intentional so the probe does not wait
through the active-conversation suppression window it is not testing.

Run this against the branch's local stack (see ``tools/e2e/README.md``). The
probe refuses non-loopback API targets through :class:`E2EClient`.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.e2e.client import E2EClient  # noqa: E402


CLEAN_WAKE_REPLY = "好，棋先停着\n你要干嘛去了"
LEAKED_WAKE_REPLY = (
    '<parameter name="tool_name">reply</parameter>\n' + CLEAN_WAKE_REPLY
)


class _ProviderState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.health_calls = 0
        self.business_calls = 0
        self.wake_calls = 0
        self._armed = False

    def arm_after_setup(self) -> None:
        """Discard setup probes; classify subsequent calls by their real prompt."""
        with self._lock:
            self.business_calls = 0
            self.wake_calls = 0
            self._armed = True

    def reply_for(self, payload: dict) -> str:
        messages = payload.get("messages") or []
        is_health = any(
            isinstance(message, dict)
            and message.get("content")
            == "You are a health check endpoint. Reply with exactly: ok"
            for message in messages
        )
        with self._lock:
            if not self._armed or is_health:
                self.health_calls += 1
                return "ok"
            self.business_calls += 1
            serialized_messages = json.dumps(messages, ensure_ascii=False)
            if "platform presence moment" in serialized_messages:
                self.wake_calls += 1
                return LEAKED_WAKE_REPLY
            return "T036_UNEXPECTED_NON_WAKE_CALL"


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
                    {"object": "list", "data": [{"id": "stub/t036-wake"}]},
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
                    "id": f"chatcmpl-t036-{state.business_calls}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "stub/t036-wake",
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
                    "model": "stub/t036-wake",
                    "api_key": "t036-local-probe",
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
            state.arm_after_setup()

            wake_started = time.time()
            tick = client.post("/v1/proactive/tick", json={"force": True})
            tick.raise_for_status()
            tick_body = tick.json()
            job = tick_body.get("job")
            if not isinstance(job, dict) or job.get("lane") != "manual_wake":
                raise AssertionError(f"manual wake was not admitted: {tick_body}")

            wake = client.wait_reply(wake_started, timeout=args.timeout)
            if wake is None:
                raise AssertionError("timed out waiting for manual-wake reply")
            plaintext = client.decrypt_reply(wake)
            if plaintext != CLEAN_WAKE_REPLY:
                raise AssertionError(f"unexpected visible wake reply: {plaintext!r}")
            if "<parameter" in plaintext or "tool_name" in plaintext:
                raise AssertionError("tool markup leaked into decrypted wake reply")
            if state.wake_calls != 1:
                raise AssertionError(
                    "expected exactly one wake call, "
                    f"got business={state.business_calls} wake={state.wake_calls}"
                )

            print(f"setup test_status={test_status} health_calls={state.health_calls}")
            print(
                f"wake lane={job.get('lane')} job_id={job.get('id')} "
                f"provider_business_calls={state.business_calls}"
            )
            print(f"decrypted_wake_reply={plaintext!r}")
            print("tool_markup_visible=false teardown=automatic")
        return 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
