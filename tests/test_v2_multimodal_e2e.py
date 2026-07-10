"""The image block must survive responder -> provider_client -> the actual HTTP body,
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
from model_api_runtime.v2 import responder as v2_responder

# provider_client.validate_config only accepts https:// or a local http://127.0.0.1 base_url.
_LOCAL = "http://127.0.0.1:9"   # never actually dialled — httpx.post is patched

_BLOCKS = [
    {"type": "text", "text": "这个报告哪里有问题"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
]


def _resp(url, reply_json):
    return httpx.Response(200, json=reply_json, request=httpx.Request("POST", url))


def test_openai_compatible_wire_carries_the_image_block(monkeypatch):
    """openai/openai_compatible/deepseek/openrouter take the native ASYNC transport
    (`_build_openai_compat_payload` passes `messages` through verbatim)."""
    captured = []

    async def _fake_apost(self, url, **kw):
        captured.append(kw.get("json"))
        return _resp(url, {"choices": [{"message": {"content": "ok"}}], "usage": {}})

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_apost)
    cfg = provider_client.ProviderConfig(
        provider="openai_compatible", model="m", api_key="k", base_url=_LOCAL)
    reply = asyncio.run(v2_responder.respond(
        provider_config=cfg, summary="", tail=[{"role": "user", "content": _BLOCKS}]))

    assert reply == "ok"
    sent = captured[0]["messages"][-1]["content"]
    assert isinstance(sent, list), "content was flattened to text — the image was dropped"
    assert sent[1]["image_url"]["url"] == "data:image/png;base64,AAAA"


def test_anthropic_wire_maps_the_image_block(monkeypatch):
    """anthropic + gemini do NOT use the async transport: reliable_chat_completion_async
    bounces them through `anyio.to_thread.run_sync(chat_completion, ...)` (provider_client
    :1200-1208), i.e. the SYNC httpx.Client. Patching httpx.AsyncClient here would capture
    nothing."""
    captured = []

    def _fake_post(self, url, **kw):
        captured.append(kw.get("json"))
        return _resp(url, {"content": [{"type": "text", "text": "ok"}]})

    monkeypatch.setattr(httpx.Client, "post", _fake_post)
    cfg = provider_client.ProviderConfig(
        provider="anthropic", model="claude-x", api_key="k", base_url="")
    reply = asyncio.run(v2_responder.respond(
        provider_config=cfg, summary="", tail=[{"role": "user", "content": _BLOCKS}]))

    assert reply == "ok"
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
        provider="openai_compatible", model="m", api_key="k", base_url=_LOCAL)
    asyncio.run(v2_responder.respond(
        provider_config=cfg, summary="", tail=[{"role": "user", "content": "just text"}]))

    assert captured[0]["messages"][-1]["content"] == "just text"
