"""One-shot T798 test ingress evidence. No health, SQL, deploy or raw-log output.

The same composition endpoint and log query parameters used by Phala 1.1.19
are read directly so response bodies are bounded before buffering. Historical
coverage and restart counters remain unknown unless actually measured.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

CVM = "5bfa1543-c5b4-42ca-842d-fd88984e5edf"
APP = "173c7f49aeb54acb424676b17b17f78e5e2b2938"
API = "https://cloud-api.phala.com/api/v1"
COMPOSITION = API + "/cvms/" + CVM + "/composition"
CONTAINER = "feedling-test-ingress-1"
IMAGE = "dstacktee/dstack-ingress:2.2@sha256:d05a7b343c37c1cca1bba8dbf7e8f3c6d2118158af2d41c455103796db4f67f0"
BRANCH = "refs/heads/fix/t798-test-ingress-readonly-r2"
SINCE = "2026-10-09T11:16:30Z"
UNTIL = "2026-10-09T11:21:30Z"
INPUT_LIMIT = 128 * 1024  # aggregate composition + logs, including JSON envelopes
OUTPUT_LIMIT = 64 * 1024
LINE_LIMIT = 300
SECONDS = 30
STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z)(?:\s|$)")
CATEGORIES = {
    "tls_handshake_failure_text": "SSL handshake failure",
    "tls_eof_text": "UNEXPECTED_EOF_WHILE_READING",
    "tls_timeout_text": "SSL handshake timeout",
    "tls_certificate_error_text": "certificate verify failed",
}
SHA = re.compile(r"[0-9a-f]{40}")
CID = re.compile(r"[0-9a-f]{64}")


class Refused(Exception):
    """Only fixed local codes may cross the reporting boundary."""


class Deadline(Refused):
    pass


def timestamp(text):
    if not isinstance(text, str) or not STAMP.fullmatch(text):
        raise Refused("invalid_timestamp")
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise Refused("invalid_timestamp") from None


def runtime_gate(env):
    expected = env.get("EXPECTED_COLLECTOR_SHA", "")
    if (env.get("GITHUB_ACTIONS") != "true"
            or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or env.get("GITHUB_REF") != BRANCH
            or env.get("GITHUB_REPOSITORY") != "teleport-computer/feedling-mcp"
            or not SHA.fullmatch(expected)
            or env.get("GITHUB_SHA") != expected):
        raise Refused("execution_identity_refused")
    if not env.get("PHALA_CLOUD_API_KEY"):
        raise Refused("credential_missing")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Refused("redirect_refused")


class Reader:
    """Two independent GETs; strict TLS, no proxies, no redirect or retry.

    Total elapsed deadline uses SIGALRM in the main thread on the Linux runner.
    Errors never serialize a URL, credential, remote body or exception message.
    """
    def __init__(self, key):
        self.key = key
        self.remaining = INPUT_LIMIT
        self.records = []
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            NoRedirect(),
        )

    def get(self, url, *, auth):
        if len(self.records) >= 2:
            raise Refused("request_limit")
        if auth and url != COMPOSITION:
            raise Refused("credential_target_refused")
        rec = {"operation": "composition" if auth else "logs", "bytes": 0,
               "status": None, "outcome": "started",
               "started_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        self.records.append(rec)
        started = time.monotonic()
        def expired(*_):
            raise Deadline("operation_timeout")
        prior = signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, SECONDS)
        try:
            headers = {"Accept": "application/json" if auth else "*/*",
                       "Accept-Encoding": "identity"}
            if auth:
                headers.update({"X-API-Key": self.key, "X-Phala-Version": "2025-10-28"})
            request = urllib.request.Request(url, headers=headers, method="GET")
            with self.opener.open(request, timeout=10) as response:
                rec["status"] = response.status
                if response.status != 200:
                    raise Refused("http_status")
                if response.headers.get("Content-Encoding", "identity") != "identity":
                    raise Refused("encoded_body_refused")
                chunks = []
                while True:
                    # No sentinel read: even an exactly-full response is
                    # conservatively refused when EOF cannot be established
                    # inside the physical aggregate input budget.
                    if self.remaining == 0:
                        raise Refused("input_limit")
                    chunk = response.read(min(4096, self.remaining))
                    rec["bytes"] += len(chunk)
                    if len(chunk) > self.remaining:
                        raise Refused("input_limit")
                    self.remaining -= len(chunk)
                    if not chunk:
                        break
                    chunks.append(chunk)
                rec["outcome"] = "received"
                return b"".join(chunks)
        except urllib.error.HTTPError as exc:
            rec["status"] = exc.code
            rec["outcome"] = "permission_denied" if exc.code in (401, 403) else "http_error"
            exc.close()
            raise Refused(rec["outcome"]) from None
        except Refused as exc:
            rec["outcome"] = exc.args[0]
            raise
        except (TimeoutError, socket.timeout):
            rec["outcome"] = "operation_timeout"
            raise Refused("operation_timeout") from None
        except (urllib.error.URLError, ssl.SSLError, OSError, http.client.HTTPException):
            rec["outcome"] = "transport_failure"
            raise Refused("transport_failure") from None
        finally:
            rec["elapsed_seconds"] = round(time.monotonic() - started, 3)
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, prior)


def selected_container(raw):
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise Refused("composition_json_invalid") from None
    if (not isinstance(data, dict) or data.get("is_online") is not True
            or data.get("error") or not isinstance(data.get("containers"), list)
            or not 1 <= len(data["containers"]) <= 100):
        raise Refused("composition_unavailable")
    found = []
    for row in data["containers"]:
        if not isinstance(row, dict) or not isinstance(row.get("names"), list):
            raise Refused("composition_shape")
        # SDK does not expose Compose labels. Accept only the exact frozen
        # Compose-derived name, never a substring, service fallback or prefix.
        if CONTAINER in row["names"] or "/" + CONTAINER in row["names"]:
            found.append(row)
    if len(found) != 1:
        raise Refused("ingress_identity_ambiguous")
    row = found[0]
    if (not CID.fullmatch(str(row.get("id", "")))
            or row.get("image") != IMAGE or row.get("state") != "running"):
        raise Refused("ingress_identity_refused")
    return row


def logs_url(endpoint):
    if not isinstance(endpoint, str) or len(endpoint) > 4096:
        raise Refused("log_endpoint_refused")
    # urlsplit strips some control bytes and servers differ in normalization.
    # Reject before parsing; never turn an ambiguous input into an allowed URL.
    if any(ord(char) <= 32 or ord(char) >= 127 or char == "\\" for char in endpoint):
        raise Refused("log_endpoint_refused")
    try:
        u = urllib.parse.urlsplit(endpoint)
        # A small ASCII segment grammar excludes literal/encoded/double-encoded
        # dot segments, percent escapes, backslashes and duplicate separators.
        # Unknown real endpoint formats must STOP instead of being normalized.
        if not re.fullmatch(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*/?", u.path):
            raise Refused("log_endpoint_refused")
        allowed = (u.hostname == "cloud-api.phala.com"
                   and u.path.startswith("/api/v1/cvms/" + CVM + "/"))
        # Phala's log endpoint may be routed through the fixed test app.
        allowed = allowed or bool(re.fullmatch(
            APP + r"-[0-9]{1,5}s?\.dstack-pha-prod9\.phala\.network", u.hostname or ""))
        if (u.scheme != "https" or not allowed or u.username or u.password
                or u.fragment or u.port not in (None, 443)):
            raise Refused("log_endpoint_refused")
        query = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
        if any(not re.fullmatch(r"[A-Za-z0-9_-]+", key)
               or any(ord(char) <= 32 or ord(char) >= 127 or char in ("\\", "%") for char in value)
               for key, value in query):
            raise Refused("log_endpoint_refused")
        reserved = {"since", "until", "tail", "lines", "timestamps", "follow", "text", "bare", "ansi"}
        if any(k in reserved for k, _ in query):
            raise Refused("log_endpoint_query_collision")
        query += [("since", SINCE), ("until", UNTIL), ("tail", str(LINE_LIMIT)),
                  ("lines", str(LINE_LIMIT)), ("timestamps", "true")]
        return urllib.parse.urlunsplit(u._replace(query=urllib.parse.urlencode(query)))
    except (ValueError, TypeError):
        raise Refused("log_endpoint_refused") from None


def project_logs(raw):
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError:
        raise Refused("log_encoding_invalid") from None
    # The pinned CLI supports raw timestamped text and NDJSON channel/message
    # envelopes (message can be base64). Unknown shapes are never flattened.
    lines = []
    for line in text.splitlines():
        if not line:
            continue
        if line.startswith("{"):
            try:
                obj = json.loads(line)
                message = obj["message"]
                if obj.get("channel") not in ("stdout", "stderr") or not isinstance(message, str):
                    raise ValueError()
                if re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", message) and len(message) % 4 == 0:
                    message = base64.b64decode(message, validate=True).decode("utf-8")
                lines.extend(message.splitlines())
            except (ValueError, KeyError, TypeError, UnicodeError, binascii.Error):
                raise Refused("log_envelope_invalid") from None
        else:
            lines.append(line)
        if len(lines) > LINE_LIMIT:
            raise Refused("line_limit")
    start, end = timestamp(SINCE), timestamp(UNTIL)
    counts = dict.fromkeys(CATEGORIES, 0)
    events, valid_stamps = [], []
    missing, outside, unknown = 0, 0, 0
    for line in lines:
        match = STAMP.match(line)
        if not match:
            missing += 1
            continue
        when = timestamp(match[1])
        if not start <= when <= end:
            outside += 1
            continue
        valid_stamps.append(when)
        # These are literal text indicators, not attribution of a connection or
        # an authoritative event type. Never copy addresses, bodies or suffixes.
        matched = [code for code, value in CATEGORIES.items() if value in line]
        if not matched:
            unknown += 1
        for code in matched:
            counts[code] += 1
        if matched:
            events.append({"at": when.isoformat(), "indicators": matched})
    coverage = ("EMPTY_UNPROVEN" if not lines else
                "OUT_OF_WINDOW" if outside else
                "TIMESTAMPS_MISSING" if missing else
                "TAIL_SATURATED" if len(lines) >= LINE_LIMIT else
                "PARTIAL_UNPROVEN")
    return {"coverage": coverage, "complete_window": False, "no_errors_proven": False,
            "returned_lines": len(lines), "missing_timestamps": missing,
            "outside_window": outside, "unclassified_lines": unknown,
            "earliest": min(valid_stamps).isoformat() if valid_stamps else None,
            "latest": max(valid_stamps).isoformat() if valid_stamps else None,
            "literal_indicator_counts": counts, "events": events}


def collect(reader):
    row = selected_container(reader.get(COMPOSITION, auth=True))
    url = logs_url(row.get("log_endpoint"))
    counts = {"status": "UNMEASURED", "reason": "composition_schema_has_no_restart_counter"}
    # Do not invent a Docker inspect/SSH path or treat created as StartedAt.
    projected = project_logs(reader.get(url, auth=False))
    return {"result": "PARTIAL_READONLY_EVIDENCE", "container_id_sha256":
            hashlib.sha256(row["id"].encode()).hexdigest(), "image": IMAGE,
            "restart_counts": counts, "logs": projected}


def save_report(out, report):
    encoded = json.dumps(report, ensure_ascii=True, indent=2).encode()
    if len(encoded) > OUTPUT_LIMIT:
        encoded = b'{"result":"STOP","reason":"output_limit","no_errors_proven":false}\n'
    with (out / "report.json").open("xb") as f:
        f.write(encoded)
    return len(encoded)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    if not args.run:
        print("PLAN_ONLY: no files, credentials, network or remote operations")
        return 0
    os.umask(0o077)
    args.out.mkdir(exist_ok=False)
    reader = None
    report = {"result": "STOP", "target_cvm": CVM, "since": SINCE, "until": UNTIL,
              "health_requests": 0, "models": 0, "raw_logs_saved": False,
              "no_errors_proven": False}
    try:
        runtime_gate(os.environ)
        reader = Reader(os.environ["PHALA_CLOUD_API_KEY"])
        report.update(collect(reader))
    except Refused as exc:
        report["reason"] = exc.args[0]
    except Exception:
        # No arbitrary exception text: it may embed a signed URL or secret.
        report["reason"] = "unexpected_local_failure"
    if reader:
        report["requests"] = reader.records
    save_report(args.out, report)
    print(json.dumps({"result": report["result"], "report": "report.json"}))
    # Partial historical evidence is useful but never an acceptance PASS.
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
