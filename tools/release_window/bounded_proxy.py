"""One-run, text-only V2 probe relay. No transport retries, no user credentials, no billing inference.

All starts (including setup and compaction) share one immutable budget. The
append-only journal is opened exclusively: restarting cannot reset an old run.
Wire limits measure bytes separately from conservative token/cost reservations. An attempt
reserved before sending may fail before reaching the provider; never call it a
confirmed billed request. Output limits are forwarded to the fixed provider.
"""
import argparse
import hmac
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
import time

import httpx

from tools.release_window.contract import validate_manifest, digest, cost_reservation, cost_ceiling, count_cost_reservation, PHASES, SHARED_ADMISSION
from tools.release_window.receipts import verify_input_count

UPSTREAM = "https://api.openai.com/v1/responses"
COUNT_ENDPOINT = UPSTREAM + "/input_tokens"


class Refused(Exception):
    pass


def payload(raw, limits):
    if len(raw) > limits["request_bytes"]:
        raise Refused("input_bytes")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise Refused("json") from None
    if limits['model'] == 'openai/gpt-4o-mini':
        from .openrouter_bound import payload as or_payload
        return or_payload(value, limits)
    allowed = {"model", "input", "instructions", "tools", "tool_choice",
               "parallel_tool_calls", "max_output_tokens", "store", "reasoning",
               "prompt_cache_key", "stream"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise Refused("shape")
    if value.get("model") != limits["model"] or value.get("stream", False) is not False:
        raise Refused("model_or_stream")
    items = value.get("input")
    if not isinstance(items, list) or not items:
        raise Refused("input")
    for item in items:
        if not isinstance(item, dict):
            raise Refused("input_item")
        kind = item.get("type", "message")
        if kind == "message":
            if set(item) - {"type", "role", "content"}:
                raise Refused("message_shape")
            content = item.get("content")
            if not isinstance(content, list) or any(
                not isinstance(part, dict) or set(part) - {"type", "text"}
                or part.get("type") not in {"input_text", "output_text"}
                or not isinstance(part.get("text"), str) for part in content
            ):
                raise Refused("text_only")
        elif kind == "function_call":
            if set(item) - {"type", "call_id", "name", "arguments", "id", "status"}:
                raise Refused("function_call_shape")
        elif kind == "function_call_output":
            if set(item) - {"type", "call_id", "output"} or not isinstance(item.get("output"), str):
                raise Refused("function_output_shape")
        else:
            raise Refused("text_only")
    tools = value.get("tools", [])
    if not isinstance(tools, list) or any(not isinstance(t, dict) or t.get("type") != "function" for t in tools):
        raise Refused("no_hosted_tools")
    requested = value.get("max_output_tokens")
    if type(requested) is not int or requested <= 0:
        raise Refused("output_limit")
    # This acceptance slice proves text dialogues, not callable-tool execution.
    # Disable model-initiated function calls as well as rejecting hosted tools.
    value.update(tool_choice='none', max_output_tokens=min(requested, limits["output_tokens_per_start"], 1024), store=False, stream=False)
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > limits["request_bytes"]:
        raise Refused("input_bytes")
    return encoded


# Closed diagnostic vocabulary: never persist exception text or caller field names.
PAYLOAD_REFUSAL_REASONS = frozenset({
    'function_call_shape', 'function_output_shape', 'input', 'input_bytes',
    'input_item', 'json', 'message_shape', 'model_or_stream', 'no_hosted_tools',
    'output_limit', 'shape', 'text_only', 'or_input_bytes', 'or_message',
    'or_messages', 'or_no_hosted_tools', 'or_output_limit', 'or_shape',
    'or_stream', 'or_text_only', 'or_tool_history',
})


class Run:
    def __init__(self, *, token, upstream_key, journal, manifest, transport=None, clock=time.monotonic):
        if len(token) < 32 or not upstream_key or token == upstream_key:
            raise ValueError("distinct fixture token and upstream key required")
        self.manifest = validate_manifest(manifest)
        verify_input_count(self.manifest)
        self.limits = self.manifest["limits"] | {"model": self.manifest["model"]}
        self.manifest_digest = digest(self.manifest)
        self.phase = "setup"
        self.phase_starts = {name: 0 for name in PHASES}
        self.shared_cap = self.manifest.get("admission_policy") == SHARED_ADMISSION
        self.token = token
        self.key = upstream_key
        self.clock = clock
        self.deadline = clock() + self.limits["wall_seconds"]
        self.lock = threading.Lock()
        self.attempts = self.input_bytes = 0
        self.count_requests = 0
        self.reserved_input_tokens = self.reserved_nanodollars = 0
        self.billing = self.manifest["billing"]
        self.is_openrouter = self.manifest["lane"] == "openrouter-v2-text"
        self.upstream = "https://openrouter.ai/api/v1/chat/completions" if self.is_openrouter else UPSTREAM
        self.relay_path = "/v1/chat/completions" if self.is_openrouter else "/v1/responses"
        self.stopped = False
        self.failure_reason = None
        self.billing_bound_breached = False
        self.closing = threading.Event()
        self.absolute = None
        self.probe_nonce = secrets.token_hex(32)
        self.probe_ack = secrets.token_hex(32)
        self.probe_received = False
        fd = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.journal = os.fdopen(fd, "w")
        self.client = httpx.Client(transport=transport or httpx.HTTPTransport(retries=0),
                                   follow_redirects=False, trust_env=False)
        if 'execution_window' in self.manifest:
            from .deadlines import Deadline
            self.absolute = Deadline(self.manifest['execution_window']['execution_end_epoch'])
            self.deadline = min(self.deadline, self.clock() + self.absolute.remaining())
            self.absolute.on_expire(self.closing.set)
            self.absolute.bind(self.client)
        self.record({"event": "limits", "manifest_digest": self.manifest_digest,
                     "run_id": self.manifest["run_id"], "limits": self.limits,
                     "admission_policy": self.manifest.get("admission_policy"),
                     "usage": "UNKNOWN"})

    def set_phase(self, phase):
        with self.lock:
            order = list(self.phase_starts)
            if phase not in order or order.index(phase) != order.index(self.phase) + 1 or self.stopped:
                raise Refused("phase_order")
            self.phase = phase
            self.record({"event": "phase", "phase": phase})


    def record(self, row):
        try:
            self.journal.write(json.dumps(row) + "\n")
            self.journal.flush()
            os.fsync(self.journal.fileno())
        except Exception:
            self.stopped = True
            self.failure_reason = self.failure_reason or 'journal_write_failed'
            raise

    def _fail(self, reason):
        """Called under the ledger lock; first paid failure is irreversible."""
        self.stopped = True
        if self.failure_reason is None:
            self.failure_reason = reason
            self.record({'event':'run_failed','reason':reason,'phase':self.phase})

    def reject_authenticated(self):
        with self.lock:
            if not self.closing.is_set():
                self._fail('authenticated_request_refused')

    def auth(self, header):
        return hmac.compare_digest(header or "", "Bearer " + self.token)

    def forward(self, raw):
        # Serialized forwarding makes both the total budget and stop latch
        # atomic, including requests arriving concurrently from background work.
        with self.lock:
            if self.closing.is_set():
                raise Refused('admission_closed')
            try:
                body = payload(raw, self.limits)
            except Refused as exc:
                self._fail('payload_refused')
                # Stop first; a diagnostic write failure cannot reopen admission.
                code = exc.args[0] if len(exc.args) == 1 else None
                reason = code if type(code) is str and code in PAYLOAD_REFUSAL_REASONS else 'unclassified'
                self.record({'event': 'payload_rejected', 'reason': reason, 'phase': self.phase})
                raise
            if self.stopped or self.clock() >= self.deadline:
                self._fail('stopped_or_deadline')
                raise Refused("stopped_or_deadline")
            input_reservation = self.billing['enforced_input_tokens_max']
            money_reservation = cost_reservation(self.billing, self.limits['output_tokens_per_start'])
            if (self.reserved_input_tokens + input_reservation > self.limits['total_input_tokens']
                    or self.reserved_nanodollars + money_reservation > cost_ceiling(self.billing)):
                self._fail('token_or_monetary_budget')
                raise Refused('token_or_monetary_budget')
            if (self.attempts >= self.limits["starts"] or self.input_bytes + len(body) > self.limits["total_bytes"]
                    or (not self.shared_cap and self.phase_starts[self.phase] >= self.limits["phase_starts"][self.phase])
                    or (self.attempts + 1) * self.limits["output_tokens_per_start"] > self.limits["total_output_tokens"]):
                self._fail('total_budget')
                raise Refused("total_budget")
            count = self._count(body)
            if count > input_reservation:
                self._fail('input_count_exceeds_ceiling')
                raise Refused('input_count_exceeds_ceiling')
            if self.closing.is_set() or self.clock() >= self.deadline:
                self._fail('closed_or_deadline_after_count')
                raise Refused('closed_or_deadline_after_count')
            if self.reserved_nanodollars + money_reservation > cost_ceiling(self.billing):
                self._fail('token_or_monetary_budget')
                raise Refused('token_or_monetary_budget')
            self.attempts += 1
            self.phase_starts[self.phase] += 1
            self.input_bytes += len(body)
            self.reserved_input_tokens += input_reservation
            self.reserved_nanodollars += money_reservation
            self.record({"event": "send_reserved", "attempt": self.attempts,
                         "phase": self.phase, "input_bytes": len(body), "total_input_bytes": self.input_bytes,
                         "input_tokens_reserved": input_reservation,
                         "input_tokens_counted": None if self.is_openrouter else count,
                         "input_bound_kind": self.billing["input_bound_kind"],
                         "payload_sha256": hashlib.sha256(body).hexdigest(),
                         "total_input_tokens_reserved": self.reserved_input_tokens,
                         "output_tokens_reserved": self.limits['output_tokens_per_start'],
                         "nanodollars_reserved": money_reservation,
                         "total_nanodollars_reserved": self.reserved_nanodollars,
                         "billing_state": "RESERVED_NOT_SETTLED"})
            try:
                verify_input_count(self.manifest)
            except Exception:
                self._fail('input_count_authorization_changed')
                raise Refused('input_count_authorization_changed') from None
            # Durable logging may block. Recheck immediately before dispatch;
            # an unsent reservation remains consumed, never recycled as zero.
            remaining = self.deadline - self.clock()
            if remaining <= 0 or self.closing.is_set():
                self._fail('deadline_after_reservation')
                self.record({"event": "reserved_not_sent", "attempt": self.attempts,
                             "reason": "deadline", "usage": "UNKNOWN"})
                raise Refused("deadline_after_reservation")
            try:
                with self.client.stream("POST", self.upstream, content=body,
                        headers={"Authorization": "Bearer " + self.key,
                                 "Content-Type": "application/json"},
                        timeout=min(20.0, remaining)) as response:
                    chunks = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 1024 * 1024 or self.clock() >= self.deadline:
                            raise Refused("response_size_or_deadline")
                        chunks.append(chunk)
                    result = b"".join(chunks)
                    provider_failed = False
                    try:
                        value = json.loads(result)
                        if self.is_openrouter and isinstance(value, dict):
                            choices = value.get('choices')
                            provider_failed = value.get('error') is not None or (
                                isinstance(choices, list) and any(
                                    isinstance(choice, dict) and (
                                        choice.get('error') is not None
                                        or choice.get('finish_reason') == 'error')
                                    for choice in choices))
                        usage = value.get("usage") if isinstance(value, dict) else None
                        if self.is_openrouter and isinstance(usage, dict):
                            usage = {"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"), "total_tokens": usage.get("total_tokens")}
                    except (ValueError, UnicodeError):
                        usage = None
                    # Project numbers only, never provider text or unknown fields.
                    measured = {k: usage.get(k) for k in
                                ("input_tokens", "output_tokens", "total_tokens")
                                if isinstance(usage, dict) and type(usage.get(k)) is int
                                and usage[k] >= 0}
                    complete = (len(measured) == 3 and measured["total_tokens"] ==
                                measured["input_tokens"] + measured["output_tokens"])
                    breached = complete and (
                        measured['input_tokens'] > input_reservation
                        or measured['output_tokens'] > self.limits['output_tokens_per_start'])
                    self.billing_bound_breached |= breached
                    self.record({"event": "response", "attempt": self.attempts,
                                 "http": response.status_code,
                                 "usage": measured or "UNKNOWN", "usage_complete": complete,
                                 "billing_bound_breached": breached})
                    # A transport/provider error may invite product retries; stop
                    # the paid boundary on the first error instead.
                    if (response.status_code != 200 or provider_failed or not complete
                            or measured['input_tokens'] > input_reservation
                            or measured['output_tokens'] > self.limits['output_tokens_per_start']):
                        self._fail('provider_error' if response.status_code != 200 or provider_failed else
                                   'usage_unknown' if not complete else 'billing_bound_breached')
                    return response.status_code, result
            except Exception as exc:
                self._fail('upstream_failed_unknown')
                self.record({"event": "send_failed", "attempt": self.attempts,
                             "error_type": type(exc).__name__, "usage": "UNKNOWN",
                             "provider_received": "UNKNOWN"})
                raise Refused("upstream_failed_stopped") from None

    def _count(self, body):
        """Under the forwarding lock: reserve count request before network.

        Send the entire final bytes, never a lossy input-only projection. An
        endpoint rejecting any final field stops the run; no stripped retry.
        """
        try:
            verify_input_count(self.manifest)
            if self.is_openrouter:
                return 128000  # Whole model context reservation, NOT a measured count.
            config = self.manifest['input_count']
            fee = count_cost_reservation(self.manifest)
            if (self.count_requests >= config['max_requests']
                    or self.reserved_nanodollars + fee > cost_ceiling(self.billing)):
                raise Refused('count_budget')
            self.count_requests += 1
            self.reserved_nanodollars += fee
            self.record({'event':'count_reserved', 'count_request':self.count_requests,
                'phase':self.phase, 'payload_sha256':hashlib.sha256(body).hexdigest(),
                'input_bytes':len(body), 'nanodollars_reserved':fee})
            # Durable reservation can block past authorization expiry or source
            # revocation. Counting itself requires current authority at dispatch.
            verify_input_count(self.manifest)
            remaining = self.deadline - self.clock()
            if remaining <= 0 or self.closing.is_set():
                raise Refused('count_deadline_or_closed')
            count_deadline = min(self.deadline, self.clock() + config['timeout_seconds'])
            with self.client.stream('POST', COUNT_ENDPOINT, content=body,
                    headers={'Authorization':'Bearer '+self.key, 'Content-Type':'application/json'},
                    timeout=min(config['timeout_seconds'], remaining)) as response:
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 4096 or self.clock() >= count_deadline:
                        raise Refused('count_size_or_timeout')
                if response.status_code != 200 or self.clock() >= count_deadline:
                    raise Refused('count_http_or_timeout')
                result = json.loads(raw)
                if (not isinstance(result, dict) or result.get('object') != 'response.input_tokens'
                        or type(result.get('input_tokens')) is not int or result['input_tokens'] < 0):
                    raise Refused('count_unknown')
                # Revalidate expiry/source evidence after a potentially slow exchange.
                verify_input_count(self.manifest)
                self.record({'event':'count_measured', 'count_request':self.count_requests,
                    'payload_sha256':hashlib.sha256(body).hexdigest(),
                    'input_tokens':result['input_tokens']})
                return result['input_tokens']
        except Exception:
            self._fail('input_count_failed')
            raise Refused('input_count_failed') from None

    def close(self):
        self.closing.set()  # Close admission before waiting for active forwarding.
        wait = self.limits["drain_seconds"]
        if 'execution_window' in self.manifest:
            wait = max(0, min(wait, self.manifest['execution_window']['cleanup_end_epoch']-time.time()))
        if not self.lock.acquire(timeout=wait):
            raise Refused("drain_unconfirmed")
        try:
            self.stopped = True
            if self.absolute is not None:
                self.absolute.cancel()
            self.client.close()
            if self.journal.closed:
                return  # failed journal cannot supply a cleanup receipt
            # forward holds this same lock across its entire HTTP exchange.
            # This durable receipt is emitted only after local requests drain.
            try:
                self.record({"event": "admission_closed", "local_in_flight": 0,
                             "failure_reason": self.failure_reason,
                             "total_input_tokens_reserved": self.reserved_input_tokens,
                             "total_nanodollars_reserved": self.reserved_nanodollars,
                             "count_reservations": self.count_requests,
                             "send_reservations": self.attempts, "manifest_digest": self.manifest_digest, "usage": "UNKNOWN"})
            finally:
                self.journal.close()
        finally:
            self.lock.release()


def verify_closed_admission(journal):
    rows = [json.loads(line) for line in Path(journal).read_text().splitlines()]
    reserved = [row for row in rows if row.get("event") == "send_reserved"]
    counts = [row for row in rows if row.get('event') == 'count_reserved']
    failures = [row for row in rows if row.get('event') == 'run_failed']
    if not rows or rows[-1].get('count_reservations') != len(counts):
        raise RuntimeError('count reservation ledger mismatch')
    if (not rows or rows[0].get("event") != "limits"
            or rows[-1].get("event") != "admission_closed"
            or type(rows[-1].get("local_in_flight")) is not int
            or rows[-1]["local_in_flight"] != 0
            or type(rows[-1].get("send_reservations")) is not int
            or rows[-1]["send_reservations"] != len(reserved)
            or [r.get("attempt") for r in reserved] != list(range(1, len(reserved) + 1))):
        raise RuntimeError("closed and drained admission evidence required")
    if (len(failures) > 1 or rows[-1].get('failure_reason') !=
            (failures[0].get('reason') if failures else None)):
        raise RuntimeError('paid failure ledger mismatch')
    for total, field in (('total_input_tokens_reserved','input_tokens_reserved'),
                         ('total_nanodollars_reserved','nanodollars_reserved')):
        entries = reserved + (counts if total == 'total_nanodollars_reserved' else [])
        if (type(rows[-1].get(total)) is not int
                or any(type(r.get(field)) is not int or r[field] < 0 for r in entries)
                or rows[-1][total] != sum(r[field] for r in entries)):
            raise RuntimeError('reservation ledger mismatch')
    return rows[-1]


def handler(run):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # no headers, URLs, prompts, or response bodies in logs

        def do_POST(self):
            self.connection.settimeout(5)
            status, result = 400, b'{"error":"refused"}'
            paid = False
            try:
                if not run.auth(self.headers.get("Authorization")):
                    status = 401
                elif self.path != run.relay_path:
                    status = 404
                else:
                    paid = self.headers.get('X-T779-Probe') is None
                    if self.headers.get('Transfer-Encoding'):
                        raise Refused('transfer_encoding')
                    length = int(self.headers.get("Content-Length", "-1"))
                    if length <= 0 or length > run.limits["request_bytes"]:
                        raise Refused("input_length")
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise Refused("input_length")
                    nonce = self.headers.get("X-T779-Probe")
                    if nonce is not None:
                        # Same authenticated path as paid traffic, but a probe
                        # can never fall through to the upstream provider.
                        with run.lock:
                            if (raw != b"{}" or run.stopped or run.clock() >= run.deadline
                                    or not hmac.compare_digest(nonce, run.probe_nonce)):
                                raise Refused("probe")
                            run.probe_received = True
                            status = 200
                            result = json.dumps({"nonce": nonce, "ack": run.probe_ack}).encode()
                    else:
                        status, result = run.forward(raw)
            except (Refused, ValueError, OSError):
                if paid:
                    run.reject_authenticated()
                status = 400
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(result)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(result)
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    validate_manifest(json.loads(args.manifest.read_text()))
    print("PLAN_ONLY: use bounded_v2 for owned execution; no standalone paid relay")


if __name__ == "__main__":
    main()
