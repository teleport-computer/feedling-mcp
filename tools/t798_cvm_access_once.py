"""One authorized metadata GET; no logs, retry, arbitrary URL or raw output."""
import datetime
import json
import os
from pathlib import Path
import re
import signal
import ssl
import subprocess
import urllib.error
import urllib.request

TARGET = "5bfa1543-c5b4-42ca-842d-fd88984e5edf"
URL = "https://cloud-api.phala.com/api/v1/cvms/" + TARGET
CAP = 16384
CODES = frozenset({"invalid_api_key", "unauthorized", "forbidden",
                   "permission_denied", "not_found", "access_denied"})


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def deadline(*_):
    raise TimeoutError("deadline")


def permitted(env, head):
    sha = env.get("EXPECTED_DIAGNOSTIC_SHA", "")
    return (re.fullmatch(r"[0-9a-f]{40}", sha) is not None
            and sha == head == env.get("GITHUB_SHA")
            and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
            and env.get("GITHUB_REPOSITORY") == "teleport-computer/feedling-mcp"
            and env.get("GITHUB_REF") == "refs/heads/fix/t798-cvm-access-once"
            and env.get("GITHUB_RUN_ATTEMPT") == "1")


def summarize(status, body):
    result = {"http_status": status, "bytes": len(body), "id_match": False}
    if len(body) >= CAP:
        return dict(result, outcome="body_cap_stop")
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeError):
        return dict(result, outcome="non_json_stop")
    if not isinstance(doc, dict):
        return dict(result, outcome="unexpected_shape_stop")
    result["id_match"] = any(
        isinstance(doc.get(k), str)
        and doc[k] in (TARGET, TARGET.replace("-", ""))
        for k in ("id", "cvm_id")
    )
    # Exact enumeration only: never echo free-form messages or unknown codes.
    error = doc.get("error")
    codes = [doc.get("code"), error]
    if isinstance(error, dict):
        codes.append(error.get("code"))
    result["provider_code"] = next(
        (c for c in codes if isinstance(c, str) and c in CODES), "unmeasured")
    result["outcome"] = ("target_read_ok" if status == 200 and result["id_match"]
                         else "diagnostic_stop")
    return result


def fetch_once(key):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        NoRedirect())
    request = urllib.request.Request(URL, method="GET", headers={
        "Accept": "application/json", "Accept-Encoding": "identity",
        "X-API-Key": key, "X-Phala-Version": "2025-10-28"})
    try:
        response = opener.open(request, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            return {"http_status": response.code, "outcome": "encoding_stop"}
        return summarize(response.code, response.read(CAP))


def main():
    report = {"target": TARGET, "started_at": datetime.datetime.now(
        datetime.timezone.utc).isoformat(), "requests": 0, "outcome": "guard_stop"}
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        key = os.environ.get("PHALA_CLOUD_API_KEY", "")
        if permitted(os.environ, head) and key and "\r" not in key and "\n" not in key:
            signal.signal(signal.SIGALRM, deadline)
            signal.setitimer(signal.ITIMER_REAL, 30)
            report["requests"] = 1
            report.update(fetch_once(key))
    except Exception as error:
        # Exception text can contain response metadata; retain only fixed categories.
        report["outcome"] = "timeout_stop" if isinstance(error, TimeoutError) else "transport_stop"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        Path("t798-cvm-access.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["outcome"] == "target_read_ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
