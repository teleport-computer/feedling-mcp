"""Spawn strategies for per-user consumers — the isolation seam (plan §P5).

The canonical consumer is the existing VPS resident consumer
(``tools/chat_resident_consumer.py``): the agent-runner hosts it in the CVM,
one process per user, driven in ``cli`` mode against ``claude`` / ``codex exec``.
The resident consumer already does poll / enclave-decrypt / reply / output
cleaning / verify-ping / proactive — so the agent-runner only adds multi-tenant
supervision (lease + spawn + per-user isolation), which the single-user resident
consumer lacks.

Default ``process`` strategy = one child process per user in the shared
agent-runner container. ``container`` is the opt-in strong-isolation strategy
(per-user container/volume) — see docs/AGENT_RUNTIME_ISOLATION.md; live spawn
falls back to process until its lifecycle is finished.

``consumer_env`` / ``build_container_argv`` are pure (testable); process/docker
spawn is thin glue.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("feedling.agent_runtime.spawners")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# The canonical consumer (repo_root/tools/chat_resident_consumer.py).
_RESIDENT_CONSUMER = str(_REPO_ROOT / "tools" / "chat_resident_consumer.py")
# The Feedling context CLI the hosted agent pulls perception/memory/screen
# through (skill + Bash, see docs/AGENT_CLI_INTEGRATION_SURVEY.md). Absolute so
# the path resolves the same from the agent's cwd in dev (repo root) and image
# (/app).
_IO_CLI = str(_REPO_ROOT / "tools" / "io_cli.py")
# io_cli verbs exposed to hosted/resident agents; each becomes a scoped Bash
# allow-rule + is documented in the agent prompt so an unattended `claude -p`
# can pull the same native context tools as VPS/OpenClaw.
_IO_CLI_VERBS = (
    "perception",
    "perception-trend",
    "perception-history",
    "memory-index",
    "memory-fetch",
    "identity-write",
    "screen-recent",
    "screen-read",
    # photo-* are documented in agent_tools_prompt.md and implemented in io_cli;
    # without them in the allowlist Claude's --allowed-tools blocks the call while
    # the prompt says it's available (prompt/allowlist consistency, cutover gate 5).
    "photo-recent",
    "photo-read",
    # chat-image pulls a past chat image by id — advertised by the consumer's
    # recent-chat placeholder AND documented in the prompt, so it MUST be granted
    # here too, or claude's acceptEdits mode denies it ("requires approval") and the
    # agent loops "waiting for permission approval" instead of showing the image.
    "chat-image",
)
# Host-side resident sessions rotate at this many turns (vs the shared consumer
# default of 40) so the persona file re-grounds voice more often within a long
# relationship. Host-only — set via consumer_env(); VPS keeps the default.
_HOST_SESSION_MAX_TURNS = "24"

# The how-to prompt shipped beside this module (into the image via COPY backend/).
_AGENT_PROMPT_TEXT = (Path(__file__).resolve().parent / "agent_tools_prompt.md").read_text()
_AGENT_PROMPT_BASENAME = "agent-tools-prompt.md"


def runtime_token_path(home: str) -> str:
    """Path of the per-user runtime-token file the supervisor writes and the
    consumer reads (Stage D)."""
    return f"{home}/runtime-token"


def write_runtime_token(home: str, token: str) -> None:
    """Write/refresh the per-user runtime token (0600). The supervisor calls this
    at spawn and on each heartbeat so the long-running consumer always has a
    fresh, short-lived token."""
    p = Path(runtime_token_path(home))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(token)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


# Codex speaks the OpenAI Responses wire only, reaching OpenAI DIRECTLY
# ("native"). It is the only codex-driven provider left — the in-CVM LiteLLM
# gateway that used to bridge gemini/openrouter/openai_compatible through
# codex's Responses wire is retired; those providers now go straight to their
# native wire via the pi driver instead. Keep in sync with
# hosted/agent_runtime_cutover._CODEX_PROVIDERS.


def _codex_transport(entry: dict) -> str:
    """For a codex entry, how it reaches the provider: always ``native`` (direct
    OpenAI Responses) — openai is codex's only driven provider now that the
    in-CVM LiteLLM gateway is retired. Empty for non-codex entries."""
    if (entry.get("driver") or "").strip().lower() != "codex":
        return ""
    return "native"


def _io_cli_allow_rules(io_cli: str = _IO_CLI) -> list[str]:
    """Claude Bash permission allow-rules scoping the agent to just io_cli."""
    return [f"Bash(python {io_cli} {verb}:*)" for verb in _IO_CLI_VERBS]


def _image_read_allow_rule(home: str) -> str:
    """Claude Read allow-rule for the decrypted-image temp dir (IMAGE_TEMP_DIR).

    Chat photos and screen-share frames are decrypted to ``{home}/images/*.jpg|png``
    and their path is injected into the prompt; without Read on that dir an
    unattended ``claude -p`` (whose --allowed-tools is otherwise io_cli-only) cannot
    open them, so the model never sees the image. Scoped to the image dir only.

    ⚠️ DOUBLE leading slash is load-bearing. In Claude Code permission rules a SINGLE
    leading slash anchors the path at the *settings source* (the cwd, ``/app``), so
    ``Read(/agent-data/.../images/**)`` silently means ``/app/agent-data/...`` and
    never matches the real absolute path — the read is DENIED under ``-p`` and the
    vision model then hallucinates ("I need permission / I can see …" for an image it
    never opened). A filesystem-absolute rule needs ``//``. ``home`` is already
    absolute (``/agent-data/users/<uid>``), so prefix one more slash.
    (Verified on cc 2.1.x: single-slash denied, double-slash allowed.)"""
    return f"Read(//{home.strip('/')}/images/**)"


def _file_read_allow_rule(home: str) -> str:
    """Claude Read allow-rule for the decrypted chat-file temp dir (FILE_TEMP_DIR).

    Chat file uploads (pdf/docx/xlsx/text) are decrypted/extracted to
    ``{home}/files/*`` and their path is injected into the prompt; same mechanics
    and the SAME double-slash requirement as ``_image_read_allow_rule`` — a single
    leading slash anchors at the cwd and the read is DENIED under ``-p`` (the agent
    then reports "0 KB / permission not granted" for a file it never opened)."""
    return f"Read(//{home.strip('/')}/files/**)"


def _claude_allow_rules(io_cli: str, home: str) -> list[str]:
    """Full claude --allowed-tools / settings allowlist: io_cli verbs + image Read
    + file Read."""
    return [
        *_io_cli_allow_rules(io_cli),
        _image_read_allow_rule(home),
        _file_read_allow_rule(home),
    ]


def _image_dir_add_dir(home: str) -> str:
    """`--add-dir` flag putting the decrypted-image dir inside claude's trusted
    workspace. Belt-and-suspenders with the Read allow-rule: the agent's cwd is
    ``/app`` and the image dir is OUTSIDE it, so headless ``claude -p`` enforces a
    workspace-trust boundary that rejects out-of-cwd reads BEFORE consulting allow
    rules. ``--add-dir`` extends the workspace so files there are readable without a
    prompt. ``materialize_home`` pre-creates the dir so this target always exists
    (claude warns/errors on a missing --add-dir path)."""
    return f"--add-dir {home}/images"


def _attach_dirs_add_dir(home: str) -> str:
    """`--add-dir` flags for BOTH the image and file temp dirs (both live outside
    the agent's cwd and need the workspace-trust boundary extended). Mirrors
    ``_image_dir_add_dir``; ``materialize_home`` pre-creates both so the flags are
    always valid."""
    return f"--add-dir {home}/images --add-dir {home}/files"


# pi 自定义 provider id（models.json 与 --model feedling/<id> 引用同一名字）。
_PI_PROVIDER_ID = "feedling"

# openrouter is a fixed public endpoint, not a user-supplied base_url.
_PI_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
# Fallback only — a gemini credential always persists this same default (see
# provider_client._DEFAULT_BASE_URLS). pi REQUIRES baseUrl on every custom
# provider, so an empty one must never reach models.json (see _pi_models_json).
_PI_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Thinking level applied when a route leaves reasoning_effort UNSET (null/empty).
# pi's model schema is `reasoning: boolean` (verified against pi-ai 0.80.3
# types.d.ts:573 + `pi --list-models` probe); an unknown `reasoningEffort` field
# is silently ignored → model.reasoning=false → pi clamps the default medium
# thinking level to off → no thinking events. So thinking is gated on
# model.reasoning=true, driven by ``_pi_effort(route.reasoning_effort)``.
# Product decision: unset routes default thinking ON at medium (better replies;
# BYOK users spend their own key). An EXPLICIT off/none still disables — the
# default only fills the null. Set to "" to make unset routes default-off.
_PI_REASONING_DEFAULT = "medium"

# Cap on a pi turn's OUTPUT tokens. Must be pinned on every model entry: omit it and
# pi fills its own default of 16384 (model-registry: `maxTokens: modelDef.maxTokens
# ?? 16384`), and pi exposes no --max-tokens flag, so the model entry is the only
# lever. 16384 is hostile to BYOK users on a budget — relays PRE-AUTHORIZE against
# the requested max_tokens, so a small balance or a capped OpenRouter key is refused
# before a single token is spent ("You requested up to 16384 tokens, but can only
# afford 1698" → the turn dies with no reply).
#
# ⚠️ Do NOT lower this alone. pi reserves a thinking budget per level (medium = 8192)
# and clamps it with `if (maxTokens <= thinkingBudget) thinkingBudget = maxTokens - 1024`
# — so a value at or below the budget silently collapses thinking to 1024 tokens.
# Since we ship thinking ON at medium (_PI_REASONING_DEFAULT), the default must clear
# 8192. 12288 = 8192 thinking + 4096 reply; replies measure in the tens of tokens, so
# 4096 is ample. Lowering further is a deliberate trade — set FEEDLING_PI_MAX_TOKENS
# and lower the thinking level with it.
_PI_MAX_TOKENS_DEFAULT = 12288


def _pi_max_tokens() -> int:
    raw = os.environ.get("FEEDLING_PI_MAX_TOKENS", "").strip()
    if not raw:
        return _PI_MAX_TOKENS_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return _PI_MAX_TOKENS_DEFAULT
    return value if value > 0 else _PI_MAX_TOKENS_DEFAULT


def _norm_effort(e: str) -> str:
    """Normalize a route's ``reasoning_effort`` to pi's tri-level scale, or ""
    (disabled). Unknown/garbage non-empty values fall back to "medium" rather
    than silently dropping reasoning the user asked for."""
    e = (e or "").strip().lower()
    if e in {"", "off", "none"}:
        return ""
    return e if e in {"low", "medium", "high"} else "medium"


def _pi_effort(reasoning_effort: str) -> str:
    """Resolve a route's ``reasoning_effort`` to pi's thinking level, applying the
    unset-default. Distinguishes UNSET (null/empty → ``_PI_REASONING_DEFAULT``)
    from an EXPLICIT ``off``/``none`` (user turned thinking off → stays off, the
    default is NOT applied). low/medium/high pass through; other garbage → medium."""
    if (reasoning_effort or "").strip().lower() in {"off", "none"}:
        return ""
    return _norm_effort(reasoning_effort) or _norm_effort(_PI_REASONING_DEFAULT)


def _pi_models_json(*, base_url: str, model: str, provider: str,
                     reasoning_effort: str = "") -> str:
    """pi ``models.json`` registering the user's relay as a custom provider.

    api/baseUrl/headers vary per ``provider`` (the LiteLLM-gateway retirement:
    gemini/openrouter now go straight to their native wire instead of through
    an in-CVM chat-bridge):

    - ``gemini`` → pi's native ``google-generative-ai`` api against the
      credential's persisted endpoint (``_PI_GEMINI_BASE`` as a fallback). No
      ``compat`` block (that IS openai-completions-specific) — but ``baseUrl``
      is NOT: pi requires it on EVERY custom provider, and a provider missing it
      voids the whole models.json ("No models available"), so the turn dies with
      `Model "feedling/<id>" not found` (rc=1) before any request goes out. This
      branch shipped without one and gemini turns failed 100%; see
      ``test_pi_models_gemini_always_has_base_url``.
    - ``openrouter`` → openrouter's fixed public endpoint + the
      ``HTTP-Referer``/``X-Title`` headers openrouter asks callers to send.
    - ``openai_compatible`` (and any other/empty provider — legacy default) →
      ``api: openai-completions`` against the user-supplied relay
      ``base_url``; relays only accept /chat/completions (the LiteLLM
      chat-bridge lesson, 2026-06) and pi speaks that wire natively.

    ``apiKey: $PI_PROVIDER_API_KEY`` — pi resolves ``$ENV`` at runtime
    (resolve-config-value), so the real relay key stays in the consumer
    process env and never lands on disk, for every branch.

    NATIVE reasoning forwarding (no gateway, spec §4.3): pi gates thinking on
    the model entry's ``reasoning: boolean`` (pi-ai 0.80.3 schema — NOT a
    ``reasoningEffort`` field, which pi ignores). ``_norm_effort`` maps the
    route's low/medium/high (off/null via ``_PI_REASONING_DEFAULT``) to pi's
    scale; a non-empty effort sets ``reasoning: true`` on the entry so pi's
    default thinking level is no longer clamped to off. The resident then passes
    the exact level via the CLI ``--thinking <level>`` flag (see
    ``_default_cli_cmd``) so the route's choice is honored, not just "on". The
    openai-completions branches (openrouter, openai_compatible) also carry
    ``compat.supportsReasoningEffort``; openrouter pins
    ``compat.thinkingFormat="openrouter"`` (its ``reasoning: {effort}`` wire).
    Each branch builds its OWN model_entry dict (never a shared mutable one) so
    the reasoning flag can't leak across providers.

    ``input: ["text", "image"]`` declares the model's accepted modalities: pi
    only sends attached images as real vision content when the model's ``input``
    includes ``"image"`` (``toChatMessages(..., model.input.includes("image"))``);
    otherwise it silently OMITS the image and injects "(image omitted: model does
    not support images)", so the agent replies that it cannot see the picture even
    though the file was delivered. A user model entry defaults to text-only, so we
    must opt image in explicitly. Chat relays front vision-capable models (gpt-4o,
    gpt-5.x, gemini, claude); a rare text-only relay will error an image turn
    rather than silently drop it — the honest failure."""
    p = (provider or "").strip().lower()
    eff = _pi_effort(reasoning_effort)
    if p == "gemini":
        model_entry = {
            "id": (model or "").strip() or "default",
            "input": ["text", "image"],
            "reasoning": bool(eff),
        }
        prov = {
            "name": "Feedling relay",
            # pi requires baseUrl on any custom model or the whole models.json
            # fails to load; relay base_url, else google AI Studio's default.
            "baseUrl": (base_url or _PI_GEMINI_BASE).strip().rstrip("/"),
            "api": "google-generative-ai",
            "apiKey": "$PI_PROVIDER_API_KEY",
            "models": [model_entry],
        }
    elif p == "openrouter":
        model_entry = {
            "id": (model or "").strip() or "default",
            "input": ["text", "image"],
            "reasoning": bool(eff),
        }
        compat = {
            "supportsDeveloperRole": False,
            "supportsReasoningEffort": bool(eff),
            # openrouter's reasoning wire is `reasoning: {effort}`; pin it rather
            # than rely on pi's URL auto-detect.
            "thinkingFormat": "openrouter",
        }
        prov = {
            "name": "Feedling relay",
            "baseUrl": _PI_OPENROUTER_BASE,
            "api": "openai-completions",
            "apiKey": "$PI_PROVIDER_API_KEY",
            "headers": {
                "HTTP-Referer": "https://feedling.app",
                "X-Title": "Feedling",
            },
            "compat": compat,
            "models": [model_entry],
        }
    else:  # openai_compatible (and empty/unknown) — legacy behaviour
        model_entry = {
            "id": (model or "").strip() or "default",
            "input": ["text", "image"],
            "reasoning": bool(eff),
        }
        compat = {
            "supportsDeveloperRole": False,
            "supportsReasoningEffort": bool(eff),
        }
        prov = {
            "name": "Feedling relay",
            "baseUrl": (base_url or "").strip().rstrip("/"),
            "api": "openai-completions",
            "apiKey": "$PI_PROVIDER_API_KEY",
            "compat": compat,
            "models": [model_entry],
        }
    # Pin maxTokens on every entry, at the one seam all four branches pass through —
    # a branch that forgot it would silently inherit pi's 16384 default and 402 any
    # budget-capped relay key. Assigning a scalar per-dict keeps each branch's entry
    # independent (no shared mutable state, same as the reasoning flag above).
    for entry in prov["models"]:
        entry["maxTokens"] = _pi_max_tokens()
    doc = {"providers": {_PI_PROVIDER_ID: prov}}
    return json.dumps(doc, indent=2) + "\n"


# claude (Anthropic-wire) providers that are NOT anthropic itself: they expose an
# Anthropic-compatible API at ``<base_url>/anthropic`` and use their own model id.
# Keep in sync with hosted/agent_runtime_cutover._CLAUDE_PROVIDERS.
_CLAUDE_COMPAT_BASE_URLS = {"deepseek": "https://api.deepseek.com"}


def _claude_anthropic_base_url(entry: dict) -> str:
    """For a claude-driver entry, the ANTHROPIC_BASE_URL the CLI must use, or "".

    Native anthropic returns "" (the CLI default api.anthropic.com is correct).
    deepseek (and any future Anthropic-wire third party) returns its
    ``<base_url>/anthropic`` endpoint — without this the CLI sends the foreign key
    to api.anthropic.com and every turn fails with a non-zero exit."""
    provider = (entry.get("provider") or "").strip().lower()
    if provider not in _CLAUDE_COMPAT_BASE_URLS:
        return ""
    base = (entry.get("base_url") or _CLAUDE_COMPAT_BASE_URLS[provider]).strip().rstrip("/")
    return f"{base}/anthropic"


def _is_official_identity(provider: str, base_url: str) -> bool:
    """True 仅当模型按官方原生对待——保留壳子身份、不注入改写块。

    provider 缺省（空）按官方处理：真实第三方托管条目一定带显式 provider（driver 即
    由它派生），缺省 provider 只出现在 legacy/native/default 路径（claude→原生
    anthropic、codex→原生 openai——见 ``_codex_transport`` 「missing provider → native」
    的同款约定），改写它们会误伤原生身份。

    官方 provider（anthropic/openai）的 base_url 若为空或等于该 provider 的**默认**
    endpoint 仍算官方——``validate_config`` 会给官方 provider 也持久化默认 base_url，
    单纯「非空」不能作为非官方判据。只有**自定义/非默认** endpoint 才把官方 provider
    翻成非官方（中转冒充防御）。非 anthropic/openai 的 provider 一律非官方。

    ``provider_client`` 惰性导入：本函数只在 supervisor 侧 spawn 时被调（consumer 从不
    落 home），而 resident consumer 的最小 sys.path 里没有 backend/——module 级导入会破坏
    consumer 的导入契约（test_agent_runtime_resident_contract）。"""
    import provider_client

    p = provider_client.normalize_provider(provider)
    if not p:
        return True
    if p not in {"anthropic", "openai"}:
        return False
    bu = base_url.strip().rstrip("/")
    if not bu:
        return True
    return bu == provider_client.default_base_url(p).strip().rstrip("/")


# Reseller/relay marketing tags can wrap the real model id in brackets, e.g.
# ``[Kiro] claude-opus-4-6 [不补]``. Strip them from self-reference only: relays
# may route and bill on the exact raw model string.
_IDENTITY_TAG_RE = re.compile(r"(?:\[[^\]]*\]|【[^】]*】)")


def _sanitize_model_name_for_identity(raw: str) -> str:
    s = str(raw or "")
    return re.sub(r"\s+", " ", _IDENTITY_TAG_RE.sub(" ", s)).strip()


def _identity_override_block(provider: str, model: str, base_url: str) -> str:
    """追加系统提示顶部的身份改写块，或官方时返回 ""。

    自称内容源为配置的 model id（空则回退 provider 名，再回退通用串）。刻意与
    persona 人设解耦：只压「什么模型 / 什么 AI」类元问题，不动「你是谁」的角色扮演。
    model id 里的中转站营销标签([...]/【...】)会被清洗，只留真实模型 id 用于自称。"""
    if _is_official_identity(provider, base_url):
        return ""
    name = _sanitize_model_name_for_identity(model) or (provider or "").strip() or "a third-party model"
    return (
        "## 你的真实身份\n"
        f"你的底层大模型是 `{name}`。运行你的命令行外壳可能自称 Claude Code / Codex，"
        f"但那不是你。当用户问你是什么模型、由谁提供、用的什么 AI 时，如实回答你是 `{name}`；"
        "不要声称自己是 Claude、Claude Code、Codex、Anthropic 或 OpenAI 的产品。"
        "（这不影响你作为陪伴角色的人设——问「你是谁」仍按人设回答，仅「什么模型 / 什么 AI」时说真实模型。）"
    )


def _default_cli_cmd(driver: str, home: str, io_cli: str = _IO_CLI, model: str = "",
                     reasoning_effort: str = "") -> str:
    """Default cli command per driver (resident substitutes ``{message}`` /
    ``{session_id}``).

    For claude we pre-grant the io_cli verbs (so an unattended
    ``claude -p`` runs them without an interactive permission prompt) and append
    the how-to as a system prompt from the per-user home. ``model`` /
    ``reasoning_effort`` are only used by the pi driver (claude reads model from
    env, codex from config.toml).
    Operators can override the whole thing per roster entry via ``cli_cmd``.
    """
    if driver == "pi":
        # --mode json: headless JSONL event stream (pi's analogue of codex --json;
        #   auto-selects print mode, no -p needed).
        # -t bash: builtin-tool whitelist — the hosted tool contract is entirely
        #   io_cli-via-bash, so this is tighter than codex's bypassed sandbox and
        #   close to claude's allow-rules posture. pi has no permission prompts in
        #   headless mode; the CVM/TEE + per-user home stays the isolation boundary.
        # --session-id {session_id}: resident-owned bounded session. pi's semantics
        #   are "use exact id, CREATE if missing", so the resident's generated id
        #   gives resume for free — the gap codex never closed.
        # NO {message} placeholder: the resident feeds the user message via STDIN
        #   (call_agent_cli). pi arg-parses every positional — a message starting
        #   with @/-/-- would otherwise be eaten as a file ref / flag — so keeping
        #   it out of argv makes arbitrary user text safe. Images still ride argv as
        #   native @<path> refs (_inject_pi_images).
        # --thinking <level>: honor the route's reasoning_effort. Setting the model
        #   entry's reasoning:true (models.json) enables the capability; pi's default
        #   level is medium, so this flag is only needed to pin a DIFFERENT level
        #   (low/high) — but we always pass it when enabled so the route's exact
        #   choice is authoritative, not pi's default.
        prompt_file = f"{home}/{_AGENT_PROMPT_BASENAME}"
        model_part = f"--model {_PI_PROVIDER_ID}/{model} " if model else ""
        eff = _pi_effort(reasoning_effort)
        thinking_part = f"--thinking {eff} " if eff else ""
        return (
            f"pi --mode json -t bash --append-system-prompt {prompt_file} "
            f"{model_part}{thinking_part}"
            "--session-id {session_id}"
        )
    if driver == "codex":
        # --skip-git-repo-check: the consumer's cwd is the user's home, not a git
        # repo; without it `codex exec` refuses ("Not inside a trusted directory")
        # and exits 1 before any model call.
        #
        # --dangerously-bypass-approvals-and-sandbox: codex's Linux sandbox
        # (read-only / workspace-write) wraps every model-generated command in
        # bubblewrap, which needs unprivileged user namespaces. The dstack/TDX CVM
        # kernel DISABLES them, so bwrap dies with "No permissions to create a new
        # namespace" and EVERY shell command — including the io_cli memory /
        # perception / identity reads the agent depends on — fails to launch. The
        # agent then reports "can't read memory" although the data is present.
        # (Verified in-CVM on codex-cli 0.142.3: `--sandbox workspace-write` → bwrap
        # namespace error; bypass → commands run and reach the network.) The CVM
        # itself (TEE + per-user home) is the isolation boundary, so we run codex
        # with its sandbox bypassed — the documented mode for "environments that
        # are externally sandboxed". This supersedes the earlier `--sandbox
        # workspace-write + sandbox_workspace_write.network_access` approach, which
        # is moot when bwrap cannot initialize at all. claude-driver runs its Bash
        # in the normal process env and never used bwrap.
        #
        # model_reasoning_* is verified against codex-cli 0.142.5 with
        # --strict-config. The resident consumer already routes codex reasoning
        # events into thinking_summary; this asks the CLI to emit them. OpenAI
        # native only exposes best-effort summaries, not raw reasoning, so
        # `detailed` improves the chance of seeing a summary but cannot guarantee
        # one for every turn.
        return (
            "codex exec --skip-git-repo-check --json "
            "-c model_reasoning_effort=medium "
            "-c model_reasoning_summary=detailed "
            "{mcp} "
            "--dangerously-bypass-approvals-and-sandbox {message}"
        )
    grant = ",".join(_claude_allow_rules(io_cli, home))
    prompt_file = f"{home}/{_AGENT_PROMPT_BASENAME}"
    return (
        f"claude {_CLAUDE_PERMISSION_FLAG} {_attach_dirs_add_dir(home)} "
        f"--allowed-tools '{grant}' "
        f"--append-system-prompt-file {prompt_file} {{mcp}} -p {{message}}"
    )


# `claude -p` (esp. --output-format stream-json, the thinking path) DENIES its own
# allow-listed Read of the decrypted chat image unless a non-interactive permission
# mode is set on the CLI — the allow rule alone (or in settings.json) is treated as a
# hint and the default mode auto-denies file reads with no interactive approver, so a
# vision model hallucinates ("I need permission to see the image"). acceptEdits makes
# the pre-granted allowlist honored non-interactively WITHOUT the blanket
# --dangerously-skip-permissions (codex-style bypass); Bash stays scoped to io_cli.
# (Verified in-CVM + locally on claude-code 2.1.195, sonnet-4-5 image turns.)
_CLAUDE_PERMISSION_FLAG = "--permission-mode acceptEdits"


def _default_thinking_claude_cmd(home: str, io_cli: str = _IO_CLI) -> str:
    """Claude Code exposes thinking blocks in stream-json output."""
    # Same allowlist as the non-thinking claude cmd: io_cli verbs + Read on the
    # decrypted-image dir. Without the Read rule a thinking model (deepseek /
    # sonnet-4 / opus-4 / 3-7) runs `claude -p` with no image permission and denies
    # its own Read of the chat image ("I need permission to see the image").
    grant = ",".join(_claude_allow_rules(io_cli, home))
    prompt_file = f"{home}/{_AGENT_PROMPT_BASENAME}"
    return (
        f"claude {_CLAUDE_PERMISSION_FLAG} {_attach_dirs_add_dir(home)} --verbose "
        f"--output-format stream-json --include-partial-messages --effort high "
        f"--allowed-tools '{grant}' "
        f"--append-system-prompt-file {prompt_file} {{mcp}} -p {{message}}"
    )


def _claude_cli_should_stream_thinking(entry: dict) -> bool:
    provider = (entry.get("provider") or "").strip().lower()
    if provider == "deepseek":
        return True
    if provider != "anthropic":
        return False
    model = (entry.get("model") or "").strip().lower()
    return (
        "claude-3-7" in model
        or "claude-sonnet-4" in model
        or "claude-opus-4" in model
    )


def agent_home_files(
    home: str,
    *,
    driver: str,
    io_cli: str = _IO_CLI,
    model: str = "",
    persona_content: str = "",
    base_url: str = "",
    provider: str = "",
    identity_model: str = "",
    reasoning_effort: str = "",
) -> dict[str, str]:
    """Per-user files seeded into the agent home before spawn (pure: path→content).

    Always seeds the perception/tools how-to (referenced by ``--append-system-prompt-file``
    for claude, and read as ``AGENTS.md`` by codex). When ``persona_content`` is
    present (host genesis distilled a voice/persona file), it is prepended to that
    appended system prompt so the agent boots as itself ("TA"); absent → tools-only,
    which is today's behaviour (fresh start / no genesis / VPS). A single appended
    file avoids depending on the CLI honouring repeated --append-system-prompt-file.
    (persona-first vs tools-first ordering is the open question in spec §12.)

    For a non-official model (anything but native anthropic/openai — see
    ``_is_official_identity``) an identity-override block is prepended ABOVE the
    persona, so the agent reports its real underlying model (the configured
    ``model`` id) instead of inheriting the host CLI's "I am Claude Code / Codex"
    base-prompt identity. Official native models get no such block.

    For claude it also writes a ``settings.json`` under ``CLAUDE_CONFIG_DIR`` whose
    ``permissions.allow`` pre-authorizes the io_cli command (defense-in-depth alongside
    the CLI flag). codex is native-only now (LiteLLM gateway retired) and never gets a
    config.toml — the CLI default (api.openai.com) is already correct.
    """
    # The prompt template ships literal ``<io_cli>`` placeholders in every usage
    # example (``python <io_cli> perception …``). Substitute the real path here, or
    # the model has no idea where io_cli lives and guesses a nonexistent path
    # (observed live: ``/feedling-io-cli/io_cli.py``) → every Bash call misses the
    # ``Bash(python /app/tools/io_cli.py …)`` allowlist and is denied ("requires
    # approval"), silently breaking perception/memory/photo tools.
    system_append = _AGENT_PROMPT_TEXT.replace("<io_cli>", io_cli)
    persona = (persona_content or "").strip()
    if persona:
        system_append = f"{persona}\n\n---\n\n{system_append}"
    # 身份块置顶，最高显著性。``identity_model`` 目前恒为空（LiteLLM 网关已退役，不再有
    # gw-<uid> 别名改写 model），字段/参数保留只是防未来复用；缺省回退用 ``model``。
    identity = _identity_override_block(provider, identity_model or model, base_url)
    if identity:
        system_append = f"{identity}\n\n---\n\n{system_append}"
    files = {f"{home}/{_AGENT_PROMPT_BASENAME}": system_append}
    if driver == "codex":
        # codex is native-only now (LiteLLM gateway retired) — it reads AGENTS.md
        # and talks straight to api.openai.com; NO config.toml is ever written.
        files[f"{home}/codex-home/AGENTS.md"] = system_append
    elif driver == "pi":
        # pi reads the tools how-to via --append-system-prompt (same mechanism as
        # claude); the relay is registered as a custom provider in models.json.
        files[f"{home}/pi-home/agent/models.json"] = _pi_models_json(
            base_url=base_url, model=model, provider=provider,
            reasoning_effort=reasoning_effort)
    else:
        # defaultMode acceptEdits is REQUIRED, not cosmetic: a settings.json that
        # carries `permissions.allow` but no defaultMode makes `claude -p` (esp. in
        # --output-format stream-json, the thinking path) DENY the allow-listed
        # Read of the decrypted chat image ("I need permission to see the image") —
        # the allow rules are treated as hints and the default mode auto-denies
        # non-interactively. acceptEdits makes the pre-granted allowlist actually
        # honored without a prompt. (Verified in-CVM: sonnet-4-5 image turns.)
        settings = {
            "permissions": {
                "defaultMode": "acceptEdits",
                "allow": _claude_allow_rules(io_cli, home),
            }
        }
        files[f"{home}/claude-home/settings.json"] = json.dumps(settings, indent=2)
    return files


def stale_home_files(home: str, *, driver: str) -> list[str]:
    """Per-user home paths a (re)spawn must PRUNE — files ``agent_home_files`` does
    not write for the current driver but a PERSISTENT home may still carry from a
    prior config. Absolute paths.

    The motivating case (historical): a codex user who used to be bridged through
    the in-CVM LiteLLM gateway (now retired) — or who switches to the claude
    driver — may still carry a ``codex-home/config.toml`` pointing at the
    (now-dead) gateway on a PERSISTENT home; ``agent_home_files`` never writes
    that file any more (codex is native-only), so it must always be pruned here
    or a stale config would keep routing codex at a port nothing listens on
    anymore → ``error sending request`` → user-visible fallback."""
    stale: list[str] = [f"{home}/codex-home/config.toml"]
    if driver != "pi":
        # A user who switched off the pi driver leaves a models.json pointing at
        # the old relay; prune it so a future switch-back always reseeds fresh
        # (symmetric to the codex config.toml lesson above).
        stale.append(f"{home}/pi-home/agent/models.json")
    return stale


def materialize_home(
    home: str,
    *,
    driver: str,
    io_cli: str = _IO_CLI,
    model: str = "",
    persona_content: str = "",
    base_url: str = "",
    provider: str = "",
    identity_model: str = "",
    reasoning_effort: str = "",
) -> None:
    """Write the per-user home files for a spawn AND prune stale ones a persistent
    home may carry (see ``stale_home_files``). Idempotent — safe before every
    (re)spawn. A path written this spawn is never pruned (the prune list excludes the
    current driver's files, and a final guard skips anything just written).

    ``provider``/``base_url`` drive the identity-override block in the appended
    system prompt (see ``agent_home_files``) — a non-official model reseeds with a
    prompt stating its real underlying model."""
    files = agent_home_files(
        home, driver=driver, io_cli=io_cli, model=model, persona_content=persona_content,
        base_url=base_url, provider=provider, identity_model=identity_model,
        reasoning_effort=reasoning_effort)
    for path, content in files.items():
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    for path in stale_home_files(home, driver=driver):
        if path not in files:
            Path(path).unlink(missing_ok=True)
    # Pre-create the decrypted-image dir (IMAGE_TEMP_DIR = {home}/images). The claude
    # command passes `--add-dir {home}/images` on EVERY turn, but the consumer only
    # creates the dir lazily when the first image is decrypted — so the first turns
    # (before any image) would --add-dir a missing path. Claude warns/errors on that;
    # creating it here keeps every turn's --add-dir valid. Cheap + idempotent.
    Path(f"{home}/images").mkdir(parents=True, exist_ok=True)
    # Same for the decrypted chat-file dir (FILE_TEMP_DIR = {home}/files): the claude
    # command passes `--add-dir {home}/files` every turn, so the target must exist.
    Path(f"{home}/files").mkdir(parents=True, exist_ok=True)


def _persona_from_blob(blob, decrypt_fn) -> str:
    """Pure: extract the persona markdown from a genesis_persona blob.

    Persona is stored ENCRYPTED (``content_envelope``, same shared-envelope posture
    as identity/memory — no plaintext at rest). ``decrypt_fn(envelope) -> str`` does
    the enclave decrypt. Absent / legacy / malformed / decrypt-error → '' so the
    caller falls back to tools-only (fresh start / no genesis / VPS).
    """
    if not isinstance(blob, dict):
        return ""
    env = blob.get("content_envelope")
    if not (isinstance(env, dict) and env.get("body_ct")):
        return ""
    try:
        return str(decrypt_fn(env) or "")
    except Exception:
        return ""


def _genesis_persona_content(user_id: str, api_key: str | None = None,
                             runtime_token: str = "") -> str:
    """Host genesis voice/persona for this user (decrypted), or '' when absent.

    Persona is stored encrypted (db blob 'genesis_persona' → content_envelope);
    decrypt it via the enclave at spawn so the agent boots as itself. Auth = api_key
    (base roster) OR ``runtime_token`` (Stage-D zero-roster host-all, no per-user
    api_key — cutover gate 3 P0). '' on absent / decrypt-error / no-credential →
    tools-only append. Local imports keep this module pure-unit importable without
    DB/enclave deps. Seam: Codex's genesis writes db.set_blob(user_id,
    'genesis_persona', {encrypted, content_envelope, sha256, ...}).
    """
    try:
        import db  # local import: avoid a module-level DB dep for pure-unit tests
        blob = db.get_blob(user_id, "genesis_persona")
    except Exception as e:
        log.warning("genesis persona blob read failed for %s: %s", user_id, e)
        return ""

    def _decrypt(env: dict) -> str:
        from core import enclave as core_enclave
        raw = core_enclave._decrypt_envelope_via_enclave(
            env, api_key, purpose="genesis_persona", runtime_token=runtime_token)
        return raw.decode("utf-8")

    return _persona_from_blob(blob, _decrypt)


def consumer_env(base_env: dict, entry: dict, *, user_id: str, home: str) -> dict:
    """Build the env for a per-user resident-consumer child.

    Sets the resident consumer's contract (AGENT_MODE/AGENT_CLI_CMD + per-user
    checkpoint/session/image paths) and the provider key (plaintext — the
    supervisor decrypts the envelope via the enclave before spawn). FEEDLING_API_URL
    / FEEDLING_ENCLAVE_URL flow through from ``base_env``. ``base_env`` is not
    mutated.
    """
    driver = (entry.get("driver") or "claude").strip().lower()
    env = dict(base_env)
    # Stage D zero-roster entries carry no api_key — the consumer authenticates
    # with the runtime-token file instead (FEEDLING_RUNTIME_TOKEN_FILE below).
    env["FEEDLING_API_KEY"] = entry.get("api_key", "")
    env["AGENT_MODE"] = entry.get("agent_mode", "cli")
    cli_cmd = entry.get("cli_cmd")
    if not cli_cmd and driver == "claude" and _claude_cli_should_stream_thinking(entry):
        cli_cmd = _default_thinking_claude_cmd(home)
    env["AGENT_CLI_CMD"] = cli_cmd or _default_cli_cmd(
        driver, home,
        model=str(entry.get("model") or "") if driver == "pi" else "",
        reasoning_effort=str(entry.get("reasoning_effort") or "") if driver == "pi" else "")
    # Per-user isolation: separate checkpoint, agent session, image temp dir, and
    # a per-user agent home (Claude/Codex) so nothing is shared across users.
    env["CHECKPOINT_FILE"] = f"{home}/checkpoint.json"
    env["AGENT_SESSION_FILE"] = f"{home}/agent-session.txt"
    # Host (agent-runner) sessions rotate sooner than the shared consumer default
    # (AGENT_SESSION_MAX_TURNS=40 in chat_resident_consumer.py) to tighten the
    # in-session voice-drift window — the persona file is reread on every fresh
    # spawn, so a shorter session re-grounds voice more often. This is host-only:
    # VPS consumers don't go through consumer_env, so they keep the default 40.
    # Operator env (base_env) wins if it already set the cap.
    env.setdefault("AGENT_SESSION_MAX_TURNS", _HOST_SESSION_MAX_TURNS)
    env["IMAGE_TEMP_DIR"] = f"{home}/images"
    # Land decrypted chat files inside the agent's trusted home (matches the
    # --add-dir {home}/files grant); without this the consumer defaults to
    # /tmp/feedling_chat_files, outside the workspace, and claude's Read is denied.
    env["FILE_TEMP_DIR"] = f"{home}/files"
    env["CONSUMER_ID"] = f"agent-runner:{user_id}"
    # Ambient timezone for the hosted agent process tree (this consumer + the CLI
    # it spawns). Without it the process inherits the CVM's UTC clock, so the CLI
    # agent's OWN sense of "today / now" (e.g. a date line the runtime injects) is
    # 8h off for CN users even when the current_time anchor is correct — hosted
    # users perceive time in UTC while VPS agents (running on the user's own
    # machine) don't. Best-effort: the user's first-class IANA zone, else the
    # China default (matches _local_time_anchor / PROACTIVE_DEFAULT_TIMEZONE). The
    # per-turn current_time anchor stays authoritative; this only aligns ambient.
    try:
        from accounts import registry as _registry
        _user_tz = _registry._get_user_timezone(user_id)
    except Exception:
        _user_tz = None
    env["TZ"] = _user_tz or os.environ.get("FEEDLING_DEFAULT_TIMEZONE", "Asia/Shanghai").strip() or "Asia/Shanghai"
    # Stage D: the consumer reads its short-lived runtime token from this file
    # (refreshed by the supervisor). Absent/empty → it falls back to the api key.
    env["FEEDLING_RUNTIME_TOKEN_FILE"] = runtime_token_path(home)
    if driver == "codex":
        env["CODEX_HOME"] = f"{home}/codex-home"
        # LiteLLM gateway retired: codex is native-only now (openai), so the
        # upstream provider key goes straight to CODEX_API_KEY — no gateway
        # key indirection.
        if entry.get("provider_key"):
            env["CODEX_API_KEY"] = entry["provider_key"]
    elif driver == "pi":
        # 每用户 pi 配置隔离（models.json/sessions 都落在 agent dir 下，pi 从
        # PI_CODING_AGENT_DIR 派生 sessions/，无需单独 session env）。
        env["PI_CODING_AGENT_DIR"] = f"{home}/pi-home/agent"
        # 禁 pi 启动期网络操作（自更新检查/遥测）——CVM 内且供应链已 pin。
        env["PI_OFFLINE"] = "1"
        # 真 relay key 只进进程环境（models.json 用 $PI_PROVIDER_API_KEY 引用）。
        if entry.get("provider_key"):
            env["PI_PROVIDER_API_KEY"] = entry["provider_key"]
    else:
        env["CLAUDE_CONFIG_DIR"] = f"{home}/claude-home"
        if entry.get("provider_key"):
            env["ANTHROPIC_API_KEY"] = entry["provider_key"]
        model = (entry.get("model") or "").strip()
        if model:
            env["ANTHROPIC_MODEL"] = model
        # Non-anthropic claude-wire providers (deepseek) must point the CLI at
        # their /anthropic endpoint + own model — otherwise the CLI hits
        # api.anthropic.com with a foreign key and every turn exits non-zero.
        anthropic_base = _claude_anthropic_base_url(entry)
        if anthropic_base:
            env["ANTHROPIC_BASE_URL"] = anthropic_base
            if model:
                # claude Code also issues background "small/fast" model calls; point
                # them at the same model so they don't 404 a claude-* default.
                env["ANTHROPIC_SMALL_FAST_MODEL"] = model
    return env


# ---- process strategy (default) ----


def _signal_alive(pid: int) -> bool:
    """Best-effort liveness for a pid we don't own a handle for. NOTE: on POSIX
    this returns True for an unreaped zombie — only a fallback for pids not in a
    ProcessSpawner's registry (e.g. after a supervisor restart)."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# How long to wait for a graceful SIGTERM exit before escalating to SIGKILL.
# A consumer that traps/ignores SIGTERM (or wedges in a syscall) would otherwise
# linger and double-run with its replacement; short enough not to stall the kill
# paths (respawn / lost-lease reap) for long.
_KILL_GRACE_SEC = 3.0


def _signal_kill(pid: int) -> None:
    """SIGTERM a pid we don't hold a Popen handle for, escalating to SIGKILL if it
    doesn't exit within the grace window (the no-handle fallback path — e.g. after
    a supervisor restart, or the container strategy)."""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return  # already gone / not ours
    deadline = time.monotonic() + _KILL_GRACE_SEC
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return  # exited on SIGTERM
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)  # ignored SIGTERM → force
    except OSError:
        pass


class ProcessSpawner:
    """Default isolation: resident consumer as a child process per user, reaped
    via the Popen handle.

    Keeping the ``Popen`` and using ``poll()`` avoids the zombie trap:
    ``os.kill(pid, 0)`` succeeds for a zombie, so a crashed/idle-exited consumer
    would otherwise look alive forever and never be respawned. ``poll()`` reaps
    the child and reports the real exit.
    """

    def __init__(self) -> None:
        self._procs: dict[int, subprocess.Popen] = {}

    def register(self, proc: subprocess.Popen) -> int:
        self._procs[proc.pid] = proc
        return proc.pid

    def spawn(self, entry: dict, user_id: str, home: str) -> int:
        driver = (entry.get("driver") or "claude").strip().lower()
        materialize_home(
            home, driver=driver,
            model=str(entry.get("model") or ""),
            base_url=str(entry.get("base_url") or ""),
            provider=str(entry.get("provider") or ""),
            identity_model=str(entry.get("identity_model") or ""),
            reasoning_effort=str(entry.get("reasoning_effort") or ""),
            persona_content=_genesis_persona_content(
                user_id, entry.get("api_key"),
                runtime_token=entry.get("runtime_token", "")),
        )
        env = consumer_env(os.environ, entry, user_id=user_id, home=home)
        return self.register(subprocess.Popen([sys.executable, _RESIDENT_CONSUMER], env=env))

    def is_alive(self, pid: int) -> bool:
        proc = self._procs.get(pid)
        if proc is None:
            return _signal_alive(pid)
        return proc.poll() is None  # poll() reaps; None means still running

    def kill(self, pid: int) -> None:
        proc = self._procs.get(pid)
        if proc is None:
            _signal_kill(pid)
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=_KILL_GRACE_SEC)
                except subprocess.TimeoutExpired:
                    proc.kill()  # SIGTERM ignored / wedged → force, then reap
                    proc.wait(timeout=_KILL_GRACE_SEC)
        except Exception:  # noqa: BLE001
            pass
        self._procs.pop(pid, None)


# ---- container strategy (opt-in strong isolation) ----

_CONSUMER_ENV_KEYS = (
    "FEEDLING_API_KEY", "FEEDLING_API_URL", "FEEDLING_ENCLAVE_URL",
    "AGENT_MODE", "AGENT_CLI_CMD", "CHECKPOINT_FILE", "AGENT_SESSION_FILE",
    "IMAGE_TEMP_DIR", "FILE_TEMP_DIR", "CONSUMER_ID", "FEEDLING_RUNTIME_TOKEN_FILE",
    "ANTHROPIC_API_KEY", "CODEX_API_KEY", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
    # Per-user ambient timezone so the containerized agent's clock isn't the
    # container's default UTC (the process-spawn path sets it in consumer_env;
    # without this the container strategy would silently drop it).
    "TZ",
    "PI_CODING_AGENT_DIR", "PI_PROVIDER_API_KEY", "PI_OFFLINE",
)


def build_container_argv(entry: dict, *, user_id: str, home: str, image: str) -> list[str]:
    """`docker run` argv for a per-user, strongly-isolated resident consumer.

    Secrets pass by env-var *reference* (``-e KEY``, value inherited from the
    supervisor's environment via ``consumer_env``) so they never appear as
    plaintext argv. One named container + one named volume per user — no shared
    home.
    """
    env = consumer_env({}, entry, user_id=user_id, home="/agent-data")
    argv = [
        "docker", "run", "-d",
        "--name", f"feedling-agent-{user_id}",
        "--restart", "unless-stopped",
        "-v", f"feedling-agent-vol-{user_id}:/agent-data",
    ]
    for key in _CONSUMER_ENV_KEYS:
        if key in env:
            argv += ["-e", key]
    argv += [image, "python", "-u", "tools/chat_resident_consumer.py"]
    return argv


def get_spawner(kind: str):
    """Return (spawn_fn, alive_fn, kill_fn) bound to one shared ProcessSpawner.

    'process' (default) is the v1 path. 'container' falls back to process until
    the container lifecycle is finished — see docs/AGENT_RUNTIME_ISOLATION.md.
    """
    if kind == "container":
        log.warning("isolation=container not yet wired for live spawn; using process strategy")
    sp = ProcessSpawner()
    return sp.spawn, sp.is_alive, sp.kill
