"""Whitelist enclave diagnostics → hourly local gzip → optional operator R2.

Only the Docker socket proxy and configured R2 client receive network calls.
Reconnect resumes at the durable nanosecond timestamp and skips acknowledged
boundary records. A crash before cursor persistence can still replay lines.
Docker deletion/rotation before collection cannot be recovered by any tailer.
Unrecognized/oversize lines are deliberately dropped, counted, never echoed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import calendar
import json
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import enclave_reqlog_contract as contract
import object_storage
from ops import log_shipper_store

MAX_LINE = 16384
MAX_FRAME = 1024 * 1024
ID = re.compile(r"[0-9a-f]{64}")
DENY = re.compile(rb'-----BEGIN|"(?:envelope|ciphertext|plaintext_b64)"|[0-9a-fA-F]{64,}')
STAMP = re.compile(rb"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z ")
# Match the ENTIRE gunicorn record, never an exception line containing a phrase.
LIFECYCLE = re.compile(rb"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}\] \[\d{1,10}\] \[(?:INFO|CRITICAL)\] (?:WORKER TIMEOUT \(pid:(\d{1,10})\)|Booting worker with pid: (\d{1,10})|Worker exiting \(pid: (\d{1,10})\))")
STATUS_FIELDS = ("ts", "event", "level", "received_lines", "stored_lines", "dropped_lines", "docker_errors", "storage_errors", "upload_errors", "uploaded_files", "pruned_files", "r2_state")


def safe_line(body):
    if len(body) > MAX_LINE or DENY.search(body):
        return None
    if contract.valid_request_line(body):
        return body  # Byte-for-byte request JSON, after value validation.
    match = LIFECYCLE.fullmatch(body)
    if match:
        for index, event in enumerate(("worker_timeout", "worker_boot", "worker_exit"), 1):
            if match[index] is not None:
                pid = int(match[index])
                if 0 < pid < 2**31:
                    return json.dumps({"event": event, "pid": pid}, separators=(",", ":")).encode()
    return None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DockerLogsClient:
    def __init__(self, base_url, timeout=10, cycle_seconds=30, opener=None, monotonic=time.monotonic):
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("invalid_docker_proxy_url")
        if not 0 < timeout <= 30 or not 0 < cycle_seconds <= 60:
            raise ValueError("invalid_docker_timeout")
        self.base_url = base_url.rstrip("/")
        self.timeout, self.cycle_seconds, self.monotonic = timeout, cycle_seconds, monotonic
        self.open = opener or build_opener(ProxyHandler({}), NoRedirect()).open

    def resolve(self, name):
        with self.open(Request(self.base_url + "/containers/json?all=0"), timeout=self.timeout) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("docker_listing_too_large")
        rows = json.loads(raw)
        if not isinstance(rows, list):
            raise ValueError("invalid_docker_listing")
        matches = [r["Id"] for r in rows if isinstance(r, dict) and isinstance(r.get("Names"), list)
                   and "/" + name in r["Names"] and isinstance(r.get("Id"), str) and ID.fullmatch(r["Id"])]
        if len(matches) != 1:
            raise ValueError("container_not_unique_or_absent")
        return matches[0]

    def lines(self, container_id, since, stop):
        if not ID.fullmatch(container_id) or type(since) is not int or since < 0:
            raise ValueError("invalid_docker_log_request")
        seconds, nanos = divmod(since, 1_000_000_000)
        since_param = f"{seconds}.{nanos:09d}" if since else "0"
        query = urlencode({"stdout": 1, "stderr": 1, "follow": 1, "timestamps": 1, "since": since_param})
        request = Request(f"{self.base_url}/containers/{container_id}/logs?{query}")
        deadline = self.monotonic() + self.cycle_seconds
        with self.open(request, timeout=min(self.timeout, self.cycle_seconds)) as response:
            buffers = {1: bytearray(), 2: bytearray()}
            discarding = {1: False, 2: False}
            def exact(size):
                result = bytearray()
                while len(result) < size:
                    if stop.is_set() or self.monotonic() >= deadline:
                        raise TimeoutError("docker_stream_cycle_end")
                    chunk = response.read(min(size - len(result), 65536))
                    if not chunk:
                        raise EOFError("docker_stream_eof")
                    result.extend(chunk)
                return bytes(result)
            while not stop.is_set() and self.monotonic() < deadline:
                header = exact(8)
                stream = header[0]
                size = int.from_bytes(header[4:], "big")
                if stream not in buffers or header[1:4] != b"\0\0\0" or size > MAX_FRAME:
                    raise ValueError("invalid_docker_frame")
                payload = exact(size)
                # Each stream has its own partial-line state; do not splice stderr
                # into a stdout JSON line. Bound even a newline-free malicious line.
                for piece in payload.splitlines(keepends=True):
                    complete = piece.endswith(b"\n")
                    if not discarding[stream]:
                        if len(buffers[stream]) + len(piece) > MAX_LINE + 64:
                            buffers[stream].clear()
                            discarding[stream] = True
                        else:
                            buffers[stream].extend(piece)
                    if complete:
                        yield None if discarding[stream] else bytes(buffers[stream])
                        buffers[stream].clear()
                        discarding[stream] = False


class R2Uploader:
    def __init__(self, bucket, environment, cvm, client_factory=None,
                 credentials=None, wait=None, attempts=3):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", cvm) or cvm in (".", "..") or environment not in ("prod", "test"):
            raise ValueError("invalid_log_namespace")
        self.bucket, self.environment, self.cvm = bucket.strip(), environment, cvm
        self.client_factory = client_factory or object_storage.client
        self.credentials = credentials or object_storage.credentials_present
        self.wait = wait or (lambda delay: (time.sleep(delay), False)[1])
        self.attempts = attempts

    @property
    def state(self):
        return "R2 未配置" if not self.bucket else "configured" if self.credentials() else "credentials_missing"

    def upload(self, store, counters):
        if self.state != "configured":
            return
        for path in store.archives():
            if self.wait(0):
                return
            snapshot = store.snapshot(path)
            if snapshot is None:
                continue
            stream, digest = snapshot
            with stream:
                key = f"{self.environment}/{self.cvm}/{path.relative_to(store.root).as_posix()}"
                for attempt in range(self.attempts):
                    try:
                        stream.seek(0)
                        self.client_factory().put_object(Bucket=self.bucket, Key=key, Body=stream,
                                                         ContentType="application/gzip")
                        store.mark_uploaded(path, digest)
                        counters.add("uploaded_files")
                        break
                    except Exception:
                        counters.add("upload_errors")
                        if attempt + 1 < self.attempts and self.wait(min(2**attempt, 30)):
                            return
                else:
                    # Stop this sweep on exhaustion; an outage must not spend
                    # the full timeout budget on every historical file.
                    return
                # On exhaustion leave file unmarked; next hourly sweep retries.


class Counters:
    def __init__(self):
        self.lock = threading.Lock()
        self.values = {key: 0 for key in STATUS_FIELDS if key not in ("ts", "event", "level", "r2_state")}

    def add(self, key, count=1):
        with self.lock:
            self.values[key] += count

    def emit(self, now, r2_state):
        with self.lock:
            row = {"ts": now.isoformat(), "event": "log_shipper_status", "level": "warning" if r2_state != "configured" else "info", **self.values, "r2_state": r2_state}
            print(json.dumps({key: row[key] for key in STATUS_FIELDS}, ensure_ascii=False, separators=(",", ":")), flush=True)


class LogShipper:
    def __init__(self, client, store, uploader, names, stop=None, clock=log_shipper_store.utcnow):
        if set(names) != log_shipper_store.ALIASES or len(set(names.values())) != 2:
            raise ValueError("invalid_container_targets")
        for alias, name in names.items():
            if not re.fullmatch(r"[A-Za-z0-9_.-]+-" + re.escape(alias) + r"-1", name):
                raise ValueError("invalid_container_name")
        self.client, self.store, self.uploader, self.names = client, store, uploader, names
        self.stop, self.clock = stop or threading.Event(), clock
        self.counters = Counters()
        self.ids = {}
        self._last_sweep = None

    def follow_once(self, alias):
        container_id = self.client.resolve(self.names[alias])
        if self.ids.get(alias) != container_id:
            self.store.seal(alias=alias, all_hours=True)
            self.ids[alias] = container_id
        since, acknowledged = self.store.cursor(alias, container_id)
        boundary_seen = 0
        latest, count = since, acknowledged
        for raw in self.client.lines(container_id, since, self.stop):
            self.counters.add("received_lines")
            match = STAMP.match(raw) if raw else None
            if not match:
                self.counters.add("dropped_lines")
                continue
            try:
                stamp = datetime.strptime(match[1].decode(), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                self.counters.add("dropped_lines")
                continue
            stamp_ns = calendar.timegm(stamp.utctimetuple()) * 1_000_000_000 + int((match[2] or b"0").ljust(9, b"0"))
            # Docker since includes the boundary. Count occurrences rather than
            # dropping <= cursor: separate records may have identical timestamps.
            # Compare against the initial durable cursor, not the moving cursor.
            if stamp_ns < since:
                continue
            if stamp_ns == since:
                boundary_seen += 1
                if boundary_seen <= acknowledged:
                    continue
            cleaned = safe_line(raw[match.end():].removesuffix(b"\n"))
            if cleaned is None:
                self.counters.add("dropped_lines")
            else:
                try:
                    self.store.append(alias, stamp, raw[:match.end()] + cleaned + b"\n")
                except Exception:
                    self.counters.add("storage_errors")
                    raise  # Never advance a cursor past a failed append/fsync.
                self.counters.add("stored_lines")
            if stamp_ns > latest:
                latest, count = stamp_ns, 1
            elif stamp_ns == latest:
                count += 1
            self.store.checkpoint(alias, container_id, latest, count)

    def follow(self, alias):
        while not self.stop.is_set():
            try:
                self.follow_once(alias)
            except Exception:
                self.counters.add("docker_errors")
            self.stop.wait(1)

    def maintenance(self, force=False):
        now = self.clock()
        self.store.seal(now)
        hour = now.strftime("%Y-%m-%dT%H")
        if force or hour != self._last_sweep:
            self.counters.add("pruned_files", self.store.prune(now))
            self.counters.emit(now, self.uploader.state)
            self._last_sweep = hour

    def upload_loop(self):
        last_hour = None
        while not self.stop.is_set():
            hour = self.clock().strftime("%Y-%m-%dT%H")
            if hour != last_hour:
                try:
                    self.store.seal(self.clock())
                    self.uploader.upload(self.store, self.counters)
                except Exception:
                    self.counters.add("upload_errors")
                last_hour = hour
            self.stop.wait(1)

    def run(self):
        self.store.seal(all_hours=True)  # Recover a previous process's .part files.
        threads = [threading.Thread(target=self.follow, args=(alias,), daemon=True) for alias in self.names]
        for thread in threads:
            thread.start()
        uploading = threading.Thread(target=self.upload_loop, daemon=True)
        uploading.start()
        try:
            while not self.stop.is_set():
                try:
                    self.maintenance()
                except Exception:
                    self.counters.add("storage_errors")
                self.stop.wait(1)
        finally:
            self.stop.set()
            for thread in threads:
                thread.join(self.client.timeout + 1)
            self.store.seal(all_hours=True)
            uploading.join(1)
            # Durable volume is the shutdown guarantee; pending R2 resumes on boot.


def main():
    os.umask(0o077)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    names = os.environ.get("LOG_SHIPPER_CONTAINERS", "feedling-enclave-enclave-1,feedling-enclave-enclave-domain-1").split(",")
    if len(names) != 2:
        raise ValueError("invalid_container_targets")
    targets = dict(zip(("enclave", "enclave-domain"), (name.strip() for name in names)))
    client = DockerLogsClient(os.environ.get("CPU_RECORDER_DOCKER_URL", "http://cpu-socket-proxy:2375"))
    store = log_shipper_store.HourlyGzStore(os.environ.get("LOG_SHIPPER_DATA_DIR", "/var/lib/feedling-logs"),
                                         int(os.environ.get("LOG_SHIPPER_RETENTION_DAYS", "30")))
    uploader = R2Uploader(os.environ.get("R2_LOGS_BUCKET", ""), os.environ.get("LOG_SHIPPER_ENV", "prod"),
                          os.environ.get("LOG_SHIPPER_CVM_NAME", "feedling-enclave-v2"), wait=stop.wait)
    LogShipper(client, store, uploader, targets, stop).run()


if __name__ == "__main__":
    main()
