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
  CHECKPOINT_FILE       Path to persist last-processed timestamp (default: /tmp/feedling_chat_checkpoint.json)
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
                        Broadcast-off tick interval in seconds (default: 1800)
  PROACTIVE_TICK_START_DELAY_SEC
                        Delay before the first automatic wake tick (default: 15)
  PROACTIVE_SCHEDULED_FIRE_ENABLED
                        Default true. Poll resident-owned scheduled_wake timers
                        and enqueue due hidden jobs.
  PROACTIVE_SCHEDULED_FIRE_INTERVAL_SEC
                        Scheduled wake fire cadence in seconds (default: 60)
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

from proactive.agent_protocol_v2 import build_agent_context_v2
from proactive.tool_catalog_v2 import tool_catalog_v2_for_runtime
from proactive.tool_loop_v2 import run_tool_loop_v2
# NOTE: proactive.adapters_v2 and proactive.runtime_v2 are imported lazily inside
# the proactive-job path (_resident_v2_agent_context_for_job). They transitively
# pull the backend DB layer (runtime_v2 -> observability_v2 -> db -> psycopg),
# which a resident consumer (a pure HTTP client with no database) must not be
# forced to install just to reply to chat. Keeping them out of module import
# means the chat reply loop runs psycopg-free.

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

CHECKPOINT_FILE = Path(
    os.environ.get("CHECKPOINT_FILE", "/tmp/feedling_chat_checkpoint.json")
)
PROACTIVE_JOB_SOURCE = "agent_initiated_proactive"
RESIDENT_WAKE_RUNTIME_V2_FLAG = "resident_wake_runtime_v2_enabled"
RESIDENT_CHAT_RUNTIME_V2_FLAG = "resident_chat_runtime_v2_enabled"
FOREGROUND_CHAT_TOOL_BUDGET_MODE_V2 = "foreground_chat_fast"
PROACTIVE_POLL_ENABLED = _env_bool("PROACTIVE_POLL_ENABLED", True)
PROACTIVE_POLL_TIMEOUT = int(os.environ.get("PROACTIVE_POLL_TIMEOUT", "1"))
PROACTIVE_TICK_ENABLED = _env_bool("PROACTIVE_TICK_ENABLED", True)
PROACTIVE_TICK_INTERVAL_SEC = int(os.environ.get("PROACTIVE_TICK_INTERVAL_SEC", "300"))
PROACTIVE_TICK_BROADCAST_ON_INTERVAL_SEC = int(
    os.environ.get("PROACTIVE_TICK_BROADCAST_ON_INTERVAL_SEC", str(PROACTIVE_TICK_INTERVAL_SEC))
)
PROACTIVE_TICK_BROADCAST_OFF_INTERVAL_SEC = int(
    os.environ.get("PROACTIVE_TICK_BROADCAST_OFF_INTERVAL_SEC", "1800")
)
PROACTIVE_TICK_START_DELAY_SEC = int(os.environ.get("PROACTIVE_TICK_START_DELAY_SEC", "15"))
PROACTIVE_SCHEDULED_FIRE_ENABLED = _env_bool("PROACTIVE_SCHEDULED_FIRE_ENABLED", True)
PROACTIVE_SCHEDULED_FIRE_INTERVAL_SEC = int(os.environ.get("PROACTIVE_SCHEDULED_FIRE_INTERVAL_SEC", "60"))
PROACTIVE_SCHEDULED_FIRE_START_DELAY_SEC = int(os.environ.get("PROACTIVE_SCHEDULED_FIRE_START_DELAY_SEC", "5"))
PROACTIVE_MAX_REPLY_MESSAGES = int(os.environ.get("PROACTIVE_MAX_REPLY_MESSAGES", "5"))
PROACTIVE_RECENT_CHAT_LIMIT = int(os.environ.get("PROACTIVE_RECENT_CHAT_LIMIT", "20"))
PROACTIVE_CHAT_CONTEXT_LOOKBACK_LIMIT = int(os.environ.get("PROACTIVE_CHAT_CONTEXT_LOOKBACK_LIMIT", "50"))
PROACTIVE_CHAT_FRESH_WINDOW_SEC = int(os.environ.get("PROACTIVE_CHAT_FRESH_WINDOW_SEC", "21600"))
PROACTIVE_STALE_CHAT_FALLBACK_LIMIT = int(os.environ.get("PROACTIVE_STALE_CHAT_FALLBACK_LIMIT", "2"))
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

def _load_checkpoint_data() -> dict[str, float]:
    try:
        data = json.loads(CHECKPOINT_FILE.read_text())
        if not isinstance(data, dict):
            return {}
        return {
            "last_ts": float(data.get("last_ts", 0) or 0),
            "last_job_ts": float(data.get("last_job_ts", 0) or 0),
        }
    except Exception:
        return {}


def _write_checkpoint_data(data: dict[str, float]) -> None:
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
    _write_checkpoint_data(data)


def _load_proactive_checkpoint() -> float:
    return float(_load_checkpoint_data().get("last_job_ts", 0.0) or 0.0)


def _save_proactive_checkpoint(ts: float) -> None:
    data = _load_checkpoint_data()
    data.setdefault("last_ts", 0.0)
    data["last_job_ts"] = ts
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
    return (
        f"{content}\n\n"
        f"Decrypted image file(s) for this IO message: {joined}\n"
        "Open/inspect the image before replying if your runtime has local "
        "vision or file-image support. Do not ask the user to describe the "
        "image unless this runtime truly cannot inspect local image files."
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
    "debug",
    "delta",
    "log",
    "progress",
    "reasoning",
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
    if normalized in {"provider_reasoning", "reasoning"}:
        return "provider_reasoning"
    if normalized == "runtime_trace":
        return "runtime_trace"
    if "reasoning" in normalized or "thought" in normalized:
        return "provider_reasoning_summary"
    return "agent_summary"


def _merge_agent_turn(dst: AgentTurn, src: AgentTurn) -> AgentTurn:
    dst.actions.extend(src.actions)
    dst.messages.extend(src.messages)
    dst.tool_calls.extend(src.tool_calls)
    if not dst.thinking_summary and src.thinking_summary:
        dst.thinking_summary = src.thinking_summary
        dst.thinking_kind = src.thinking_kind
        dst.thinking_source = src.thinking_source
        dst.thinking_model = src.thinking_model
        dst.thinking_native = src.thinking_native
    dst.runtime_debug.update(src.runtime_debug)
    return dst


def _agent_turn_from_obj(obj: Any) -> AgentTurn:
    turn = AgentTurn()

    if isinstance(obj, str):
        raw = obj.strip()
        if not raw:
            return turn
        json_objects = _json_objects_from_cli_output(raw)
        if json_objects:
            for item in json_objects:
                _merge_agent_turn(turn, _agent_turn_from_obj(item))
            if turn.messages or turn.actions or turn.thinking_summary or turn.tool_calls:
                return turn
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

    for key in _JSON_THINKING_FIELDS:
        value = obj.get(key)
        if isinstance(value, str) and value.strip() and not turn.thinking_summary:
            turn.thinking_summary = _sanitize_thinking_summary(value)
            turn.thinking_kind = explicit_kind or _default_thinking_kind_for_key(key)
            turn.thinking_source = explicit_source
            turn.thinking_model = explicit_model
            turn.thinking_native = explicit_native
        elif isinstance(value, dict) and not turn.thinking_summary:
            summary = value.get("summary") or value.get("content") or value.get("text")
            if isinstance(summary, str):
                turn.thinking_summary = _sanitize_thinking_summary(summary)
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


def _call_agent_http_simple(message: str, images: list[dict[str, str]] | None = None) -> Any:
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


def _call_agent_http_openai(message: str, images: list[dict[str, str]] | None = None) -> Any:
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
    turn = _agent_turn_from_raw(body)
    if turn.actions or turn.thinking_summary or turn.tool_calls or len(turn.messages) > 1:
        return body
    if turn.messages:
        return turn.messages[0]
    raise ValueError("OpenAI-compatible response has no usable reply text")


def call_agent_http(message: str, images: list[dict[str, str]] | None = None) -> Any:
    if not AGENT_HTTP_URL:
        raise ValueError("AGENT_HTTP_URL is not set for http mode")
    if AGENT_HTTP_PROTOCOL in {"openai", "hermes", "chat_completions", "chat-completions"}:
        return _call_agent_http_openai(message, images=images)
    if AGENT_HTTP_PROTOCOL in {"simple", "generic", "json"}:
        return _call_agent_http_simple(message, images=images)
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
    rendered_message = message
    if image_paths and "{image_path" not in AGENT_CLI_CMD:
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
        if sid and not _has_cli_resume(cmd) and not _has_claude_session_id(cmd):
            cmd = [cmd[0], "--resume", sid, *cmd[1:]]

    return _resolve_cli_executable(cmd)


def call_agent_cli(message: str, image_paths: list[str] | None = None) -> Any:
    if not AGENT_CLI_CMD:
        raise ValueError("AGENT_CLI_CMD is not set for cli mode")

    cmd = _prepare_cli_command(message, image_paths=image_paths)
    command_sid = _cli_flag_value(cmd, "--session-id")
    log.debug("running cli agent: %s", cmd)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

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
            f"cli agent exited {result.returncode}: {(result.stderr or '')[:300]}"
        )
    raw = result.stdout
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
) -> Any:
    if AGENT_MODE == "http":
        raw = call_agent_http(message, images=images)
    elif AGENT_MODE == "cli":
        raw = call_agent_cli(message, image_paths=image_paths)
    else:
        raise ValueError(f"unknown AGENT_MODE: {AGENT_MODE!r}")

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


# ---------------------------------------------------------------------------
# V2 proactive tool loop (D11) — resident HTTP call_tool + loop entrypoint
# ---------------------------------------------------------------------------

def _resident_call_tool_v2(name: str, args: Any, *, budget_mode: str = "") -> dict:
    """POST to /v1/proactive/tool/execute and return the normalised result dict.

    Note: a network-level exception (not an HTTP status, e.g. connection refused)
    propagates out of this function and is caught by _process_proactive_jobs's
    try/except, marking the job failed — this is intentional fail-closed behaviour.
    """
    body = {"name": name, "args": dict(args or {})}
    if budget_mode:
        body["budget_mode"] = budget_mode
    resp = httpx.post(
        f"{FEEDLING_API_URL}/v1/proactive/tool/execute",
        headers=_HEADERS,
        json=body,
        timeout=60,
    )
    if resp.status_code >= 400:
        return {
            "name": name,
            "ok": False,
            "outcome": "error",
            "result": {},
            "error_code": f"http_{resp.status_code}",
            "needs_background": False,
        }
    return resp.json()


def _resident_run_agent_v2(message: str, *, foreground_chat: bool = False) -> Any:
    """Run the multi-turn V2 tool loop for a proactive resident turn.

    The external agent is called via call_agent (CLI or HTTP, no images for V2
    jobs).  Tool execution happens via _resident_call_tool_v2 (HTTP bridge to
    /v1/proactive/tool/execute).  Returns the terminal reply from call_agent
    (may be a dict in HTTP mode or a str in CLI mode); _split_agent_turn already
    knows how to parse both.
    """
    base_messages: list[dict[str, Any]] = [{"role": "user", "content": message}]

    def call_model(messages: list[dict[str, Any]]) -> Any:
        # Re-serialise the running transcript as a single text block for the
        # external agent (CLI / HTTP).  Non-string content (e.g. dict returned
        # by call_agent in HTTP mode) is serialised as JSON so multi-round
        # history never arrives as a Python repr like {'tool_calls': [...]}.
        # V2 jobs never pass images.
        text = "\n\n".join(
            m["content"] if isinstance(m["content"], str)
            else json.dumps(m["content"], ensure_ascii=False)
            for m in messages
        )
        return call_agent(text)

    def call_tool(name: str, args: Any) -> dict:
        if not foreground_chat:
            return _resident_call_tool_v2(name, args)
        res = _resident_call_tool_v2(
            name,
            args,
            budget_mode=FOREGROUND_CHAT_TOOL_BUDGET_MODE_V2,
        )
        if res.get("needs_background"):
            return {
                "name": name,
                "ok": False,
                "outcome": "unavailable",
                "result": {},
                "error_code": "foreground_slow_tool_unavailable",
                "error_message": "This tool is not available in foreground chat.",
                "needs_background": False,
            }
        return res

    return run_tool_loop_v2(call_model, call_tool, base_messages, max_iters=4)


def _resident_foreground_chat_message_v2(content: str) -> str:
    """Resident foreground chat is a native-agent turn.

    Hosted LLMs need prompt-injected JSON tool instructions. Resident agents
    such as OpenClaw/Claude Code should receive the user's message directly and
    use their registered native tools (io_cli for Feedling perception).
    """
    return content


# ---------------------------------------------------------------------------
# Feedling API helpers
# ---------------------------------------------------------------------------

# Cached from /v1/users/whoami for diagnostics and fallback state. Refreshed
# before every encrypted write so resident agents do not keep wrapping replies
# to a stale iOS content public key.
_whoami_cache: dict = {"user_id": "", "user_pk": None, "enclave_pk": None}

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

    _whoami_cache.update(user_id=user_id, user_pk=user_pk, enclave_pk=enc_pk)
    log.info(
        "whoami loaded — user_id=%s user_pk=%s enclave_pk=%s",
        user_id,
        _fingerprint_bytes(user_pk),
        _fingerprint_bytes(enc_pk),
    )
    return bool(user_id and user_pk)


def _load_whoami_with_retries() -> bool:
    """Fetch whoami at startup, tolerating transient network/TLS failures."""
    attempts = max(1, WHOAMI_STARTUP_RETRIES)
    delay = max(0.0, WHOAMI_STARTUP_RETRY_DELAY_SEC)

    for idx in range(attempts):
        if _load_whoami():
            return True
        if idx + 1 < attempts:
            log.warning(
                "whoami startup check failed; retrying %s/%s in %.1fs",
                idx + 2,
                attempts,
                delay,
            )
            if delay:
                time.sleep(delay)
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


def _proactive_tick_interval_for_broadcast_state(broadcast_state: str) -> int:
    state = str(broadcast_state or "").strip().lower()
    if not state or state == "off":
        return max(60, PROACTIVE_TICK_BROADCAST_OFF_INTERVAL_SEC)
    return max(30, PROACTIVE_TICK_BROADCAST_ON_INTERVAL_SEC)


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

    if _ENCRYPTION_AVAILABLE and not _load_whoami():
        log.error("whoami refresh failed before encrypted reply; skipping write")
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
    if not text:
        ctype = str(msg.get("content_type") or "").lower()
        if ctype == "image" or msg.get("image_b64"):
            text = "[image]"
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


def _split_proactive_actions(actions: list[dict]) -> tuple[list[dict], list[dict]]:
    proactive: list[dict] = []
    memory_identity: list[dict] = []
    proactive_types = {
        "sleep",
        "set_ai_state",
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


def _message_for_proactive_job(
    job: dict,
    screen_text: str = "",
    recent_chat_context: Any = "",
) -> str:
    chat_context = _coerce_proactive_chat_context(recent_chat_context)
    wake_kind = _proactive_wake_kind(job, screen_text=screen_text)
    screen_available = bool(screen_text)
    parts = [
        "[Feedling proactive wake]",
        "This wake is not a user request and not an instruction to respond.",
        "It is an awareness / presence check.",
        "The platform did not judge this moment and did not require a memory match. You decide what to do.",
        "Before visible speech, ask whether you genuinely want to appear now from your own companion identity.",
        "Speak only when there is a strong enough self-directed reason, feeling, memory, or presence impulse to appear.",
        "If the impulse is weak, unclear, generic, or merely caused by this wake, return proactive.sleep.",
        "Do not mention this hidden wake, job metadata, or system wording to the user.",
        "If you speak, use your normal voice. You may send 1-5 short chat bubbles.",
        "If you should stay quiet, return JSON: {\"actions\":[{\"type\":\"proactive.sleep\",\"reason\":\"...\"}],\"messages\":[]}",
        "If you need the user to turn screen sharing back on, return JSON: {\"actions\":[{\"type\":\"proactive.request_broadcast\",\"reason\":\"...\",\"copy\":\"...\"}],\"messages\":[]}",
        "If your presence state changed, include an action like {\"type\":\"proactive.set_ai_state\",\"state\":\"curious\"}.",
        "For visible replies, return JSON exactly like {\"messages\":[\"...\",\"...\"]}. Plain text is also accepted for a single message.",
        (
            "wake_metadata:\n"
            f"- wake_id: {str(job.get('wake_id') or job.get('gate_decision_id') or '')}\n"
            f"- trigger: {str(job.get('trigger') or 'wake')}\n"
            f"- manual: {bool(job.get('manual', False))}\n"
            f"- forced: {bool(job.get('forced', False))}\n"
            f"- wake_kind: {wake_kind}\n"
            f"- user_state: {str(job.get('user_state') or 'default')}\n"
            f"- ai_state: {str(job.get('ai_state') or 'present')}\n"
            f"- broadcast_state: {str(job.get('broadcast_state') or 'unknown')}\n"
            f"- current_app: {str(job.get('current_app') or 'unknown')}\n"
            f"- screen_context_available: {str(screen_available).lower()}"
        ),
        _proactive_attention_facts(chat_context),
    ]
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
            "screen_context:\n"
            "No current screen context is available. Do not imply you can see the user's screen."
        )
    if screen_text:
        parts.append(screen_text)
    return "\n\n".join(parts)


def _resident_runtime_v2_enabled_for_job(job: dict) -> bool:
    profile = job.get("runtime_v2") if isinstance(job.get("runtime_v2"), dict) else {}
    return bool(profile.get(RESIDENT_WAKE_RUNTIME_V2_FLAG) or job.get(RESIDENT_WAKE_RUNTIME_V2_FLAG))


def _resident_user_id_for_job(job: dict) -> str:
    return str(
        _whoami_cache.get("user_id")
        or job.get("user_id")
        or job.get("owner_user_id")
        or "resident_user"
    )


def _recent_chat_for_v2_context(value: Any) -> list[dict[str, Any]]:
    chat = _coerce_proactive_chat_context(value)
    if not chat.text:
        return []
    return [{
        "role": "recent_chat_context",
        "content": chat.text,
        "freshness": chat.freshness,
        "included_count": chat.included_count,
        "last_message_age_sec": chat.last_message_age_sec,
        "last_user_message_age_sec": chat.last_user_message_age_sec,
        "last_visible_proactive_age_sec": chat.last_visible_proactive_age_sec,
        "visible_proactive_count_24h": chat.visible_proactive_count_24h,
    }]


def _resident_v2_agent_context_for_job(
    job: dict,
    *,
    recent_chat_context: Any = "",
    runtime: str = "resident",
) -> dict[str, Any]:
    # Lazy import: these pull the backend DB layer (psycopg) and are only needed
    # on the proactive-job path, not for chat replies. See the import-block note.
    from proactive.adapters_v2 import wake_event_v2_from_legacy_job
    from proactive.runtime_v2 import merge_wakes_v2
    event = wake_event_v2_from_legacy_job(_resident_user_id_for_job(job), job)
    merged = merge_wakes_v2([event], tool_catalog=tool_catalog_v2_for_runtime(runtime))
    return build_agent_context_v2(
        merged,
        recent_chat=_recent_chat_for_v2_context(recent_chat_context),
    )


def _message_for_proactive_job_v2(
    job: dict,
    screen_text: str = "",
    recent_chat_context: Any = "",
) -> str:
    _ = screen_text
    context = _resident_v2_agent_context_for_job(
        job,
        recent_chat_context=recent_chat_context,
        runtime="resident",
    )
    parts = [
        "[Feedling Runtime V2 proactive wake]",
        "This is a hidden wake turn, not a user request.",
        "The platform supplied wake context and tools only; it did not decide whether you should speak.",
        "You own the decision. If appearing is not useful or natural, return sleep.",
        "Return JSON only using the V2 action contract.",
        (
            "Allowed actions: "
            "send_message {text}, sleep {reason}, schedule_wake {at,tz,note,origin_refs}, "
            "cancel_wake {wake_id,reason}."
        ),
        "For visible speech, prefer actions:[{\"type\":\"send_message\",\"text\":\"...\"}] or messages:[\"...\"].",
        "Do not mention this hidden wake, runtime metadata, or system wording to the user.",
        (
            "Tool use: you may return {\"tool_calls\":[{\"name\":\"<tool>\",\"args\":{...}}]} to gather "
            "information from the tools listed in v2_context_json. You will receive results and may call "
            "more tools (a few rounds). When you have enough information, finish with messages/actions "
            "and NO tool_calls."
        ),
        "v2_context_json:\n" + json.dumps(context, ensure_ascii=False, sort_keys=True, indent=2),
    ]
    return "\n\n".join(parts)


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


def _scheduled_wake_actions(actions: list[dict]) -> list[dict]:
    out: list[dict] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        typ = _proactive_action_type(action).removeprefix("proactive.")
        if typ in {"schedule_wake", "cancel_wake"}:
            out.append(action)
    return out


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

        frame_ids = job.get("frame_ids")
        if not isinstance(frame_ids, list):
            frame_ids = []
        screen_text, screen_payloads, screen_paths = _screen_context_for_frame_ids(frame_ids)
        recent_context = recent_chat_context_for_proactive()
        use_runtime_v2 = _resident_runtime_v2_enabled_for_job(job)
        if use_runtime_v2:
            message = _message_for_proactive_job_v2(
                job,
                screen_text=screen_text,
                recent_chat_context=recent_context,
            )
        else:
            message = _message_for_proactive_job(
                job,
                screen_text=screen_text,
                recent_chat_context=recent_context,
            )
        log.info(
            "proactive job [ts=%.3f] id=%s intent=%s frames=%d",
            ts,
            job.get("job_id"),
            job.get("intent_label"),
            len(frame_ids),
        )

        update_proactive_job_status(job_id, "realizing")
        try:
            agent_images = [] if use_runtime_v2 else screen_payloads
            agent_image_paths = [] if use_runtime_v2 else screen_paths
            if use_runtime_v2:
                agent_result = _resident_run_agent_v2(message)
            else:
                agent_result = call_agent(
                    message,
                    images=agent_images,
                    image_paths=agent_image_paths,
                )
        except Exception as e:
            log.error("proactive agent call failed; not posting fallback: %s", e)
            update_proactive_job_status(job_id, "failed", f"agent_call_failed: {e}")
            continue

        turn = _split_agent_turn(agent_result, max_items=PROACTIVE_MAX_REPLY_MESSAGES)
        actions, replies = turn.actions, turn.messages
        if use_runtime_v2 and not replies:
            replies = _send_message_replies_from_actions(actions)
        proactive_actions, memory_identity_actions = _split_proactive_actions(actions)
        status_actions = [_compact_action_for_status(a) for a in proactive_actions]
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

        schedule_action_results: list[dict] = []
        scheduled_action_failed = False
        schedule_actions = _scheduled_wake_actions(proactive_actions) if use_runtime_v2 else []
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

        set_ai_state = _first_proactive_action(proactive_actions, {"set_ai_state"})
        if set_ai_state:
            ai_state = str(set_ai_state.get("state") or set_ai_state.get("ai_state") or "").strip().lower()
            if ai_state:
                update_proactive_state(ai_state=ai_state)
                update_proactive_job_status(
                    job_id,
                    "realizing",
                    "agent_set_ai_state",
                    extra={
                        "agent_action": "set_ai_state",
                        "agent_action_status": ai_state,
                        "agent_actions": status_actions,
                        "ai_state": ai_state,
                    },
                )

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

        if use_runtime_v2 and schedule_actions and not replies:
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

        if set_ai_state and not replies:
            state_label = str(set_ai_state.get("state") or set_ai_state.get("ai_state") or "updated").strip()
            update_proactive_job_status(
                job_id,
                "completed",
                f"agent_set_ai_state:{state_label}",
                extra={
                    "agent_action": "set_ai_state",
                    "agent_action_status": state_label[:240],
                    "agent_actions": status_actions,
                    "wake_result": "state_only",
                },
            )
            log.info("proactive wake updated ai_state without reply id=%s state=%s", job_id, state_label)
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
                if probe_thread.is_alive():
                    log.warning("verify ping — agent slow (>%ss); canned ack fallback so verify still passes", VERIFY_PROBE_TIMEOUT_SEC)
                    post_reply(VERIFY_PING_REPLY, suppress_push=True)
                elif "result" in probe:
                    replies = _normalize_agent_replies(probe["result"]) or [VERIFY_PING_REPLY]
                    post_reply(replies[0], suppress_push=True)
                    log.info("verify ping — real agent reply OK")
                elif "no_usable_reply" in probe:
                    log.error("verify ping — agent produced no usable reply; NOT acking so verify fails (live loop is broken): %s", probe["no_usable_reply"])
                    # post nothing — verify_loop stays unsatisfied on purpose
                else:
                    log.warning("verify ping — agent call errored (%s); canned ack fallback", probe.get("error"))
                    post_reply(VERIFY_PING_REPLY, suppress_push=True)
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

        screen_text, screen_payloads, screen_paths = _screen_context_for_message(content)
        if screen_text:
            content = f"{content}\n\n{screen_text}"
            image_payloads.extend(screen_payloads)
            image_paths.extend(screen_paths)
            log.info(
                "attached screen context to agent message ts=%.3f images=%d",
                ts,
                len(screen_payloads),
            )

        use_runtime_v2 = _resident_chat_runtime_v2_enabled() and not (image_payloads or image_paths)
        try:
            if use_runtime_v2:
                agent_result = call_agent(_resident_foreground_chat_message_v2(content))
            elif image_payloads or image_paths:
                agent_result = call_agent(
                    content,
                    images=image_payloads,
                    image_paths=image_paths,
                )
            else:
                agent_result = call_agent(content)
        except Exception as e:
            log.error("agent call failed; posting user-visible fallback: %s", e)
            if SEND_FALLBACK_ON_AGENT_ERROR:
                agent_result = [FALLBACK_REPLY]
            else:
                log.warning("agent error fallback disabled by env; this user turn will not get a visible reply")
                latest = max(latest, ts)
                continue

        turn = _split_agent_turn(agent_result)
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

    log.info(
        "starting poll loop — last_ts=%.3f last_job_ts=%.3f poll_timeout=%ds proactive=%s proactive_tick=%s tick_on=%ds tick_off=%ds scheduled_fire=%s scheduled_fire_interval=%ds",
        last_ts,
        last_job_ts,
        POLL_TIMEOUT,
        proactive_enabled,
        PROACTIVE_TICK_ENABLED,
        PROACTIVE_TICK_BROADCAST_ON_INTERVAL_SEC,
        PROACTIVE_TICK_BROADCAST_OFF_INTERVAL_SEC,
        scheduled_fire_enabled,
        PROACTIVE_SCHEDULED_FIRE_INTERVAL_SEC,
    )

    consecutive_errors = 0

    while _running:
        try:
            _refresh_auth_header()  # pick up a freshly-minted runtime token (Stage D)
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
                        next_interval = _proactive_tick_interval_for_broadcast_state(last_broadcast_state)
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
                    job_result = poll_proactive_jobs(last_job_ts)
                    jobs = job_result.get("jobs") or []
                    if jobs:
                        new_job_ts = _process_proactive_jobs(jobs)
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
                decrypted = get_decrypted_history(since=last_ts, limit=20)
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
                    log.warning(
                        "poll returned claimed messages but decrypt history did not "
                        "include those ids; keeping checkpoint for retry"
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
