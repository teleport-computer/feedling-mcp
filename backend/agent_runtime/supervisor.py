"""Agent-runner supervisor — one resident consumer process per user.

The canonical consumer is ``tools/chat_resident_consumer.py`` (the VPS resident
consumer). This supervisor hosts it multi-tenant in the CVM: it holds a DB-backed
lease per user (``agent_runtime_instances``) so exactly one supervisor runs a
given user's consumer across workers/processes, spawns one resident consumer per
active user (driven in cli mode against claude/codex), heartbeats leases, and
reaps dead children.

``Supervisor.tick`` takes injected ``spawn_fn``/``alive_fn``/``kill_fn`` so the
orchestration is testable against the real lease table without spawning
processes. ``main()`` wires the real spawner + run loop.

Roster (P1): the users to service come from ``AGENT_RUNTIME_USERS`` (inline JSON)
or ``AGENT_RUNTIME_ROSTER`` (file). Each entry: a Feedling API key + a provider
key (plaintext, or ``provider_key_envelope`` decrypted JIT via the enclave) +
optional ``driver``/``cli_cmd``/``model``.

Env:
  DATABASE_URL          lease table                                  [required]
  FEEDLING_API_URL      backend base url
  FEEDLING_ENCLAVE_URL  enclave decrypt proxy (for provider-key envelope)
  AGENT_RUNTIME_USERS   inline JSON roster, or
  AGENT_RUNTIME_ROSTER  path to a JSON roster file
  AGENT_DATA_ROOT       per-user home root (default /agent-data)
  AGENT_LEASE_TTL_SEC   lease TTL / heartbeat budget (default 60)
  AGENT_TICK_INTERVAL_SEC  loop interval (default 15)
  AGENT_MAX_SPAWNS_PER_TICK  new consumers spawned per tick (default 8; 0=unlimited)
  AGENT_ENVELOPE_REFETCH_SEC  provider-key envelope cache TTL (default 300; 0=off)
  AGENT_RESOLVE_CONCURRENCY  per-tick roster-resolution fan-out (default 8; 1=serial)
  AGENT_RUNTIME_ISOLATION  process (default) | container
"""

from __future__ import annotations

import base64
import concurrent.futures
from datetime import datetime
import json
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

import httpx

# Put the backend dir on sys.path BEFORE importing backend modules. When this is
# launched as a script (``python backend/agent_runtime/supervisor.py`` from /app
# in the CVM image), sys.path[0] is the script's own dir, NOT backend/ — so
# ``import db`` / ``from core import …`` would fail unless we insert it first.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import db
from core import runtime_token
from agent_runtime import leases, litellm_gateway, spawners

log = logging.getLogger("feedling.agent_runtime.supervisor")

INTRODUCTION_JOB_KIND = "introduction"
INTRODUCTION_TRIGGER = "post_spawn_genesis"
INTRODUCTION_INTENT_LABEL = "post_respawn_introduction"
_INTRODUCTION_ACTIVE_STATUSES = {"pending", "claimed", "realizing"}


def parse_roster(raw) -> list[dict]:
    """Parse a roster (JSON string or list) into entries that have an api_key."""
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and str(e.get("api_key") or "").strip()]


class Supervisor:
    def __init__(
        self,
        *,
        owner: str,
        lease_ttl: float,
        data_root: str,
        spawn_fn,
        alive_fn,
        kill_fn=spawners._signal_kill,
        now=time.time,
        token_writer=None,
        api_url: str | None = None,
        enclave_url: str | None = None,
        introduction_enqueuer=None,
        max_spawns_per_tick: int = 0,
        max_children: int = 0,
    ) -> None:
        self.owner = owner
        self.lease_ttl = lease_ttl
        self.data_root = data_root.rstrip("/")
        self.spawn_fn = spawn_fn      # (entry, user_id, home) -> pid
        self.alive_fn = alive_fn      # (pid) -> bool
        self.kill_fn = kill_fn        # (pid) -> None
        self.token_writer = token_writer  # (user_id, home) -> None, or None (Stage D off)
        self.api_url = api_url or os.environ.get("FEEDLING_API_URL", "http://localhost:5001")
        self.enclave_url = enclave_url or os.environ.get("FEEDLING_ENCLAVE_URL", "")
        self.introduction_enqueuer = introduction_enqueuer or _enqueue_introduction_job_if_needed
        # Cold-start fan-out cap: at most N NEW consumers spawned per tick (0 =
        # unlimited, legacy behaviour). Spreads a many-user cold start across
        # ticks so the CVM isn't hit by dozens of simultaneous process forks.
        self.max_spawns_per_tick = int(max_spawns_per_tick or 0)
        # Steady-state per-runner capacity ceiling (0 = unlimited). Unlike the
        # per-tick spawn RATE cap above, this bounds the TOTAL live children so one
        # runner doesn't grab every user — the rest get acquired by other runners.
        self.max_children = int(max_children or 0)
        self._now = now
        self.children: dict[str, dict] = {}  # user_id -> {pid, entry, home}
        # Guards self.children against concurrent mutation by the main tick
        # (spawn/reap/respawn) and the dedicated renew thread (renew_live).
        # Reentrant so a method already holding it can call another that takes it.
        self._lock = threading.RLock()

    def _write_token(self, user_id: str, home: str) -> None:
        if self.token_writer is None:
            return
        try:
            self.token_writer(user_id, home)
        except Exception as e:  # noqa: BLE001 — a token-refresh failure must not crash the tick
            log.warning("runtime-token refresh failed for %s: %s", user_id, e)

    def _home(self, user_id: str) -> str:
        return f"{self.data_root}/users/{user_id}"

    def _enqueue_introduction(self, user_id: str, entry: dict) -> None:
        try:
            job = self.introduction_enqueuer(
                user_id,
                entry,
                api_url=self.api_url,
                enclave_url=self.enclave_url,
                now=self._now,
            )
            if job:
                log.info(
                    "enqueued post-respawn introduction for %s job=%s",
                    user_id,
                    job.get("job_id", ""),
                )
        except Exception as e:  # noqa: BLE001 — introduction is best-effort, spawn must continue
            log.warning("introduction enqueue failed for %s: %s", user_id, e)

    def tick(self, roster: list[dict], *, pre_spawn=None) -> None:
        """One supervision pass: heartbeat live children, reap dead ones, drop
        children whose user left the roster, and acquire+spawn for any user we
        don't already run.

        ``pre_spawn(new_user_ids)`` (optional) is invoked ONCE after all of this
        tick's new leases are acquired but BEFORE any new consumer is spawned, so a
        side-channel a consumer needs at startup (e.g. its LiteLLM ``gw-<uid>``
        gateway route) can be configured first. It is best-effort — a failure logs
        and the spawns still proceed."""
        # Reap children whose user is no longer in the (re-derived) roster — e.g.
        # a user who disabled hosting. Kill + release so disable takes effect this
        # tick rather than lingering until lease expiry.
        roster_uids = {str(e.get("user_id") or "") for e in roster}
        with self._lock:
            tracked = list(self.children.items())
        for user_id, child in tracked:
            if user_id not in roster_uids:
                log.info("user %s left the roster; terminating its consumer", user_id)
                self.kill_fn(child["pid"])
                leases.release(user_id, self.owner, now=self._now())
                with self._lock:
                    self.children.pop(user_id, None)

        spawned_this_tick = 0
        newly_acquired: list[tuple[str, dict, str]] = []  # (user_id, entry, home)
        for entry in roster:
            user_id = str(entry.get("user_id") or "")
            if not user_id:
                continue
            with self._lock:
                child = self.children.get(user_id)

            if child and self.alive_fn(child["pid"]):
                if not leases.renew(user_id, self.owner, ttl=self.lease_ttl,
                                    pid=child["pid"], status="running", now=self._now()):
                    # Lost the lease (another supervisor took over after an expiry
                    # window). Kill our now-orphaned child so we don't double-run
                    # alongside the new owner ("exactly one consumer per user").
                    log.warning("lost lease for %s; terminating orphaned child", user_id)
                    self.kill_fn(child["pid"])
                    with self._lock:
                        self.children.pop(user_id, None)
                elif _spawn_identity(entry) != _spawn_identity(child["entry"]):
                    # Config changed for a still-running user (driver/provider/model/
                    # key) — the consumer's env + home are stale. Respawn in place;
                    # we keep our live lease (just renewed) so no reacquire needed.
                    log.info("config changed for %s; respawning consumer", user_id)
                    home = child["home"]
                    # Serialize the whole kill→spawn→renew→swap against the renew
                    # thread. Otherwise renew_live could snapshot the OLD child, see
                    # its (just-killed) pid dead, and release the lease this branch is
                    # renewing for the replacement pid — leaving the new consumer
                    # lease-less and reaped next tick. Holding the lock across the swap
                    # keeps renew_live's `is child` check observing the new child.
                    with self._lock:
                        self.kill_fn(child["pid"])
                        pid = self.spawn_fn(entry, user_id, home)
                        self._write_token(user_id, home)
                        leases.renew(user_id, self.owner, ttl=self.lease_ttl, pid=pid,
                                     status="running", driver=entry.get("driver"),
                                     now=self._now())
                        self.children[user_id] = {"pid": pid, "entry": entry, "home": home}
                    self._enqueue_introduction(user_id, entry)
                else:
                    self._write_token(user_id, child["home"])  # refresh short-lived token
                continue

            if child:  # tracked but dead → reap
                log.info("child for %s exited; releasing lease", user_id)
                leases.release(user_id, self.owner, now=self._now())
                with self._lock:
                    self.children.pop(user_id, None)

            if not _genesis_ready_to_spawn(user_id):
                # "先 genesis 后 spawn" (spec §5): don't boot a blank consumer while
                # an import genesis is still distilling persona/facts. Fresh-start
                # (no genesis) and done/failed both fall through to spawn.
                log.info("genesis in progress for %s; deferring spawn this tick", user_id)
                continue
            with self._lock:
                # Count this tick's not-yet-spawned acquisitions toward the cap so
                # a single tick can't blow past max_children before they're tracked.
                at_capacity = (self.max_children
                               and len(self.children) + len(newly_acquired) >= self.max_children)
            if at_capacity:
                # At capacity: don't acquire a new user — leave it for another runner.
                # Existing children above were already renewed, never force-reaped.
                continue
            if self.max_spawns_per_tick and spawned_this_tick >= self.max_spawns_per_tick:
                # Per-tick spawn cap reached — spawn the remaining users next tick
                # (don't even acquire, so another supervisor could take them).
                continue
            home = self._home(user_id)
            if not leases.acquire(user_id, driver=entry.get("driver", "claude"),
                                  runtime_home=home, lease_owner=self.owner,
                                  ttl=self.lease_ttl, now=self._now()):
                continue  # another supervisor holds a live lease
            # Acquired — defer the spawn so the gateway route can be configured for
            # the whole batch first (route-before-spawn; avoids a queued first turn
            # hitting a missing gw-<uid> route).
            spawned_this_tick += 1
            newly_acquired.append((user_id, entry, home))

        # Configure any side-channel the new consumers need at startup (gateway
        # routes) for the now-owned batch BEFORE starting them. Lease-scoped: only
        # users we just acquired this tick. Best-effort — a blip must not strand
        # the spawn; the post-tick reconcile still converges.
        if newly_acquired and pre_spawn is not None:
            try:
                pre_spawn([uid for uid, _, _ in newly_acquired])
            except Exception as e:  # noqa: BLE001
                log.warning("pre-spawn reconcile failed; spawning anyway: %s", e)

        for user_id, entry, home in newly_acquired:
            pid = self.spawn_fn(entry, user_id, home)
            self._write_token(user_id, home)  # initial short-lived token for the child
            leases.renew(user_id, self.owner, ttl=self.lease_ttl, pid=pid,
                         status="running", now=self._now())
            with self._lock:
                self.children[user_id] = {"pid": pid, "entry": entry, "home": home}
            log.info("spawned resident consumer for %s (pid=%s, home=%s)", user_id, pid, home)
            self._enqueue_introduction(user_id, entry)

    def renew_live(self) -> None:
        """Renew leases for every currently-live child — and ONLY that.

        Runs from a dedicated thread (``_renew_loop``) so lease freshness is
        decoupled from the main loop's discover/resolve/spawn reconcile, which at
        host-all scale can take far longer than the lease TTL. Reconcile work
        (spawn new users, respawn on config change) stays in ``tick``; this path
        never spawns, so a slow resolve can't starve heartbeats.

        The DB renew runs as ONE batched statement OUTSIDE the lock (the lock
        only guards the children-dict snapshot and any reap), so a slow renew
        never blocks the tick and the pass stays well under the TTL regardless of
        fleet size. A child we find dead, or whose lease we've lost to a live
        other supervisor, is reaped here just as ``tick`` would — keeping
        "exactly one consumer per user"."""
        with self._lock:
            snapshot = list(self.children.items())
        # Reap dead children first so we only renew live ones.
        live: list[tuple[str, dict]] = []
        for user_id, child in snapshot:
            if not self.alive_fn(child["pid"]):
                with self._lock:
                    if self.children.get(user_id) is child:
                        leases.release(user_id, self.owner, now=self._now())
                        self.children.pop(user_id, None)
                continue
            live.append((user_id, child))
        if not live:
            return
        held = leases.renew_many(
            [(user_id, child["pid"]) for user_id, child in live],
            self.owner, ttl=self.lease_ttl, now=self._now())
        for user_id, child in live:
            if user_id in held:
                continue
            # Lost the lease (a live other supervisor took over). Kill our orphaned
            # child so two consumers don't both run. A brief self-expiry that
            # nobody took is reclaimed by renew_many, not killed here.
            log.warning("renew lost lease for %s; terminating orphaned child", user_id)
            with self._lock:
                if self.children.get(user_id) is child:
                    self.kill_fn(child["pid"])
                    self.children.pop(user_id, None)

    def shutdown(self) -> None:
        with self._lock:
            items = list(self.children.items())
            self.children.clear()
        for user_id, child in items:
            self.kill_fn(child["pid"])
            leases.release(user_id, self.owner)


# ---- real-process wiring ----


def _load_roster() -> list[dict]:
    inline = os.environ.get("AGENT_RUNTIME_USERS", "").strip()
    if inline:
        return parse_roster(inline)
    path = os.environ.get("AGENT_RUNTIME_ROSTER", "").strip()
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return parse_roster(fh.read())
    return []


def _whoami(api_url: str, api_key: str) -> str:
    """Resolve a roster entry's user_id (lease key) via /v1/users/whoami."""
    try:
        resp = httpx.get(f"{api_url.rstrip('/')}/v1/users/whoami",
                         headers={"X-API-Key": api_key}, timeout=10)
        resp.raise_for_status()
        return str(resp.json().get("user_id") or "")
    except Exception as e:  # noqa: BLE001
        log.warning("whoami failed for a roster entry: %s", e)
        return ""


def _auth_headers(*, api_key: str = "", runtime_token: str = "") -> dict:
    """Auth header for a backend/enclave call. A runtime token (Stage D) is
    preferred when present — it lets the supervisor act for a DB-discovered user
    WITHOUT holding that user's api_key (zero-roster host-all). Both the backend
    whoami and the enclave ``/v1/envelope/decrypt`` accept either credential."""
    if runtime_token:
        return {"X-Feedling-Runtime-Token": runtime_token}
    return {"X-API-Key": api_key}


def _decrypt_provider_key(enclave_url: str, api_key: str = "", envelope: dict | None = None,
                          *, runtime_token: str = "") -> str:
    """JIT-decrypt a provider-key envelope via the enclave (purpose reuses the
    model_api config scheme). Plaintext is handed to the child via env. Auth is
    the api_key, or a runtime token (Stage D zero-roster) when provided."""
    try:
        resp = httpx.post(f"{enclave_url.rstrip('/')}/v1/envelope/decrypt",
                          headers=_auth_headers(api_key=api_key, runtime_token=runtime_token),
                          json={"envelope": envelope, "purpose": "model_api_provider_key"},
                          timeout=20, verify=False)
        resp.raise_for_status()
        return base64.b64decode(resp.json()["plaintext_b64"]).decode("utf-8")
    except Exception as e:  # noqa: BLE001
        log.error("provider key decrypt failed for a roster entry: %s", e)
        return ""


def _identity_signature_empty(value) -> bool:
    if isinstance(value, list):
        return not any(str(item or "").strip() for item in value)
    return not str(value or "").strip()


def _needs_introduction_identity(identity: dict | None) -> bool:
    if not isinstance(identity, dict):
        return False
    status = str(identity.get("decrypt_status") or "").strip()
    if status and status != "ok":
        return False
    return (
        not str(identity.get("self_introduction") or "").strip()
        and _identity_signature_empty(identity.get("signature"))
    )


def _fetch_identity_plain_for_intro(entry: dict, *, api_url: str, enclave_url: str) -> tuple[dict | None, str]:
    if not str(enclave_url or "").strip():
        return None, "enclave_url_missing"
    headers = _auth_headers(
        api_key=str(entry.get("api_key") or ""),
        runtime_token=str(entry.get("runtime_token") or ""),
    )
    if not headers:
        return None, "auth_missing"
    try:
        resp = httpx.get(
            f"{enclave_url.rstrip('/')}/v1/identity/get",
            headers=headers,
            timeout=10,
            verify=False,
        )
        if resp.status_code == 404:
            return None, "identity_not_found"
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:  # noqa: BLE001
        return None, f"identity_fetch_failed:{type(e).__name__}"
    identity = body.get("identity") if isinstance(body, dict) else None
    if not isinstance(identity, dict):
        return None, "identity_not_initialized"
    return identity, ""


def _has_active_introduction_job(store) -> bool:
    try:
        jobs = store.list_proactive_jobs(since_epoch=0, limit=0)
    except Exception as e:  # noqa: BLE001
        log.warning("introduction active-job scan failed for %s: %s", getattr(store, "user_id", ""), e)
        return True
    for job in jobs or []:
        if str((job or {}).get("job_kind") or "").strip() != INTRODUCTION_JOB_KIND:
            continue
        status = str((job or {}).get("status") or "pending").strip()
        if status in _INTRODUCTION_ACTIVE_STATUSES:
            return True
    return False


def _build_introduction_job(*, now: float) -> dict:
    from core import util
    job_id = util._new_public_id("pj")
    return {
        "job_id": job_id,
        "schema_version": 2,
        "ts": float(now),
        "created_at": datetime.fromtimestamp(float(now)).isoformat(),
        "wake_id": job_id,
        "source": "agent_initiated_proactive",
        "status": "pending",
        "intent_label": INTRODUCTION_INTENT_LABEL,
        "context_hint": "",
        "connections": [],
        "connection": {},
        "frame_ids": [],
        "device_event_ids": [],
        "current_app": "",
        "trigger": INTRODUCTION_TRIGGER,
        "job_kind": INTRODUCTION_JOB_KIND,
        "manual": False,
        "forced": False,
        "user_state": "",
        "ai_state": "",
        "broadcast_state": "",
        "wake_kind": "introduction",
        "screen_context_available": False,
        "agent_action": "",
        "agent_action_status": "",
    }


def _enqueue_introduction_job_if_needed(
    user_id: str,
    entry: dict,
    *,
    api_url: str,
    enclave_url: str,
    now=time.time,
    get_store_fn=None,
) -> dict | None:
    identity, reason = _fetch_identity_plain_for_intro(entry, api_url=api_url, enclave_url=enclave_url)
    if not _needs_introduction_identity(identity):
        if reason:
            log.debug("introduction skipped for %s: %s", user_id, reason)
        return None
    if get_store_fn is None:
        from core.store import get_store
        get_store_fn = get_store
    store = get_store_fn(user_id)
    if _has_active_introduction_job(store):
        return None
    clock = now() if callable(now) else float(now)
    return store.append_proactive_job(_build_introduction_job(now=clock))


def _fetch_key_envelope(api_url: str, api_key: str = "", *, runtime_token: str = "") -> dict | None:
    """Fetch the user's OWN provider-key envelope ciphertext from the backend
    (Stage B: ``GET /v1/model_api/key_envelope``), so a roster need only carry
    api_keys (or, Stage D, nothing — a runtime token authenticates instead).
    Returns the envelope dict, or None if unconfigured/unreachable."""
    try:
        resp = httpx.get(f"{api_url.rstrip('/')}/v1/model_api/key_envelope",
                         headers=_auth_headers(api_key=api_key, runtime_token=runtime_token), timeout=10)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        env = resp.json().get("api_key_envelope")
        return env if isinstance(env, dict) else None
    except Exception as e:  # noqa: BLE001
        log.warning("key_envelope fetch failed for a roster entry: %s", e)
        return None


def _resolve_roster(roster: list[dict]) -> list[dict]:
    """Fill each entry's ``user_id`` (whoami) and resolve its provider key.

    Precedence: an explicit roster ``provider_key`` (dev) > a roster-supplied
    ``provider_key_envelope`` > self-fetching the user's own envelope from the
    backend (Stage B). The envelope is enclave-decrypted JIT; plaintext is handed
    to the child via env and never persisted."""
    api_url = os.environ.get("FEEDLING_API_URL", "http://localhost:5001")
    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "")
    resolved = []
    for entry in roster:
        user_id = entry.get("user_id") or _whoami(api_url, entry["api_key"])
        if not user_id:
            log.warning("skipping roster entry: could not resolve user_id")
            continue
        out = {**entry, "user_id": user_id}
        if not out.get("provider_key"):
            env = entry.get("provider_key_envelope")
            if not env:
                env = _fetch_key_envelope(api_url, entry["api_key"])
            if env and enclave_url:
                key = _decrypt_provider_key(enclave_url, entry["api_key"], env)
                if key:
                    out["provider_key"] = key
            out.pop("provider_key_envelope", None)
        resolved.append(out)
    return resolved


def _discover_enabled(include_gateway: bool = False) -> dict[str, dict]:
    """Map ``user_id -> {"driver", "provider", "model", "base_url"}`` for users
    whose ``model_api`` config is test_ok and uses a fit provider (Stage C+).
    No per-user flag required — aligned with hosted/agent_runtime_cutover.resolve_driver.
    ``include_gateway`` mirrors whether the LiteLLM gateway is running — when off,
    gateway-only providers are excluded so they aren't spawned against a proxy that
    isn't there."""
    return {u["user_id"]: {"driver": u["driver"], "provider": u.get("provider", ""),
                           "model": u.get("model", ""), "base_url": u.get("base_url", ""),
                           "supports_responses": bool(u.get("supports_responses", False))}
            for u in db.list_agent_runtime_enabled_users(include_gateway=include_gateway)}


def _apply_discovery(roster: list[dict], enabled: dict[str, dict]) -> list[dict]:
    """Filter the (credential-bearing) roster to users the backend has enabled,
    taking driver/provider/model/base_url from the backend flag — the
    /v1/model_api/driver setter is the gradual-migration control plane. Roster
    still supplies api_keys until Stage D's runtime-token. Entries must already
    have ``user_id`` (whoami)."""
    out = []
    for entry in roster:
        uid = entry.get("user_id")
        info = enabled.get(uid)
        if info is not None:
            out.append({**entry, "driver": info["driver"],
                        "provider": info.get("provider", ""),
                        "model": info.get("model", ""),
                        "base_url": info.get("base_url", ""),
                        "supports_responses": bool(info.get("supports_responses", False))})
    return out


# How long a cached provider-key envelope is reused before the supervisor
# refetches it from the backend. The envelope ciphertext changes only when the
# user rotates their provider key (rare), so a multi-minute TTL turns the
# per-tick, per-user backend round-trip (the dominant cost of a host-all lap at
# ~50 users) into one fetch every few minutes. A rotation takes up to this long
# to apply — acceptable. 0 disables the cache (refetch every tick, legacy).
_ENVELOPE_REFETCH_DEFAULT_SEC = 300.0
# Bounded fan-out for per-user resolution. The fetch/decrypt are independent and
# I/O-bound (httpx releases the GIL), so a small pool collapses a cold-cache lap
# from N×serial to N/W. Kept modest because the enclave decrypt is single-
# threaded upstream — too many concurrent decrypts just queue there.
_RESOLVE_CONCURRENCY_DEFAULT = 8


def _resolve_one(uid: str, info: dict, *, mint_token, api_url: str, enclave_url: str,
                 cache: dict, refetch_sec: float, clock: float) -> dict | None:
    """Resolve a single discovered user into a credential-bearing entry.

    Isolated so a single user's mint/fetch/decrypt hang or failure (a banned
    gateway upstream, an enclave blip) returns None and is skipped, never
    aborting the rest of the pass. The HTTP calls carry their own timeouts
    (fetch 10s, decrypt 20s). Safe to run concurrently across users: each call
    only touches its own ``cache[uid]`` slot."""
    try:
        tok = mint_token(uid)
        cached = cache.get(uid)
        # T1.2 — reuse the cached envelope within the refetch TTL instead of a
        # backend round-trip every tick. A transient refetch failure (None) keeps
        # the last good envelope so a healthy consumer isn't respawned keyless.
        env = None
        if (refetch_sec and cached and cached.get("env") is not None
                and clock - cached.get("env_at", 0.0) < refetch_sec):
            env = cached["env"]
        else:
            fetched = _fetch_key_envelope(api_url, runtime_token=tok)
            if fetched is not None:
                env = fetched
                slot = cache.setdefault(uid, {})
                slot["env"] = fetched
                slot["env_at"] = clock
            elif cached and cached.get("env") is not None:
                env = cached["env"]

        provider_key = ""
        if env and enclave_url:
            sig = json.dumps(env, sort_keys=True)
            if cached and cached.get("sig") == sig:
                provider_key = cached["provider_key"]
            else:
                decrypted = _decrypt_provider_key(enclave_url, envelope=env, runtime_token=tok)
                if decrypted:
                    provider_key = decrypted
                    slot = cache.setdefault(uid, {})
                    slot["sig"] = sig
                    slot["provider_key"] = decrypted
                elif cached:
                    # Decrypt failed (transient) on a changed envelope — keep the last
                    # good key so a healthy consumer isn't respawned keyless this tick.
                    provider_key = cached.get("provider_key", "")
        elif cached:
            # Envelope unavailable (transient) — fall back to the last good key
            # rather than dropping it (which _spawn_identity reads as a config change).
            provider_key = cached.get("provider_key", "")

        entry = {"user_id": uid, "driver": info["driver"], "provider": info.get("provider", ""),
                 "model": info.get("model", ""), "base_url": info.get("base_url", ""),
                 "supports_responses": bool(info.get("supports_responses", False))}
        if provider_key:
            entry["provider_key"] = provider_key
        # Carry the freshly-minted runtime token so ProcessSpawner.spawn can decrypt
        # the genesis_persona blob at spawn time (zero-roster has no api_key — cutover
        # gate 3 P0). Minted before spawn (this tick), so timing is solved. Excluded
        # from _spawn_identity, so per-tick rotation does not bounce the consumer.
        if tok:
            entry["runtime_token"] = tok
        return entry
    except Exception as e:  # noqa: BLE001
        log.warning("resolve_discovered failed for %s; skipping this tick: %s", uid, e)
        return None


def _resolve_discovered(enabled: dict, *, mint_token, api_url: str, enclave_url: str,
                        cache: dict, now=None) -> list[dict]:
    """Stage D zero-roster: build credential-bearing entries for DB-discovered
    users WITHOUT any api_key. For each user_id the supervisor mints a short-lived
    runtime token, self-fetches the provider-key envelope and enclave-decrypts it
    JIT (auth = token). ``cache`` (user_id -> {"sig","provider_key","env","env_at"})
    avoids re-fetching AND re-decrypting an unchanged envelope every tick. Entries
    carry no api_key — the consumer authenticates with the token file the
    supervisor writes per tick. Users are resolved concurrently across a bounded
    pool so a cold cache (fresh supervisor) doesn't serialize into a multi-minute
    lap."""
    clock = (now or time.time)()
    refetch_sec = float(os.environ.get("AGENT_ENVELOPE_REFETCH_SEC", _ENVELOPE_REFETCH_DEFAULT_SEC))
    workers = max(1, int(os.environ.get("AGENT_RESOLVE_CONCURRENCY", _RESOLVE_CONCURRENCY_DEFAULT)))
    items = list(enabled.items())
    if not items:
        return []

    def _one(pair):
        uid, info = pair
        return _resolve_one(uid, info, mint_token=mint_token, api_url=api_url,
                            enclave_url=enclave_url, cache=cache,
                            refetch_sec=refetch_sec, clock=clock)

    if workers == 1 or len(items) == 1:
        results = [_one(p) for p in items]
    else:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(workers, len(items)),
                thread_name_prefix="resolve") as pool:
            results = list(pool.map(_one, items))
    return [r for r in results if r is not None]


def _post_verify_loop(api_url: str, headers: dict) -> bool:
    """POST /v1/chat/verify_loop — a synthetic ping the consumer answers, which
    flips the user's ``chat_loop_verified`` and opens the bootstrap gate. Returns
    whether the verify passed (an agent reply landed)."""
    try:
        resp = httpx.post(f"{api_url.rstrip('/')}/v1/chat/verify_loop",
                          headers=headers, json={"timeout_sec": 30}, timeout=40)
        resp.raise_for_status()
        return bool(resp.json().get("passing"))
    except Exception as e:  # noqa: BLE001
        log.warning("autoverify post failed: %s", e)
        return False


# Backoff (seconds) between verify_loop attempts, indexed by consecutive failure
# count (capped at the last entry). A user that cannot pass yet — e.g. still at
# `needs_identity`, where the verify reply is gate-rejected — must NOT be re-probed
# every tick; this spaces retries out so a genuinely-advancing user still gets
# verified soon while a stuck one generates little traffic.
_AUTOVERIFY_BACKOFF = [0.0, 30.0, 120.0, 600.0, 1800.0]


def _maybe_autoverify(user_id: str, *, mint_token, api_url: str, state: dict,
                      post_verify=_post_verify_loop, now=time.time) -> None:
    """Open the bootstrap gate for a freshly-hosted user by running verify_loop.
    ``state`` (user_id -> {"done", "fails", "next"}) makes it idempotent AND backed
    off: a passed user is marked ``done`` and never re-probed; a failed attempt
    schedules the next try with increasing backoff instead of every tick."""
    s = state.get(user_id)
    if s is not None and s.get("done"):
        return
    t = now()
    if s is not None and t < s.get("next", 0.0):
        return                                  # within the backoff window
    if post_verify(api_url, _auth_headers(runtime_token=mint_token(user_id))):
        state[user_id] = {"done": True}
        return
    fails = (s.get("fails", 0) if s else 0) + 1
    delay = _AUTOVERIFY_BACKOFF[min(fails, len(_AUTOVERIFY_BACKOFF) - 1)]
    state[user_id] = {"done": False, "fails": fails, "next": t + delay}


# A host user is blocked from spawning only while an import genesis is actively
# running. No genesis_state blob = fresh start (never uploaded) → allow. done/failed
# → allow (failed degrades to a normal spawn, never deadlocks the user). Coarse
# status is maintained by Codex's genesis via db.set_blob(user_id,'genesis_state').
_GENESIS_BLOCKING_STATUS = frozenset({"uploaded", "finalizing", "processing"})


def _genesis_status_blocks_spawn(blob) -> bool:
    """Pure gate logic (no DB) — True only when a FOUNDING genesis is in progress.
    A background ``companion_persona_backfill`` writes the same in-progress status but
    must NOT block spawn: the user boots with the identity/tools baseline and picks up
    voice via the persona_version respawn once the blob lands (cutover gate 4 — avoids
    "POST backfill before spawn wedges this user's spawn")."""
    if not isinstance(blob, dict):
        return False  # no genesis underway → fresh start, allow
    if str(blob.get("source_kind") or "").strip().lower() == "companion_persona_backfill":
        return False  # voice top-up, not a founding genesis — never block spawn
    return str(blob.get("status") or "").strip().lower() in _GENESIS_BLOCKING_STATUS


def _genesis_ready_to_spawn(user_id: str) -> bool:
    """Gate read: block spawn only while this user's import genesis is in progress.
    Read errors → allow (don't wedge spawning on a transient DB hiccup). Local ``db``
    import keeps this module pure-unit importable without a DB/PG dependency."""
    try:
        import db  # local import: avoid a module-level DB dep for pure-unit tests
        blob = db.get_blob(user_id, "genesis_state")
    except Exception as e:
        log.warning("genesis_state read failed for %s; allowing spawn: %s", user_id, e)
        return True
    return not _genesis_status_blocks_spawn(blob)


# Last-good persona digest per user. A transient DB read failure must NOT flip
# persona_version to '' (that would bounce a healthy consumer, then bounce it again
# when the read recovers). Only a SUCCESSFUL read updates the cache; on exception we
# return the last-good value so _spawn_identity stays stable through DB blips.
_PERSONA_VERSION_CACHE: dict[str, str] = {}


def _persona_version(user_id: str) -> str:
    """genesis_persona content digest (sha256) for respawn fingerprinting, or ''.
    Read-only, no decrypt — the digest is plaintext metadata on the blob. Empty when
    legitimately absent (no genesis / not backfilled yet), so a user without persona
    has a stable '' and isn't bounced. On a transient DB failure we return the last
    successfully-read value (not '') to avoid respawn flapping (cutover gate 4 C)."""
    if not user_id:
        return ""
    try:
        import db  # local import: keep this module pure-unit importable
        blob = db.get_blob(user_id, "genesis_persona")
    except Exception as e:
        last = _PERSONA_VERSION_CACHE.get(user_id, "")
        log.warning("persona_version read failed for %s; keeping last-good %r: %s",
                    user_id, last, e)
        return last
    version = str(blob.get("sha256") or "") if isinstance(blob, dict) else ""
    _PERSONA_VERSION_CACHE[user_id] = version
    return version


# Cutover gate 4 B-3: lazy persona backfill. For a host user with no genesis_persona
# blob, POST the backfill endpoint so the worker distills voice from their identity.
# Off by default (cutover-time switch). Bounded so it NEVER blocks the tick.
_BACKFILL_ATTEMPTED: dict[str, float] = {}


def _lazy_persona_backfill_enabled() -> bool:
    return os.environ.get("FEEDLING_PERSONA_BACKFILL_LAZY", "").strip().lower() in ("1", "true", "yes")


def _run_lazy_persona_backfill(roster: list[dict], *, secret_raw: str, owner: str,
                               api_url: str, now: float) -> None:
    """POST /v1/genesis/persona_backfill for up to N no-blob users per tick, with a
    per-user cooldown. Uses a dedicated short-TTL ``["genesis","envelope_decrypt"]``
    token — NOT the consumer's spawn token (genesis is background maintenance, not a
    long-lived agent permission — Codex review). Best-effort: errors logged, never
    raised, so a slow/failed backfill POST never wedges the supervisor tick."""
    if not secret_raw:
        return
    secret = secret_raw.encode("utf-8")
    cooldown = float(os.environ.get("FEEDLING_PERSONA_BACKFILL_COOLDOWN_SEC", "3600"))
    cap = int(os.environ.get("FEEDLING_PERSONA_BACKFILL_MAX_PER_TICK", "2"))
    done = 0
    for entry in roster:
        if done >= cap:
            break
        if str(entry.get("persona_version") or ""):
            continue  # already has a persona blob → nothing to backfill
        uid = str(entry.get("user_id") or "")
        # `uid in dict` first so the very first attempt isn't skipped under a small
        # test clock (now - 0.0 < cooldown); real time.time() never hits this.
        if not uid or (uid in _BACKFILL_ATTEMPTED and now - _BACKFILL_ATTEMPTED[uid] < cooldown):
            continue
        _BACKFILL_ATTEMPTED[uid] = now  # record before the call so a failure still backs off
        try:
            token = runtime_token.mint(secret, user_id=uid, runtime_instance_id=owner,
                                       scope=["genesis", "envelope_decrypt"], ttl=300)
            # Short timeout so a stuck backend can't wedge the tick (worst case =
            # cap × timeout); the endpoint is normally fast. Backgrounding is a later
            # hardening if this proves too tight under load.
            resp = httpx.post(f"{api_url.rstrip('/')}/v1/genesis/persona_backfill",
                              headers={"X-Feedling-Runtime-Token": token}, timeout=5)
            log.info("persona backfill %s → http=%s %s", uid, resp.status_code, resp.text[:120])
        except Exception as e:  # noqa: BLE001
            log.warning("persona backfill POST failed for %s: %s", uid, e)
        done += 1


def _spawn_identity(entry: dict) -> tuple:
    """The spawn-determining fields of a roster entry — when any changes, the
    running consumer's env/home is stale and it must be respawned. Mirrors what
    ``spawners.consumer_env`` / ``agent_home_files`` consume. The upstream
    ``provider_key`` is EXCLUDED for gateway users (it goes to LiteLLM, not the
    consumer env), so rotating it doesn't bounce the consumer. ``persona_version``
    (genesis_persona digest) IS included so a voice backfill/Dream re-seeds the
    persona prompt via a natural respawn (gate 4 C)."""
    driver = (entry.get("driver") or "claude").strip().lower()
    gateway = spawners._codex_transport(entry) == "gateway"
    return (
        entry.get("api_key") or "",
        driver,
        entry.get("cli_cmd") or "",
        entry.get("provider") or "",
        entry.get("model") or "",
        "" if gateway else (entry.get("provider_key") or ""),
        entry.get("persona_version") or "",
    )


def _gateway_entries(roster: list[dict]) -> list[dict]:
    """Codex users that must be bridged through the in-CVM LiteLLM gateway
    (gemini/openrouter/openai_compatible). Each carries the REAL upstream
    provider/model/key for building the per-user LiteLLM routing."""
    out = []
    for e in roster:
        if spawners._codex_transport(e) == "gateway":
            out.append({
                "user_id": e["user_id"],
                "provider": e.get("provider", ""),
                "model": e.get("model") or "",
                "base_url": e.get("base_url") or "",
                "supports_responses": bool(e.get("supports_responses", False)),
                "provider_key": e.get("provider_key") or "",
            })
    return out


def _owned_gateway_entries(gateway_entries: list[dict], owned) -> list[dict]:
    """Scope the gateway routing entries to ONLY users this runner currently holds
    a lease/child for, so a user owned by another runner never enters this runner's
    LiteLLM config (multi-node: provider keys don't fan out to every runner).

    ``owned`` is the set of owned user_ids (or the children mapping, whose keys are
    those ids). Callers should pass a SNAPSHOT taken under ``Supervisor._lock`` —
    the renew thread mutates the live children dict concurrently.

    Each entry's key is taken AS-IS from ``gateway_entries`` (built from the freshly
    resolved roster this tick), NOT from the stored child entry — so an upstream
    key rotation reaches LiteLLM on the next tick even though it deliberately does
    not respawn the consumer (``_spawn_identity`` excludes the gateway key)."""
    owned_ids = set(owned)
    return [g for g in gateway_entries if g.get("user_id") in owned_ids]


def _drop_gateway_users(roster: list[dict]) -> list[dict]:
    """Remove codex users that would need the LiteLLM gateway — used when the
    gateway is DISABLED. Without a running proxy, spawning them as gateway codex
    yields a config pointing at nothing (and usually no gateway key), so they must
    be dropped rather than half-spawned. Native (openai) + claude users stay."""
    kept = [e for e in roster if spawners._codex_transport(e) != "gateway"]
    dropped = [e.get("user_id") for e in roster if spawners._codex_transport(e) == "gateway"]
    if dropped:
        log.warning("litellm gateway disabled — dropping %d gateway codex user(s): %s",
                    len(dropped), dropped)
    return kept


def _wire_gateway_models(roster: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (roster, gateway_entries). Gateway codex entries are rewritten so
    codex REQUESTS the ``gw-<uid>`` model (which LiteLLM maps to the real upstream
    model+key); the returned gateway_entries retain the real model for building
    the LiteLLM config. Non-gateway entries pass through unchanged."""
    gateways = _gateway_entries(roster)
    if not gateways:
        return roster, []
    gateway_uids = {g["user_id"] for g in gateways}
    wired = [
        {**e, "model": litellm_gateway.gateway_model_id(e["user_id"])}
        if e.get("user_id") in gateway_uids and spawners._codex_transport(e) == "gateway"
        else e
        for e in roster
    ]
    return wired, gateways


def _effective_roster(base_roster: list[dict], *, autodiscover: bool,
                      gateway_enabled: bool,
                      host_all_discovered: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """Compute the roster to actually run this tick + the gateway routing entries.

    ``base_roster`` carries credentials (resolved once at boot). Each tick we:
    optionally intersect it with the backend-enabled set (autodiscover — the
    gradual-migration control plane); drop gateway-only codex users when the
    gateway is off (no proxy to reach); and when the gateway is on, rewrite those
    users' requested model to ``gw-<uid>`` (returning the real models as gateway
    entries for the LiteLLM config). An empty result is fine — the supervisor
    idles rather than exiting.

    ``host_all_discovered`` (Stage D host-all) supplies pre-credentialed entries
    resolved from the DB via runtime token — they BECOME the roster (no api_key
    roster needed). A ``base_roster`` entry with the same user_id still wins (dev
    override / pinned local credential)."""
    if host_all_discovered is not None:
        by_uid = {e["user_id"]: e for e in host_all_discovered if e.get("user_id")}
        for e in base_roster:
            if e.get("user_id"):
                by_uid[e["user_id"]] = e        # dev override wins
        roster = list(by_uid.values())
    elif autodiscover:
        enabled = _discover_enabled(include_gateway=gateway_enabled)
        roster = _apply_discovery(base_roster, enabled)
    else:
        roster = base_roster
    if not gateway_enabled:
        return _drop_gateway_users(roster), []
    return _wire_gateway_models(roster)


_GENESIS_TICK_DEFAULT_SEC = 30
_GENESIS_TOKEN_TTL_DEFAULT_SEC = 7200  # a large import can spend >15min in LLM calls


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _genesis_worker_should_start(*, enabled: str, secret: str, enclave_url: str) -> bool:
    """Activate the in-CVM genesis worker only when explicitly enabled AND its
    prerequisites are present (runtime-token secret to mint scoped tokens, enclave
    URL to decrypt chunks). Default OFF — landing the hook must not run genesis
    until the env opts in; missing prereqs stay dormant rather than fail jobs."""
    if not _truthy(enabled):
        return False
    return bool(str(secret or "").strip()) and bool(str(enclave_url or "").strip())


def _genesis_worker_loop(*, api_url, enclave_url, mint_genesis, interval, stop_event):
    """Background daemon: poll genesis.worker.tick. Separate from sup.tick so a
    long genesis run never blocks chat-consumer supervision. Worker claim is
    FOR UPDATE SKIP LOCKED, so multiple supervisors/threads are safe."""
    from genesis import worker as genesis_worker
    while not stop_event.is_set():
        try:
            # Reap imports wedged in 'processing' (worker/daemon died mid-run)
            # before claiming new work, so a crashed job can't block spawn forever.
            genesis_worker.reap_stale_processing_jobs()
            genesis_worker.tick(api_url=api_url, enclave_url=enclave_url,
                                mint_runtime_token=mint_genesis, max_jobs=1)
        except Exception as e:  # noqa: BLE001
            log.exception("genesis worker tick failed: %s", e)
        stop_event.wait(interval)


def _supervisor_heartbeat_payload(owner: str, *, host_all: bool, gateway: bool, ts: float) -> dict:
    """Global heartbeat the backend's wedge guard reads. ``host_all``/``gateway``
    let the backend detect the cross-service config divergence (supervisor up but
    not actually hosting / gateway off) that its own startup check can't see."""
    return {"ts": float(ts), "owner": str(owner),
            "host_all": bool(host_all), "gateway": bool(gateway)}


def _supervisor_instance_payload(owner: str, *, host: str | None, host_all: bool,
                                 gateway: bool, active_children: int, max_children: int,
                                 shard_index: int, shard_count: int, version: str | None,
                                 ts: float) -> dict:
    """Rich per-owner heartbeat row (migration 0009). Beyond the legacy payload's
    host_all/gateway, it carries this runner's live capacity (active vs max
    children) and shard config, so multiple runners report independently and the
    backend can aggregate without one clobbering another."""
    return {"ts": float(ts), "owner": str(owner), "host": host,
            "host_all": bool(host_all), "gateway": bool(gateway),
            "active_children": int(active_children), "max_children": int(max_children),
            "shard_index": int(shard_index), "shard_count": int(shard_count),
            "version": version}


def _heartbeat_loop(*, owner: str, host_all: bool, gateway: bool,
                    interval: float, stop_event, instance_payload_fn=None,
                    prune_max_age_sec: float | None = None) -> None:
    """Write the supervisor heartbeat on a fixed cadence from a dedicated thread,
    decoupled from the (potentially slow) discover→resolve→spawn loop.

    The backend's wedge guard reads this heartbeat to confirm a supervisor is
    hosting before routing a send. During a cold start of many users, the main
    loop's per-user token-mint + enclave-decrypt + spawn work can run for minutes
    in a single pass; if the heartbeat were written inline at the top of that loop
    it would lag with it and the guard would wrongly 503 every send. Writing it
    from here keeps it fresh regardless of how long supervision takes. Best-effort
    — a write blip must never kill the thread (else one DB blip wedges sends).

    Writes the per-owner multi-instance row (via ``instance_payload_fn(ts)``, which
    closes over the live supervisor so active_children is current) AND, as a
    transitional fallback, the legacy single global key. Each is independently
    guarded so one failing never starves the other or kills the thread."""
    while not stop_event.is_set():
        ts = time.time()
        if instance_payload_fn is not None:
            try:
                db.set_supervisor_instance_heartbeat(owner, instance_payload_fn(ts))
            except Exception as e:  # noqa: BLE001
                log.warning("supervisor instance heartbeat write failed: %s", e)
            if prune_max_age_sec is not None:
                try:
                    db.prune_supervisor_instance_heartbeats(prune_max_age_sec)
                except Exception as e:  # noqa: BLE001
                    log.warning("supervisor instance heartbeat prune failed: %s", e)
        try:
            db.set_supervisor_heartbeat(_supervisor_heartbeat_payload(
                owner, host_all=host_all, gateway=gateway, ts=ts))
        except Exception as e:  # noqa: BLE001
            log.warning("supervisor heartbeat write failed: %s", e)
        stop_event.wait(interval)


def _renew_loop(*, sup, interval: float, stop_event) -> None:
    """Renew per-user leases on a fixed cadence from a dedicated thread, decoupled
    from the main discover→resolve→spawn loop.

    Why separate from the tick: at host-all scale a single resolve lap (per-user
    token-mint + envelope-fetch + JIT decrypt) can run for minutes. Renewal lived
    inside that loop, so leases were only refreshed once per lap — far longer than
    the TTL — and sends 503'd against expired leases. Renewing here keeps every
    live child's lease fresh regardless of how long a resolve takes. Best-effort:
    a renew blip must never kill the thread."""
    while not stop_event.is_set():
        try:
            sup.renew_live()
        except Exception as e:  # noqa: BLE001
            log.warning("lease renew pass failed: %s", e)
        stop_event.wait(interval)


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    owner = f"{socket.gethostname()}:{os.getpid()}"
    # Default well above a worst-case multi-minute supervision lap so a lease
    # never lapses between renew passes. The compose pins this explicitly (300);
    # the code default matches so a missing env can't silently fall back to the
    # old 60s that re-opened the churn window. (renew now reclaims an uncontested
    # self-expiry too, so this is defence in depth, not the sole guard.)
    lease_ttl = float(os.environ.get("AGENT_LEASE_TTL_SEC", "300"))
    interval = float(os.environ.get("AGENT_TICK_INTERVAL_SEC", "15"))
    # Spread a many-user cold start across ticks (default 8/tick) instead of
    # forking dozens of consumers in one pass. Set 0 to restore unlimited.
    max_spawns_per_tick = int(os.environ.get("AGENT_MAX_SPAWNS_PER_TICK", "8"))
    # Steady-state per-runner capacity ceiling (0 = unlimited). Multi-node: with
    # several runners scanning the same host-all set, this bounds how many users a
    # single runner takes so the rest get acquired elsewhere. Distinct from the
    # per-tick spawn rate above.
    max_children = int(os.environ.get("AGENT_MAX_CHILDREN", "0"))
    data_root = os.environ.get("AGENT_DATA_ROOT", "/agent-data")
    isolation = os.environ.get("AGENT_RUNTIME_ISOLATION", "process").strip().lower()

    gateway_enabled = os.environ.get("FEEDLING_LITELLM_ENABLE", "").strip().lower() in ("1", "true", "yes")
    autodiscover = os.environ.get("AGENT_RUNTIME_AUTODISCOVER", "").strip().lower() in ("1", "true", "yes")
    # Stage D host-all: every configured user is hosted with NO api_key roster —
    # the supervisor mints a runtime token per DB-discovered user and resolves the
    # provider key with it. Requires FEEDLING_RUNTIME_TOKEN_SECRET (set below);
    # without the secret there is no token to authenticate with, so host-all is inert.
    host_all = os.environ.get("FEEDLING_HOST_ALL", "").strip().lower() in ("1", "true", "yes")
    api_url = os.environ.get("FEEDLING_API_URL", "http://localhost:5001")
    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "")

    # Credentials resolved once (whoami + envelope decrypt are network calls). The
    # backend-enabled intersection + gateway wiring re-run each tick off this base,
    # so enabling/disabling a user (or the gateway) takes effect without a redeploy.
    base_roster = _resolve_roster(_load_roster())
    if not base_roster and not autodiscover and not host_all:
        log.warning("no roster and autodiscover off — supervisor will idle "
                    "(set AGENT_RUNTIME_USERS or AGENT_RUNTIME_AUTODISCOVER=1)")

    spawn_fn, alive_fn, kill_fn = spawners.get_spawner(isolation)

    # Stage D: if the shared HMAC secret is set, the supervisor mints a per-user,
    # short-lived runtime token and writes it to the user's home, refreshed each
    # tick. The consumer authenticates with the token instead of the long-term
    # API key. Secret unset → no token files → consumer stays on the API key.
    token_writer = None
    mint_token = None
    secret_raw = os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET", "").strip()
    if secret_raw:
        secret = secret_raw.encode("utf-8")
        ttl = float(os.environ.get("AGENT_RUNTIME_TOKEN_TTL_SEC", "900"))
        scopes = ["chat", "memory", "identity", "perception", "envelope_decrypt"]

        def mint_token(user_id):
            return runtime_token.mint(secret, user_id=user_id, runtime_instance_id=owner,
                                      scope=scopes, ttl=ttl)

        def _mint_and_write(user_id, home):
            spawners.write_runtime_token(home, mint_token(user_id))

        token_writer = _mint_and_write

    # in-CVM LiteLLM gateway (codex non-openai providers). Off by default: only
    # when FEEDLING_LITELLM_ENABLE is set do we run a LiteLLM proxy that holds the
    # gateway users' upstream keys (in the proxy's env, never on disk) and rewrite
    # those users to the gw-<uid> model. Codex reaches it at 127.0.0.1:<port>.
    gateway_mgr = None
    if gateway_enabled:
        port = int(os.environ.get("FEEDLING_LITELLM_PORT", "4000"))
        cfg_path = os.environ.get("FEEDLING_LITELLM_CONFIG", f"{data_root}/litellm.yaml")
        os.environ.setdefault("FEEDLING_LITELLM_BASE_URL", f"http://127.0.0.1:{port}/v1")
        gateway_mgr = litellm_gateway.GatewayManager(config_path=cfg_path, port=port)
        log.info("litellm gateway enabled — config=%s port=%d", cfg_path, port)

    if host_all and mint_token is None:
        log.warning("FEEDLING_HOST_ALL set but FEEDLING_RUNTIME_TOKEN_SECRET is not — "
                    "host-all is inert (no token to authenticate zero-roster users)")
    host_all_active = host_all and mint_token is not None
    cred_cache: dict = {}
    autoverify_state: dict = {}    # user_id -> {done, fails, next} (backed-off gate-open)
    autoverify_inflight: set = set()

    sup = Supervisor(
        owner=owner, lease_ttl=lease_ttl, data_root=data_root,
        spawn_fn=spawn_fn, alive_fn=alive_fn, kill_fn=kill_fn,
        token_writer=token_writer, max_spawns_per_tick=max_spawns_per_tick,
        max_children=max_children,
    )
    log.info("supervisor up — owner=%s base_users=%d autodiscover=%s host_all=%s gateway=%s ttl=%.0fs max_children=%d",
             owner, len(base_roster), autodiscover, host_all_active, gateway_enabled, lease_ttl, max_children)

    # Genesis CVM worker — runs in its own daemon thread (never inline in the tick
    # loop). Default OFF; activates only when FEEDLING_GENESIS_WORKER_ENABLED is set
    # AND a runtime-token secret + enclave URL are present (else dormant + warn).
    genesis_enabled = os.environ.get("FEEDLING_GENESIS_WORKER_ENABLED", "")
    genesis_stop = threading.Event()
    if _genesis_worker_should_start(enabled=genesis_enabled, secret=secret_raw, enclave_url=enclave_url):
        g_secret = secret_raw.encode("utf-8")
        genesis_ttl = float(os.environ.get(
            "FEEDLING_GENESIS_RUNTIME_TOKEN_TTL_SEC", str(_GENESIS_TOKEN_TTL_DEFAULT_SEC)))
        g_interval = float(os.environ.get(
            "FEEDLING_GENESIS_WORKER_INTERVAL_SEC", str(_GENESIS_TICK_DEFAULT_SEC)))

        def mint_genesis(user_id, scopes=None):
            return runtime_token.mint(
                g_secret, user_id=user_id, runtime_instance_id=owner,
                scope=list(scopes or ["envelope_decrypt", "genesis"]), ttl=genesis_ttl)

        threading.Thread(
            target=_genesis_worker_loop, daemon=True,
            kwargs={"api_url": api_url, "enclave_url": enclave_url,
                    "mint_genesis": mint_genesis, "interval": g_interval,
                    "stop_event": genesis_stop},
        ).start()
        log.info("genesis worker enabled — interval=%.0fs token_ttl=%.0fs", g_interval, genesis_ttl)
    elif _truthy(genesis_enabled):
        log.warning("FEEDLING_GENESIS_WORKER_ENABLED set but prerequisites missing "
                    "(need FEEDLING_RUNTIME_TOKEN_SECRET + FEEDLING_ENCLAVE_URL) — genesis worker dormant")

    # Heartbeat from a dedicated thread, decoupled from the (potentially slow)
    # discover→resolve→spawn loop below: a multi-minute cold-start pass must not
    # stall the wedge-guard heartbeat (else the backend 503s every send). Capped
    # well under the wedge's ~90s staleness threshold.
    hb_stop = threading.Event()

    def _instance_payload(ts):
        # Closes over `sup` so active_children is read live each beat (under the
        # children lock). shard defaults (0/1) until sharding is wired.
        with sup._lock:
            active = len(sup.children)
        return _supervisor_instance_payload(
            owner, host=socket.gethostname(), host_all=host_all_active,
            gateway=gateway_enabled, active_children=active,
            max_children=sup.max_children, shard_index=0, shard_count=1,
            version=None, ts=ts)

    threading.Thread(
        target=_heartbeat_loop, daemon=True,
        kwargs={"owner": owner, "host_all": host_all_active,
                "gateway": gateway_enabled, "interval": min(interval, 15.0),
                "stop_event": hb_stop, "instance_payload_fn": _instance_payload,
                # Drop rows from dead runners (each restart is a new hostname:pid
                # owner). Generous so a briefly-paused runner isn't pruned mid-life.
                "prune_max_age_sec": float(os.environ.get(
                    "AGENT_SUPERVISOR_HEARTBEAT_PRUNE_SEC", "3600"))},
    ).start()

    # Lease renewal from its own thread, on the same cadence — decoupled from the
    # (potentially multi-minute) resolve/spawn pass so leases stay fresh and sends
    # don't 503 against an expired lease while a slow lap is in flight.
    renew_stop = threading.Event()
    threading.Thread(
        target=_renew_loop, daemon=True,
        kwargs={"sup": sup, "interval": min(interval, 15.0), "stop_event": renew_stop},
    ).start()

    try:
        while True:
            try:
                # Re-derive the live roster each tick so enabling/disabling a user
                # (or the gateway) takes effect without restarting the supervisor.
                discovered = None
                if host_all_active:
                    enabled = _discover_enabled(include_gateway=gateway_enabled)
                    discovered = _resolve_discovered(
                        enabled, mint_token=mint_token, api_url=api_url,
                        enclave_url=enclave_url, cache=cred_cache)
                roster, gateways = _effective_roster(
                    base_roster, autodiscover=autodiscover, gateway_enabled=gateway_enabled,
                    host_all_discovered=discovered)
                # Tag each entry with the genesis_persona digest (unified point: covers
                # base + discovered). _spawn_identity includes it, so when a voice
                # backfill/Dream writes a new persona blob the next tick respawns and
                # re-seeds the persona prompt file (only written at spawn) — cutover
                # gate 4 C. No kill, no in-place prompt rewrite.
                for _entry in roster:
                    _entry["persona_version"] = _persona_version(_entry.get("user_id", ""))
                # Lease-scoped gateway, race-free across spawn: configure the
                # gw-<uid> routes for users acquired THIS tick BEFORE their consumers
                # start (pre_spawn), then converge AFTER tick to drop routes for
                # users that left/were reaped. Both passes only ever include users
                # this runner owns, so another runner's key never enters our config
                # (multi-node key isolation). ``gateways`` carries this tick's
                # freshly-resolved upstream keys, so a key rotation still reaches
                # LiteLLM even though it deliberately doesn't respawn the consumer.
                # owned ids are snapshotted under the lock — the renew thread mutates
                # sup.children concurrently (iterating it live could raise
                # "dictionary changed size during iteration" and abort the tick).
                def _reconcile_gateway(extra_ids=()):
                    if gateway_mgr is None:
                        return
                    with sup._lock:
                        owned_ids = set(sup.children) | set(extra_ids)
                    gateway_mgr.reconcile(_owned_gateway_entries(gateways, owned_ids))

                sup.tick(roster, pre_spawn=_reconcile_gateway)
                _reconcile_gateway()
                # Gate 4 B-3: after the spawn reconcile, top up voice for no-blob users
                # (bounded + cooldown'd so it never blocks the tick). Off by default.
                if _lazy_persona_backfill_enabled():
                    _run_lazy_persona_backfill(roster, secret_raw=secret_raw, owner=owner,
                                               api_url=api_url, now=time.time())
                # Stage D host-all: a freshly-hosted user is dead-ended at
                # needs_live_connection until verify_loop runs once. Open the gate
                # in the background (the POST blocks ~30s for the consumer's reply).
                # Only for users THIS supervisor owns a running consumer for (sup.children)
                # — so the verify ping can actually be answered — and backed off so a
                # user stuck earlier in bootstrap isn't re-probed every tick.
                if host_all_active:
                    with sup._lock:
                        children_snapshot = list(sup.children)
                    for uid in children_snapshot:
                        if uid in autoverify_inflight or autoverify_state.get(uid, {}).get("done"):
                            continue
                        autoverify_inflight.add(uid)

                        def _autoverify(uid=uid):
                            try:
                                _maybe_autoverify(uid, mint_token=mint_token, api_url=api_url,
                                                  state=autoverify_state)
                            finally:
                                autoverify_inflight.discard(uid)

                        threading.Thread(target=_autoverify, daemon=True).start()
            except Exception as e:  # noqa: BLE001
                log.exception("supervisor tick failed: %s", e)
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("interrupted; releasing leases")
        return 0
    finally:
        hb_stop.set()
        renew_stop.set()
        genesis_stop.set()
        sup.shutdown()
        if gateway_mgr is not None:
            gateway_mgr.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
