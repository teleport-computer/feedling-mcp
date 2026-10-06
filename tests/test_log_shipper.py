"""Real HTTP Docker framing + durable spool and privacy export contracts."""
from __future__ import annotations

import ast
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import struct
import sys
import threading
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import enclave_reqlog_contract as contract
from ops import log_shipper as ship
from ops import log_shipper_store as spool

NOW = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
NAMES = {"enclave": "feedling-test-enclave-1", "enclave-domain": "feedling-test-enclave-domain-1"}
CID = "a" * 64
CID2 = "b" * 64


def record(**updates):
    row = dict(ts="2026-09-17T12:00:00.123Z", pid=23, method="POST", route="/v1/envelope/decrypt",
               status=200, dur_ms=12.3, purpose="v2_chat_read", auth_kind="runtime_token",
               whoami_source="local_token", whoami_ms=None, decrypt_ms=4.1, decrypt_queue_ms=2.2,
               user_prefix="usr_12345678", failure_class="none", req_bytes=234, resp_bytes=567)
    row.update(updates)
    return json.dumps(row, separators=(",", ":")).encode()


def line(body=None, timestamp="2026-09-17T12:00:00.123456789Z"):
    return timestamp.encode() + b" " + (record() if body is None else body) + b"\n"


def frame(payload, stream=1):
    return struct.pack(">BxxxI", stream, len(payload)) + payload


class Response(io.BytesIO):
    def __init__(self, data, chunk=2**20):
        super().__init__(data)
        self.chunk = chunk
    def read(self, size=-1):
        return super().read(min(size, self.chunk) if size >= 0 else self.chunk)


@contextmanager
def docker_http(responses, ids=None):
    calls = []
    ids = ids or [CID]
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        def do_GET(self):
            calls.append(self.path)
            if urlsplit(self.path).path == "/containers/json":
                current = ids.pop(0) if len(ids) > 1 else ids[0]
                body = json.dumps([{"Id": current, "Names": ["/unrelated", "/" + NAMES["enclave"]]},
                                   {"Id": "c" * 64, "Names": ["/backend"]}]).encode()
            else:
                body = responses.pop(0)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            for i in range(0, len(body), 7):
                self.wfile.write(body[i:i+7])
                self.wfile.flush()
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def collector(tmp_path, client, bucket="", clock=lambda: NOW):
    return ship.LogShipper(client, spool.HourlyGzStore(tmp_path),
                           ship.R2Uploader(bucket, "test", "test-cvm", credentials=lambda: True),
                           NAMES, clock=clock)


def contents(store):
    return b"".join(gzip.decompress(p.read_bytes()) for p in store.archives())


def test_contract_shared_with_emitter_and_routes():
    from enclave.routes import _reqlog
    assert contract.FIELDS is _reqlog.FIELDS
    assert contract.FAILURE_CLASSES is _reqlog.FAILURE_CLASSES
    routes = set()
    for path in (Path(__file__).resolve().parents[1] / "backend/enclave/routes").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in node.decorator_list:
                    if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and isinstance(d.func.value, ast.Name) and d.func.value.id == "router":
                        routes.add(d.args[0].value)
    assert contract.ROUTES == routes | {"<unmatched>"}


def test_normal_json_retained_byte_for_byte():
    assert ship.safe_line(record()) == record()


@pytest.mark.parametrize("bad", [b'-----BEGIN PRIVATE KEY-----', b'"envelope"', b'"ciphertext"',
                                  b'"plaintext_b64"', b'ab' * 32, b'AB' * 32])
def test_deny_is_an_independent_backstop(bad, monkeypatch):
    # Make the primary recognizer permissive so removal of deny genuinely fails.
    monkeypatch.setattr(contract, "valid_request_line", lambda _: True)
    assert ship.safe_line(bad) is None
    assert ship.safe_line(b"normal") == b"normal"


@pytest.mark.parametrize("updates", [dict(route="/secret?key=token"), dict(user_prefix="usr_complete_user_secret"),
    dict(purpose="secret"), dict(failure_class="private exception"), dict(auth_kind="secret"),
    dict(whoami_source="secret"), dict(method="secret"), dict(ts="secret"), dict(pid=True),
    dict(status="200"), dict(req_bytes=-1), dict(resp_bytes="secret"), dict(decrypt_ms=float("nan")),
    dict(whoami_ms=float("inf")), dict(dur_ms=None), dict(extra="secret"), dict(decrypt_queue_ms={"x":"secret"})])
def test_untrusted_fields_never_leave(updates):
    assert ship.safe_line(record(**updates)) is None


@pytest.mark.parametrize("body", [b'[chat/history:usr_secret] context_memories failed: secret',
 b'screen_frame_image_absent user_id=usr_secret frame_id=secret',
 b'envelope_aead_verify_failed authorized_user_id=usr_secret item_id=secret',
 b'raw-secret', b'{"pid":1,"pid":2}', b'[' * 2000, b'WORKER TIMEOUT secret'])
def test_old_free_text_and_malformed_lines_drop(body):
    assert ship.safe_line(body) is None


@pytest.mark.parametrize("message,event", [(b"WORKER TIMEOUT (pid:32)", "worker_timeout"),
    (b"Booting worker with pid: 32", "worker_boot"), (b"Worker exiting (pid: 32)", "worker_exit")])
def test_lifecycle_is_reencoded_not_raw(message, event):
    raw = b"[2026-09-17 12:34:56 +0000] [4] [INFO] " + message
    assert json.loads(ship.safe_line(raw)) == {"event": event, "pid": 32}
    assert ship.safe_line(raw + b" secret") is None


def test_real_http_frames_disconnect_resume_restart_and_recreation(tmp_path):
    first, second, replacement = line(), line(timestamp="2026-09-17T12:00:01.1Z"), line(timestamp="2026-09-17T11:59:59.1Z")
    responses = [frame(first) + frame(second)[:-10], frame(first) + frame(second), frame(replacement)]
    with docker_http(responses, ids=[CID, CID, CID2]) as (url, calls):
        client = ship.DockerLogsClient(url)
        worker = collector(tmp_path, client)
        with pytest.raises(EOFError): worker.follow_once("enclave")
        assert worker.store.cursor("enclave", CID) == (int(NOW.timestamp()) * 10**9 + 123456789, 1)
        # Simulate process restart: resume from durable cursor, not process memory.
        worker = collector(tmp_path, client)
        with pytest.raises(EOFError): worker.follow_once("enclave")
        with pytest.raises(EOFError): worker.follow_once("enclave")
        worker.store.seal(all_hours=True)
        data = contents(worker.store)
        assert data.count(first) == data.count(second) == data.count(replacement) == 1
        logs = [urlsplit(p) for p in calls if "/logs" in p]
        queries = [parse_qs(p.query) for p in logs]
        assert queries[0]["since"] == ["0"]
        assert queries[1]["since"] == [f"{int(NOW.timestamp())}.123456789"]
        assert queries[2]["since"] == ["0"]  # new ID gets its full retained history
        assert CID2 in logs[2].path
        assert all(q["follow"] == ["1"] and q["timestamps"] == ["1"] and q["stdout"] == ["1"] and q["stderr"] == ["1"] for q in queries)
        assert worker.counters.values["stored_lines"] == 2


def test_mux_partial_frames_separate_stdout_stderr_and_bound_line():
    a, b = line(), line(timestamp="2026-09-17T12:00:02Z")
    wire = frame(a[:10]) + frame(b, 2) + frame(a[10:]) + frame(b"z" * (ship.MAX_LINE+100) + b"\n")
    client = ship.DockerLogsClient("http://proxy", opener=lambda *a, **kw: Response(wire, chunk=3))
    actual = []
    with pytest.raises(EOFError):
        actual.extend(client.lines(CID, 0, threading.Event()))
    assert actual == [b, a, None]


@pytest.mark.parametrize("wire", [b"raw tty\n", struct.pack(">BxxxI", 3, 0), struct.pack(">BxxxI", 1, ship.MAX_FRAME+1)])
def test_rejects_tty_invalid_stream_and_oversize_frame(wire):
    client = ship.DockerLogsClient("http://proxy", opener=lambda *a, **kw: Response(wire))
    with pytest.raises((ValueError, EOFError)):
        list(client.lines(CID, 0, threading.Event()))


def test_stream_cycle_deadline_and_socket_timeout_are_bounded():
    calls = []
    tick = iter([0, 0, 31])
    def opener(request, timeout):
        calls.append(timeout)
        return Response(frame(line()))
    client = ship.DockerLogsClient("http://proxy", opener=opener, monotonic=lambda: next(tick))
    with pytest.raises(TimeoutError):
        list(client.lines(CID, 0, threading.Event()))
    assert calls == [10]


def test_failed_append_cannot_advance_checkpoint(tmp_path, monkeypatch):
    with docker_http([frame(line())]) as (url, _):
        worker = collector(tmp_path, ship.DockerLogsClient(url))
        def fail(*_): raise OSError("secret disk error")
        monkeypatch.setattr(worker.store, "append", fail)
        with pytest.raises(OSError): worker.follow_once("enclave")
        assert worker.store.cursor("enclave", CID) == (0, 0)
        assert worker.counters.values["storage_errors"] == 1


def test_dropped_lines_counted_but_not_written(tmp_path):
    with docker_http([frame(line(b'"envelope": "secret"')) + frame(line())]) as (url, _):
        worker = collector(tmp_path, ship.DockerLogsClient(url))
        with pytest.raises(EOFError): worker.follow_once("enclave")
        worker.store.seal(all_hours=True)
        assert contents(worker.store) == line()
        assert worker.counters.values["dropped_lines"] == 1
        assert b"secret" not in contents(worker.store)


def test_hour_rotation_late_append_restart_and_retention(tmp_path):
    store = spool.HourlyGzStore(tmp_path)
    store.append("enclave", NOW, line())
    store.seal(NOW + timedelta(minutes=59))
    assert not store.archives()
    store.seal(NOW + timedelta(hours=1))
    assert contents(store) == line()
    store.append("enclave", NOW, b"late\n")
    store.seal(NOW + timedelta(hours=1))
    assert contents(store) == line() + b"late\n"
    store.append("enclave-domain", NOW, b"restart\n")
    restarted = spool.HourlyGzStore(tmp_path)
    restarted.seal(all_hours=True)
    assert b"restart\n" in contents(restarted)
    assert restarted.prune(NOW + timedelta(days=30, minutes=59)) == 0
    assert restarted.prune(NOW + timedelta(days=30, hours=1)) == 2
    assert restarted.archives() == []


def test_atomic_seal_failure_keeps_previous_archive_and_part(tmp_path, monkeypatch):
    store = spool.HourlyGzStore(tmp_path)
    store.append("enclave", NOW, b"first\n");store.seal(all_hours=True)
    store.append("enclave", NOW, b"second\n")
    original = spool.os.replace
    def fail(src, dst):
        if str(dst).endswith(".gz"): raise OSError("disk")
        return original(src, dst)
    monkeypatch.setattr(spool.os, "replace", fail)
    with pytest.raises(OSError): store.seal(all_hours=True)
    assert contents(store) == b"first\n"
    monkeypatch.setattr(spool.os, "replace", original)
    store.seal(all_hours=True)
    assert contents(store) == b"first\nsecond\n"


def test_r2_retry_pending_reupload_and_exact_key(tmp_path):
    store = spool.HourlyGzStore(tmp_path)
    store.append("enclave", NOW, line());store.seal(all_hours=True)
    calls, delays = [], []
    class Client:
        broken = True
        def put_object(self, **kwargs):
            calls.append((kwargs["Bucket"], kwargs["Key"], kwargs["Body"].read()))
            if self.broken: raise RuntimeError("private credentials error")
    client = Client()
    uploader = ship.R2Uploader("logs-bucket", "prod", "cvm", lambda: client, lambda: True,
                              wait=lambda t: delays.append(t) or False)
    counters = ship.Counters()
    uploader.upload(store, counters)
    assert len(calls) == 3 and [x for x in delays if x] == [1, 2]
    assert len(set(body for _,_,body in calls)) == 1
    assert not list(tmp_path.rglob("*.sent"))
    client.broken = False
    uploader.upload(store, counters)
    assert calls[-1][:2] == ("logs-bucket", "prod/cvm/enclave/2026-09-17/12.log.gz")
    assert gzip.decompress(calls[-1][2]) == line()
    uploader.upload(store, counters)
    assert len(calls) == 4  # receipt suppresses duplicate upload
    store.append("enclave", NOW, b"late\n");store.seal(all_hours=True)
    uploader.upload(store, counters)
    assert len(calls) == 5 and gzip.decompress(calls[-1][2]) == line() + b"late\n"


def test_empty_bucket_still_spools_and_warns_once_per_hour(tmp_path, capsys):
    client = ship.DockerLogsClient("http://proxy")
    worker = collector(tmp_path, client)
    worker.store.append("enclave", NOW - timedelta(hours=1), line())
    worker.uploader.client_factory = lambda: pytest.fail("empty bucket must never create R2 client")
    worker.maintenance(); worker.maintenance()
    worker.uploader.upload(worker.store, worker.counters)
    assert contents(worker.store) == line()
    row, = [json.loads(s) for s in capsys.readouterr().out.splitlines()]
    assert set(row) == set(ship.STATUS_FIELDS)
    assert row["r2_state"] == "R2 未配置"
    worker.clock = lambda: NOW + timedelta(hours=1)
    worker.maintenance()
    assert len(capsys.readouterr().out.splitlines()) == 1


def test_missing_credentials_keeps_local_data(tmp_path):
    store = spool.HourlyGzStore(tmp_path)
    store.append("enclave", NOW, line());store.seal(all_hours=True)
    uploader = ship.R2Uploader("logs", "test", "cvm", client_factory=lambda: pytest.fail("no credentials"), credentials=lambda: False)
    uploader.upload(store, ship.Counters())
    assert uploader.state == "credentials_missing" and contents(store) == line()


def test_partial_crash_tail_is_replayed_without_corrupting_next_line(tmp_path):
    store = spool.HourlyGzStore(tmp_path)
    store.append("enclave", NOW, line())
    part = next(tmp_path.rglob("*.part"))
    with part.open("ab") as file:
        file.write(b"partial-unacknowledged")
    store.append("enclave", NOW, line(timestamp="2026-09-17T12:00:01Z"))
    store.seal(all_hours=True)
    assert contents(store) == line() + line(timestamp="2026-09-17T12:00:01Z")


def test_crash_between_gzip_replace_and_part_unlink_is_duplicate_not_loss(tmp_path, monkeypatch):
    store = spool.HourlyGzStore(tmp_path)
    store.append("enclave", NOW, line())
    original = Path.unlink
    def fail(path, *args, **kwargs):
        if path.suffix == ".part": raise OSError("interrupted")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail)
    with pytest.raises(OSError): store.seal(all_hours=True)
    assert contents(store) == line()
    monkeypatch.setattr(Path, "unlink", original)
    store.seal(all_hours=True)
    assert contents(store) == line() * 2


def test_late_replace_during_upload_cannot_mark_new_data_sent(tmp_path):
    store = spool.HourlyGzStore(tmp_path)
    store.append("enclave", NOW, line());store.seal(all_hours=True)
    path, = store.archives()
    stream, digest = store.snapshot(path)
    with stream:
        store.append("enclave", NOW, b"late\n");store.seal(all_hours=True)
        assert gzip.decompress(stream.read()) == line()  # stable inode
        store.mark_uploaded(path, digest)
    stream, new_digest = store.snapshot(path)
    with stream:
        assert new_digest != digest and gzip.decompress(stream.read()) == line() + b"late\n"


def test_retention_removes_expired_pending_and_crash_temp_files(tmp_path):
    store = spool.HourlyGzStore(tmp_path)
    store.append("enclave", NOW, line())
    part, = list(tmp_path.rglob("*.part"))
    part.with_suffix(".gz.tmp").write_bytes(b"crash")
    assert store.prune(NOW + timedelta(days=31)) == 2
    assert list(tmp_path.rglob("*.part")) == []


def test_automatic_follow_reconnects_after_timeout(tmp_path):
    class Client:
        calls = 0
        def resolve(self, _): return CID
        def lines(self, cid, since, stop):
            self.calls += 1
            if self.calls == 1: raise TimeoutError("private")
            yield line()
            stop.set()
    client = Client()
    worker = collector(tmp_path, client)
    worker.follow("enclave")
    worker.store.seal(all_hours=True)
    assert client.calls == 2 and contents(worker.store) == line()
    assert worker.counters.values["docker_errors"] == 1


def test_upload_outage_does_not_block_collection_rotation_or_status(tmp_path, capsys):
    entered, release = threading.Event(), threading.Event()
    class Client:
        def put_object(self, **kwargs):
            entered.set()
            assert release.wait(3)
            raise TimeoutError("secret credentials")
    worker = collector(tmp_path, ship.DockerLogsClient("http://proxy"), bucket="logs")
    worker.uploader = ship.R2Uploader("logs", "test", "cvm", lambda: Client(), lambda: True, wait=lambda _: False)
    worker.store.append("enclave", NOW - timedelta(hours=1), line())
    thread = threading.Thread(target=worker.upload_loop)
    thread.start()
    try:
        assert entered.wait(1)
        worker.store.append("enclave-domain", NOW - timedelta(hours=1), line())
        worker.maintenance()
        assert len(worker.store.archives()) == 2
        assert json.loads(capsys.readouterr().out)["r2_state"] == "configured"
    finally:
        worker.stop.set()
        release.set()
        thread.join(3)
    assert not thread.is_alive()


def test_shared_r2_client_is_used_with_log_bucket(monkeypatch, tmp_path):
    store = spool.HourlyGzStore(tmp_path)
    store.append("enclave", NOW, line());store.seal(all_hours=True)
    calls = []
    class Client:
        def put_object(self, **kwargs): calls.append(kwargs["Bucket"])
    monkeypatch.setattr(ship.object_storage, "client", lambda: Client())
    monkeypatch.setattr(ship.object_storage, "credentials_present", lambda: True)
    ship.R2Uploader("log-bucket", "test", "cvm").upload(store, ship.Counters())
    assert calls == ["log-bucket"]


def test_import_does_not_load_fastapi_or_create_r2_client():
    import subprocess
    result = subprocess.run([sys.executable, "-c", "import sys; from ops import log_shipper; assert 'fastapi' not in sys.modules; assert 'boto3' not in sys.modules; assert 'enclave.routes' not in sys.modules"],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("restart", [False, True])
def test_same_second_reconnect_retains_each_line_once_and_accepts_new(tmp_path, restart):
    # Adjacent nanoseconds must not collapse through float/microsecond rounding.
    lines = [line(record(pid=pid), timestamp=f"2026-09-17T12:00:00.12345678{pid}Z") for pid in (1, 2, 3, 4)]
    with docker_http([b"".join(frame(x) for x in lines[:3]),
                      b"".join(frame(x) for x in lines),
                      frame(lines[-1])]) as (url, calls):
        client = ship.DockerLogsClient(url)
        worker = collector(tmp_path, client)
        with pytest.raises(EOFError): worker.follow_once("enclave")
        worker.store.seal(all_hours=True)
        if restart:
            worker = collector(tmp_path, client)
        with pytest.raises(EOFError): worker.follow_once("enclave")
        with pytest.raises(EOFError): worker.follow_once("enclave")
        worker.store.seal(all_hours=True)
        assert contents(worker.store) == b"".join(lines)
        queries = [parse_qs(urlsplit(p).query) for p in calls if "/logs" in p]
        assert [q["since"] for q in queries] == [["0"], [f"{int(NOW.timestamp())}.123456783"], [f"{int(NOW.timestamp())}.123456784"]]
        assert worker.counters.values["stored_lines"] == (1 if restart else 4)


def test_identical_nanosecond_records_are_not_lost_at_resume_boundary(tmp_path):
    # Distinct and even byte-identical legitimate events can share a timestamp.
    records = [line(record(pid=1)), line(record(pid=2)), line(record(pid=2))]
    with docker_http([frame(records[0]), b"".join(frame(x) for x in records),
                      b"".join(frame(x) for x in records)]) as (url, _):
        client = ship.DockerLogsClient(url)
        for _ in range(3):
            worker = collector(tmp_path, client)
            with pytest.raises(EOFError): worker.follow_once("enclave")
        worker.store.seal(all_hours=True)
        assert contents(worker.store) == b"".join(records)
        assert worker.store.cursor("enclave", CID) == (int(NOW.timestamp()) * 10**9 + 123456789, 3)


def test_failed_checkpoint_may_replay_but_cannot_skip_line(tmp_path, monkeypatch):
    with docker_http([frame(line()), frame(line())]) as (url, _):
        client = ship.DockerLogsClient(url)
        worker = collector(tmp_path, client)
        def fail(*_): raise OSError("interrupted cursor replacement")
        monkeypatch.setattr(worker.store, "checkpoint", fail)
        with pytest.raises(OSError): worker.follow_once("enclave")
        worker = collector(tmp_path, client)
        with pytest.raises(EOFError): worker.follow_once("enclave")
        worker.store.seal(all_hours=True)
        assert contents(worker.store) == line() * 2  # explicit crash window only
