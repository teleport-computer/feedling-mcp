"""in-CVM LiteLLM gateway config — per-user model routing for codex (non-openai).

Codex 0.136 speaks the OpenAI Responses wire ONLY. To host users whose provider
is gemini / openrouter / openai_compatible, the supervisor runs a LiteLLM proxy
inside the CVM and points codex at it (``wire_api=responses``). LiteLLM fans out
to the real provider.

This module is PURE: it builds the LiteLLM proxy config + the env map the
supervisor injects into the LiteLLM subprocess. Security invariants:
  - The upstream provider key is referenced in the config by env var
    (``os.environ/FEEDLING_UPKEY_<uid>``), NEVER inlined — the on-disk config
    holds no plaintext key.
  - The decrypted key lives only in the {env_var: key} map the supervisor passes
    to the LiteLLM child's environment (in memory, never persisted to disk).
  - The gateway auth key (what codex presents as ``CODEX_API_KEY``) is LiteLLM's
    ``master_key``, also an env reference (``FEEDLING_LITELLM_API_KEY``).

Keep the provider set in sync with hosted/agent_runtime_cutover (the codex
catch-all) and agent_runtime/spawners (codex gateway transport).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys

log = logging.getLogger("feedling.agent_runtime.litellm_gateway")

# Feedling provider id → LiteLLM model prefix. openai_compatible routes through
# LiteLLM's openai handler with an explicit api_base.
_LITELLM_PREFIX = {
    "gemini": "gemini",
    "openrouter": "openrouter",
    "openai_compatible": "openai",
}
# Env-var name codex presents as its bearer to LiteLLM (the proxy master key).
GATEWAY_KEY_ENV = "FEEDLING_LITELLM_API_KEY"

_SANITIZE = re.compile(r"[^A-Za-z0-9_]")


def _norm_provider(provider: str) -> str:
    return (provider or "").strip().lower().replace("-", "_")


# Reasoning (provider "thinking") switch for gateway backends.
# ``FEEDLING_AGENT_REASONING_EFFORT`` sets the effort ("high"/"medium"/"low");
# "off"/"none"/"" disables reasoning entirely (no extra_body sent, i.e. the
# pre-reasoning behaviour). Default is "off" while the end-to-end thinking path
# is not yet verified — flip to "medium" (env or per-user) to enable. Note this
# switch only governs whether reasoning is REQUESTED; it has nothing to do with
# leak safety, which the consumer guarantees regardless.
# Per-user override arrives via the roster entry's ``reasoning_effort`` so iOS
# can later expose a per-account reasoning toggle without touching this code.
_DEFAULT_REASONING_EFFORT = (os.environ.get("FEEDLING_AGENT_REASONING_EFFORT", "off") or "").strip().lower()
_REASONING_OFF = {"", "off", "none", "no", "false", "0", "disabled"}
# The Responses `reasoning.effort` enum. codex speaks the Responses wire and
# OpenRouter serves it natively, so effort is the only reasoning shape that
# reaches the upstream (see _openrouter_reasoning_request).
_OPENROUTER_REASONING_EFFORTS = ("low", "medium", "high")


def _resolve_reasoning_effort(entry_effort: str | None = None) -> str:
    """Effective reasoning effort: per-user override wins, else the global
    default. Returns "" when reasoning is disabled (no reasoning requested)."""
    effort = (str(entry_effort or "").strip().lower()) or _DEFAULT_REASONING_EFFORT
    return "" if effort in _REASONING_OFF else effort


def _openrouter_reasoning_request(effort: str) -> dict:
    """OpenRouter reasoning params for the Responses wire.

    codex only ever speaks the OpenAI Responses wire, and OpenRouter serves it
    NATIVELY (OpenRouterResponsesAPIConfig, litellm 1.89.4) — not via the
    responses->chat bridge. The Responses ``reasoning`` object accepts
    {effort, summary}; a chat-wire {max_tokens} budget is silently dropped there
    (probed 2026-07-11 against /responses: effort -> 17 reasoning_tokens with a
    real chain, max_tokens -> 0). So emit effort for ALL OpenRouter families,
    Anthropic included. low/medium/high are the valid Responses effort values;
    anything else (incl. legacy numeric budgets) falls back to medium.
    ``summary:auto`` makes OpenRouter Anthropic return visible reasoning summary
    text; without it the response may only include encrypted_content that counts
    tokens but cannot be shown (zhihao probe 2026-07-11 §3/§6.3).
    """
    normalized = effort if effort in _OPENROUTER_REASONING_EFFORTS else "medium"
    return {"effort": normalized, "summary": "auto"}


def gateway_model_id(user_id: str) -> str:
    """The LiteLLM ``model_name`` for this user — what codex requests (its
    config.toml ``model``). LiteLLM maps it to the real upstream model+key."""
    return f"gw-{user_id}"


def upstream_env_var(user_id: str) -> str:
    """Env-var name holding this user's decrypted upstream provider key. Referenced
    by the on-disk config as ``os.environ/<name>`` so the key itself never lands
    on disk."""
    return "FEEDLING_UPKEY_" + _SANITIZE.sub("_", user_id)


def litellm_model_string(provider: str, model: str) -> str:
    """The LiteLLM ``model`` (``<prefix>/<model>``) for a Feedling provider."""
    p = _norm_provider(provider)
    prefix = _LITELLM_PREFIX.get(p, p)
    return f"{prefix}/{model}"


def build_model_entry(
    *, user_id: str, provider: str, model: str, base_url: str = "",
    supports_responses: bool = False, reasoning_effort: str | None = None,
) -> dict:
    """One LiteLLM ``model_list`` entry routing ``gw-<uid>`` to the real provider,
    keyed by an env reference (never the plaintext upstream key).

    ``supports_responses`` (openai_compatible only) selects the transport:
      - True  → the relay implements /v1/responses natively, so pass codex's
        Responses request straight through (preserves the codex tool loop the
        chat bridge mangles).
      - False → chat-only relay; force the bridge (see below). This is the
        default, matching the pre-detection behaviour and keeping chat-only
        relays working."""
    params = {
        "model": litellm_model_string(provider, model),
        "api_key": "os.environ/" + upstream_env_var(user_id),
    }
    normalized_provider = _norm_provider(provider)
    if normalized_provider == "openrouter":
        # OpenRouter serves the Responses API natively and codex speaks Responses
        # only, so the request never hits the responses->chat bridge. The
        # Responses `reasoning` object takes {effort} — the chat-wire {max_tokens}
        # budget is silently ignored there (probed 2026-07-11 against /responses:
        # effort -> reasoning_tokens>0, max_tokens -> 0). So emit effort for every
        # OpenRouter family, Anthropic included. Default-off per-user.
        effort = _resolve_reasoning_effort(reasoning_effort)
        if effort:
            params["extra_body"] = {
                "reasoning": _openrouter_reasoning_request(effort),
            }
    if normalized_provider == "openai_compatible":
        # codex `exec` always ships a `web_search` tool and offers no switch to
        # suppress it (checked on codex-cli 0.142.4: `[tools] web_search = false`,
        # `-c tools.web_search=false` and `--disable web_search_request` all leave
        # it on the wire). The responses→chat bridge below lifts that tool out of
        # `tools` into a top-level `web_search_options` carrying an empty
        # `user_location`; relays re-materialize it as Anthropic's
        # `web_search_20250305` and the upstream 400s on the empty location
        # (`tools.8.web_search_20250305.user_location: At least one field must be
        # specified.`), killing the turn before the model runs.
        #
        # `drop_params` alone cannot fix this: it only strips params the provider
        # does NOT support, and `web_search_options` IS an openai param. Only
        # `additional_drop_params` removes it — and it must ride HERE, in
        # litellm_params: `get_optional_params` reads it from the per-call kwargs
        # (which the proxy fills from this dict) and never falls back to the
        # `litellm_settings` global. Dropping it leaves the `function` tools
        # (io_cli via `exec_command`) untouched; the agent loses web search, which
        # has never once succeeded on these relays.
        params["additional_drop_params"] = ["web_search_options"]
        # Codex speaks the OpenAI Responses wire (POST /v1/responses) ONLY. LiteLLM
        # treats provider=openai as natively Responses-capable (utils.get_provider_
        # responses_api_config → OpenAIResponsesAPIConfig) and passes /v1/responses
        # straight to the upstream. For a chat-only relay that 500s on /responses,
        # this first-class flag forces LiteLLM's responses→chat-completions bridge
        # (responses/main.py `use_chat_completions_api is True`), turning codex's
        # /v1/responses into a /chat/completions call the relay supports. We set it
        # ONLY for relays that lack /responses — forcing the bridge on a relay that
        # DOES implement /responses breaks codex's tool loop.
        if not supports_responses:
            params["use_chat_completions_api"] = True
        if base_url:
            params["api_base"] = base_url
    return {"model_name": gateway_model_id(user_id), "litellm_params": params}


def build_config(entries: list[dict]) -> dict:
    """The full LiteLLM proxy config for the gateway-user set.

    ``entries`` are dicts with ``user_id``/``provider``/``model`` (+ optional
    ``base_url``); any ``provider_key`` is ignored here (it goes to the env map,
    not the config). ``drop_params`` + ``additional_drop_params`` strip the
    Anthropic-only params Claude/codex emit that non-Anthropic backends 400 on.
    ``master_key`` is the gateway bearer codex presents, by env reference."""
    return {
        "model_list": [
            build_model_entry(
                user_id=e["user_id"], provider=e["provider"],
                model=e.get("model") or "", base_url=e.get("base_url") or "",
                supports_responses=bool(e.get("supports_responses", False)),
                reasoning_effort=e.get("reasoning_effort"),
            )
            for e in entries
        ],
        "litellm_settings": {
            "drop_params": True,
            # Preserve OpenAI Responses reasoning params from codex so gateway
            # providers can emit reasoning summaries. Native Anthropic/DeepSeek
            # thinking uses the claude driver; gateway backends still drop the
            # Anthropic-only `thinking` param to avoid 400s.
            "additional_drop_params": ["thinking"],
        },
        "general_settings": {
            "master_key": "os.environ/" + GATEWAY_KEY_ENV,
        },
    }


def render_config_yaml(config: dict) -> str:
    """Serialize the config for the LiteLLM proxy ``--config`` input.

    Emitted as JSON, which is a strict subset of YAML 1.2 — LiteLLM loads its
    config via ``yaml.safe_load`` and parses this identically. Rendering JSON via
    the stdlib avoids a hard PyYAML dependency at import time (this module is
    imported by the supervisor, which must load under the hash-locked backend
    requirements that don't include PyYAML)."""
    return json.dumps(config, indent=2, sort_keys=True) + "\n"


def upstream_env(entries: list[dict]) -> dict[str, str]:
    """{env_var: decrypted upstream key} for the supervisor to inject into the
    LiteLLM subprocess env. Entries without a resolved ``provider_key`` are
    skipped (LiteLLM can't authenticate them)."""
    out: dict[str, str] = {}
    for e in entries:
        key = e.get("provider_key")
        if key:
            out[upstream_env_var(e["user_id"])] = key
    return out


def config_signature(entries: list[dict]) -> str:
    """A stable hash of the gateway-user ROUTING set (user_id/provider/model/
    base_url/reasoning) — NOT the secret keys. The supervisor restarts LiteLLM
    only when this changes, so key rotation alone doesn't bounce the proxy."""
    norm = sorted(
        (
            {
                "user_id": e["user_id"],
                "provider": _norm_provider(e["provider"]),
                "model": e.get("model") or "",
                "base_url": e.get("base_url") or "",
                "supports_responses": bool(e.get("supports_responses", False)),
                "reasoning_effort": e.get("reasoning_effort") or "",
            }
            for e in entries
        ),
        key=lambda d: d["user_id"],
    )
    return hashlib.sha256(json.dumps(norm, sort_keys=True).encode("utf-8")).hexdigest()


# ---- subprocess lifecycle (thin glue; launcher/stopper injected for tests) ----


def _default_write(path: str, content: str) -> None:
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    try:
        os.chmod(p, 0o600)  # config has no plaintext keys, but keep it tight
    except OSError:
        pass


def _default_launch(config_path: str, env: dict, port: int):
    """Start the LiteLLM proxy as a child, injecting the per-user upstream keys
    into its env (merged over the supervisor's, so FEEDLING_LITELLM_API_KEY and
    any provider SDK vars are inherited). Returns the Popen handle.

    LiteLLM is installed in its OWN venv (``FEEDLING_LITELLM_PYTHON``) so its large
    dependency tree never perturbs the supervisor's hash-locked backend env; falls
    back to the current interpreter when unset (dev)."""
    full_env = {**os.environ, **env}
    # LiteLLM proxy switches to a Prisma/Postgres-backed store the moment it sees
    # DATABASE_URL in its env, then crashes at startup ("No module named 'prisma'")
    # — the proxy venv ships no prisma and this gateway is a stateless router (its
    # whole config is the in-memory model_list). The supervisor's own DATABASE_URL
    # (RDS, for leases/heartbeats) inherits via os.environ, so strip it (and the
    # litellm-specific synonym) or every gateway turn dies in a litellm crash-loop.
    for _db_var in ("DATABASE_URL", "LITELLM_DATABASE_URL"):
        full_env.pop(_db_var, None)
    python = os.environ.get("FEEDLING_LITELLM_PYTHON", sys.executable)
    # LiteLLM has no ``__main__``, so ``python -m litellm`` aborts at startup
    # with "No module named litellm.__main__" and the proxy never binds :port.
    # The proxy ships as a ``litellm`` console script in the SAME venv bin dir
    # (litellm[proxy]); its shebang points back at this interpreter, so invoking
    # it keeps the isolated-venv guarantee while actually launching the server.
    litellm_bin = os.path.join(os.path.dirname(python), "litellm")
    return subprocess.Popen(
        [litellm_bin, "--config", config_path,
         "--port", str(port), "--host", "127.0.0.1"],
        env=full_env,
    )


def _default_stop(handle) -> None:
    try:
        if handle.poll() is None:
            handle.terminate()
            handle.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass


def _handle_alive(handle) -> bool:
    """Whether a launcher handle is still running. A Popen-like ``poll()`` returns
    None while alive, an exit code once dead; handles without ``poll`` (test fakes
    or unknown) are assumed alive."""
    if handle is None:
        return False
    poll = getattr(handle, "poll", None)
    if poll is None:
        return True
    try:
        return poll() is None
    except Exception:  # noqa: BLE001
        return False


class GatewayManager:
    """Owns the in-CVM LiteLLM subprocess lifecycle for the codex-gateway user set.

    The supervisor calls ``reconcile(entries)`` each tick. The proxy is (re)started
    when the ROUTING signature changes, when an upstream key rotates (keys are only
    injected at launch, so the running proxy would otherwise keep a stale key), or
    when the proxy has died; it's stopped when no gateway users remain.
    ``launcher``/``stopper``/``writer`` are injected so the lifecycle is
    unit-testable without a real proxy."""

    def __init__(self, *, config_path: str, port: int = 4000,
                 launcher=_default_launch, stopper=_default_stop, writer=_default_write):
        self.config_path = config_path
        self.port = port
        self._launcher = launcher
        self._stopper = stopper
        self._writer = writer
        self._sig: str | None = None
        self._env: dict[str, str] | None = None
        self._handle = None

    def reconcile(self, entries: list[dict]) -> None:
        if not entries:
            self._stop()
            return
        sig = config_signature(entries)
        env = upstream_env(entries)
        # No-op only when routing AND keys are unchanged AND the proxy is alive —
        # a crash or key rotation must still relaunch.
        if sig == self._sig and env == self._env and _handle_alive(self._handle):
            return
        self._writer(self.config_path, render_config_yaml(build_config(entries)))
        self._stop()
        try:
            self._handle = self._launcher(self.config_path, env, self.port)
            self._sig = sig
            self._env = env
            log.info("litellm gateway (re)started for %d users on :%d", len(entries), self.port)
        except Exception as e:  # noqa: BLE001
            log.error("litellm gateway launch failed: %s", e)
            self._sig = None
            self._env = None

    def _stop(self) -> None:
        if self._handle is not None:
            self._stopper(self._handle)
            self._handle = None
        self._sig = None
        self._env = None

    def shutdown(self) -> None:
        self._stop()
