"""Guard: Hosted Runtime V2 never routes through the in-CVM LiteLLM gateway child.

Parity matrix §G Q2 / §F bucket 4. This blocked deleting the agent-runner container:
if V2 secretly depended on the LiteLLM child, dropping that child would break every
gemini / openrouter / openai_compatible user.

The answer is no, and this file pins it. Two independent checks:

1. **Source guard** — no V2 or capability module may name the gateway. Cheap, catches
   an accidental `os.environ["FEEDLING_LITELLM_BASE_URL"]` creeping in.
2. **Behavioural guard** — poison the gateway endpoint, then drive the real
   `responder.respond` for each of the three providers the gateway used to bridge, and
   assert the HTTP request lands on the USER'S upstream. A source grep can be fooled by
   an indirection; an outbound URL cannot.

Why the gateway existed at all: it is a **codex-CLI** bridge. codex speaks the OpenAI
Responses wire and cannot talk to gemini/openrouter directly, so the resident runtime
ran a per-CVM LiteLLM proxy and pointed codex at it. V2 never spawns codex — its
responder/planner/extraction call `provider_client` over plain HTTP, and
`provider_client` speaks all three providers natively.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))

import httpx
import pytest

import provider_client
from model_api_runtime.v2 import responder as v2_responder

_GATEWAY_TOKENS = ("litellm", "FEEDLING_LITELLM", "gateway_model_id", ":4000")

_V2_DIR = pathlib.Path(__file__).parent.parent / "backend" / "model_api_runtime" / "v2"
_CAP_DIR = pathlib.Path(__file__).parent.parent / "backend" / "capabilities"

# serve_worker.py is the assembly layer; it may legitimately reference runner-side
# concerns. Everything else in v2/ is core turn logic and must stay gateway-free.
_EXEMPT = {"serve_worker.py", "__init__.py"}


def _modules():
    for d in (_V2_DIR, _CAP_DIR):
        for p in sorted(d.glob("*.py")):
            if p.name not in _EXEMPT:
                yield p


def test_no_v2_or_capability_module_names_the_gateway():
    offenders = {}
    for path in _modules():
        src = path.read_text()
        # Comments are fine (provider_client's docstring mentions it); we scan code lines.
        code = "\n".join(
            line for line in src.splitlines()
            if not line.lstrip().startswith("#")
        )
        hits = [t for t in _GATEWAY_TOKENS if t in code]
        if hits:
            offenders[path.name] = hits
    assert not offenders, (
        "V2 core / capabilities must not reference the in-CVM LiteLLM gateway "
        f"(it is a codex-CLI bridge; V2 calls providers directly). Offenders: {offenders}"
    )


# The three providers the LiteLLM gateway used to bridge for codex, and the user's
# REAL upstream that V2 must reach instead.
_GATEWAY_PROVIDERS = [
    ("gemini", "https://generativelanguage.googleapis.com/v1beta"),
    ("openrouter", "https://openrouter.ai/api/v1"),
    ("openai_compatible", "https://relay.example.com/v1"),
]

_POISON = "http://POISONED-GATEWAY:4000/v1"


@pytest.mark.parametrize("provider,upstream", _GATEWAY_PROVIDERS)
def test_v2_responder_reaches_the_users_upstream_not_the_gateway(monkeypatch, provider, upstream):
    """Poison the gateway endpoint. If V2 depended on the in-CVM child at all, the
    outbound request would aim at it."""
    monkeypatch.setenv("FEEDLING_LITELLM_BASE_URL", _POISON)
    monkeypatch.setenv("FEEDLING_LITELLM_ENABLE", "1")
    seen: list[str] = []

    def _sync_post(self, url, **kw):
        seen.append(str(url))
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "ok"}],
                  "candidates": [{"content": {"parts": [{"text": "ok"}]}}]},
            request=httpx.Request("POST", url))

    async def _async_post(self, url, **kw):
        seen.append(str(url))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}},
            request=httpx.Request("POST", url))

    # gemini/anthropic bounce through anyio.to_thread -> the SYNC client;
    # openrouter/openai_compatible take the native async transport. Patch both.
    monkeypatch.setattr(httpx.Client, "post", _sync_post)
    monkeypatch.setattr(httpx.AsyncClient, "post", _async_post)

    cfg = provider_client.ProviderConfig(
        provider=provider, model="m", api_key="k", base_url=upstream)
    asyncio.run(v2_responder.respond(
        provider_config=cfg, summary="", tail=[{"role": "user", "content": "hi"}]))

    assert seen, "no outbound request was made"
    assert "POISONED-GATEWAY" not in seen[0], f"V2 routed through the gateway: {seen[0]}"
    assert seen[0].startswith(upstream), f"expected the user's upstream, got {seen[0]}"


def test_gateway_stops_itself_once_the_roster_is_empty():
    """kill-resident mechanics: the D0 exclusivity guard drops db_action_v2 users from
    `db.list_agent_runtime_enabled_users`, so `_gateway_entries(roster)` yields []; and
    `GatewayManager.reconcile([])` stops the child. No separate teardown is needed."""
    from agent_runtime import litellm_gateway

    stopped = {"n": 0}
    launched = {"n": 0}
    mgr = litellm_gateway.GatewayManager(config_path="/tmp/unused.yaml", port=4000)
    mgr._stopper = lambda handle: stopped.update(n=stopped["n"] + 1)
    mgr._launcher = lambda *a, **k: launched.update(n=launched["n"] + 1)
    mgr._handle = object()          # pretend a child is running

    mgr.reconcile([])               # empty roster == every user migrated to V2

    assert launched["n"] == 0
    assert stopped["n"] == 1
    assert mgr._handle is None
