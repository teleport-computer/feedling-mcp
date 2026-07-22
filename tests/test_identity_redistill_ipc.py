"""
identity-redistill: io_cli <-> resident consumer local IPC (T11)
==================================================================

Two independent surfaces, tested at the level each actually owns:

- io_cli side (client): socket round-trip against a STUB listener — no
  consumer code involved. Covers the 64KB local cap, "consumer not running",
  and the same-request_id retry-once-on-timeout contract.
- consumer side (server): direct handler-function tests
  (``crc._handle_redistill_ipc`` / ``crc._build_redistill_envelope``) with
  ``crc._HTTP.post`` monkeypatched — no real network, no DB. Covers
  request_id dedup, the 409 -> "already_running" mapping, generic HTTP
  errors, and — the one crypto-touching test — that the request body POSTed
  to the backend never carries the material's plaintext.

Pure-unit: no Postgres, no real sockets to the internet, only loopback
AF_UNIX sockets on tmp_path. Safe to add to conftest's _PURE_UNIT allowlist.

Run with: pytest tests/test_identity_redistill_ipc.py -v
"""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module bootstrap — mirrors test_chat_resident_consumer.py's header exactly,
# so this file also runs standalone (`pytest tests/test_identity_redistill_ipc.py`)
# without depending on collection order.
# ---------------------------------------------------------------------------
_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_identity_redistill",
    "AGENT_MODE": "http",
    "CHECKPOINT_FILE": "/tmp/feedling_test_checkpoint_redistill.json",
}
for _k, _v in _ENV_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {
        "v": 1, "stub": True, "id": kw.get("item_id") or "stub",
        "owner_user_id": kw.get("owner_user_id"),
    }
    sys.modules.setdefault("content_encryption", _fake_enc)

import tools.chat_resident_consumer as crc  # noqa: E402
import io_cli  # noqa: E402


def _content_encryption_is_real() -> bool:
    """True only when the CURRENTLY-bound ``content_encryption`` module is the
    real, file-backed backend module — never the in-file fake stub above
    (which has no ``__file__`` and drops the plaintext entirely, returning a
    fixed marker dict regardless of input).

    Checked at CALL time, not at this file's own import time above: pytest
    caches modules process-wide, so a DIFFERENT test file imported earlier in
    the same session may already have installed the fake into
    ``sys.modules["content_encryption"]`` before this file's own
    ``import content_encryption`` line even runs — that import would then
    silently succeed against the cached fake (no ``ModuleNotFoundError``),
    which would make a naive "did the try/except above take the except
    branch" flag say "real" while ``crc._build_envelope`` is actually still
    the fake lambda. This is exactly the vacuous-pass risk flagged in review:
    the leak test must fail closed (skip, not silently pass) whenever it
    cannot tell the difference."""
    mod = sys.modules.get("content_encryption")
    return mod is not None and getattr(mod, "__file__", None) is not None


@pytest.fixture(autouse=True)
def _isolate_redistill_state():
    """Every test gets a clean in-memory dedupe dict — this global otherwise
    leaks request_ids across tests (same failure shape the proactive-guard
    fixture in test_chat_resident_consumer.py exists to prevent)."""
    crc._redistill_ipc_seen.clear()
    yield
    crc._redistill_ipc_seen.clear()


# ---------------------------------------------------------------------------
# io_cli side: socket round-trip against a stub listener
# ---------------------------------------------------------------------------

class _StubListener:
    """A minimal one-line-JSON-in / one-line-JSON-out Unix socket server.

    ``handlers`` is a list of callables ``(request_dict) -> dict | None``,
    one per accepted connection, in order; returning ``None`` closes the
    connection WITHOUT replying (simulates a consumer that never answers —
    the client should treat that the same as a timeout and retry)."""

    def __init__(self, sock_path: Path, handlers):
        self.sock_path = sock_path
        self.handlers = handlers
        self.received: list[dict] = []
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(sock_path))
        self._srv.listen(4)
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _serve(self):
        for handler in self.handlers:
            try:
                conn, _addr = self._srv.accept()
            except OSError:
                return
            try:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                req = json.loads(buf.decode("utf-8").strip())
                self.received.append(req)
                reply = handler(req)
                if reply is not None:
                    conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        try:
            self._srv.close()
        except Exception:
            pass

    def join(self, timeout=5):
        self._thread.join(timeout=timeout)


@pytest.fixture
def short_tmp_dir():
    """AF_UNIX's sun_path is capped at ~104-108 bytes on macOS/BSD — pytest's
    default ``tmp_path`` (nested under a long per-test directory name) blows
    right past that. Use a short-named dir directly under /tmp instead."""
    d = tempfile.mkdtemp(prefix="fio_")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def sock_path(short_tmp_dir):
    return short_tmp_dir / "resident_ipc.sock"


def test_io_cli_round_trip_success(monkeypatch, sock_path):
    listener = _StubListener(
        sock_path, [lambda req: {"ok": True, "job_id": "genesis_abc123"}],
    ).start()
    monkeypatch.setattr(io_cli, "_resident_ipc_sock_path", lambda: str(sock_path))

    reply = io_cli._resident_ipc_request("some material", timeout=5)
    listener.join()

    assert reply["ok"] is True
    assert reply["job_id"] == "genesis_abc123"
    assert len(listener.received) == 1
    assert listener.received[0]["op"] == "redistill"
    assert listener.received[0]["material"] == "some material"
    assert reply["request_id"] == listener.received[0]["request_id"]


def test_io_cli_consumer_not_running_when_socket_missing(short_tmp_dir, monkeypatch):
    missing = short_tmp_dir / "no_such.sock"
    monkeypatch.setattr(io_cli, "_resident_ipc_sock_path", lambda: str(missing))

    reply = io_cli._resident_ipc_request("hello", timeout=1)

    assert reply["ok"] is False
    assert reply["error"] == "consumer_not_running"
    assert "request_id" in reply


def test_io_cli_material_over_64kb_rejected_locally(monkeypatch, short_tmp_dir):
    """cmd_identity_redistill must reject an oversized --material-text BEFORE
    ever touching the socket (exit 2) — no listener needed for this test."""
    monkeypatch.setattr(io_cli, "_resident_ipc_sock_path",
                        lambda: str(short_tmp_dir / "unused.sock"))
    calls = []
    monkeypatch.setattr(io_cli, "_resident_ipc_request",
                        lambda material, **kw: calls.append(material) or {"ok": True})

    big = "a" * (io_cli._RESIDENT_REDISTILL_MAX_MATERIAL_BYTES + 1)
    args = types.SimpleNamespace(material_file=None, material_text=big)
    with pytest.raises(SystemExit) as exc:
        io_cli.cmd_identity_redistill(args)

    assert exc.value.code == 2
    assert calls == []  # never reached the IPC layer


def test_io_cli_retries_once_with_same_request_id_on_timeout(monkeypatch, sock_path):
    """Attempt 1 hangs past the client's timeout (consumer alive but slow);
    the client must retry EXACTLY once, reusing the SAME request_id, and
    succeed on attempt 2."""
    seen_ids = []
    client_timeout = 0.4

    def _slow_no_reply(req):
        seen_ids.append(req["request_id"])
        time.sleep(client_timeout * 1.5)  # outlives attempt 1's own timeout
        return None  # never replies on this connection

    def _fast_ok(req):
        seen_ids.append(req["request_id"])
        return {"ok": True, "job_id": "genesis_retry_ok"}

    listener = _StubListener(sock_path, [_slow_no_reply, _fast_ok]).start()
    monkeypatch.setattr(io_cli, "_resident_ipc_sock_path", lambda: str(sock_path))

    reply = io_cli._resident_ipc_request("retry material", timeout=client_timeout)
    listener.join()

    assert reply["ok"] is True
    assert reply["job_id"] == "genesis_retry_ok"
    assert len(seen_ids) == 2
    assert seen_ids[0] == seen_ids[1] == reply["request_id"]


def test_io_cli_reports_uncertainty_after_both_attempts_time_out(monkeypatch, sock_path):
    client_timeout = 0.3

    def _never(req):
        time.sleep(client_timeout * 2)
        return None

    listener = _StubListener(sock_path, [_never, _never]).start()
    monkeypatch.setattr(io_cli, "_resident_ipc_sock_path", lambda: str(sock_path))

    reply = io_cli._resident_ipc_request("material", timeout=client_timeout)
    listener.join()

    assert reply["ok"] is False
    assert reply["error"] == "timeout_uncertain"
    assert "request_id" in reply


# ---------------------------------------------------------------------------
# consumer side: direct handler-function tests, HTTP monkeypatched
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def redistill_home(tmp_path, monkeypatch):
    monkeypatch.setattr(crc, "RESIDENT_IPC_STATE_FILE", tmp_path / "resident_ipc_state.json")
    return tmp_path


def test_handle_redistill_ipc_posts_sealed_update_identity_job(monkeypatch, redistill_home):
    captured = {}

    def _fake_build_envelope(material, *, item_id):
        return {"sealed_marker": True, "item_id": item_id}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeResp(200, {"job": {"job_id": "genesis_new123", "status": "processing"}})

    monkeypatch.setattr(crc, "_build_redistill_envelope", _fake_build_envelope)
    monkeypatch.setattr(crc._HTTP, "post", _fake_post)

    reply = crc._handle_redistill_ipc(
        {"op": "redistill", "request_id": "req-1", "material": "hello world"})

    assert reply == {"ok": True, "job_id": "genesis_new123", "request_id": "req-1"}
    assert captured["url"].endswith("/v1/genesis/imports/plaintext")
    body = captured["json"]
    assert body["format"] == "sealed_v1"
    assert body["mode"] == "update_identity"
    assert body["job_kind"] == "resident_redistill"
    assert body["client_job_id"] == "req-1"
    assert body["envelope"] == {"sealed_marker": True, "item_id": "req-1"}


def test_handle_redistill_ipc_dedupes_by_request_id_without_reposting(monkeypatch, redistill_home):
    post_calls = []

    monkeypatch.setattr(crc, "_build_redistill_envelope", lambda material, *, item_id: {"e": 1})

    def _fake_post(url, json=None, headers=None, timeout=None):
        post_calls.append(json)
        return _FakeResp(200, {"job": {"job_id": "genesis_once"}})

    monkeypatch.setattr(crc._HTTP, "post", _fake_post)

    msg = {"op": "redistill", "request_id": "dup-1", "material": "same material"}
    first = crc._handle_redistill_ipc(msg)
    second = crc._handle_redistill_ipc(msg)

    assert first == second == {"ok": True, "job_id": "genesis_once", "request_id": "dup-1"}
    assert len(post_calls) == 1  # second call served entirely from the dedupe cache


def test_handle_redistill_ipc_dedup_survives_in_memory_cache_eviction(monkeypatch, redistill_home):
    """Restart-safety: the disk-backed state file must recover a reply even
    when the in-memory dict alone was cleared (simulates a consumer restart
    between io_cli's two retry attempts)."""
    monkeypatch.setattr(crc, "_build_redistill_envelope", lambda material, *, item_id: {"e": 1})
    post_calls = []

    def _fake_post(url, json=None, headers=None, timeout=None):
        post_calls.append(json)
        return _FakeResp(200, {"job": {"job_id": "genesis_persisted"}})

    monkeypatch.setattr(crc._HTTP, "post", _fake_post)

    msg = {"op": "redistill", "request_id": "restart-1", "material": "m"}
    first = crc._handle_redistill_ipc(msg)
    assert first["ok"] is True

    crc._redistill_ipc_seen.clear()  # simulate the process having restarted
    second = crc._handle_redistill_ipc(msg)

    assert second == first
    assert len(post_calls) == 1  # never reposted — recovered from disk


def test_handle_redistill_ipc_material_too_large_never_seals_or_posts(monkeypatch, redistill_home):
    build_calls = []
    post_calls = []
    monkeypatch.setattr(crc, "_build_redistill_envelope",
                        lambda material, *, item_id: build_calls.append(1) or {})
    monkeypatch.setattr(crc._HTTP, "post",
                        lambda *a, **kw: post_calls.append(1) or _FakeResp(200, {}))

    big = "a" * (crc._REDISTILL_IPC_MAX_MATERIAL_BYTES + 1)
    reply = crc._handle_redistill_ipc(
        {"op": "redistill", "request_id": "big-1", "material": big})

    assert reply["ok"] is False
    assert reply["error"] == "material_too_large"
    assert reply["max_bytes"] == crc._REDISTILL_IPC_MAX_MATERIAL_BYTES
    assert not build_calls
    assert not post_calls


def test_handle_redistill_ipc_maps_409_to_already_running(monkeypatch, redistill_home):
    monkeypatch.setattr(crc, "_build_redistill_envelope", lambda material, *, item_id: {"e": 1})
    monkeypatch.setattr(
        crc._HTTP, "post",
        lambda *a, **kw: _FakeResp(409, {"error": "redistill_job_active",
                                          "active_job_id": "genesis_running_1"}),
    )

    reply = crc._handle_redistill_ipc(
        {"op": "redistill", "request_id": "conflict-1", "material": "m"})

    assert reply == {
        "ok": False, "error": "already_running",
        "active_job_id": "genesis_running_1", "request_id": "conflict-1",
    }


def test_handle_redistill_ipc_generic_http_error_surfaced(monkeypatch, redistill_home):
    monkeypatch.setattr(crc, "_build_redistill_envelope", lambda material, *, item_id: {"e": 1})
    monkeypatch.setattr(
        crc._HTTP, "post", lambda *a, **kw: _FakeResp(500, {"error": "boom"}),
    )

    reply = crc._handle_redistill_ipc(
        {"op": "redistill", "request_id": "err-1", "material": "m"})

    assert reply["ok"] is False
    assert reply["error"].startswith("http_500")
    assert reply["request_id"] == "err-1"


def test_handle_redistill_ipc_missing_request_id_rejected(redistill_home):
    reply = crc._handle_redistill_ipc({"op": "redistill", "material": "m"})
    assert reply == {"ok": False, "error": "request_id_required"}


def test_request_body_never_carries_material_plaintext(monkeypatch, redistill_home):
    """The one crypto-touching test: seal with the REAL _build_redistill_envelope
    (real content_encryption.build_envelope) and assert the exact JSON body
    POSTed to the backend never contains the material's plaintext, anywhere,
    as a substring — not just in an 'envelope' field some other refactor
    could rename away from.

    Must fail CLOSED, not vacuously pass: if the real backend module isn't
    importable in this environment, ``crc._build_envelope`` is the in-file
    fake stub (see the bootstrap above) which drops the material entirely —
    the assertion below would then trivially hold regardless of whether the
    real code has a leak. Skip rather than give a false green."""
    if not _content_encryption_is_real():
        pytest.skip(
            "real content_encryption module not bound (crc._build_envelope is the "
            "fake stub) — the plaintext-leak assertion would be vacuous here"
        )
    assert crc._build_envelope.__module__ == "content_encryption", (
        "crc._build_envelope is not content_encryption.build_envelope — "
        "the leak assertion below would not be testing the real seal path"
    )
    monkeypatch.setattr(crc, "_whoami_cache_loaded_at", time.monotonic())
    monkeypatch.setitem(crc._whoami_cache, "user_id", "usr_redistill_test")
    monkeypatch.setitem(crc._whoami_cache, "user_pk", os.urandom(32))
    monkeypatch.setitem(crc._whoami_cache, "enclave_pk", os.urandom(32))

    marker = "SEKRIT-PLAINTEXT-MARKER-xyz123abc-do-not-leak"
    captured = {}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _FakeResp(200, {"job": {"job_id": "genesis_sealed_ok"}})

    monkeypatch.setattr(crc._HTTP, "post", _fake_post)

    reply = crc._handle_redistill_ipc(
        {"op": "redistill", "request_id": "seal-1", "material": marker})

    assert reply["ok"] is True
    serialized = json.dumps(captured["json"], default=str, ensure_ascii=False)
    assert marker not in serialized
    assert marker.encode("utf-8").hex() not in serialized  # not even hex-encoded raw


def test_build_redistill_envelope_reuses_capture_style_seal(monkeypatch):
    """_build_redistill_envelope must go through the SAME _build_envelope
    entry point _capture_build_envelope uses (no parallel crypto path) and
    seal with visibility=shared so the enclave can open it for resident_pending."""
    calls = []

    def _fake_build_envelope(**kw):
        calls.append(kw)
        return {"v": 1, "id": kw["item_id"]}

    monkeypatch.setattr(crc, "_build_envelope", _fake_build_envelope)
    monkeypatch.setattr(crc, "_whoami_cache_loaded_at", time.monotonic())
    monkeypatch.setitem(crc._whoami_cache, "user_id", "usr_x")
    monkeypatch.setitem(crc._whoami_cache, "user_pk", b"\x01" * 32)
    monkeypatch.setitem(crc._whoami_cache, "enclave_pk", b"\x02" * 32)

    env = crc._build_redistill_envelope("material text", item_id="req-abc")

    assert env == {"v": 1, "id": "req-abc"}
    assert len(calls) == 1
    assert calls[0]["owner_user_id"] == "usr_x"
    assert calls[0]["visibility"] == "shared"
    assert calls[0]["item_id"] == "req-abc"
    assert calls[0]["plaintext"] == b"material text"


def test_redistill_ipc_refuses_to_bind_in_dir_not_owned_by_us(monkeypatch, short_tmp_dir, caplog):
    """Directory-ownership hardening (Codex review, T11 follow-up): a
    pre-existing socket dir owned by a DIFFERENT uid must refuse to bind
    rather than silently listen there (possible /tmp squat). Simulated
    without root by making ``os.getuid()`` disagree with the real owner of a
    dir this test process itself created — the code path exercised is
    identical to a genuine uid mismatch."""
    if crc._IS_WINDOWS:
        pytest.skip("uid ownership check is POSIX-only")
    sock_path = short_tmp_dir / "sub" / "resident_ipc.sock"
    real_getuid = os.getuid
    monkeypatch.setattr(os, "getuid", lambda: real_getuid() + 1)

    with caplog.at_level("ERROR"):
        crc._redistill_ipc_serve_forever(sock_path)  # returns immediately, no accept loop

    assert not sock_path.exists()
    assert any("not ours" in rec.message or "refusing to bind" in rec.message
               for rec in caplog.records)


# ---------------------------------------------------------------------------
# Full listener loop (thread target), one round trip end to end
# ---------------------------------------------------------------------------

def test_redistill_ipc_serve_forever_end_to_end(monkeypatch, short_tmp_dir):
    """Starts the REAL consumer-side listener thread (_redistill_ipc_serve_forever)
    against a temp socket, with only the network POST monkeypatched, and drives
    one request through it from a real client socket — exercising the exact
    accept/recv/reply loop the process runs in production."""
    sock_path = short_tmp_dir / "resident_ipc.sock"
    monkeypatch.setattr(crc, "RESIDENT_IPC_STATE_FILE", short_tmp_dir / "state.json")
    monkeypatch.setattr(crc, "_build_redistill_envelope", lambda material, *, item_id: {"e": 1})
    monkeypatch.setattr(
        crc._HTTP, "post",
        lambda *a, **kw: _FakeResp(200, {"job": {"job_id": "genesis_e2e"}}),
    )
    monkeypatch.setattr(crc, "_running", True)

    thread = threading.Thread(
        target=crc._redistill_ipc_serve_forever, args=(sock_path,), daemon=True)
    thread.start()
    try:
        deadline = time.time() + 5
        while not sock_path.exists() and time.time() < deadline:
            time.sleep(0.02)
        assert sock_path.exists(), "listener never bound the socket"

        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(str(sock_path))
        s.sendall((json.dumps(
            {"op": "redistill", "request_id": "e2e-1", "material": "end to end"}) + "\n"
        ).encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        s.close()
        reply = json.loads(buf.decode("utf-8").strip())
        assert reply == {"ok": True, "job_id": "genesis_e2e", "request_id": "e2e-1"}
    finally:
        monkeypatch.setattr(crc, "_running", False)
        # Unblock the listener's accept() (bounded by its own 1s timeout) and
        # let it observe _running=False and exit cleanly.
        thread.join(timeout=3)
