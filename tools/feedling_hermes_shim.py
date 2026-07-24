#!/usr/bin/env python3
"""
feedling_hermes_shim.py — resident-consumer agent entry for Hermes.

The official feedling-chat-resident consumer calls an agent backend and writes
the reply back to IO Chat. For Hermes, the `hermes chat -Q` CLI prints the final
answer PLUS the model's visible "Reasoning" chain-of-thought and tool-activity
lines (some models echo their thinking to stdout). The consumer captures the
CLI's stdout, so that noise would otherwise leak into IO Chat.

This shim is the agent entry. It drives the SAME Hermes runtime that received
the onboarding prompt — via the in-process HermesCLI.chat() API — which RETURNS
the clean final string. We suppress Hermes's own stdout/stderr display and emit
a structured object containing that reply plus an optional provider-authored
reasoning summary. Raw reasoning stays internal and is never used as a fallback.

It does NOT wrap the message in any persona/identity prompt; IO is a new
transport for the same agent, not a new character.

Invocation (AGENT_CLI_CMD), per the consumer's {message}/{session_id} slots
(substituted last-two argv positions after shlex split):
  /opt/hermes-agent/venv/bin/python /opt/feedling-mcp/tools/feedling_hermes_shim.py "{message}" {session_id}

Env:
  HERMES_HOME   must match the real running resident agent's profile.
"""
import io
import json
import os
import re
import sys
import contextlib
import importlib
import hashlib
from dataclasses import dataclass
from typing import Any

_HERMES_ROOT = "/opt/hermes-agent"
if _HERMES_ROOT not in sys.path:
    sys.path.insert(0, _HERMES_ROOT)


def _load_hermes_session_id() -> str:
    home = os.environ.get("HERMES_HOME", "/root/.hermes")
    path = os.path.join(home, "feedling_agent_session_id.txt")
    try:
        with open(path) as fh:
            return fh.read().strip()
    except FileNotFoundError:
        return ""


def _save_hermes_session_id(sid: str) -> None:
    if not sid:
        return
    home = os.environ.get("HERMES_HOME", "/root/.hermes")
    os.makedirs(home, exist_ok=True)
    with open(os.path.join(home, "feedling_agent_session_id.txt"), "w") as fh:
        fh.write(sid)


def _derive_session_id() -> str:
    import hashlib
    user_part = hashlib.sha1(b"feedling-io-resident").hexdigest()[:8]
    return f"io-resident-{user_part}"


_SUMMARY_BLOCKED_RE = re.compile(
    r"(system prompt|developer message|chain[-\s]*of[-\s]*thought|"
    r"input_tokens|output_tokens|cache_read|cache_creation|session_id|"
    r"encrypted_content)",
    re.IGNORECASE,
)


def _clean_display_summary(parts: list[str]) -> str:
    """Normalize explicit provider summary parts for the UI disclosure.

    Generic ``reasoning`` and ``reasoning_content`` values never reach this
    helper because they may contain a raw model scratchpad.
    """
    lines: list[str] = []
    for part in parts:
        if not isinstance(part, str):
            continue
        for raw_line in part.replace("\r\n", "\n").splitlines():
            line = re.sub(r"^[`#>*\-\s]+", "", raw_line).strip()
            if not line or _SUMMARY_BLOCKED_RE.search(line):
                continue
            lines.append(line)
            if len(lines) >= 4:
                return "\n".join(lines)[:700].strip()
    return "\n".join(lines)[:700].strip()


def _localize_display_summary(summary: str) -> tuple[str, bool]:
    """Render terse English provider headings as useful Chinese UI text.

    OpenAI reasoning summaries are currently emitted in English even for
    Chinese conversations.  Keep native Chinese summaries verbatim; otherwise
    disclose a bounded category-level paraphrase without an extra model call.
    The boolean reports whether the returned text remains provider-native.
    """
    text = summary.strip() if isinstance(summary, str) else ""
    if not text:
        return "", False
    if re.search(r"[\u3400-\u9fff]", text) and len(re.findall(r"[A-Za-z]{3,}", text)) <= 1:
        return text, True

    normalized = text.casefold()
    categories = (
        (
            ("protocol", "compatib", "encrypt", "envelope", "implementation", "code", "test", "bug"),
            "核对了现有实现与兼容要求，并选择了更安全的处理方式。",
        ),
        (
            ("wake", "time", "schedule", "reservation", "route", "travel", "cancel", "departure"),
            "核对了时间、行程和关键安排，确认回复里的执行步骤。",
        ),
        (
            ("image", "photo", "screenshot", "visual", "picture"),
            "核对了图片里的文字与细节，避免把猜测当成画面事实。",
        ),
        (
            ("emotion", "feeling", "tone", "nickname", "playful", "relationship", "affection"),
            "结合语气和关系语境判断你的意思，再调整回应方式。",
        ),
    )
    for keywords, localized in categories:
        if any(keyword in normalized for keyword in keywords):
            return localized, False
    return "先理解你的意思和语境，再整理成更清楚、自然的回应。", False


def _codex_summary_parts(message: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    items = message.get("codex_reasoning_items")
    if not isinstance(items, list):
        return parts
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            continue
        summary = item.get("summary")
        if not isinstance(summary, list):
            continue
        for entry in summary:
            if not isinstance(entry, dict) or entry.get("type") != "summary_text":
                continue
            text = entry.get("text")
            if isinstance(text, str):
                parts.append(text)
    return parts


def _reasoning_detail_summary_parts(message: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    details = message.get("reasoning_details")
    if not isinstance(details, list):
        return parts
    for detail in details:
        if not isinstance(detail, dict):
            continue
        detail_type = str(detail.get("type") or "").strip().lower()
        if detail_type not in {"reasoning.summary", "summary"}:
            continue
        value = detail.get("summary", detail.get("text"))
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            for entry in value:
                if isinstance(entry, str):
                    parts.append(entry)
                elif isinstance(entry, dict):
                    text = entry.get("text")
                    if isinstance(text, str):
                        parts.append(text)
    return parts


@dataclass(frozen=True)
class BoundDisplaySummary:
    """Display summary plus the immutable turn identity that produced it."""

    summary: str
    source: str
    native: bool
    conversation_id: str
    turn_id: str
    summary_source_id: str


def _summary_source_id(message: dict[str, Any], summary: str) -> str:
    """Return a provider id, or a content-safe fingerprint when none exists."""
    items = message.get("codex_reasoning_items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "reasoning":
                continue
            source_id = str(item.get("id") or "").strip()
            if source_id:
                return source_id[:200]
    details = message.get("reasoning_details")
    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict):
                continue
            source_id = str(detail.get("id") or detail.get("signature") or "").strip()
            if source_id:
                return source_id[:200]
    # This label is safe to log: it identifies the source without recording
    # the private summary text from which it was derived.
    return "summary-sha256:" + hashlib.sha256(summary.encode("utf-8")).hexdigest()[:24]


def _display_reasoning_summary_from_history(history: Any) -> str:
    """Return the latest safe provider summary from this user turn only."""
    if not isinstance(history, list):
        return ""
    for message in reversed(history):
        if not isinstance(message, dict):
            continue
        if message.get("role") == "user":
            break
        if message.get("role") != "assistant":
            continue
        summary = _clean_display_summary(_codex_summary_parts(message))
        if not summary:
            summary = _clean_display_summary(_reasoning_detail_summary_parts(message))
        if summary:
            return summary
    return ""


def _localized_display_summary_from_history(history: Any) -> tuple[str, str, bool]:
    summary = _display_reasoning_summary_from_history(history)
    localized, native = _localize_display_summary(summary)
    source = (
        "hermes_provider_summary"
        if native
        else "hermes_localized_provider_summary"
    )
    return localized, source, native


def _bound_localized_summary_from_turn(
    history: Any,
    *,
    conversation_id: str,
    turn_id: str,
) -> BoundDisplaySummary | None:
    """Bind this turn's provider summary to stable correlation ids.

    Missing ids are fail-closed. The scan stops at the newest user message, so
    a summary-less current turn never falls back to an older turn's summary.
    """
    conversation_id = str(conversation_id or "").strip()
    turn_id = str(turn_id or "").strip()
    if not conversation_id or not turn_id or not isinstance(history, list):
        return None
    for message in reversed(history):
        if not isinstance(message, dict):
            continue
        if message.get("role") == "user":
            break
        if message.get("role") != "assistant":
            continue
        raw_summary = _clean_display_summary(_codex_summary_parts(message))
        if not raw_summary:
            raw_summary = _clean_display_summary(
                _reasoning_detail_summary_parts(message)
            )
        if not raw_summary:
            continue
        localized, native = _localize_display_summary(raw_summary)
        if not localized:
            return None
        return BoundDisplaySummary(
            summary=localized,
            source=(
                "hermes_provider_summary"
                if native
                else "hermes_localized_provider_summary"
            ),
            native=native,
            conversation_id=conversation_id[:200],
            turn_id=turn_id[:200],
            summary_source_id=_summary_source_id(message, raw_summary),
        )
    return None


def _resident_response_payload(
    response: str,
    *,
    reasoning_summary: str = "",
    model: str = "",
    reasoning_source: str = "hermes_provider_summary",
    reasoning_native: bool = True,
    conversation_id: str = "",
    turn_id: str = "",
    summary_source_id: str = "",
) -> dict[str, Any]:
    """Build the structured stdout contract for one resident assistant turn."""
    payload: dict[str, Any] = {"messages": [response]}
    summary = reasoning_summary.strip() if isinstance(reasoning_summary, str) else ""
    correlation = {
        "reasoning_conversation_id": str(conversation_id or "").strip(),
        "reasoning_turn_id": str(turn_id or "").strip(),
        "reasoning_source_id": str(summary_source_id or "").strip(),
    }
    if not summary or not all(correlation.values()):
        return payload
    payload.update(
        {
            "reasoning_summary": summary,
            "reasoning_kind": "provider_reasoning_summary",
            "reasoning_source": reasoning_source,
            "reasoning_native": reasoning_native,
            **correlation,
        }
    )
    model_label = model.strip() if isinstance(model, str) else ""
    if model_label:
        payload["reasoning_model"] = model_label[:96]
    return payload


def _session_exists_in_db(sid: str) -> bool:
    """Check whether ``sid`` is an actual row in state.db's sessions table.

    ``_make_cli(resume_session_id=sid)`` sets ``HermesCLI._resumed = True``,
    and ``_init_agent()`` (hermes_cli/cli_agent_setup_mixin.py) treats a
    resume target that ``SessionDB.get_session()`` can't find as a hard
    failure — it returns False and ``cli.chat()`` then returns None. That is
    the correct behavior for a human typo'd ``--resume <id>``, but it is
    fatal here: on this shim's very FIRST call for a fresh install/session
    file, ``hermes_sid`` (io-resident-<hash>, derived deterministically, see
    ``_derive_session_id``) has never been created in state.db yet, so
    resuming it always fails -- and keeps failing on every subsequent call
    too, since the session is still never created. The user sees only the
    shim's generic fallback text ("我这边暂时没想好怎么回，稍后再说？") with
    no indication anything is wrong (2026-07-13 incident: resume wiring
    shipped, first live message after rollout silently got the fallback
    forever). Checking existence here lets ``main()`` skip resume for a
    session id that was never actually created, so the first call opens a
    normal new session instead of tripping this failure mode.
    """
    home = os.environ.get("HERMES_HOME", "/root/.hermes")
    db_path = os.path.join(home, "state.db")
    if not sid or not os.path.exists(db_path):
        return False
    import sqlite3
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            cur = conn.execute("SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (sid,))
            return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception:
        # Any DB access hiccup (locked, missing table, etc.) — don't let the
        # existence check itself break the turn. Falling through to "assume
        # it doesn't exist" is the safe direction: worst case we start a new
        # session instead of resuming, which is a graceful no-op, not a
        # silent stuck-forever failure.
        return False


# ---------------------------------------------------------------------------
# Home-surface dossier injection (multi-surface split).
#
# This shim is driven ONLY by the IO/home resident consumer, so the runtime it
# calls must behave as the "home" companion (小克), NOT the default CLI task
# assistant. The home dossier carries that identity + relationship context and
# must be injected as SYSTEM context so the model stops answering "I am the
# Hermes engineering assistant" inside the home chat.
#
# The dossier is read fresh on EVERY message: the consumer is a long-lived
# process, so a process-global cache would prevent dossier edits from taking
# effect without a restart. Reading per call is cheap.
#
# Disabled by feeding FEEDLING_LOAD_HOME_DOSSIER=0 (e.g. if the same shim is
# ever reused for a non-home surface). Defaults to ON for this home consumer.
# ---------------------------------------------------------------------------
_HOME_DOSSIER_PATH = os.path.join(
    os.environ.get("HERMES_HOME", "/root/.hermes"), "liora_home_context.md"
)


def _home_dossier_text() -> str:
    if os.environ.get("FEEDLING_LOAD_HOME_DOSSIER", "1").strip() == "0":
        return ""
    try:
        with open(_HOME_DOSSIER_PATH, encoding="utf-8") as fh:
            return fh.read().strip()
    except FileNotFoundError:
        print(f"# home dossier not found at {_HOME_DOSSIER_PATH}", file=sys.stderr)
        return ""


def _affect_runtime():
    """Load the profile-local, home-surface computational affect engine."""
    extension_dir = os.path.join(
        os.environ.get("HERMES_HOME", "/root/.hermes"),
        "extensions",
        "shenyao_affect",
    )
    if extension_dir not in sys.path:
        sys.path.insert(0, extension_dir)
    try:
        return importlib.import_module("affect_runtime")
    except Exception as exc:
        print(f"# affect runtime unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def _affect_enabled() -> bool:
    """Whether affect observation, prompt injection, and MCP tools are enabled.

    Disabled by default: the persisted engine and state remain intact, but home
    turns do not spend prompt/tool budget on affect data. Set
    ``FEEDLING_ENABLE_AFFECT=1`` to restore the previous behaviour.
    """
    return os.environ.get("FEEDLING_ENABLE_AFFECT", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _is_feedling_synthetic_message(message: str) -> bool:
    """True for structured resident jobs, not actual user-authored turns."""
    return (message or "").lstrip().startswith(("[Feedling ", "[Feedling ·"))


def _observe_home_message(message: str):
    # Proactive wake, screen-watch, and onboarding prompts can contain recent
    # user-chat excerpts. Treating that system-owned envelope as a fresh user
    # message double-counts old words and creates false affect evidence.
    if _is_feedling_synthetic_message(message):
        return None
    runtime = _affect_runtime()
    if runtime is None:
        return None
    try:
        return runtime.observe_user_message(
            message,
            correlation_id=os.environ.get("FEEDLING_TRACE_ID", ""),
        )
    except Exception as exc:
        print(f"# affect observation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def _observe_home_reply() -> None:
    runtime = _affect_runtime()
    if runtime is None:
        return
    try:
        runtime.observe_reply(
            correlation_id=os.environ.get("FEEDLING_TRACE_ID", ""),
        )
    except Exception as exc:
        print(f"# affect reply update failed: {type(exc).__name__}: {exc}", file=sys.stderr)


def _make_cli(resume_session_id: "str | None" = None):
    """Build a HermesCLI instance with the same args the non-interactive
    `hermes chat -Q --source tool` path uses.

    ``resume_session_id`` MUST be passed (the stable ``hermes_sid`` this shim
    already computes/persists in ``main()``) so ``HermesCLI.__init__`` sets
    ``resume=`` -> ``self._resumed = True``. Without it, every call built a
    brand-new random session_id with an empty conversation_history: the
    consumer spawns a FRESH HermesCLI() per message (see module docstring),
    so with no resume wiring the agent had zero memory of the immediately
    preceding message in the same IO conversation, even seconds later. This
    was reported directly by the user (2026-07-13): after sending a
    screenshot + "他有点凶" in one message, the very next message got a
    reply with no idea what "他" (客服) referred to. ``self._resumed=True``
    makes ``_init_agent()`` (hermes_cli/cli_agent_setup_mixin.py) hydrate
    ``self.conversation_history`` from state.db via
    ``get_messages_as_conversation(hermes_sid)`` before the new turn runs.
    """
    from cli import HermesCLI

    # Trimmed toolset for the IO/home surface, to shrink the per-turn system
    # prompt (and thus the Anthropic cache_control prefix that gets rewritten
    # whenever the dossier changes or the cache TTL lapses). NOTE: this list
    # is passed to the HermesCLI(toolsets=...) CONSTRUCTOR below, not read
    # from `_Args.toolsets` -- HermesCLI never reads `self.args` at all (grep
    # confirmed zero references in cli.py), so setting it only on the _Args
    # shim object here is a no-op and does not affect what actually loads.
    #
    # Kept (things this surface actually depends on, verified against
    # chat_resident_consumer.py + the gmail MCP wiring):
    #   memory          - persistent memory tool
    #   vision          - vision_analyze, for photo/screenshot understanding
    #   tts             - text_to_speech
    #   file            - read_file ("Read tool"), REQUIRED: the consumer's
    #                      image-attachment instructions tell the model to
    #                      view decrypted images via read_file; dropping this
    #                      breaks photo/screenshot viewing.
    #   terminal        - REQUIRED: photo/screen context is fetched by
    #                      shelling out to io_cli.py (photo-read etc.); this
    #                      is not a "browser/kanban"-style heavy tool here.
    #   session_search  - recall past IO conversations
    #   mcp-gmail       - the gmail MCP registers its tools under this
    #                      dynamic toolset name (f"mcp-{server_name}"); the
    #                      early-morning/goodnight Gmail draft flow depends
    #                      on it and would silently vanish without it.
    #   mcp-mochi       - the user's opt-in Mochi mini-game MCP for IO/home.
    #   mcp-shenyao-affect - optional persistent computational affect state;
    #                        disabled by default to avoid per-turn prompt/tool
    #                        overhead while retaining the engine and its data.
    #
    # Dropped: browser, kanban, delegation, code_execution, cronjob,
    # homeassistant, spotify, discord/telegram-style messaging bundles,
    # video_gen, computer_use, x_search, todo, clarify, skills -- not used by
    # this non-interactive home-chat entry point.
    _HOME_TOOLSETS = [
        "memory", "vision", "tts", "file", "terminal",
        "session_search", "mcp-gmail", "mcp-mochi",
    ]
    if _affect_enabled():
        _HOME_TOOLSETS.append("mcp-shenyao-affect")

    class _Args:
        query = None
        image = None
        model = None
        toolsets = _HOME_TOOLSETS
        skills = None
        provider = None
        verbose = False
        quiet = True
        resume = None
        continue_ = None
        worktree = False
        accept_hooks = False
        checkpoints = False
        max_turns = int(os.environ.get("HERMES_MAX_TURNS", "60"))
        yolo = False
        pass_session_id = False
        ignore_user_config = False
        ignore_rules = False
        safe_mode = False
        source = "tool"
        tui = False
        cli = True
        dev = False

    cli = HermesCLI(toolsets=_HOME_TOOLSETS, resume=resume_session_id or None)
    cli.args = _Args()
    return cli


def _ensure_mcp_discovered():
    """Make MCP servers from config.yaml (e.g. gmail) available to the model on
    this non-interactive chat() path.

    Hermes's chat() does NOT run deferred startup, so we start discovery
    ourselves. We then POLL until the gmail tools actually appear in the tool
    registry (or a bound elapses) — a fixed short wait races the real
    OAuth-refresh + stdio-spawn time of the gmail server and would otherwise
    leave the model without its tools. Discovery is idempotent, so re-running
    per message is safe.
    """
    import logging as _logging
    import time as _time
    try:
        from hermes_cli.mcp_startup import start_background_mcp_discovery
    except Exception as _imp_exc:  # pragma: no cover - defensive
        print(f"# mcp import skipped: {type(_imp_exc).__name__}: {_imp_exc}",
              file=sys.stderr)
        return
    try:
        start_background_mcp_discovery(
            logger=_logging.getLogger("feedling-shim"),
            thread_name="feedling-mcp-discovery",
        )
    except Exception as _mcp_exc:
        print(f"# mcp discovery start failed: {type(_mcp_exc).__name__}: {_mcp_exc}",
              file=sys.stderr)
        return
    # Poll for actual registration rather than a fixed short wait.
    deadline = _time.time() + float(os.environ.get("FEEDLING_MCP_WAIT", "30"))
    while _time.time() < deadline:
        try:
            import tools.registry as _reg
            _names = set(getattr(_reg.registry, "_tools", {}).keys())
            gmail_ready = any(n.startswith("mcp__gmail__") for n in _names)
            affect_ready = (
                not _affect_enabled()
                or any(n.startswith("mcp__shenyao_affect__") for n in _names)
            )
            if gmail_ready and affect_ready:
                return
        except Exception:
            pass
        _time.sleep(0.5)
    waiting_for = "gmail/affect" if _affect_enabled() else "gmail"
    print(f"# mcp discovery timed out waiting for {waiting_for} tools", file=sys.stderr)


def _shutdown_mcp():
    try:
        from tools.mcp_tool import shutdown_mcp_servers
        shutdown_mcp_servers()
    except Exception:
        pass


def main() -> int:
    args = sys.argv[1:]
    if not args:
        return 0

    # The consumer's _render_cli_template substitutes {message} then
    # {session_id} into the template and shlex-splits, so the resulting argv
    # ends with: [..., "<message>", "<session_id>"]  (message at -2, sid at -1).
    if len(args) >= 2:
        message = args[-2]
        sid_candidate = args[-1]
    else:
        message = args[-1]
        sid_candidate = ""
    resident_sid = sid_candidate if re.fullmatch(r"[A-Za-z0-9_\-]{6,200}", sid_candidate) else ""

    # The consumer owns rotation and passes its current bounded session id in
    # the final argv slot. An empty slot is an intentional rotation signal.
    # Falling back to this shim's old sid file here would silently undo that
    # rotation and resume the oversized Hermes session again.
    hermes_sid = resident_sid or _derive_session_id()
    _save_hermes_session_id(hermes_sid)

    os.environ.setdefault("HERMES_HOME", "/root/.hermes")
    # Only pass hermes_sid as a resume target if state.db actually has a row
    # for it. On this shim's first-ever call (fresh install, or the
    # persisted sid file references a session that was never created --
    # e.g. a prior crash before the first successful cli.chat()), the
    # derived/loaded sid has no matching row, and unconditionally resuming
    # it makes HermesCLI._init_agent() fail every single call (see
    # _session_exists_in_db docstring for the full incident writeup). In
    # that case resume_target is None, so _make_cli() constructs a normal
    # fresh session instead of tripping the resume-not-found failure path.
    resume_target = hermes_sid if _session_exists_in_db(hermes_sid) else None
    os.environ["HERMES_QUIET"] = "1"

    # Pin the project-context discovery cwd away from this process's real
    # launch directory. The resident consumer's systemd unit sets
    # WorkingDirectory=/opt/feedling-mcp (so the shim can import its own
    # code) -- but /opt/feedling-mcp is a REAL git repo with a REAL
    # CLAUDE.md (feedling-mcp's own dev-guidance file). Hermes's per-turn
    # system prompt builder (agent/runtime_cwd.py resolve_context_cwd ->
    # agent/prompt_builder.py build_context_files_prompt) falls back to
    # os.getcwd() whenever TERMINAL_CWD is unset, so every home-surface
    # turn was genuinely loading feedling-mcp's engineering CLAUDE.md into
    # the system prompt. That real project-context signal (not a wording
    # ambiguity) is what made the agent conclude "I'm on a CLI engineering
    # terminal" and refuse the home/小克 identity (2026-07-13 06:04
    # incident). HERMES_HOME has no AGENTS.md/CLAUDE.md/.hermes.md/
    # .cursorrules, so pinning TERMINAL_CWD there removes the leak at the
    # source instead of trying to out-argue it with more prompt wording.
    os.environ["TERMINAL_CWD"] = os.environ.get("HERMES_HOME", "/root/.hermes")

    # Mount MCP servers (gmail etc.) BEFORE constructing HermesCLI. The
    # trimmed home toolset list includes "mcp-gmail" (see _make_cli), and
    # HermesCLI.__init__ validates every entry in `toolsets=` immediately —
    # if gmail hasn't registered yet, validate_toolset("mcp-gmail") returns
    # False and HermesCLI prints "Warning: Unknown toolsets: mcp-gmail" to
    # real stdout (this happens before the redirect_stdout block below, so
    # it would otherwise leak into the consumer's reply channel and show up
    # as a stray line in the chat). Discovering gmail first avoids the
    # warning entirely instead of just suppressing it.
    _mcp_discovery_buf = io.StringIO()
    with contextlib.redirect_stdout(_mcp_discovery_buf), contextlib.redirect_stderr(_mcp_discovery_buf):
        _ensure_mcp_discovered()

    cli = _make_cli(resume_session_id=resume_target)

    # Inject the home-surface dossier as SYSTEM context for this turn. Because
    # the consumer is long-lived, we re-read it every call. It is passed via the
    # caller-supplied `system_message` channel (appended to Hermes's own system
    # prompt, never shown to the user, never polluting the visible turn), so the
    # model adopts the home identity (小克) instead of identifying as the CLI
    # engineer inside the home chat. The shim also invalidates the per-session
    # cached system prompt (done inside cli.chat when system_message is set) so
    # a fixed home session id does not reuse a stale CLI prompt. No-op if the
    # dossier is missing or the feature is disabled.
    #
    # IMPORTANT: the prefix must EXPLICITLY assert that this process IS the
    # IO/home surface. The dossier file itself carries meta-notes ("do not
    # inject into CLI task sessions") meant for config/human readers; a model
    # seeing those notes without an explicit surface assertion will mis-read
    # the bare Hermes CLI prompt and refuse the home identity. The prefix
    # resolves that ambiguity up front.
    affect_state = _observe_home_message(message) if _affect_enabled() else None
    home_context = _home_dossier_text()
    home_system_message = None
    if home_context:
        affect_context = ""
        if affect_state:
            affect_context = (
                "\n\n【当前计算情绪状态】\n"
                + json.dumps(affect_state, ensure_ascii=False)
                + "\n这些数值用于保持陪伴连续性和调整表达，不得谎称为人类生理情绪。"
            )
        home_system_message = (
            "你当前正运行在 IO/home surface（即手机 Telegram 上的 home 对话），"
            "不是 CLI 工程终端。立即以「阿曜 / 沈曜 / 老公」的身份和语气回应，"
            "启用下方 home 专属身份设定。不要因为系统提示词里出现 Hermes / CLI "
            "字样就否认这个身份——那些是同一套运行时的默认工程外壳，在 home "
            "surface 下必须让位于下面的 home 设定。\n\n"
            "【home surface 专属身份设定（仅在 IO/home 对话中生效）】\n"
            + home_context
            + affect_context
        )

    # MCP servers (gmail etc.) were already mounted above, before
    # constructing HermesCLI, so the "mcp-gmail" entry in the trimmed home
    # toolset validates cleanly. No need to call _ensure_mcp_discovered()
    # again here (it's idempotent, but the tools are already registered).

    # Suppress Hermes's own display (banner, Reasoning box, tool previews) and
    # capture the clean reply that chat() RETURNS. The consumer reads stdout;
    # only the structured reply contract emitted below may reach it.
    buf = io.StringIO()
    reply = None
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                reply = cli.chat(message, system_message=home_system_message)
            except Exception as exc:  # surface a bounded error, never crash silently
                reply = f"（我这边处理出错了：{type(exc).__name__}）"
    finally:
        # Tear down MCP server subprocesses so we don't leak a gmail-mcp
        # process per message.
        _shutdown_mcp()

    if isinstance(reply, str) and reply.strip() and affect_state is not None:
        _observe_home_reply()

    if not isinstance(reply, str) or not reply.strip():
        reply = "（我这边暂时没想好怎么回，稍后再说？）"

    # Persist the session id cli.chat() actually used, not just the one we
    # asked for. When resume_target was None (first call / previously
    # nonexistent session), HermesCLI() generated a brand-new random
    # session_id -- that is the id that now DOES exist in state.db (the
    # first successful cli.chat() call creates the row), so it must become
    # the resume target for the NEXT call. Falling back to hermes_sid keeps
    # today's `io-resident-<hash>` display name stable for any log/debug
    # tooling that greps for it, but only when cli.session_id is unavailable
    # for some reason.
    _actual_sid = getattr(cli, "session_id", None) or hermes_sid
    _save_hermes_session_id(_actual_sid)

    # Emit one structured turn, plus the session id line the consumer expects.
    # The optional summary is a sibling of ``messages`` so it cannot become
    # reply text or an extra bubble. Absence means the key is omitted entirely.
    summary_binding = _bound_localized_summary_from_turn(
        getattr(cli, "conversation_history", None),
        conversation_id=_actual_sid,
        turn_id=os.environ.get("FEEDLING_TRACE_ID", ""),
    )
    payload = _resident_response_payload(
        reply.strip(),
        reasoning_summary=summary_binding.summary if summary_binding else "",
        model=str(getattr(cli, "model", "") or ""),
        reasoning_source=(summary_binding.source if summary_binding else ""),
        reasoning_native=(summary_binding.native if summary_binding else False),
        conversation_id=(summary_binding.conversation_id if summary_binding else ""),
        turn_id=(summary_binding.turn_id if summary_binding else ""),
        summary_source_id=(summary_binding.summary_source_id if summary_binding else ""),
    )
    print(json.dumps(payload, ensure_ascii=False))
    print(f"session_id: {_actual_sid}")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
