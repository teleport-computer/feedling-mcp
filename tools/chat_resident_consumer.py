#!/usr/bin/env python3
"""
Feedling Chat Resident Consumer
================================
Polls /v1/chat/poll, routes each user message to a configured agent backend,
and writes the reply back via /v1/chat/response.

Supports two agent backend modes (set AGENT_MODE env var):

  http  — POST the user message to an HTTP endpoint and read the response body.
          Supports simple JSON endpoints and Hermes' OpenAI-compatible
          /v1/chat/completions API.

  cli   — Run a shell command with the user message passed via --query/-q flag.
          Works with any CLI agent that writes its reply to stdout.
          Prefer machine-readable JSON stdout with a final-answer field such as
          {"reply": "..."}; plain-text stdout is supported only as a fallback.
          See SKILL.md § "Chat Resident Consumer" for per-agent configuration
          requirements.

Required env vars (all keys go in CHAT_RESIDENT_ENV_FILE, never hardcoded):
  FEEDLING_API_URL      Base URL of the Feedling backend (e.g. http://localhost:5001)
  FEEDLING_API_KEY      Per-user API key from POST /v1/users/register
  AGENT_MODE            "http" or "cli"

HTTP mode:
  AGENT_HTTP_URL        Endpoint to POST user messages to
  AGENT_HTTP_TOKEN      Bearer token (optional)
  AGENT_HTTP_PROTOCOL   "simple" (POST {"message"}) or "openai" for Hermes
  AGENT_HTTP_FIELD      JSON response field containing the reply (default: "response")

CLI mode:
  AGENT_CLI_CMD         Full command template; {message} is replaced with the
                        user's message text.
                        Image messages can also use {image_path} or
                        {image_paths}; otherwise the path is appended to
                        the message text.
                        Example (Hermes): hermes chat -Q --source tool --max-turns 60 -q "{message}"
                        Example (plain):  mycli ask {message}
                        For Hermes, the consumer stores session_id and
                        auto-injects --resume on later turns.
  AGENT_CLI_PATH        Optional colon-separated executable search path added
                        before PATH. Useful for systemd services.

Optional:
  CHECKPOINT_FILE       Path to persist last-processed timestamp.
                        Default is scoped by API key to avoid cross-account
                        cursor reuse: /tmp/feedling_chat_checkpoint_<keyhash>.json
  PROACTIVE_POLL_ENABLED
                        Default true. Poll hidden proactive jobs created by
                        the proactive wake scheduler and realize them through the same agent
                        entry used for chat replies.
  PROACTIVE_POLL_TIMEOUT
                        Short long-poll timeout for proactive jobs (default: 1)
  PROACTIVE_TICK_ENABLED
                        Default true. Periodically post agent-owned proactive
                        wake ticks.
  PROACTIVE_TICK_INTERVAL_SEC
                        Broadcast-on/unknown tick interval in seconds (default: 300)
  PROACTIVE_TICK_BROADCAST_OFF_INTERVAL_SEC
                        Broadcast-off tick interval in seconds (default: 7200)
  PROACTIVE_TICK_START_DELAY_SEC
                        Delay before the first automatic wake tick (default: 15)
  PROACTIVE_SCHEDULED_FIRE_ENABLED
                        Default true. Poll resident-owned scheduled_wake timers
                        and enqueue due hidden jobs.
  PROACTIVE_SCHEDULED_FIRE_INTERVAL_SEC
                        Scheduled wake fire cadence in seconds (default: 60)
  WHOAMI_REFRESH_RETRIES
                        Short retry count before encrypted reply writes (default: 3)
  WHOAMI_REFRESH_RETRY_DELAY_SEC
                        Initial reply whoami retry backoff in seconds (default: 0.5)
  SEND_FALLBACK_ON_AGENT_ERROR
                        Default true. Agent failures post a visible, bounded
                        failure reply instead of silently dropping the turn.
  FALLBACK_REPLY        Optional user-visible fallback text
  AGENT_SESSION_MAX_TURNS / AGENT_SESSION_MAX_BYTES
                        Bound resident-owned CLI/HTTP sessions. When either
                        limit is reached, the next turn starts a fresh session.
  IMAGE_TEMP_DIR        Where decrypted chat images are written for CLI agents
  SCREEN_CONTEXT_MODE   "on_mention" (default), "always", or "off". When active,
                        recent screen-sharing context is attached to screen
                        questions so the agent does not need to run curl/MCP
                        commands from its own sandbox.
  POLL_TIMEOUT          Long-poll timeout in seconds (default: 30)
  LOG_LEVEL             DEBUG / INFO / WARNING (default: INFO)
"""

import base64
from dataclasses import dataclass, field
import hashlib
import inspect
import json
import logging
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# v1 Envelope encryption (same logic as mcp_server.py / _whoami_pubkeys)
# ---------------------------------------------------------------------------
# The backend's build_envelope lives in backend/content_encryption.py.
# We add that directory to the path so the consumer can encrypt replies
# without duplicating crypto code.

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
try:
    from content_encryption import build_envelope as _build_envelope
    _ENCRYPTION_AVAILABLE = True
except ImportError:
    _ENCRYPTION_AVAILABLE = False

from memory.capture_prompt_v1 import build_capture_prompt, parse_capture_cards
from memory.dream_prompt_v1 import build_dream_prompt, parse_dream_consolidations
from memory.migrate_prompt_v1 import build_migrate_prompt, parse_migrated_cards

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("feedling.resident")


@dataclass
class AgentTurn:
    """Canonical shape for one upstream agent turn.

    Raw provider / CLI output may contain final messages, visible reasoning
    summaries, tool/action intents, and runtime diagnostics in the same JSON
    object. The resident must classify those buckets before it writes anything
    to IO Chat; user-visible chat may only receive messages plus an optional
    display-safe thinking summary.
    """

    messages: list[str] = field(default_factory=list)
    thinking_summary: str = ""
    thinking_kind: str = ""
    thinking_source: str = ""
    thinking_model: str = ""
    thinking_native: bool | None = None
    actions: list[dict] = field(default_factory=list)
    runtime_debug: dict = field(default_factory=dict)
    tool_calls: list[dict] = field(default_factory=list)


@dataclass
class ProactiveChatContext:
    text: str = ""
    freshness: str = "empty"
    included_count: int = 0
    last_message_age_sec: float | None = None
    last_user_message_age_sec: float | None = None
    last_visible_proactive_age_sec: float | None = None
    visible_proactive_count_24h: int = 0


def _mask(val: str) -> str:
    if not val or len(val) < 8:
        return "***"
    return val[:4] + "***" + val[-4:]


def _fingerprint_bytes(val: bytes | None) -> str:
    if not val:
        return "missing"
    return hashlib.sha256(val).hexdigest()[:12]


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FEEDLING_API_URL = os.environ["FEEDLING_API_URL"].rstrip("/")
FEEDLING_API_KEY = os.environ["FEEDLING_API_KEY"]
AGENT_MODE = os.environ.get("AGENT_MODE", "http").lower()

AGENT_HTTP_URL = os.environ.get("AGENT_HTTP_URL", "")
AGENT_HTTP_TOKEN = os.environ.get("AGENT_HTTP_TOKEN", "")
AGENT_HTTP_FIELD = os.environ.get("AGENT_HTTP_FIELD", "response")
AGENT_HTTP_PROTOCOL = os.environ.get("AGENT_HTTP_PROTOCOL", "simple").lower()
AGENT_HTTP_MODEL = os.environ.get("AGENT_HTTP_MODEL", "hermes-agent")
AGENT_HTTP_SESSION_KEY = os.environ.get("AGENT_HTTP_SESSION_KEY", "")
AGENT_HTTP_SESSION_HEADER = os.environ.get(
    "AGENT_HTTP_SESSION_HEADER", "X-Hermes-Session-Id"
)
AGENT_HTTP_SESSION_KEY_HEADER = os.environ.get(
    "AGENT_HTTP_SESSION_KEY_HEADER", "X-Hermes-Session-Key"
)

AGENT_CLI_CMD = os.environ.get("AGENT_CLI_CMD", "")
AGENT_CLI_PATH = os.environ.get("AGENT_CLI_PATH", "")

CHECKPOINT_API_KEY_FINGERPRINT = hashlib.sha1(FEEDLING_API_KEY.encode()).hexdigest()[:10]
CHECKPOINT_FILE = Path(
    os.environ.get(
        "CHECKPOINT_FILE",
        f"/tmp/feedling_chat_checkpoint_{CHECKPOINT_API_KEY_FINGERPRINT}.json",
    )
)
PROACTIVE_JOB_SOURCE = "agent_initiated_proactive"
RESIDENT_CHAT_RUNTIME_V2_FLAG = "resident_chat_runtime_v2_enabled"
PROACTIVE_POLL_ENABLED = _env_bool("PROACTIVE_POLL_ENABLED", True)
PROACTIVE_POLL_TIMEOUT = int(os.environ.get("PROACTIVE_POLL_TIMEOUT", "1"))
PROACTIVE_TICK_ENABLED = _env_bool("PROACTIVE_TICK_ENABLED", True)
PROACTIVE_TICK_INTERVAL_SEC = int(os.environ.get("PROACTIVE_TICK_INTERVAL_SEC", "300"))
PROACTIVE_TICK_BROADCAST_ON_INTERVAL_SEC = int(
    os.environ.get("PROACTIVE_TICK_BROADCAST_ON_INTERVAL_SEC", str(PROACTIVE_TICK_INTERVAL_SEC))
)
# Fallback heartbeat cadence when the backend tick decision carries no per-user
# wake_interval_sec (legacy / rollout). Default aligned to the product default of
# 2h (7200) set 2026-07-04, so no path silently reverts to the old 30min.
PROACTIVE_TICK_BROADCAST_OFF_INTERVAL_SEC = int(
    os.environ.get("PROACTIVE_TICK_BROADCAST_OFF_INTERVAL_SEC", "7200")
)
PROACTIVE_TICK_START_DELAY_SEC = int(os.environ.get("PROACTIVE_TICK_START_DELAY_SEC", "15"))
# Screen-watch lane — decoupled from the heavy heartbeat. While the user is
# actively screen-sharing, a lightweight loop lets the agent look at recent
# frames every SCREEN_WATCH_INTERVAL_SEC, but ONLY when the screen actually
# changed and the user is not mid-conversation. It carries frames + a names-only
# tool list, NOT the cross-domain board / full tool catalog. The heartbeat keeps
# its own (broadcast-independent) cadence.
SCREEN_WATCH_ENABLED = _env_bool("FEEDLING_SCREEN_WATCH_ENABLED", True)
SCREEN_WATCH_INTERVAL_SEC = int(os.environ.get("FEEDLING_SCREEN_WATCH_INTERVAL_SEC", "120"))
SCREEN_WATCH_CHAT_SUPPRESS_SEC = int(os.environ.get("FEEDLING_SCREEN_WATCH_CHAT_SUPPRESS_SEC", "180"))
SCREEN_WATCH_FRAMES = int(os.environ.get("FEEDLING_SCREEN_WATCH_FRAMES", "5"))
SCREEN_WATCH_START_DELAY_SEC = int(os.environ.get("FEEDLING_SCREEN_WATCH_START_DELAY_SEC", "20"))
# A frame newer than this means sharing is genuinely live right now (iOS captures
# ~1 frame / 30 s). Used instead of the heartbeat's broadcast_state, which is only
# refreshed on the slow heartbeat tick and would be stale for a 2-min loop.
SCREEN_WATCH_FRESH_SEC = int(os.environ.get("FEEDLING_SCREEN_WATCH_FRESH_SEC", "90"))
PROACTIVE_SCHEDULED_FIRE_ENABLED = _env_bool("PROACTIVE_SCHEDULED_FIRE_ENABLED", True)
PROACTIVE_SCHEDULED_FIRE_INTERVAL_SEC = int(os.environ.get("PROACTIVE_SCHEDULED_FIRE_INTERVAL_SEC", "60"))
PROACTIVE_SCHEDULED_FIRE_START_DELAY_SEC = int(os.environ.get("PROACTIVE_SCHEDULED_FIRE_START_DELAY_SEC", "5"))
CAPTURE_TICK_ENABLED = _env_bool("FEEDLING_CAPTURE_TICK_ENABLED", True)
CAPTURE_TICK_INTERVAL_SEC = int(os.environ.get(
    "FEEDLING_CAPTURE_TICK_INTERVAL_SEC",
    str(PROACTIVE_SCHEDULED_FIRE_INTERVAL_SEC),
))
CAPTURE_TICK_START_DELAY_SEC = int(os.environ.get(
    "FEEDLING_CAPTURE_TICK_START_DELAY_SEC",
    str(PROACTIVE_SCHEDULED_FIRE_START_DELAY_SEC),
))
PROACTIVE_MAX_REPLY_MESSAGES = int(os.environ.get("PROACTIVE_MAX_REPLY_MESSAGES", "5"))
PROACTIVE_RECENT_CHAT_LIMIT = int(os.environ.get("PROACTIVE_RECENT_CHAT_LIMIT", "20"))
PROACTIVE_CHAT_CONTEXT_LOOKBACK_LIMIT = int(os.environ.get("PROACTIVE_CHAT_CONTEXT_LOOKBACK_LIMIT", "50"))
PROACTIVE_CHAT_FRESH_WINDOW_SEC = int(os.environ.get("PROACTIVE_CHAT_FRESH_WINDOW_SEC", "21600"))
PROACTIVE_STALE_CHAT_FALLBACK_LIMIT = int(os.environ.get("PROACTIVE_STALE_CHAT_FALLBACK_LIMIT", "2"))
CAPTURE_HISTORY_LIMIT = int(os.environ.get("FEEDLING_CAPTURE_HISTORY_LIMIT", "160"))
CAPTURE_WINDOW_MAX_CHARS = int(os.environ.get("FEEDLING_CAPTURE_WINDOW_MAX_CHARS", "12000"))
CAPTURE_CONTEXT_MAX_CHARS = int(os.environ.get("FEEDLING_CAPTURE_CONTEXT_MAX_CHARS", "4000"))
DREAM_MEMORY_INDEX_LIMIT = int(os.environ.get("FEEDLING_DREAM_MEMORY_INDEX_LIMIT", "0"))
DREAM_FETCH_BATCH_SIZE = int(os.environ.get("FEEDLING_DREAM_FETCH_BATCH_SIZE", "100"))
DREAM_RECENT_CHAT_LIMIT = int(os.environ.get("FEEDLING_DREAM_RECENT_CHAT_LIMIT", "80"))
DREAM_MEMORY_MAX_CARDS = int(os.environ.get("FEEDLING_DREAM_MEMORY_MAX_CARDS", "200"))
DREAM_MAX_CONSOLIDATIONS = int(os.environ.get("FEEDLING_DREAM_MAX_CONSOLIDATIONS", "12"))
CONSUMER_ID = os.environ.get(
    "CONSUMER_ID",
    f"{socket.gethostname()}:{os.getpid()}",
)
AGENT_SESSION_FILE_TEMPLATE = os.environ.get(
    "AGENT_SESSION_FILE",
    f"/tmp/feedling_agent_session_{hashlib.sha1(FEEDLING_API_KEY.encode()).hexdigest()[:10]}_{{user_id}}.txt",
)
AGENT_SESSION_MAX_TURNS = int(os.environ.get("AGENT_SESSION_MAX_TURNS", "40"))
AGENT_SESSION_MAX_BYTES = int(os.environ.get("AGENT_SESSION_MAX_BYTES", "250000"))
AGENT_SESSION_ROTATE_PREFIX = os.environ.get("AGENT_SESSION_ROTATE_PREFIX", "feedling-io")
IMAGE_TEMP_DIR = Path(os.environ.get("IMAGE_TEMP_DIR", "/tmp/feedling_chat_images"))
SCREEN_CONTEXT_MODE = os.environ.get("SCREEN_CONTEXT_MODE", "on_mention").strip().lower()
SCREEN_CONTEXT_MAX_AGE_SEC = int(os.environ.get("SCREEN_CONTEXT_MAX_AGE_SEC", "300"))
SCREEN_CONTEXT_INCLUDE_IMAGE = _env_bool("SCREEN_CONTEXT_INCLUDE_IMAGE", True)
# Foreground chat continuity. codex has no --resume and the hosted claude command
# carries no session, so those drivers otherwise forget everything after the first
# turn. When active we prepend a short recent-chat transcript to each foreground
# turn so continuity does not depend on the agent's own (missing/fragile) session.
#   auto (default) — inject only for codex / claude (drivers with no reliable
#                    cross-turn memory); pi resumes natively and is skipped.
#   on/always      — inject for every driver (escape hatch).
#   off            — never inject; claude falls back to its --resume path.
FOREGROUND_CHAT_CONTEXT_MODE = os.environ.get(
    "FEEDLING_FOREGROUND_CHAT_CONTEXT", "auto"
).strip().lower()
# 50 messages ≈ 25 full rounds; this default sits exactly at the clamp in
# _recent_chat_context_for_foreground — raise both together or the extra is
# silently dropped.
FOREGROUND_CHAT_CONTEXT_LIMIT = int(os.environ.get("FEEDLING_FOREGROUND_CHAT_CONTEXT_LIMIT", "50"))
FOREGROUND_CHAT_CONTEXT_HEADER = os.environ.get(
    "FEEDLING_FOREGROUND_CHAT_CONTEXT_HEADER",
    "[最近对话记录 — 仅供你保持连续；最后那条用户消息才是此刻要回应的]",
)
FALLBACK_REPLY = os.environ.get(
    "FALLBACK_REPLY", "我这会儿有点慢，刚刚没接上。你稍后再发一次，我会继续接。"
)
# Canned reply for /v1/chat/verify_loop liveness pings — see the short-circuit
# in _process_messages. The server GCs both the ping and this reply once the
# verify completes, so it never reaches the user's visible chat; it only has
# to be a non-empty agent-role write that lands fast.
VERIFY_PING_REPLY = os.environ.get("VERIFY_PING_REPLY", "__verify_ack__")
# Verify probe (real-agent liveness): on a verify_ping we now run a real, bounded
# agent call so verify catches a broken reply pipeline (e.g. unparseable agent
# output) instead of always passing via the canned ack. VERIFY_PROBE_MESSAGE is
# the synthetic prompt sent to the agent; VERIFY_PROBE_TIMEOUT_SEC bounds the
# wait before we fall back to the canned ack (keeps a slow-but-healthy agent
# from falsely failing). See the verify_ping branch in _process_messages.
VERIFY_PROBE_MESSAGE = os.environ.get("VERIFY_PROBE_MESSAGE", "（连接自检）请用一句话回复，确认你能收到我的消息。")
VERIFY_PROBE_TIMEOUT_SEC = float(os.environ.get("VERIFY_PROBE_TIMEOUT_SEC", "20"))
SEND_FALLBACK_ON_AGENT_ERROR = _env_bool("SEND_FALLBACK_ON_AGENT_ERROR", True)
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT", "30"))
WHOAMI_STARTUP_RETRIES = int(os.environ.get("WHOAMI_STARTUP_RETRIES", "8"))
WHOAMI_STARTUP_RETRY_DELAY_SEC = float(
    os.environ.get("WHOAMI_STARTUP_RETRY_DELAY_SEC", "5")
)
WHOAMI_REFRESH_RETRIES = int(os.environ.get("WHOAMI_REFRESH_RETRIES", "3"))
WHOAMI_REFRESH_RETRY_DELAY_SEC = float(os.environ.get("WHOAMI_REFRESH_RETRY_DELAY_SEC", "0.5"))
# TTL gate for the pre-reply whoami refresh. Encryption keys are stable (the
# user's own pubkey never changes; the enclave content pubkey is dstack-KMS
# derived and stable across compose rotations), so re-fetching before every
# reply just adds a reentrant backend round-trip under load. 0 = always refresh.
WHOAMI_REFRESH_TTL_SEC = float(os.environ.get("WHOAMI_REFRESH_TTL_SEC", "300"))

# Provider payment (HTTP 402 / out-of-credits) circuit breaker. After a provider
# payment failure, pause PROACTIVE agent calls for this window so a broke key
# stops flooding the logs with per-tick retries. User-initiated chat replies are
# NOT gated. 0 = disabled (always attempt).
PROVIDER_PAYMENT_COOLDOWN_SEC = float(os.environ.get("PROVIDER_PAYMENT_COOLDOWN_SEC", "600"))
_provider_payment_cooldown_until: float = 0.0
_PROVIDER_PAYMENT_MARKERS = ("402", "payment required", "requires more credits")


def _is_provider_payment_error(exc: BaseException) -> bool:
    s = str(exc).lower()
    return any(m in s for m in _PROVIDER_PAYMENT_MARKERS)


def _provider_payment_cooling_down() -> bool:
    return PROVIDER_PAYMENT_COOLDOWN_SEC > 0 and time.monotonic() < _provider_payment_cooldown_until


def _note_provider_payment_failure() -> None:
    global _provider_payment_cooldown_until
    _provider_payment_cooldown_until = time.monotonic() + PROVIDER_PAYMENT_COOLDOWN_SEC


def _clear_provider_payment_cooldown() -> None:
    global _provider_payment_cooldown_until
    _provider_payment_cooldown_until = 0.0

# Prompt routed only when an agent entry cannot receive a native image object.
# The consumer still extracts decrypted image bytes and passes them through
# the richest available channel:
#   - OpenAI-compatible HTTP gets a multimodal `image_url` content block.
#   - simple HTTP gets an `images` array.
#   - CLI gets local image file paths in the message or command template.
IMAGE_PLACEHOLDER = os.environ.get(
    "IMAGE_PLACEHOLDER",
    "[The user sent an image in IO Chat. Inspect the attached/local image "
    "before replying. If your current runtime cannot open the image, say "
    "plainly that this connector has not enabled image vision yet.]",
)

_SCREEN_CONTEXT_TRIGGER_RE = re.compile(
    r"(screen|broadcast|share|sharing|see\s+(my|the)|look\s+at|current\s+screen|"
    r"屏幕|共享|画面|看得到|看见|看到|能看|看一下|这张|这个|这里|当前)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Decrypt sources — at least one must be set for v1 encrypted backends.
#
# FEEDLING_ENCLAVE_URL: direct HTTP to the enclave decrypt proxy (fastest,
#   same value as FEEDLING_ENCLAVE_URL in mcp_server.py, e.g. https://127.0.0.1:5003).
#
FEEDLING_ENCLAVE_URL = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")


def _consumer_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).parent.parent),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


_HEADERS = {
    "X-API-Key": FEEDLING_API_KEY,
    "X-Feedling-Consumer": "feedling-chat-resident",
    "X-Feedling-Consumer-Id": CONSUMER_ID,
    "X-Feedling-Consumer-Version": "resident-v1",
    "X-Feedling-Consumer-Commit": os.environ.get("FEEDLING_CONSUMER_COMMIT", _consumer_commit()),
}


def _post_debug_trace_event(payload: dict) -> None:
    """Actual network call for a debug-trace event. Runs on a background
    thread (see `_emit_debug_trace`) — never raises, short timeout."""
    try:
        httpx.post(
            f"{FEEDLING_API_URL}/v1/debug/trace/event",
            json=payload,
            headers=_HEADERS, timeout=2,
        )
    except Exception:
        pass  # observability must never affect the turn


# Short-TTL cache of whether debug-trace recording is enabled (per-user gate AND
# deploy kill-switch both true). Lets the hot path (`_emit_debug_trace`) skip
# all work — including spawning a thread — on every turn while it's off,
# instead of paying a POST (that the backend would just no-op) each time.
_DBG_TRACE_ENABLED = {"val": None, "exp": 0.0}
_DBG_TRACE_TTL = 60.0


def _debug_trace_probably_enabled() -> tuple[bool, bool]:
    """Pure, non-network read of the cached enabled flag.

    Returns (known, enabled). `known` is True only when the cache is fresh
    (not expired) and has a value; in that case `enabled` reflects it.
    Otherwise returns (False, False) — the enabled value is meaningless when
    stale/unknown and callers must not act on it."""
    if _DBG_TRACE_ENABLED["val"] is not None and time.monotonic() < _DBG_TRACE_ENABLED["exp"]:
        return True, bool(_DBG_TRACE_ENABLED["val"])
    return False, False


def _refresh_debug_trace_enabled() -> None:
    """Refresh the cached debug-trace enabled flag from the backend. Runs on
    the daemon thread spawned by `_emit_debug_trace` — never on the calling
    thread, never raises. Fail-closed: any error (network, bad JSON, non-2xx)
    caches False so we don't keep hammering an unhappy backend every turn."""
    enabled = False
    try:
        resp = httpx.get(
            f"{FEEDLING_API_URL}/v1/debug/trace",
            params={"limit": 1},
            headers=_HEADERS, timeout=2,
        )
        resp.raise_for_status()
        body = resp.json() or {}
        enabled = bool(body.get("enabled") and body.get("deploy_enabled"))
    except Exception:
        enabled = False  # observability must never affect the turn; fail closed
    _DBG_TRACE_ENABLED["val"] = enabled
    _DBG_TRACE_ENABLED["exp"] = time.monotonic() + _DBG_TRACE_TTL


def _emit_debug_trace(subsystem: str, type: str, *, status: str = "ok",
                      summary: str = "", explain: str = "", detail: dict | None = None,
                      content_excerpt: dict | None = None, trace_id: str = "",
                      dur_ms: float | None = None) -> None:
    """Fire-and-forget flow-trace emit. Offloads all network I/O (both the
    cache-refresh GET and the event POST) to a daemon thread and returns
    immediately, so it never blocks or slows a turn — even if the backend is
    slow/unreachable. When the cache is warm and says disabled, this is a
    zero-cost no-op: no thread spawned, no network at all."""
    try:
        known, enabled = _debug_trace_probably_enabled()
        if known and not enabled:
            return  # warm cache says off — do essentially zero work
        payload = {"event": {
            "subsystem": subsystem, "type": type, "status": status,
            "summary": summary, "explain": explain, "detail": detail or {},
            "content_excerpt": content_excerpt or {}, "trace_id": trace_id,
            "turn_id": trace_id, "actor": "vps_resident", "dur_ms": dur_ms,
        }}

        def _dispatch() -> None:
            if not known:
                _refresh_debug_trace_enabled()
            _, still_enabled = _debug_trace_probably_enabled()
            if still_enabled:
                _post_debug_trace_event(payload)

        threading.Thread(target=_dispatch, daemon=True).start()
    except Exception:
        pass  # observability must never affect the turn


# Stage D: when hosted, the supervisor writes a short-lived runtime token to this
# file (and refreshes it). We authenticate with the token instead of the
# long-term API key, re-reading the file so refreshes are picked up. Unset/empty
# (e.g. a self-hosted VPS user) → we keep using X-API-Key, unchanged.
FEEDLING_RUNTIME_TOKEN_FILE = os.environ.get("FEEDLING_RUNTIME_TOKEN_FILE", "").strip()


def _runtime_token_exp(token: str) -> float | None:
    """Read the ``exp`` claim from a runtime token WITHOUT verifying its signature
    (no secret here). Lets us avoid sending a token we can already see is expired.
    Returns the exp epoch, or None if unparseable."""
    try:
        payload_b64 = token.split(".", 1)[0]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        return float(claims.get("exp"))
    except Exception:
        return None


def _refresh_auth_header() -> None:
    """Choose the request auth header from the runtime-token file (Stage D).

    Uses the token only when the file holds one that is NOT already expired
    (decoding its ``exp``); otherwise falls back to the long-term api key. This
    avoids wedging on a stale token if the supervisor stops refreshing the file.
    Mutates ``_HEADERS`` in place so all existing call sites pick it up."""
    if not FEEDLING_RUNTIME_TOKEN_FILE:
        return
    token = ""
    try:
        token = Path(FEEDLING_RUNTIME_TOKEN_FILE).read_text().strip()
    except OSError:
        token = ""
    exp = _runtime_token_exp(token) if token else None
    fresh = exp is not None and exp > time.time() + 5  # small skew margin
    if fresh:
        _HEADERS.pop("X-API-Key", None)
        _HEADERS["X-Feedling-Runtime-Token"] = token
    else:
        _HEADERS.pop("X-Feedling-Runtime-Token", None)
        _HEADERS["X-API-Key"] = FEEDLING_API_KEY


_refresh_auth_header()  # adopt a token immediately if one is already present


# ---------------------------------------------------------------------------
# Self-update — keep a self-hosted resident on the commit the backend deploys.
#
# The backend advertises its deployed commit in the chat-poll response
# (``client_release.expected_consumer_commit``). When ours differs AND the
# difference actually touches a file this consumer loads, we fetch + checkout
# that commit and re-exec in place. Hosted (supervisor-managed CVM) runs are
# excluded — their code is baked into an attested, immutable image.
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parent.parent

# Default-on; a self-hoster can set FEEDLING_AUTO_UPDATE=0 to opt out.
AUTO_UPDATE = os.environ.get("FEEDLING_AUTO_UPDATE", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
    "",
)
# A runtime-token file is only written by the in-CVM supervisor — treat its
# presence as "hosted" and never self-mutate there.
_HOSTED = bool(FEEDLING_RUNTIME_TOKEN_FILE)


def _runtime_repo_files() -> set[str]:
    """Repo-relative ``.py`` files this process actually loaded (auto-derived
    dependency whitelist), plus files distributed alongside us that never show
    up in ``sys.modules`` (io_cli is shelled out; requirements gate pip).

    Used to decide whether a backend release touches anything we run — a pure
    backend change (routes/db/accounts the consumer never imports) must not
    trigger a needless restart."""
    files: set[str] = set()
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        try:
            rel = Path(f).resolve().relative_to(_REPO)
        except ValueError:
            continue  # stdlib / site-packages / outside the repo
        if rel.suffix == ".py":
            files.add(str(rel))
    files.update(
        {
            "tools/io_cli.py",
            "tools/chat_resident_requirements.txt",
            "backend/requirements.txt",
        }
    )
    return files


def _should_self_update(
    local: str,
    target: str,
    dirty: bool,
    enabled: bool,
    hosted: bool,
    relevant_changed: bool,
) -> bool:
    """Pure decision: should we update from ``local`` to ``target`` now?

    Side-effect-free so it is exhaustively unit-tested. The caller owns the git
    work and is responsible for warning when a dirty tree blocks an update."""
    if not enabled or hosted:
        return False
    if not target or target == "dev" or not local:
        return False
    # Short vs full hash of the same commit -> already there, nothing to do.
    if target.startswith(local) or local.startswith(target):
        return False
    if dirty:
        return False  # protect uncommitted local edits (caller warns)
    return relevant_changed


# Don't re-attempt the git fetch/diff dance more than once per window — the
# backend re-advertises the target on every (often timed-out) poll.
_SELF_UPDATE_MIN_INTERVAL_SEC = 300.0
_last_self_update_mono = 0.0

_REQUIREMENTS_FILES = {
    "tools/chat_resident_requirements.txt",
    "backend/requirements.txt",
}

# Repo paths that are part of this consumer's runtime but may be imported
# LAZILY (e.g. proactive.adapters_v2 / runtime_v2 only load once a proactive job
# runs), so they won't appear in sys.modules on a fresh, idle consumer. We still
# want a release touching them to trigger an update — hence a static layer on
# top of the sys.modules-derived set in _runtime_repo_files().
_RELEVANT_PATH_PREFIXES = ("backend/proactive/",)
_RELEVANT_PATH_FILES = {"backend/content_encryption.py"}


def _git(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(_REPO), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _git_tree_dirty() -> bool:
    """True if there are uncommitted changes — or if we can't tell (fail safe:
    an unknown state must not be overwritten)."""
    try:
        r = _git("status", "--porcelain", timeout=10)
    except Exception:
        return True
    if r.returncode != 0:
        return True
    return bool(r.stdout.strip())


def _git_fetch(target: str) -> bool:
    try:
        return _git("fetch", "--quiet", "origin", target, timeout=120).returncode == 0
    except Exception:
        return False


def _git_changed_files(local: str, target: str) -> set[str]:
    try:
        r = _git("diff", "--name-only", local, target, "--", timeout=30)
    except Exception:
        return set()
    if r.returncode != 0:
        return set()
    return {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}


def _git_checkout(target: str) -> bool:
    # Detached checkout pins us exactly to the backend's commit (lockstep). A
    # self-hoster who wants to take over manually can `git checkout main`.
    try:
        r = _git("checkout", "--detach", "--force", target, timeout=60)
    except Exception as e:
        log.error("self-update checkout error: %s", e)
        return False
    if r.returncode != 0:
        log.error("self-update checkout failed: %s", r.stderr.strip())
        return False
    return True


def _pip_install(req_rel: str) -> None:
    # Best-effort: a re-exec into new code that needs new deps would otherwise
    # crash-loop. Failure here only warns; systemd will still respawn.
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(_REPO / req_rel)],
            timeout=600,
            check=False,
        )
    except Exception as e:
        log.warning("self-update pip install %s failed: %s", req_rel, e)


def _relevant_changed(changed: set[str]) -> bool:
    """Does this release touch anything this consumer actually runs?

    Combines the auto-derived (sys.modules) dependency set with a static layer
    for lazily-imported runtime code that may not be loaded yet."""
    if changed & _runtime_repo_files():
        return True
    for path in changed:
        if path in _RELEVANT_PATH_FILES:
            return True
        if any(path.startswith(p) for p in _RELEVANT_PATH_PREFIXES):
            return True
    return False


def _apply_self_update(local: str, target: str, changed: set[str]) -> None:
    """Checkout the target commit, install any changed deps, then re-exec.

    Checkout happens FIRST so _pip_install reads the target commit's
    requirements file — installing the deps the new code needs, not the old
    ones (a release adding a dependency must not re-exec into code that can't
    import it)."""
    if not _git_checkout(target):
        return
    for req in sorted(changed & _REQUIREMENTS_FILES):
        log.info("self-update: %s changed — pip installing after checkout", req)
        _pip_install(req)
    log.info("self-update %s -> %s applied; re-exec into new code", local, target)
    try:
        os.execv(sys.executable, [sys.executable, *sys.argv])
    except Exception as e:
        # execv replaces the process and normally never returns; if it failed,
        # exit cleanly so the supervisor/systemd respawns the new code.
        log.error("self-update re-exec failed (%s); exiting for restart", e)
        sys.exit(0)


def _run_self_update(target: str) -> None:
    """Decide and (if warranted) apply a self-update to ``target``.

    Cheap pre-checks short-circuit before any git network call; the expensive
    fetch/diff only runs when an update is genuinely plausible. Re-exec happens
    inside ``_apply_self_update`` and does not return."""
    global _last_self_update_mono
    if not AUTO_UPDATE or _HOSTED or not target or target == "dev":
        return
    now = time.monotonic()
    if now - _last_self_update_mono < _SELF_UPDATE_MIN_INTERVAL_SEC:
        return
    local = _consumer_commit()
    if not local or target.startswith(local) or local.startswith(target):
        return  # unknown local, or already on the target commit
    _last_self_update_mono = now  # throttle the fetch/diff attempt below

    if not _git_fetch(target):
        log.warning("self-update: could not fetch %s; will retry later", target)
        return
    changed = _git_changed_files(local, target)
    relevant = _relevant_changed(changed)
    if not relevant:
        return  # backend release doesn't touch anything this consumer loads
    dirty = _git_tree_dirty()
    if not _should_self_update(local, target, dirty, AUTO_UPDATE, _HOSTED, relevant):
        if dirty:
            log.warning(
                "self-update %s -> %s available but working tree has uncommitted "
                "changes; skipping (run `git stash` / commit to allow it)",
                local,
                target,
            )
        return
    _apply_self_update(local, target, changed)


def _maybe_self_update(poll_result: Any) -> None:
    """Extract the backend-advertised target commit from a chat-poll response
    and run the self-update check. Called at idle (timed-out) polls only."""
    if not isinstance(poll_result, dict):
        return
    release = poll_result.get("client_release")
    target = ""
    if isinstance(release, dict):
        target = str(release.get("expected_consumer_commit") or "").strip()
    if target:
        _run_self_update(target)


# Separate HTTP client for the enclave (self-signed TLS, verify=False).
_ENCLAVE_CLIENT: httpx.Client | None = (
    httpx.Client(timeout=20, verify=False) if FEEDLING_ENCLAVE_URL else None
)

_decrypt_sources = (
    f"enclave={FEEDLING_ENCLAVE_URL}" if FEEDLING_ENCLAVE_URL else ""
).strip() or "NONE — replies will not work for v1 encrypted messages"

log.info(
    "Starting resident consumer — mode=%s api_url=%s decrypt_sources=%s key=%s",
    AGENT_MODE, FEEDLING_API_URL, _decrypt_sources, _mask(FEEDLING_API_KEY),
)

# ---------------------------------------------------------------------------
# Checkpoint (persist last processed message timestamp)
# ---------------------------------------------------------------------------

def _checkpoint_user_id() -> str:
    try:
        return str(_whoami_cache.get("user_id") or "").strip()
    except NameError:
        return ""


def _empty_checkpoint_data() -> dict[str, Any]:
    data: dict[str, Any] = {
        "last_ts": 0.0,
        "last_job_ts": 0.0,
        "api_key_fingerprint": CHECKPOINT_API_KEY_FINGERPRINT,
    }
    user_id = _checkpoint_user_id()
    if user_id:
        data["user_id"] = user_id
    return data


def _load_checkpoint_data() -> dict[str, Any]:
    try:
        data = json.loads(CHECKPOINT_FILE.read_text())
        if not isinstance(data, dict):
            return {}
        current_user_id = _checkpoint_user_id()
        stored_user_id = str(data.get("user_id") or "").strip()
        stored_fingerprint = str(data.get("api_key_fingerprint") or "").strip()
        if stored_fingerprint and stored_fingerprint != CHECKPOINT_API_KEY_FINGERPRINT:
            log.warning(
                "checkpoint owner api key changed; resetting cursor file=%s old_key=%s new_key=%s",
                CHECKPOINT_FILE,
                stored_fingerprint,
                CHECKPOINT_API_KEY_FINGERPRINT,
            )
            return _empty_checkpoint_data()
        if current_user_id and stored_user_id and stored_user_id != current_user_id:
            log.warning(
                "checkpoint owner user changed; resetting cursor file=%s old_user=%s new_user=%s",
                CHECKPOINT_FILE,
                stored_user_id,
                current_user_id,
            )
            return _empty_checkpoint_data()
        result: dict[str, Any] = {
            "last_ts": float(data.get("last_ts", 0) or 0),
            "last_job_ts": float(data.get("last_job_ts", 0) or 0),
            "api_key_fingerprint": stored_fingerprint or CHECKPOINT_API_KEY_FINGERPRINT,
        }
        if stored_user_id or current_user_id:
            result["user_id"] = stored_user_id or current_user_id
        return result
    except Exception:
        return {}


def _write_checkpoint_data(data: dict[str, Any]) -> None:
    try:
        CHECKPOINT_FILE.write_text(json.dumps(data))
    except Exception as e:
        log.warning("checkpoint write failed: %s", e)


def _load_checkpoint() -> float:
    return float(_load_checkpoint_data().get("last_ts", 0.0) or 0.0)


def _save_checkpoint(ts: float) -> None:
    data = _load_checkpoint_data()
    data["last_ts"] = ts
    data.setdefault("last_job_ts", 0.0)
    data["api_key_fingerprint"] = CHECKPOINT_API_KEY_FINGERPRINT
    user_id = _checkpoint_user_id()
    if user_id:
        data["user_id"] = user_id
    _write_checkpoint_data(data)


def _load_proactive_checkpoint() -> float:
    return float(_load_checkpoint_data().get("last_job_ts", 0.0) or 0.0)


def _save_proactive_checkpoint(ts: float) -> None:
    data = _load_checkpoint_data()
    data.setdefault("last_ts", 0.0)
    data["last_job_ts"] = ts
    data["api_key_fingerprint"] = CHECKPOINT_API_KEY_FINGERPRINT
    user_id = _checkpoint_user_id()
    if user_id:
        data["user_id"] = user_id
    _write_checkpoint_data(data)


# ---------------------------------------------------------------------------
# Message dedup
# ---------------------------------------------------------------------------

def _msg_key(msg: dict) -> str:
    """Stable identity key: prefer explicit id field, fall back to ts:role."""
    mid = str(msg.get("id") or msg.get("message_id") or "").strip()
    if mid:
        return mid
    ts = msg.get("ts", msg.get("timestamp", 0)) or 0
    return f"{ts}:{msg.get('role', '')}"


_DECRYPT_SINCE_EPSILON = 0.001


def _poll_decrypt_since(last_ts: float, poll_messages: list[dict]) -> float:
    """Decrypt-history window for this poll batch.

    Normally the cursor. But the server's lost-turn redelivery backstop can
    hand back a message whose ts is BEHIND the cursor (its turn was lost to a
    respawn); fetching plaintext with since=last_ts would never include it,
    _filter_messages_to_poll_ids would come back empty, and the wedge-skip
    path would burn the claim. Pull the window back to just before the oldest
    message in the batch so every claimed message is fetchable.
    """
    since = last_ts
    for m in poll_messages:
        if not isinstance(m, dict):
            continue
        try:
            pts = float(m.get("ts", m.get("timestamp", 0)) or 0)
        except (TypeError, ValueError):
            continue
        if pts and pts - _DECRYPT_SINCE_EPSILON < since:
            since = pts - _DECRYPT_SINCE_EPSILON
    return since


def _poll_decrypt_limit(decrypt_since: float, last_ts: float, poll_messages: list[dict]) -> int:
    """Decrypt-history fetch size for this poll batch.

    A pulled-back window (redelivered messages) spans more history than the
    usual tail, and EVERY claimed message must fit in one fetch: a truncated
    fetch drops claimed messages, and a redelivery claim can't be retried until
    its TTL expires. Sized to the batch (interleaved openclaw replies roughly
    double the row count) with a floor of 50."""
    if decrypt_since >= last_ts:
        return 20
    return max(50, 2 * len(poll_messages) + 20)


def _filter_messages_to_poll_ids(messages: list[dict], poll_messages: list[dict]) -> list[dict]:
    """Keep only decrypted rows that this poll cycle actually claimed.

    /v1/chat/poll is the server-side responder lease. Decrypted history may
    contain other users' recent messages for the same account, including rows
    claimed by another responder, so the resident must not treat history as the
    source of work ownership.
    """
    poll_ids = {
        str(m.get("id") or m.get("message_id") or "").strip()
        for m in poll_messages
        if isinstance(m, dict)
    }
    poll_ids.discard("")
    if not poll_ids:
        return messages
    return [
        m for m in messages
        if str(m.get("id") or m.get("message_id") or "").strip() in poll_ids
    ]


# The chat cursor wedges when /v1/chat/poll keeps claiming message ids the enclave
# decrypt-history never returns (an undecryptable row, or one sitting exactly at the
# exclusive `since` boundary). We retry a bounded number of cycles — transient
# decrypt hiccups self-heal — then skip PAST the claimed batch so one permanently
# unreturnable message can't block every newer message forever.
CHAT_POLL_WEDGE_SKIP_AFTER = int(os.environ.get("CHAT_POLL_WEDGE_SKIP_AFTER", "5"))
_WEDGE_SKIP_EPSILON = 1e-3


def _advance_past_unfetchable(last_ts: float, poll_messages: list[dict]) -> float:
    """Next checkpoint that skips the poll-claimed rows the decrypt source won't
    return. Jumps to the newest claimed ts; if that is not strictly past the cursor
    (the stuck row sits at the boundary), nudge just beyond it so the next poll
    excludes it."""
    max_ts = max(
        (float(m.get("ts", m.get("timestamp", 0)) or 0) for m in poll_messages),
        default=last_ts,
    )
    return max_ts if max_ts > last_ts else last_ts + _WEDGE_SKIP_EPSILON


def _mark_seen(key: str) -> bool:
    """Mark key as seen. Returns True (new) or False (already processed)."""
    if key in _seen_ids:
        return False
    _seen_ids.add(key)
    _seen_ids_order.append(key)
    if len(_seen_ids_order) > _SEEN_MAX:
        _seen_ids.discard(_seen_ids_order.pop(0))
    return True


# ---------------------------------------------------------------------------
# Decrypt sources — plaintext content for v1 encrypted messages
# ---------------------------------------------------------------------------

def _filter_since(msgs: list, since: float) -> list:
    return [m for m in msgs if float(m.get("ts", m.get("timestamp", 0)) or 0) > since]


def _fetch_from_enclave(since: float, limit: int) -> list[dict] | None:
    """Direct HTTP to the enclave decrypt proxy.

    Returns list (possibly empty) on success, None on error or not configured.
    """
    if not FEEDLING_ENCLAVE_URL or _ENCLAVE_CLIENT is None:
        return None
    try:
        resp = _ENCLAVE_CLIENT.get(
            f"{FEEDLING_ENCLAVE_URL}/v1/chat/history",
            params={"limit": limit, "since": since},
            headers=_HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()
        msgs = data.get("messages") or data.get("history") or []
        return _filter_since(msgs, since)
    except httpx.HTTPStatusError as e:
        # The enclave maps transient dependency failures to self-describing
        # codes (502 backend_unreachable / 503 key_derivation_unavailable).
        # httpx's str(e) carries the status + URL but NOT the body, so log the
        # body explicitly — it's the only field that tells the operator WHICH
        # dependency broke without shelling into the CVM.
        body = (e.response.text or "").strip().replace("\n", " ")[:300]
        log.warning(
            "enclave history fetch failed: HTTP %d — %s",
            e.response.status_code, body or "(empty body)",
        )
        return None
    except Exception as e:
        log.warning("enclave history fetch failed: %s", e)
        return None


def _verify_decrypt_sources() -> bool:
    """Probe all configured decrypt sources at startup.

    Returns True if at least one configured source is reachable.
    Each unreachable source is logged at ERROR level so the operator
    can distinguish "configured but broken" from "not configured at all".
    """
    any_ok = False

    if FEEDLING_ENCLAVE_URL:
        try:
            client = _ENCLAVE_CLIENT or httpx
            resp = client.get(
                f"{FEEDLING_ENCLAVE_URL}/v1/chat/history",
                params={"limit": 1},
                headers=_HEADERS,
                timeout=10,
            )
            resp.raise_for_status()
            log.info("decrypt source OK: enclave at %s", FEEDLING_ENCLAVE_URL)
            any_ok = True
        except Exception as e:
            log.error(
                "decrypt source UNREACHABLE: enclave at %s — %s",
                FEEDLING_ENCLAVE_URL, e,
            )

    return any_ok


def get_decrypted_history(since: float, limit: int = 20) -> list[dict] | None:
    """Try all configured decrypt sources in priority order.

    Returns:
      list  — source was reachable; contains messages newer than `since`
              (may be empty if no new messages).
      None  — no source configured, or all configured sources failed.
    """
    if FEEDLING_ENCLAVE_URL:
        result = _fetch_from_enclave(since, limit)
        if result is not None:
            return result
        log.warning("enclave source failed")

    return None  # no configured source succeeded


# ---------------------------------------------------------------------------
# Image message handling
# ---------------------------------------------------------------------------

def _decode_image_b64(value: Any) -> bytes | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[1]
    if raw.startswith("<vision_block:"):
        return None
    try:
        return base64.b64decode(raw, validate=True)
    except Exception:
        try:
            return base64.b64decode(raw)
        except Exception as e:
            log.warning("image_b64 decode failed: %s", e)
            return None


def _image_payloads_from_msg(msg: dict) -> list[dict[str, str]]:
    image_bytes = _decode_image_b64(msg.get("image_b64"))
    if not image_bytes:
        return []
    mime = msg.get("image_mime") or "image/jpeg"
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return [
        {
            "mime_type": str(mime),
            "data": b64,
            "data_url": f"data:{mime};base64,{b64}",
        }
    ]


def _image_file_paths_for_msg(msg: dict) -> list[str]:
    payloads = _image_payloads_from_msg(msg)
    if not payloads:
        return []
    key = re.sub(r"[^A-Za-z0-9_.-]+", "_", _msg_key(msg))[:96] or "image"
    IMAGE_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for idx, payload in enumerate(payloads):
        ext = ".png" if payload.get("mime_type") == "image/png" else ".jpg"
        path = IMAGE_TEMP_DIR / f"{key}_{idx}{ext}"
        try:
            path.write_bytes(base64.b64decode(payload["data"]))
            paths.append(str(path))
        except Exception as e:
            log.warning("failed to write image temp file %s: %s", path, e)
    return paths


def _image_file_paths_from_payloads(prefix: str, payloads: list[dict[str, str]]) -> list[str]:
    if not payloads:
        return []
    key = re.sub(r"[^A-Za-z0-9_.-]+", "_", prefix)[:96] or "image"
    IMAGE_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for idx, payload in enumerate(payloads):
        ext = ".png" if payload.get("mime_type") == "image/png" else ".jpg"
        path = IMAGE_TEMP_DIR / f"{key}_{idx}{ext}"
        try:
            path.write_bytes(base64.b64decode(payload["data"]))
            paths.append(str(path))
        except Exception as e:
            log.warning("failed to write image temp file %s: %s", path, e)
    return paths


def _message_for_agent(content: str, image_paths: list[str] | None = None) -> str:
    image_paths = image_paths or []
    if not image_paths:
        return content
    joined = ", ".join(image_paths)
    # This text is the ONLY channel by which a claude/other-CLI agent (no native
    # --image injection) learns a pixel image is attached. It must be unambiguous, or
    # live transcripts show two failure modes: the model reaches for io_cli
    # photo-recent (wrong tool, wrong path) instead of Read, OR it invents a
    # "click allow to authorize" approval flow that does not exist and then
    # fabricates the image contents. So: name the Read tool + exact path, assert
    # permission is already granted (there is no approval UI), and forbid asking the
    # user to authorize / re-send.
    return (
        f"{content}\n\n"
        f"Decrypted image file(s) for THIS message, already saved on local disk: {joined}\n"
        "Use the Read tool on that exact absolute path to view the image, then reply "
        "about what you actually see. You ALREADY have permission to read these "
        "files — there is no approval step and no 'allow' button for the user to "
        "click, so never ask the user to authorize, grant access, enable a "
        "permission, or re-send the image. Do NOT use the io_cli photo-recent / "
        "photo-read tools for this image (those fetch OLDER photos); this file is the "
        "current attachment. Only say you cannot see it if the Read tool itself "
        "returns an error — never claim you can see an image you have not Read."
    )


# ---------------------------------------------------------------------------
# Screen-sharing context
# ---------------------------------------------------------------------------

def _should_attach_screen_context(content: str) -> bool:
    mode = SCREEN_CONTEXT_MODE
    if mode in {"0", "false", "off", "none", "disabled"}:
        return False
    if mode in {"1", "true", "always", "on"}:
        return True
    return bool(_SCREEN_CONTEXT_TRIGGER_RE.search(content or ""))


def _fetch_screen_json(path: str) -> dict | None:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            resp = httpx.get(f"{FEEDLING_API_URL}{path}", headers=_HEADERS, timeout=20)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            body = resp.json()
            return body if isinstance(body, dict) else None
        except Exception as e:
            last_error = e
            if attempt < 2:
                time.sleep(0.25 * (attempt + 1))
                continue
    log.warning("screen context fetch failed path=%s error=%s", path, last_error)
    return None


def _screen_context_for_message(content: str) -> tuple[str, list[dict[str, str]], list[str]]:
    """Attach recent screen-sharing context for screen/deictic questions.

    The resident already has the Feedling API key, so it should decrypt the
    latest frame itself instead of making the agent run curl/MCP commands from a
    sandbox that may require user approval.
    """
    if not _should_attach_screen_context(content):
        return "", [], []

    latest = _fetch_screen_json("/v1/screen/frames/latest")
    if not latest:
        return "", [], []

    frame_id = str(latest.get("id") or "").strip()
    ts = float(latest.get("ts") or 0.0)
    if not frame_id:
        return "", [], []
    if ts and time.time() - ts > SCREEN_CONTEXT_MAX_AGE_SEC:
        log.info(
            "screen context skipped — latest frame is stale age=%.1fs id=%s",
            time.time() - ts,
            frame_id,
        )
        return "", [], []

    include_image = "true" if SCREEN_CONTEXT_INCLUDE_IMAGE else "false"
    decrypted = _fetch_screen_json(
        f"/v1/screen/frames/{frame_id}/decrypt?include_image={include_image}"
    )
    if not decrypted:
        return "", [], []

    app = decrypted.get("app") or latest.get("app") or "unknown"
    ocr_text = (decrypted.get("ocr_text") or "").strip()
    captured_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else "unknown"

    payloads: list[dict[str, str]] = []
    image_b64 = decrypted.get("image_b64")
    if isinstance(image_b64, str) and image_b64.strip():
        raw_b64 = image_b64.split(",", 1)[1] if image_b64.startswith("data:") else image_b64
        mime = decrypted.get("image_mime") or "image/jpeg"
        payloads.append(
            {
                "mime_type": str(mime),
                "data": raw_b64,
                "data_url": f"data:{mime};base64,{raw_b64}",
            }
        )

    paths = _image_file_paths_from_payloads(f"screen_{frame_id}", payloads)
    parts = [
        "[Live Feedling screen-sharing context]",
        f"captured_at_utc: {captured_at}",
        f"app: {app}",
    ]
    if ocr_text:
        parts.append(f"ocr_text:\n{ocr_text[:2000]}")
    elif payloads:
        parts.append("ocr_text: empty; inspect the attached screenshot image if your runtime supports vision.")
    else:
        parts.append("ocr_text: empty and no screenshot image was available.")
    if paths:
        parts.append("screenshot_file: " + ", ".join(paths))

    return "\n".join(parts), payloads, paths


def _worldbook_context_for_foreground(content: str) -> str:
    text = str(content or "").strip()
    if not text:
        return ""
    try:
        resp = httpx.post(
            f"{FEEDLING_API_URL}/v1/worldbook/match",
            headers=_HEADERS,
            json={"message": text},
            timeout=20,
        )
        if resp.status_code == 404:
            return ""
        resp.raise_for_status()
        body = resp.json()
        block = str((body or {}).get("block") or "").strip()
        if block:
            names = (body or {}).get("matched_names") or []
            log.info("worldbook context injected names=%s", names)
        return block
    except Exception as exc:
        log.warning("worldbook context fetch failed: %s", exc)
        return ""


def _screen_context_for_frame_ids(frame_ids: list[str]) -> tuple[str, list[dict[str, str]], list[str]]:
    """Attach the concrete frames named by a proactive wake job."""
    frame_ids = [str(fid).strip() for fid in (frame_ids or []) if str(fid).strip()]
    if not frame_ids:
        return "", [], []

    include_image = "true" if SCREEN_CONTEXT_INCLUDE_IMAGE else "false"
    context_parts = ["[Feedling proactive screen context]"]
    payloads: list[dict[str, str]] = []
    paths: list[str] = []

    for frame_id in frame_ids[-4:]:
        decrypted = _fetch_screen_json(
            f"/v1/screen/frames/{frame_id}/decrypt?include_image={include_image}"
        )
        if not decrypted:
            continue

        app = decrypted.get("app") or "unknown"
        ts = float(decrypted.get("ts") or 0.0)
        captured_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else "unknown"
        ocr_text = (decrypted.get("ocr_text") or "").strip()
        context_parts.append(f"frame_id: {frame_id}")
        context_parts.append(f"captured_at_utc: {captured_at}")
        context_parts.append(f"app: {app}")
        if ocr_text:
            context_parts.append(f"ocr_text:\n{ocr_text[:2000]}")

        image_b64 = decrypted.get("image_b64")
        if isinstance(image_b64, str) and image_b64.strip():
            raw_b64 = image_b64.split(",", 1)[1] if image_b64.startswith("data:") else image_b64
            mime = decrypted.get("image_mime") or "image/jpeg"
            payloads.append(
                {
                    "mime_type": str(mime),
                    "data": raw_b64,
                    "data_url": f"data:{mime};base64,{raw_b64}",
                }
            )

    paths = _image_file_paths_from_payloads(
        "proactive_screen_" + hashlib.sha1(",".join(frame_ids).encode()).hexdigest()[:12],
        payloads,
    )
    if paths:
        context_parts.append("screenshot_file: " + ", ".join(paths))
    if len(context_parts) == 1:
        return "", [], []
    return "\n".join(context_parts), payloads, paths


# ---------------------------------------------------------------------------
# Agent backends
# ---------------------------------------------------------------------------

# Decoration / system lines that are never part of the actual reply.
_NOISE_LINE_RE = re.compile(
    r"^\s*("
    r"session_id\s*:.*"      # hermes session footer
    r"|[↻⟳]?\s*(resumed|created|started)\s+session\b.*"  # hermes session banner
    r"|[A-Za-z0-9_\-]{8,}\s*\(\d+\s+user\s+messages?,\s*\d+\s+total\s+messages?\)"
    r"|---+|={3,}|[-–—_]{3,}" # separator lines
    r"|\[.*\]\s*$"           # [bracket] meta lines
    r"|💭.*"                 # hermes thinking-emoji prefix
    r"|[└┌│╰╭─].*"           # box-drawing UI chrome
    r"|\*\*[^*]+\*\*\s*$"   # **standalone bold header**
    r"|</?think>"            # <think> XML tags
    r"|Reasoning:\s*$"       # bare "Reasoning:" label
    r"|[✵✦✧★☆※].*"          # decorative symbol lines
    r")",
    re.IGNORECASE,
)

# Internal/system identity tokens that must never leak to end-user chat.
_IDENTITY_LEAK_RE = re.compile(r"\b(hermes|reasoning|chain\s*of\s*thought)\b", re.IGNORECASE)

# Typical leaked planning / chain-of-thought lead-ins from agent UIs.
_REASONING_LINE_RE = re.compile(
    r"^\s*\.?\s*(i\s+need\s+to|i\'?m\s+thinking|the\s+user\s+wrote|the\s+user\s+wants|"
    r"this\s+(means|doesn\'?t)|i\s+think|i\s+should|i\'ll|let\s+me\s+|my\s+plan\s+is|"
    r"i\s+could\s+use|it\s+seems|i\s+really\s+should|let\'?s\s+(see|make)|"
    r"perhaps\b|maybe\s+through\b)",
    re.IGNORECASE,
)

_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_TAGGED_THINKING_RE = re.compile(
    r"<\s*(?P<tag>think|thinking|reasoning|thought)\s*>\s*"
    r"(?P<body>.*?)"
    r"\s*<\s*/\s*(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)


def _split_tagged_thinking(text: str) -> tuple[str, str]:
    """Split leaked reasoning tags from visible reply text.

    Structured reasoning fields remain the preferred path. This only handles
    plain terminal text where an upstream wrapper serialized reasoning as
    `<think>...</think>`, `<reasoning>...</reasoning>`, or `<thought>...</thought>`.
    """
    raw = str(text or "")
    blocks: list[str] = []

    def _collect(match: re.Match) -> str:
        body = (match.group("body") or "").strip()
        if body:
            blocks.append(body)
        return "\n"

    visible = _TAGGED_THINKING_RE.sub(_collect, raw)
    visible = re.sub(r"\n{3,}", "\n\n", visible).strip()
    thinking = "\n\n".join(blocks).strip()
    return visible, thinking


def _strip_leading_non_cjk_preamble(lines: list[str]) -> list[str]:
    """Drop a leading non-CJK transcript block before a CJK final answer.

    This avoids phrase-specific patches for leaked CLI planning. If the final
    answer is clearly Chinese, any initial English/UI-only block before the
    first Chinese line is treated as transport transcript, not user-facing text.
    Pure English replies and bilingual content after the first Chinese line are
    preserved.
    """
    if not lines or not any(_CJK_RE.search(ln) for ln in lines):
        return lines

    first_cjk = next((i for i, ln in enumerate(lines) if _CJK_RE.search(ln)), None)
    if first_cjk is None or first_cjk == 0:
        return lines
    return lines[first_cjk:]


def _collapse_repeated_line_blocks(lines: list[str]) -> list[str]:
    """Collapse adjacent repeated answer blocks while preserving one copy."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        max_block = (len(lines) - i) // 2
        collapsed = False
        for size in range(max_block, 0, -1):
            block = lines[i : i + size]
            nxt = lines[i + size : i + 2 * size]
            if block == nxt:
                out.extend(block)
                i += size * 2
                collapsed = True
                break
        if not collapsed:
            out.append(lines[i])
            i += 1
    return out


def _strip_reasoning_sections(raw: str) -> str:
    """Remove explicit reasoning/code sections while preserving final answer.

    Hermes/OpenClaw UIs often print a visible block like:

      💭 Reasoning:
      ```copy
      **Doing work**
      I need to ...
      ```

    The older consumer avoided this by keeping only the last CJK paragraph,
    which also destroyed normal multi-paragraph answers. This keeps the full
    answer and removes only the declared reasoning block.
    """
    lines = raw.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.search(r"(^|\b|💭)\s*Reasoning\s*:", line, re.IGNORECASE):
            i += 1
            # Skip optional language/copy marker before a fenced block.
            while i < len(lines) and lines[i].strip().lower() in {"copy", ""}:
                i += 1
            if i < len(lines) and lines[i].strip().startswith("```"):
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("```"):
                    i += 1
                if i < len(lines):
                    i += 1
                continue
            # Unfenced reasoning: skip until a blank line, then resume.
            while i < len(lines) and lines[i].strip():
                i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


_JSON_REPLY_FIELDS = (
    "reply",
    "response",
    "result",
    "content",
    "text",
    "message",
    "final",
    "final_answer",
    "answer",
    "output",
)

_JSON_THINKING_FIELDS = (
    "provider_reasoning",
    "reasoning",
    "reasoning_details",
    "reasoning_content",
    "reasoning_text",
    "runtime_trace",
    "thinking_summary",
    "reasoning_summary",
    "thought_summary",
    "visible_reasoning",
    "thinkingSummary",
    "reasoningSummary",
)

_JSON_RUNTIME_DEBUG_FIELDS = {
    "cache_creation",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "contextWindow",
    "costUSD",
    "duration_ms",
    "ephemeral_1h_input_tokens",
    "ephemeral_5m_input_tokens",
    "fast_mode_state",
    "inference_geo",
    "iterations",
    "latency_ms",
    "maxOutputTokens",
    "modelUsage",
    "permission_denials",
    "raw_id",
    "service_tier",
    "session_id",
    "sessionId",
    "speed",
    "terminal_reason",
    "usage",
    "uuid",
    "webSearchRequests",
}

_JSON_NON_FINAL_EVENTS = {
    "agent_message_delta",
    "agent_reasoning",
    "agent_reasoning_delta",
    "agent_reasoning_section_break",
    "debug",
    "delta",
    "log",
    "progress",
    "reasoning",
    "reasoning_delta",
    "status",
    "stderr",
    "stdout",
    "system",
    "thinking",
    "thought",
    "tool",
    "tool_call",
    "tool_result",
    "trace",
}


def _openclaw_payload_texts(obj: Any) -> list[str]:
    """OpenClaw `agent --json` nests its reply under result.payloads[].text.

    The generic reply-field walker stops at `result` (it does not treat
    `payloads` as a reply field), so without this the consumer reports
    "no usable reply" for a perfectly good OpenClaw answer. Returns each
    payload's text in order (multi-bubble preserved); [] when not this shape.
    """
    if not isinstance(obj, dict):
        return []
    result = obj.get("result")
    if not isinstance(result, dict):
        return []
    payloads = result.get("payloads")
    if not isinstance(payloads, list):
        return []
    texts: list[str] = []
    for item in payloads:
        if isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
    return texts


def _reply_from_json_obj(obj: Any) -> str:
    """Extract the final answer from a structured agent response object."""
    if isinstance(obj, str):
        return obj.strip()

    if isinstance(obj, list):
        for item in reversed(obj):
            text = _reply_from_json_obj(item)
            if text:
                return text
        return ""

    if not isinstance(obj, dict):
        return ""

    openclaw_texts = _openclaw_payload_texts(obj)
    if openclaw_texts:
        return openclaw_texts[0]

    marker = str(
        obj.get("event")
        or obj.get("type")
        or obj.get("kind")
        or obj.get("phase")
        or ""
    ).strip().lower()
    if marker in _JSON_NON_FINAL_EVENTS:
        return ""

    for field in _JSON_REPLY_FIELDS:
        value = obj.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (dict, list)):
            text = _reply_from_json_obj(value)
            if text:
                return text

    choices = obj.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            text = _reply_from_json_obj(choice)
            if text:
                return text

    messages = obj.get("messages")
    if isinstance(messages, list):
        for item in reversed(messages):
            if not isinstance(item, dict):
                text = _reply_from_json_obj(item)
                if text:
                    return text
                continue
            role = str(item.get("role") or "").lower()
            if role and role not in {"assistant", "agent", "openclaw", "model"}:
                continue
            text = _reply_from_json_obj(item)
            if text:
                return text

    return ""


def _safe_json_loads(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _looks_like_json_text(text: str) -> bool:
    stripped = (text or "").strip()
    return bool(stripped) and stripped[0] in "[{"


def _visible_reply_fragment_from_text(text: str) -> Any:
    """Recover the display protocol when the model omits the outer braces.

    Some providers follow the requested
    {"thinking_summary":"...","messages":["..."]} shape only halfway and emit
    a JSON object fragment like `"thinking_summary": "...", "messages": [...]`.
    If we do not recover it here, the protocol text leaks as the visible chat
    bubble. Keep this narrow: only the visible thinking protocol keys qualify.
    """
    stripped = (text or "").strip()
    if not stripped or stripped[0] in "[{":
        return None
    if '"thinking_summary"' not in stripped or '"messages"' not in stripped:
        return None
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, re.IGNORECASE | re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    starts = [idx for key in ('"thinking_summary"', '"messages"') if (idx := stripped.find(key)) >= 0]
    if not starts:
        return None
    fragment = stripped[min(starts):].strip().rstrip(",")
    candidates = ["{" + fragment + "}"]
    if fragment.endswith("}"):
        candidates.append("{" + fragment)
    parsed = None
    for candidate in candidates:
        parsed = _safe_json_loads(candidate)
        if isinstance(parsed, dict) and isinstance(parsed.get("messages"), list):
            break
    if isinstance(parsed, dict) and isinstance(parsed.get("messages"), list):
        return parsed
    return None


def _sanitize_thinking_summary(text: str) -> str:
    """Keep only a short, display-safe reasoning summary.

    This is intentionally stricter than reply sanitization. We never expose
    raw chain-of-thought, system prompts, token/account metadata, or tool
    transcript text in the chat UI.
    """
    if not isinstance(text, str):
        return ""
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return ""
    blocked = re.compile(
        r"(system prompt|developer message|chain[-\s]*of[-\s]*thought|"
        r"modelUsage|terminal_reason|permission_denials|cache_read|"
        r"cache_creation|session_id|uuid|costUSD|input_tokens|output_tokens)",
        re.IGNORECASE,
    )
    kept: list[str] = []
    for raw_ln in text.splitlines():
        ln = raw_ln.strip()
        if not ln or blocked.search(ln):
            continue
        if _NOISE_LINE_RE.match(ln) or _REASONING_LINE_RE.match(ln):
            continue
        ln = re.sub(r"^[`#>*\-\s]+", "", ln).strip()
        if ln:
            kept.append(ln)
        if len(kept) >= 4:
            break
    out = "\n".join(kept).strip()
    return out[:700]


_THINKING_KINDS = {
    "provider_reasoning",
    "provider_reasoning_summary",
    "runtime_trace",
    "agent_summary",
    "context_summary",
}


def _sanitize_thinking_kind(value: Any) -> str:
    kind = str(value or "").strip().lower()
    return kind if kind in _THINKING_KINDS else ""


def _sanitize_thinking_meta(value: Any, *, max_len: int = 96) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return re.sub(r"[\r\n\t]+", " ", text)[:max_len].strip()


def _boolish(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return None


def _default_thinking_kind_for_key(key: str) -> str:
    normalized = key.strip().lower()
    if normalized in {"provider_reasoning", "reasoning", "reasoning_details", "reasoning_content", "reasoning_text"}:
        return "provider_reasoning"
    if normalized == "runtime_trace":
        return "runtime_trace"
    if "reasoning" in normalized or "thought" in normalized:
        return "provider_reasoning_summary"
    return "agent_summary"


def _thinking_summary_from_value(value: Any) -> str:
    if isinstance(value, str):
        return _sanitize_thinking_summary(value)
    if isinstance(value, dict):
        for key in ("summary", "content", "text", "reasoning"):
            summary = value.get(key)
            if isinstance(summary, str):
                sanitized = _sanitize_thinking_summary(summary)
                if sanitized:
                    return sanitized
        return ""
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            sanitized = _thinking_summary_from_value(item)
            if sanitized:
                parts.append(sanitized)
            if len(parts) >= 4:
                break
        return _sanitize_thinking_summary("\n".join(parts))
    return ""


def _ensure_visible_thinking_summary(turn: AgentTurn, *, source: str) -> AgentTurn:
    """Attach a conservative visible-summary fallback for user-facing replies.

    Some runtimes spend reasoning tokens but do not expose a displayable
    reasoning event or follow the JSON thinking protocol. The UI still expects a
    collapsible summary, so provide a short, honest context summary without
    inventing hidden chain-of-thought.
    """
    if turn.thinking_summary or not turn.messages:
        return turn
    turn.thinking_summary = _sanitize_thinking_summary(
        "参考了当前消息和最近对话上下文，整理成这次可见回复。"
    )
    turn.thinking_kind = "agent_summary"
    turn.thinking_source = _sanitize_thinking_meta(source, max_len=80) or "resident_fallback"
    turn.thinking_native = False
    return turn


def _merge_agent_turn(dst: AgentTurn, src: AgentTurn) -> AgentTurn:
    dst.actions.extend(src.actions)
    dst.messages.extend(src.messages)
    dst.tool_calls.extend(src.tool_calls)
    prefer_src_thinking = bool(src.thinking_summary) and (
        not dst.thinking_summary
        or (
            src.thinking_kind == "agent_summary"
            and dst.thinking_kind == "provider_reasoning"
        )
    )
    if prefer_src_thinking:
        dst.thinking_summary = src.thinking_summary
        dst.thinking_kind = src.thinking_kind
        dst.thinking_source = src.thinking_source
        dst.thinking_model = src.thinking_model
        dst.thinking_native = src.thinking_native
    dst.runtime_debug.update(src.runtime_debug)
    return dst


def _agent_turn_from_content_blocks(
    blocks: Any,
    *,
    thinking_source: str = "",
    thinking_model: str = "",
) -> AgentTurn:
    turn = AgentTurn()
    if not isinstance(blocks, list):
        return turn
    for block in blocks:
        if isinstance(block, str):
            _merge_agent_turn(turn, _agent_turn_from_obj(block))
            continue
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "").strip().lower()
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str):
                _merge_agent_turn(turn, _agent_turn_from_obj(text))
            continue
        if block_type == "thinking" and not turn.thinking_summary:
            summary = block.get("thinking") or block.get("text")
            if isinstance(summary, str):
                turn.thinking_summary = _sanitize_thinking_summary(summary)
                turn.thinking_kind = "provider_reasoning"
                turn.thinking_source = thinking_source or "anthropic_thinking"
                turn.thinking_model = thinking_model
                turn.thinking_native = True
    return turn


def _dedupe_agent_turn_messages(turn: AgentTurn) -> AgentTurn:
    seen = set()
    unique: list[str] = []
    for message_text in turn.messages:
        key = message_text.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(key)
    turn.messages = unique
    return turn


def _agent_turn_from_obj(obj: Any) -> AgentTurn:
    turn = AgentTurn()

    if isinstance(obj, str):
        raw = obj.strip()
        if not raw:
            return turn
        visible_fragment = _visible_reply_fragment_from_text(raw)
        if visible_fragment is not None:
            return _agent_turn_from_obj(visible_fragment)
        json_objects = _json_objects_from_cli_output(raw)
        if json_objects:
            for item in json_objects:
                _merge_agent_turn(turn, _agent_turn_from_obj(item))
            if turn.messages or turn.actions or turn.thinking_summary or turn.tool_calls:
                return _dedupe_agent_turn_messages(turn)
        nested = _safe_json_loads(raw) if _looks_like_json_text(raw) else None
        if isinstance(nested, (dict, list)):
            return _agent_turn_from_obj(nested)
        raw, tagged_thinking = _split_tagged_thinking(raw)
        if tagged_thinking:
            turn.thinking_summary = _sanitize_thinking_summary(tagged_thinking)
            turn.thinking_kind = "provider_reasoning_summary"
            turn.thinking_source = "tagged_content"
            turn.thinking_native = False
        clean = _sanitize_reply_text(raw)
        if clean:
            turn.messages.append(clean)
        return turn

    if isinstance(obj, list):
        for item in obj:
            _merge_agent_turn(turn, _agent_turn_from_obj(item))
        return turn

    if not isinstance(obj, dict):
        return turn

    # Streaming transport events (reasoning/thinking/tool/delta/handshake) carry
    # no user-visible reply — only their final-answer sibling does. `_reply_from_
    # json_obj` already skips these; mirror it here so a stray reasoning event
    # (e.g. codex 0.142 `agent_reasoning`) can never be emitted as a chat bubble.
    marker = str(
        obj.get("event")
        or obj.get("type")
        or obj.get("kind")
        or obj.get("phase")
        or ""
    ).strip().lower()
    if marker in _JSON_NON_FINAL_EVENTS:
        return turn

    # Capture-lane agents are asked to return a strict {"cards": [...]} JSON
    # object. Preserve that JSON as the final text so the capture handler can
    # parse it instead of treating it as an unknown non-chat payload.
    if isinstance(obj.get("cards"), list):
        turn.messages.append(json.dumps({"cards": obj.get("cards")}, ensure_ascii=False))
        return turn

    # OpenClaw `agent --json` nests reply text under result.payloads[].text,
    # which the generic reply-field recursion below does not reach. Capture it
    # explicitly so an OpenClaw resident entry produces usable messages instead
    # of "no usable reply after sanitization".
    openclaw_texts = _openclaw_payload_texts(obj)
    if openclaw_texts:
        turn.messages.extend(openclaw_texts)

    for key in _JSON_RUNTIME_DEBUG_FIELDS:
        if key in obj:
            value = obj.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                turn.runtime_debug[key] = value
            else:
                turn.runtime_debug[key] = "<structured>"

    raw_actions = obj.get("actions")
    if isinstance(raw_actions, list):
        turn.actions.extend([a for a in raw_actions if isinstance(a, dict)])

    raw_tool_calls = obj.get("tool_calls")
    if isinstance(raw_tool_calls, list):
        for tc in raw_tool_calls:
            if not isinstance(tc, dict):
                continue
            name = str(tc.get("name") or "").strip()
            if not name:
                continue
            args = dict(tc["args"]) if isinstance(tc.get("args"), dict) else {}
            turn.tool_calls.append({"name": name, "args": args})

    explicit_kind = _sanitize_thinking_kind(obj.get("thinking_kind") or obj.get("reasoning_kind"))
    explicit_source = _sanitize_thinking_meta(
        obj.get("thinking_source") or obj.get("reasoning_source"),
        max_len=80,
    )
    explicit_model = _sanitize_thinking_meta(
        obj.get("thinking_model") or obj.get("reasoning_model") or obj.get("model"),
        max_len=96,
    )
    explicit_native = _boolish(obj.get("thinking_native", obj.get("reasoning_native")))

    role = str(obj.get("role") or "").lower()
    if (not role or role in {"assistant", "agent", "openclaw", "model"}) and isinstance(obj.get("content"), list):
        _merge_agent_turn(
            turn,
            _agent_turn_from_content_blocks(
                obj.get("content"),
                thinking_source=explicit_source,
                thinking_model=explicit_model,
            ),
        )

    for key in _JSON_THINKING_FIELDS:
        value = obj.get(key)
        summary = _thinking_summary_from_value(value) if not turn.thinking_summary else ""
        if summary:
            turn.thinking_summary = summary
            turn.thinking_kind = explicit_kind or _default_thinking_kind_for_key(key)
            turn.thinking_source = explicit_source
            turn.thinking_model = explicit_model
            turn.thinking_native = explicit_native
            if isinstance(value, dict):
                turn.thinking_kind = (
                    _sanitize_thinking_kind(value.get("kind"))
                    or explicit_kind
                    or _default_thinking_kind_for_key(key)
                )
                turn.thinking_source = (
                    _sanitize_thinking_meta(value.get("source"), max_len=80)
                    or explicit_source
                )
                turn.thinking_model = (
                    _sanitize_thinking_meta(value.get("model"), max_len=96)
                    or explicit_model
                )
                turn.thinking_native = _boolish(value.get("native"))
                if turn.thinking_native is None:
                    turn.thinking_native = explicit_native

    messages = obj.get("messages")
    if isinstance(messages, list):
        for item in messages:
            if isinstance(item, dict):
                role = str(item.get("role") or "").lower()
                if role and role not in {"assistant", "agent", "openclaw", "model"}:
                    continue
            _merge_agent_turn(turn, _agent_turn_from_obj(item))

    choices = obj.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            _merge_agent_turn(turn, _agent_turn_from_obj(choice))

    for field_name in _JSON_REPLY_FIELDS:
        value = obj.get(field_name)
        if value is None:
            continue
        nested_turn = _agent_turn_from_obj(value)
        _merge_agent_turn(turn, nested_turn)

    # OpenAI-style choice objects usually nest the final text at
    # choice.message.content. The generic reply-field loop above sees
    # `message`, but this explicit path keeps role filtering intact when
    # other metadata is present beside the message object.
    message = obj.get("message")
    if isinstance(message, dict):
        role = str(message.get("role") or "").lower()
        if not role or role in {"assistant", "agent", "openclaw", "model"}:
            _merge_agent_turn(turn, _agent_turn_from_obj(message.get("content")))

    # Drop accidental full-runtime JSON messages when no final-answer field was
    # found. Returning an empty turn is better than sending token/account JSON
    # to the user.
    if not turn.messages and turn.runtime_debug:
        return turn

    # De-dupe while preserving order.
    seen = set()
    unique: list[str] = []
    for message_text in turn.messages:
        key = message_text.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(key)
    turn.messages = unique

    # De-dupe tool_calls too: one emission can arrive via multiple nested paths
    # (e.g. an OpenAI choice.message handled by both the reply-field loop and
    # the explicit message path), and the tool loop would otherwise execute the
    # same call twice.
    seen_tc: set = set()
    unique_tc: list[dict] = []
    for tc in turn.tool_calls:
        tc_key = (tc.get("name"), json.dumps(tc.get("args") or {}, sort_keys=True))
        if tc_key in seen_tc:
            continue
        seen_tc.add(tc_key)
        unique_tc.append(tc)
    turn.tool_calls = unique_tc
    return turn


def _agent_turn_from_raw(raw_reply: Any, max_items: int | None = None) -> AgentTurn:
    turn = _agent_turn_from_obj(raw_reply)
    turn.messages = _cap_agent_replies(turn.messages, max_items=max_items)
    return turn


def _multi_reply_json_from_obj(obj: Any) -> str:
    """Preserve explicit multi-bubble JSON instead of collapsing it."""
    openclaw_texts = _openclaw_payload_texts(obj)
    if openclaw_texts:
        return json.dumps({"messages": openclaw_texts}, ensure_ascii=False)
    messages: Any = None
    if isinstance(obj, dict) and isinstance(obj.get("messages"), list):
        messages = obj.get("messages")
    elif isinstance(obj, list):
        messages = obj
    if not isinstance(messages, list):
        return ""
    out = [item.strip() for item in messages if isinstance(item, str) and item.strip()]
    if not out:
        return ""
    return json.dumps({"messages": out}, ensure_ascii=False)


def _json_objects_from_cli_output(raw: str) -> list[Any]:
    """Parse structured CLI output without interpreting human terminal UI."""
    raw = raw.strip()
    if not raw:
        return []

    try:
        return [json.loads(raw)]
    except (json.JSONDecodeError, TypeError):
        pass

    objects: list[Any] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] not in "[{":
            continue
        try:
            objects.append(json.loads(stripped))
        except (json.JSONDecodeError, TypeError):
            continue
    return objects


def _cli_error_detail(stdout: str, stderr: str) -> str:
    """Best error string for a non-zero CLI exit.

    Both CLIs report API failures on STDOUT while stderr is often empty or just a
    warning: claude ``--output-format json`` emits a result object
    (``is_error`` + ``result`` text + ``api_error_status``); codex ``--json`` emits
    ``error`` events (``message``). Surface that so ``cli agent exited`` is
    actionable instead of blank. Falls back to stderr, then a stdout snippet.
    """
    claude_err = ""
    codex_err = ""
    for obj in _json_objects_from_cli_output(stdout or ""):
        if not isinstance(obj, dict):
            continue
        if not claude_err and obj.get("is_error") and isinstance(obj.get("result"), str):
            status = obj.get("api_error_status")
            claude_err = obj["result"] + (f" (api_status={status})" if status else "")
        if obj.get("type") == "error" and isinstance(obj.get("message"), str):
            codex_err = obj["message"]   # keep the last error event (the final one)
    detail = claude_err or codex_err
    if detail:
        return detail[:300]
    if (stderr or "").strip():
        return stderr.strip()[:300]
    return (stdout or "").strip()[:300]


def _codex_turn_from_stream(raw: str) -> tuple[str, str]:
    """Split a ``codex exec --json`` event stream into (reply, reasoning_summary).

    codex emits JSONL events. Two protocols are seen in the wild and both are
    handled here so the resident survives codex CLI upgrades:

    - **0.136 item protocol**: ``{"type":"item.completed","item":{"type":
      "agent_message","text":...}}`` with reasoning under ``item.type ==
      "reasoning"``.
    - **0.142 flat EventMsg protocol**: ``{"type":"agent_message","message":...}``
      with reasoning under ``{"type":"agent_reasoning","text":...}``.

    The assistant reply is joined in order; the reasoning summary is returned
    SEPARATELY so the caller routes it to the collapsible thinking disclosure
    instead of letting it leak as a chat bubble (the 0.142 regression: the old
    reader matched nothing → the turn fell through to the generic extractor →
    the reasoning event's ``text`` was emitted as a message). Both empty means a
    handshake-only / failed turn so the caller can fall back without leaking.
    """
    replies: list[str] = []
    reasoning: list[str] = []
    for obj in _json_objects_from_cli_output(raw):
        if not isinstance(obj, dict):
            continue
        etype = str(obj.get("type") or "").strip()

        # 0.136 item protocol: the payload is nested under `item`.
        if etype == "item.completed":
            item = obj.get("item")
            if not isinstance(item, dict):
                continue
            itype = str(item.get("type") or "").strip()
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            if itype == "agent_message":
                replies.append(text.strip())
            elif itype in {"reasoning", "agent_reasoning"}:
                reasoning.append(text.strip())
            continue

        # 0.142 flat EventMsg protocol: payload is the event object itself. The
        # final answer rides `message`; reasoning summaries ride `text`. Only the
        # consolidated `agent_reasoning` event is collected — the streaming
        # `agent_reasoning_delta` fragments would just duplicate it.
        if etype == "agent_message":
            text = obj.get("message")
            if not isinstance(text, str):
                text = obj.get("text")
            if isinstance(text, str) and text.strip():
                replies.append(text.strip())
        elif etype == "agent_reasoning":
            text = obj.get("text")
            if not isinstance(text, str):
                text = obj.get("message")
            if isinstance(text, str) and text.strip():
                reasoning.append(text.strip())

    return "\n\n".join(replies), "\n\n".join(reasoning)


def _codex_reply_from_stream(raw: str) -> str:
    """Back-compat shim: the assistant reply only (reasoning dropped)."""
    return _codex_turn_from_stream(raw)[0]


def _codex_attach_reasoning(reply: str, reasoning: str) -> str:
    """Fold codex reasoning-summary text into the reply payload as a thinking
    summary so the resident routes it to the collapsible disclosure instead of
    leaking it as a chat bubble.

    The reply's own JSON shape is preserved when present (a codex
    ``agent_message`` is often an ``{"actions":[...]}`` / ``{"messages":[...]}``
    object), so this never double-wraps actions into a bubble.
    """
    parsed: Any = None
    try:
        parsed = json.loads(reply)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        payload = dict(parsed)
    elif isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
        payload = {"messages": parsed}
    else:
        payload = {"messages": [reply]}
    payload.setdefault("thinking_summary", reasoning)
    payload.setdefault("thinking_kind", "provider_reasoning_summary")
    payload.setdefault("thinking_source", "codex_reasoning")
    payload.setdefault("thinking_native", True)
    return json.dumps(payload, ensure_ascii=False)


def _extract_text_from_cli_output(raw: str) -> str:
    """Best-effort extraction from raw CLI stdout.

    1. Try JSON parse first when a runtime provides structured output.
    2. Remove explicit reasoning/code sections.
    3. Strip known headers/footers.
    4. Return the full remaining answer, preserving multi-paragraph replies.
    """
    raw = raw.strip()
    if not raw:
        return ""

    for obj in reversed(_json_objects_from_cli_output(raw)):
        multi = _multi_reply_json_from_obj(obj)
        if multi:
            return multi
        text = _reply_from_json_obj(obj)
        if text:
            return text

    raw, _tagged_thinking = _split_tagged_thinking(raw)
    raw = _strip_reasoning_sections(raw)
    clean = [ln.rstrip() for ln in raw.splitlines() if not _NOISE_LINE_RE.match(ln)]
    text = "\n".join(clean).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text


def _agent_http_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if AGENT_HTTP_TOKEN:
        headers["Authorization"] = f"Bearer {AGENT_HTTP_TOKEN}"
    return headers


def _agent_session_key() -> str:
    if AGENT_HTTP_SESSION_KEY.strip():
        return AGENT_HTTP_SESSION_KEY.strip()
    user_id = (_whoami_cache.get("user_id") or "").strip()
    if user_id:
        return f"feedling:{user_id}"
    digest = hashlib.sha1(FEEDLING_API_KEY.encode()).hexdigest()[:12]
    return f"feedling:{digest}"


def _extract_openai_reply(body: dict) -> str:
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
            text = first.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    raise ValueError("OpenAI-compatible response has no usable reply text")


def _response_text_len(resp: httpx.Response) -> int:
    try:
        return len((resp.text or "").encode("utf-8"))
    except Exception:
        return 0


def _remember_http_session(resp: httpx.Response, *, sent_bytes: int = 0, received_bytes: int = 0) -> None:
    sid = (resp.headers.get(AGENT_HTTP_SESSION_HEADER) or "").strip()
    if sid:
        _save_agent_session_id(sid)
        _record_agent_session_turn(sid, sent_bytes=sent_bytes, received_bytes=received_bytes)


def _content_blocks_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return ""


def _raw_assistant_text(body: Any) -> str:
    """The model's *literal* assistant text, with NO chat-bubble sanitization.

    Memory background lanes (capture/dream) parse JSON out of the model output
    with their own robust extractors; they must NOT go through
    _sanitize_reply_text, which is built for user-visible chat and decapitates a
    pretty-printed JSON object (it strips every non-CJK line before the first
    Chinese character). Returns "" when no content string can be located, so the
    caller can fall back to the normal sanitized path.
    """
    if isinstance(body, str):
        return body
    if not isinstance(body, dict):
        return ""
    choices = body.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if isinstance(message, dict):
                text = _content_blocks_to_text(message.get("content"))
                if text.strip():
                    return text
            text = _content_blocks_to_text(choice.get("text"))
            if text.strip():
                return text
    # Generic / "simple" protocols: a top-level reply field.
    for reply_field in ("response", "reply", "content", "text", "output"):
        text = _content_blocks_to_text(body.get(reply_field))
        if text.strip():
            return text
    return ""


def _call_agent_http_simple(message: str, images: list[dict[str, str]] | None = None, raw_text: bool = False) -> Any:
    headers = _agent_http_headers()
    payload = {"message": message}
    if images:
        payload["images"] = images
    resp = httpx.post(AGENT_HTTP_URL, json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    _remember_http_session(
        resp,
        sent_bytes=len(message.encode("utf-8")),
        received_bytes=_response_text_len(resp),
    )
    body = resp.json()
    if raw_text:
        text = _raw_assistant_text(body)
        if text.strip():
            return text
    if isinstance(body, dict):
        turn = _agent_turn_from_raw(body)
        if turn.actions or turn.thinking_summary or turn.tool_calls or len(turn.messages) > 1:
            return body
        if turn.messages:
            return turn.messages[0]
        raise ValueError(f"response field not found in: {list(body.keys())}")
    if isinstance(body, str):
        return body.strip()
    raise ValueError(f"unexpected response type: {type(body)}")


def _call_agent_http_openai(message: str, images: list[dict[str, str]] | None = None, raw_text: bool = False) -> Any:
    headers = _agent_http_headers()
    sid = _load_agent_session_id()
    if sid:
        headers[AGENT_HTTP_SESSION_HEADER] = sid
    session_key = _agent_session_key()
    if session_key:
        headers[AGENT_HTTP_SESSION_KEY_HEADER] = session_key

    content: Any = message
    if images:
        blocks: list[dict[str, Any]] = [{"type": "text", "text": message}]
        for image in images:
            data_url = image.get("data_url")
            if data_url:
                blocks.append({"type": "image_url", "image_url": {"url": data_url}})
        content = blocks

    payload = {
        "model": AGENT_HTTP_MODEL,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
    }
    resp = httpx.post(AGENT_HTTP_URL, json=payload, headers=headers, timeout=120)
    resp.raise_for_status()
    _remember_http_session(
        resp,
        sent_bytes=len(str(content).encode("utf-8")),
        received_bytes=_response_text_len(resp),
    )
    body = resp.json()
    if not isinstance(body, dict):
        raise ValueError(f"unexpected OpenAI response type: {type(body)}")
    if raw_text:
        text = _raw_assistant_text(body)
        if text.strip():
            return text
    turn = _agent_turn_from_raw(body)
    if turn.actions or turn.thinking_summary or turn.tool_calls or len(turn.messages) > 1:
        return body
    if turn.messages:
        return turn.messages[0]
    raise ValueError("OpenAI-compatible response has no usable reply text")


def call_agent_http(message: str, images: list[dict[str, str]] | None = None, raw_text: bool = False) -> Any:
    if not AGENT_HTTP_URL:
        raise ValueError("AGENT_HTTP_URL is not set for http mode")
    if AGENT_HTTP_PROTOCOL in {"openai", "hermes", "chat_completions", "chat-completions"}:
        return _call_agent_http_openai(message, images=images, raw_text=raw_text)
    if AGENT_HTTP_PROTOCOL in {"simple", "generic", "json"}:
        return _call_agent_http_simple(message, images=images, raw_text=raw_text)
    raise ValueError(f"unknown AGENT_HTTP_PROTOCOL: {AGENT_HTTP_PROTOCOL!r}")


def _agent_session_file_for_user() -> Path:
    user_id = _agent_session_user_id()
    path = AGENT_SESSION_FILE_TEMPLATE.replace("{user_id}", user_id)
    return Path(path)


def _agent_session_user_id() -> str:
    return (_whoami_cache.get("user_id") or "unknown").strip() or "unknown"


def _empty_agent_session_meta(session_id: str = "") -> dict[str, Any]:
    return {
        "session_id": session_id,
        "turns": 0,
        "bytes": 0,
        "created_at": time.time() if session_id else 0.0,
        "updated_at": time.time() if session_id else 0.0,
    }


def _coerce_agent_session_meta(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return _empty_agent_session_meta()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return _empty_agent_session_meta(text)
        return _coerce_agent_session_meta(parsed)
    if not isinstance(raw, dict):
        return _empty_agent_session_meta()

    sid = str(raw.get("session_id") or raw.get("sessionId") or raw.get("session") or "").strip()
    meta = _empty_agent_session_meta(sid)
    for key in ("turns", "bytes"):
        try:
            meta[key] = max(0, int(raw.get(key) or 0))
        except (TypeError, ValueError):
            meta[key] = 0
    for key in ("created_at", "updated_at"):
        try:
            meta[key] = float(raw.get(key) or meta[key] or 0.0)
        except (TypeError, ValueError):
            pass
    return meta


def _agent_session_meta_exceeds_bounds(meta: dict[str, Any]) -> bool:
    if not str(meta.get("session_id") or "").strip():
        return False
    if AGENT_SESSION_MAX_TURNS > 0 and int(meta.get("turns") or 0) >= AGENT_SESSION_MAX_TURNS:
        return True
    if AGENT_SESSION_MAX_BYTES > 0 and int(meta.get("bytes") or 0) >= AGENT_SESSION_MAX_BYTES:
        return True
    return False


def _clear_agent_session_id(reason: str = "") -> None:
    user_id = _agent_session_user_id()
    _agent_session_id_cache.pop(user_id, None)
    _agent_session_meta_cache.pop(user_id, None)
    try:
        _agent_session_file_for_user().unlink(missing_ok=True)
    except Exception as e:
        log.warning("failed to clear agent session id: %s", e)
    if reason:
        log.warning("rotating resident agent session for user=%s reason=%s", user_id, reason)


def _load_agent_session_meta(*, check_bounds: bool = True) -> dict[str, Any]:
    user_id = _agent_session_user_id()
    cached_meta = _agent_session_meta_cache.get(user_id)
    if isinstance(cached_meta, dict):
        meta = _coerce_agent_session_meta(cached_meta)
    else:
        cached_sid = _agent_session_id_cache.get(user_id)
        if cached_sid:
            meta = _empty_agent_session_meta(cached_sid)
        else:
            f = _agent_session_file_for_user()
            try:
                meta = _coerce_agent_session_meta(f.read_text(encoding="utf-8"))
            except Exception:
                meta = _empty_agent_session_meta()

    if check_bounds and _agent_session_meta_exceeds_bounds(meta):
        reason = f"turns={meta.get('turns')} bytes={meta.get('bytes')}"
        _clear_agent_session_id(reason)
        return _empty_agent_session_meta()

    sid = str(meta.get("session_id") or "").strip()
    if sid:
        _agent_session_id_cache[user_id] = sid
        _agent_session_meta_cache[user_id] = dict(meta)
    return dict(meta)


def _load_agent_session_id() -> str:
    return str(_load_agent_session_meta().get("session_id") or "").strip()


def _save_agent_session_id(sid: str) -> None:
    sid = (sid or "").strip()
    if not sid:
        return

    user_id = _agent_session_user_id()
    existing = _load_agent_session_meta(check_bounds=False)
    if str(existing.get("session_id") or "").strip() == sid:
        meta = dict(existing)
    else:
        meta = _empty_agent_session_meta(sid)
    meta["session_id"] = sid
    meta["updated_at"] = time.time()

    _agent_session_id_cache[user_id] = sid
    _agent_session_meta_cache[user_id] = dict(meta)

    f = _agent_session_file_for_user()
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(meta, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    except Exception as e:
        log.warning("failed to persist agent session id: %s", e)


def _record_agent_session_turn(sid: str, *, sent_bytes: int = 0, received_bytes: int = 0) -> None:
    sid = (sid or "").strip()
    if not sid:
        return
    existing = _load_agent_session_meta(check_bounds=False)
    meta = dict(existing) if str(existing.get("session_id") or "").strip() == sid else _empty_agent_session_meta(sid)
    meta["session_id"] = sid
    meta["turns"] = int(meta.get("turns") or 0) + 1
    meta["bytes"] = int(meta.get("bytes") or 0) + max(0, int(sent_bytes or 0)) + max(0, int(received_bytes or 0))
    meta["updated_at"] = time.time()

    user_id = _agent_session_user_id()
    _agent_session_id_cache[user_id] = sid
    _agent_session_meta_cache[user_id] = dict(meta)
    try:
        f = _agent_session_file_for_user()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(meta, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    except Exception as e:
        log.warning("failed to persist agent session metrics: %s", e)


def _extract_session_id(raw: str) -> str:
    if not raw:
        return ""
    for obj in reversed(_json_objects_from_cli_output(raw)):
        sid = _session_id_from_obj(obj)
        if sid:
            return sid
    m = re.search(r'"?session_id"?\s*:\s*"?([A-Za-z0-9_\-]+)"?', raw)
    if m:
        return m.group(1)
    m = re.search(r'"?sessionId"?\s*:\s*"?([A-Za-z0-9_\-]+)"?', raw)
    if m:
        return m.group(1)
    m = re.search(r"Resumed session\s+([A-Za-z0-9_\-]+)", raw)
    if m:
        return m.group(1)
    return ""


def _session_id_from_obj(obj: Any) -> str:
    if isinstance(obj, dict):
        for field in ("session_id", "sessionId", "session"):
            value = obj.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in obj.values():
            sid = _session_id_from_obj(value)
            if sid:
                return sid
    elif isinstance(obj, list):
        for item in obj:
            sid = _session_id_from_obj(item)
            if sid:
                return sid
    return ""


def _resolve_cli_executable(cmd: list[str]) -> list[str]:
    if not cmd:
        return cmd

    executable = cmd[0]
    if os.path.sep in executable:
        return cmd

    search_parts: list[str] = []
    if AGENT_CLI_PATH:
        search_parts.extend(p for p in AGENT_CLI_PATH.split(os.pathsep) if p)
    if os.environ.get("PATH"):
        search_parts.extend(p for p in os.environ["PATH"].split(os.pathsep) if p)

    home = Path.home()
    search_parts.extend(
        [
            str(home / ".local" / "bin"),
            str(home / ".hermes" / "hermes-agent" / "venv" / "bin"),
            str(home / ".hermes" / "bin"),
            str(home / ".cargo" / "bin"),
            "/usr/local/bin",
            "/opt/homebrew/bin",
        ]
    )
    search_path = os.pathsep.join(dict.fromkeys(search_parts))
    resolved = shutil.which(executable, path=search_path)
    if not resolved:
        raise FileNotFoundError(
            f"CLI executable {executable!r} was not found. Use an absolute path "
            "in AGENT_CLI_CMD or set AGENT_CLI_PATH for the systemd service."
        )

    if resolved != executable:
        log.debug("resolved cli executable %s -> %s", executable, resolved)
    return [resolved, *cmd[1:]]


def _is_hermes_chat_cmd(cmd: list[str]) -> bool:
    return bool(cmd) and Path(cmd[0]).name == "hermes" and "chat" in cmd[1:]


def _is_claude_code_cmd(cmd: list[str]) -> bool:
    return bool(cmd) and Path(cmd[0]).name == "claude"


def _is_codex_cmd(cmd: list[str]) -> bool:
    return bool(cmd) and Path(cmd[0]).name == "codex"


def _cli_cmd_tokens() -> list[str]:
    """Tokenize the raw AGENT_CLI_CMD template for driver detection.

    Placeholders like ``{message}`` survive shlex.split unharmed; we only need
    cmd[0] to name the driver, so no substitution is required."""
    try:
        return shlex.split(AGENT_CLI_CMD)
    except ValueError:
        return AGENT_CLI_CMD.split()


def _foreground_history_injection_enabled(cmd: list[str] | None = None) -> bool:
    """Whether foreground turns get a resident-injected recent-chat transcript.

    Gated so we don't double up context for agents that already carry it: only
    codex (no --resume) and claude (hosted command has no session; its scrape +
    --resume continuity is unreliable) inject in ``auto``. pi resumes natively so
    it is skipped. ``on``/``always`` forces it for any driver; ``off`` disables.

    A claude command that ALREADY carries its own continuity in the operator's
    template (``--resume`` / ``-r`` / ``--session-id``) is skipped in ``auto`` too:
    it has native session, so a resident transcript would double-supply context.
    The hosted default claude command has none of these, so it still injects."""
    mode = FOREGROUND_CHAT_CONTEXT_MODE
    if mode in {"0", "false", "off", "no", "none", "disabled"}:
        return False
    if mode in {"1", "true", "on", "yes", "always"}:
        return True
    cmd = cmd if cmd is not None else _cli_cmd_tokens()
    if _is_codex_cmd(cmd):
        return True
    if _is_claude_code_cmd(cmd):
        return not (_has_cli_resume(cmd) or _has_claude_session_id(cmd))
    return False


def _cli_template_is_codex() -> bool:
    """True when AGENT_CLI_CMD drives ``codex`` (so we attach images natively)."""
    return _is_codex_cmd(_cli_cmd_tokens())


def _inject_codex_images(cmd: list[str], image_paths: list[str]) -> list[str]:
    """Attach decrypted image files to a ``codex exec`` command as vision input.

    codex's ``--image <FILE>`` feeds the image as real vision input, unlike the
    text file-path the model can't actually see. We emit the *=-bound* form
    ``--image=<path>`` (one per image): each occurrence carries exactly one value,
    so clap's variadic ``--image <FILE>...`` cannot greedily swallow the positional
    prompt — critical for minimal templates like ``codex exec {message}`` where the
    prompt immediately follows the injected flags (a bare ``-i <path> <prompt>``
    would eat ``<prompt>`` as a second image). No-op when the operator already wired
    an explicit ``-i``/``--image`` into their own template — they own images then.
    """
    if not image_paths or any(t == "-i" or t.startswith("--image") for t in cmd):
        return cmd
    try:
        insert_at = cmd.index("exec") + 1
    except ValueError:
        insert_at = 1
    flags = [f"--image={path}" for path in image_paths]
    return [*cmd[:insert_at], *flags, *cmd[insert_at:]]


def _cli_flag_value(cmd: list[str], flag: str) -> str:
    try:
        idx = cmd.index(flag)
    except ValueError:
        return ""
    if idx + 1 >= len(cmd):
        return ""
    return cmd[idx + 1]


def _set_cli_option_value(cmd: list[str], flag: str, value: str) -> list[str]:
    out = list(cmd)
    try:
        idx = out.index(flag)
    except ValueError:
        return out
    if idx + 1 >= len(out) or out[idx + 1].startswith("-"):
        out.insert(idx + 1, value)
    else:
        out[idx + 1] = value
    return out


def _new_agent_session_id() -> str:
    user_id = _agent_session_user_id()
    user_part = hashlib.sha1((user_id or FEEDLING_API_KEY).encode()).hexdigest()[:8]
    nonce = f"{int(time.time())}-{os.getpid()}-{int(time.monotonic() * 1000) % 100000}"
    return f"{AGENT_SESSION_ROTATE_PREFIX}-{user_part}-{nonce}"


def _ensure_explicit_cli_session_id(cmd: list[str], sid: str) -> tuple[list[str], str]:
    if "--session-id" not in cmd:
        return cmd, sid
    bounded_sid = sid.strip() if sid else _new_agent_session_id()
    if not sid:
        _save_agent_session_id(bounded_sid)
    fixed_sid = _cli_flag_value(cmd, "--session-id")
    if fixed_sid and fixed_sid != bounded_sid:
        log.warning(
            "replacing fixed AGENT_CLI_CMD --session-id %s with bounded resident session %s",
            fixed_sid,
            bounded_sid,
        )
    return _set_cli_option_value(cmd, "--session-id", bounded_sid), bounded_sid


def _warn_if_agent_entry_may_drift() -> None:
    """Log non-fatal warnings for common context/persona drift configs.

    The resident consumer should call the user's real runtime entry. It should
    not invent a mini persona prompt or a shallow throwaway session just for IO.
    We keep this as diagnostics instead of hard failure because non-Hermes
    runtimes legitimately vary, but the warnings make bad configs visible in
    systemd logs before users experience a strange persona shift.
    """
    if AGENT_MODE != "cli" or not AGENT_CLI_CMD:
        return

    lower_template = AGENT_CLI_CMD.lower()
    if re.search(r"\b(you are|user message|reply naturally|same style|persona)\b", lower_template):
        log.warning(
            "AGENT_CLI_CMD appears to wrap {message} in an identity/persona "
            "prompt. For continuity, call the real agent entry directly and "
            "let the runtime's own profile/memory shape the reply."
        )

    try:
        cmd = shlex.split(AGENT_CLI_CMD.replace("{message}", "__MSG__"))
    except ValueError as e:
        log.warning("AGENT_CLI_CMD could not be parsed for drift checks: %s", e)
        return

    if not _is_hermes_chat_cmd(cmd):
        return

    if not os.environ.get("HERMES_HOME"):
        log.warning(
            "Hermes/OpenClaw CLI is configured without HERMES_HOME. systemd may "
            "use a different profile than the user's resident agent. Set "
            "HERMES_HOME to the real profile, for example "
            "/home/openclaw/.hermes/profiles/daily."
        )

    if "--source" not in cmd:
        log.warning(
            "Hermes/OpenClaw CLI has no --source flag. Use --source tool so IO "
            "messages enter the normal tool-origin conversation path."
        )

    output_mode = _cli_flag_value(cmd, "--output-mode")
    if output_mode:
        log.warning(
            "Hermes/OpenClaw CLI includes --output-mode %s. Current Hermes chat "
            "deployments do not support this flag; the resident will remove it "
            "before execution.",
            output_mode,
        )

    turns_raw = _cli_flag_value(cmd, "--max-turns")
    if turns_raw:
        try:
            turns = int(turns_raw)
            if turns < 20:
                log.warning(
                    "Hermes/OpenClaw CLI uses --max-turns %d. Very small turn "
                    "limits often produce short/template replies. Prefer "
                    "--max-turns 60 for IO chat unless your runtime has a "
                    "stronger native session endpoint.",
                    turns,
                )
        except ValueError:
            pass


def _strip_hermes_continue(cmd: list[str]) -> tuple[list[str], bool]:
    """Remove Hermes --continue/-c from resident-owned commands.

    The resident owns continuity by persisting the first Hermes session_id and
    injecting --resume <session_id> on later turns. --continue means "latest
    local session" and can attach Feedling to the wrong conversation.
    """
    out: list[str] = []
    i = 0
    removed = False
    while i < len(cmd):
        token = cmd[i]
        if token in {"--continue", "-c"}:
            removed = True
            i += 1
            # Hermes accepts an optional session name after --continue. Drop it
            # only when it is clearly not another flag.
            if i < len(cmd) and not cmd[i].startswith("-"):
                i += 1
            continue
        out.append(token)
        i += 1
    return out, removed


def _strip_cli_option_value(cmd: list[str], flags: set[str]) -> tuple[list[str], bool]:
    out: list[str] = []
    i = 0
    removed = False
    while i < len(cmd):
        token = cmd[i]
        if token in flags:
            removed = True
            i += 1
            if i < len(cmd) and not cmd[i].startswith("-"):
                i += 1
            continue
        out.append(token)
        i += 1
    return out, removed


def _strip_cli_flags(cmd: list[str], flags: set[str]) -> tuple[list[str], bool]:
    out: list[str] = []
    removed = False
    for token in cmd:
        if token in flags:
            removed = True
            continue
        out.append(token)
    return out, removed


def _has_cli_resume(cmd: list[str]) -> bool:
    return "--resume" in cmd or "-r" in cmd


def _has_claude_session_id(cmd: list[str]) -> bool:
    return "--session-id" in cmd


def _has_claude_print(cmd: list[str]) -> bool:
    return "--print" in cmd or "-p" in cmd


def _has_claude_output_format(cmd: list[str]) -> bool:
    return "--output-format" in cmd


def _render_cli_template(message: str, sid: str, image_paths: list[str] | None = None) -> list[str]:
    image_paths = image_paths or []
    msg_token = "__FEEDLING_MESSAGE__"
    sid_token = "__FEEDLING_SESSION_ID__"
    image_path_token = "__FEEDLING_IMAGE_PATH__"
    image_paths_token = "__FEEDLING_IMAGE_PATHS__"
    template = (
        AGENT_CLI_CMD
        .replace("{message}", msg_token)
        .replace("{session_id}", sid_token)
        .replace("{image_path}", image_path_token)
        .replace("{image_paths}", image_paths_token)
    )
    cmd = shlex.split(template)
    first_image = image_paths[0] if image_paths else ""
    all_images = " ".join(image_paths)
    return [
        part
        .replace(msg_token, message)
        .replace(sid_token, sid)
        .replace(image_path_token, first_image)
        .replace(image_paths_token, all_images)
        for part in cmd
    ]


def _prepare_cli_command(message: str, image_paths: list[str] | None = None) -> list[str]:
    sid = _load_agent_session_id()
    template_has_image_slot = "{image_path" in AGENT_CLI_CMD
    # codex gets pixels natively via injected --image= flags (_inject_codex_images);
    # skip the file-path prose that only makes sense for a runtime that must open
    # the file itself (e.g. claude reading it via its Read tool).
    codex_native_images = (
        bool(image_paths) and not template_has_image_slot and _cli_template_is_codex()
    )
    rendered_message = message
    if image_paths and not template_has_image_slot and not codex_native_images:
        rendered_message = _message_for_agent(message, image_paths)
    cmd = _render_cli_template(rendered_message, sid, image_paths=image_paths)
    cmd, sid = _ensure_explicit_cli_session_id(cmd, sid)

    if _is_hermes_chat_cmd(cmd):
        cmd, removed_continue = _strip_hermes_continue(cmd)
        if removed_continue:
            log.warning(
                "removed Hermes --continue from AGENT_CLI_CMD; resident "
                "continuity uses stored session_id plus --resume"
            )
        cmd, removed_output_mode = _strip_cli_option_value(cmd, {"--output-mode"})
        if removed_output_mode:
            log.warning(
                "removed Hermes --output-mode from AGENT_CLI_CMD; this Hermes "
                "chat CLI does not support that flag in current deployments"
            )
        if sid and not _has_cli_resume(cmd) and "--session-id" not in cmd:
            cmd = [cmd[0], "--resume", sid, *cmd[1:]]
    elif _is_claude_code_cmd(cmd):
        cmd, removed_continue = _strip_cli_flags(cmd, {"--continue", "-c"})
        if removed_continue:
            log.warning(
                "removed Claude Code --continue from AGENT_CLI_CMD; resident "
                "continuity uses stored session_id plus --resume"
            )
        if not _has_claude_print(cmd):
            cmd = [cmd[0], "--print", *cmd[1:]]
        if not _has_claude_output_format(cmd):
            cmd = [cmd[0], "--output-format", "json", *cmd[1:]]
        # When THIS turn's message actually carries an injected recent-chat
        # transcript (see _foreground_agent_message), that transcript is the single
        # continuity source — do NOT also inject claude's fragile --resume, which
        # would duplicate context or start a fresh session on a stale id. But when
        # no transcript was injected (injection off, history unavailable, or first
        # turn), keep --resume as the fallback so continuity is never dropped on
        # both sides at once.
        if (
            sid
            and not _has_cli_resume(cmd)
            and not _has_claude_session_id(cmd)
            and not _message_has_injected_history(message)
        ):
            cmd = [cmd[0], "--resume", sid, *cmd[1:]]

    if codex_native_images:
        cmd = _inject_codex_images(cmd, image_paths or [])

    return _resolve_cli_executable(cmd)


def _codex_turn_metrics(raw: str) -> dict:
    """Best-effort {steps, input_tokens, output_tokens} from a codex event stream.

    codex ``exec --json`` (both the 0.136 ``item.completed`` and 0.142 flat
    protocols) has NO duration fields — unlike claude — so latency cannot be split
    from the stream. Token usage + agent-message count still characterize the turn.
    Token events are cumulative, so we keep the max seen. Never raises.
    """
    steps = 0
    in_tok = out_tok = 0

    def _pull_tokens(o: Any) -> None:
        nonlocal in_tok, out_tok
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    if k in ("input_tokens", "prompt_tokens"):
                        in_tok = max(in_tok, int(v))
                    elif k in ("output_tokens", "completion_tokens"):
                        out_tok = max(out_tok, int(v))
                else:
                    _pull_tokens(v)
        elif isinstance(o, list):
            for it in o:
                _pull_tokens(it)

    for obj in _json_objects_from_cli_output(raw):
        if not isinstance(obj, dict):
            continue
        etype = str(obj.get("type") or "").strip()
        item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
        if etype == "agent_message" or (etype == "item.completed" and item.get("type") == "agent_message"):
            steps += 1
        if "token" in etype or "usage" in obj or "info" in obj:
            _pull_tokens(obj)
    return {"steps": steps, "input_tokens": in_tok, "output_tokens": out_tok}


def _cli_turn_metrics(cmd: list[str], result: "subprocess.CompletedProcess", wall_ms: int) -> dict:
    """Driver-aware metrics for one CLI turn. Never raises.

    Returns a dict with keys shared across drivers so callers (timing logs,
    debug-trace events) don't need driver-specific branching:
    ``driver, rc, wall_ms, agent_ms, api_ms, num_turns, steps, input_tokens,
    output_tokens, out_chars``. Fields the driver doesn't report stay ``None``.
    """
    m = {"driver": "codex" if _is_codex_cmd(cmd) else "claude", "rc": result.returncode,
         "wall_ms": wall_ms, "agent_ms": None, "api_ms": None, "num_turns": None,
         "steps": None, "input_tokens": None, "output_tokens": None,
         "out_chars": len(result.stdout or "")}
    try:
        if m["driver"] == "codex":
            m.update(_codex_turn_metrics(result.stdout or ""))
        else:
            for obj in _json_objects_from_cli_output(result.stdout or ""):
                if isinstance(obj, dict) and obj.get("type") == "result":
                    m["agent_ms"] = obj.get("duration_ms")
                    m["api_ms"] = obj.get("duration_api_ms")
                    m["num_turns"] = obj.get("num_turns")
                    break
    except Exception:  # noqa: BLE001 — a metrics computation must never break a turn
        pass
    return m


def _log_cli_turn_timing(cmd: list[str], result: "subprocess.CompletedProcess", wall_ms: int) -> None:
    """Emit ONE structured timing line per CLI agent turn (observability only).

    Driver-aware — the two CLIs expose different metrics:

    - **claude** (``--output-format json``) reports ``duration_ms`` (agent total),
      ``duration_api_ms`` (time in provider calls) and ``num_turns``, so we derive:
        cold_start_ms    = wall_ms - agent_ms    (Node boot + MCP init the CLI
                           does not count — the per-turn cold-start tax)
        orchestration_ms = agent_ms - api_ms     (tool loop / memory reads)
        api_ms           = time inside the provider (e.g. deepseek) calls
    - **codex** (``exec --json``) has no duration fields; we log wall_ms plus
      best-effort token usage + agent-message step count.

    Best-effort: never raises, never changes behavior. ``driver=`` is always
    logged so blank fields aren't mistaken for missing claude data.
    """
    m = _cli_turn_metrics(cmd, result, wall_ms)

    if m["driver"] == "codex":
        log.info(
            "[turn-timing] driver=codex rc=%s wall_ms=%d steps=%s in_tokens=%s "
            "out_tokens=%s out_chars=%d",
            m["rc"], m["wall_ms"], m.get("steps"), m.get("input_tokens"),
            m.get("output_tokens"), m["out_chars"],
        )
        return

    agent_ms, api_ms = m.get("agent_ms"), m.get("api_ms")
    cold_start_ms = orchestration_ms = None
    if isinstance(agent_ms, (int, float)):
        cold_start_ms = max(0, wall_ms - int(agent_ms))
        if isinstance(api_ms, (int, float)):
            orchestration_ms = max(0, int(agent_ms) - int(api_ms))
    log.info(
        "[turn-timing] driver=claude rc=%s wall_ms=%d agent_ms=%s api_ms=%s "
        "orchestration_ms=%s cold_start_ms=%s num_turns=%s out_chars=%d",
        m["rc"], m["wall_ms"], agent_ms, api_ms, orchestration_ms,
        cold_start_ms, m.get("num_turns"), m["out_chars"],
    )


def call_agent_cli(
    message: str,
    image_paths: list[str] | None = None,
    raw_text: bool = False,
    trace_id: str = "",
) -> Any:
    if not AGENT_CLI_CMD:
        raise ValueError("AGENT_CLI_CMD is not set for cli mode")

    cmd = _prepare_cli_command(message, image_paths=image_paths)
    command_sid = _cli_flag_value(cmd, "--session-id")
    log.debug("running cli agent: %s", cmd)
    _turn_t0 = time.monotonic()
    _emit_debug_trace("agent", "agent.model.call.start", trace_id=trace_id,
                      summary="cli turn start",
                      explain="模型调用发起（" + ("codex" if _is_codex_cmd(cmd) else "claude") + "）",
                      content_excerpt={"prompt_head": (message or "")[:1000]})
    child_env = os.environ.copy()
    if trace_id:
        child_env["FEEDLING_TRACE_ID"] = trace_id
        child_env["FEEDLING_DEBUG_TRACE_ID"] = trace_id
    else:
        child_env.pop("FEEDLING_TRACE_ID", None)
        child_env.pop("FEEDLING_DEBUG_TRACE_ID", None)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=child_env)
    except subprocess.TimeoutExpired:
        _emit_debug_trace("agent", "agent.model.call.error", status="error", trace_id=trace_id,
                          dur_ms=(time.monotonic() - _turn_t0) * 1000,
                          summary="cli turn timeout", explain="模型调用超时（120s 上限）— 卡在模型这一步")
        log.warning(
            "[turn-timing] driver=%s rc=timeout wall_ms=%d (hit 120s subprocess cap)",
            "codex" if _is_codex_cmd(cmd) else "claude",
            int((time.monotonic() - _turn_t0) * 1000),
        )
        raise
    _wall_ms = int((time.monotonic() - _turn_t0) * 1000)
    _log_cli_turn_timing(cmd, result, _wall_ms)
    _m = _cli_turn_metrics(cmd, result, _wall_ms)
    _emit_debug_trace(
        "agent", "agent.model.call.done" if result.returncode == 0 else "agent.model.call.error",
        status="ok" if result.returncode == 0 else "error", trace_id=trace_id, dur_ms=_wall_ms,
        summary=f"cli turn rc={result.returncode} {_m['driver']}",
        explain=(f"模型返回（{_m['driver']}，{_wall_ms}ms" +
                 (f"，{_m['num_turns']} 轮" if _m.get('num_turns') else "") + "）"
                 if result.returncode == 0 else f"模型调用失败 rc={result.returncode}"),
        detail={k: _m[k] for k in ("driver", "rc", "agent_ms", "api_ms", "num_turns",
                                   "steps", "input_tokens", "output_tokens")},
        content_excerpt={"reply_head": (result.stdout or "")[:1000],
                         "stderr_head": (result.stderr or "")[:500]},
    )

    raw_transport = (result.stdout or "") + "\n" + (result.stderr or "")
    observed_sid = _extract_session_id(raw_transport) or command_sid
    if observed_sid:
        _save_agent_session_id(observed_sid)
        _record_agent_session_turn(
            observed_sid,
            sent_bytes=len((message or "").encode("utf-8")),
            received_bytes=len(raw_transport.encode("utf-8")),
        )

    if result.returncode != 0:
        raise RuntimeError(
            f"cli agent exited {result.returncode}: "
            f"{_cli_error_detail(result.stdout or '', result.stderr or '')}"
        )

    # codex `exec --json` streams JSONL events; the assistant's text and its
    # reasoning summary live in dedicated events, NOT in any field the generic
    # extractor recognizes. Pull both from the stream before falling through
    # (else the consumer would mis-send the `thread.started` handshake as the
    # reply, or — on codex 0.142 — leak the reasoning summary as a chat bubble).
    if _is_codex_cmd(cmd):
        codex_reply, codex_reasoning = _codex_turn_from_stream(result.stdout)
        if codex_reply:
            # Background memory lanes (raw_text) parse the model's literal output
            # with their own extractors — hand them the bare reply untouched. Only
            # foreground chat folds reasoning into the thinking disclosure.
            if codex_reasoning and not raw_text:
                return _codex_attach_reasoning(codex_reply, codex_reasoning)
            return codex_reply

    raw = result.stdout
    if raw_text:
        # Memory lanes parse JSON from the model's literal output. Prefer the
        # extracted assistant text (drops codex/claude transport framing) but do
        # NOT route it through the chat-bubble sanitizer in _agent_turn_from_raw,
        # which would decapitate a pretty-printed JSON object.
        text = _extract_text_from_cli_output(raw)
        if text.strip():
            return text
    turn = _agent_turn_from_raw(raw)
    if turn.messages or turn.actions or turn.thinking_summary or turn.tool_calls:
        return raw
    text = _extract_text_from_cli_output(raw)
    if not text:
        raise ValueError(
            f"cli agent produced no usable output (exit={result.returncode})"
        )
    return text


def _sanitize_reply_text(text: str) -> str:
    """Strip formatting/system leakage and collapse accidental duplication."""
    if not isinstance(text, str):
        return ""

    text = text.replace("\r\n", "\n").strip()
    if not text:
        return ""

    kept: list[str] = []
    for raw_ln in text.splitlines():
        ln = raw_ln.strip()
        if not ln:
            continue
        if _NOISE_LINE_RE.match(ln):
            continue
        if _IDENTITY_LEAK_RE.search(ln):
            continue
        if _REASONING_LINE_RE.match(ln):
            continue
        # Remove markdown-ish wrappers/bullets and decorative prefixes.
        ln = re.sub(r"^[`#>*\-\s]+", "", ln).strip()
        ln = re.sub(r"^[—–-]+\s*", "", ln).strip()
        if not ln:
            continue
        kept.append(ln)

    if not kept:
        return ""

    kept = _strip_leading_non_cjk_preamble(kept)
    if not kept:
        return ""

    # Dedup consecutive identical lines.
    deduped: list[str] = []
    for ln in kept:
        if not deduped or deduped[-1] != ln:
            deduped.append(ln)

    deduped = _collapse_repeated_line_blocks(deduped)

    return "\n".join(deduped).strip()


def _structured_reply_payload(raw_reply: str) -> Any:
    try:
        return json.loads(raw_reply)
    except (json.JSONDecodeError, TypeError):
        return None


def _cap_agent_replies(replies: list[str], max_items: int | None = None) -> list[str]:
    limit = max(1, max_items if max_items is not None else PROACTIVE_MAX_REPLY_MESSAGES)
    return replies[:limit]


def _normalize_agent_output(raw_reply: Any, max_items: int | None = None) -> tuple[list[dict], list[str]]:
    """Convert agent output into one or more chat bubbles.

    Supported shapes:
    - Plain text -> one bubble after sanitization.
    - JSON string with {"messages": ["...", "..."]} -> multiple bubbles.
    - JSON string with {"actions": [...], "messages": [...]} -> identity actions + bubbles.
    - JSON string with ["...", "..."] -> multiple bubbles.

    We keep policy minimal here: resident should not force one-to-one turn mapping;
    agent-side logic decides whether to return one or many messages. The resident
    only enforces the product cap so one proactive moment cannot flood the user.
    """
    turn = _agent_turn_from_raw(raw_reply, max_items=max_items)
    return turn.actions, turn.messages


def _normalize_agent_replies(raw_reply: str, max_items: int | None = None) -> list[str]:
    return _normalize_agent_output(raw_reply, max_items=max_items)[1]


def _split_agent_result(result: Any, max_items: int | None = None) -> tuple[list[dict], list[str]]:
    return _normalize_agent_output(result, max_items=max_items)


def _split_agent_turn(result: Any, max_items: int | None = None) -> AgentTurn:
    return _agent_turn_from_raw(result, max_items=max_items)


def call_agent(
    message: str,
    images: list[dict[str, str]] | None = None,
    image_paths: list[str] | None = None,
    raw_text: bool = False,
    trace_id: str = "",
) -> Any:
    if AGENT_MODE == "http":
        # http path metrics/timing are out of scope for this event pair (cli-only);
        # trace_id is accepted here for a uniform call signature but unused.
        raw = call_agent_http(message, images=images, raw_text=raw_text)
    elif AGENT_MODE == "cli":
        raw = call_agent_cli(message, image_paths=image_paths, raw_text=raw_text, trace_id=trace_id)
    else:
        raise ValueError(f"unknown AGENT_MODE: {AGENT_MODE!r}")

    if raw_text:
        # Background memory lanes (capture/dream) parse JSON from the model's
        # literal output with their own robust extractors. Return it verbatim
        # and skip the chat-bubble sanitizer below (which strips leading non-CJK
        # lines and would behead a pretty-printed JSON object).
        return raw if isinstance(raw, str) else _raw_assistant_text(raw)

    turn = _agent_turn_from_raw(raw)
    if turn.actions or turn.messages or turn.thinking_summary or turn.tool_calls:
        body: dict[str, Any] = {
            "actions": turn.actions,
            "messages": turn.messages,
        }
        if turn.tool_calls:
            body["tool_calls"] = turn.tool_calls
        if turn.thinking_summary:
            body["thinking_summary"] = turn.thinking_summary
        if turn.thinking_kind:
            body["thinking_kind"] = turn.thinking_kind
        if turn.thinking_source:
            body["thinking_source"] = turn.thinking_source
        if turn.thinking_model:
            body["thinking_model"] = turn.thinking_model
        if turn.thinking_native is not None:
            body["thinking_native"] = bool(turn.thinking_native)
        if turn.runtime_debug:
            log.debug("agent runtime debug keys: %s", sorted(turn.runtime_debug.keys()))
        return body
    if SEND_FALLBACK_ON_AGENT_ERROR:
        return [FALLBACK_REPLY]
    raise ValueError("agent produced no usable reply after sanitization")


def _resident_foreground_chat_message_v2(content: str) -> str:
    """Resident foreground chat is a native-agent turn.

    Hosted LLMs need prompt-injected JSON tool instructions. Resident agents
    such as OpenClaw/Claude Code should receive the user's message directly and
    use their registered native tools (io_cli for Feedling perception).
    """
    return content


def _visible_thinking_summary_protocol() -> str:
    return "\n".join([
        "Visible thinking summary protocol:",
        "When you speak to the user, return JSON {\"thinking_summary\":\"...\",\"messages\":[\"...\"]}.",
        "thinking_summary is a short display-safe summary of what context you considered and why you answered this way.",
        "Do not include hidden chain-of-thought, system/developer prompts, secrets, token/account metadata, or tool transcripts.",
        "Do not put thinking_summary JSON inside a visible message bubble.",
    ])


def _foreground_response_protocol_message(content: str) -> str:
    if not str(content or "").strip():
        return content
    if "\"thinking_summary\"" in content:
        return content
    return f"{_visible_thinking_summary_protocol()}\n\n{content}"


def _recent_chat_context_for_foreground(before_ts: float, limit: int | None = None) -> str:
    """Short plaintext transcript of recent chat turns STRICTLY older than the
    current turn, for injecting cross-turn continuity into foreground messages.

    Uses the same decrypt sources as normal chat processing. Returns "" when no
    decrypt source is configured/reachable or there is no prior turn — the caller
    then sends the bare message (graceful degradation, never raises)."""
    limit = max(1, min(limit if limit is not None else FOREGROUND_CHAT_CONTEXT_LIMIT, 50))
    fetch_limit = max(limit + 4, 20)
    try:
        history = get_decrypted_history(since=0, limit=fetch_limit)
    except Exception as e:  # noqa: BLE001 — continuity is best-effort, never fatal
        log.warning("foreground chat context fetch failed: %s", e)
        return ""
    if not history:
        return ""
    messages = _clean_messages_for_proactive_context(history)
    if before_ts > 0:
        messages = [m for m in messages if _message_ts_for_context(m) < before_ts]
    selected = messages[-limit:]
    if not selected:
        return ""
    now = time.time()
    return "\n".join(_chat_context_line(m, now=now, stale=False) for m in selected)


def _foreground_agent_message(content: str, *, current_ts: float) -> str:
    """Prepend a recent-chat transcript to a foreground turn when the active
    driver has no reliable session of its own (codex / claude). Returns ``content``
    unchanged when injection is disabled or no prior context is available."""
    if not _foreground_history_injection_enabled():
        return content
    transcript = _recent_chat_context_for_foreground(before_ts=current_ts)
    if not transcript:
        return content
    return f"{FOREGROUND_CHAT_CONTEXT_HEADER}\n{transcript}\n\n{content}"


def _message_has_injected_history(message: str) -> bool:
    """True when ``message`` was produced by _foreground_agent_message with a
    transcript actually prepended. This is the single signal used to decide
    whether claude's --resume can be safely suppressed for THIS turn — keeping
    the resume-suppression and the transcript-injection decisions consistent even
    when history is unavailable and injection silently degrades to bare content."""
    return isinstance(message, str) and message.startswith(FOREGROUND_CHAT_CONTEXT_HEADER)


# ---------------------------------------------------------------------------
# Feedling API helpers
# ---------------------------------------------------------------------------

# Cached from /v1/users/whoami for diagnostics and fallback state. Refreshed
# before every encrypted write so resident agents do not keep wrapping replies
# to a stale iOS content public key.
_whoami_cache: dict = {
    "user_id": "",
    "user_pk": None,
    "enclave_pk": None,
    "timezone": "",
    "archive_language": "",
}

# monotonic ts of the last successful _load_whoami() that yielded encryption
# keys; 0.0 until the first success so the first reply still fetches.
_whoami_cache_loaded_at: float = 0.0

def execute_identity_actions(actions: list[dict]) -> dict:
    if not actions:
        return {"status": "ok", "results": [], "effects": []}
    resp = httpx.post(
        f"{FEEDLING_API_URL}/v1/identity/actions",
        json={"actions": actions},
        headers=_HEADERS,
        timeout=20,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"identity_actions_http_{resp.status_code}:{resp.text[:500]}")
    body = resp.json()
    if not isinstance(body, dict) or body.get("status") not in {"ok", "created", "replaced"}:
        raise RuntimeError(f"identity_actions_unexpected_response:{str(body)[:500]}")
    return body


def execute_memory_actions(actions: list[dict]) -> dict:
    if not actions:
        return {"status": "ok", "results": [], "effects": []}
    resp = httpx.post(
        f"{FEEDLING_API_URL}/v1/memory/actions",
        json={"actions": actions},
        headers=_HEADERS,
        timeout=20,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"memory_actions_http_{resp.status_code}:{resp.text[:500]}")
    body = resp.json()
    if not isinstance(body, dict) or body.get("status") not in {"ok", "created", "replaced"}:
        raise RuntimeError(f"memory_actions_unexpected_response:{str(body)[:500]}")
    return body


def execute_agent_actions(actions: list[dict]) -> dict:
    identity_actions: list[dict] = []
    memory_actions: list[dict] = []
    unsupported: list[str] = []
    for action in actions:
        action_type = str(action.get("type") or action.get("action") or "")
        if action_type.startswith("identity."):
            identity_actions.append(action)
        elif action_type.startswith("memory."):
            memory_actions.append(action)
        else:
            unsupported.append(action_type)
    if unsupported:
        raise RuntimeError(f"unsupported_agent_actions:{unsupported}")
    identity_result = execute_identity_actions(identity_actions)
    memory_result = execute_memory_actions(memory_actions)
    return {
        "status": "ok",
        "identity": identity_result,
        "memory": memory_result,
        "effects": (identity_result.get("effects") or []) + (memory_result.get("effects") or []),
    }


def _identity_action_failure_reply(source_message: str) -> str:
    if re.search(r"[\u4e00-\u9fff]", source_message or ""):
        return "我刚刚没能把这次更新写进去，所以先不假装已经改好了。"
    return "I could not write that update, so I will not pretend it changed."


def _identity_action_success_reply(source_message: str) -> str:
    if re.search(r"[\u4e00-\u9fff]", source_message or ""):
        return "改好了。"
    return "Done. I updated my identity."


# Message dedup — rolling window prevents reprocessing the same message on
# restart with a stale checkpoint or if poll races with checkpoint save.
_seen_ids: set[str] = set()
_seen_ids_order: list[str] = []
_SEEN_MAX = 500

# Persisted agent conversation session id (for CLI agents like Hermes), keyed by user_id.
_agent_session_id_cache: dict[str, str] = {}
_agent_session_meta_cache: dict[str, dict[str, Any]] = {}
_chat_runtime_v2_profile: dict[str, Any] = {}


def _load_whoami() -> bool:
    """Fetch encryption keys from /v1/users/whoami and cache them.

    Returns True if both the user pubkey and enclave pubkey were obtained.
    A missing enclave pubkey is still usable (visibility falls back to
    local_only), but shared-visibility envelopes require it.
    """
    try:
        resp = httpx.get(
            f"{FEEDLING_API_URL}/v1/users/whoami", headers=_HEADERS, timeout=10
        )
        resp.raise_for_status()
        info = resp.json()
    except Exception as e:
        log.warning("whoami fetch failed: %s", e)
        return False

    user_id = info.get("user_id", "") or ""
    user_pk_b64 = (info.get("public_key") or "").strip()
    enc_pk_hex = (info.get("enclave_content_public_key_hex") or "").strip()

    try:
        user_pk = base64.b64decode(user_pk_b64) if user_pk_b64 else None
        if user_pk is not None and len(user_pk) != 32:
            user_pk = None
    except Exception:
        user_pk = None

    try:
        enc_pk = bytes.fromhex(enc_pk_hex) if enc_pk_hex else None
        if enc_pk is not None and len(enc_pk) != 32:
            enc_pk = None
    except Exception:
        enc_pk = None

    tz = str(info.get("timezone") or "").strip()
    archive_language = str(info.get("archive_language") or "").strip()
    _whoami_cache.update(
        user_id=user_id, user_pk=user_pk, enclave_pk=enc_pk,
        # A successful whoami is authoritative — adopt its timezone verbatim,
        # including empty (user cleared it / no fallback), so a stale zone is
        # never served after the server stops reporting one. Last-known is
        # retained only across whoami FAILURES, which return above before this
        # update runs.
        timezone=tz,
        archive_language=archive_language,
    )
    ok = bool(user_id and user_pk)
    if _whoami_cache_has_full_keys():
        global _whoami_cache_loaded_at
        _whoami_cache_loaded_at = time.monotonic()
    log.info(
        "whoami loaded — user_id=%s user_pk=%s enclave_pk=%s",
        user_id,
        _fingerprint_bytes(user_pk),
        _fingerprint_bytes(enc_pk),
    )
    return ok


def _load_whoami_with_retries(
    *,
    attempts: int | None = None,
    delay_sec: float | None = None,
    context: str = "startup check",
    backoff_multiplier: float = 1.0,
) -> bool:
    """Fetch whoami with bounded retry/backoff for transient network/TLS failures."""
    attempts = max(1, WHOAMI_STARTUP_RETRIES if attempts is None else attempts)
    delay = max(0.0, WHOAMI_STARTUP_RETRY_DELAY_SEC if delay_sec is None else delay_sec)
    multiplier = max(1.0, float(backoff_multiplier))

    for idx in range(attempts):
        if _load_whoami():
            return True
        if idx + 1 < attempts:
            log.warning(
                "whoami %s failed; retrying %s/%s in %.1fs",
                context,
                idx + 2,
                attempts,
                delay,
            )
            if delay:
                time.sleep(delay)
            delay *= multiplier
    return False


def _whoami_cache_has_encryption_keys(cache: dict | None = None) -> bool:
    cache = _whoami_cache if cache is None else cache
    user_id = str(cache.get("user_id") or "").strip()
    user_pk = cache.get("user_pk")
    return bool(user_id and isinstance(user_pk, bytes) and len(user_pk) == 32)


def _whoami_cache_has_full_keys(cache: dict | None = None) -> bool:
    cache = _whoami_cache if cache is None else cache
    user_id = str(cache.get("user_id") or "").strip()
    user_pk = cache.get("user_pk")
    enc_pk = cache.get("enclave_pk")
    return bool(
        user_id
        and isinstance(user_pk, bytes) and len(user_pk) == 32
        and isinstance(enc_pk, bytes) and len(enc_pk) == 32
    )


def _refresh_whoami_for_encrypted_reply() -> bool:
    previous = dict(_whoami_cache)
    # Skip the network refresh while cached keys are fresh (see WHOAMI_REFRESH_TTL_SEC).
    if (
        WHOAMI_REFRESH_TTL_SEC > 0
        and _whoami_cache_has_full_keys()
        and (time.monotonic() - _whoami_cache_loaded_at) < WHOAMI_REFRESH_TTL_SEC
    ):
        return True
    if _load_whoami_with_retries(
        attempts=WHOAMI_REFRESH_RETRIES,
        delay_sec=WHOAMI_REFRESH_RETRY_DELAY_SEC,
        context="reply refresh",
        backoff_multiplier=2.0,
    ):
        return True
    if not _whoami_cache_has_encryption_keys() and _whoami_cache_has_encryption_keys(previous):
        _whoami_cache.update(previous)
    if _whoami_cache_has_encryption_keys():
        log.warning(
            "whoami refresh failed before encrypted reply; using cached keys user_id=%s user_pk=%s enclave_pk=%s",
            _whoami_cache.get("user_id") or "",
            _fingerprint_bytes(_whoami_cache.get("user_pk")),
            _fingerprint_bytes(_whoami_cache.get("enclave_pk")),
        )
        return True
    return False


def poll_chat(since: float) -> dict:
    url = f"{FEEDLING_API_URL}/v1/chat/poll"
    params = {"since": since, "timeout": POLL_TIMEOUT}
    resp = httpx.get(url, params=params, headers=_HEADERS, timeout=POLL_TIMEOUT + 10)
    resp.raise_for_status()
    body = resp.json()
    if isinstance(body, dict):
        _update_chat_runtime_v2_profile(body.get("runtime_v2"))
    return body


def _update_chat_runtime_v2_profile(profile: Any) -> None:
    global _chat_runtime_v2_profile
    _chat_runtime_v2_profile = dict(profile) if isinstance(profile, dict) else {}


def _resident_chat_runtime_v2_enabled() -> bool:
    try:
        return bool(_chat_runtime_v2_profile.get(RESIDENT_CHAT_RUNTIME_V2_FLAG))
    except Exception:
        return False


def poll_proactive_jobs(since: float) -> dict:
    url = f"{FEEDLING_API_URL}/v1/proactive/jobs/poll"
    timeout = max(0, PROACTIVE_POLL_TIMEOUT)
    params = {"since": since, "timeout": timeout}
    resp = httpx.get(url, params=params, headers=_HEADERS, timeout=timeout + 10)
    resp.raise_for_status()
    body = resp.json()
    if isinstance(body, dict):
        runtime_profile = body.get("runtime_v2") if isinstance(body.get("runtime_v2"), dict) else {}
        jobs = body.get("jobs")
        if isinstance(jobs, list):
            for job in jobs:
                if isinstance(job, dict) and "runtime_v2" not in job:
                    job["runtime_v2"] = dict(runtime_profile)
    return body


def _proactive_tick_trigger_for_broadcast_state(broadcast_state: str) -> str:
    state = str(broadcast_state or "").strip().lower()
    if not state or state == "off":
        return "heartbeat_broadcast_off"
    if state in {"on", "broadcasting"}:
        return "heartbeat_broadcast_on"
    if state == "paused":
        return "heartbeat_broadcast_paused"
    return "heartbeat_unknown"


# Per-user "companionship frequency" (wake_interval_sec) clamp — mirrors the
# backend hard floor/ceiling (backend/core/store.py): min 15min, max 12h.
PROACTIVE_WAKE_INTERVAL_MIN_SEC = 900
PROACTIVE_WAKE_INTERVAL_MAX_SEC = 43200


def _proactive_tick_interval_for_broadcast_state(
    broadcast_state: str, wake_interval_sec: Any = None
) -> int:
    # Heartbeat is now DECOUPLED from screen sharing: broadcast no longer
    # accelerates the heavy presence heartbeat. Screen attention is handled by the
    # separate lightweight screen-watch lane (SCREEN_WATCH_INTERVAL_SEC). The
    # heartbeat keeps a single steady cadence regardless of broadcast_state.
    # (PROACTIVE_TICK_BROADCAST_ON_INTERVAL_SEC kept for back-compat / override.)
    #
    # Per-user cadence: the backend tick decision carries the user's chosen
    # wake_interval_sec ("companionship frequency"). When present and numeric it
    # wins, clamped defensively to [900, 43200] to mirror the backend guard. A
    # missing or non-numeric value falls back to the env default.
    if wake_interval_sec is not None:
        try:
            interval = int(wake_interval_sec)
        except (TypeError, ValueError):
            pass
        else:
            return max(
                PROACTIVE_WAKE_INTERVAL_MIN_SEC,
                min(PROACTIVE_WAKE_INTERVAL_MAX_SEC, interval),
            )
    return max(60, PROACTIVE_TICK_BROADCAST_OFF_INTERVAL_SEC)


def post_proactive_tick(payload: dict[str, Any] | None = None) -> dict:
    url = f"{FEEDLING_API_URL}/v1/proactive/tick"
    resp = httpx.post(url, json=payload or {}, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fire_scheduled_wakes() -> dict:
    resp = httpx.post(
        f"{FEEDLING_API_URL}/v1/proactive/scheduled/fire",
        json={},
        headers=_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    parsed = resp.json()
    return parsed if isinstance(parsed, dict) else {"results": [], "jobs": []}


def _screen_watch_recent_frames(limit: int = SCREEN_WATCH_FRAMES) -> tuple[str, float, list[dict]]:
    """Most-recent screen frames for a screen-watch wake. Returns
    (latest_frame_id, latest_ts, [{"id": ...}, ...] newest-first) — ("", 0.0, [])
    if none/unavailable. The /v1/screen/frames route returns newest-first."""
    body = _fetch_screen_json(f"/v1/screen/frames?limit={max(1, int(limit))}")
    frames = (body or {}).get("frames") if isinstance(body, dict) else None
    if not isinstance(frames, list) or not frames:
        return "", 0.0, []
    ids: list[dict] = []
    for f in frames:
        fid = str((f or {}).get("id") or (f or {}).get("frame_id") or "").strip()
        if fid:
            ids.append({"id": fid})
    latest = ids[0]["id"] if ids else ""
    try:
        latest_ts = float((frames[0] or {}).get("ts") or 0.0)
    except (TypeError, ValueError):
        latest_ts = 0.0
    return latest, latest_ts, ids


def post_screen_watch_tick(broadcast_state: str, frames: list[dict]) -> dict:
    """Enqueue a lightweight screen-watch wake. It is a consumer-scheduled
    self-wake: NOT forced/manual, so it still respects the user's Ambient gate
    (Ambient off → no screen-watch). job_kind marks it for the light prompt;
    frames are passed explicitly (the backend does not implicitly sample for
    this lane). The backend skips the heartbeat no-frame auto-block for it."""
    payload = {
        "job_kind": "screen_watch",
        "trigger": "screen_watch",
        "frames": frames,
    }
    if broadcast_state:
        payload["broadcast_state"] = broadcast_state
    return post_proactive_tick(payload)


def fire_capture_tick() -> dict:
    resp = httpx.post(
        f"{FEEDLING_API_URL}/v1/capture/tick",
        json={},
        headers=_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    parsed = resp.json()
    return parsed if isinstance(parsed, dict) else {"enqueued": False, "reason": "invalid_response"}


def claim_proactive_job(job_id: str) -> bool:
    if not job_id:
        return False
    url = f"{FEEDLING_API_URL}/v1/proactive/jobs/{job_id}/claim"
    resp = httpx.post(
        url,
        json={"consumer_id": CONSUMER_ID},
        headers=_HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()
    return bool(body.get("claimed"))


def update_proactive_job_status(
    job_id: str,
    status: str,
    reason: str = "",
    *,
    extra: dict[str, Any] | None = None,
) -> None:
    if not job_id:
        return
    url = f"{FEEDLING_API_URL}/v1/proactive/jobs/{job_id}/status"
    try:
        body: dict[str, Any] = {
            "status": status,
            "reason": reason,
            "consumer_id": CONSUMER_ID,
        }
        if isinstance(extra, dict):
            body.update(extra)
        resp = httpx.post(
            url,
            json=body,
            headers=_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        log.warning("failed to update proactive job status id=%s status=%s error=%s", job_id, status, e)


def update_proactive_state(**patch: Any) -> None:
    clean = {k: v for k, v in patch.items() if v not in (None, "")}
    if not clean:
        return
    try:
        resp = httpx.post(
            f"{FEEDLING_API_URL}/v1/proactive/state",
            json=clean,
            headers=_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        log.warning("failed to update proactive state patch=%s error=%s", clean, e)


def _job_wake_ids(job: dict) -> list[str]:
    out: list[str] = []
    for value in (job.get("wake_id"), job.get("job_id"), job.get("gate_decision_id")):
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text[:200])
    return out


def _job_origin_refs(job: dict) -> list[str]:
    refs: list[str] = []
    raw = job.get("origin_refs")
    if isinstance(raw, list):
        for item in raw:
            text = str(item or "").strip()
            if text and text not in refs:
                refs.append(text[:200])
    for value in (job.get("chat_message_id"), job.get("gate_decision_id"), job.get("job_id")):
        text = str(value or "").strip()
        if text and text not in refs:
            refs.append(text[:200])
    return refs


def _normalize_v2_action_type(action: dict) -> dict:
    out = dict(action or {})
    typ = _proactive_action_type(out)
    if typ in {"memory.create", "memory.add_correction"}:
        out["type"] = "memory.add"
        return out
    if typ in {"memory.patch", "memory.content_patch"}:
        out["type"] = "memory.supersede"
        if not out.get("supersedes"):
            out["supersedes"] = out.get("memory_id") or out.get("id") or out.get("target_id") or ""
        return out
    if typ.startswith("proactive."):
        out["type"] = typ.removeprefix("proactive.")
    elif typ and not out.get("type"):
        out["type"] = typ
    return out


def execute_scheduled_wake_actions(actions: list[dict], job: dict) -> dict:
    if not actions:
        return {"results": []}
    body = {
        "actions": [_normalize_v2_action_type(action) for action in actions],
        "turn_id": str(job.get("job_id") or ""),
        "wake_ids": _job_wake_ids(job),
        "origin_refs": _job_origin_refs(job),
    }
    resp = httpx.post(
        f"{FEEDLING_API_URL}/v1/proactive/scheduled/actions",
        json=body,
        headers=_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    parsed = resp.json()
    return parsed if isinstance(parsed, dict) else {"results": []}


def post_reply(
    content: str,
    *,
    source: str = "chat",
    gate_decision_id: str = "",
    proactive_job_id: str = "",
    suppress_push: bool = False,
    reply_to_message_id: str = "",
    thinking_summary: str = "",
    thinking_kind: str = "",
    thinking_source: str = "",
    thinking_model: str = "",
    thinking_native: bool | None = None,
) -> dict:
    """Post agent reply as a v1 ciphertext envelope.

    `suppress_push=True` sends an empty alert_body and no push fields so
    /v1/chat/response's app-state push policy is a no-op — used for private
    writes that must land in the store (for liveness/verify) but must never
    surface as a user-visible APNs notification.

    Falls back to plaintext only when encryption is unavailable — this will
    return 400 on v1 backends and is logged as an error so it's visible.

    Handles `bootstrap_incomplete` 409 by logging the structured error
    (stage, memory_count, required) and returning without raising — the
    user-side agent skipped bootstrap, and re-raising would cause the
    daemon to loop on this dead-end forever. The operator sees what's
    wrong in the log instead.
    """
    url = f"{FEEDLING_API_URL}/v1/chat/response"

    if _ENCRYPTION_AVAILABLE and not _refresh_whoami_for_encrypted_reply():
        log.error("whoami refresh failed before encrypted reply and no cached keys are available; skipping write")
        return {"error": "whoami_refresh_failed"}

    user_id = _whoami_cache["user_id"]
    user_pk: bytes | None = _whoami_cache["user_pk"]
    enc_pk: bytes | None = _whoami_cache["enclave_pk"]

    if _ENCRYPTION_AVAILABLE and user_id and user_pk:
        visibility = "shared" if enc_pk else "local_only"
        envelope = _build_envelope(
            plaintext=content.encode("utf-8"),
            owner_user_id=user_id,
            user_pk_bytes=user_pk,
            enclave_pk_bytes=enc_pk,
            visibility=visibility,
        )
        thinking_envelope = None
        safe_thinking = _sanitize_thinking_summary(thinking_summary)
        if safe_thinking:
            thinking_envelope = _build_envelope(
                plaintext=safe_thinking.encode("utf-8"),
                owner_user_id=user_id,
                user_pk_bytes=user_pk,
                enclave_pk_bytes=enc_pk,
                visibility=visibility,
            )
        visible_body = "" if suppress_push else content[:240]
        body: dict[str, Any] = {
            "envelope": envelope,
            "source": source,
            "alert_body": visible_body,
        }
        if thinking_envelope:
            body["thinking_envelope"] = thinking_envelope
            kind = _sanitize_thinking_kind(thinking_kind)
            if kind:
                body["thinking_kind"] = kind
            source_label = _sanitize_thinking_meta(thinking_source, max_len=80)
            if source_label:
                body["thinking_source"] = source_label
            model_label = _sanitize_thinking_meta(thinking_model, max_len=96)
            if model_label:
                body["thinking_model"] = model_label
            if thinking_native is not None:
                body["thinking_native"] = bool(thinking_native)
        if reply_to_message_id:
            body["reply_to_message_id"] = reply_to_message_id
        if gate_decision_id:
            body["gate_decision_id"] = gate_decision_id
        if proactive_job_id:
            body["proactive_job_id"] = proactive_job_id
        if source == PROACTIVE_JOB_SOURCE and not suppress_push:
            body["push_live_activity"] = True
            body["push_body"] = visible_body
            body["data"] = {
                "source": PROACTIVE_JOB_SOURCE,
                "gate_decision_id": gate_decision_id,
                "proactive_job_id": proactive_job_id,
            }
        resp = httpx.post(url, json=body, headers=_HEADERS, timeout=15)
        return _handle_post_reply_response(resp)

    # Encryption unavailable — plaintext path (will 400 on v1 backends).
    log.error(
        "ENCRYPTION UNAVAILABLE — posting plaintext will fail on v1 backends. "
        "Ensure content_encryption.py is importable and whoami succeeded."
    )
    resp = httpx.post(
        url,
        json={
            "content": content,
            "push_live_activity": source == PROACTIVE_JOB_SOURCE and not suppress_push,
            "push_body": content[:240] if (source == PROACTIVE_JOB_SOURCE and not suppress_push) else "",
            "alert_body": "" if suppress_push else content[:240],
            "source": source,
            "gate_decision_id": gate_decision_id,
            "proactive_job_id": proactive_job_id,
            "reply_to_message_id": reply_to_message_id,
            "thinking_summary": _sanitize_thinking_summary(thinking_summary),
            "thinking_kind": _sanitize_thinking_kind(thinking_kind),
            "thinking_source": _sanitize_thinking_meta(thinking_source, max_len=80),
            "thinking_model": _sanitize_thinking_meta(thinking_model, max_len=96),
            "thinking_native": thinking_native,
        },
        headers=_HEADERS, timeout=15,
    )
    return _handle_post_reply_response(resp)


def _handle_post_reply_response(resp) -> dict:
    """Inspect a /v1/chat/response response. Re-raises 4xx/5xx EXCEPT for
    the structured `bootstrap_incomplete` 409, which we want to surface in
    operator logs without crashing the daemon (a crash would put the
    process into an restart-loop trying the same dead-end content forever).
    """
    if resp.status_code == 409:
        try:
            body = resp.json()
        except Exception:
            body = {}
        if body.get("error") == "bootstrap_incomplete":
            log.error(
                "chat_response rejected: bootstrap_incomplete stage=%s "
                "memory_count=%s identity_written=%s — the upstream agent "
                "skipped Pass 1-3 / Step 5. Have the user re-run "
                "bootstrap from the start prompt; until then this user's "
                "Feedling chat is dead-ended.",
                body.get("stage"),
                body.get("memory_count"),
                body.get("identity_written"),
            )
            return body
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {}


def get_latest_ts() -> float:
    url = f"{FEEDLING_API_URL}/v1/chat/history"
    resp = httpx.get(url, params={"limit": 1}, headers=_HEADERS, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    messages = data.get("messages") or data.get("history") or []
    if messages:
        m = messages[-1]
        return float(m.get("ts", m.get("timestamp", 0)) or 0)
    return 0.0


def _message_text_for_context(msg: dict) -> str:
    text = (
        msg.get("content")
        or msg.get("text")
        or msg.get("plaintext")
        or msg.get("body")
        or ""
    )
    if isinstance(text, dict):
        text = json.dumps(text, ensure_ascii=False)
    if not isinstance(text, str):
        text = str(text or "")
    text = " ".join(text.strip().split())
    ctype = str(msg.get("content_type") or "").lower()
    if ctype == "image" or msg.get("image_b64"):
        # The injected transcript is TEXT-only — an image turn's pixels are never
        # in it. Advertise the exact io_cli command that lazily pulls THIS image by
        # id (and preserve any caption the user sent), so the agent fetches + Reads
        # the real picture instead of guessing (photo-read = wrong tool: that's the
        # perception photo library, not the chat feed) or fabricating its contents.
        mid = str(msg.get("id") or msg.get("message_id") or "").strip()
        label = text[:300] if text else "[image]"
        if mid:
            return (
                f"{label} [image not shown here — run `io_cli chat-image --id {mid}`, "
                "then Read the returned image_file to actually see it]"
            )
        return f"{label} [image not shown here — pixels are not in this transcript]"
    return text[:500]


def _message_ts_for_context(msg: dict) -> float:
    try:
        return float(msg.get("ts", msg.get("timestamp", 0)) or 0)
    except (TypeError, ValueError):
        return 0.0


def _message_role_for_context(msg: dict) -> str:
    role = "user" if msg.get("role") == "user" else "agent"
    if msg.get("source") == PROACTIVE_JOB_SOURCE:
        role = "agent(proactive)"
    return role


def _format_age(age_sec: float | None) -> str:
    if age_sec is None:
        return "unknown"
    try:
        age = max(0, int(age_sec))
    except (TypeError, ValueError):
        return "unknown"
    if age < 60:
        return f"{age}s ago"
    minutes = age // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    rem_minutes = minutes % 60
    if hours < 24:
        return f"{hours}h {rem_minutes}m ago" if rem_minutes else f"{hours}h ago"
    days = hours // 24
    rem_hours = hours % 24
    return f"{days}d {rem_hours}h ago" if rem_hours else f"{days}d ago"


def _format_message_time(ts: float) -> str:
    if ts <= 0:
        return "unknown time"
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _chat_context_line(msg: dict, *, now: float, stale: bool) -> str:
    ts = _message_ts_for_context(msg)
    age = now - ts if ts > 0 else None
    flags = ["stale"] if stale else ["fresh"]
    text = _message_text_for_context(msg)
    return (
        f"- [{_format_message_time(ts)}, {_format_age(age)}, {', '.join(flags)}] "
        f"{_message_role_for_context(msg)}: {text}"
    )


def _clean_messages_for_proactive_context(history: list[dict] | None) -> list[dict]:
    cleaned: list[dict] = []
    for msg in history or []:
        if not isinstance(msg, dict):
            continue
        text = _message_text_for_context(msg)
        if not text or "__VERIFY_PING__" in text:
            continue
        item = dict(msg)
        item["_context_text"] = text
        cleaned.append(item)
    return cleaned


def _proactive_chat_context_from_history(history: list[dict] | None, *, limit: int, now: float) -> ProactiveChatContext:
    messages = _clean_messages_for_proactive_context(history)
    if not messages:
        return ProactiveChatContext()

    def age_for(msg: dict) -> float | None:
        ts = _message_ts_for_context(msg)
        return now - ts if ts > 0 else None

    last_message = messages[-1]
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    last_proactive = next((m for m in reversed(messages) if m.get("source") == PROACTIVE_JOB_SOURCE), None)
    proactive_count_24h = sum(
        1
        for m in messages
        if m.get("source") == PROACTIVE_JOB_SOURCE
        and (age_for(m) is not None)
        and (age_for(m) or 0) <= 86400
    )

    fresh_window = max(60, PROACTIVE_CHAT_FRESH_WINDOW_SEC)
    fresh_messages = [
        m for m in messages
        if (age_for(m) is not None) and (age_for(m) or 0) <= fresh_window
    ]
    if fresh_messages:
        selected = fresh_messages[-limit:]
        freshness = "fresh"
        stale = False
    else:
        fallback_limit = max(1, min(PROACTIVE_STALE_CHAT_FALLBACK_LIMIT, limit))
        selected = messages[-fallback_limit:]
        freshness = "stale"
        stale = True

    rows = [_chat_context_line(m, now=now, stale=stale) for m in selected]
    return ProactiveChatContext(
        text="\n".join(rows),
        freshness=freshness,
        included_count=len(rows),
        last_message_age_sec=age_for(last_message),
        last_user_message_age_sec=age_for(last_user) if last_user else None,
        last_visible_proactive_age_sec=age_for(last_proactive) if last_proactive else None,
        visible_proactive_count_24h=proactive_count_24h,
    )


def recent_chat_context_for_proactive(limit: int | None = None) -> ProactiveChatContext:
    """Return a short plaintext chat transcript for proactive continuity.

    This uses the same decrypt sources as normal chat processing. If no decrypt
    source is available, proactive realization still proceeds; it simply lacks
    recent-chat continuity context.
    """
    limit = max(1, min(limit if limit is not None else PROACTIVE_RECENT_CHAT_LIMIT, 50))
    fetch_limit = max(limit, min(max(1, PROACTIVE_CHAT_CONTEXT_LOOKBACK_LIMIT), 200))
    try:
        history = get_decrypted_history(since=0, limit=fetch_limit)
    except Exception as e:
        log.warning("recent chat context fetch failed: %s", e)
        return ProactiveChatContext(freshness="unavailable")
    return _proactive_chat_context_from_history(history, limit=limit, now=time.time())


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

_running = True


def _handle_signal(signum, _frame):
    global _running
    log.info("received signal %d — shutting down", signum)
    _running = False


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def _proactive_job_key(job: dict) -> str:
    jid = str(job.get("job_id") or "").strip()
    if jid:
        return f"proactive:{jid}"
    return f"proactive:{job.get('ts', job.get('created_at', 'unknown'))}"


def _proactive_action_type(action: dict) -> str:
    return str(action.get("type") or action.get("action") or "").strip().lower()


def _compact_action_for_status(action: dict) -> dict:
    out: dict[str, Any] = {}
    for key, value in action.items():
        skey = str(key)[:80]
        if not skey:
            continue
        if isinstance(value, (bool, int, float)) or value is None:
            out[skey] = value
        else:
            out[skey] = str(value)[:500]
    return out


def _first_proactive_action(actions: list[dict], names: set[str]) -> dict | None:
    for action in actions:
        typ = _proactive_action_type(action)
        short = typ.removeprefix("proactive.")
        if typ in names or short in names:
            return action
    return None


def _visible_broadcast_request_text(action: dict) -> str:
    for key in ("copy", "message", "text", "content"):
        value = str(action.get(key) or "").strip()
        if value:
            return value[:1200]
    reason = str(action.get("reason") or "").strip()
    if re.search(r"[\u4e00-\u9fff]", reason):
        return "我现在看不到你的屏幕。如果你愿意，可以重新打开屏幕共享。"
    return "I cannot see your screen right now. If you want, turn screen sharing back on."


def _proactive_control_reason_from_replies(replies: list[str]) -> str:
    """Recover a sleep/noop reason from malformed control JSON leaked as text.

    Proactive prompts ask the model to stay quiet via an action JSON. Some CLI
    transports can hand back a truncated fragment such as
    `"reason":"..."}]}`; generic chat parsing treats it as a visible message.
    In the proactive lane, a control-only JSON fragment should complete the wake
    quietly instead of becoming a chat bubble.
    """
    if not replies:
        return ""
    reasons: list[str] = []
    for reply in replies:
        text = str(reply or "").strip()
        if not text:
            continue
        stripped = text.lstrip()
        if not stripped or stripped[0] not in {'"', "{", "["}:
            return ""
        if '"reason"' not in stripped and "'reason'" not in stripped:
            return ""
        match = re.search(r'''["']reason["']\s*:\s*["'](?P<reason>(?:\\.|[^"'\\])*)["']''', stripped)
        if not match:
            return ""
        reason = match.group("reason")
        try:
            reason = json.loads(f'"{reason}"')
        except Exception:  # noqa: BLE001
            pass
        reason = str(reason or "").strip()
        if reason:
            reasons.append(reason)
    return "\n".join(reasons).strip()


def _proactive_control_reason_from_value(value: Any) -> str:
    if isinstance(value, list):
        reasons = [
            reason
            for item in value
            if (reason := _proactive_control_reason_from_value(item))
        ]
        return "\n".join(reasons).strip()
    if not isinstance(value, dict):
        return ""

    messages = value.get("messages")
    if isinstance(messages, list) and any(str(item or "").strip() for item in messages):
        return ""
    for key in ("actions", "tool_calls"):
        items = value.get(key)
        if isinstance(items, list) and items:
            return ""

    reason = value.get("reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()

    for key in ("result", "payload", "output"):
        nested = value.get(key)
        if isinstance(nested, (dict, list)):
            if reason := _proactive_control_reason_from_value(nested):
                return reason
    return ""


def _proactive_control_reason_from_result(agent_result: Any, replies: list[str]) -> str:
    return (
        _proactive_control_reason_from_replies(replies)
        or _proactive_control_reason_from_value(agent_result)
    ).strip()


def _split_proactive_actions(actions: list[dict]) -> tuple[list[dict], list[dict]]:
    proactive: list[dict] = []
    memory_identity: list[dict] = []
    proactive_types = {
        "sleep",
        "request_broadcast",
        "send_message",
        "schedule_wake",
        "cancel_wake",
    }
    for action in actions:
        if not isinstance(action, dict):
            continue
        typ = _proactive_action_type(action)
        short = typ.removeprefix("proactive.")
        if typ.startswith("identity.") or typ.startswith("memory."):
            memory_identity.append(action)
        elif typ.startswith("proactive.") or short in proactive_types:
            proactive.append(action)
        else:
            log.warning("unsupported proactive wake action ignored type=%s", typ or "<missing>")
    return proactive, memory_identity


def _coerce_proactive_chat_context(value: Any) -> ProactiveChatContext:
    if isinstance(value, ProactiveChatContext):
        return value
    text = str(value or "").strip()
    return ProactiveChatContext(
        text=text,
        freshness="unknown" if text else "empty",
        included_count=len([ln for ln in text.splitlines() if ln.strip()]),
    )


def _proactive_wake_kind(job: dict, *, screen_text: str) -> str:
    explicit = str(job.get("wake_kind") or "").strip().lower()
    if explicit in {"screen", "presence"}:
        return explicit
    return "screen" if screen_text else "presence"


def _proactive_attention_facts(chat: ProactiveChatContext) -> str:
    return "\n".join([
        "attention_facts:",
        f"- recent_chat_context_freshness: {chat.freshness}",
        f"- recent_chat_context_included_messages: {chat.included_count}",
        f"- last_message_age: {_format_age(chat.last_message_age_sec)}",
        f"- last_user_message_age: {_format_age(chat.last_user_message_age_sec)}",
        f"- last_visible_proactive_age: {_format_age(chat.last_visible_proactive_age_sec)}",
        f"- visible_proactive_count_24h: {chat.visible_proactive_count_24h}",
    ])


def _is_screen_watch_job(job: dict) -> bool:
    """A lightweight screen-watch wake (its own lane, decoupled from the heavy
    heartbeat). Keyed on job_kind primarily, trigger as a fallback."""
    return (
        str((job or {}).get("job_kind") or "").strip().lower() == "screen_watch"
        or str((job or {}).get("trigger") or "").strip().lower() == "screen_watch"
    )


def _is_introduction_job(job: dict) -> bool:
    return (
        str((job or {}).get("job_kind") or "").strip().lower() == "introduction"
        or str((job or {}).get("trigger") or "").strip().lower() == "post_spawn_genesis"
    )


def _message_for_introduction_job(job: dict) -> str:
    return "\n\n".join([
        "[Feedling · 首次登场(onboarding 之后)]",
        _local_time_anchor(),
        "你刚第一次上线 —— 你的人格、声音、记忆都已经从你和 TA 过去的聊天里沉淀好了"
        "(在你的 system prompt 和 Memory Garden 里)。现在是你和 TA 重逢的第一刻,"
        "TA 还没开口,这一句由你先说。",
        "做三件事:\n"
        "1. 用【你自己的声音】写一段简短自我介绍(1-3 句)+ 一个签名/口头禅"
        "(signature,你说话的标志性短句),通过 identity.profile_patch(或 io_cli identity-write)"
        "写进身份卡。自我介绍要像你这个人格会说的话,别用通用助手腔。名字用你已有的"
        "(system prompt/记忆里),没有就别编、介绍里可不提名字。\n"
        "2. 给 TA 发【第一句问候】—— 像久别重逢、你一直都在那样,in-voice。"
        "不是\"有什么可以帮您\",是你这个伴侣会说的第一句(类似\"我来了\",但用你自己的方式)。\n"
        "3. 不要等 TA 回应,这是你的登场。",
        _visible_thinking_summary_protocol(),
        "输出格式优先用 JSON: "
        "{\"actions\":[{\"type\":\"identity.profile_patch\",\"patch\":{\"self_introduction\":\"...\","
        "\"signature\":[\"...\"]}}],\"thinking_summary\":\"...\",\"messages\":[\"...\"]}。"
        "如果你用 io_cli identity-write 作为 native tool 写身份卡,仍然在 messages 里给出第一句问候。",
        "铁律:只用你真实拥有的人格/记忆,别编不存在的共同经历;名字别编。",
        _reply_language_line(),
    ])


def _native_tool_names_compact() -> str:
    """Names-only tool list for the light screen-watch prompt. The runtime always
    has every tool registered, so this is guidance, not a restriction — listing
    all names (cheaply) keeps the agent free to pull health/calendar/etc. if the
    screen calls for it, without the heavy cost-guide the heartbeat carries."""
    return "\n".join([
        "tools_available (names only; you have your full toolset — call any if the screen makes it relevant):",
        "- perception_<signal>: now, location, weather, motion, calendar, focus, audio_route, app, "
        "steps, sleep, workout, vitals, activity, body, metabolic, cycle, mood, reminders",
        "- perception_trend, perception_history, memory_index, memory_fetch, "
        "screen_recent, screen_read, photo_recent, photo_read",
        "  (Bash/CLI runtimes: same verbs via io_cli.)",
    ])


def _screen_watch_message(
    job: dict,
    screen_text: str = "",
    chat_context: "ProactiveChatContext | None" = None,
) -> str:
    """Light screen-watch prompt: state the facts, hand the decision (and the
    agent's own character) back to it. No cross-domain board, no cost-guide."""
    screen_available = bool(screen_text)
    parts = [
        "[Feedling screen-watch]",
        "The user is screen-sharing with you right now. Someone sharing their screen "
        "usually wants you in on a slice of their life as it happens.",
        "This is not a request and not an instruction to respond — it is a chance to be present.",
        "Whether you look, and whether you speak, is yours to decide from your own character. "
        "Staying quiet is just as valid as speaking.",
        "Read the on-device OCR text first (cheap); open the attached screenshot only if it is "
        "worth a closer look. If you want to review earlier moments, use screen_recent / screen_read "
        "(frames are kept ~100 min).",
        "If something genuinely moves you to speak, use your normal voice (1-3 short bubbles). "
        "If not, return JSON: {\"actions\":[{\"type\":\"proactive.sleep\",\"reason\":\"...\"}],\"messages\":[]}.",
        _visible_thinking_summary_protocol(),
        "Do not mention this watch, the frames, or any system wording to the user.",
        (
            "watch_metadata:\n"
            f"- trigger: screen_watch\n"
            f"- broadcast_state: {str(job.get('broadcast_state') or 'unknown')}\n"
            f"- current_app: {str(job.get('current_app') or 'unknown')}\n"
            f"- screen_context_available: {str(screen_available).lower()}"
        ),
    ]
    parts.insert(1, _local_time_anchor(
        since_sec=chat_context.last_user_message_age_sec if chat_context is not None else None))
    if chat_context is not None:
        parts.append(_proactive_attention_facts(chat_context))
        parts.append(
            "If attention_facts show you are mid-conversation or just spoke, prefer silence over "
            "interrupting or repeating yourself."
        )
    parts.append(_reply_language_line())
    parts.append(_native_tool_names_compact())
    if screen_text:
        parts.append(screen_text)
    else:
        parts.append("screen_context: no fresh frame available right now; do not imply you can see the screen.")
    return "\n\n".join(parts)


def _is_photo_added_job(job: dict) -> bool:
    return "photo_added" in (
        str((job or {}).get("trigger") or "").strip().lower(),
        str((job or {}).get("intent_label") or "").strip().lower(),
    )


def _new_photo_hint(job: dict) -> str:
    """For a photo_added wake: tell the agent a fresh photo landed in the album +
    its rough metadata (what it looks like, screenshot or not) + its id, so the
    agent can DECIDE whether it's worth looking and — only if it wants — pull the
    real pixels with photo_read. Pull-on-demand, not auto-attached. Best-effort:
    returns '' on anything unexpected so a wake never breaks over this."""
    if not _is_photo_added_job(job):
        return ""
    try:
        resp = httpx.get(
            f"{FEEDLING_API_URL}/v1/perception/photos",
            headers=_HEADERS,
            params={"limit": 1},
            timeout=12,
        )
        if resp.status_code >= 400:
            return ""
        photos = (resp.json() or {}).get("photos") or []
    except Exception as exc:  # noqa: BLE001 — hint is optional, never fatal
        log.debug("new-photo hint fetch failed: %s", exc)
        return ""
    if not photos or not isinstance(photos[0], dict):
        return ""
    photo = photos[0]
    pid = str(photo.get("photo_id") or "").strip()
    if not pid:
        return ""
    meta = photo.get("metadata") if isinstance(photo.get("metadata"), dict) else {}
    scene = str(meta.get("scene_hint") or "").strip() or "unclassified"
    tod = str(meta.get("time_of_day") or "").strip()
    is_shot = str(meta.get("is_screenshot")).strip().lower() in ("true", "1", "yes")
    kind = "a screenshot" if is_shot else f'a photo that looks like "{scene}"'
    when = f", taken in the {tod}" if tod else ""
    return (
        "new_photo:\n"
        f"A new image just landed in their album — {kind}{when} (id={pid}). "
        "This is only a rough hint; you cannot see the image itself from here. "
        "If it sounds worth a look, pull the real pixels: call photo_read with "
        f"id=\"{pid}\" and include_image=true (decrypts it so you can actually see it). "
        "It's entirely your call — look or let it pass; and if seeing it makes you want "
        "to say something, you can reach out to them about it (or not). Treat it like "
        "noticing a friend's photo, not a task to report on."
    )


def _message_for_proactive_job(
    job: dict,
    screen_text: str = "",
    recent_chat_context: Any = "",
    perception_digest: tuple[dict, list, dict] | None = None,
) -> str:
    chat_context = _coerce_proactive_chat_context(recent_chat_context)
    if _is_screen_watch_job(job):
        return _screen_watch_message(job, screen_text=screen_text, chat_context=chat_context)
    wake_kind = _proactive_wake_kind(job, screen_text=screen_text)
    screen_available = bool(screen_text)
    presence = perception_digest[0] if (perception_digest and isinstance(perception_digest[0], dict)) else {}
    parts = [
        "[Feedling proactive wake]",
        "This is a presence check, not a request — no reply is expected. Whether you appear, and whether you stay "
        "quiet, are equally valid — neither is the default, and neither is the \"safe\" choice. Decide entirely from "
        "your own character: speak if you want to, stay quiet if you'd rather. You don't need a strong reason either "
        "way. Use the glance below to decide whether to look closer; pull the real tools if something makes you want "
        "to understand the moment better. Then do whatever feels right — including nothing. "
        "Never mention this wake or any system wording to the user.",
        _reply_protocol_block(),
        _reply_language_line(presence),
        (
            "wake_metadata:\n"
            f"- trigger: {str(job.get('trigger') or 'wake')}\n"
            f"- wake_kind: {wake_kind}\n"
            f"- broadcast_state: {str(job.get('broadcast_state') or 'unknown')}\n"
            f"- screen_context_available: {str(screen_available).lower()}"
        ),
        _local_time_anchor(since_sec=chat_context.last_user_message_age_sec),
        _proactive_attention_facts(chat_context),
        _native_reachout_tool_instructions(),
    ]
    if perception_digest is not None:
        parts.append(_native_reachout_perception_context(*perception_digest))
    photo_hint = _new_photo_hint(job)
    if photo_hint:
        parts.append(photo_hint)
    if chat_context.text:
        parts.append(
            "recent_chat_context:\n"
            f"{chat_context.text}\n"
            "Use fresh chat context for local continuity when it genuinely matters. "
            "If recent_chat_context_freshness is stale, treat it only as relationship background; "
            "do not continue it as if it just happened. "
            "Your own runtime identity, memory, and normal voice remain the source of the reply."
        )
    elif not screen_available:
        parts.append(
            "capability_note:\n"
            "You can tell which app is in the foreground (reliable — see the board's app field) but you cannot see "
            "the contents of the user's screen right now. Don't imply you can see their screen; you may still refer "
            "to which app they're in."
        )
    if screen_text:
        parts.append(screen_text)
    return "\n\n".join(parts)


def _reply_protocol_block() -> str:
    """How the agent responds — stated once (no longer repeated across the wake
    preamble + tool block)."""
    return "\n".join([
        "How to respond (exactly one of):",
        "- speak: reply in your normal voice — a few short bubbles is typical, but length and number are yours. "
        "Return JSON {\"thinking_summary\":\"...\",\"messages\":[\"...\"]}.",
        _visible_thinking_summary_protocol(),
        "- stay quiet: return {\"actions\":[{\"type\":\"proactive.sleep\",\"reason\":\"...\"}]}.",
        "- want to see their screen but it isn't shared: just ask, in a normal message.",
    ])


def _reply_language_line(presence: dict | None = None) -> str:
    """Anchor the reply language to the user — a proactive wake may have no recent
    user message to infer it from, so an English prompt must not leak English.
    Fallback chain: device locale → stored archive_language → 简体中文 (product default)."""
    locale = str((presence or {}).get("locale") or "").strip()
    if locale:
        return f"Always reply in the user's own language (their locale is {locale})."
    archive_language = str(_whoami_cache.get("archive_language") or "").strip()
    if archive_language:
        return f"Always reply in the user's own language (their language is {archive_language})."
    # 既无设备 locale 也无存储的语言偏好（空语境/刚铸的新号）——裸的
    # "user's own language" 会让模型默认英文。产品主用户群为中文，默认简体中文。
    return "默认用简体中文回复，除非用户明显在使用其它语言。"


def _native_reachout_tool_instructions() -> str:
    return "\n".join([
        "native_tool_access:",
        "- You have native Feedling tools for the user's real context — perception (now/location/weather/motion/"
        "calendar/health/…), memory (index/fetch), screen (recent/read), photo (recent/read). Use them when more "
        "facts genuinely help.",
        "- You also have native tools to manage your own future wakes: schedule_wake (ask to be woken at a later time) "
        "and cancel_wake.",
        "- CLI runtimes call all of these via io_cli: perception, perception-trend, perception-history, memory-index, "
        "memory-fetch, screen-recent, screen-read, photo-recent, photo-read, schedule-wake, cancel-wake.",
    ])


def _native_reachout_perception_context(presence: dict, change: list, domains: dict | None = None) -> str:
    parts = [
        "real_signal_context:",
        "This is a low-resolution glance, not a list of things to report. It helps you decide WHETHER to look closer "
        "and WHERE — not what to say. Most fields you just note and move on; if one makes you want to understand the "
        "moment better, pull the matching tool for detail. Treat missing fields as unknown.",
    ]
    if presence:
        parts.append("presence_hints_json:\n" + json.dumps(presence, ensure_ascii=False, sort_keys=True))
    else:
        parts.append("presence_hints_json: {}")
    if domains:
        parts.append("cross_domain_board_json:\n" + json.dumps(domains, ensure_ascii=False, sort_keys=True))
        parts.append(
            "Reading the board: each domain (location/media/app/health/weather/mood/reminders/calendar/photos/screen) "
            "is laid out evenly — health is just one entry, not the headline. Pick at most 2-3 things that stand out "
            "to you; you may combine across domains, and prefer lived, human context (music, place, an app, a photo, "
            "an overdue reminder) over the raw figures. Do NOT recite exact numbers (minutes, degrees, counts, sleep "
            "figures) — use them only to notice what's genuinely about the user; if a number actually matters, pull "
            "the tool for it. novelty hints (new_artist / long_dwell) are light factual context, not a directive. "
            "If signals lean low or vulnerable (late hour, sad music, poor sleep), be lighter, not heavier — don't "
            "diagnose, don't stack worries; one warm, light touch is enough. If nothing stands out, staying quiet is "
            "equally fine."
        )
    elif change:
        # Back-compat: an older backend without the board still returns top-N deltas.
        parts.append("perception_change_json:\n" + json.dumps(change, ensure_ascii=False, sort_keys=True))
    else:
        parts.append("cross_domain_board_json: {}")
    return "\n".join(parts)


def _is_memory_capture_job(job: dict) -> bool:
    return (
        str((job or {}).get("job_kind") or "").strip() == "memory_capture"
        or str((job or {}).get("source") or "").strip() == "memory_capture"
    )


def _is_memory_dream_job(job: dict) -> bool:
    return (
        str((job or {}).get("job_kind") or "").strip() == "memory_dream"
        or str((job or {}).get("source") or "").strip() == "memory_dream"
    )


def _resident_perception_trend(signal: str, field: str) -> dict:
    """Best-effort GET of one signal's rolling baseline/delta (Tier 2 history)."""
    try:
        resp = httpx.get(
            f"{FEEDLING_API_URL}/v1/agent/perception/trend",
            headers=_HEADERS,
            params={"signal": signal, "field": field, "days": 30},
            timeout=15,
        )
        if resp.status_code >= 400:
            return {}
        return resp.json()
    except Exception as e:
        log.debug("proactive trend pull failed %s.%s: %s", signal, field, e)
        return {}


def _resident_perception_now() -> dict:
    """Best-effort direct pull of /v1/agent/perception for native reach-out digest.

    Native reach-out can preload cheap presence hints without reintroducing the
    retired simulated resident tool bridge.
    """
    try:
        resp = httpx.get(
            f"{FEEDLING_API_URL}/v1/agent/perception",
            headers=_HEADERS,
            params={"signals": "now"},
            timeout=15,
        )
        if resp.status_code >= 400:
            return {}
        body = resp.json()
    except Exception as e:
        log.debug("proactive presence pull failed: %s", e)
        return {}
    signals = body.get("signals") if isinstance(body, dict) else {}
    now = signals.get("now") if isinstance(signals, dict) else {}
    return now if isinstance(now, dict) else {}


# Time grounding — the agent otherwise has no reliable "what time is it now":
# foreground chat passed the user's text verbatim, and the device-reported
# local_time goes stale when the app is backgrounded overnight (the agent then
# keeps acting on last night's frame). We compute the user's CURRENT local time
# from the consumer's real clock + the user's timezone (stable; sourced from
# the whoami cache), so every turn/wake is anchored to the real present.
_last_interaction_unix: float = 0.0
_WEEKDAYS_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _user_timezone() -> str:
    """User's IANA timezone, sourced from the whoami cache (refreshed with the
    encryption-key whoami fetch). whoami already resolves record-or-perception
    fallback server-side, so this needs no perception pull."""
    return str(_whoami_cache.get("timezone") or "").strip()


def _local_time_anchor(since_sec: float | None = None) -> str:
    """A reliable 'current local time' line for the agent. Uses the consumer's
    real clock (never stale) + the user's timezone. Optionally appends how long
    since the last interaction so the agent notices an overnight gap."""
    from datetime import datetime, timezone as _tzmod
    tzs = _user_timezone()
    local = datetime.now(_tzmod.utc)
    if tzs:
        try:
            from zoneinfo import ZoneInfo
            local = local.astimezone(ZoneInfo(tzs))
        except Exception:
            pass
    h = local.hour
    seg = "凌晨" if h < 6 else "上午" if h < 12 else "中午" if h < 14 else "下午" if h < 18 else "晚上"
    body = f"{local.strftime('%Y-%m-%d')} {_WEEKDAYS_ZH[local.weekday()]} {local.strftime('%H:%M')} {seg}"
    if tzs:
        body += f" {tzs}"
    line = f"current_time: {body}"
    if since_sec is not None and since_sec >= 1800:  # only note gaps >= 30 min
        gap = _format_age(since_sec).replace(" ago", " 前")
        line += f" (距上次互动 {gap})"
    return line


def _prepend_time_anchor_foreground(content: str, msg_unix_ts: float) -> str:
    """Prepend the real current-time anchor to a foreground user turn so the
    agent is never stuck in a stale (e.g. last-night) frame. since = gap from the
    previous processed message."""
    global _last_interaction_unix
    since = None
    if _last_interaction_unix > 0 and msg_unix_ts > _last_interaction_unix:
        since = msg_unix_ts - _last_interaction_unix
    if msg_unix_ts > _last_interaction_unix:
        _last_interaction_unix = msg_unix_ts
    return f"[{_local_time_anchor(since_sec=since)}]\n\n{content}"


def _resident_perception_digest_board() -> tuple[list, dict]:
    """Best-effort GET of the wake digest. Returns (changes, domains):

    - ``domains`` = the balanced cross-domain board (location/media/app/health/
      weather/mood/reminders/calendar/photos/screen) — what the agent should
      judge from, so the wake impulse isn't health-only.
    - ``changes`` = legacy top-N numeric deltas, kept as a fallback for an older
      backend that has not shipped the board yet.

    Degrades to ([], {}) if the endpoint is unavailable. The agent can still
    drill into any signal on demand via the perception_trend/history tools."""
    try:
        resp = httpx.get(
            f"{FEEDLING_API_URL}/v1/agent/perception/digest",
            headers=_HEADERS,
            params={"days": 30},
            timeout=15,
        )
        if resp.status_code >= 400:
            return [], {}
        body = resp.json()
        if not isinstance(body, dict):
            return [], {}
        changes = list(body.get("changes") or [])
        domains = body.get("domains") if isinstance(body.get("domains"), dict) else {}
        return changes, domains
    except Exception as e:
        log.debug("proactive digest pull failed: %s", e)
        return [], {}


def _proactive_perception_digest() -> tuple[dict, list, dict]:
    """Pre-load real signals into the wake turn so the agent decides from facts,
    not a blind prompt. presence = current cheap snapshot; domains = balanced
    cross-domain board the agent judges from; change = legacy top-N deltas kept
    as a back-compat fallback. All best-effort — failures degrade to empty."""
    presence: dict[str, Any] = {}
    snap = _resident_perception_now()
    if isinstance(snap, dict):
        # local_time/timezone dropped (current_time anchor is the source; device
        # local_time is UTC-stamped + stale). battery_level/charging dropped on
        # purpose: device trivia doesn't belong in every wake's glance — whatever
        # is always in front of the agent is what it ends up reciting. locale stays
        # so the reply-language line is right.
        keys = (
            "place_label", "motion_state", "now_playing",
            "locale", "broadcast_state", "broadcast_active",
        )
        presence = {k: snap.get(k) for k in keys if snap.get(k) is not None}
    change, domains = _resident_perception_digest_board()
    return presence, change, domains


def _send_message_replies_from_actions(actions: list[dict]) -> list[str]:
    replies: list[str] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        typ = _proactive_action_type(action).removeprefix("proactive.")
        if typ != "send_message":
            continue
        text = str(action.get("text") or action.get("message") or "").strip()
        if text:
            replies.append(text[:4000])
    return _cap_agent_replies(replies, max_items=PROACTIVE_MAX_REPLY_MESSAGES)


def _introduction_greeting_from_identity_actions(actions: list[dict]) -> str:
    """Last-resort first greeting when the intro turn wrote identity but omitted messages."""
    fallback_intro = ""
    saw_profile_patch = False
    for action in actions:
        if not isinstance(action, dict):
            continue
        typ = str(action.get("type") or action.get("action") or "").strip()
        if typ != "identity.profile_patch":
            continue
        saw_profile_patch = True
        patch = action.get("patch") if isinstance(action.get("patch"), dict) else action
        signature = patch.get("signature") if isinstance(patch, dict) else None
        if isinstance(signature, list):
            for item in signature:
                text = _sanitize_reply_text(str(item or ""))
                if text:
                    return text[:4000]
        else:
            text = _sanitize_reply_text(str(signature or ""))
            if text:
                return text[:4000]
        intro = _sanitize_reply_text(str((patch or {}).get("self_introduction") or ""))
        if intro and not fallback_intro:
            fallback_intro = intro
    if fallback_intro:
        return fallback_intro[:4000]
    return "我来了。" if saw_profile_patch else ""


def _scheduled_wake_actions(actions: list[dict]) -> list[dict]:
    out: list[dict] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        typ = _proactive_action_type(action).removeprefix("proactive.")
        if typ in {"schedule_wake", "cancel_wake"}:
            out.append(action)
    return out


def _capture_get_json(
    path: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: int = 15,
    base_url: str | None = None,
) -> dict:
    _refresh_auth_header()
    root = (base_url or FEEDLING_API_URL).rstrip("/")
    verify_tls = not (FEEDLING_ENCLAVE_URL and root == FEEDLING_ENCLAVE_URL)
    try:
        resp = httpx.get(
            f"{root}{path}",
            params=params or {},
            headers=_HEADERS,
            timeout=timeout,
            verify=verify_tls,
        )
        resp.raise_for_status()
        body = resp.json()
        return body if isinstance(body, dict) else {}
    except Exception as e:
        log.warning("capture context fetch failed path=%s error=%s", path, e)
        return {}


def _capture_post_json(
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: int = 20,
    base_url: str | None = None,
) -> dict:
    _refresh_auth_header()
    root = (base_url or FEEDLING_API_URL).rstrip("/")
    verify_tls = not (FEEDLING_ENCLAVE_URL and root == FEEDLING_ENCLAVE_URL)
    try:
        resp = httpx.post(
            f"{root}{path}",
            json=payload or {},
            headers=_HEADERS,
            timeout=timeout,
            verify=verify_tls,
        )
        resp.raise_for_status()
        body = resp.json()
        return body if isinstance(body, dict) else {}
    except Exception as e:
        log.warning("capture context post failed path=%s error=%s", path, e)
        return {}


def _capture_context_text(value: Any, *, empty: str = "（暂无）") -> str:
    if value in (None, "", [], {}):
        return empty
    if isinstance(value, str):
        return value[:CAPTURE_CONTEXT_MAX_CHARS]
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)[:CAPTURE_CONTEXT_MAX_CHARS]
    except Exception:
        return str(value)[:CAPTURE_CONTEXT_MAX_CHARS]


def _capture_identity_context() -> tuple[dict, str, str, str]:
    body = (
        _capture_get_json("/v1/identity/get", base_url=FEEDLING_ENCLAVE_URL)
        if FEEDLING_ENCLAVE_URL
        else {}
    )
    if not isinstance(body.get("identity"), dict):
        body = _capture_get_json("/v1/identity/get")
    identity = body.get("identity") if isinstance(body.get("identity"), dict) else {}
    identity = {
        key: value
        for key, value in identity.items()
        if key in {
            "agent_name",
            "ai_name",
            "name",
            "user_preferred_name",
            "user_name",
            "companion_user_name",
            "self_introduction",
            "dimensions",
            "days_with_user",
            "category",
            "signature",
            "visibility",
            "decrypt_status",
        }
        and value not in (None, "", [], {})
    }
    ai_name = str(
        identity.get("agent_name")
        or identity.get("ai_name")
        or identity.get("name")
        or ""
    ).strip() or "我"
    user_name = str(
        identity.get("user_preferred_name")
        or identity.get("user_name")
        or identity.get("companion_user_name")
        or ""
    ).strip() or "TA"
    return identity, ai_name, user_name, _capture_context_text(identity)


def _capture_memory_terms_context() -> tuple[str, str]:
    buckets_body = _capture_get_json("/v1/memory/buckets")
    threads_body = _capture_get_json("/v1/memory/threads")
    return (
        _capture_context_text(buckets_body.get("buckets")),
        _capture_context_text(threads_body.get("threads")),
    )


def _capture_message_text(msg: dict) -> str:
    text = (
        msg.get("content")
        or msg.get("text")
        or msg.get("plaintext")
        or msg.get("body")
        or ""
    )
    if isinstance(text, dict):
        text = json.dumps(text, ensure_ascii=False)
    if not isinstance(text, str):
        text = str(text or "")
    text = re.sub(r"\s+", " ", text.strip())
    if not text:
        ctype = str(msg.get("content_type") or "").lower()
        if ctype == "image" or msg.get("image_b64"):
            return "[image]"
    return text[:2000]


def _capture_message_role(msg: dict) -> str:
    role = str(msg.get("role") or "").strip().lower()
    if role == "user":
        return "user"
    return "agent"


def _capture_message_id(msg: dict) -> str:
    return str(msg.get("id") or msg.get("message_id") or "").strip()


def _capture_live_history(history: list[dict] | None) -> list[dict]:
    out: list[dict] = []
    for msg in history or []:
        if not isinstance(msg, dict):
            continue
        source = str(msg.get("source") or "").strip()
        if source == "verify_ping":
            continue
        role = str(msg.get("role") or "").strip().lower()
        if role not in {"user", "openclaw", "assistant", "agent"}:
            continue
        text = _capture_message_text(msg)
        if not text or "__VERIFY_PING__" in text:
            continue
        item = dict(msg)
        item["_capture_text"] = text
        out.append(item)
    return out


def _capture_window_messages(job: dict) -> list[dict]:
    window = job.get("window") if isinstance(job.get("window"), dict) else {}
    after_id = str(window.get("after_message_id") or "").strip()
    until_id = str(window.get("until_message_id") or "").strip()
    try:
        until_ts = float(window.get("until_ts") or 0)
    except (TypeError, ValueError):
        until_ts = 0.0
    try:
        window_count = int(window.get("message_count") or 0)
    except (TypeError, ValueError):
        window_count = 0
    limit = max(20, CAPTURE_HISTORY_LIMIT)
    history = get_decrypted_history(since=0, limit=limit)
    live = _capture_live_history(history)
    if not live:
        return []
    selected: list[dict] = []
    after_seen = not after_id
    for msg in live:
        msg_id = _capture_message_id(msg)
        ts = _message_ts_for_context(msg)
        if not after_seen:
            if msg_id == after_id:
                after_seen = True
            elif until_ts and ts > until_ts:
                break
            continue
        if until_ts and ts > until_ts:
            break
        selected.append(msg)
        if until_id and msg_id == until_id:
            break
    if not selected and until_ts:
        selected = [msg for msg in live if 0 < _message_ts_for_context(msg) <= until_ts]
    selected = selected[-limit:]
    if window_count > 0:
        selected = selected[-window_count:]
    return selected


def _capture_window_text(messages: list[dict]) -> str:
    lines: list[str] = []
    for msg in messages:
        ts = _message_ts_for_context(msg)
        lines.append(
            f"- [{_format_message_time(ts)}] {_capture_message_role(msg)}: "
            f"{msg.get('_capture_text') or _capture_message_text(msg)}"
        )
    text = "\n".join(lines).strip()
    return text[-CAPTURE_WINDOW_MAX_CHARS:] if len(text) > CAPTURE_WINDOW_MAX_CHARS else text


def _capture_occurred_at(job: dict, messages: list[dict]) -> str:
    window = job.get("window") if isinstance(job.get("window"), dict) else {}
    try:
        ts = float(window.get("until_ts") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    if ts <= 0 and messages:
        ts = _message_ts_for_context(messages[-1])
    if ts <= 0:
        ts = time.time()
    return _format_message_time(ts)


def _capture_agent_reply_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        if isinstance(result.get("cards"), list):
            return json.dumps({"cards": result.get("cards")}, ensure_ascii=False)
        messages = result.get("messages")
        if isinstance(messages, list) and messages:
            return "\n".join(str(item) for item in messages if str(item).strip())
        return json.dumps(result, ensure_ascii=False)
    if isinstance(result, list):
        return json.dumps(result, ensure_ascii=False)
    return str(result or "")


def _capture_build_envelope(card: dict, *, occurred_at: str, source: str = "memory_capture", item_id: str = "") -> dict:
    if not _ENCRYPTION_AVAILABLE:
        raise RuntimeError("capture_encryption_unavailable")
    if not _refresh_whoami_for_encrypted_reply():
        raise RuntimeError("capture_whoami_refresh_failed")
    user_id = str(_whoami_cache.get("user_id") or "").strip()
    user_pk: bytes | None = _whoami_cache.get("user_pk")
    enc_pk: bytes | None = _whoami_cache.get("enclave_pk")
    if not user_id or not user_pk:
        raise RuntimeError("capture_missing_user_key")
    if not enc_pk:
        raise RuntimeError("capture_shared_envelope_requires_enclave_key")

    inner = {
        "summary": str(card.get("summary") or "").strip(),
        "content": str(card.get("content") or "").strip(),
        "bucket": str(card.get("bucket") or "").strip(),
        "threads": list(card.get("threads") or []),
    }
    envelope = _build_envelope(
        plaintext=json.dumps(inner, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        owner_user_id=user_id,
        user_pk_bytes=user_pk,
        enclave_pk_bytes=enc_pk,
        visibility="shared",
        # Migration must seal with the ORIGINAL card id so the AEAD AAD (owner|v|id)
        # matches on decrypt and the upgraded card stays readable AND id-stable.
        # capture/dream (new cards) pass "" -> build_envelope mints a random id.
        item_id=item_id or None,
    )
    envelope.update({
        "type": str(card.get("type") or "event").strip().lower() or "event",
        "occurred_at": occurred_at,
        "importance": float(card.get("importance") or 0),
        "pulse": float(card.get("pulse") or 0),
        "anchor_memory_ids": [],
        "source": str(source or "memory_capture")[:80],
        "last_referenced_at": occurred_at,
    })
    return envelope


def _capture_actions_from_cards(cards: list[dict], *, job: dict, messages: list[dict]) -> tuple[list[dict], int, int]:
    occurred_at = _capture_occurred_at(job, messages)
    source_ids = [_capture_message_id(msg) for msg in messages if _capture_message_id(msg)]
    actions: list[dict] = []
    cards_added = 0
    cards_superseded = 0
    for card in cards:
        action = str(card.get("action") or "").strip().lower()
        target_id = str(card.get("target_id") or "").strip()
        envelope = _capture_build_envelope(card, occurred_at=occurred_at)
        base = {
            "envelope": envelope,
            "reason": "Memory captured from a completed chat window.",
            "capture_mode": "memory_capture",
            "source_chat_message_ids": source_ids,
        }
        if action == "add" or (action in {"merge", "supersede"} and not target_id):
            actions.append({"type": "memory.add", **base})
            cards_added += 1
            continue
        if action in {"merge", "supersede"} and target_id:
            actions.append({"type": "memory.supersede", "supersedes": target_id, **base})
            cards_superseded += 1
    if cards and not actions:
        raise ValueError("capture_no_memory_actions")
    return actions, cards_added, cards_superseded


def _process_capture_jobs(jobs: list) -> float:
    """Realize memory_capture jobs through the native resident agent.

    Capture is background memory maintenance: it never writes chat, never uses
    delivery gates, and never runs the V2 tool loop.
    """
    latest = 0.0
    for job in jobs:
        ts = float(job.get("ts", job.get("timestamp", 0)) or 0)
        latest = max(latest, ts)
        if not _is_memory_capture_job(job):
            continue
        key = _proactive_job_key(job)
        if not _mark_seen(key):
            log.debug("skipping already-processed capture job key=%s", key)
            continue
        job_id = str(job.get("job_id") or "")
        try:
            if not claim_proactive_job(job_id):
                log.info("capture job not claimed id=%s", job_id)
                continue
        except Exception as e:
            log.error("capture job claim failed id=%s: %s", job_id, e)
            continue
        window = job.get("window") if isinstance(job.get("window"), dict) else {}
        update_proactive_job_status(job_id, "realizing")
        messages = _capture_window_messages(job)
        window_text = _capture_window_text(messages)
        if not window_text:
            update_proactive_job_status(
                job_id,
                "failed",
                "capture_window_unavailable",
                extra={
                    "capture_result": {"status": "failed", "reason": "capture_window_unavailable"},
                    "capture_window": window,
                    "cards_added": 0,
                    "cards_superseded": 0,
                    "noop_reason": "capture_window_unavailable",
                },
            )
            continue
        buckets_text, threads_text = _capture_memory_terms_context()
        identity, ai_name, user_name, identity_text = _capture_identity_context()
        prompt = build_capture_prompt(
            ai_name=ai_name,
            user_name=user_name,
            buckets=buckets_text,
            threads=threads_text,
            identity=identity_text,
            window=window_text,
        )
        try:
            reply_text = _capture_agent_reply_text(call_agent(prompt, raw_text=True))
        except Exception as e:
            reason = f"capture_agent_call_failed:{type(e).__name__}"
            log.error("capture agent call failed id=%s: %s", job_id, e)
            update_proactive_job_status(
                job_id,
                "failed",
                reason,
                extra={
                    "capture_result": {"status": "failed", "reason": reason},
                    "capture_window": window,
                    "cards_added": 0,
                    "cards_superseded": 0,
                    "noop_reason": reason,
                },
            )
            continue
        cards, err = parse_capture_cards(reply_text)
        if err:
            update_proactive_job_status(
                job_id,
                "failed",
                err,
                extra={
                    "capture_result": {"status": "failed", "reason": err},
                    "capture_window": window,
                    "cards_added": 0,
                    "cards_superseded": 0,
                    "noop_reason": err,
                },
            )
            continue
        if not cards:
            update_proactive_job_status(
                job_id,
                "completed",
                "nothing_worth_keeping",
                extra={
                    "capture_result": {"status": "noop", "reason": "nothing_worth_keeping"},
                    "capture_window": window,
                    "cards_added": 0,
                    "cards_superseded": 0,
                    "noop_reason": "nothing_worth_keeping",
                },
            )
            log.info("capture job completed noop id=%s", job_id)
            continue
        try:
            actions, cards_added, cards_superseded = _capture_actions_from_cards(
                cards,
                job=job,
                messages=messages,
            )
            memory_result = execute_memory_actions(actions)
        except ValueError as e:
            reason = str(e) or "capture_invalid_memory_action"
            log.error("capture memory action invalid id=%s: %s", job_id, e)
            update_proactive_job_status(
                job_id,
                "failed",
                reason,
                extra={
                    "capture_result": {"status": "failed", "reason": reason},
                    "capture_window": window,
                    "cards_added": 0,
                    "cards_superseded": 0,
                    "noop_reason": reason,
                    "memory_action_status": {"status": "failed", "reason": reason},
                },
            )
            continue
        except Exception as e:
            reason = f"capture_memory_write_failed:{type(e).__name__}"
            log.error("capture memory write failed id=%s: %s", job_id, e)
            update_proactive_job_status(
                job_id,
                "failed",
                reason,
                extra={
                    "capture_result": {"status": "failed", "reason": reason},
                    "capture_window": window,
                    "cards_added": 0,
                    "cards_superseded": 0,
                    "noop_reason": reason,
                    "memory_action_status": {"status": "failed", "reason": str(e)[:500]},
                },
            )
            continue
        update_proactive_job_status(
            job_id,
            "completed",
            "capture_memory_actions_applied",
            extra={
                "capture_result": {
                    "status": "ok",
                    "cards": len(cards),
                    "job_kind": "memory_capture",
                },
                "capture_window": window,
                "memory_action_status": {
                    "status": memory_result.get("status", "ok"),
                    "results": len(memory_result.get("results") or []),
                    "effects": len(memory_result.get("effects") or []),
                },
                "memory_results": memory_result.get("results") or [],
                "cards_added": cards_added,
                "cards_superseded": cards_superseded,
            },
        )
        log.info(
            "capture job completed id=%s cards=%d added=%d superseded=%d identity=%s",
            job_id,
            len(cards),
            cards_added,
            cards_superseded,
            bool(identity),
        )
    return latest


def _dream_index_items() -> list[dict]:
    body = _capture_post_json(
        "/v1/memory/index",
        payload={"limit": max(0, DREAM_MEMORY_INDEX_LIMIT)},
        timeout=30,
    )
    items = body.get("items") if isinstance(body.get("items"), list) else []
    out: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        memory_id = str(item.get("id") or "").strip()
        if not memory_id or memory_id in seen:
            continue
        seen.add(memory_id)
        out.append(dict(item))
        if len(out) >= max(1, DREAM_MEMORY_MAX_CARDS):
            break
    return out


def _dream_fetch_items(ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    by_id: dict[str, dict] = {}
    batch_size = max(1, min(DREAM_FETCH_BATCH_SIZE, 200))
    for offset in range(0, len(ids), batch_size):
        batch = ids[offset : offset + batch_size]
        body = _capture_post_json(
            "/v1/memory/fetch",
            payload={"ids": batch, "limit": len(batch)},
            timeout=30,
        )
        for item in body.get("items") if isinstance(body.get("items"), list) else []:
            if isinstance(item, dict) and str(item.get("id") or "").strip():
                by_id[str(item.get("id") or "").strip()] = dict(item)
    return by_id


def _dream_card_field(card: dict, *names: str) -> str:
    for name in names:
        value = card.get(name)
        if isinstance(value, str) and value.strip():
            return re.sub(r"\s+", " ", value.strip())
    return ""


def _dream_card_threads(card: dict) -> list[str]:
    raw = card.get("threads") or card.get("thread") or []
    values = raw if isinstance(raw, list) else [raw]
    out: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text[:80])
    return out[:8]


def _dream_cards_context() -> tuple[str, dict[str, dict]]:
    index_items = _dream_index_items()
    ids = [str(item.get("id") or "").strip() for item in index_items if str(item.get("id") or "").strip()]
    fetched = _dream_fetch_items(ids)
    merged: list[dict] = []
    by_id: dict[str, dict] = {}
    for item in index_items:
        memory_id = str(item.get("id") or "").strip()
        if not memory_id:
            continue
        card = {**item, **fetched.get(memory_id, {})}
        merged.append(card)
        by_id[memory_id] = card
    lines: list[str] = []
    for card in merged:
        memory_id = str(card.get("id") or "").strip()
        bucket = _dream_card_field(card, "bucket", "category")
        threads = _dream_card_threads(card)
        summary = _dream_card_field(card, "summary", "title", "description")
        content = _dream_card_field(card, "content", "body", "text", "plaintext")
        parts = [f"- id={memory_id}"]
        if bucket:
            parts.append(f"bucket={bucket}")
        if threads:
            parts.append("threads=" + ",".join(threads))
        if summary:
            parts.append(f"summary={summary[:500]}")
        if content and content != summary:
            parts.append(f"content={content[:900]}")
        lines.append(" | ".join(parts))
    text = "\n".join(lines).strip()
    return (text or "（暂无卡）")[:20000], by_id


def _dream_recent_conversations_context() -> str:
    try:
        history = get_decrypted_history(since=0, limit=max(1, min(DREAM_RECENT_CHAT_LIMIT, 240)))
    except Exception as e:
        log.warning("dream recent conversation fetch failed: %s", e)
        return "（这几天没有可读对话）"
    live = _capture_live_history(history)
    if not live:
        return "（这几天没有新对话）"
    lines: list[str] = []
    for msg in live[-max(1, min(DREAM_RECENT_CHAT_LIMIT, 240)):]:
        ts = _message_ts_for_context(msg)
        lines.append(
            f"- [{_format_message_time(ts)}] {_capture_message_role(msg)}: "
            f"{msg.get('_capture_text') or _capture_message_text(msg)}"
        )
    text = "\n".join(lines).strip()
    return text[-12000:] if len(text) > 12000 else text


def _dream_actions_from_consolidations(
    consolidations: list[dict],
    *,
    card_map: dict[str, dict],
    occurred_at: str,
) -> tuple[list[dict], int, int, int]:
    actions: list[dict] = []
    cards_merged = 0
    cards_thickened = 0
    cards_superseded = 0
    for row in consolidations[: max(1, DREAM_MAX_CONSOLIDATIONS)]:
        op = str(row.get("op") or "").strip().lower()
        card_ids = [
            str(memory_id or "").strip()
            for memory_id in (row.get("card_ids") if isinstance(row.get("card_ids"), list) else [])
            if str(memory_id or "").strip()
        ]
        card_ids = list(dict.fromkeys(card_ids))
        if not card_ids:
            continue
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        card = {
            "type": "fact",
            "bucket": str(result.get("bucket") or "").strip(),
            "threads": list(result.get("threads") or []),
            "summary": str(result.get("summary") or "").strip(),
            "content": str(result.get("content") or result.get("summary") or "").strip(),
            "importance": float(result.get("importance") or 0),
            "pulse": float(result.get("pulse") or 0),
        }
        envelope = _capture_build_envelope(card, occurred_at=occurred_at, source="memory_dream")
        actions.append({
            "type": "memory.supersede",
            "supersedes": card_ids,
            "envelope": envelope,
            "reason": f"Memory dream {op} consolidation.",
            "capture_mode": "memory_dream",
            "dream_op": op,
            "dream_card_ids": card_ids,
        })
        if op == "merge":
            cards_merged += 1
        elif op == "thicken":
            cards_thickened += 1
        cards_superseded += len(card_ids)
    if consolidations and not actions:
        raise ValueError("dream_no_memory_actions")
    return actions, cards_merged, cards_thickened, cards_superseded


def _process_dream_jobs(jobs: list) -> float:
    """Realize memory_dream jobs through the native resident agent.

    Dream is background memory organization. It writes only memory actions and
    job status; it never posts chat or uses delivery gates.
    """
    latest = 0.0
    for job in jobs:
        ts = float(job.get("ts", job.get("timestamp", 0)) or 0)
        latest = max(latest, ts)
        if not _is_memory_dream_job(job):
            continue
        key = _proactive_job_key(job)
        if not _mark_seen(key):
            log.debug("skipping already-processed dream job key=%s", key)
            continue
        job_id = str(job.get("job_id") or "")
        try:
            if not claim_proactive_job(job_id):
                log.info("dream job not claimed id=%s", job_id)
                continue
        except Exception as e:
            log.error("dream job claim failed id=%s: %s", job_id, e)
            continue
        update_proactive_job_status(job_id, "realizing")
        cards_text, card_map = _dream_cards_context()
        if not card_map:
            update_proactive_job_status(
                job_id,
                "completed",
                "dream_no_cards_available",
                extra={
                    "dream_result": {"status": "noop", "reason": "dream_no_cards_available", "job_kind": "memory_dream"},
                    "cards_merged": 0,
                    "cards_superseded": 0,
                    "questions": [],
                    "noop_reason": "dream_no_cards_available",
                },
            )
            continue
        recent_text = _dream_recent_conversations_context()
        _identity, ai_name, user_name, _identity_text = _capture_identity_context()
        prompt = build_dream_prompt(
            ai_name=ai_name,
            user_name=user_name,
            cards=cards_text,
            recent_conversations=recent_text,
        )
        try:
            reply_text = _capture_agent_reply_text(call_agent(prompt, raw_text=True))
        except Exception as e:
            reason = f"dream_agent_call_failed:{type(e).__name__}"
            log.error("dream agent call failed id=%s: %s", job_id, e)
            update_proactive_job_status(
                job_id,
                "failed",
                reason,
                extra={
                    "dream_result": {"status": "failed", "reason": reason, "job_kind": "memory_dream"},
                    "cards_merged": 0,
                    "cards_superseded": 0,
                    "questions": [],
                    "noop_reason": reason,
                },
            )
            continue
        consolidations, questions, err = parse_dream_consolidations(reply_text)
        if err:
            update_proactive_job_status(
                job_id,
                "failed",
                err,
                extra={
                    "dream_result": {"status": "failed", "reason": err, "job_kind": "memory_dream"},
                    "cards_merged": 0,
                    "cards_superseded": 0,
                    "questions": questions,
                    "noop_reason": err,
                },
            )
            continue
        if not consolidations:
            update_proactive_job_status(
                job_id,
                "completed",
                "dream_nothing_to_consolidate",
                extra={
                    "dream_result": {
                        "status": "noop",
                        "reason": "dream_nothing_to_consolidate",
                        "job_kind": "memory_dream",
                        "questions": len(questions),
                    },
                    "cards_merged": 0,
                    "cards_superseded": 0,
                    "questions": questions,
                    "noop_reason": "dream_nothing_to_consolidate",
                },
            )
            log.info("dream job completed noop id=%s questions=%d", job_id, len(questions))
            continue
        try:
            occurred_at = _format_message_time(time.time())
            actions, cards_merged, cards_thickened, cards_superseded = _dream_actions_from_consolidations(
                consolidations,
                card_map=card_map,
                occurred_at=occurred_at,
            )
            memory_result = execute_memory_actions(actions)
        except ValueError as e:
            reason = str(e) or "dream_invalid_memory_action"
            log.error("dream memory action invalid id=%s: %s", job_id, e)
            update_proactive_job_status(
                job_id,
                "failed",
                reason,
                extra={
                    "dream_result": {"status": "failed", "reason": reason, "job_kind": "memory_dream"},
                    "cards_merged": 0,
                    "cards_superseded": 0,
                    "questions": questions,
                    "noop_reason": reason,
                    "memory_action_status": {"status": "failed", "reason": reason},
                },
            )
            continue
        except Exception as e:
            reason = f"dream_memory_write_failed:{type(e).__name__}"
            log.error("dream memory write failed id=%s: %s", job_id, e)
            update_proactive_job_status(
                job_id,
                "failed",
                reason,
                extra={
                    "dream_result": {"status": "failed", "reason": reason, "job_kind": "memory_dream"},
                    "cards_merged": 0,
                    "cards_superseded": 0,
                    "questions": questions,
                    "noop_reason": reason,
                    "memory_action_status": {"status": "failed", "reason": str(e)[:500]},
                },
            )
            continue
        update_proactive_job_status(
            job_id,
            "completed",
            "dream_memory_actions_applied",
            extra={
                "dream_result": {
                    "status": "ok",
                    "job_kind": "memory_dream",
                    "consolidations": len(consolidations),
                    "actions": len(actions),
                    "questions": len(questions),
                    "cards_thickened": cards_thickened,
                },
                "memory_action_status": {
                    "status": memory_result.get("status", "ok"),
                    "results": len(memory_result.get("results") or []),
                    "effects": len(memory_result.get("effects") or []),
                },
                "memory_results": memory_result.get("results") or [],
                "cards_merged": cards_merged,
                "cards_superseded": cards_superseded,
                "questions": questions,
            },
        )
        log.info(
            "dream job completed id=%s consolidations=%d actions=%d merged=%d superseded=%d questions=%d",
            job_id,
            len(consolidations),
            len(actions),
            cards_merged,
            cards_superseded,
            len(questions),
        )
    return latest


def _process_proactive_jobs(jobs: list) -> float:
    """Realize hidden proactive jobs through the same configured agent entry."""
    latest = 0.0
    for job in jobs:
        ts = float(job.get("ts", job.get("timestamp", 0)) or 0)
        latest = max(latest, ts)

        if job.get("source") and job.get("source") != PROACTIVE_JOB_SOURCE:
            continue

        key = _proactive_job_key(job)
        if not _mark_seen(key):
            log.debug("skipping already-processed proactive job key=%s", key)
            continue

        job_id = str(job.get("job_id") or "")
        try:
            if not claim_proactive_job(job_id):
                log.info("proactive job not claimed id=%s", job_id)
                continue
        except Exception as e:
            log.error("proactive job claim failed id=%s: %s", job_id, e)
            continue

        is_introduction = _is_introduction_job(job)
        if is_introduction:
            frame_ids = []
            screen_payloads = []
            screen_paths = []
            message = _message_for_introduction_job(job)
        else:
            frame_ids = job.get("frame_ids")
            if not isinstance(frame_ids, list):
                frame_ids = []
            screen_text, screen_payloads, screen_paths = _screen_context_for_frame_ids(frame_ids)
            recent_context = recent_chat_context_for_proactive()
            # Screen-watch is a light lane: skip the heavy cross-domain digest fetch
            # (its prompt deliberately omits the board).
            perception_digest = None if _is_screen_watch_job(job) else _proactive_perception_digest()
            message = _message_for_proactive_job(
                job,
                screen_text=screen_text,
                recent_chat_context=recent_context,
                perception_digest=perception_digest,
            )
        log.info(
            "proactive job [ts=%.3f] id=%s kind=%s intent=%s frames=%d",
            ts,
            job.get("job_id"),
            job.get("job_kind"),
            job.get("intent_label"),
            len(frame_ids),
        )

        if _provider_payment_cooling_down():
            log.warning(
                "proactive job skipped — provider payment required (cooling down); job_id=%s",
                job_id,
            )
            update_proactive_job_status(
                job_id, "failed", "provider_payment_required: cooling down"
            )
            continue
        update_proactive_job_status(job_id, "realizing")
        try:
            agent_result = call_agent(
                message,
                images=screen_payloads,
                image_paths=screen_paths,
            )
        except Exception as e:
            if _is_provider_payment_error(e):
                _note_provider_payment_failure()
                log.error(
                    "proactive agent call failed — provider payment required; "
                    "cooling down %.0fs: %s",
                    PROVIDER_PAYMENT_COOLDOWN_SEC,
                    e,
                )
                update_proactive_job_status(
                    job_id, "failed", f"provider_payment_required: {e}"
                )
                continue
            log.error("proactive agent call failed; not posting fallback: %s", e)
            update_proactive_job_status(job_id, "failed", f"agent_call_failed: {e}")
            continue
        _clear_provider_payment_cooldown()

        turn = _ensure_visible_thinking_summary(
            _split_agent_turn(agent_result, max_items=PROACTIVE_MAX_REPLY_MESSAGES),
            source="proactive_fallback",
        )
        actions, replies = turn.actions, turn.messages
        if not replies:
            replies = _send_message_replies_from_actions(actions)
        proactive_actions, memory_identity_actions = _split_proactive_actions(actions)
        status_actions = [_compact_action_for_status(a) for a in proactive_actions]
        control_reply_reason = _proactive_control_reason_from_result(agent_result, replies)
        if control_reply_reason and not proactive_actions and not memory_identity_actions:
            update_proactive_job_status(
                job_id,
                "completed",
                control_reply_reason[:240],
                extra={
                    "agent_action": "sleep",
                    "agent_action_status": control_reply_reason[:240],
                    "wake_result": "sleep",
                },
            )
            log.info("proactive wake slept from control reply id=%s reason=%s", job_id, control_reply_reason)
            continue
        if memory_identity_actions:
            try:
                result = execute_agent_actions(memory_identity_actions)
                log.info(
                    "proactive memory/identity actions applied id=%s effects=%d",
                    job_id,
                    len(result.get("effects") or []),
                )
            except Exception as e:
                log.warning("proactive memory/identity actions failed id=%s error=%s", job_id, e)
                if is_introduction:
                    update_proactive_job_status(
                        job_id,
                        "failed",
                        f"introduction_identity_action_failed:{type(e).__name__}",
                        extra={
                            "agent_action": "identity.profile_patch",
                            "agent_action_status": str(e)[:240],
                            "wake_result": "identity_action_failed",
                        },
                    )
                    continue
        if is_introduction and not replies and memory_identity_actions:
            reply = _introduction_greeting_from_identity_actions(memory_identity_actions)
            if reply:
                replies = [reply]
                log.info("introduction greeting recovered from identity action id=%s", job_id)

        schedule_action_results: list[dict] = []
        scheduled_action_failed = False
        schedule_actions = _scheduled_wake_actions(proactive_actions)
        if schedule_actions:
            try:
                result = execute_scheduled_wake_actions(schedule_actions, job)
                schedule_action_results = [
                    dict(item)
                    for item in (result.get("results") or [])
                    if isinstance(item, dict)
                ]
                update_proactive_job_status(
                    job_id,
                    "realizing",
                    "agent_scheduled_wake_actions",
                    extra={
                        "agent_action": "scheduled_wake_actions",
                        "agent_action_status": json.dumps(
                            schedule_action_results,
                            ensure_ascii=False,
                        )[:240],
                        "agent_actions": status_actions + schedule_action_results,
                    },
                )
            except Exception as e:
                log.warning("proactive scheduled wake actions failed id=%s error=%s", job_id, e)
                scheduled_action_failed = True
                schedule_action_results = [{
                    "type": "scheduled_wake_actions_result",
                    "status": "failed",
                    "reason": str(e)[:240],
                }]
            if schedule_action_results:
                status_actions.extend(schedule_action_results)

        if scheduled_action_failed and not replies:
            update_proactive_job_status(
                job_id,
                "failed",
                "scheduled_wake_actions_failed",
                extra={
                    "agent_action": "scheduled_wake_actions",
                    "agent_action_status": json.dumps(
                        schedule_action_results,
                        ensure_ascii=False,
                    )[:240],
                    "agent_actions": status_actions,
                    "wake_result": "action_failed",
                },
            )
            continue

        request_broadcast = _first_proactive_action(proactive_actions, {"request_broadcast"})
        if request_broadcast and not replies:
            replies = [_visible_broadcast_request_text(request_broadcast)]
            update_proactive_job_status(
                job_id,
                "realizing",
                "agent_request_broadcast",
                extra={
                    "agent_action": "request_broadcast",
                    "agent_action_status": str(request_broadcast.get("reason") or "")[:240],
                    "agent_actions": status_actions,
                    "request_broadcast": request_broadcast,
                },
            )

        sleep_action = _first_proactive_action(proactive_actions, {"sleep"})
        if sleep_action and not replies:
            update_proactive_job_status(
                job_id,
                "completed",
                str(sleep_action.get("reason") or "agent_sleep"),
                extra={
                    "agent_action": "sleep",
                    "agent_action_status": str(sleep_action.get("reason") or "agent_sleep")[:240],
                    "agent_actions": status_actions,
                    "wake_result": "sleep",
                },
            )
            log.info("proactive wake slept id=%s reason=%s", job_id, sleep_action.get("reason") or "")
            continue

        if schedule_actions and not replies:
            update_proactive_job_status(
                job_id,
                "completed",
                "agent_scheduled_wake_actions",
                extra={
                    "agent_action": "scheduled_wake_actions",
                    "agent_action_status": json.dumps(
                        schedule_action_results,
                        ensure_ascii=False,
                    )[:240],
                    "agent_actions": status_actions,
                    "wake_result": "action_only",
                },
            )
            log.info("proactive wake completed scheduled actions id=%s", job_id)
            continue

        posted_any = False
        last_error = ""
        for idx, reply in enumerate(replies):
            try:
                post_kwargs = {
                    "source": PROACTIVE_JOB_SOURCE,
                    "gate_decision_id": str(job.get("gate_decision_id") or ""),
                    "proactive_job_id": job_id,
                }
                if idx == 0 and turn.thinking_summary:
                    post_kwargs["thinking_summary"] = turn.thinking_summary
                    post_kwargs["thinking_kind"] = turn.thinking_kind
                    post_kwargs["thinking_source"] = turn.thinking_source
                    post_kwargs["thinking_model"] = turn.thinking_model
                    post_kwargs["thinking_native"] = turn.thinking_native
                result = post_reply(reply, **post_kwargs)
                if isinstance(result, dict) and result.get("error"):
                    raise RuntimeError(str(result)[:500])
                posted_any = True
                if isinstance(result, dict):
                    extra = {
                        "wake_result": "posted",
                    }
                    if status_actions:
                        extra["agent_actions"] = status_actions
                    if request_broadcast:
                        extra["agent_action"] = "request_broadcast"
                        extra["request_broadcast"] = request_broadcast
                    update_proactive_job_status(
                        job_id,
                        "posted",
                        f"chat_message_id={result.get('id', '')}",
                        extra=extra,
                    )
                log.info("proactive reply sent: %s", reply[:80])
            except Exception as e:
                last_error = str(e)
                log.error("failed to post proactive reply: %s", e)
        if not posted_any:
            update_proactive_job_status(job_id, "failed", last_error or "empty_agent_reply")

    return latest


def _is_memory_migrate_job(job: dict) -> bool:
    return (
        str((job or {}).get("job_kind") or "").strip() == "memory_migrate"
        or str((job or {}).get("source") or "").strip() == "memory_migrate"
    )


def _migrate_render_old_cards(batch: list[dict]) -> str:
    """Render the legacy batch (raw old inner) for the migrate prompt — id + only
    the old content fields that are present."""
    lines: list[str] = []
    for row in batch:
        inner = row.get("inner") if isinstance(row.get("inner"), dict) else {}
        fields = {
            k: inner.get(k)
            for k in ("title", "description", "her_quote", "context", "linked_dimension")
            if inner.get(k)
        }
        lines.append(json.dumps({"id": row.get("id"), **fields}, ensure_ascii=False))
    return "\n".join(lines) if lines else "（没有要升级的卡）"


def _process_migrate_jobs(jobs: list) -> float:
    """Realize memory_migrate jobs: upgrade a batch of legacy cards to v1 in place.

    Server picks + raw-decrypts the legacy batch (/v1/memory/legacy_batch); the
    agent derives v1; we write each back via memory.upgrade (in-place,保 id, CAS).
    A card counts as migrated ONLY on upgrade status=ok; skipped(stale)/empty(db
    write fail)/parser-dropped all stay for the next quiet window (self-heal);
    skipped(not_found) just drops (card gone). Writes only memory actions + the
    migration-state cache; never posts chat.
    """
    latest = 0.0
    from memory.migration import migration_enabled
    if not migration_enabled():
        return latest  # FEEDLING_MIGRATE_ENABLE off → full stop, don't process queued migrate jobs
    for job in jobs:
        ts = float(job.get("ts", job.get("timestamp", 0)) or 0)
        latest = max(latest, ts)
        if not _is_memory_migrate_job(job):
            continue
        key = _proactive_job_key(job)
        if not _mark_seen(key):
            continue
        job_id = str(job.get("job_id") or "")
        try:
            if not claim_proactive_job(job_id):
                log.info("migrate job not claimed id=%s", job_id)
                continue
        except Exception as e:
            log.error("migrate job claim failed id=%s: %s", job_id, e)
            continue
        update_proactive_job_status(job_id, "realizing")

        try:
            batch_size = max(1, min(int(os.environ.get("FEEDLING_MIGRATE_BATCH", "8")), 50))
        except (TypeError, ValueError):
            batch_size = 8
        batch_body = _capture_post_json("/v1/memory/legacy_batch", payload={"batch_size": batch_size})
        if not isinstance(batch_body.get("batch"), list) or "legacy_remaining" not in batch_body:
            reason = "legacy_batch_unavailable"
            update_proactive_job_status(
                job_id, "failed", reason,
                extra={"migrate_result": {"status": "failed", "reason": reason}},
            )
            log.warning("migrate job failed id=%s reason=%s body_keys=%s",
                        job_id, reason, sorted(batch_body.keys()) if isinstance(batch_body, dict) else [])
            continue
        batch = batch_body.get("batch") if isinstance(batch_body.get("batch"), list) else []
        legacy_remaining = int(batch_body.get("legacy_remaining") or 0)
        if not batch:
            _capture_post_json("/v1/memory/migration_state", payload={"migrated": 0, "legacy_remaining": 0})
            update_proactive_job_status(
                job_id, "completed", "migrate_no_legacy",
                extra={"migrate_result": {"status": "noop", "reason": "no_legacy", "migrated": 0}},
            )
            log.info("migrate job completed noop (no legacy) id=%s", job_id)
            continue

        allowed_ids = {str(r.get("id")) for r in batch if r.get("id")}
        hash_by_id = {str(r.get("id")): str(r.get("old_body_hash") or "") for r in batch}
        _identity, ai_name, user_name, _identity_text = _capture_identity_context()
        buckets_text, threads_text = _capture_memory_terms_context()
        prompt = build_migrate_prompt(
            ai_name=ai_name,
            user_name=user_name,
            old_cards=_migrate_render_old_cards(batch),
            vocab=f"已有桶: {buckets_text}\n已有线索: {threads_text}",
        )
        try:
            reply_text = _capture_agent_reply_text(call_agent(prompt, raw_text=True))
        except Exception as e:
            reason = f"migrate_agent_call_failed:{type(e).__name__}"
            log.error("migrate agent call failed id=%s: %s", job_id, e)
            update_proactive_job_status(
                job_id, "failed", reason,
                extra={"migrate_result": {"status": "failed", "reason": reason}},
            )
            continue
        upgrades, unmigrated_ids, err = parse_migrated_cards(reply_text, allowed_ids=allowed_ids)
        if err:
            update_proactive_job_status(
                job_id, "failed", err,
                extra={"migrate_result": {"status": "failed", "reason": err}},
            )
            continue

        occurred_at = _format_message_time(time.time())
        migrated = 0
        # A11: any batch card that did NOT migrate this round is a failed attempt — the
        # agent dropped it (unmigrated_ids) OR envelope build / memory.upgrade failed.
        # Seed with the parser's unmigrated set, then add per-card write failures and
        # remove the ones that actually succeed. The server bumps each card's attempt
        # count; after FEEDLING_MIGRATE_MAX_ATTEMPTS it marks the card skipped so it
        # stops looping and legacy_remaining can reach 0.
        failed_ids: set[str] = set(unmigrated_ids)
        for up in upgrades:
            mid = str(up.get("id") or "")
            if not mid:
                continue
            try:
                envelope = _capture_build_envelope(up, occurred_at=occurred_at, source="memory_migrate", item_id=mid)
            except Exception as e:
                log.error("migrate envelope build failed id=%s card=%s: %s", job_id, mid, e)
                failed_ids.add(mid)
                continue  # retry next round (until cap)
            # Let memory.upgrade carry the existing metadata (don't reset). Migration
            # is not a "user just used this memory", so last_referenced_at must NOT be
            # bumped to now — drop it (and importance/pulse) so existing values stay.
            envelope.pop("importance", None)
            envelope.pop("pulse", None)
            envelope.pop("last_referenced_at", None)
            body = _capture_post_json("/v1/memory/actions", payload={"action": {
                "type": "memory.upgrade",
                "id": mid,
                "envelope": envelope,
                "old_body_hash": hash_by_id.get(mid, ""),
            }})
            res = (body.get("results") or [{}])[0] if isinstance(body, dict) else {}
            if res.get("status") == "ok" and not res.get("skipped"):
                migrated += 1
                failed_ids.discard(mid)
            else:
                # skipped(stale)/empty(db_write_failed,network)/dropped → not migrated → counts
                # as a failed attempt → retry next window until the per-card cap is hit.
                failed_ids.add(mid)

        remaining = max(0, legacy_remaining - migrated)
        _capture_post_json("/v1/memory/migration_state", payload={
            "migrated": migrated,
            "legacy_remaining": remaining,
            "failed_ids": sorted(failed_ids),
        })
        update_proactive_job_status(
            job_id, "completed", "migrate_batch_done",
            extra={"migrate_result": {
                "status": "ok",
                "migrated": migrated,
                "batch": len(batch),
                "unmigrated": len(unmigrated_ids),
                "failed": len(failed_ids),
                "remaining": remaining,
            }},
        )
        log.info(
            "migrate job completed id=%s migrated=%d/%d unmigrated=%d failed=%d remaining=%d",
            job_id, migrated, len(batch), len(unmigrated_ids), len(failed_ids), remaining,
        )
    return latest


def _process_resident_jobs(jobs: list) -> float:
    capture_jobs = [
        job for job in (jobs or [])
        if isinstance(job, dict) and _is_memory_capture_job(job)
    ]
    dream_jobs = [
        job for job in (jobs or [])
        if isinstance(job, dict) and _is_memory_dream_job(job)
    ]
    migrate_jobs = [
        job for job in (jobs or [])
        if isinstance(job, dict) and _is_memory_migrate_job(job)
    ]
    proactive_jobs = [
        job for job in (jobs or [])
        if not (
            isinstance(job, dict)
            and (_is_memory_capture_job(job) or _is_memory_dream_job(job) or _is_memory_migrate_job(job))
        )
    ]
    return max(
        _process_capture_jobs(capture_jobs) if capture_jobs else 0.0,
        _process_dream_jobs(dream_jobs) if dream_jobs else 0.0,
        _process_migrate_jobs(migrate_jobs) if migrate_jobs else 0.0,
        _process_proactive_jobs(proactive_jobs) if proactive_jobs else 0.0,
    )


def _process_messages(messages: list) -> float:
    """Process a batch of messages, return the highest timestamp seen."""
    latest = 0.0
    for msg in messages:
        # Tolerate both "ts" and "timestamp" key names across API versions.
        ts = float(msg.get("ts", msg.get("timestamp", 0)) or 0)
        role = msg.get("role", "")
        if role != "user":
            latest = max(latest, ts)
            continue

        # Idempotency — skip messages already processed in this session.
        key = _msg_key(msg)
        if not _mark_seen(key):
            log.debug("skipping already-processed message key=%s", key)
            latest = max(latest, ts)
            continue

        # Synthetic liveness probe from /v1/chat/verify_loop. Identified ONLY
        # by `source`, which the server stamps as "verify_ping" across all
        # three delivery paths — direct /v1/chat/poll, the enclave decrypt
        # proxy (enclave_app.py passes source through even for local_only), and
        # MCP (mcp_server.py merges source+content back by id). We must NOT also
        # match the __VERIFY_PING__ content marker: a real user message that
        # merely contains that string (e.g. debugging this feature) would be
        # falsely swallowed and never reach the agent. (The probe is
        # visibility=local_only, so over the enclave path its content is None —
        # this check sits BEFORE the empty-content skip below so the probe is
        # still answered.) Reply immediately with a canned token instead of
        # routing the probe through the full agent — a hermes turn can exceed
        # verify_loop's timeout and is fragile to mid-run SIGTERM, so the probe
        # would time out (passing=false) even on a healthy reply pipeline.
        if msg.get("source") == "verify_ping":
            # Exercise the REAL agent path so verify catches a broken reply
            # pipeline (e.g. an agent whose output the consumer can't parse).
            # The old canned short-circuit let verify pass while the live loop
            # was actually dead. A slow-but-healthy agent must not falsely fail,
            # so the probe is bounded: on timeout/transient error we fall back to
            # the canned ack (verify still passes); only a COMPLETED call that
            # yields no usable reply is a real failure — we then post nothing so
            # verify_loop stays unsatisfied and onboarding does not green-light a
            # dead loop. The probe reply is visibility=local_only and GC'd by the
            # server, so it never reaches the user's visible chat.
            log.info("verify ping [ts=%.3f] — exercising real agent path", ts)
            probe: dict[str, Any] = {}

            def _run_verify_probe() -> None:
                try:
                    probe["result"] = call_agent(VERIFY_PROBE_MESSAGE)
                except ValueError as exc:        # no usable reply after sanitization
                    probe["no_usable_reply"] = str(exc)
                except Exception as exc:         # timeout / transport / runtime
                    probe["error"] = str(exc)

            probe_thread = threading.Thread(target=_run_verify_probe, daemon=True)
            probe_thread.start()
            probe_thread.join(timeout=VERIFY_PROBE_TIMEOUT_SEC)
            try:
                # All verify replies carry source="verify_ping" so the server
                # filters them out of the user's visible chat history (and
                # verify_loop's GC matches them) even when the reply lands after
                # the GC window — otherwise the (real or canned) ack leaks as a
                # stray visible message. suppress_push already kills the APNs push.
                if probe_thread.is_alive():
                    log.warning("verify ping — agent slow (>%ss); canned ack fallback so verify still passes", VERIFY_PROBE_TIMEOUT_SEC)
                    post_reply(VERIFY_PING_REPLY, source="verify_ping", suppress_push=True)
                elif "result" in probe:
                    replies = _normalize_agent_replies(probe["result"]) or [VERIFY_PING_REPLY]
                    post_reply(replies[0], source="verify_ping", suppress_push=True)
                    log.info("verify ping — real agent reply OK")
                elif "no_usable_reply" in probe:
                    log.error("verify ping — agent produced no usable reply; NOT acking so verify fails (live loop is broken): %s", probe["no_usable_reply"])
                    # post nothing — verify_loop stays unsatisfied on purpose
                else:
                    log.warning("verify ping — agent call errored (%s); canned ack fallback", probe.get("error"))
                    post_reply(VERIFY_PING_REPLY, source="verify_ping", suppress_push=True)
            except Exception as e:
                log.error("failed to post verify-ping reply: %s", e)
            latest = max(latest, ts)
            continue

        content = msg.get("content", "").strip()
        content_type = msg.get("content_type", "text")
        image_payloads: list[dict[str, str]] = []
        image_paths: list[str] = []

        if content_type == "image":
            # Image messages legitimately have content == "" — the JPEG
            # lives in image_b64. Extract it here so the agent entry receives
            # real image context instead of only a vague "image arrived" hint.
            log.info(
                "image message [ts=%.3f] — preparing image context for agent",
                ts,
            )
            image_payloads = _image_payloads_from_msg(msg)
            image_paths = _image_file_paths_for_msg(msg) if image_payloads else []
            if not image_payloads:
                log.warning(
                    "image message [ts=%.3f] has no decrypted image_b64; "
                    "routing honest image-unavailable prompt",
                    ts,
                )
            # Preserve the user's text caption — enclave history now decrypts and
            # fills `content` for captioned image turns ("what is wrong here?").
            # Only fall back to the placeholder when there is genuinely no text,
            # otherwise the agent gets the attachment but loses the actual prompt.
            if not content:
                content = IMAGE_PLACEHOLDER
        elif not content:
            # Genuinely empty text — decrypt source missing or failed.
            # Never send a fallback for content we cannot read.
            log.warning(
                "user message has no plaintext content ts=%.3f content_type=%s "
                "— skipping (set FEEDLING_ENCLAVE_URL to enable decryption)",
                ts, content_type,
            )
            latest = max(latest, ts)
            continue
        else:
            log.info("user message [ts=%.3f]: %s", ts, content[:80])

        trace_id = str(msg.get("id") or msg.get("message_id") or "").strip()

        screen_text, screen_payloads, screen_paths = _screen_context_for_message(content)
        screen_attached = bool(screen_payloads or screen_paths)
        _emit_debug_trace("context", "context.build", trace_id=trace_id,
                          summary="context assembled",
                          explain=("本轮附加了屏幕上下文" if screen_attached else "本轮未附加屏幕上下文"),
                          detail={"screen_attached": screen_attached})
        if screen_text:
            content = f"{content}\n\n{screen_text}"
            image_payloads.extend(screen_payloads)
            image_paths.extend(screen_paths)
            log.info(
                "attached screen context to agent message ts=%.3f images=%d",
                ts,
                len(screen_payloads),
            )
        worldbook_text = _worldbook_context_for_foreground(content)
        if worldbook_text:
            content = f"World book context:\n{worldbook_text}\n\n{content}"

        # Ground every foreground turn in the real current time (+ gap since last
        # interaction) so the agent never carries a stale, e.g. overnight, frame.
        content = _prepend_time_anchor_foreground(content, ts)
        # Then inject cross-turn continuity for drivers with no reliable session of
        # their own (codex / hosted claude). No-op for pi / when disabled / when
        # there is no prior turn. Done once here so every dispatch branch below
        # (v2, image, plain) carries the same context. Wraps the time-anchored
        # content so the transcript sits above this turn's grounded message.
        content = _foreground_agent_message(content, current_ts=ts)
        content = _foreground_response_protocol_message(content)

        use_runtime_v2 = _resident_chat_runtime_v2_enabled() and not (image_payloads or image_paths)
        try:
            if use_runtime_v2:
                agent_result = call_agent(_resident_foreground_chat_message_v2(content), trace_id=trace_id)
            elif image_payloads or image_paths:
                agent_result = call_agent(
                    content,
                    images=image_payloads,
                    image_paths=image_paths,
                    trace_id=trace_id,
                )
            else:
                agent_result = call_agent(content, trace_id=trace_id)
        except Exception as e:
            log.error("agent call failed; posting user-visible fallback: %s", e)
            if SEND_FALLBACK_ON_AGENT_ERROR:
                agent_result = [FALLBACK_REPLY]
            else:
                log.warning("agent error fallback disabled by env; this user turn will not get a visible reply")
                latest = max(latest, ts)
                continue

        turn = _ensure_visible_thinking_summary(
            _split_agent_turn(agent_result),
            source="foreground_fallback",
        )
        _reply_text = "\n\n".join(m for m in turn.messages if isinstance(m, str) and m.strip())
        _emit_debug_trace(
            "agent", "agent.reply", trace_id=trace_id,
            summary=f"reply parsed ({len(turn.messages)} msg)",
            explain=("回复已解析：" + f"{len(turn.messages)} 段"
                     + ("，含思考摘要" if turn.thinking_summary else "，无思考摘要")),
            detail={"n_messages": len(turn.messages), "n_actions": len(turn.actions),
                    "thinking_kind": turn.thinking_kind or "", "thinking_model": turn.thinking_model or ""},
            content_excerpt={"reply": _reply_text[:3000], "thinking": (turn.thinking_summary or "")[:2000]},
        )
        actions, replies = turn.actions, turn.messages
        if use_runtime_v2:
            actions = [
                action for action in actions
                if _proactive_action_type(action).removeprefix("proactive.") != "needs_background"
            ]
        if actions:
            try:
                action_result = execute_agent_actions(actions)
                log.info(
                    "agent action(s) executed count=%d effects=%d",
                    len(actions),
                    len(action_result.get("effects") or []),
                )
                if not replies:
                    replies = [_identity_action_success_reply(content)]
            except Exception as e:
                log.error("agent action execution failed; suppressing optimistic agent reply: %s", e)
                replies = [_identity_action_failure_reply(content)]

        reply_to_message_id = str(msg.get("id") or msg.get("message_id") or "").strip()
        posted_any = False
        terminal_response_error = False
        for idx, reply in enumerate(replies):
            try:
                post_kwargs = {}
                if reply_to_message_id:
                    post_kwargs["reply_to_message_id"] = reply_to_message_id
                if idx == 0 and turn.thinking_summary:
                    post_kwargs["thinking_summary"] = turn.thinking_summary
                    post_kwargs["thinking_kind"] = turn.thinking_kind
                    post_kwargs["thinking_source"] = turn.thinking_source
                    post_kwargs["thinking_model"] = turn.thinking_model
                    post_kwargs["thinking_native"] = turn.thinking_native
                result = post_reply(reply, **post_kwargs)
                if isinstance(result, dict) and result.get("error"):
                    if result.get("error") == "bootstrap_incomplete":
                        terminal_response_error = True
                        log.error("reply rejected by bootstrap gate; advancing past this dead-end message")
                        continue
                    raise RuntimeError(str(result)[:500])
                posted_any = True
                log.info("reply sent: %s", reply[:80])
            except Exception as e:
                log.error("failed to post reply: %s", e)

        if replies and not posted_any and not terminal_response_error:
            # Keep checkpoint behind this message. The server-side claim lease
            # will expire, allowing this or another responder to retry instead
            # of permanently dropping a user turn after a transient write error.
            continue

        latest = max(latest, ts)

    return latest


def run() -> None:
    # Hard auth check before entering the poll loop.
    # A missing user_id or public_key means every encrypted reply will fail;
    # exit now so the operator sees an immediate error instead of silent no-ops.
    if not _ENCRYPTION_AVAILABLE:
        log.critical(
            "content_encryption module not found — v1 envelope posting disabled. "
            "Make sure the consumer runs from the feedling-mcp repo root."
        )
        sys.exit(1)

    if not _load_whoami_with_retries():
        log.critical(
            "whoami failed at startup — cannot obtain user_id or public_key. "
            "Check FEEDLING_API_URL and FEEDLING_API_KEY, then restart."
        )
        sys.exit(1)

    _warn_if_agent_entry_may_drift()

    if FEEDLING_ENCLAVE_URL:
        if not _verify_decrypt_sources():
            log.critical(
                "Decrypt source unreachable (enclave=%s). "
                "Cannot decrypt user messages — exiting.",
                FEEDLING_ENCLAVE_URL,
            )
            sys.exit(1)
    else:
        log.warning(
            "⚠️  No decryption source configured (FEEDLING_ENCLAVE_URL is unset). "
            "User messages in v1 encrypted mode have content=\"\" and will be "
            "silently skipped — the consumer will never send replies. "
            "Set FEEDLING_ENCLAVE_URL (direct enclave) to fix this."
        )

    last_ts = _load_checkpoint()

    if last_ts == 0.0:
        try:
            last_ts = get_latest_ts()
            log.info("no checkpoint — seeding from history ts=%.3f", last_ts)
        except Exception as e:
            log.warning("could not seed from history: %s", e)

    _save_checkpoint(last_ts)
    # Wedge guard: consecutive poll cycles where the claimed ids never show up in
    # decrypt history, keyed on the cursor they're stuck behind (see
    # _advance_past_unfetchable). After CHAT_POLL_WEDGE_SKIP_AFTER we skip past them.
    wedge_miss_ts: float | None = None
    wedge_miss_count = 0
    last_job_ts = _load_proactive_checkpoint()
    proactive_enabled = PROACTIVE_POLL_ENABLED
    if proactive_enabled and last_job_ts == 0.0:
        # Start from "now" on first boot so historical hidden jobs are not
        # replayed after an operator installs the consumer.
        last_job_ts = time.time()
        _save_proactive_checkpoint(last_job_ts)
    last_broadcast_state = ""
    next_proactive_tick_mono = time.monotonic() + max(0, PROACTIVE_TICK_START_DELAY_SEC)
    scheduled_fire_enabled = proactive_enabled and PROACTIVE_SCHEDULED_FIRE_ENABLED
    next_scheduled_fire_mono = time.monotonic() + max(0, PROACTIVE_SCHEDULED_FIRE_START_DELAY_SEC)
    capture_tick_enabled = CAPTURE_TICK_ENABLED
    next_capture_tick_mono = time.monotonic() + max(0, CAPTURE_TICK_START_DELAY_SEC)
    screen_watch_enabled = proactive_enabled and SCREEN_WATCH_ENABLED
    next_screen_watch_mono = time.monotonic() + max(0, SCREEN_WATCH_START_DELAY_SEC)
    last_screen_watch_frame_id = ""

    log.info(
        "starting poll loop — last_ts=%.3f last_job_ts=%.3f poll_timeout=%ds proactive=%s proactive_tick=%s tick_on=%ds tick_off=%ds scheduled_fire=%s scheduled_fire_interval=%ds capture_tick=%s capture_tick_interval=%ds",
        last_ts,
        last_job_ts,
        POLL_TIMEOUT,
        proactive_enabled,
        PROACTIVE_TICK_ENABLED,
        PROACTIVE_TICK_BROADCAST_ON_INTERVAL_SEC,
        PROACTIVE_TICK_BROADCAST_OFF_INTERVAL_SEC,
        scheduled_fire_enabled,
        PROACTIVE_SCHEDULED_FIRE_INTERVAL_SEC,
        capture_tick_enabled,
        CAPTURE_TICK_INTERVAL_SEC,
    )

    consecutive_errors = 0

    while _running:
        try:
            _refresh_auth_header()  # pick up a freshly-minted runtime token (Stage D)
            if capture_tick_enabled and time.monotonic() >= next_capture_tick_mono:
                try:
                    capture_result = fire_capture_tick()
                    if capture_result.get("enqueued") or str(capture_result.get("reason") or "") not in {"", "no_new_messages", "quiet_not_due", "already_captured"}:
                        log.info(
                            "capture tick enqueued=%s reason=%s quiet_for=%s",
                            bool(capture_result.get("enqueued")),
                            capture_result.get("reason"),
                            capture_result.get("quiet_for_sec", ""),
                        )
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 404:
                        capture_tick_enabled = False
                        log.warning(
                            "capture tick endpoint not available on this backend; "
                            "disabling capture tick for this process"
                        )
                    else:
                        raise
                finally:
                    next_capture_tick_mono = time.monotonic() + max(10, CAPTURE_TICK_INTERVAL_SEC)
            if proactive_enabled:
                try:
                    if scheduled_fire_enabled and time.monotonic() >= next_scheduled_fire_mono:
                        try:
                            fire_result = fire_scheduled_wakes()
                            fire_results = fire_result.get("results") or []
                            fire_jobs = fire_result.get("jobs") or []
                            if fire_results or fire_jobs:
                                statuses = [
                                    str(item.get("status") or "")
                                    for item in fire_results
                                    if isinstance(item, dict)
                                ]
                                log.info(
                                    "scheduled wake fire results=%d queued=%d statuses=%s",
                                    len(fire_results),
                                    len(fire_jobs),
                                    ",".join(statuses) or "none",
                                )
                        except httpx.HTTPStatusError as e:
                            if e.response.status_code == 404:
                                scheduled_fire_enabled = False
                                log.warning(
                                    "scheduled wake fire endpoint not available on this backend; "
                                    "disabling scheduled wake fire for this process"
                                )
                            else:
                                raise
                        finally:
                            next_scheduled_fire_mono = (
                                time.monotonic() + max(10, PROACTIVE_SCHEDULED_FIRE_INTERVAL_SEC)
                            )
                    if PROACTIVE_TICK_ENABLED and time.monotonic() >= next_proactive_tick_mono:
                        tick_payload = {
                            "trigger": _proactive_tick_trigger_for_broadcast_state(last_broadcast_state),
                        }
                        if last_broadcast_state:
                            tick_payload["broadcast_state"] = last_broadcast_state
                        tick = post_proactive_tick(tick_payload)
                        decision = tick.get("decision") or {}
                        last_broadcast_state = str(
                            decision.get("broadcast_state") or last_broadcast_state or ""
                        ).strip().lower()
                        next_interval = _proactive_tick_interval_for_broadcast_state(
                            last_broadcast_state, decision.get("wake_interval_sec")
                        )
                        log.info(
                            "proactive wake tick wake=%s reason=%s enqueued=%s frames=%d broadcast=%s next=%ds",
                            bool(decision.get("should_reach_out")),
                            decision.get("reason"),
                            bool(tick.get("enqueued")),
                            len(decision.get("frame_ids") or []),
                            last_broadcast_state or "unknown",
                            next_interval,
                        )
                        next_proactive_tick_mono = time.monotonic() + next_interval
                    if screen_watch_enabled and time.monotonic() >= next_screen_watch_mono:
                        try:
                            latest_fid, latest_ts, watch_frames = _screen_watch_recent_frames()
                            fresh = bool(latest_fid) and (time.time() - latest_ts) <= SCREEN_WATCH_FRESH_SEC
                            changed = bool(latest_fid) and latest_fid != last_screen_watch_frame_id
                            if fresh and changed:
                                # Only act on genuinely new content; backlog stays
                                # reachable via screen_recent in the light prompt.
                                last_screen_watch_frame_id = latest_fid
                                sw_chat = recent_chat_context_for_proactive()
                                user_age = sw_chat.last_user_message_age_sec
                                chatting = (
                                    user_age is not None
                                    and user_age < SCREEN_WATCH_CHAT_SUPPRESS_SEC
                                )
                                if chatting:
                                    log.info(
                                        "screen-watch yielding to active chat (user_msg_age=%.0fs)",
                                        user_age if user_age is not None else -1,
                                    )
                                else:
                                    sw = post_screen_watch_tick("on", watch_frames)
                                    log.info(
                                        "screen-watch tick enqueued=%s frames=%d frame_id=%s",
                                        bool(sw.get("enqueued")),
                                        len(watch_frames),
                                        latest_fid[:12],
                                    )
                        except Exception as e:
                            log.warning("screen-watch tick failed: %s", e)
                        finally:
                            next_screen_watch_mono = time.monotonic() + max(30, SCREEN_WATCH_INTERVAL_SEC)
                    job_result = poll_proactive_jobs(last_job_ts)
                    jobs = job_result.get("jobs") or []
                    if jobs:
                        new_job_ts = _process_resident_jobs(jobs)
                        if new_job_ts > last_job_ts:
                            last_job_ts = new_job_ts
                            _save_proactive_checkpoint(last_job_ts)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 404:
                        proactive_enabled = False
                        log.warning(
                            "proactive jobs endpoint not available on this backend; "
                            "disabling proactive polling for this process"
                        )
                    else:
                        raise

            result = poll_chat(last_ts)
            consecutive_errors = 0

            if result.get("timed_out"):
                # Idle moment: safe to swap to the backend's commit and re-exec
                # (no in-flight message to interrupt). Does not return if it updates.
                _maybe_self_update(result)
                continue

            poll_messages = result.get("messages") or []
            if not poll_messages:
                continue

            # poll is used only as a trigger — its content fields are "" for
            # v1 encrypted envelopes. Fetch actual plaintext from a decrypt source.
            if FEEDLING_ENCLAVE_URL:
                decrypt_since = _poll_decrypt_since(last_ts, poll_messages)
                decrypted = get_decrypted_history(
                    since=decrypt_since,
                    limit=_poll_decrypt_limit(decrypt_since, last_ts, poll_messages),
                )
                if decrypted is None:
                    # All configured sources failed — skip this cycle, keep checkpoint.
                    log.warning(
                        "poll triggered but all decrypt sources failed; "
                        "skipping cycle (messages not processed)"
                    )
                    continue
                if not decrypted:
                    # Sources OK but no new messages — advance from poll timestamps.
                    log.debug("poll triggered but decrypt sources returned no new messages")
                    for m in poll_messages:
                        pts = float(m.get("ts", m.get("timestamp", 0)) or 0)
                        if pts > last_ts:
                            last_ts = pts
                            _save_checkpoint(last_ts)
                    continue
                messages = _filter_messages_to_poll_ids(decrypted, poll_messages)
                if not messages:
                    if wedge_miss_ts == last_ts:
                        wedge_miss_count += 1
                    else:
                        wedge_miss_ts = last_ts
                        wedge_miss_count = 1
                    if wedge_miss_count >= CHAT_POLL_WEDGE_SKIP_AFTER:
                        skip_ts = _advance_past_unfetchable(last_ts, poll_messages)
                        log.error(
                            "poll claimed %d message(s) absent from decrypt history "
                            "for %d cycles; advancing cursor %.3f→%.3f to unwedge "
                            "(undecryptable/boundary message skipped)",
                            len(poll_messages), wedge_miss_count, last_ts, skip_ts,
                        )
                        last_ts = skip_ts
                        _save_checkpoint(last_ts)
                        wedge_miss_ts = None
                        wedge_miss_count = 0
                    else:
                        log.warning(
                            "poll returned claimed messages but decrypt history did "
                            "not include those ids; keeping checkpoint for retry "
                            "(%d/%d)", wedge_miss_count, CHAT_POLL_WEDGE_SKIP_AFTER,
                        )
                    continue
            else:
                # No decrypt source — fall through with poll content (will be
                # empty for v1 encrypted messages, skipped in _process_messages).
                messages = poll_messages

            new_ts = _process_messages(messages)
            if new_ts > last_ts:
                last_ts = new_ts
                _save_checkpoint(last_ts)

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            log.error("HTTP %d on poll: %s", status, e)
            if status == 401:
                log.warning("401 on poll — API key may have changed; refreshing whoami")
                if not _load_whoami():
                    log.critical(
                        "whoami returned 401 — API key is invalid. "
                        "Update FEEDLING_API_KEY and restart the service."
                    )
                    sys.exit(1)
            consecutive_errors += 1
            time.sleep(min(2 ** consecutive_errors, 60))
        except Exception as e:
            log.error("poll error: %s", e)
            consecutive_errors += 1
            time.sleep(min(2 ** consecutive_errors, 60))

    log.info("resident consumer stopped")


if __name__ == "__main__":
    run()
