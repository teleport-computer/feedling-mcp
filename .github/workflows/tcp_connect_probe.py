"""T547: independent direct-connect observations; stdlib plus curl >= 7.70.

Probe failure is time_connect == 0 OR curl exit != 0. It is not a claim that
TCP itself failed: DNS, TLS and post-connect errors remain separate evidence.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import time

TARGETS = (
    "https://api.feedling.app/healthz",
    "https://test-api.feedling.app/healthz",
    "https://test-enclave.feedling.app/healthz",
    "https://cloudflare.com/",
)
SAMPLES = 10
RED_THRESHOLD = 2
CONNECT_TIMEOUT = 5
MAX_TIME = 8
TIMINGS = ("time_namelookup", "time_connect", "time_appconnect", "time_total")


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def curl_command(url):
    # -q must be first: a runner/local .curlrc must not add proxies or retries.
    # Separate processes prevent connection reuse; HTTP/1.1 ensures TCP (not QUIC).
    return ["curl", "-q", "--silent", "--show-error", "--noproxy", "*",
            "--proxy", "", "--http1.1", "--retry", "0", "--connect-timeout", str(CONNECT_TIMEOUT),
            "--max-time", str(MAX_TIME), "--output", os.devnull, "--write-out", "%{json}", url]


def sample(url, number):
    row = {"url": url, "sample": number, "started_at_utc": utc_now(),
           "curl_exit": None, "http_code": None, **dict.fromkeys(TIMINGS),
           "remote_ip": None, "remote_port": None, "curl_stderr": "",
           "measurement_error": None, "probe_failed": None,
           "tcp_not_established": None, "failure_stage": None, "error_kind": None}
    try:
        result = subprocess.run(curl_command(url), capture_output=True, text=True,
                                timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        row["measurement_error"] = type(exc).__name__
        row["finished_at_utc"] = utc_now()
        return row
    row["curl_exit"] = result.returncode
    row["curl_stderr"] = result.stderr[:2000]
    try:
        raw = json.loads(result.stdout)
        for key in TIMINGS:
            value = raw[key]
            if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
                raise ValueError(f"invalid {key}")
        code = raw["http_code"]
        if type(code) is not int or (code != 0 and not 100 <= code <= 599):
            raise ValueError("invalid http_code")
        if result.returncode < 0:
            raise ValueError("curl terminated by signal")
        row.update({key: raw[key] for key in TIMINGS})
        row.update(http_code=f"{code:03d}", remote_ip=raw.get("remote_ip"),
                   remote_port=raw.get("remote_port"))
        row["tcp_not_established"] = row["time_connect"] == 0
        row["probe_failed"] = row["tcp_not_established"] or result.returncode != 0
        row["error_kind"] = {0: "none", 6: "dns_error", 7: "connect_error",
                             28: "timeout", 35: "tls_error", 60: "tls_error"}.get(
                                 result.returncode, "other_curl_error")
        if result.returncode == 6:
            row["failure_stage"] = "dns"
        elif row["tcp_not_established"]:
            row["failure_stage"] = "before_tcp_complete"
        elif result.returncode != 0:
            row["failure_stage"] = "after_tcp_complete"
        else:
            row["failure_stage"] = "none"
    except (ValueError, KeyError, TypeError) as exc:
        row["measurement_error"] = f"invalid curl metrics: {exc}"
        row["raw_stdout"] = result.stdout[:4000]
    row["finished_at_utc"] = utc_now()
    return row


def aggregate(rows):
    summary = []
    for url in TARGETS:
        group = [r for r in rows if r["url"] == url]
        measured = [r for r in group if r["measurement_error"] is None]
        failures = sum(r["probe_failed"] for r in measured)
        unmeasured = len(group) - len(measured)
        incomplete = len(group) != SAMPLES
        status = ("RED" if failures >= RED_THRESHOLD else
                  "UNMEASURED" if unmeasured or incomplete else "BELOW_THRESHOLD")
        summary.append({"url": url, "attempted": len(group), "measured": len(measured),
                        "unmeasured": unmeasured, "incomplete": incomplete,
                        "probe_failures": failures,
                        "failure_rate": failures / len(measured) if measured else None,
                        "tcp_not_established": sum(r["tcp_not_established"] for r in measured),
                        "dns_errors": sum(r["failure_stage"] == "dns" for r in measured),
                        "connect_errors": sum(r["error_kind"] == "connect_error" for r in measured),
                        "timeouts": sum(r["error_kind"] == "timeout" for r in measured),
                        "post_tcp_errors": sum(r["failure_stage"] == "after_tcp_complete" for r in measured),
                        "http_codes": dict(collections.Counter(r["http_code"] for r in measured)),
                        "status": status})
    return summary


def render_summary(metadata, summary, rows):
    lines = ["# TCP connect observations", "",
             f"UTC: {metadata['started_at_utc']} → {metadata['finished_at_utc']}",
             f"Run: {metadata['run_id']} / attempt {metadata['run_attempt']}; "
             f"event: {metadata['event_name']}; observer: {metadata['observer']}", "",
             "Failure = time_connect == 0 OR curl_exit != 0. Red at ≥2/10 per host.",
             "TCP not established includes DNS failures; TLS/HTTP are not TCP attribution.",
             "Missing measurements are unknown, never successful samples. HTTP 4xx/5xx are recorded separately.", "",
             "| Target | Result | Failures / measured | TCP not established | DNS | Connect errors (7) | Timeouts (28) | Post-TCP errors | Unknown | HTTP codes |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    labels = {"RED": "🔴 RED", "UNMEASURED": "🟡 UNMEASURED",
              "BELOW_THRESHOLD": "🟢 BELOW THRESHOLD"}
    for item in summary:
        lines.append(f"| {item['url']} | {labels[item['status']]} | "
                     f"{item['probe_failures']}/{item['measured']} | "
                     f"{item['tcp_not_established']} | {item['dns_errors']} | "
                     f"{item['connect_errors']} | {item['timeouts']} | "
                     f"{item['post_tcp_errors']} | {item['unmeasured']} | "
                     f"{json.dumps(item['http_codes'], sort_keys=True)} |")
    lines += ["", "## Raw per-attempt values", "",
              "| Target | # | UTC start | curl exit | HTTP | time_connect (s) | Error stage | Error kind |",
              "|---|---:|---|---:|---|---:|---|---|"]
    for row in rows:
        stage = "UNMEASURED" if row["measurement_error"] else row["failure_stage"]
        lines.append(f"| {row['url']} | {row['sample']} | {row['started_at_utc']} | "
                     f"{row['curl_exit']} | {row['http_code']} | {row['time_connect']} | {stage} | {row['error_kind']} |")
    lines += ["", "Artifact: samples.jsonl, summary.json, summary.md. Actual runs are the denominator; "
              "the cron schedule does not prove that a sample ran.", ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("tcp-connect-results"))
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    metadata = {"schema_version": 1, "started_at_utc": utc_now(),
                "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
                "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
                "event_name": os.environ.get("GITHUB_EVENT_NAME", "local"),
                "commit": os.environ.get("GITHUB_SHA", "unknown"),
                "observer": "github-actions" if os.environ.get("GITHUB_ACTIONS") == "true" else "local",
                "runner_os": platform.system(), "runner_release": platform.release(), "samples_per_host": SAMPLES,
                "red_threshold": RED_THRESHOLD, "proxy": "disabled",
                "connect_timeout_s": CONNECT_TIMEOUT, "max_time_s": MAX_TIME}
    (args.output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    rows = []
    # Interleave targets so an outage during the batch can be compared in time.
    with (args.output / "samples.jsonl").open("w") as stream:
        for number in range(1, SAMPLES + 1):
            for url in TARGETS:
                row = sample(url, number)
                row.update(run_id=metadata["run_id"], run_attempt=metadata["run_attempt"],
                           observer=metadata["observer"])
                rows.append(row)
                stream.write(json.dumps(row, allow_nan=False) + "\n")
                stream.flush()
            if number < SAMPLES:
                time.sleep(1)
    metadata["finished_at_utc"] = utc_now()
    summary = aggregate(rows)
    (args.output / "summary.json").write_text(
        json.dumps({"metadata": metadata, "hosts": summary}, indent=2, allow_nan=False) + "\n")
    markdown = render_summary(metadata, summary, rows)
    (args.output / "summary.md").write_text(markdown)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as stream:
            stream.write(markdown)
    print(markdown)
    if any(item["unmeasured"] or item["incomplete"] for item in summary):
        return 2
    return 1 if any(item["status"] == "RED" for item in summary) else 0


if __name__ == "__main__":
    raise SystemExit(main())
