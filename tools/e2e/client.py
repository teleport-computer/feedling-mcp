"""Thin Feedling API client for E2E runs: register / seal / chat / memory / teardown.

Mirrors what the iOS app does on the wire (X25519 keypair, v1 envelopes via
``backend/content_encryption.build_envelope``) so the harness exercises the REAL
user path — no test-only backdoors. Hard-refuses production hosts: the harness
creates and deletes throwaway accounts, which must never happen on prod.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO / "backend"))

import httpx  # noqa: E402
from nacl.public import PrivateKey  # noqa: E402  (PyNaCl — backend dep)

from content_encryption import build_envelope  # noqa: E402

# Target is env-overridable so the same harness can qualify the pre Runtime-V2
# deployment; default stays the test env. Prod is refused outright:
# provisioning/teardown on api.feedling.app would create (then hard-delete)
# accounts in the real user registry — api.feedling.app is NOT in the allowlist.
TEST_API = os.environ.get("FEEDLING_E2E_API", "https://test-api.feedling.app")
# The custom-domain entrypoint, NOT the gateway `-5003s.` passthrough. Both are
# documented in deploy/docker-compose.phala.test.yaml, but the passthrough host
# is app_id-derived and therefore dies on every redeploy to a new CVM: the
# hardcoded one here stopped answering (TLS handshake timeout) while
# test-enclave.feedling.app returned 200 (verified 2026-08-08). Anything that
# needs a decrypt source — notably a locally run resident consumer — silently
# fails to start on the stale host, so keep the stable domain as the default.
TEST_ENCLAVE = os.environ.get(
    "FEEDLING_E2E_ENCLAVE",
    "https://test-enclave.feedling.app",
)
# Test-family hosts only. pre-api is the Runtime V2 qualification target
# (2026-07-21); prod stays out → any provisioning against it is refused.
# 本机目标(serve_dev + dev enclave)用于**合并前**在分支上真跑一遍 —— 2026-08-04
# 这样抓出了四个单测发现不了的问题。回环地址不可能是 prod,放行是安全的。
_ALLOWED_HOSTS = ("test-api.feedling.app", "pre-api.feedling.app",
                  "127.0.0.1", "localhost")
# Credentials of not-yet-torn-down accounts (leak manifest; see provision()).
_ORPHANS_DIR = Path.home() / ".feedling-e2e-orphans"
_FAILURES_DIR = Path.home() / ".feedling-e2e-failures"
_ADMIN_TOKEN_FILE = Path.home() / ".feedling" / "data-track-admin-token"
FAILURE_RETENTION_DAYS = 7


def _write_private_json(path: Path, body: dict) -> None:
    """Write diagnostic data 0600 from birth; its parent is private too."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(body, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _write_orphan_manifest(api_url: str, user_id: str, api_key: str) -> Path:
    """Atomically create the leak-manifest file with 0o600 FROM BIRTH
    (O_CREAT|O_EXCL open — never a 0644 window) and fsync it durable."""
    _ORPHANS_DIR.mkdir(parents=True, exist_ok=True)
    path = _ORPHANS_DIR / f"{user_id}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"api_url": api_url, "user_id": user_id,
                                "api_key": api_key}))
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def _refuse_prod(api_url: str) -> None:
    """Require the explicit test API host before any destructive E2E action."""
    from urllib.parse import urlparse
    host = (urlparse(api_url).hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise RuntimeError(f"E2E only permits the test API host: {api_url}")


class E2EClient:
    """One throwaway account. Use as a context manager so teardown is never skipped:

        with E2EClient.provision(route="model_api") as c:
            ...
    """

    def __init__(self, api_url: str, user_id: str, api_key: str, sk: PrivateKey,
                 enclave_pk: bytes, content_encryption_effective: str = "on"):
        _refuse_prod(api_url)
        self.api_url = api_url.rstrip("/")
        self.user_id = user_id
        self.api_key = api_key
        self._sk = sk
        self._enclave_pk = enclave_pk
        self.content_encryption_effective = (
            "off" if content_encryption_effective == "off" else "on"
        )
        self._http = httpx.Client(timeout=60, verify=False)
        self._deleted = False
        self._orphan_file: Path | None = None
        self._preserve_reason = ""
        self._failure_cell = "unknown"
        self._failure_artifacts: dict[str, str] = {}
        self._failure_locators: dict[str, set[str]] = {"job_id": set(), "trace_id": set()}
        self.failure_evidence_path: Path | None = None

    # -- lifecycle ----------------------------------------------------------
    @classmethod
    def provision(cls, *, route: str, api_url: str = TEST_API,
                  archive_language: str = "zh-Hans") -> "E2EClient":
        _refuse_prod(api_url)
        sk = PrivateKey.generate()
        pk_b64 = base64.b64encode(bytes(sk.public_key)).decode()
        api_key = ""
        manifest: Path | None = None
        with httpx.Client(timeout=30, verify=False) as boot:
            try:
                # register is NOT idempotent (each call mints a new account), so we
                # retry ONLY on ConnectError — a TLS/connect failure means the request
                # never reached the server (no account created), safe to retry. A
                # post-send ReadError could have created an account, so it is NOT
                # retried here. The test env flaps on single-CVM deploy windows.
                reg_body = {"public_key": pk_b64, "archive_language": archive_language,
                            "access_mode": route, "label": "e2e-p0"}
                for _attempt in range(3):
                    try:
                        r = boot.post(f"{api_url}/v1/users/register", json=reg_body)
                        break
                    except httpx.ConnectError:
                        if _attempt == 2:
                            raise
                        time.sleep(3 * (_attempt + 1))
                r.raise_for_status()
                body = r.json()
                user_id, api_key = body["user_id"], body["api_key"]
                # Leak manifest, written the INSTANT the account exists — before
                # whoami or anything else can fail — so no crash window can
                # orphan an undeletable account (the 2026-07-18 shakedown lost
                # usr_89f65673 exactly that way). 0o600 from birth; removed on
                # successful teardown; `p0.py --cleanup-orphans` sweeps the
                # rest. If the manifest itself cannot be written, the except
                # arm best-effort resets rather than proceeding untracked.
                manifest = _write_orphan_manifest(api_url, user_id, api_key)
                who = boot.get(f"{api_url}/v1/users/whoami",
                               headers={"X-API-Key": api_key})
                who.raise_for_status()
                who_body = who.json()
                enclave_pk = bytes.fromhex(
                    who_body.get("enclave_content_public_key_hex") or "")
                if len(enclave_pk) != 32:
                    raise RuntimeError("whoami returned an invalid enclave content public key")
                content_encryption_effective = str(
                    who_body.get("content_encryption_effective") or "on"
                ).strip().lower()
            except Exception:
                if api_key:
                    try:
                        cleanup = boot.post(
                            f"{api_url}/v1/account/reset",
                            headers={"X-API-Key": api_key},
                            json={"confirm": "delete-all-data"},
                        )
                        cleanup.raise_for_status()
                        if manifest is not None:
                            manifest.unlink(missing_ok=True)
                    except Exception as cleanup_error:  # noqa: BLE001 — manifest
                        # stays behind as the recoverable evidence trail
                        print(f"[e2e] WARNING provision cleanup failed: {cleanup_error}",
                              file=sys.stderr)
                raise
        client = cls(
            api_url,
            user_id,
            api_key,
            sk,
            enclave_pk,
            content_encryption_effective=content_encryption_effective,
        )
        client._orphan_file = manifest
        return client

    def teardown(self) -> None:
        """Hard-delete the account (test-account-hygiene: create → use → delete).
        Rides the transport-retry wrapper, so a flapping connection gets three
        chances before we surface a leak."""
        if self._deleted:
            return
        r = self.post("/v1/account/reset", json={"confirm": "delete-all-data"})
        r.raise_for_status()
        self._deleted = True
        if self._orphan_file is not None:
            self._orphan_file.unlink(missing_ok=True)

    def configure_failure_evidence(self, *, cell: str,
                                   artifacts: dict[str, str] | None = None) -> None:
        """Attach non-secret P0 context before a step can fail."""
        self._failure_cell = cell
        self._failure_artifacts = dict(artifacts or {})

    def preserve_failure(self, reason: str) -> None:
        """Mark this account as failed: __exit__ captures evidence, not teardown."""
        if not self._preserve_reason:
            self._preserve_reason = reason or "P0 step failed"

    def record_failure_locator(self, kind: str, value: object) -> None:
        """Remember the exact turn/job before later diagnostics can race it."""
        if kind in self._failure_locators and value:
            self._failure_locators[kind].add(str(value))

    @staticmethod
    def _collect_locators(value, found: dict[str, set[str]]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ("job_id", "activity_job_id") and child:
                    found["job_id"].add(str(child))
                if key in ("trace_id", "activity_turn_id", "reply_to_message_id") and child:
                    found["trace_id"].add(str(child))
                E2EClient._collect_locators(child, found)
        elif isinstance(value, list):
            for child in value:
                E2EClient._collect_locators(child, found)

    def _capture_failure_evidence(self) -> Path:
        """Capture redacted diagnostics while the failed account still exists.

        Exact provider input stays in the server-side encrypted Runtime V2
        trajectory; this bundle records the job/trace locators needed for the
        audited inspector instead of creating a second plaintext copy.
        """
        run_dir = _FAILURES_DIR / self.user_id
        run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        os.chmod(run_dir, 0o700)
        captures: dict[str, object] = {}
        errors: dict[str, str] = {}

        try:
            r = self.get("/v1/debug/trace", params={"limit": 200})
            r.raise_for_status()
            captures["user_trace"] = r.json()
            _write_private_json(run_dir / "user-trace.json", r.json())
        except Exception as e:  # noqa: BLE001 — preserving the account is primary
            errors["user_trace"] = f"{type(e).__name__}: {e}"

        # Chat history exposes fixed, content-free turn/job linkage even when
        # the optional debug trace ring is disabled. Keep only those locators;
        # never duplicate ciphertext or user-visible content into this index.
        try:
            r = self.get("/v1/chat/history", params={"limit": 50})
            r.raise_for_status()
            turn_index = []
            for msg in r.json().get("messages") or []:
                if not isinstance(msg, dict):
                    continue
                turn_index.append({
                    key: msg.get(key) for key in (
                        "id", "role", "ts", "reply_to_message_id", "activity_turn_id",
                        "activity_job_id", "reply_status",
                    ) if msg.get(key) is not None
                })
            captures["turn_index"] = turn_index
            _write_private_json(run_dir / "turn-index.json", {"messages": turn_index})
        except Exception as e:  # noqa: BLE001
            errors["turn_index"] = f"{type(e).__name__}: {e}"

        try:
            token = _ADMIN_TOKEN_FILE.read_text().strip()
            if not token:
                raise RuntimeError("admin token file is empty")
            r = httpx.get(
                f"{self.api_url}/v1/admin/data-track/users/{self.user_id}",
                headers={"X-Admin-Token": token}, timeout=60, verify=False,
            )
            r.raise_for_status()
            captures["admin_user"] = r.json()
            _write_private_json(run_dir / "admin-user.json", r.json())
        except Exception as e:  # noqa: BLE001 — user trace/orphan still useful
            errors["admin_user"] = f"{type(e).__name__}: {e}"

        locators: dict[str, set[str]] = {"job_id": set(), "trace_id": set()}
        self._collect_locators(captures, locators)
        for key, values in self._failure_locators.items():
            locators[key].update(values)
        turn_activity: dict[str, object] = {}
        for trace_id in sorted(locators["trace_id"]):
            try:
                r = self.get(f"/v1/chat/turn-activity/{trace_id}")
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                turn_activity[trace_id] = r.json()
            except Exception as e:  # noqa: BLE001
                errors[f"turn_activity:{trace_id}"] = f"{type(e).__name__}: {e}"
        if turn_activity:
            captures["turn_activity"] = turn_activity
            self._collect_locators(turn_activity, locators)
            _write_private_json(run_dir / "turn-activity.json", turn_activity)
        created = datetime.now(timezone.utc)
        manifest = {
            "schema": 1,
            "created_at": created.isoformat(),
            "retention_days": FAILURE_RETENTION_DAYS,
            "cleanup_owner": "P0 operator",
            "cleanup_command": "python3 tools/e2e/p0.py --cleanup-expired-failures",
            "api_url": self.api_url,
            "cell": self._failure_cell,
            "user_id": self.user_id,
            "reason": self._preserve_reason,
            "orphan_manifest": str(self._orphan_file or ""),
            "locators": {key: sorted(values) for key, values in locators.items()},
            "final_provider_input": {
                "storage": "server-side encrypted Runtime V2 trajectory",
                "inspection": "audited break-glass trajectory inspector by job_id/trace_id",
                "plaintext_copied_here": False,
            },
            "artifacts": self._failure_artifacts,
            "capture_errors": errors,
        }
        _write_private_json(run_dir / "evidence.json", manifest)
        return run_dir

    def __enter__(self) -> "E2EClient":
        return self

    def __exit__(self, exc_type, exc, _tb) -> bool:
        if exc_type is not None and not self._preserve_reason:
            self.preserve_failure(f"{exc_type.__name__}: {exc}")
        if self._preserve_reason:
            try:
                self.failure_evidence_path = self._capture_failure_evidence()
                print(
                    f"[e2e] FAILURE SITE PRESERVED: user_id={self.user_id} "
                    f"evidence={self.failure_evidence_path} "
                    "cleanup='python3 tools/e2e/p0.py --cleanup-expired-failures'",
                    file=sys.stderr,
                )
            except Exception as capture_error:  # noqa: BLE001
                print(
                    f"[e2e] FAILURE SITE PRESERVED: user_id={self.user_id} "
                    f"orphan={self._orphan_file}; evidence capture failed: {capture_error}",
                    file=sys.stderr,
                )
            finally:
                self._http.close()
            return False
        try:
            self.teardown()
        except Exception as teardown_error:  # noqa: BLE001
            if exc_type is None:
                raise
            print(f"[e2e] WARNING teardown failed for {self.user_id}: {teardown_error} "
                  f"— delete manually via /v1/account/reset", file=sys.stderr)
        finally:
            self._http.close()
        return False

    # -- plumbing -----------------------------------------------------------
    @property
    def _headers(self) -> dict:
        return {"X-API-Key": self.api_key}

    def _seal(self, plaintext: str) -> dict:
        return build_envelope(
            plaintext=plaintext.encode("utf-8"),
            owner_user_id=self.user_id,
            user_pk_bytes=bytes(self._sk.public_key),
            enclave_pk_bytes=self._enclave_pk,
            visibility="shared",
        )

    def open_envelope(self, env: dict) -> str:
        """Decrypt a v1 envelope with OUR user private key (what the app does).
        Lets continuity checks assert on actual reply text, not just arrival."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import (
            X25519PrivateKey,
            X25519PublicKey,
        )
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
        from cryptography.hazmat.primitives.hashes import SHA256
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        sealed_key = base64.b64decode(env["K_user"])
        if len(sealed_key) != 80:
            raise ValueError(f"K_user must be 80 bytes, got {len(sealed_key)}")
        ephemeral_pk, wrapped_key = sealed_key[:32], sealed_key[32:]
        recipient_sk = X25519PrivateKey.from_private_bytes(bytes(self._sk))
        recipient_pk = recipient_sk.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        shared = recipient_sk.exchange(X25519PublicKey.from_public_bytes(ephemeral_pk))
        wrap_key = HKDF(
            algorithm=SHA256(), length=32, salt=None, info=b"feedling-box-seal-v1",
        ).derive(shared)
        wrap_nonce = hashlib.sha256(ephemeral_pk + recipient_pk).digest()[:12]
        K = ChaCha20Poly1305(wrap_key).decrypt(wrap_nonce, wrapped_key, None)
        aad = f"{env['owner_user_id']}|{env.get('v', 1)}|{env['id']}".encode()
        return ChaCha20Poly1305(K).decrypt(
            base64.b64decode(env["nonce"]), base64.b64decode(env["body_ct"]), aad,
        ).decode("utf-8", errors="replace")

    def message_text(self, msg: dict) -> str:
        """Best-effort plaintext of a history row: plaintext content if the
        server returned it, else local decrypt of the row's envelope fields."""
        content = str(msg.get("content") or "")
        if content:
            return content
        body = msg.get("body")
        if isinstance(body, str):
            return body
        if msg.get("body_ct") and msg.get("K_user"):
            try:
                return self.open_envelope(msg)
            except Exception:  # noqa: BLE001 — diagnostic helper, never fatal
                return ""
        env = msg.get("envelope")
        if isinstance(env, dict):
            try:
                return self.open_envelope(env)
            except Exception:  # noqa: BLE001
                return ""
        return ""

    def decrypt_reply(self, msg: dict) -> str:
        """STRICT decryption-continuity check — what the P0 hard-blocker asserts.

        Unlike ``message_text`` (best-effort, swallows failures, accepts server
        plaintext), this method proves the USER can actually read the reply with
        THEIR private key: it decrypts the row's sealed envelope and never falls
        back to a server-provided ``content`` shortcut. Raises on a missing
        envelope or a decrypt failure — that IS the usr_f13f922a bug (AI keeps
        sending, user's screen is garbage). Returns the plaintext on success.
        """
        env = msg if (msg.get("body_ct") and msg.get("K_user")) else msg.get("envelope")
        if not isinstance(env, dict) or not (env.get("body_ct") and env.get("K_user")):
            # A short text reply must carry its sealed envelope inline. If the body
            # was offloaded/omitted, the P0 fact message was too large — shrink it,
            # don't silently pass. Either way, no envelope = cannot prove readability.
            raise RuntimeError("reply carries no inline sealed envelope to decrypt")
        return self.open_envelope(env)

    def read_reply_strict(self, msg: dict) -> str:
        """Prove a reply matches and is readable under the effective tier.

        Encrypted accounts must pass the existing user-private-key decrypt
        proof.  Plaintext accounts must carry the canonical ``body`` shape and
        no residual crypto fields; accepting a generic ``content`` shortcut
        here would hide a mixed-shape write regression.
        """
        if self.content_encryption_effective != "off":
            return self.decrypt_reply(msg)
        body = msg.get("body")
        if not isinstance(body, str):
            raise RuntimeError("plaintext-tier reply carries no body")
        if any(
            msg.get(key) is not None
            for key in ("body_ct", "nonce", "K_user", "K_enclave")
        ):
            raise RuntimeError("plaintext-tier reply retains crypto fields")
        return body

    def get(self, path: str, **kw) -> httpx.Response:
        return self._request("GET", path, **kw)

    def post(self, path: str, **kw) -> httpx.Response:
        return self._request("POST", path, **kw)

    def delete(self, path: str, **kw) -> httpx.Response:
        # 有真实的 DELETE 路由（`/v1/worldbook/delete?id=…`），探针不该为此去戳
        # 私有的 `_request`。重试语义与 GET/POST 同源：按 id 删除是幂等的。
        return self._request("DELETE", path, **kw)

    def _request(self, method: str, path: str, *, _retries: int = 3, **kw) -> httpx.Response:
        """Transport-retry wrapper. This dev/test environment demonstrably flaps
        (proxy resets, single-CVM deploy windows — the 2026-07-18 shakedown died
        mid-cell to exactly such a window; genesis_e2e._http carries an 8-retry
        history for the same reason), and one dropped connection must not fail a
        whole P0 cell.

        Retrying POSTs is safe for every path this client uses: both send paths
        carry a client_msg_id minted ONCE per logical send and reused across
        retries (/v1/chat/message and /v1/model_api/chat/send dedup on it);
        setup and reset are idempotent; verify_loop is a repeatable probe (each
        call may post a fresh hidden ping — harmless, GC'd server-side).
        (register does NOT ride this wrapper — provision uses its own client.)"""
        last: Exception | None = None
        for attempt in range(_retries):
            try:
                return self._http.request(
                    method, f"{self.api_url}{path}", headers=self._headers, **kw)
            except httpx.TransportError as e:
                last = e
                if attempt + 1 < _retries:      # no pointless backoff after the final try
                    time.sleep(3 * (attempt + 1))
        assert last is not None
        raise last

    # -- chat ---------------------------------------------------------------
    def send_chat(self, text: str) -> float:
        """Send one sealed user message; return its server timestamp.
        client_msg_id is minted once here and rides every transport retry, so a
        retried POST can never double-ingest (memory ring / wake side effects
        included — envelope-id dedup alone only covers the DB row)."""
        sent_at = time.time()
        envelope = (
            {
                "body": text,
                "owner_user_id": self.user_id,
                "visibility": "shared",
            }
            if self.content_encryption_effective == "off"
            else self._seal(text)
        )
        self.record_failure_locator("trace_id", envelope.get("id"))
        r = self.post("/v1/chat/message", json={
            "envelope": envelope,
            "client_msg_id": str(uuid.uuid4()),
        })
        r.raise_for_status()
        return float(r.json().get("ts") or sent_at)

    def wait_reply(self, since: float, *, timeout: float = 180.0) -> dict | None:
        """Poll history for an agent-role message newer than ``since``.
        Returns the message row (metadata; content may be sealed) or None."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.get("/v1/chat/history",
                         params={"since": since - 1, "limit": 50})
            r.raise_for_status()
            h = r.json()
            for m in h.get("messages") or []:
                if (m.get("role") in ("agent", "openclaw")
                        and float(m.get("ts") or 0) > since):
                    self.record_failure_locator("job_id", m.get("activity_job_id"))
                    self.record_failure_locator(
                        "trace_id", m.get("activity_turn_id") or m.get("reply_to_message_id"),
                    )
                    return m
            time.sleep(3)
        return None

    def system_bubbles_since(self, since: float) -> list[dict]:
        """Error notices (role=system) newer than ``since`` — P0's sanity check."""
        r = self.get("/v1/chat/history",
                     params={"since": since - 1, "limit": 100})
        r.raise_for_status()
        h = r.json()
        return [m for m in h.get("messages") or []
                if m.get("role") == "system" and float(m.get("ts") or 0) > since]

    # -- memory / distill ---------------------------------------------------
    def memory_summaries(self, *, limit: int = 50) -> list[str]:
        r = self.post("/v1/memory/index", json={"limit": limit})
        r.raise_for_status()
        return [str(it.get("summary") or "")
                for it in r.json().get("items") or [] if isinstance(it, dict)]

    def upload_distill_material(self, document: str, *, mode: str = "add_memory",
                                material_kind: str = "memory_summary") -> str:
        """Resident-lane sealed upload (format=sealed_v1). Returns job_id."""
        r = self.post("/v1/genesis/imports/plaintext", json={
            "format": "sealed_v1",
            "envelope": self._seal(document),
            "mode": mode,
            "material_kind": material_kind,
        })
        r.raise_for_status()
        return r.json()["job"]["job_id"]
