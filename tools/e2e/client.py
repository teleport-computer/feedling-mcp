"""Thin Feedling API client for E2E runs: register / seal / chat / memory / teardown.

Mirrors what the iOS app does on the wire (X25519 keypair, v1 envelopes via
``backend/content_encryption.build_envelope``) so the harness exercises the REAL
user path — no test-only backdoors. Hard-refuses production hosts: the harness
creates and deletes throwaway accounts, which must never happen on prod.
"""
from __future__ import annotations

import base64
import hashlib
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO / "backend"))

import httpx  # noqa: E402
from nacl.public import PrivateKey  # noqa: E402  (PyNaCl — backend dep)

from content_encryption import build_envelope  # noqa: E402

# The only environment this harness may touch. Prod is refused outright:
# provisioning/teardown on api.feedling.app would create (then hard-delete)
# accounts in the real user registry.
TEST_API = "https://test-api.feedling.app"
TEST_ENCLAVE = (
    "https://173c7f49aeb54acb424676b17b17f78e5e2b2938-5003s.dstack-pha-prod9.phala.network"
)
_ALLOWED_HOSTS = ("test-api.feedling.app",)


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
                 enclave_pk: bytes):
        _refuse_prod(api_url)
        self.api_url = api_url.rstrip("/")
        self.user_id = user_id
        self.api_key = api_key
        self._sk = sk
        self._enclave_pk = enclave_pk
        self._http = httpx.Client(timeout=60, verify=False)
        self._deleted = False

    # -- lifecycle ----------------------------------------------------------
    @classmethod
    def provision(cls, *, route: str, api_url: str = TEST_API,
                  archive_language: str = "zh-Hans") -> "E2EClient":
        _refuse_prod(api_url)
        sk = PrivateKey.generate()
        pk_b64 = base64.b64encode(bytes(sk.public_key)).decode()
        api_key = ""
        with httpx.Client(timeout=30, verify=False) as boot:
            try:
                r = boot.post(f"{api_url}/v1/users/register", json={
                    "public_key": pk_b64,
                    "archive_language": archive_language,
                    "access_mode": route,
                    "label": "e2e-p0",
                })
                r.raise_for_status()
                body = r.json()
                user_id, api_key = body["user_id"], body["api_key"]
                who = boot.get(f"{api_url}/v1/users/whoami",
                               headers={"X-API-Key": api_key})
                who.raise_for_status()
                enclave_pk = bytes.fromhex(
                    who.json().get("enclave_content_public_key_hex") or "")
                if len(enclave_pk) != 32:
                    raise RuntimeError("whoami returned an invalid enclave content public key")
            except Exception:
                if api_key:
                    try:
                        cleanup = boot.post(
                            f"{api_url}/v1/account/reset",
                            headers={"X-API-Key": api_key},
                            json={"confirm": "delete-all-data"},
                        )
                        cleanup.raise_for_status()
                    except Exception as cleanup_error:  # noqa: BLE001
                        print(f"[e2e] WARNING provision cleanup failed: {cleanup_error}",
                              file=sys.stderr)
                raise
        return cls(api_url, user_id, api_key, sk, enclave_pk)

    def teardown(self) -> None:
        """Hard-delete the account (test-account-hygiene: create → use → delete)."""
        if self._deleted:
            return
        r = self._http.post(f"{self.api_url}/v1/account/reset",
                            headers=self._headers,
                            json={"confirm": "delete-all-data"})
        r.raise_for_status()
        self._deleted = True

    def __enter__(self) -> "E2EClient":
        return self

    def __exit__(self, exc_type, _exc, _tb) -> bool:
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

    def get(self, path: str, **kw) -> httpx.Response:
        return self._http.get(f"{self.api_url}{path}", headers=self._headers, **kw)

    def post(self, path: str, **kw) -> httpx.Response:
        return self._http.post(f"{self.api_url}{path}", headers=self._headers, **kw)

    # -- chat ---------------------------------------------------------------
    def send_chat(self, text: str) -> float:
        """Send one sealed user message; return its server timestamp."""
        sent_at = time.time()
        r = self.post("/v1/chat/message", json={"envelope": self._seal(text)})
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
