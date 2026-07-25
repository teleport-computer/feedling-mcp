"""Small hosted-response compatibility helpers for Runtime V2.

The public response still carries the historical provider-driver label. It is a
wire-compatibility label only: Runtime V2 calls providers natively and there is
no hosted resident CLI process or supervisor behind it.
"""

from __future__ import annotations

import logging
import os
import time

import db
import provider_client

log = logging.getLogger("feedling.hosted.agent_runtime_cutover")

# The public API historically exposed a provider-derived ``driver`` field.
# Runtime V2 keeps those labels for wire compatibility only; every provider now
# executes in the same native in-process tool-calling loop.
_CLAUDE_PROVIDERS = {"anthropic", "deepseek"}
_CODEX_PROVIDERS = {"openai"}
_PI_PROVIDERS = {"openai_compatible", "gemini", "openrouter"}


def driver_for_provider(provider: str) -> str:
    """Return the historical response label for a provider, not an executor.

    The values preserve the pre-V2 API mapping: anthropic/deepseek →
    ``claude``; OpenAI-compatible relays → ``pi``; OpenAI → ``codex``. No
    configured fit → ``legacy``. None of these labels selects a separate V2
    execution path."""
    p = provider_client.normalize_provider(provider)
    if p in _CLAUDE_PROVIDERS:
        return "claude"
    if p in _PI_PROVIDERS:
        return "pi"
    if p in _CODEX_PROVIDERS:
        return "codex"
    return "legacy"


def codex_transport(provider: str) -> str:
    """Historical telemetry field: OpenAI is native, other labels are empty."""
    p = provider_client.normalize_provider(provider)
    if driver_for_provider(p) != "codex":
        return ""
    return "native"


class UnsupportedProviderError(Exception):
    """provider 未配置或 Runtime V2 没有 native provider mapping。"""


def assert_hosting_ready() -> None:
    """Fail fast unless the pooled V2 worker token contract can be honored."""
    from hosted import config_store

    config_store.hosted_runtime_policy()
    missing = []
    if not os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET", "").strip():
        missing.append("FEEDLING_RUNTIME_TOKEN_SECRET")
    if missing:
        raise RuntimeError(
            "托管前置缺失：" + ", ".join(missing) +
            "。缺少运行时前置会让当前托管策略无法安全处理消息；"
            "请在 backend 与对应 runtime worker 两侧设置后再启动。"
        )


def resolve_driver(config: dict | None) -> str:
    """返回 wire-compatible provider label：``claude`` / ``codex`` / ``pi``。

    Runtime V2 不按 label 分层或选择 loop；所有支持 provider 均进入同一 loop。
    无法托管时 raise ``UnsupportedProviderError``。"""
    provider = str((config or {}).get("provider") or "")
    driver = driver_for_provider(provider)
    if driver not in ("claude", "codex", "pi"):
        raise UnsupportedProviderError(provider or "unconfigured")
    return driver


def _runtime_block(driver: str) -> dict:
    return {"engine": "feedling_agent_runtime", "mode": "hosted_agent", "driver": driver, "version": 1}


def build_processing_response(user_row: dict, *, driver: str) -> tuple[dict, int]:
    """The client reads the eventual V2 result via Chat history/poll. Always
    202; never a `reply` field
    (the server holds only ciphertext under E2E)."""
    return (
        {
            "status": "processing",
            "reply_ready": False,
            "user_message": {"id": user_row.get("id"), "ts": user_row.get("ts")},
            "runtime": _runtime_block(driver),
        },
        202,
    )


# ---------------------------------------------------------------------------
# Resident-runtime helpers (restored for the dual-runtime send path).
#
# Under the ``dual`` hosted-runtime policy a per-user ``resident_cli``/``resident``
# fence routes the send to the out-of-process resident consumer instead of the V2
# worker pool. These helpers are the wedge guard + reply-wait machinery the
# resident branch depends on; they are inert under the ``v2_only`` policy (the V2
# path never calls them).
# ---------------------------------------------------------------------------

# Wedge guard: how stale the supervisor heartbeat may be before the backend
# treats hosting as down. Generous (≈6 ticks at the 15s default) so a brief
# supervisor restart doesn't 503 sends. Env-overridable for slow deployments.
_SUPERVISOR_HEARTBEAT_MAX_AGE_SEC = 90.0


def _heartbeat_max_age() -> float:
    raw = os.environ.get("FEEDLING_SUPERVISOR_HEARTBEAT_MAX_AGE_SEC", "").strip()
    if not raw:
        return _SUPERVISOR_HEARTBEAT_MAX_AGE_SEC
    try:
        return float(raw)
    except (TypeError, ValueError):
        return _SUPERVISOR_HEARTBEAT_MAX_AGE_SEC


def evaluate_supervisor_heartbeat(
    hb: dict | None, *, now: float, max_age: float,
    require_pi: bool = False
) -> tuple[bool, str]:
    """Pure verdict on whether a supervisor is actively hosting, from its
    heartbeat dict. Returns ``(live, reason)``; ``reason`` is "" when live.

    Not-live cases (the turn would park in ``processing`` with no consumer): no
    heartbeat at all, a malformed/absent ts, a stale heartbeat (supervisor dead
    or wedged), or host-all turned off on the supervisor side (the cross-service
    config divergence the backend's startup ``assert_hosting_ready`` cannot see).

    The in-CVM LiteLLM gateway is retired — a ``gateway`` key on the heartbeat
    dict (from an old supervisor) is ignored entirely.

    ``require_pi=True`` (pi-driven sends) additionally requires the runner to
    report ``pi`` — else a backend-pi-on / runner-pi-off drift would route the
    send to a runner that never spawned a pi consumer, parking it in
    ``processing``. An old runner whose heartbeat has no ``pi`` key reads as
    pi-off → 503 (the safe direction: clean fail, not stuck)."""
    if not isinstance(hb, dict):
        return (False, "no_supervisor_heartbeat")
    try:
        ts = float(hb.get("ts") or 0)
    except (TypeError, ValueError):
        return (False, "bad_supervisor_heartbeat")
    if ts <= 0:
        return (False, "bad_supervisor_heartbeat")
    age = now - ts
    if age > max_age:
        return (False, f"stale_supervisor_heartbeat_{int(age)}s")
    if not hb.get("host_all"):
        return (False, "supervisor_host_all_inactive")
    if require_pi and not hb.get("pi"):
        return (False, "supervisor_pi_disabled")
    return (True, "")


def evaluate_supervisor_instances(
    instances: list[dict] | None, *, now: float, max_age: float,
    require_pi: bool = False
) -> tuple[bool, str]:
    """Aggregate verdict across the per-owner heartbeat rows of a runner cluster.

    The cluster is live iff AT LEAST ONE fresh runner is actually hosting
    (``host_all``, plus ``pi`` when ``require_pi``). Capacity is NOT considered
    here: a runner at ``max_children`` still proves the cluster is up — the
    message simply parks in the DB until a runner with room polls it, so gating
    sends on capacity would wrongly 503 a healthy cluster.

    When no instance is live, the reported reason is taken from the freshest row
    so the operator sees the most current cluster state rather than an arbitrary
    stale one. Reuses the single-heartbeat verdict per row."""
    if not instances:
        return (False, "no_supervisor_heartbeat")
    not_live: list[tuple[dict, str]] = []
    for hb in instances:
        live, reason = evaluate_supervisor_heartbeat(
            hb, now=now, max_age=max_age, require_pi=require_pi)
        if live:
            return (True, "")
        not_live.append((hb, reason))

    def _ts(hb: dict) -> float:
        try:
            return float(hb.get("ts") or 0)
        except (TypeError, ValueError):
            return 0.0

    freshest = max(not_live, key=lambda pair: _ts(pair[0]))
    return (False, freshest[1])


def _instance_is_fresh(hb: dict, *, now: float, max_age: float) -> bool:
    """Whether a per-owner heartbeat row is recent enough to be authoritative."""
    try:
        ts = float(hb.get("ts") or 0)
    except (TypeError, ValueError):
        return False
    return ts > 0 and (now - ts) <= max_age


def check_supervisor_live(*, require_pi: bool = False,
                          now: float | None = None) -> tuple[bool, str]:
    """Evaluate whether any runner is hosting. Prefers the per-owner multi-instance
    heartbeats (multiple runners don't clobber each other); falls back to the
    legacy single-key heartbeat when the new table has no FRESH row — i.e. it is
    empty (pre-migration) OR holds only stale rows from dead/rolled-back runners,
    so an old supervisor still writing the legacy key is honoured. Fail-OPEN on a
    legacy-read DB error — the guard must never become a new outage vector; only a
    heartbeat that is present-and-not-live (or definitively absent) blocks a send.

    A FRESH instance row IS authoritative even when it reports not-hosting (e.g.
    host_all off): that's the real cluster state, so we do not fall back then.

    Pass ``require_pi=True`` for pi-driven providers so a runner that isn't running
    the pi driver is treated as not-live for them (avoids stuck ``processing``)."""
    now = time.time() if now is None else now
    max_age = _heartbeat_max_age()
    try:
        instances = db.list_supervisor_instance_heartbeats()
    except Exception as e:  # noqa: BLE001 — new table unreadable → try legacy key
        log.warning("supervisor instance heartbeats read failed; trying legacy key: %s", e)
        instances = []
    if any(_instance_is_fresh(hb, now=now, max_age=max_age) for hb in instances):
        return evaluate_supervisor_instances(
            instances, now=now, max_age=max_age, require_pi=require_pi)
    # No FRESH multi-instance row (empty, or only stale orphan rows): fall back to
    # the legacy single-key heartbeat a transitional/rolled-back runner may write.
    try:
        hb = db.read_supervisor_heartbeat()
    except Exception as e:  # noqa: BLE001 — DB hiccup → don't block sends
        log.warning("supervisor heartbeat read failed; routing send anyway (fail-open): %s", e)
        return (True, "")
    return evaluate_supervisor_heartbeat(hb, now=now, max_age=max_age, require_pi=require_pi)


def _is_assistant(row: dict) -> bool:
    return str(row.get("role") or "") in ("openclaw", "assistant", "agent")


def find_reply_row(store, user_message_id: str) -> dict | None:
    """The agent's reply to ``user_message_id``, or None if not answered yet.

    Prefers the precise link (the user row gains ``reply_message_id`` when a
    reply with ``reply_to_message_id`` is posted); falls back to scanning for an
    assistant row that points back at the user message.
    """
    messages = list(getattr(store, "chat_messages", []) or [])
    by_id = {str(m.get("id")): m for m in messages}
    user_row = by_id.get(str(user_message_id))
    if user_row:
        reply_id = str(user_row.get("reply_message_id") or "")
        if reply_id and reply_id in by_id:
            return by_id[reply_id]
    for m in messages:
        if _is_assistant(m) and str(m.get("reply_to_message_id") or "") == str(user_message_id):
            return m
    return None


def wait_for_reply(
    store,
    user_message_id: str,
    *,
    timeout: float = 8.0,
    poll_interval: float = 0.5,
    now=time.time,
    sleep=time.sleep,
) -> dict | None:
    """Poll the store for the agent's reply up to ``timeout`` seconds."""
    deadline = now() + timeout
    while True:
        row = find_reply_row(store, user_message_id)
        if row is not None:
            return row
        if now() >= deadline:
            return None
        sleep(poll_interval)


def build_ready_response(user_row: dict, reply_row: dict, *, driver: str) -> tuple[dict, int]:
    """The reply landed within the wait window. Still 202 (not 200/`ok`) and
    still no plaintext `reply`: we hand back the assistant message ref so the
    client can fetch+decrypt it immediately, but the text never transits the
    server. Avoids the misleading `200 ok` with no reply text."""
    return (
        {
            "status": "processing",
            "reply_ready": True,
            "user_message": {"id": user_row.get("id"), "ts": user_row.get("ts")},
            "assistant_message": {"id": reply_row.get("id"), "ts": reply_row.get("ts")},
            "runtime": _runtime_block(driver),
        },
        202,
    )


def handle_send(store, user_row: dict, driver: str, *, timeout: float = 8.0) -> tuple[dict, int]:
    """Delegate a flagged send to the agent runtime: the user message is already
    in the store; wait briefly and report whether the reply is ready (the client
    fetches the ciphertext reply via chat poll either way)."""
    reply = wait_for_reply(store, str(user_row.get("id") or ""), timeout=timeout)
    if reply is not None:
        return build_ready_response(user_row, reply, driver=driver)
    return build_processing_response(user_row, driver=driver)
