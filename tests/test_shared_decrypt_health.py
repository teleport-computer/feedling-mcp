"""Runner-shared decrypt-health probe (2026-07-24).

The reachability probe verifies only SHARED infrastructure — enclave alive,
enclave→backend loopback, content_sk present — none of it per-user. N resident
consumers each probing every DECRYPT_HEALTH_REFRESH_SEC is O(users) redundancy
for one answer. Shared mode publishes/reuses a runner-shared health file so the
probe rate drops to O(runner≈1).

Two layers stay orthogonal:
  - infra layer  (shared)     → this file
  - per-user envelope layer   → local passive signal (_note_decrypt_read_*),
                                a standing `degraded` still wins over shared `ok`.

Design + rationale: docs/proposals/shared-decrypt-health-probe.md.

Network + enclave are mocked — no real backend needed.

Run:  python -m pytest tests/test_shared_decrypt_health.py -q
"""

import json
import os
import sys
import types
from pathlib import Path

import pytest

_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "cli",
    "AGENT_CLI_CMD": "claude --allowed-tools 'x' {mcp} -p {message}",
    "CHECKPOINT_FILE": "/tmp/feedling_test_shared_health_checkpoint.json",
}
for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules.setdefault("content_encryption", _fake_enc)

import tools.chat_resident_consumer as c  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_health():
    c._decrypt_health.update({"status": "unknown", "checked_at": 0.0})
    c._decrypt_health_last_refresh["at"] = 0.0
    c._decrypt_read_failures["count"] = 0
    yield


@pytest.fixture()
def shared(tmp_path, monkeypatch):
    """Shared mode on, pointed at an isolated per-test health file."""
    hf = tmp_path / "decrypt_health.json"
    monkeypatch.setattr(c, "DECRYPT_HEALTH_SHARED", True)
    monkeypatch.setattr(c, "DECRYPT_HEALTH_FILE", hf)
    monkeypatch.setattr(c, "DECRYPT_HEALTH_REFRESH_SEC", 210.0)
    return hf


def _write_file(hf: Path, status: str, checked_at: float) -> None:
    hf.write_text(json.dumps({"status": status, "checked_at": checked_at}))


def _spy_measure(monkeypatch, result="ok"):
    """Replace the enclave probe with a call counter."""
    calls = {"n": 0}

    def fake():
        calls["n"] += 1
        return result

    monkeypatch.setattr(c, "_measure_infra_health", fake)
    return calls


# --------------------------------------------------------------------------
# _measure_infra_health — pure probe, infra statuses only, never touches health
# --------------------------------------------------------------------------

def test_measure_returns_ok_without_touching_health(monkeypatch):
    monkeypatch.setattr(c, "FEEDLING_ENCLAVE_URL", "https://enclave.example/")
    monkeypatch.setattr(c, "_client_for", lambda url: _FakeClient(ok=True))
    assert c._measure_infra_health() == "ok"
    # pure: _decrypt_health untouched (still unknown from the fixture)
    assert c._decrypt_health["status"] == "unknown"


def test_measure_unreachable_on_error(monkeypatch):
    monkeypatch.setattr(c, "FEEDLING_ENCLAVE_URL", "https://enclave.example/")
    monkeypatch.setattr(c, "_client_for", lambda url: _FakeClient(ok=False))
    assert c._measure_infra_health() == "unreachable"


def test_measure_unconfigured_without_url(monkeypatch):
    monkeypatch.setattr(c, "FEEDLING_ENCLAVE_URL", "")
    assert c._measure_infra_health() == "unconfigured"


class _FakeResp:
    def raise_for_status(self):
        return None


class _FakeClient:
    def __init__(self, ok=True):
        self._ok = ok

    def get(self, *a, **kw):
        if not self._ok:
            raise RuntimeError("boom")
        return _FakeResp()


# --------------------------------------------------------------------------
# _apply_infra_health — degrade-masking + carried checked_at
# --------------------------------------------------------------------------

def test_apply_ok_stamps_now_by_default(monkeypatch):
    monkeypatch.setattr(c.time, "time", lambda: 500.0)
    c._apply_infra_health("ok")
    assert c._decrypt_health == {"status": "ok", "checked_at": 500.0}


def test_apply_carries_shared_probe_time(monkeypatch):
    # A consumer reusing a shared reading must report that reading's REAL probe
    # time, not its own clock — so a lagging consumer ages into the backend's
    # 300s staleness window instead of vouching a stale ok.
    monkeypatch.setattr(c.time, "time", lambda: 9_999.0)
    c._apply_infra_health("ok", checked_at=1234.0)
    assert c._decrypt_health["checked_at"] == 1234.0


def test_apply_ok_never_upgrades_degraded(monkeypatch):
    monkeypatch.setattr(c.time, "time", lambda: 100.0)
    c._set_decrypt_health("degraded")
    c._apply_infra_health("ok", checked_at=200.0)
    assert c._decrypt_health["status"] == "degraded"        # not upgraded
    assert c._decrypt_health["checked_at"] == 200.0          # heartbeat forward


def test_apply_degraded_heartbeat_never_moves_backward(monkeypatch):
    monkeypatch.setattr(c.time, "time", lambda: 300.0)
    c._set_decrypt_health("degraded")                        # checked_at=300
    c._apply_infra_health("ok", checked_at=250.0)            # older shared reading
    assert c._decrypt_health["checked_at"] == 300.0          # kept fresher local


def test_apply_ok_does_not_move_checked_at_backward(monkeypatch):
    # A consumer that just decrypted a real message (ok @ now) then reuses an
    # OLDER shared 'ok' reading must NOT age its own checked_at backward — that
    # could push a working consumer past the backend's 300s staleness window.
    monkeypatch.setattr(c.time, "time", lambda: 8000.0)
    c._set_decrypt_health("ok")                              # ok @ 8000 (fresh)
    c._apply_infra_health("ok", checked_at=7900.0)           # older shared reading
    assert c._decrypt_health["checked_at"] == 8000.0         # kept fresher local


def test_apply_unreachable_overrides_even_ok(monkeypatch):
    # Infra genuinely down overrides a stale local ok (matches legacy probe).
    monkeypatch.setattr(c.time, "time", lambda: 400.0)
    c._set_decrypt_health("ok")
    c._apply_infra_health("unreachable")
    assert c._decrypt_health["status"] == "unreachable"


def test_reachability_unreachable_never_launders_degraded(monkeypatch):
    # A per-user 'degraded' (real broken envelope) must survive a transient
    # reachability 'unreachable' AND the subsequent 'ok' — otherwise the two-step
    # unreachable→ok path would clear degraded and mask a real per-user outage.
    monkeypatch.setattr(c.time, "time", lambda: 500.0)
    c._set_decrypt_health("degraded")                        # per-user degrade
    c._apply_infra_health("unreachable")                     # transient blip
    assert c._decrypt_health["status"] == "degraded"         # NOT clobbered
    c._apply_infra_health("ok")                              # infra recovers
    assert c._decrypt_health["status"] == "degraded"         # still degraded
    # only a real decrypt success may clear it
    c._note_decrypt_read_success()
    assert c._decrypt_health["status"] == "ok"


# --------------------------------------------------------------------------
# shared file read/write — fail-open, infra-only
# --------------------------------------------------------------------------

def test_read_missing_file_returns_none(shared):
    assert c._read_shared_infra_health() is None


def test_write_then_read_roundtrip(shared):
    c._write_shared_infra_health("ok", 1700.5)
    assert c._read_shared_infra_health() == ("ok", 1700.5)


def test_read_corrupt_file_fails_open(shared):
    shared.write_text("{ not json ")
    assert c._read_shared_infra_health() is None


def test_read_reuses_only_positive_ok(shared):
    # Only a positive 'ok' is a reusable shared reading. A negative 'unreachable'
    # must never be reused (it would latch every peer to a one-off blip);
    # 'unconfigured' saves no probe; 'degraded'/'unknown' are per-user/unmeasured.
    for bad in ("unreachable", "unconfigured", "degraded", "unknown"):
        _write_file(shared, bad, 1000.0)
        assert c._read_shared_infra_health() is None, bad
    _write_file(shared, "ok", 1000.0)
    assert c._read_shared_infra_health() == ("ok", 1000.0)


def test_write_publishes_only_positive_ok(shared):
    # A negative or per-user status must never cross the shared file — publishing
    # 'unreachable' would latch the whole runner to one consumer's blip.
    for bad in ("unreachable", "unconfigured", "degraded", "unknown"):
        c._write_shared_infra_health(bad, 1000.0)
        assert not shared.exists(), bad
    c._write_shared_infra_health("ok", 1000.0)
    assert shared.exists()


def test_write_is_atomic(shared, monkeypatch):
    seen = {}
    real = c._atomic_write_text

    def spy(path, content, mode=0o600):
        seen["path"] = path
        return real(path, content, mode)

    monkeypatch.setattr(c, "_atomic_write_text", spy)
    c._write_shared_infra_health("ok", 1.0)
    assert seen["path"] == str(shared)


# --------------------------------------------------------------------------
# _maybe_refresh_decrypt_health — the O(users)→O(runner) behavior
# --------------------------------------------------------------------------

def test_fresh_shared_reading_is_reused_without_probing(shared, monkeypatch):
    calls = _spy_measure(monkeypatch)
    monkeypatch.setattr(c.time, "time", lambda: 1000.0)
    _write_file(shared, "ok", 950.0)                         # 50s old < 210s
    c._maybe_refresh_decrypt_health()
    assert calls["n"] == 0                                   # reused, no probe
    assert c._decrypt_health["status"] == "ok"
    assert c._decrypt_health["checked_at"] == 950.0          # carries real time


def test_stale_shared_triggers_one_probe_and_republish(shared, monkeypatch):
    calls = _spy_measure(monkeypatch, result="ok")
    monkeypatch.setattr(c.time, "time", lambda: 2000.0)
    _write_file(shared, "ok", 1500.0)                        # 500s old > 210s
    c._maybe_refresh_decrypt_health()
    assert calls["n"] == 1                                   # probed once
    assert c._read_shared_infra_health() == ("ok", 2000.0)   # republished now
    assert c._decrypt_health["checked_at"] == 2000.0         # own probe → now


def test_missing_shared_file_fails_open_to_probe(shared, monkeypatch):
    calls = _spy_measure(monkeypatch, result="ok")
    monkeypatch.setattr(c.time, "time", lambda: 3000.0)
    c._maybe_refresh_decrypt_health()
    assert calls["n"] == 1
    assert c._read_shared_infra_health() == ("ok", 3000.0)


def test_corrupt_shared_file_fails_open_to_probe(shared, monkeypatch):
    shared.write_text("garbage{")
    calls = _spy_measure(monkeypatch, result="ok")
    monkeypatch.setattr(c.time, "time", lambda: 3100.0)
    c._maybe_refresh_decrypt_health()
    assert calls["n"] == 1


def test_shared_ok_does_not_override_local_degraded(shared, monkeypatch):
    # per-user envelope layer wins: a streak-degraded consumer stays degraded
    # even though shared infra reads ok.
    _spy_measure(monkeypatch, result="ok")
    monkeypatch.setattr(c.time, "time", lambda: 4000.0)
    c._set_decrypt_health("degraded")                        # checked_at=4000
    _write_file(shared, "ok", 3950.0)                        # fresh shared ok
    c._maybe_refresh_decrypt_health()
    assert c._decrypt_health["status"] == "degraded"         # not overridden


def test_many_consumers_collapse_to_one_probe(shared, monkeypatch):
    # The core claim: a runner of N consumers reusing one shared file probes the
    # enclave ~once per refresh window, not N times. Simulate 12 sequential
    # consumer refreshes at times inside one window; only the first (file stale/
    # missing) probes, the rest reuse the fresh republish.
    calls = _spy_measure(monkeypatch, result="ok")
    t = {"now": 5000.0}
    monkeypatch.setattr(c.time, "time", lambda: t["now"])
    for i in range(12):
        t["now"] = 5000.0 + i * 5.0                          # 5s apart, all < 210
        c._maybe_refresh_decrypt_health()
    assert calls["n"] == 1, f"expected 1 probe across 12 consumers, got {calls['n']}"


def test_unreachable_probe_is_not_published_to_peers(shared, monkeypatch):
    # Blast-radius fix: a prober that measures 'unreachable' degrades ONLY itself
    # and must NOT overwrite the shared file — otherwise one transient blip would
    # latch every co-hosted consumer to 'unreachable' for a whole refresh window.
    _spy_measure(monkeypatch, result="unreachable")
    monkeypatch.setattr(c.time, "time", lambda: 6000.0)
    _write_file(shared, "ok", 5000.0)                        # stale ok → triggers probe
    c._maybe_refresh_decrypt_health()
    assert c._decrypt_health["status"] == "unreachable"      # local degrade only
    # shared file still holds the old ok, NOT a freshly-latched 'unreachable'
    assert c._read_shared_infra_health() == ("ok", 5000.0)


def test_no_reachability_status_uses_the_bare_setter():
    # Invariant: every reachability outcome (ok/unreachable/unconfigured) MUST go
    # through _apply_infra_health (which preserves a standing per-user degraded),
    # never the bare _set_decrypt_health. Otherwise a runtime 'unreachable' set
    # could clobber degraded and a later 'ok' would launder a real per-user
    # decrypt outage back to green. _set_decrypt_health may only carry per-user
    # outcomes: 'degraded' (streak) and 'ok' (real decrypt success).
    #
    # AST-based, NOT a regex on double-quoted literals: a reachability status
    # reintroduced via a variable (_set_decrypt_health(status)) or a single-quoted
    # literal must still be caught. Every call's arg must be a constant in the
    # per-user allowlist; a non-constant arg is itself a violation (reachability
    # is the only reason you'd pass a computed status here).
    import ast
    src = (Path(__file__).parent.parent / "tools" / "chat_resident_consumer.py").read_text()
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_set_decrypt_health"):
            arg = node.args[0] if node.args else None
            if (isinstance(arg, ast.Constant) and arg.value in {"degraded", "ok"}):
                continue
            literal = arg.value if isinstance(arg, ast.Constant) else "<non-literal>"
            offenders.append((node.lineno, literal))
    assert not offenders, (
        f"_set_decrypt_health called with a non per-user status "
        f"(reachability must route through _apply_infra_health): {offenders}"
    )
    assert '_apply_infra_health("unreachable")' in src    # the guarded route exists


def test_reading_older_than_refresh_is_not_reused(shared, monkeypatch):
    # The reuse grace is exactly REFRESH_SEC (no jitter): a reading past that is
    # re-probed, not reused — keeping the reported age within the freshness
    # budget without any POLL_TIMEOUT-coupled clamp.
    calls = _spy_measure(monkeypatch, result="ok")
    monkeypatch.setattr(c.time, "time", lambda: 10_000.0)
    _write_file(shared, "ok", 10_000.0 - 205.0)             # 205s: < REFRESH(210) → reuse
    c._maybe_refresh_decrypt_health()
    assert calls["n"] == 0
    assert c._decrypt_health["checked_at"] == 10_000.0 - 205.0
    _write_file(shared, "ok", 10_000.0 - 215.0)             # 215s: > REFRESH → re-probe
    c._decrypt_health_last_refresh["at"] = 0.0
    c._maybe_refresh_decrypt_health()
    assert calls["n"] == 1


def test_shared_probe_is_throttled_when_file_never_fresh(shared, monkeypatch):
    # Fail-open must not become a probe storm: if the shared file is unwritable /
    # never fresh, each consumer still probes at most once per refresh window
    # (not every ~30s idle cycle).
    monkeypatch.setattr(c, "_read_shared_infra_health", lambda: None)
    monkeypatch.setattr(c, "_write_shared_infra_health", lambda s, t: None)
    calls = _spy_measure(monkeypatch, result="unreachable")
    t = {"now": 7000.0}
    monkeypatch.setattr(c.time, "time", lambda: t["now"])
    for i in range(6):
        t["now"] = 7000.0 + i * 30.0                         # 6 idle cycles, span 150s
        c._maybe_refresh_decrypt_health()
    assert calls["n"] == 1, f"probe must be throttled to once/window, got {calls['n']}"


# --------------------------------------------------------------------------
# Stage 2: probe via /v1/decrypt/selfcheck (FEEDLING_DECRYPT_SELFCHECK)
# --------------------------------------------------------------------------

class _Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _RoutingClient:
    """Records the paths hit and returns a canned response per endpoint."""

    def __init__(self, responses):
        self.responses = responses           # substring → _Resp (or raises)
        self.paths = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.paths.append(url)
        for frag, resp in self.responses.items():
            if frag in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected url {url}")


@pytest.fixture()
def selfcheck(monkeypatch):
    monkeypatch.setattr(c, "DECRYPT_SELFCHECK", True)
    monkeypatch.setattr(c, "FEEDLING_ENCLAVE_URL", "https://enclave.example/")


def test_selfcheck_ok_maps_to_ok(selfcheck, monkeypatch):
    client = _RoutingClient({"/v1/decrypt/selfcheck":
                             _Resp(200, {"decrypt": "ok", "loopback": "ok"})})
    monkeypatch.setattr(c, "_client_for", lambda url: client)
    assert c._measure_infra_health() == "ok"
    assert any("/v1/decrypt/selfcheck" in p for p in client.paths)
    assert not any("/v1/chat/history" in p for p in client.paths)  # no user probe


def test_selfcheck_decrypt_fail_maps_unreachable(selfcheck, monkeypatch):
    client = _RoutingClient({"/v1/decrypt/selfcheck":
                             _Resp(200, {"decrypt": "fail", "loopback": "ok"})})
    monkeypatch.setattr(c, "_client_for", lambda url: client)
    assert c._measure_infra_health() == "unreachable"


def test_selfcheck_loopback_fail_maps_unreachable(selfcheck, monkeypatch):
    client = _RoutingClient({"/v1/decrypt/selfcheck":
                             _Resp(200, {"decrypt": "ok", "loopback": "fail"})})
    monkeypatch.setattr(c, "_client_for", lambda url: client)
    assert c._measure_infra_health() == "unreachable"


def test_selfcheck_404_falls_back_to_history_probe(selfcheck, monkeypatch):
    # Old enclave without the endpoint → 404 → transparently fall back.
    client = _RoutingClient({
        "/v1/decrypt/selfcheck": _Resp(404, {}),
        "/v1/chat/history": _Resp(200, {}),
    })
    monkeypatch.setattr(c, "_client_for", lambda url: client)
    assert c._measure_infra_health() == "ok"
    assert any("/v1/decrypt/selfcheck" in p for p in client.paths)
    assert any("/v1/chat/history" in p for p in client.paths)     # fell back


def test_selfcheck_dial_error_maps_unreachable(selfcheck, monkeypatch):
    client = _RoutingClient({"/v1/decrypt/selfcheck": RuntimeError("boom")})
    monkeypatch.setattr(c, "_client_for", lambda url: client)
    assert c._measure_infra_health() == "unreachable"


def test_selfcheck_decrypt_fail_logs_keydrift_not_network(selfcheck, monkeypatch, caplog):
    # decrypt:fail is content-key drift (enclave reachable, key can't decrypt).
    # It still gates via 'unreachable', but must log the ACTUAL fault so the
    # operator isn't sent to fix network/TLS for a key-drift problem.
    client = _RoutingClient({"/v1/decrypt/selfcheck":
                             _Resp(200, {"decrypt": "fail", "loopback": "ok"})})
    monkeypatch.setattr(c, "_client_for", lambda url: client)
    import logging
    with caplog.at_level(logging.ERROR, logger=c.log.name):
        assert c._measure_via_selfcheck() == "unreachable"
    joined = " ".join(r.message for r in caplog.records).lower()
    assert "content key" in joined or "content-key" in joined or "drift" in joined
    assert "network" in joined  # explicitly steers AWAY from network remediation


def test_selfcheck_401_falls_back_to_history_probe(selfcheck, monkeypatch):
    # The endpoint now requires a locally-verifiable runtime token; a keyed
    # consumer holding only an api_key gets 401 and must fall back to the history
    # probe (which its api_key can serve), NOT report a false 'unreachable'.
    client = _RoutingClient({
        "/v1/decrypt/selfcheck": _Resp(401, {}),
        "/v1/chat/history": _Resp(200, {}),
    })
    monkeypatch.setattr(c, "_client_for", lambda url: client)
    assert c._measure_infra_health() == "ok"
    assert any("/v1/chat/history" in p for p in client.paths)     # fell back


def test_flag_off_never_calls_selfcheck(monkeypatch):
    monkeypatch.setattr(c, "DECRYPT_SELFCHECK", False)
    monkeypatch.setattr(c, "FEEDLING_ENCLAVE_URL", "https://enclave.example/")
    client = _RoutingClient({"/v1/chat/history": _Resp(200, {})})
    monkeypatch.setattr(c, "_client_for", lambda url: client)
    assert c._measure_infra_health() == "ok"
    assert not any("/v1/decrypt/selfcheck" in p for p in client.paths)


def test_flag_off_never_touches_shared_file(shared, monkeypatch):
    # With the flag off the legacy per-consumer probe runs and the shared file
    # is neither read nor written.
    monkeypatch.setattr(c, "DECRYPT_HEALTH_SHARED", False)
    probed = {"n": 0}
    monkeypatch.setattr(c, "_probe_decrypt_reachability",
                        lambda: probed.__setitem__("n", probed["n"] + 1))
    monkeypatch.setattr(c.time, "time", lambda: 6000.0)
    c._maybe_refresh_decrypt_health()
    assert probed["n"] == 1
    assert not shared.exists()
