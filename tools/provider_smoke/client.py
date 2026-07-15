"""Headless E2E client for the hosted (202-async) model_api path on test CVM."""

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from tools.provider_smoke import crypto

_OPENCLAW_ROLES = ("openclaw", "assistant", "agent")


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never replay a credentialed request at a Location supplied by a server."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "redirect refused",
            headers,
            fp,
        )


def _ssl_context() -> ssl.SSLContext:
    # macOS' bundled Python often lacks a system CA bundle, so stdlib urllib hits
    # CERTIFICATE_VERIFY_FAILED against the test CVM. Prefer certifi's bundle when
    # installed; fall back to the platform default otherwise.
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


class SmokeError(Exception):
    def __init__(self, stage: str, detail: str):
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail


@dataclass
class Session:
    user_id: str
    api_key: str
    sk: bytes
    pk: bytes


def is_hosted_response(body: dict) -> bool:
    runtime = body.get("runtime") or {}
    return (
        body.get("status") == "processing"
        and runtime.get("engine") == "feedling_agent_runtime"
    )


def identity_init_body() -> dict:
    """Plaintext identity card for /v1/identity/init (path B: server builds the
    envelope). A fresh hosted account is gated at stage `needs_identity` until a
    card exists; writing one advances it to `main_loop`. days_with_user=0 is
    accepted for a fresh account (no memories => no earliest-memory mismatch);
    relationship_anchor_evidence must be >=8 chars."""
    return {
        "identity": {
            "agent_name": "Smoke",
            "self_introduction": "Provider smoke-test identity card (automated).",
            "dimensions": [],
        },
        "days_with_user": 0,
        "relationship_anchor_evidence": "provider-smoke automated fresh-start test account",
    }


def newest_openclaw_after(messages: list, after_ts: float) -> dict | None:
    """Legacy timestamp-only lookup.

    This helper is intentionally not used by the qualification polling path.
    Timestamp ordering cannot prove that a reply belongs to a particular user
    turn when unrelated traffic is present.
    """
    cands = [
        m
        for m in messages
        if str(m.get("role") or "") in _OPENCLAW_ROLES
        and float(m.get("ts", 0)) > after_ts
        and (m.get("body_ct") or "")
    ]
    if not cands:
        return None
    return max(cands, key=lambda m: float(m.get("ts", 0)))


def _message_ts(message: dict) -> float:
    try:
        return float(message.get("ts", 0))
    except (TypeError, ValueError) as exc:
        raise SmokeError(
            "reply-correlation",
            f"message {message.get('id') or '<missing-id>'} has invalid ts",
        ) from exc


def correlated_openclaw_reply(
    messages: list,
    user_message_id: str,
    *,
    user_message_ts: float | None = None,
) -> dict | None:
    """Return only the assistant row linked to one exact user turn.

    Current history records the authoritative link on the user row as
    ``reply_message_id``.  ``reply_to_message_id`` on assistant rows is also
    accepted for forward/backward compatibility.  Any duplicate, unrelated, or
    out-of-order traffic makes qualification evidence ambiguous and therefore
    fails closed instead of selecting whichever reply has the newest timestamp.
    """
    turn_id = str(user_message_id or "").strip()
    if not turn_id:
        raise SmokeError("reply-correlation", "user message id is required")
    if not isinstance(messages, list) or any(not isinstance(m, dict) for m in messages):
        raise SmokeError(
            "reply-correlation", "history messages must be a list of objects"
        )

    seen_ids: set[str] = set()
    previous_ts: float | None = None
    for message in messages:
        message_id = str(message.get("id") or "").strip()
        if not message_id:
            raise SmokeError(
                "reply-correlation", "history contains a message without id"
            )
        if message_id in seen_ids:
            raise SmokeError(
                "reply-correlation", f"duplicate history message id={message_id}"
            )
        seen_ids.add(message_id)
        current_ts = _message_ts(message)
        if previous_ts is not None and current_ts < previous_ts:
            raise SmokeError(
                "reply-correlation", "history records are out of timestamp order"
            )
        previous_ts = current_ts

    turn_rows = [m for m in messages if str(m.get("id") or "") == turn_id]
    if not turn_rows:
        # A different worker can briefly serve a stale transcript. Poll again;
        # never guess which assistant row belongs to the absent user turn.
        return None
    if len(turn_rows) != 1:
        raise SmokeError("reply-correlation", f"duplicate user turn id={turn_id}")
    turn = turn_rows[0]
    if str(turn.get("role") or "") != "user":
        raise SmokeError(
            "reply-correlation", f"turn id={turn_id} is not a user message"
        )
    turn_ts = _message_ts(turn)
    if user_message_ts is not None and abs(turn_ts - float(user_message_ts)) > 0.001:
        raise SmokeError(
            "reply-correlation",
            f"turn id={turn_id} timestamp mismatch expected={float(user_message_ts):.6f} got={turn_ts:.6f}",
        )

    assistants = [
        m
        for m in messages
        if str(m.get("role") or "") in _OPENCLAW_ROLES and (m.get("body_ct") or "")
    ]
    linked_id = str(turn.get("reply_message_id") or "").strip()
    linked = [
        m for m in assistants if linked_id and str(m.get("id") or "") == linked_id
    ]
    direct = [
        m
        for m in assistants
        if str(
            m.get("reply_to_message_id")
            or m.get("reply_to_id")
            or m.get("in_reply_to")
            or ""
        ).strip()
        == turn_id
    ]

    candidates_by_id = {str(m.get("id")): m for m in [*linked, *direct]}
    if len(candidates_by_id) > 1:
        ids = ",".join(sorted(candidates_by_id))
        raise SmokeError(
            "reply-correlation", f"duplicate replies for turn id={turn_id}: {ids}"
        )
    if (
        linked_id
        and direct
        and any(str(m.get("id") or "") != linked_id for m in direct)
    ):
        raise SmokeError(
            "reply-correlation",
            f"conflicting reply links for turn id={turn_id}",
        )

    later_users = [
        m
        for m in messages
        if str(m.get("role") or "") == "user"
        and str(m.get("id") or "") != turn_id
        and _message_ts(m) > turn_ts
    ]
    assistants_after = [m for m in assistants if _message_ts(m) > turn_ts]
    candidate = next(iter(candidates_by_id.values()), None)

    if candidate is None:
        if later_users:
            raise SmokeError(
                "reply-correlation",
                f"later user turn appeared before reply to turn id={turn_id}",
            )
        if assistants_after:
            ids = ",".join(str(m.get("id") or "") for m in assistants_after)
            raise SmokeError(
                "reply-correlation",
                f"unrelated assistant reply after turn id={turn_id}: {ids}",
            )
        return None

    candidate_id = str(candidate.get("id") or "")
    if _message_ts(candidate) <= turn_ts:
        raise SmokeError(
            "reply-correlation",
            f"reply id={candidate_id} is not after turn id={turn_id}",
        )
    if later_users:
        raise SmokeError(
            "reply-correlation",
            f"later user turn appeared before correlated reply id={candidate_id}",
        )
    unrelated = [m for m in assistants_after if str(m.get("id") or "") != candidate_id]
    if unrelated:
        ids = ",".join(str(m.get("id") or "") for m in unrelated)
        raise SmokeError(
            "reply-correlation",
            f"unrelated assistant replies beside id={candidate_id}: {ids}",
        )
    return candidate


def _thinking_envelope(message: dict) -> dict | None:
    """Rebuild the optional encrypted thinking sub-envelope from chat-history fields.

    Older records may omit a dedicated thinking id/owner/version; clients use the
    enclosing message values as the AAD fallback in that case.  No thinking fields
    means there is simply no disclosure.  A partial envelope is evidence corruption
    and must not be silently treated as "reasoning absent" by qualification tests.
    """
    fields = ("thinking_body_ct", "thinking_nonce", "thinking_K_user")
    present = [bool(message.get(field)) for field in fields]
    if not any(present):
        return None
    if not all(present):
        missing = [field for field in fields if not message.get(field)]
        raise SmokeError(
            "thinking-decrypt", f"incomplete thinking envelope: {','.join(missing)}"
        )
    return {
        "v": message.get("thinking_v") or message.get("v", 1),
        "id": message.get("thinking_id") or message.get("id"),
        "owner_user_id": (
            message.get("thinking_owner_user_id") or message.get("owner_user_id")
        ),
        "body_ct": message["thinking_body_ct"],
        "nonce": message["thinking_nonce"],
        "K_user": message["thinking_K_user"],
    }


def decrypt_reply_record(
    message: dict,
    sk_raw: bytes,
    pk_raw: bytes,
    *,
    include_thinking: bool = True,
) -> dict:
    """Return raw chat evidence plus decrypted user-visible reply/reasoning.

    ``include_thinking=False`` supports text-only compatibility callers even if
    a deployment emits malformed optional thinking metadata. Qualification
    callers evaluating reasoning should use the default strict mode.
    """
    result = {
        "message": dict(message),
        "reply": crypto.decrypt_reply(message, sk_raw, pk_raw),
        "thinking": None,
        "thinking_present": False,
        "thinking_kind": str(message.get("thinking_kind") or ""),
        "thinking_source": str(message.get("thinking_source") or ""),
        "thinking_model": str(message.get("thinking_model") or ""),
        "thinking_native": message.get("thinking_native"),
    }
    if not include_thinking:
        return result
    envelope = _thinking_envelope(message)
    if envelope is None:
        return result
    try:
        result["thinking"] = crypto.decrypt_reply(envelope, sk_raw, pk_raw)
    except Exception as exc:
        raise SmokeError(
            "thinking-decrypt",
            f"unable to decrypt thinking envelope: {type(exc).__name__}",
        ) from exc
    result["thinking_present"] = True
    return result


class SmokeClient:
    def __init__(self, base_url: str):
        parsed = urllib.parse.urlsplit(str(base_url or ""))
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError(
                "base_url must be an http(s) origin without credentials, path, query, or fragment"
            )
        self.base_url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, "", "", "")
        )
        self._origin = (parsed.scheme.lower(), parsed.hostname.lower(), parsed.port)
        self._ssl = _ssl_context()
        self._opener = urllib.request.build_opener(
            _RejectRedirectHandler(),
            urllib.request.HTTPSHandler(context=self._ssl),
        )
        self._sent_turns: dict[tuple[str, float], str] = {}

    # Connection-level failures worth retrying on a flaky CVM gateway. NOT
    # HTTPError (a real HTTP response, must surface as-is). socket timeouts
    # str() to "" so they must be caught explicitly and given a message.
    _RETRYABLE = (
        urllib.error.URLError,
        TimeoutError,
        ssl.SSLError,
        ConnectionError,
        OSError,
    )

    def _url(self, path: str) -> str:
        raw_path = str(path or "")
        parsed = urllib.parse.urlsplit(raw_path)
        if (
            not raw_path.startswith("/")
            or raw_path.startswith("//")
            or parsed.scheme
            or parsed.netloc
            or "\\" in raw_path
        ):
            raise SmokeError(
                "request", "path must be relative to the configured Feedling origin"
            )
        url = f"{self.base_url}{raw_path}"
        target = urllib.parse.urlsplit(url)
        if (
            target.scheme.lower(),
            (target.hostname or "").lower(),
            target.port,
        ) != self._origin:
            raise SmokeError(
                "request", "request target escaped the configured Feedling origin"
            )
        return url

    def _req(
        self,
        method: str,
        path: str,
        *,
        api_key: str | None = None,
        body: dict | None = None,
        attempts: int = 5,
        read_timeout: float = 45,
    ):
        url = self._url(path)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        last_err: Exception | None = None
        for attempt in range(attempts):
            try:
                with self._opener.open(req, timeout=read_timeout) as r:
                    return r.status, json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                if 300 <= e.code < 400:
                    raise SmokeError(
                        "redirect",
                        f"{method} {path} refused status={e.code}",
                    ) from e
                try:
                    payload = json.loads(e.read() or b"{}")
                except Exception:
                    payload = {}
                return e.code, payload
            except self._RETRYABLE as e:
                last_err = e
                if attempt + 1 < attempts:
                    time.sleep(2.0 * (attempt + 1))
        detail = str(last_err) or repr(last_err)  # socket timeout str() is ""
        raise SmokeError(
            "network", f"{method} {path} failed after {attempts} tries: {detail}"
        )

    def register(self, label: str) -> Session:
        sk, pk = crypto.generate_keypair()
        status, body = self._req(
            "POST",
            "/v1/users/register",
            body={
                "public_key": crypto.b64(pk),
                "access_mode": "model_api",
                "label": label,
            },
        )
        if status not in (200, 201) or not body.get("api_key"):
            raise SmokeError("register", f"status={status} body={body}")
        return Session(user_id=body["user_id"], api_key=body["api_key"], sk=sk, pk=pk)

    def setup(
        self,
        sess: Session,
        provider: str,
        model: str,
        base_url: str,
        api_key: str,
        *,
        reasoning_effort: str | None = None,
    ) -> dict:
        status, body = self.setup_raw(
            sess,
            provider,
            model,
            base_url,
            api_key,
            reasoning_effort=reasoning_effort,
        )
        if status != 200 or body.get("status") != "configured":
            raise SmokeError(
                "setup",
                f"status={status} detail={body.get('detail') or body.get('error') or body}",
            )
        return body.get("config") or {}

    def setup_raw(
        self,
        sess: Session,
        provider: str,
        model: str,
        base_url: str,
        api_key: str,
        *,
        reasoning_effort: str | None = None,
    ) -> tuple[int, dict]:
        """Submit provider setup without converting expected 4xx responses to exceptions.

        The release suite uses this for invalid-key assertions and then calls the
        strict ``setup`` method with the real credential to prove recovery.
        """
        payload = {"provider": provider, "model": model, "api_key": api_key}
        if base_url:
            payload["base_url"] = base_url
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
        return self._req(
            "POST", "/v1/model_api/setup", api_key=sess.api_key, body=payload
        )

    def init_identity(self, sess: Session) -> dict:
        """Write the bootstrap identity card so the account leaves stage
        `needs_identity`. Idempotent: a 409 `already_initialized` is treated as
        success. Other failures (e.g. enclave unreachable -> envelope build 409)
        surface under stage `identity`."""
        status, resp = self._req(
            "POST", "/v1/identity/init", api_key=sess.api_key, body=identity_init_body()
        )
        if status == 201 or (
            status == 409 and resp.get("error") == "already_initialized"
        ):
            return resp
        raise SmokeError(
            "identity", f"status={status} detail={resp.get('error') or resp}"
        )

    def enable_hosting(self, sess: Session) -> str:
        status, body = self._req(
            "POST", "/v1/model_api/driver", api_key=sess.api_key, body={"enabled": True}
        )
        if status != 200:
            raise SmokeError("setup", f"enable-hosting status={status} body={body}")
        return str(body.get("driver") or "")

    def open_chat_gate(
        self, sess: Session, *, timeout_sec: int = 50, tries: int = 3
    ) -> dict:
        """Open the `needs_live_connection` bootstrap gate by running verify_loop
        until it reports passing=true. The synthetic ping proves the consumer can
        complete one reply; the gate then stays open. This MUST happen before the
        first real send — the gate 409s any reply to a message sent while closed,
        and that message is not retried. Raises stage `verify` if it never passes."""
        last = {}
        for _ in range(tries):
            _, last = self._req(
                "POST",
                "/v1/chat/verify_loop",
                api_key=sess.api_key,
                body={"timeout_sec": timeout_sec},
                read_timeout=timeout_sec + 45,
            )
            if last.get("passing"):
                return last
        raise SmokeError("verify", f"verify_loop never passed: {last}")

    def send(
        self, sess: Session, message: str, *, read_timeout: float = 45
    ) -> dict:
        status, body = self._req(
            "POST",
            "/v1/model_api/chat/send",
            api_key=sess.api_key,
            body={"message": message},
            # This mutation has no idempotency key. If the server accepts the
            # turn but its response is lost, replaying the POST creates a second
            # user turn and can make qualification evaluate the wrong reply.
            attempts=1,
            read_timeout=max(0.1, min(float(read_timeout), 45.0)),
        )
        if status != 202 or not is_hosted_response(body):
            raise SmokeError(
                "not-hosted", f"expected 202 hosted, got status={status} body={body}"
            )
        user_message = body.get("user_message") or {}
        user_message_id = str(user_message.get("id") or "").strip()
        try:
            user_message_ts = float(user_message["ts"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SmokeError(
                "not-hosted", "hosted response missing user_message.ts"
            ) from exc
        if not user_message_id:
            raise SmokeError("not-hosted", "hosted response missing user_message.id")
        self._sent_turns[(sess.user_id, user_message_ts)] = user_message_id
        return body

    def poll_reply(
        self,
        sess: Session,
        after_ts: float,
        timeout: float,
        interval: float = 3.0,
        *,
        user_message_id: str | None = None,
    ) -> str | None:
        record = self.poll_reply_record(
            sess,
            after_ts,
            timeout,
            interval=interval,
            include_thinking=False,
            user_message_id=user_message_id,
        )
        return None if record is None else str(record["reply"])

    def poll_reply_record(
        self,
        sess: Session,
        after_ts: float,
        timeout: float,
        interval: float = 3.0,
        *,
        include_thinking: bool = True,
        user_message_id: str | None = None,
    ) -> dict | None:
        """Poll for a reply and retain the complete message/metadata evidence.

        Qualification evidence always uses exact, fail-closed turn correlation.
        The turn id must either be passed explicitly or come from this client's
        own ``send`` call.  Timestamp-only lookup is available solely through
        the explicitly named ``poll_reply_record_legacy`` method.
        """
        turn_id = str(user_message_id or "").strip() or self._sent_turns.get(
            (sess.user_id, float(after_ts)),
            "",
        )
        if not turn_id:
            raise SmokeError(
                "reply-correlation",
                "exact user_message_id is required; timestamp-only lookup is not qualification evidence",
            )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            since = max(0.0, float(after_ts) - 0.001)
            query = urllib.parse.urlencode({"since": f"{since:.6f}", "limit": 200})
            status, body = self._req(
                "GET",
                f"/v1/chat/history?{query}",
                api_key=sess.api_key,
                attempts=1,
                read_timeout=max(0.1, min(remaining, 10.0)),
            )
            if status != 200 or not isinstance(body.get("messages"), list):
                raise SmokeError("history", f"status={status} body={body}")
            msg = correlated_openclaw_reply(
                body["messages"],
                turn_id,
                user_message_ts=float(after_ts),
            )
            if msg:
                return decrypt_reply_record(
                    msg,
                    sess.sk,
                    sess.pk,
                    include_thinking=include_thinking,
                )
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(interval, remaining))
        return None

    def poll_reply_legacy(
        self,
        sess: Session,
        after_ts: float,
        timeout: float,
        interval: float = 3.0,
    ) -> str | None:
        """Legacy timestamp-only polling; never valid as qualification evidence."""
        record = self.poll_reply_record_legacy(
            sess,
            after_ts,
            timeout,
            interval=interval,
            include_thinking=False,
        )
        return None if record is None else str(record["reply"])

    def poll_reply_record_legacy(
        self,
        sess: Session,
        after_ts: float,
        timeout: float,
        interval: float = 3.0,
        *,
        include_thinking: bool = True,
    ) -> dict | None:
        """Preserve historical newest-after-timestamp behavior explicitly.

        This method exists only for non-qualification compatibility callers. It
        cannot establish which user turn caused a reply and must not be used by
        the release QA workflow.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            query = urllib.parse.urlencode(
                {
                    "since": f"{float(after_ts):.6f}",
                    "limit": 200,
                }
            )
            status, body = self._req(
                "GET",
                f"/v1/chat/history?{query}",
                api_key=sess.api_key,
            )
            if status != 200 or not isinstance(body.get("messages"), list):
                raise SmokeError("history", f"status={status} body={body}")
            msg = newest_openclaw_after(body["messages"], after_ts)
            if msg:
                return decrypt_reply_record(
                    msg,
                    sess.sk,
                    sess.pk,
                    include_thinking=include_thinking,
                )
            time.sleep(interval)
        return None

    def enable_trace(self, sess: Session, enabled: bool = True) -> dict:
        status, body = self._req(
            "POST",
            "/v1/debug/trace/enable",
            api_key=sess.api_key,
            body={"enabled": bool(enabled)},
        )
        if status != 200 or body.get("enabled") is not bool(enabled):
            raise SmokeError("trace", f"enable status={status} body={body}")
        return body

    def runtime_status(self, sess: Session) -> dict:
        """Read the authenticated account's effective hosted-runtime profile."""
        status, body = self._req(
            "GET",
            "/v1/model_api/runtime",
            api_key=sess.api_key,
        )
        if status != 200 or not isinstance(body, dict):
            raise SmokeError("runtime", f"status={status} body={body}")
        return body

    def read_trace(
        self, sess: Session, *, limit: int = 200, subsystem: str = ""
    ) -> dict:
        query = urllib.parse.urlencode({"limit": int(limit), "subsystem": subsystem})
        status, body = self._req(
            "GET",
            f"/v1/debug/trace?{query}",
            api_key=sess.api_key,
        )
        if status != 200 or not isinstance(body.get("events"), list):
            raise SmokeError("trace", f"read status={status} body={body}")
        return body

    def clear_trace(self, sess: Session) -> dict:
        status, body = self._req("DELETE", "/v1/debug/trace", api_key=sess.api_key)
        if status != 200 or body.get("status") != "ok":
            raise SmokeError("trace", f"clear status={status} body={body}")
        return body

    def reset_account(self, sess: Session) -> dict:
        """Permanently delete a throwaway account and all per-user test data."""
        status, body = self._req(
            "POST",
            "/v1/account/reset",
            api_key=sess.api_key,
            body={"confirm": "delete-all-data"},
        )
        if status != 200 or body.get("deleted") is not True:
            raise SmokeError("cleanup", f"reset status={status} body={body}")
        return body

    def delete_config(self, sess: Session) -> None:
        try:
            self._req("DELETE", "/v1/model_api/delete", api_key=sess.api_key)
        except Exception:
            pass
