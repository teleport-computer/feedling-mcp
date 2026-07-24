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
    parts: list[str] = []
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        parts.append(str(message.get("content") or ""))
        # Native tool transcripts carry meaningful prompt bytes outside
        # ``content``.  Count the assistant calls as well as the catalog that is
        # resent on every tools-enabled round; otherwise the V2 rollback gate
        # materially understates the production unified-loop prompt.
        if message.get("tool_calls"):
            parts.append(json.dumps(message["tool_calls"], ensure_ascii=False))
    if payload.get("tools"):
        parts.append(json.dumps(payload["tools"], ensure_ascii=False))
    return estimate_tokens_from_text("".join(parts))


def _estimate_responses_prompt_tokens(payload: dict) -> int:
    """OpenAI Responses 请求体的 prompt 规模。

    `instructions` + `input` + `tools` 三项都算。codex 的 `instructions` 单独就有 ~20k
    字符、`tools` 有 9 个函数定义，而且**每回合原样重发** —— 只数用户那句话会把 resident
    的真实 tokens/turn 低估一个数量级以上。
    """
    parts = [str(payload.get("instructions") or "")]
    for key in ("input", "tools"):
        value = payload.get(key)
        if value:
            parts.append(json.dumps(value, ensure_ascii=False))
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

            # codex 只说 OpenAI Responses（`/responses`），不说 `/chat/completions`。
            # 量 resident 基线时必须服务这条路由，否则一个请求都收不到。
            if self.path.rstrip("/").endswith("/responses"):
                self._serve_responses(raw_body)
                return

            try:
                payload = json.loads(raw_body) if raw_body else {}
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}

            # Optional native-tool behavior for the D4 V2 rollback gate.  A
            # tools-enabled request receives one valid OpenAI function call;
            # the loop's reserved tools-disabled request receives ``reply`` as
            # terminal text.  Default ``tool_call=None`` preserves the original
            # one-shot mock behavior for every other load test.
            raw_tool_call = provider.tool_call if payload.get("tools") else None
            if raw_tool_call:
                call_id = str(raw_tool_call.get("id") or "loadtest-call-1")
                call_name = str(raw_tool_call.get("name") or "memory_search")
                call_args = raw_tool_call.get("args") or {}
                assistant_message = {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": call_name,
                            "arguments": json.dumps(call_args, ensure_ascii=False),
                        },
                    }],
                }
                finish_reason = "tool_calls"
                completion_text = json.dumps(assistant_message["tool_calls"], ensure_ascii=False)
            else:
                assistant_message = {"role": "assistant", "content": provider.reply}
                finish_reason = "stop"
                completion_text = provider.reply

            if provider.estimate_tokens:
                prompt_tokens = _estimate_prompt_tokens(payload)
                completion_tokens = estimate_tokens_from_text(completion_text)
            else:
                prompt_tokens = provider.prompt_tokens
                completion_tokens = provider.completion_tokens
            # 服务端累加器：无论统一工具循环跑到第几轮，每一次 provider 调用都被计入。
            # 这是"整回合 token"唯一可靠的观测点——它不依赖调用方自报 usage，也不需要
            # 预先知道一个回合内到底发生了几次 LLM 调用。
            with provider._lock:
                provider.request_payloads.append(payload)
                provider.total_prompt_tokens += prompt_tokens
                provider.total_completion_tokens += completion_tokens
                provider.request_count += 1
            body = {
                "id": "mock-chatcmpl-0",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": assistant_message,
                        "finish_reason": finish_reason,
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

        def _serve_responses(self, raw_body: bytes) -> None:
            """OpenAI Responses wire (`POST /v1/responses`), SSE — what codex speaks.

            Token accounting covers the WHOLE request, not just the user's message:
            `instructions` (codex's own ~20k-char system prompt) + `input` + `tools`.
            That overhead is re-sent on every single turn and is the dominant term in
            the resident runtime's tokens/turn — measuring only the user's message
            would understate it by more than an order of magnitude.
            """
            try:
                payload = json.loads(raw_body) if raw_body else {}
            except json.JSONDecodeError:
                payload = {}

            if provider.estimate_tokens:
                prompt_tokens = _estimate_responses_prompt_tokens(payload)
                completion_tokens = estimate_tokens_from_text(provider.reply)
            else:
                prompt_tokens = provider.prompt_tokens
                completion_tokens = provider.completion_tokens
            with provider._lock:
                provider.total_prompt_tokens += prompt_tokens
                provider.total_completion_tokens += completion_tokens
                provider.request_count += 1

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def _event(kind: str, data: dict) -> None:
                self.wfile.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode("utf-8"))
                self.wfile.flush()

            _event("response.created",
                   {"type": "response.created", "response": {"id": "resp_mock", "status": "in_progress"}})
            _event("response.output_item.done",
                   {"type": "response.output_item.done",
                    "item": {"type": "message", "role": "assistant", "id": "msg_mock",
                             "content": [{"type": "output_text", "text": provider.reply}]}})
            _event("response.completed",
                   {"type": "response.completed",
                    "response": {"id": "resp_mock", "status": "completed",
                                 "usage": {"input_tokens": prompt_tokens,
                                           "output_tokens": completion_tokens,
                                           "total_tokens": prompt_tokens + completion_tokens}}})

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
        tool_call: dict | None = None,
        prompt_tokens: int = DEFAULT_PROMPT_TOKENS,
        completion_tokens: int = DEFAULT_COMPLETION_TOKENS,
        latency_ms: int = DEFAULT_LATENCY_MS,
        estimate_tokens: bool = False,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.reply = reply
        self.tool_call = dict(tool_call) if tool_call else None
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
        self.request_payloads: list[dict] = []

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
