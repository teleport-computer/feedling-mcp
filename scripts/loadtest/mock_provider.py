"""In-process mock LLM provider for V2 load tests.

Impersonates the OpenAI/Anthropic chat wire well enough that the load-test
harness can drive real turns through the hosted-runtime pipeline without
calling a real LLM: no BYOK credit burn, deterministic token counts, and
optional configurable latency to simulate a slow provider.

Stdlib only (``http.server`` / ``threading``) — no external dependencies, so
this can run standalone on a bare Python interpreter during a real load test.

Usage as a library::

    from scripts.loadtest.mock_provider import MockProvider

    with MockProvider(reply="hi there", prompt_tokens=100, completion_tokens=20) as p:
        requests.post(p.base_url + "/v1/chat/completions", json={...})

Usage standalone::

    python -m scripts.loadtest.mock_provider --port 8900 --latency-ms 50
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_REPLY = "This is a canned mock provider reply for load testing."
DEFAULT_PROMPT_TOKENS = 100
DEFAULT_COMPLETION_TOKENS = 20
DEFAULT_LATENCY_MS = 0

# 4 chars/token 是所有主流 BPE 分词器在混合中英文本上的常用粗估。我们只需要一个对
# prompt 长度**单调**的量：token 门比较的是"循环前 vs 循环后"的比值，不是绝对 token 数。
_CHARS_PER_TOKEN = 4


def estimate_tokens_from_text(text: str) -> int:
    """粗估 token 数。对长度单调、恒 >= 1（空串也算一个 token 的开销）。"""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _estimate_prompt_tokens(payload: dict) -> int:
    parts = [
        str(m.get("content") or "")
        for m in (payload.get("messages") or [])
        if isinstance(m, dict)
    ]
    return estimate_tokens_from_text("".join(parts))


def _make_handler(provider: "MockProvider") -> type:
    class Handler(BaseHTTPRequestHandler):
        # Silence default stderr access logging; load tests can generate a
        # lot of traffic and the harness has its own instrumentation.
        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

        def do_POST(self) -> None:  # noqa: N802 (stdlib naming convention)
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw_body = self.rfile.read(length) if length else b""

            if provider.latency_ms:
                time.sleep(provider.latency_ms / 1000.0)

            if provider.estimate_tokens:
                try:
                    payload = json.loads(raw_body) if raw_body else {}
                except json.JSONDecodeError:
                    payload = {}
                prompt_tokens = _estimate_prompt_tokens(payload)
                completion_tokens = estimate_tokens_from_text(provider.reply)
            else:
                prompt_tokens = provider.prompt_tokens
                completion_tokens = provider.completion_tokens
            # 服务端累加器：无论调用方是 planner 的第 N 轮还是 responder，每一次
            # provider 调用都被计入。这是"整回合 token"唯一可靠的观测点——它不依赖
            # 调用方自报 usage，也不需要知道一个回合内到底发生了几次 LLM 调用。
            with provider._lock:
                provider.total_prompt_tokens += prompt_tokens
                provider.total_completion_tokens += completion_tokens
                provider.request_count += 1
            body = {
                "id": "mock-chatcmpl-0",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": provider.reply},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }
            payload = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        # Accept GET too (e.g. health checks) with a trivial 200, in case a
        # harness pings the provider before driving real traffic.
        def do_GET(self) -> None:  # noqa: N802
            payload = b'{"status": "ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


class MockProvider:
    """Minimal stdlib HTTP server that stands in for a real LLM provider."""

    def __init__(
        self,
        *,
        reply: str = DEFAULT_REPLY,
        prompt_tokens: int = DEFAULT_PROMPT_TOKENS,
        completion_tokens: int = DEFAULT_COMPLETION_TOKENS,
        latency_ms: int = DEFAULT_LATENCY_MS,
        estimate_tokens: bool = False,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.reply = reply
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.latency_ms = latency_ms
        self.estimate_tokens = estimate_tokens
        self._host = host
        self._requested_port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.request_count = 0

    def start(self) -> None:
        if self._server is not None:
            return  # already started; idempotent
        handler = _make_handler(self)
        self._server = ThreadingHTTPServer((self._host, self._requested_port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("MockProvider is not started")
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self.port}"

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

    def __enter__(self) -> "MockProvider":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the load-test mock LLM provider as a standalone HTTP server."
    )
    parser.add_argument("--port", type=int, default=8900, help="port to bind (default: 8900)")
    parser.add_argument(
        "--latency-ms",
        type=int,
        default=DEFAULT_LATENCY_MS,
        help="artificial latency per request in milliseconds (default: 0)",
    )
    parser.add_argument(
        "--prompt-tokens",
        type=int,
        default=DEFAULT_PROMPT_TOKENS,
        help=f"fixed usage.prompt_tokens to report (default: {DEFAULT_PROMPT_TOKENS})",
    )
    parser.add_argument(
        "--completion-tokens",
        type=int,
        default=DEFAULT_COMPLETION_TOKENS,
        help=f"fixed usage.completion_tokens to report (default: {DEFAULT_COMPLETION_TOKENS})",
    )
    parser.add_argument(
        "--reply",
        type=str,
        default=DEFAULT_REPLY,
        help="canned assistant reply text to return",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    provider = MockProvider(
        reply=args.reply,
        prompt_tokens=args.prompt_tokens,
        completion_tokens=args.completion_tokens,
        latency_ms=args.latency_ms,
        port=args.port,
    )
    provider.start()
    print(f"mock_provider listening on {provider.base_url} (latency_ms={args.latency_ms})")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        provider.stop()


if __name__ == "__main__":
    main()
