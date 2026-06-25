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
  AGENT_RUNTIME_ISOLATION  process (default) | container
"""

from __future__ import annotations

import base64
import json
import logging
import os
import socket
import sys
import time
from pathlib import Path

import httpx

import db
from core import runtime_token

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from agent_runtime import leases, spawners

log = logging.getLogger("feedling.agent_runtime.supervisor")


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
    ) -> None:
        self.owner = owner
        self.lease_ttl = lease_ttl
        self.data_root = data_root.rstrip("/")
        self.spawn_fn = spawn_fn      # (entry, user_id, home) -> pid
        self.alive_fn = alive_fn      # (pid) -> bool
        self.kill_fn = kill_fn        # (pid) -> None
        self.token_writer = token_writer  # (user_id, home) -> None, or None (Stage D off)
        self._now = now
        self.children: dict[str, dict] = {}  # user_id -> {pid, entry, home}

    def _write_token(self, user_id: str, home: str) -> None:
        if self.token_writer is None:
            return
        try:
            self.token_writer(user_id, home)
        except Exception as e:  # noqa: BLE001 — a token-refresh failure must not crash the tick
            log.warning("runtime-token refresh failed for %s: %s", user_id, e)

    def _home(self, user_id: str) -> str:
        return f"{self.data_root}/users/{user_id}"

    def tick(self, roster: list[dict]) -> None:
        """One supervision pass: heartbeat live children, reap dead ones, and
        acquire+spawn for any user we don't already run."""
        for entry in roster:
            user_id = str(entry.get("user_id") or "")
            if not user_id:
                continue
            child = self.children.get(user_id)

            if child and self.alive_fn(child["pid"]):
                if not leases.renew(user_id, self.owner, ttl=self.lease_ttl,
                                    pid=child["pid"], status="running", now=self._now()):
                    # Lost the lease (another supervisor took over after an expiry
                    # window). Kill our now-orphaned child so we don't double-run
                    # alongside the new owner ("exactly one consumer per user").
                    log.warning("lost lease for %s; terminating orphaned child", user_id)
                    self.kill_fn(child["pid"])
                    self.children.pop(user_id, None)
                else:
                    self._write_token(user_id, child["home"])  # refresh short-lived token
                continue

            if child:  # tracked but dead → reap
                log.info("child for %s exited; releasing lease", user_id)
                leases.release(user_id, self.owner, now=self._now())
                self.children.pop(user_id, None)

            home = self._home(user_id)
            if not leases.acquire(user_id, driver=entry.get("driver", "claude"),
                                  runtime_home=home, lease_owner=self.owner,
                                  ttl=self.lease_ttl, now=self._now()):
                continue  # another supervisor holds a live lease
            pid = self.spawn_fn(entry, user_id, home)
            self._write_token(user_id, home)  # initial short-lived token for the child
            leases.renew(user_id, self.owner, ttl=self.lease_ttl, pid=pid,
                         status="running", now=self._now())
            self.children[user_id] = {"pid": pid, "entry": entry, "home": home}
            log.info("spawned resident consumer for %s (pid=%s, home=%s)", user_id, pid, home)

    def shutdown(self) -> None:
        for user_id, child in list(self.children.items()):
            self.kill_fn(child["pid"])
            leases.release(user_id, self.owner)
        self.children.clear()


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


def _decrypt_provider_key(enclave_url: str, api_key: str, envelope: dict) -> str:
    """JIT-decrypt a provider-key envelope via the enclave (purpose reuses the
    model_api config scheme). Plaintext is handed to the child via env."""
    try:
        resp = httpx.post(f"{enclave_url.rstrip('/')}/v1/envelope/decrypt",
                          headers={"X-API-Key": api_key},
                          json={"envelope": envelope, "purpose": "model_api_provider_key"},
                          timeout=20, verify=False)
        resp.raise_for_status()
        return base64.b64decode(resp.json()["plaintext_b64"]).decode("utf-8")
    except Exception as e:  # noqa: BLE001
        log.error("provider key decrypt failed for a roster entry: %s", e)
        return ""


def _fetch_key_envelope(api_url: str, api_key: str) -> dict | None:
    """Fetch the user's OWN provider-key envelope ciphertext from the backend
    (Stage B: ``GET /v1/model_api/key_envelope``), so a roster need only carry
    api_keys. Returns the envelope dict, or None if unconfigured/unreachable."""
    try:
        resp = httpx.get(f"{api_url.rstrip('/')}/v1/model_api/key_envelope",
                         headers={"X-API-Key": api_key}, timeout=10)
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


def _discover_enabled() -> dict[str, str]:
    """Map ``user_id -> driver`` for users opted into the hosted runtime (Stage C),
    read straight from the DB the supervisor already connects to for leases."""
    return {u["user_id"]: u["driver"] for u in db.list_agent_runtime_enabled_users()}


def _apply_discovery(roster: list[dict], enabled: dict[str, str]) -> list[dict]:
    """Filter the (credential-bearing) roster to users the backend has enabled,
    taking each driver from the backend flag — the /v1/model_api/driver setter is
    the gradual-migration control plane. Roster still supplies api_keys until
    Stage D's runtime-token. Entries must already have ``user_id`` (whoami)."""
    out = []
    for entry in roster:
        uid = entry.get("user_id")
        if uid in enabled:
            out.append({**entry, "driver": enabled[uid]})
    return out


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    owner = f"{socket.gethostname()}:{os.getpid()}"
    lease_ttl = float(os.environ.get("AGENT_LEASE_TTL_SEC", "60"))
    interval = float(os.environ.get("AGENT_TICK_INTERVAL_SEC", "15"))
    data_root = os.environ.get("AGENT_DATA_ROOT", "/agent-data")
    isolation = os.environ.get("AGENT_RUNTIME_ISOLATION", "process").strip().lower()

    roster = _resolve_roster(_load_roster())
    if os.environ.get("AGENT_RUNTIME_AUTODISCOVER", "").strip().lower() in ("1", "true", "yes"):
        enabled = _discover_enabled()
        roster = _apply_discovery(roster, enabled)
        log.info("autodiscover: %d roster users intersect %d backend-enabled", len(roster), len(enabled))
    if not roster:
        log.error("empty roster — set AGENT_RUNTIME_USERS or AGENT_RUNTIME_ROSTER")
        return 2

    spawn_fn, alive_fn, kill_fn = spawners.get_spawner(isolation)

    # Stage D: if the shared HMAC secret is set, the supervisor mints a per-user,
    # short-lived runtime token and writes it to the user's home, refreshed each
    # tick. The consumer authenticates with the token instead of the long-term
    # API key. Secret unset → no token files → consumer stays on the API key.
    token_writer = None
    secret_raw = os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET", "").strip()
    if secret_raw:
        secret = secret_raw.encode("utf-8")
        ttl = float(os.environ.get("AGENT_RUNTIME_TOKEN_TTL_SEC", "900"))
        scopes = ["chat", "memory", "identity", "perception", "envelope_decrypt"]

        def _mint_and_write(user_id, home):
            tok = runtime_token.mint(secret, user_id=user_id, runtime_instance_id=owner,
                                     scope=scopes, ttl=ttl)
            spawners.write_runtime_token(home, tok)

        token_writer = _mint_and_write

    sup = Supervisor(
        owner=owner, lease_ttl=lease_ttl, data_root=data_root,
        spawn_fn=spawn_fn, alive_fn=alive_fn, kill_fn=kill_fn,
        token_writer=token_writer,
    )
    log.info("supervisor up — owner=%s users=%d ttl=%.0fs", owner, len(roster), lease_ttl)
    try:
        while True:
            try:
                sup.tick(roster)
            except Exception as e:  # noqa: BLE001
                log.exception("supervisor tick failed: %s", e)
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("interrupted; releasing leases")
        return 0
    finally:
        sup.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
