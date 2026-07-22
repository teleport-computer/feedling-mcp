"""Pure-unit tests for the supervisor's per-tick roster resolution hot path.

The host-all supervisor re-resolves every enabled user each tick. At ~50 users
that was a serial 50× backend envelope-fetch (+ JIT enclave decrypt) per tick,
so one supervision lap ran for minutes — far longer than the lease TTL — and
leases sat expired, 503-ing sends. These tests pin the two fixes:

  * the provider-key ENVELOPE fetch is cached per user with a TTL (it changes
    ~never), so steady-state ticks make no backend call per user; and
  * resolution fans out across a bounded thread pool so a cold cache (e.g. a
    fresh supervisor after a CVM resize) resolves all users concurrently
    instead of one-at-a-time.

No DB / network — ``_fetch_key_envelope`` / ``_decrypt_provider_key`` are
patched, so this file is in conftest's ``_PURE_UNIT`` set.
"""

import sys
import threading
import time as _time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from agent_runtime import supervisor as supervisor_mod


def _enabled(*uids):
    return {u: {"driver": "claude", "provider": "anthropic", "model": "m", "base_url": ""}
            for u in uids}


def _resolve(enabled, *, cache, now=None, **kw):
    return supervisor_mod._resolve_discovered(
        enabled, mint_token=lambda uid: f"tok-{uid}",
        api_url="http://backend", enclave_url="http://enclave",
        cache=cache, now=now, **kw)


# ---- T1.2: envelope-fetch TTL cache ----


def test_envelope_fetch_is_cached_within_ttl(monkeypatch):
    fetches = []
    monkeypatch.setenv("AGENT_ENVELOPE_REFETCH_SEC", "300")
    monkeypatch.setattr(supervisor_mod, "_fetch_key_envelope",
                        lambda api_url, runtime_token="": (fetches.append(runtime_token) or {"ct": "c1"}))
    decrypts = []
    monkeypatch.setattr(supervisor_mod, "_decrypt_provider_key",
                        lambda enclave_url, envelope=None, runtime_token="": (decrypts.append(1) or "sk-1"))
    cache: dict = {}
    clock = [1_000.0]
    enabled = _enabled("u1")

    out1 = _resolve(enabled, cache=cache, now=lambda: clock[0])
    assert out1[0]["provider_key"] == "sk-1"
    assert len(fetches) == 1 and len(decrypts) == 1   # cold: fetched + decrypted

    clock[0] += 30
    out2 = _resolve(enabled, cache=cache, now=lambda: clock[0])
    assert out2[0]["provider_key"] == "sk-1"
    assert len(fetches) == 1                           # served from envelope cache
    assert len(decrypts) == 1                          # sig unchanged → no re-decrypt

    clock[0] += 10_000                                 # past TTL
    _resolve(enabled, cache=cache, now=lambda: clock[0])
    assert len(fetches) == 2                           # refetched after TTL


def test_envelope_transient_fetch_failure_keeps_last_good(monkeypatch):
    # After TTL, a refetch that returns None (backend blip) must NOT drop the
    # cached key — a healthy consumer should not be respawned keyless.
    state = {"env": {"ct": "c1"}}
    monkeypatch.setenv("AGENT_ENVELOPE_REFETCH_SEC", "100")
    monkeypatch.setattr(supervisor_mod, "_fetch_key_envelope",
                        lambda api_url, runtime_token="": state["env"])
    monkeypatch.setattr(supervisor_mod, "_decrypt_provider_key",
                        lambda enclave_url, envelope=None, runtime_token="": "sk-1")
    cache: dict = {}
    clock = [0.0]
    enabled = _enabled("u1")

    _resolve(enabled, cache=cache, now=lambda: clock[0])
    state["env"] = None                                # backend now blips
    clock[0] += 1_000                                  # force refetch
    out = _resolve(enabled, cache=cache, now=lambda: clock[0])
    assert out[0]["provider_key"] == "sk-1"            # last good key retained


# ---- T1.3: bounded-concurrency resolution ----


def test_resolution_fans_out_across_users(monkeypatch):
    active = {"now": 0, "max": 0}
    lock = threading.Lock()

    def slow_fetch(api_url, runtime_token=""):
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        _time.sleep(0.05)
        with lock:
            active["now"] -= 1
        return {"ct": runtime_token}

    monkeypatch.setenv("AGENT_RESOLVE_CONCURRENCY", "4")
    monkeypatch.setattr(supervisor_mod, "_fetch_key_envelope", slow_fetch)
    monkeypatch.setattr(supervisor_mod, "_decrypt_provider_key",
                        lambda enclave_url, envelope=None, runtime_token="": "sk")
    enabled = _enabled(*[f"u{i}" for i in range(8)])

    out = _resolve(enabled, cache={})
    assert {e["user_id"] for e in out} == set(enabled)   # all resolved
    assert active["max"] >= 2                            # ran concurrently, not serial


def test_one_user_failure_does_not_abort_the_pass(monkeypatch):
    def fetch(api_url, runtime_token=""):
        if runtime_token == "tok-bad":
            raise RuntimeError("enclave blip for one user")
        return {"ct": runtime_token}

    monkeypatch.setattr(supervisor_mod, "_fetch_key_envelope", fetch)
    monkeypatch.setattr(supervisor_mod, "_decrypt_provider_key",
                        lambda enclave_url, envelope=None, runtime_token="": "sk")
    enabled = _enabled("good1", "bad", "good2")

    out = _resolve(enabled, cache={})
    got = {e["user_id"] for e in out}
    assert "good1" in got and "good2" in got            # healthy users still resolve
