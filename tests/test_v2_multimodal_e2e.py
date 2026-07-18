"""The image block must survive unified tool loop -> provider_client -> HTTP body,
for BOTH wire families. A unit test on the injector cannot show this — the seam between
`context.build_turn_messages` and the provider wire is exactly where a "multimodal"
change silently degrades back to text.

Parity matrix rows: §A chat image (wire).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import httpx

import provider_client
from model_api_runtime.v2 import context, tool_loop

# provider_client.validate_config only accepts https:// or a local http://127.0.0.1 base_url.
_LOCAL = "http://127.0.0.1:9"   # never actually dialled — httpx.post is patched

_BLOCKS = [
    {"type": "text", "text": "这个报告哪里有问题"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
]


def _resp(url, reply_json):
    return httpx.Response(200, json=reply_json, request=httpx.Request("POST", url))


async def _run_native_turn(provider_config, content):
    async def _dispatch(_calls):
        raise AssertionError("plain provider reply must not dispatch tools")

    async def _reply(_text, *, final):
        assert final is True

    async def _fold():
        return []

    return await tool_loop.run_tool_loop(
        provider_config=provider_config,
        build_messages=lambda _transcript: context.build_turn_messages(
            system_prompt="test", summary="", tail=[{"role": "user", "content": content}]),
        dispatch_tools=_dispatch,
        on_reply=_reply,
        fold_new_messages=_fold,
        add_usage=lambda _usage: None,
        max_calls=2,
    )


def test_openai_compatible_wire_carries_the_image_block(monkeypatch):
    """openai/openai_compatible/deepseek/openrouter take the native ASYNC transport
    (`_build_openai_compat_payload` passes `messages` through verbatim)."""
    captured = []

    async def _fake_apost(self, url, **kw):
        captured.append(kw.get("json"))
        return _resp(url, {"choices": [{"message": {"content": "ok"}}], "usage": {}})

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_apost)
    cfg = provider_client.ProviderConfig(
        provider="openai_compatible", model="m", api_key="k", base_url=_LOCAL,
        context_window_tokens=128_000)
    outcome = asyncio.run(_run_native_turn(cfg, _BLOCKS))

    assert outcome.final_text == "ok"
    sent = captured[0]["messages"][-1]["content"]
    assert isinstance(sent, list), "content was flattened to text — the image was dropped"
    assert sent[1]["image_url"]["url"] == "data:image/png;base64,AAAA"


def test_anthropic_wire_maps_the_image_block(monkeypatch):
    """anthropic now takes the native ASYNC transport too (PR B Task 7): no more
    anyio.to_thread bridge to the sync httpx.Client — chat_completion_async POSTs
    directly via `_async_http_client()`. Patching httpx.Client here would capture
    nothing."""
    captured = []

    async def _fake_apost(self, url, **kw):
        captured.append(kw.get("json"))
        return _resp(url, {"content": [{"type": "text", "text": "ok"}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_apost)
    cfg = provider_client.ProviderConfig(
        provider="anthropic", model="claude-sonnet-4-test", api_key="k", base_url="")
    outcome = asyncio.run(_run_native_turn(cfg, _BLOCKS))

    assert outcome.final_text == "ok"
    sent = captured[0]["messages"][-1]["content"]
    assert isinstance(sent, list), "content was flattened to text — the image was dropped"
    img = [p for p in sent if p.get("type") == "image"][0]
    assert img["source"] == {"type": "base64", "media_type": "image/png", "data": "AAAA"}


def test_caption_only_turn_still_sends_plain_text(monkeypatch):
    """No image blocks -> the wire shape must be unchanged from before this round."""
    captured = []

    async def _fake_apost(self, url, **kw):
        captured.append(kw.get("json"))
        return _resp(url, {"choices": [{"message": {"content": "ok"}}], "usage": {}})

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_apost)
    cfg = provider_client.ProviderConfig(
        provider="openai_compatible", model="m", api_key="k", base_url=_LOCAL,
        context_window_tokens=128_000)
    asyncio.run(_run_native_turn(cfg, "just text"))

    assert captured[0]["messages"][-1]["content"] == "just text"
