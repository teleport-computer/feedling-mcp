"""Headless E2E client for the hosted (202-async) model_api path on test CVM."""
import json
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from tools.provider_smoke import crypto

_OPENCLAW_ROLES = ("openclaw", "assistant", "agent")


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
    return body.get("status") == "processing" and runtime.get("engine") == "feedling_agent_runtime"


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


def _reply_has_content(m: dict) -> bool:
    """An agent reply carries content either as an encrypted envelope
    (``body_ct``, the e2ee/plaintext-off path) or as a plaintext ``body`` (the
    hosted plaintext-tier path that new accounts default to on Runtime V2).
    The smoke used to require ``body_ct``, so it silently skipped every V2
    reply and reported "no reply" though the turn had completed and stored one
    (T542)."""
    return bool(m.get("body_ct") or m.get("body"))


def newest_openclaw_after(messages: list, after_ts: float) -> dict | None:
    cands = [
        m for m in messages
        if str(m.get("role") or "") in _OPENCLAW_ROLES
        and float(m.get("ts", 0)) > after_ts
        and _reply_has_content(m)
    ]
    if not cands:
        return None
    return max(cands, key=lambda m: float(m.get("ts", 0)))


class SmokeClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._ssl = _ssl_context()

    # Connection-level failures worth retrying on a flaky CVM gateway. NOT
    # HTTPError (a real HTTP response, must surface as-is). socket timeouts
    # str() to "" so they must be caught explicitly and given a message.
    _RETRYABLE = (urllib.error.URLError, TimeoutError, ssl.SSLError, ConnectionError, OSError)

    def _req(self, method: str, path: str, *, api_key: str | None = None, body: dict | None = None,
             attempts: int = 5, read_timeout: float = 45):
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        last_err: Exception | None = None
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(req, timeout=read_timeout, context=self._ssl) as r:
                    return r.status, json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                try:
                    payload = json.loads(e.read() or b"{}")
                except Exception:
                    payload = {}
                return e.code, payload
            except self._RETRYABLE as e:
                last_err = e
                time.sleep(2.0 * (attempt + 1))
        detail = str(last_err) or repr(last_err)  # socket timeout str() is ""
        raise SmokeError("network", f"{method} {path} failed after {attempts} tries: {detail}")

    def register(self, label: str) -> Session:
        sk, pk = crypto.generate_keypair()
        status, body = self._req("POST", "/v1/users/register", body={
            "public_key": crypto.b64(pk),
            "access_mode": "model_api",
            "label": label,
        })
        if status not in (200, 201) or not body.get("api_key"):
            raise SmokeError("register", f"status={status} body={body}")
        return Session(user_id=body["user_id"], api_key=body["api_key"], sk=sk, pk=pk)

    def setup(self, sess: Session, provider: str, model: str, base_url: str, api_key: str) -> dict:
        payload = {"provider": provider, "model": model, "api_key": api_key}
        if base_url:
            payload["base_url"] = base_url
        status, body = self._req("POST", "/v1/model_api/setup", api_key=sess.api_key, body=payload)
        if status != 200 or body.get("status") != "configured":
            raise SmokeError("setup", f"status={status} detail={body.get('detail') or body.get('error') or body}")
        return body.get("config") or {}

    def init_identity(self, sess: Session) -> dict:
        """Write the bootstrap identity card so the account leaves stage
        `needs_identity`. Idempotent: a 409 `already_initialized` is treated as
        success. Other failures (e.g. enclave unreachable -> envelope build 409)
        surface under stage `identity`."""
        status, resp = self._req("POST", "/v1/identity/init", api_key=sess.api_key, body=identity_init_body())
        if status == 201 or (status == 409 and resp.get("error") == "already_initialized"):
            return resp
        raise SmokeError("identity", f"status={status} detail={resp.get('error') or resp}")

    def enable_hosting(self, sess: Session) -> str:
        status, body = self._req("POST", "/v1/model_api/driver", api_key=sess.api_key, body={"enabled": True})
        if status != 200:
            raise SmokeError("setup", f"enable-hosting status={status} body={body}")
        return str(body.get("driver") or "")

    def open_chat_gate(self, sess: Session, *, timeout_sec: int = 50, tries: int = 3) -> dict:
        """Open the `needs_live_connection` bootstrap gate by running verify_loop
        until it reports passing=true. The synthetic ping proves the consumer can
        complete one reply; the gate then stays open. This MUST happen before the
        first real send — the gate 409s any reply to a message sent while closed,
        and that message is not retried. Raises stage `verify` if it never passes."""
        last = {}
        for _ in range(tries):
            _, last = self._req("POST", "/v1/chat/verify_loop", api_key=sess.api_key,
                                body={"timeout_sec": timeout_sec}, read_timeout=timeout_sec + 45)
            if last.get("passing"):
                return last
        raise SmokeError("verify", f"verify_loop never passed: {last}")

    def send(self, sess: Session, message: str) -> dict:
        status, body = self._req("POST", "/v1/model_api/chat/send", api_key=sess.api_key, body={"message": message})
        if status != 202 or not is_hosted_response(body):
            raise SmokeError("not-hosted", f"expected 202 hosted, got status={status} body={body}")
        return body

    def _reply_text(self, msg: dict, sess: Session) -> str:
        """A plaintext-tier reply (hosted V2 default) has no envelope — read its
        `body` directly; only decrypt when `body_ct` (e2ee/plaintext-off) is set."""
        if msg.get("body_ct"):
            return crypto.decrypt_reply(msg, sess.sk, sess.pk)
        return str(msg.get("body") or "")

    def reply_for_turn(self, sess: Session, turn_id: str) -> str | None:
        """The agent reply bound to THIS turn by ``reply_to_message_id``, not the
        newest agent row after a timestamp — a proactive/unrelated reply must not
        be attributed to the turn under test."""
        _, body = self._req("GET", f"/v1/chat/history?since=0&limit=50", api_key=sess.api_key)
        for m in body.get("messages") or []:
            if (str(m.get("role") or "") in _OPENCLAW_ROLES
                    and str(m.get("reply_to_message_id") or "") == turn_id
                    and _reply_has_content(m)):
                return self._reply_text(m, sess)
        return None

    def poll_turn(self, sess: Session, turn_id: str, timeout: float,
                  interval: float = 3.0) -> dict:
        """Poll the turn to a verdict bound to ``turn_id`` (the sent message id).

        The backend turn-activity ``failure`` field is authoritative (T532/T540):
        a non-empty failure means the turn failed regardless of complete/phase/job
        status, even if a nonempty fallback reply was delivered. A settled turn
        with no failure resolves to its bound reply. Fail closed: an unreadable or
        never-settling verdict is a failure, not a pass.

        Returns {settled, failed, failure, reply}.
        """
        deadline = time.monotonic() + timeout
        last = "no_response"
        while time.monotonic() < deadline:
            status, act = self._req(
                "GET", f"/v1/chat/turn-activity/{turn_id}", api_key=sess.api_key)
            if status == 200 and isinstance(act, dict):
                failure = act.get("failure")
                if failure:
                    code = str((failure.get("code") if isinstance(failure, dict) else failure) or "turn_failed")
                    return {"settled": True, "failed": True, "failure": code, "reply": None}
                if act.get("complete"):
                    reply = self.reply_for_turn(sess, turn_id)
                    if reply is None:
                        return {"settled": True, "failed": True,
                                "failure": "complete_without_bound_reply", "reply": None}
                    return {"settled": True, "failed": False, "failure": None, "reply": reply}
                last = f"pending(phase={act.get('phase')})"
            elif status == 404:
                last = "turn_not_registered_yet"
            else:
                last = f"http_{status}"
            time.sleep(interval)
        # Fail closed: never treat an unread/unsettled verdict as success.
        return {"settled": False, "failed": True,
                "failure": f"verdict_unsettled_timeout:{last}", "reply": None}

    def delete_config(self, sess: Session) -> None:
        try:
            self._req("DELETE", "/v1/model_api/delete", api_key=sess.api_key)
        except Exception:
            pass
