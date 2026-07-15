#!/usr/bin/env python3
"""Genesis end-to-end harness: drive one real genesis pass against a deployed env.

Mimics the iOS upload client: creates a genesis import job, seals a test transcript
into v1 chunk envelopes (K_enclave -> enclave content pubkey, visibility=shared, so
the in-CVM worker can decrypt), uploads + finalizes, then polls + verifies the
distilled outputs (genesis_state done, persona blob ENCRYPTED with no plaintext,
Garden facts written) and asserts no raw plaintext leaked.

Run against the TEST CVM (not locally — needs the enclave + a test user + the worker
flag FEEDLING_GENESIS_WORKER_ENABLED=1). Crypto matches content_encryption.build_envelope.

  upload:  python3 tools/genesis_e2e.py upload  --api-url <U> --api-key <K> --user-id <UID> \
                   --transcript transcript.txt [--enclave-pk-hex <hex> | --attestation-url <U>] \
                   [--source-kind history] [--chunk-size 12000]
  verify:  python3 tools/genesis_e2e.py verify  --api-url <U> --api-key <K> --job-id <J>
  qualify an already-provisioned profile (preferred for release qualification):
           python3 tools/genesis_e2e.py distill-existing-session --api-url <U> \
             --session-manifest <private-manifest.json> --profile-id <profile> \
             --fixture qa/fixtures/persona-import-v1.json \
             --private-evidence </private/tmp/evidence.json> --artifact-dir <QA_ARTIFACT_DIR>
           # Codex reads evidence, writes a bounded hash-bound judgment, then:
           python3 tools/genesis_e2e.py distill-existing-session-finalize \
             --fixture qa/fixtures/persona-import-v1.json \
             --private-evidence </private/tmp/evidence.json> \
             --semantic-judgment </private/tmp/judgment.json> \
             --artifact-dir <QA_ARTIFACT_DIR>
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from content_encryption import build_envelope  # noqa: E402

try:
    from nacl.public import PrivateKey  # PyNaCl (a backend dep)
except Exception as e:  # noqa: BLE001
    print(json.dumps({"ok": False, "error": f"PyNaCl required (backend dep): {e}"}))
    sys.exit(2)


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Never forward a Feedling API key to a redirected destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "Feedling API redirect rejected",
            headers,
            fp,
        )


def _multipart_form_body(
    fields: dict[str, str],
    file_upload: dict[str, object],
) -> tuple[bytes, str]:
    """Encode one bounded multipart file without leaking it through temp files."""
    boundary = "----feedling-qa-" + os.urandom(16).hex()
    field_name = str(file_upload.get("field_name") or "")
    filename = str(file_upload.get("filename") or "")
    content_type = str(file_upload.get("content_type") or "")
    content = file_upload.get("content")
    header_values = [field_name, filename, content_type, *map(str, fields.keys())]
    if any("\r" in value or "\n" in value for value in header_values):
        raise ValueError("multipart header injection rejected")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", field_name):
        raise ValueError("multipart field name invalid")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", filename):
        raise ValueError("multipart filename invalid")
    if not re.fullmatch(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+", content_type):
        raise ValueError("multipart content type invalid")
    if not isinstance(content, bytes) or not content:
        raise ValueError("multipart file content invalid")

    chunks: list[bytes] = []
    for key, value in fields.items():
        name = str(key)
        rendered = str(value)
        if "\r" in rendered or "\n" in rendered:
            raise ValueError("multipart field value invalid")
        chunks.extend(
            (
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                    "ascii"
                ),
                rendered.encode("utf-8"),
                b"\r\n",
            )
        )
    chunks.extend(
        (
            f"--{boundary}\r\n".encode("ascii"),
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{filename}"\r\n'
            ).encode("ascii"),
            f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode("ascii"),
        )
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _http(
    method,
    url,
    api_key,
    *,
    json_body=None,
    multipart_fields=None,
    file_upload=None,
    timeout=60,
    retries=8,
):
    """Single request with retry on transient read/connection errors (IncompleteRead,
    URLError, socket timeout). HTTPError (a real status) is returned immediately, never
    retried. Generous retries + backoff because the local proxy (198.18 fake-IP) flaps
    a lot and acceptance runs poll for 10+ min. HTTPError 409 on register (a re-POST
    after a lost response created the account) is treated as transient-but-fatal -> the
    caller re-provisions. Safe: every call site is idempotent (GET) or create-on a
    throwaway test user."""
    import http.client
    import socket

    if json_body is not None and (
        multipart_fields is not None or file_upload is not None
    ):
        raise ValueError("request body modes are mutually exclusive")
    if multipart_fields is not None or file_upload is not None:
        if not isinstance(multipart_fields, dict) or not isinstance(file_upload, dict):
            raise ValueError("multipart request invalid")
        data, request_content_type = _multipart_form_body(
            {str(key): str(value) for key, value in multipart_fields.items()},
            file_upload,
        )
    else:
        data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        request_content_type = "application/json"
    last_exc = None
    opener = urllib.request.build_opener(_RejectRedirects())
    for attempt in range(retries):
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"X-API-Key": api_key, "Content-Type": request_content_type},
        )
        try:
            with opener.open(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            return e.code, {"error": e.read().decode("utf-8", "replace")[:400]}
        except (
            http.client.IncompleteRead,
            urllib.error.URLError,
            socket.timeout,
            ConnectionError,
        ) as e:
            last_exc = e
            time.sleep(min(2.0 * (attempt + 1), 12.0))
    raise SystemExit(f"{method} {url} failed after {retries} attempts: {last_exc}")


def _enclave_pk_bytes(args) -> bytes:
    if args.enclave_pk_hex:
        return bytes.fromhex(args.enclave_pk_hex.strip())
    url = args.attestation_url or f"{args.api_url.rstrip('/')}/attestation"
    with urllib.request.urlopen(url, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    pk_hex = body.get("enclave_content_pk_hex") or body.get("content_pk_hex") or ""
    if not pk_hex:
        raise SystemExit(f"no enclave_content_pk_hex at {url}")
    return bytes.fromhex(pk_hex)


def _chunks(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), max(1, size))] or [""]


def _provision_user(args) -> tuple[str, str, bytes, bytes]:
    """Self-provision a throwaway test user: register a fresh content keypair, set up
    its model_api provider key (validated -> test_status=ok), and read its enclave pk.
    Returns (api_key, user_id, enclave_pk_bytes, user_pk_bytes). Provider key comes
    from the env (GENESIS_E2E_PROVIDER_API_KEY), never persisted to disk."""
    import os

    base = args.api_url.rstrip("/")
    api_key = user_id = ""
    # Content public key must be base64 of the raw 32-byte X25519 pk (core/envelope.py
    # _decode_content_public_key base64-decodes it + asserts 32 bytes); hex would
    # decode to 48 bytes -> user_content_public_key_invalid_length at model_api/setup.
    # A flaky proxy can lose the register RESPONSE after the account was created; the
    # _http retry then re-POSTs the same key -> 409 account_exists. On 409, burn the key
    # and try a fresh one.
    for _attempt in range(5):
        sk = PrivateKey.generate()
        args._user_sk = bytes(sk)  # raw X25519 private key, kept for acceptance decrypt
        user_pk_b64 = base64.b64encode(bytes(sk.public_key)).decode("ascii")
        s, b = _http(
            "POST",
            f"{base}/v1/users/register",
            "",
            json_body={"public_key": user_pk_b64},
        )
        if s == 409:
            continue  # key collided via a retried POST whose first response was lost
        if s >= 400:
            raise SystemExit(f"register failed {s}: {b}")
        api_key = b.get("api_key") or b.get("apiKey") or ""
        user_id = b.get("user_id") or b.get("userId") or ""
        if api_key and user_id:
            break
    if not api_key or not user_id:
        raise SystemExit("register failed after retries (proxy/409)")
    user_pk = bytes(sk.public_key)  # the successful keypair's public key
    if not getattr(args, "skip_setup", False):
        provider_key = os.environ.get("GENESIS_E2E_PROVIDER_API_KEY", "").strip()
        if not provider_key:
            raise SystemExit(
                "GENESIS_E2E_PROVIDER_API_KEY env required (worker LLM key); or use --skip-setup for a plumbing dry-run"
            )
        s, b = _http(
            "POST",
            f"{base}/v1/model_api/setup",
            api_key,
            json_body={
                "provider": args.provider,
                "model": args.model,
                "base_url": args.base_url,
                "api_key": provider_key,
            },
        )
        if s >= 400 or b.get("test_status") not in ("ok", None):
            raise SystemExit(f"model_api/setup failed {s}: {b}")
    s, b = _http("GET", f"{base}/v1/users/whoami", api_key)
    enclave_hex = b.get("enclave_content_public_key_hex") or ""
    # The plaintext / acceptance path does no client-side sealing, so it doesn't need the
    # enclave pk; only chunked `upload` does. Don't hard-fail if whoami omits it (e.g. the
    # enclave is still warming up after a redeploy).
    enclave_pk = bytes.fromhex(enclave_hex) if enclave_hex else b""
    return api_key, user_id, enclave_pk, user_pk


def cmd_upload(args):
    if args.register:
        args.api_key, args.user_id, enclave_pk, user_pk = _provision_user(args)
        print(
            json.dumps(
                {"provisioned": True, "user_id": args.user_id, "api_key": args.api_key},
                ensure_ascii=False,
            )
        )
    else:
        enclave_pk = _enclave_pk_bytes(args)
        user_pk = bytes(
            PrivateKey.generate().public_key
        )  # throwaway: worker decrypts via K_enclave
    parts = _chunks(Path(args.transcript).read_text(encoding="utf-8"), args.chunk_size)

    status, body = _http(
        "POST",
        f"{args.api_url.rstrip('/')}/v1/genesis/imports",
        args.api_key,
        json_body={"source_kind": args.source_kind, "total_chunks": len(parts)},
    )
    if status >= 400:
        print(
            json.dumps({"ok": False, "step": "create", "status": status, "body": body})
        )
        return 1
    job_id = body.get("job_id") or body.get("job", {}).get("job_id")
    if not job_id:
        print(
            json.dumps(
                {"ok": False, "step": "create", "error": "no job_id", "body": body}
            )
        )
        return 1

    for seq, part in enumerate(parts):
        env = build_envelope(
            plaintext=part.encode("utf-8"),
            owner_user_id=args.user_id,
            user_pk_bytes=user_pk,
            enclave_pk_bytes=enclave_pk,
            visibility="shared",
        )
        body_ct = env["body_ct"]
        cct_sha = hashlib.sha256(base64.b64decode(body_ct)).hexdigest()
        s, b = _http(
            "PUT",
            f"{args.api_url.rstrip('/')}/v1/genesis/imports/{job_id}/chunks/{seq}",
            args.api_key,
            json_body={
                "envelope": env,
                "ciphertext_sha256": cct_sha,
                "byte_start": 0,
                "byte_end": len(part.encode("utf-8")),
            },
        )
        if s >= 400:
            print(
                json.dumps(
                    {"ok": False, "step": f"chunk:{seq}", "status": s, "body": b}
                )
            )
            return 1

    s, b = _http(
        "POST",
        f"{args.api_url.rstrip('/')}/v1/genesis/imports/{job_id}/finalize",
        args.api_key,
    )
    print(
        json.dumps(
            {
                "ok": s < 400,
                "step": "finalize",
                "status": s,
                "job_id": job_id,
                "body": b,
            },
            ensure_ascii=False,
        )
    )
    return 0 if s < 400 else 1


def cmd_verify(args):
    url = f"{args.api_url.rstrip('/')}/v1/genesis/imports/{args.job_id}"

    def _state_of(b: dict) -> str:
        # GET /v1/genesis/imports/<id> returns `state` as a dict blob {status,...},
        # not a string. Read state.status first, then fall back to top-level/job.status
        # (older/edge shapes) so verify doesn't false-negative on a dict.
        st = b.get("state")
        if isinstance(st, dict):
            return str(st.get("status") or "").lower()
        return str(
            st or b.get("status") or b.get("job", {}).get("status") or ""
        ).lower()

    deadline = time.time() + args.timeout
    last = {}
    while time.time() < deadline:
        s, b = _http("GET", url, args.api_key)
        last = b
        state = _state_of(b)
        if state in ("done", "failed"):
            break
        time.sleep(args.poll)
    state = _state_of(last)
    out = {"state": state, "job": last}
    # Privacy spot-check. The ACCURATE signal: distinctive transcript fragments
    # (--privacy-needle, e.g. "蛋子,西湖") must NOT appear in the status payload.
    # The old generic-keyword scan false-positived on field names (total_chunks)
    # and on privacy_copy text ("...imported plaintext"), so it's dropped to the
    # two keys that would only show up if real content leaked.
    blob = json.dumps(last, ensure_ascii=False).lower()
    needles = [
        n.strip().lower()
        for n in str(getattr(args, "privacy_needle", "") or "").split(",")
        if n.strip()
    ]
    out["privacy_leak"] = [n for n in needles if n in blob]
    out["status_payload_raw_keys"] = [
        k for k in ("transcript", "raw_text") if k in blob
    ]
    out["ok"] = (state == "done") and not out["privacy_leak"]
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["ok"] else 1


def cmd_upload_plaintext(args):
    """One-shot plaintext genesis ingest — mirrors the iOS uploadGenesisPlaintext:
    a single POST of the old history_import payload shape to /v1/genesis/imports/plaintext.
    No client-side sealing/chunking. Then poll with `verify --job-id`."""
    if args.register:
        args.api_key, args.user_id, _enclave_pk, _user_pk = _provision_user(args)
        print(
            json.dumps(
                {"provisioned": True, "user_id": args.user_id, "api_key": args.api_key},
                ensure_ascii=False,
            )
        )
    content = Path(args.transcript).read_text(encoding="utf-8")
    payload = {
        "format": "auto",
        "content": content,
        "fresh_start": False,
        "client_job_id": args.client_job_id
        or ("e2e_" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:40]),
    }
    if args.ai_persona:
        payload["ai_persona_content"] = Path(args.ai_persona).read_text(
            encoding="utf-8"
        )
    if args.personal_profile:
        payload["personal_profile_content"] = Path(args.personal_profile).read_text(
            encoding="utf-8"
        )
    if args.memory_summary:
        payload["memory_summary_content"] = Path(args.memory_summary).read_text(
            encoding="utf-8"
        )
    s, b = _http(
        "POST",
        f"{args.api_url.rstrip('/')}/v1/genesis/imports/plaintext",
        args.api_key,
        json_body=payload,
    )
    job_id = (b.get("job") or {}).get("job_id") or b.get("job_id")
    print(
        json.dumps(
            {
                "ok": s < 400 and bool(job_id),
                "step": "plaintext_upload",
                "status": s,
                "job_id": job_id,
                "body": b,
            },
            ensure_ascii=False,
        )
    )
    return 0 if (s < 400 and job_id) else 1


def _box_seal_open(sealed: bytes, sk_raw: bytes) -> bytes:
    """Reverse of content_encryption.box_seal: X25519 ECDH(sk, ek_pub) ->
    HKDF-SHA256(info='feedling-box-seal-v1') -> nonce=SHA256(ek_pub||rcp_pub)[:12]
    -> ChaCha20-Poly1305 decrypt. `sealed` = ek_pub(32) || ct || tag(16)."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey,
        X25519PublicKey,
    )
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives import serialization

    ek_pub, ct = sealed[:32], sealed[32:]
    sk = X25519PrivateKey.from_private_bytes(sk_raw)
    rcp_pub = sk.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    shared = sk.exchange(X25519PublicKey.from_public_bytes(ek_pub))
    k_wrap = HKDF(
        algorithm=SHA256(), length=32, salt=None, info=b"feedling-box-seal-v1"
    ).derive(shared)
    nonce = hashlib.sha256(ek_pub + rcp_pub).digest()[:12]
    return ChaCha20Poly1305(k_wrap).decrypt(nonce, ct, None)


def _decrypt_envelope_user(env: dict, sk_raw: bytes) -> str:
    """Decrypt a shared envelope's body with the user's content private key.
    AAD = owner_user_id|v|id (must match content_encryption.build_envelope)."""
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    K = _box_seal_open(base64.b64decode(env["K_user"]), sk_raw)
    aad = f"{env['owner_user_id']}|{env.get('v', 1)}|{env['id']}".encode("utf-8")
    pt = ChaCha20Poly1305(K).decrypt(
        base64.b64decode(env["nonce"]), base64.b64decode(env["body_ct"]), aad
    )
    return pt.decode("utf-8", "replace")


def _norm_text(value: object) -> str:
    text = str(value or "").lower()
    return re.sub(r"[\s，。、“”‘’：:；;,.!?！？\-_/\\|()（）\[\]{}<>《》]+", "", text)


_ENGLISH_NEGATION_RE = re.compile(
    r"\b(?:no|never|cannot|can't|cant|won't|wont|isn't|isnt|aren't|arent|"
    r"wasn't|wasnt|weren't|werent|doesn't|doesnt|don't|dont|didn't|didnt|"
    r"hasn't|hasnt|haven't|havent|hadn't|hadnt|false|incorrect|wrong)\b|"
    r"\bnot\s+(?!only\b|just\b)",
    re.IGNORECASE,
)
_CHINESE_NEGATION_RE = re.compile(r"(?:并非|绝非|从不|不是|没有|不(?!仅|只)|没|未|无)")
_SEMANTIC_REVIEW_SURFACES = ["identity", "persona", "memories"]
_AGENT_CHAT_ROLES = {"agent", "assistant", "openclaw"}
_SEMANTIC_JUDGMENT_BOOL_KEYS = (
    "persona_identity_consistent",
    "ground_truth_facts_supported",
    "contradictions_absent",
)


def _contains_explicit_negation(value: object) -> bool:
    text = str(value or "")
    return bool(_ENGLISH_NEGATION_RE.search(text) or _CHINESE_NEGATION_RE.search(text))


def _value_explicitly_negated(observed: object, expected: object) -> bool:
    """Catch direct negation without pretending to perform semantic review."""
    text = str(observed or "").casefold()
    term = str(expected or "").strip().casefold()
    if not text or not term:
        return False
    escaped = re.escape(term).replace(r"\ ", r"[\s_-]+")
    english = re.compile(
        rf"(?:\b(?:no|never)\b|\bnot\b(?![\s_-]+(?:only|just)\b)|"
        rf"\b(?:is|are|was|were|do|does|did)\s+n['’]?t\b)"
        rf"[\s_-]*"
        rf"(?:the\s+)?{escaped}\b",
        re.IGNORECASE,
    )
    chinese = re.compile(rf"(?:并非|绝非|不是|不叫|不属于|没有|无)\s*{escaped}")
    return bool(english.search(text) or chinese.search(text))


def _value_lexically_supported(observed: object, expected: object) -> bool:
    expected_norm = _norm_text(expected)
    return bool(
        expected_norm
        and expected_norm in _norm_text(observed)
        and not _value_explicitly_negated(observed, expected)
    )


def _memory_text(memory: dict) -> str:
    parts: list[str] = []
    for key in ("title", "description", "summary", "content", "her_quote", "context"):
        value = memory.get(key)
        if value:
            parts.append(str(value))
    if not parts and isinstance(memory.get("inner"), dict):
        parts.append(_memory_text(memory["inner"]))
    return "｜".join(parts).strip()


def _memory_duplicate_text(memory: dict) -> str:
    for key in ("description", "content", "summary"):
        value = str(memory.get(key) or "").strip()
        if value:
            return value
    if isinstance(memory.get("inner"), dict):
        return _memory_duplicate_text(memory["inner"])
    return _memory_text(memory)


def _fact_keywords(fact: dict) -> list[str]:
    keywords = fact.get("keywords")
    if isinstance(keywords, list) and keywords:
        return [str(k).strip() for k in keywords if str(k).strip()]
    return [str(fact.get("text") or "").strip()]


def _fact_matched(fact: dict, memory_text: str) -> bool:
    normalized_memory = _norm_text(memory_text)
    keywords = [_norm_text(k) for k in _fact_keywords(fact)]
    keywords = [k for k in keywords if k]
    if not keywords:
        return False
    return all(k in normalized_memory for k in keywords) and not _fact_contradicted(
        fact, memory_text
    )


def _fact_contradicted(fact: dict, memory_text: str) -> bool:
    """Flag explicit negation in a clause containing every locked fact keyword."""
    expected_text = str(fact.get("text") or "")
    if _contains_explicit_negation(expected_text):
        return False
    keywords = [_norm_text(k) for k in _fact_keywords(fact)]
    keywords = [keyword for keyword in keywords if keyword]
    if not keywords:
        return False
    clauses = re.split(r"[\n\r.!?！？。；;]+", str(memory_text or ""))
    return any(
        all(keyword in _norm_text(clause) for keyword in keywords)
        and _contains_explicit_negation(clause)
        for clause in clauses
        if clause.strip()
    )


def _semantic_judgment_summary(
    judgment: object,
    expected_fact_ids: list[str],
    evidence_sha256: str,
) -> dict:
    """Return bounded evidence that the qualification agent made the semantic call."""
    required_keys = {
        "schema_version",
        "judge",
        "evidence_sha256",
        "reviewed_surfaces",
        "reviewed_fact_ids",
        *_SEMANTIC_JUDGMENT_BOOL_KEYS,
    }
    provided = (
        isinstance(judgment, dict)
        and set(judgment) == required_keys
        and judgment.get("schema_version") == 1
        and judgment.get("judge") == "qualification_agent"
        and bool(re.fullmatch(r"[0-9a-f]{64}", evidence_sha256))
        and judgment.get("evidence_sha256") == evidence_sha256
        and judgment.get("reviewed_surfaces") == _SEMANTIC_REVIEW_SURFACES
        and judgment.get("reviewed_fact_ids") == expected_fact_ids
        and all(type(judgment.get(key)) is bool for key in _SEMANTIC_JUDGMENT_BOOL_KEYS)
    )
    return {
        "required": True,
        "provided": bool(provided),
        "judge": "qualification_agent" if provided else None,
        "evidence_sha256": evidence_sha256 if provided else None,
        "reviewed_surfaces": list(_SEMANTIC_REVIEW_SURFACES) if provided else [],
        "reviewed_fact_ids": list(expected_fact_ids) if provided else [],
        **{
            key: judgment[key] if provided else None
            for key in _SEMANTIC_JUDGMENT_BOOL_KEYS
        },
    }


def _dimension_matches(identity_dims: list, expected_dims: list) -> bool:
    if not expected_dims:
        return bool(identity_dims)
    blob = _norm_text(json.dumps(identity_dims, ensure_ascii=False))
    rendered = json.dumps(identity_dims, ensure_ascii=False)
    for dim in expected_dims:
        raw_name = dim.get("name") if isinstance(dim, dict) else dim
        name = _norm_text(raw_name)
        if name and name not in blob:
            return False
        if raw_name and _value_explicitly_negated(rendered, raw_name):
            return False
    return True


def _duplicate_pairs(memories: list[dict]) -> list[dict]:
    seen: dict[str, str] = {}
    pairs: list[dict] = []
    for idx, memory in enumerate(memories):
        text = _norm_text(_memory_duplicate_text(memory))
        if not text:
            continue
        mid = str(memory.get("id") or f"memory_{idx}")
        if text in seen:
            pairs.append(
                {"left_id": seen[text], "right_id": mid, "reason": "normalized_text"}
            )
        else:
            seen[text] = mid
    return pairs


def _privacy_forbidden_values(fixture: dict) -> list[str]:
    privacy = fixture.get("privacy") if isinstance(fixture.get("privacy"), dict) else {}
    values = privacy.get("forbidden_in_agent_identity_or_persona") or []
    if not isinstance(values, list):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def _privacy_surface_has_forbidden(surface: object, forbidden: list[str]) -> bool:
    """Return a boolean leak signal without returning or logging matched content."""
    if isinstance(surface, str):
        rendered = surface
    else:
        rendered = json.dumps(surface, ensure_ascii=False, sort_keys=True, default=str)
    folded = rendered.casefold()
    normalized = _norm_text(rendered)
    for value in forbidden:
        if not value:
            continue
        normalized_value = _norm_text(value)
        if value.casefold() in folded or (
            normalized_value and normalized_value in normalized
        ):
            return True
    return False


def _utc_today() -> date:
    """Return the qualification clock's UTC calendar date.

    The deployed backend may use a neighboring local calendar date around UTC
    midnight, so relationship-duration qualification accepts a one-day window
    around this value while still requiring the stored anchor itself exactly.
    """

    return datetime.now(timezone.utc).date()


def _relationship_checks(relationship: dict, identity_meta: dict) -> tuple[bool, bool]:
    expected_start = str(relationship.get("relationship_started_at") or "").strip()
    stored_start = str(identity_meta.get("relationship_started_at") or "").strip()
    days = identity_meta.get("days_with_user")

    if expected_start:
        try:
            start_date = date.fromisoformat(expected_start)
        except ValueError:
            return False, False
        anchor_exact = stored_start == expected_start
        expected_days = max(0, (_utc_today() - start_date).days)
        days_match = type(days) is int and days >= 0 and abs(days - expected_days) <= 1
        return anchor_exact, days_match

    # Preserve the generic acceptance helper's older explicit-duration contract.
    # The locked P0-06 fixture always supplies an exact relationship start date.
    expected_days = relationship.get("expected_days_with_user")
    return (
        True,
        type(days) is int and isinstance(expected_days, int) and days == expected_days,
    )


def evaluate_distill_acceptance(
    fixture: dict,
    *,
    identity: dict,
    identity_meta: dict,
    memories: list[dict],
    validate: dict,
    persona_text: str,
    voice_text: str,
    greeting_messages: list[dict],
    job: dict,
    semantic_judgment: dict | None = None,
    evidence_sha256: str = "",
) -> dict:
    expected_persona = (
        fixture.get("persona") if isinstance(fixture.get("persona"), dict) else {}
    )
    relationship = (
        fixture.get("relationship")
        if isinstance(fixture.get("relationship"), dict)
        else {}
    )
    ground_truth = (
        fixture.get("ground_truth")
        if isinstance(fixture.get("ground_truth"), dict)
        else {}
    )
    expected_facts = [
        f for f in (ground_truth.get("facts") or []) if isinstance(f, dict)
    ]
    expected_name = str(expected_persona.get("agent_name") or "").strip()
    expected_category = str(expected_persona.get("category") or "").strip()
    expected_dims = [
        d for d in (expected_persona.get("dimensions") or []) if isinstance(d, dict)
    ]
    expected_fact_ids = [str(fact.get("id") or "") for fact in expected_facts]
    semantic = _semantic_judgment_summary(
        semantic_judgment, expected_fact_ids, evidence_sha256
    )

    memory_rows = [
        {
            "id": str(memory.get("id") or f"memory_{idx}"),
            "text": _memory_text(memory),
            "raw": memory,
        }
        for idx, memory in enumerate(memories)
        if isinstance(memory, dict)
    ]
    recalled: list[dict] = []
    missed: list[dict] = []
    contradicted: list[dict] = []
    matched_memory_ids: set[str] = set()
    for fact in expected_facts:
        contradiction_matches = [
            row for row in memory_rows if _fact_contradicted(fact, row["text"])
        ]
        if contradiction_matches:
            contradicted.append(
                {
                    "id": str(fact.get("id") or ""),
                    "matched_memory_ids": [row["id"] for row in contradiction_matches],
                }
            )
        matches = [row for row in memory_rows if _fact_matched(fact, row["text"])]
        if matches:
            recalled.append(
                {
                    "id": str(fact.get("id") or ""),
                    "matched_memory_ids": [m["id"] for m in matches],
                }
            )
            matched_memory_ids.update(m["id"] for m in matches)
        else:
            missed.append(
                {
                    "id": str(fact.get("id") or ""),
                }
            )

    false_positives = [
        {"id": row["id"]}
        for row in memory_rows
        if row["id"] not in matched_memory_ids and row["text"]
    ]
    duplicates = _duplicate_pairs([row["raw"] for row in memory_rows])
    total = len(expected_facts)
    memory_count = len(memory_rows)

    agent_name = str(identity.get("agent_name") or "").strip()
    category = str(identity.get("category") or "").strip()
    self_intro = str(identity.get("self_introduction") or "").strip()
    dims = (
        identity.get("dimensions")
        if isinstance(identity.get("dimensions"), list)
        else []
    )
    days = identity_meta.get("days_with_user")
    relationship_anchor_exact, relationship_days_match = _relationship_checks(
        relationship, identity_meta
    )
    greeting_ok = any(
        str(m.get("content") or m.get("text") or "").strip()
        and str(m.get("role") or "").lower() in _AGENT_CHAT_ROLES
        for m in greeting_messages
        if isinstance(m, dict)
    )
    voice_ok = bool(
        str(voice_text or "").strip() or job.get("voice_ref") or job.get("voice_sha256")
    )

    forbidden = _privacy_forbidden_values(fixture)
    # Keep self-introduction as its own surface so a failure says where the leak
    # occurred.  The identity surface intentionally excludes that field to avoid
    # double-counting the same occurrence.
    identity_without_intro = dict(identity)
    identity_without_intro.pop("self_introduction", None)
    privacy_violating_surfaces = [
        surface_name
        for surface_name, surface_value in (
            ("identity", identity_without_intro),
            ("persona", persona_text),
            ("self_introduction", self_intro),
        )
        if _privacy_surface_has_forbidden(surface_value, forbidden)
    ]

    checks = {
        "identity_agent_name": bool(agent_name)
        and (
            not expected_name or _value_lexically_supported(agent_name, expected_name)
        ),
        "identity_category": bool(category)
        and (
            not expected_category
            or _value_lexically_supported(category, expected_category)
        ),
        "identity_dimensions": bool(dims)
        and all(
            isinstance(d, dict) and str(d.get("description") or "").strip()
            for d in dims
        )
        and _dimension_matches(dims, expected_dims),
        "identity_self_introduction": bool(self_intro)
        and all(
            _value_lexically_supported(self_intro, k)
            for k in expected_persona.get("self_introduction_keywords", [])
            if str(k).strip()
        ),
        "memory_count_reasonable": memory_count >= min(1, total),
        "ground_truth_recall": len(missed) == 0,
        "no_explicit_contradictions": len(contradicted) == 0,
        "no_duplicate_memories": len(duplicates) == 0,
        "relationship_started_at": relationship_anchor_exact,
        "relationship_days": relationship_days_match,
        "greeting_non_empty": greeting_ok,
        "persona_non_empty": bool(
            str(persona_text or "").strip()
            or job.get("persona_ref")
            or job.get("persona_sha256")
        ),
        "voice_non_empty": voice_ok,
        "validate_passing": bool(validate.get("passing")),
        "privacy_identity_clear": "identity" not in privacy_violating_surfaces,
        "privacy_persona_clear": "persona" not in privacy_violating_surfaces,
        "privacy_self_introduction_clear": "self_introduction"
        not in privacy_violating_surfaces,
        "semantic_persona_identity_consistent": bool(
            semantic["provided"] and semantic["persona_identity_consistent"]
        ),
        "semantic_ground_truth_facts_supported": bool(
            semantic["provided"] and semantic["ground_truth_facts_supported"]
        ),
        "semantic_contradictions_absent": bool(
            semantic["provided"] and semantic["contradictions_absent"]
        ),
    }
    metrics = {
        "ground_truth_total": total,
        "recall_count": len(recalled),
        "miss_count": len(missed),
        "recall_rate": (len(recalled) / total) if total else 1.0,
        "miss_rate": (len(missed) / total) if total else 0.0,
        "memory_count": memory_count,
        "false_positive_count": len(false_positives),
        "false_positive_rate": (
            (len(false_positives) / memory_count) if memory_count else 0.0
        ),
        "duplicate_pair_count": len(duplicates),
        "duplicate_rate": (len(duplicates) / memory_count) if memory_count else 0.0,
        "explicit_contradiction_count": len(contradicted),
    }
    hard_ok = all(checks.values())
    # False positives are reported but not a hard fail by default: the model may extract
    # extra valid facts from support materials. Miss/duplicate/check failures are hard.
    return {
        "ok": bool(hard_ok),
        "job_id": job.get("job_id") or "",
        "metrics": metrics,
        "checks": checks,
        "recalled_facts": recalled,
        "missed_facts": missed,
        "contradicted_facts": contradicted,
        "false_positives": false_positives,
        "duplicates": duplicates,
        "semantic_judgment": semantic,
        # Never echo identity, persona, self-introduction, memory text, or the
        # forbidden fixture values in a report.  CI only needs bounded signals;
        # the intelligent qualification agent may inspect plaintext in memory.
        "privacy": {
            "forbidden_value_count": len(forbidden),
            "violation_count": len(privacy_violating_surfaces),
            "violating_surfaces": privacy_violating_surfaces,
        },
        "identity_summary": {
            "agent_name_present": bool(agent_name),
            "category_present": bool(category),
            "dimension_count": len(dims),
            "self_introduction_present": bool(self_intro),
            "relationship_started_at_matches": relationship_anchor_exact,
            "relationship_days_present": type(days) is int,
        },
        "validate_summary": {"passing": bool(validate.get("passing"))},
    }


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_distill_acceptance_report(report: dict) -> str:
    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    lines = [
        "# Genesis 人物信息蒸馏验收报告",
        "",
        f"结论：{'PASS' if report.get('ok') else 'FAIL'}",
        "",
        "## 指标总览",
        f"- ground-truth 总数：{metrics.get('ground_truth_total', 0)}",
        f"- 召回：{metrics.get('recall_count', 0)} / {metrics.get('ground_truth_total', 0)}",
        f"- 召回率：{_pct(float(metrics.get('recall_rate') or 0.0))}",
        f"- 漏抽率：{_pct(float(metrics.get('miss_rate') or 0.0))}",
        f"- 误报数：{metrics.get('false_positive_count', 0)}（误报率：{_pct(float(metrics.get('false_positive_rate') or 0.0))}）",
        f"- 重复对：{metrics.get('duplicate_pair_count', 0)}（重复率：{_pct(float(metrics.get('duplicate_rate') or 0.0))}）",
        "",
        "## 数据齐检查",
    ]
    checks = report.get("checks") if isinstance(report.get("checks"), dict) else {}
    for key, value in checks.items():
        lines.append(f"- {key}：{'PASS' if value else 'FAIL'}")
    lines.extend(["", "## 漏抽"])
    missed = (
        report.get("missed_facts")
        if isinstance(report.get("missed_facts"), list)
        else []
    )
    if missed:
        for item in missed:
            lines.append(f"- 漏抽：{item.get('id', '')}")
    else:
        lines.append("- 无")
    lines.extend(["", "## 误报"])
    false_positives = (
        report.get("false_positives")
        if isinstance(report.get("false_positives"), list)
        else []
    )
    if false_positives:
        for item in false_positives:
            lines.append(f"- 误报：{item.get('id', '')}")
    else:
        lines.append("- 无")
    lines.extend(["", "## 重复"])
    duplicates = (
        report.get("duplicates") if isinstance(report.get("duplicates"), list) else []
    )
    if duplicates:
        for item in duplicates:
            lines.append(
                f"- 重复：{item.get('left_id', '')} ↔ {item.get('right_id', '')}｜{item.get('reason', '')}"
            )
    else:
        lines.append("- 无")
    return "\n".join(lines) + "\n"


_UPLOAD_MATERIALS = (
    ("chat_history", "history_filename"),
    ("ai_persona", "ai_persona_filename"),
    ("personal_profile", "personal_profile_filename"),
    ("memory_summary", "memory_summary_filename"),
)
_MAX_QUALIFICATION_UPLOAD_BYTES = 1024 * 1024


def _load_fixture(path: str) -> dict:
    fixture_path = Path(path)
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("fixture must be a JSON object")
    materials = data.get("materials") if isinstance(data.get("materials"), dict) else {}
    upload_files = (
        materials.get("upload_files")
        if isinstance(materials.get("upload_files"), dict)
        else None
    )
    if upload_files is not None:
        expected = {material for material, _filename_field in _UPLOAD_MATERIALS}
        if set(upload_files) != expected:
            raise ExistingSessionDistillError("fixture", "four_upload_files_required")
        fixture_root = fixture_path.parent.resolve(strict=True)
        for material, _filename_field in _UPLOAD_MATERIALS:
            spec = upload_files.get(material)
            relative = str(spec.get("path") or "") if isinstance(spec, dict) else ""
            candidate = fixture_path.parent / relative
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(fixture_root)
                metadata = candidate.lstat()
                if candidate.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                    raise OSError
                raw = candidate.read_bytes()
            except (OSError, ValueError):
                raise ExistingSessionDistillError(
                    "fixture", "upload_file_unreadable"
                ) from None
            if not raw or len(raw) > _MAX_QUALIFICATION_UPLOAD_BYTES:
                raise ExistingSessionDistillError("fixture", "upload_file_size_invalid")
            try:
                materials[material] = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise ExistingSessionDistillError(
                    "fixture", "upload_file_encoding_invalid"
                ) from None
    return data


def _decrypt_memory_rows(
    rows: list,
    sk_raw: bytes,
    expected_owner_user_id: str = "",
) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = {"id": str(row.get("id") or "")} if expected_owner_user_id else dict(row)
        if row.get("body_ct"):
            if expected_owner_user_id:
                _require_envelope_owner(
                    row,
                    expected_owner_user_id,
                    stage="memory",
                    code="memory_owner_mismatch",
                )
            try:
                inner = json.loads(_decrypt_envelope_user(row, sk_raw))
                if isinstance(inner, dict):
                    item.update(inner)
                    item["inner"] = inner
                else:
                    item["decrypt_error"] = "memory_plaintext_not_object"
            except ExistingSessionDistillError:
                raise
            except Exception:  # noqa: BLE001
                item["decrypt_error"] = "memory_decrypt_failed"
        elif expected_owner_user_id:
            item["decrypt_error"] = "memory_ciphertext_missing"
        out.append(item)
    return out


class ExistingSessionDistillError(RuntimeError):
    """A bounded, secret-free failure from an existing-session distill run."""

    def __init__(self, stage: str, code: str, http_status: int | None = None):
        self.stage = stage
        self.code = code
        self.http_status = http_status
        suffix = f" (http_status={http_status})" if isinstance(http_status, int) else ""
        super().__init__(f"{stage}: {code}{suffix}")

    def as_result(self) -> dict:
        result = {"ok": False, "stage": self.stage, "code": self.code}
        if isinstance(self.http_status, int):
            result["http_status"] = self.http_status
        return result


def _require_envelope_owner(
    envelope: dict,
    expected_user_id: str,
    *,
    stage: str,
    code: str,
) -> None:
    if str(envelope.get("owner_user_id") or "") != expected_user_id:
        raise ExistingSessionDistillError(stage, code)


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _secure_file_flags(base_flags: int) -> int:
    flags = base_flags
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _read_owner_only_file(path_value: str, *, stage: str, code_prefix: str) -> bytes:
    path = Path(path_value)
    try:
        before = path.lstat()
    except OSError:
        raise ExistingSessionDistillError(stage, f"{code_prefix}_unreadable") from None
    if not stat.S_ISREG(before.st_mode):
        raise ExistingSessionDistillError(stage, f"{code_prefix}_not_regular")
    try:
        fd = os.open(path, _secure_file_flags(os.O_RDONLY))
    except OSError:
        raise ExistingSessionDistillError(stage, f"{code_prefix}_unreadable") from None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise ExistingSessionDistillError(stage, f"{code_prefix}_not_regular")
        if opened.st_uid != os.geteuid():
            raise ExistingSessionDistillError(stage, f"{code_prefix}_owner_mismatch")
        if stat.S_IMODE(opened.st_mode) != 0o600:
            raise ExistingSessionDistillError(
                stage, f"{code_prefix}_permissions_invalid"
            )
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            return handle.read()
    except ExistingSessionDistillError:
        raise
    except OSError:
        raise ExistingSessionDistillError(stage, f"{code_prefix}_unreadable") from None
    finally:
        if fd >= 0:
            os.close(fd)


def _require_private_path_outside_artifacts(
    path_value: str,
    artifact_dir: str,
    *,
    stage: str = "capture",
    code_prefix: str = "private_evidence",
) -> Path:
    destination = Path(path_value)
    if not destination.is_absolute() or not str(artifact_dir or "").strip():
        raise ExistingSessionDistillError(stage, f"{code_prefix}_path_invalid")
    artifact_root = Path(artifact_dir).resolve(strict=False)
    resolved_destination = destination.resolve(strict=False)
    try:
        inside_artifacts = os.path.commonpath(
            [str(resolved_destination), str(artifact_root)]
        ) == str(artifact_root)
    except ValueError:
        inside_artifacts = False
    if inside_artifacts:
        raise ExistingSessionDistillError(stage, f"{code_prefix}_inside_artifacts")
    return destination


def _write_private_report(path_value: str, artifact_dir: str, content: str) -> None:
    """Create an owner-only sanitized helper report outside public artifacts."""
    destination = _require_private_path_outside_artifacts(
        path_value,
        artifact_dir,
        stage="report",
        code_prefix="report",
    )
    if not destination.parent.is_dir():
        raise ExistingSessionDistillError("report", "report_parent_missing")

    flags = _secure_file_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        fd = os.open(destination, flags, 0o600)
    except OSError:
        raise ExistingSessionDistillError("report", "report_create_failed") from None
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise ExistingSessionDistillError("report", "report_write_failed") from None
    finally:
        if fd >= 0:
            os.close(fd)


def _write_private_evidence(
    path_value: str,
    artifact_dir: str,
    evidence: dict,
) -> str:
    destination = _require_private_path_outside_artifacts(path_value, artifact_dir)
    if not destination.parent.is_dir():
        raise ExistingSessionDistillError("capture", "private_evidence_parent_missing")

    raw = _canonical_json_bytes(evidence)
    flags = _secure_file_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        fd = os.open(destination, flags, 0o600)
    except OSError:
        raise ExistingSessionDistillError(
            "capture", "private_evidence_create_failed"
        ) from None
    wrote = False
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        wrote = True
    except OSError:
        raise ExistingSessionDistillError(
            "capture", "private_evidence_write_failed"
        ) from None
    finally:
        if fd >= 0:
            os.close(fd)
        if not wrote:
            try:
                destination.unlink()
            except OSError:
                pass
    return _sha256_hex(raw)


def _delete_private_evidence(path_value: str) -> None:
    try:
        current = os.lstat(path_value)
        if stat.S_ISDIR(current.st_mode):
            return
        os.unlink(path_value)
    except FileNotFoundError:
        pass
    except OSError:
        # Finalization remains fail closed if cleanup cannot be proven.
        raise ExistingSessionDistillError(
            "cleanup", "private_evidence_delete_failed"
        ) from None


def _decrypt_chat_history(
    messages: object,
    sk_raw: bytes,
    expected_user_id: str,
) -> tuple[list[dict], int, int]:
    """Decrypt opaque live history and discard any untrusted plaintext decoys."""
    if not isinstance(messages, list):
        return [], 1, 0
    decrypted: list[dict] = []
    decrypt_errors = 0
    decrypted_agent_messages = 0
    for raw in messages:
        if not isinstance(raw, dict):
            decrypt_errors += 1
            continue
        role = str(raw.get("role") or "").lower()
        item = {"role": role}
        if raw.get("body_ct"):
            _require_envelope_owner(
                raw,
                expected_user_id,
                stage="chat",
                code="chat_owner_mismatch",
            )
            try:
                item["content"] = _decrypt_envelope_user(raw, sk_raw)
                if role in _AGENT_CHAT_ROLES:
                    decrypted_agent_messages += 1
            except Exception:  # noqa: BLE001
                item["content"] = ""
                item["decrypt_error"] = "chat_decrypt_failed"
                decrypt_errors += 1
        else:
            # Qualification must prove the user-visible greeting came from the
            # encrypted history record, never from a server-supplied plaintext
            # convenience/decoy field.
            item["content"] = ""
            if role in _AGENT_CHAT_ROLES:
                item["decrypt_error"] = "chat_ciphertext_missing"
                decrypt_errors += 1
        decrypted.append(item)
    return decrypted, decrypt_errors, decrypted_agent_messages


_QUALIFICATION_JOB_ID_RE = re.compile(r"^genesis_[0-9a-f]{16}$")
_QUALIFICATION_ARCHIVE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _require_success_status(status: int, stage: str, rejection_code: str) -> None:
    if 300 <= status < 400:
        raise ExistingSessionDistillError(stage, "redirect_rejected", status)
    if not 200 <= status < 300:
        raise ExistingSessionDistillError(stage, rejection_code, status)


def _qualification_base_url(raw: str) -> str:
    parsed = urllib.parse.urlsplit(str(raw or "").strip())
    try:
        port = parsed.port
    except ValueError:
        raise ExistingSessionDistillError("target", "unsafe_target") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "test-api.feedling.app"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ExistingSessionDistillError("target", "unsafe_target")
    return "https://test-api.feedling.app"


def _existing_session_request(
    request_fn,
    method: str,
    url: str,
    api_key: str,
    stage: str,
    **kwargs,
):
    """Normalize transport failures without reflecting response bodies/exceptions."""
    try:
        response = request_fn(method, url, api_key, **kwargs)
    except (Exception, SystemExit):  # noqa: BLE001
        raise ExistingSessionDistillError(stage, "request_failed") from None
    if (
        not isinstance(response, tuple)
        or len(response) != 2
        or not isinstance(response[0], int)
        or not isinstance(response[1], dict)
    ):
        raise ExistingSessionDistillError(stage, "response_invalid")
    return response


def _qualification_material_uploads(fixture: dict) -> list[dict]:
    materials = (
        fixture.get("materials") if isinstance(fixture.get("materials"), dict) else {}
    )
    specs = (
        materials.get("upload_files")
        if isinstance(materials.get("upload_files"), dict)
        else {}
    )
    if set(specs) != {material for material, _field in _UPLOAD_MATERIALS}:
        raise ExistingSessionDistillError("fixture", "four_upload_files_required")
    uploads: list[dict] = []
    filenames: set[str] = set()
    for material, filename_field in _UPLOAD_MATERIALS:
        spec = specs.get(material)
        if not isinstance(spec, dict):
            raise ExistingSessionDistillError("fixture", "upload_file_spec_invalid")
        filename = str(spec.get("filename") or "")
        content_type = str(spec.get("content_type") or "")
        content_text = materials.get(material)
        if not isinstance(content_text, str) or not content_text.strip():
            raise ExistingSessionDistillError("fixture", "four_materials_required")
        content = content_text.encode("utf-8")
        if not content or len(content) > _MAX_QUALIFICATION_UPLOAD_BYTES:
            raise ExistingSessionDistillError("fixture", "upload_file_size_invalid")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", filename):
            raise ExistingSessionDistillError("fixture", "upload_filename_invalid")
        if filename in filenames:
            raise ExistingSessionDistillError("fixture", "upload_filename_duplicate")
        if not re.fullmatch(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+", content_type):
            raise ExistingSessionDistillError("fixture", "upload_content_type_invalid")
        filenames.add(filename)
        uploads.append(
            {
                "material": material,
                "filename_field": filename_field,
                "filename": filename,
                "content_type": content_type,
                "content": content,
                "content_text": content_text,
                "content_sha256": _sha256_hex(content),
                "size_bytes": len(content),
            }
        )
    return uploads


def _archive_existing_session_materials(
    *,
    request_fn,
    base: str,
    api_key: str,
    user_id: str,
    client_job_id: str,
    uploads: list[dict],
) -> list[dict]:
    receipts: list[dict] = []
    for upload in uploads:
        material = str(upload["material"])
        stage = f"archive_{material}"
        status, body = _existing_session_request(
            request_fn,
            "POST",
            f"{base}/v1/onboarding/archive",
            api_key,
            stage,
            multipart_fields={
                "filename": upload["filename"],
                "content_type": upload["content_type"],
                "client_job_id": client_job_id,
            },
            file_upload={
                "field_name": "file",
                "filename": upload["filename"],
                "content_type": upload["content_type"],
                "content": upload["content"],
            },
            # The archive endpoint has no idempotency lookup. A lost success
            # response must fail closed instead of retrying a duplicate upload.
            retries=1,
        )
        _require_success_status(status, stage, "archive_rejected")
        archive_id = str(body.get("archive_id") or "")
        expected_key = (
            f"onboarding/{user_id}/{archive_id}/{upload['filename']}"
            if _QUALIFICATION_ARCHIVE_ID_RE.fullmatch(archive_id)
            else ""
        )
        if (
            status != 201
            or body.get("status") != "ok"
            or not _QUALIFICATION_ARCHIVE_ID_RE.fullmatch(archive_id)
            or str(body.get("key") or "") != expected_key
        ):
            raise ExistingSessionDistillError(stage, "archive_receipt_invalid", status)
        receipts.append(
            {
                "material": material,
                "filename": upload["filename"],
                "content_type": upload["content_type"],
                "content_sha256": upload["content_sha256"],
                "size_bytes": upload["size_bytes"],
                "http_status": status,
                "archive_id": archive_id,
                "upload_accepted": True,
                "storage_key_scope_verified": True,
            }
        )
    return receipts


def _build_existing_session_payload(
    fixture: dict, client_job_id: str, uploads: list[dict]
) -> dict:
    materials = (
        fixture.get("materials") if isinstance(fixture.get("materials"), dict) else {}
    )
    relationship = (
        fixture.get("relationship")
        if isinstance(fixture.get("relationship"), dict)
        else {}
    )
    payload = {
        "format": str(materials.get("format") or "auto"),
        "content": str(materials.get("chat_history") or ""),
        "fresh_start": False,
        "client_job_id": client_job_id,
    }
    if relationship.get("relationship_started_at"):
        payload["relationship_started_at"] = str(
            relationship["relationship_started_at"]
        )
    for src_key, payload_key in (
        ("ai_persona", "ai_persona_content"),
        ("personal_profile", "personal_profile_content"),
        ("memory_summary", "memory_summary_content"),
    ):
        value = materials.get(src_key)
        if value:
            payload[payload_key] = str(value)
    for upload in uploads:
        payload[str(upload["filename_field"])] = str(upload["filename"])
    return payload


def _job_upload_metadata_evidence(job: dict, expected_client_job_id: str) -> dict:
    """Require and validate the deployed Genesis filename/source metadata."""
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    if "client_job_id" not in metadata:
        raise ExistingSessionDistillError("distill", "job_client_job_id_missing")
    if str(metadata.get("client_job_id") or "") != expected_client_job_id:
        raise ExistingSessionDistillError("distill", "job_client_job_id_mismatch")
    file_count_exposed = "file_count" in metadata
    source_fields = {
        "history": "history_count",
        "ai_persona": "ai_persona_count",
        "user_profile": "user_profile_count",
        "memory_summary": "memory_summary_count",
    }
    exposed_source_fields = [
        field for field in source_fields.values() if field in metadata
    ]
    if exposed_source_fields and len(exposed_source_fields) != len(source_fields):
        raise ExistingSessionDistillError("distill", "job_source_counts_incomplete")
    source_counts_exposed = len(exposed_source_fields) == len(source_fields)
    if file_count_exposed:
        try:
            file_count = int(metadata["file_count"])
        except (TypeError, ValueError):
            raise ExistingSessionDistillError(
                "distill", "job_file_count_invalid"
            ) from None
        if file_count != len(_UPLOAD_MATERIALS):
            raise ExistingSessionDistillError("distill", "job_file_count_mismatch")
    else:
        file_count = None

    source_families: list[str] = []
    if source_counts_exposed:
        for family, field in source_fields.items():
            try:
                count = int(metadata[field])
            except (TypeError, ValueError):
                raise ExistingSessionDistillError(
                    "distill", "job_source_counts_invalid"
                ) from None
            if count <= 0:
                raise ExistingSessionDistillError(
                    "distill", "job_source_family_missing"
                )
            source_families.append(family)
    if not file_count_exposed or not source_counts_exposed:
        raise ExistingSessionDistillError("distill", "job_upload_metadata_missing")
    return {
        "client_job_id_exposed": True,
        "client_job_id_matched": True,
        "file_count_exposed": file_count_exposed,
        "file_count": file_count,
        "source_counts_exposed": source_counts_exposed,
        "source_families": source_families,
    }


def _job_view(body: dict) -> dict:
    """Normalize current and older import-status response shapes."""
    merged: dict = {}
    state = body.get("state") if isinstance(body, dict) else None
    if isinstance(state, dict):
        merged.update(state)
    job = body.get("job") if isinstance(body, dict) else None
    if isinstance(job, dict):
        merged.update(job)
    if isinstance(body, dict):
        for key in (
            "job_id",
            "status",
            "persona_ref",
            "persona_sha256",
            "voice_ref",
            "voice_sha256",
        ):
            if key in body and key not in merged:
                merged[key] = body[key]
    return merged


def capture_existing_session_distill_evidence(
    *,
    api_url: str,
    api_key: str,
    user_id: str,
    content_private_key: bytes,
    fixture: dict,
    private_evidence_path: str,
    artifact_dir: str,
    timeout: float = 900,
    poll: float = 10,
    intro_timeout: float = 180,
    memory_limit: int = 100,
    client_job_id: str = "",
    request_fn=None,
) -> dict:
    """Run persona import once and capture exact decrypted surfaces for Codex.

    The private evidence file is owner-only and must live outside the public
    artifact tree. The returned receipt is content-free and safe to log.
    """
    if not str(api_key or "").strip():
        raise ExistingSessionDistillError("session", "api_key_missing")
    if not str(user_id or "").strip():
        raise ExistingSessionDistillError("session", "user_id_missing")
    if not isinstance(content_private_key, bytes) or len(content_private_key) != 32:
        raise ExistingSessionDistillError("session", "content_private_key_invalid")
    if not isinstance(fixture, dict):
        raise ExistingSessionDistillError("fixture", "fixture_invalid")

    materials = (
        fixture.get("materials") if isinstance(fixture.get("materials"), dict) else {}
    )
    if any(
        not str(materials.get(key) or "").strip()
        for key in ("chat_history", "ai_persona", "personal_profile", "memory_summary")
    ):
        raise ExistingSessionDistillError("fixture", "four_materials_required")
    ground_truth = (
        fixture.get("ground_truth")
        if isinstance(fixture.get("ground_truth"), dict)
        else {}
    )
    if not isinstance(ground_truth.get("facts"), list) or not ground_truth["facts"]:
        raise ExistingSessionDistillError("fixture", "ground_truth_required")
    if not _privacy_forbidden_values(fixture):
        raise ExistingSessionDistillError("fixture", "privacy_contract_required")
    try:
        bounded_memory_limit = int(memory_limit)
    except (TypeError, ValueError):
        raise ExistingSessionDistillError("fixture", "memory_limit_invalid") from None
    if not 1 <= bounded_memory_limit <= 500:
        raise ExistingSessionDistillError("fixture", "memory_limit_invalid")

    request = request_fn or _http
    base = _qualification_base_url(api_url)
    job_token = client_job_id or ("qa_existing_distill_" + os.urandom(8).hex())
    # Match the Genesis backend's client-job normalization exactly.  The archive
    # endpoint stores this value verbatim, while Genesis strips characters
    # outside [A-Za-z0-9_-]; accepting dots here would silently break the shared
    # token that binds the two requests.
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,96}", job_token):
        raise ExistingSessionDistillError("fixture", "client_job_id_invalid")
    uploads = _qualification_material_uploads(fixture)
    archive_receipts = _archive_existing_session_materials(
        request_fn=request,
        base=base,
        api_key=api_key,
        user_id=user_id,
        client_job_id=job_token,
        uploads=uploads,
    )
    payload = _build_existing_session_payload(fixture, job_token, uploads)
    status, body = _existing_session_request(
        request,
        "POST",
        f"{base}/v1/genesis/imports/plaintext",
        api_key,
        "upload",
        json_body=payload,
    )
    _require_success_status(status, "upload", "upload_rejected")
    upload_job = body.get("job") if isinstance(body.get("job"), dict) else {}
    job_id = str(upload_job.get("job_id") or body.get("job_id") or "")
    if not _QUALIFICATION_JOB_ID_RE.fullmatch(job_id):
        raise ExistingSessionDistillError("upload", "job_id_invalid", status)

    deadline = time.time() + max(0.0, timeout)
    job_body: dict = {}
    job: dict = {"job_id": job_id}
    while time.time() <= deadline:
        job_status, candidate = _existing_session_request(
            request,
            "GET",
            f"{base}/v1/genesis/imports/{job_id}",
            api_key,
            "poll",
        )
        _require_success_status(job_status, "poll", "job_status_rejected")
        job_body = candidate if isinstance(candidate, dict) else {}
        job = _job_view(job_body)
        job["job_id"] = job_id
        terminal = str(job.get("status") or "").lower()
        if terminal in ("done", "failed"):
            break
        time.sleep(max(0.0, poll))
    terminal = str(job.get("status") or "").lower()
    if terminal == "failed":
        raise ExistingSessionDistillError("distill", "job_failed")
    if terminal != "done":
        raise ExistingSessionDistillError("distill", "job_timeout")
    job_upload_metadata = _job_upload_metadata_evidence(job, job_token)

    identity_plain: dict = {}
    identity_meta: dict = {}
    identity_decrypted = False
    chat: dict = {}
    decrypted_chat_messages: list[dict] = []
    chat_decrypt_error_count = 0
    decrypted_agent_message_count = 0
    intro_deadline = time.time() + max(0.0, intro_timeout)
    while time.time() <= intro_deadline:
        identity_status, identity_body = _existing_session_request(
            request, "GET", f"{base}/v1/identity/get", api_key, "identity"
        )
        if 300 <= identity_status < 400:
            raise ExistingSessionDistillError(
                "identity", "redirect_rejected", identity_status
            )
        if identity_status not in (200, 404, 409):
            raise ExistingSessionDistillError(
                "identity", "identity_rejected", identity_status
            )
        if identity_status < 400 and isinstance(identity_body, dict):
            identity_payload = identity_body.get("identity") or {}
            identity_meta = (
                dict(identity_payload) if isinstance(identity_payload, dict) else {}
            )
            if isinstance(identity_payload, dict) and identity_payload.get("body_ct"):
                _require_envelope_owner(
                    identity_payload,
                    user_id,
                    stage="identity",
                    code="identity_owner_mismatch",
                )
                try:
                    decoded = json.loads(
                        _decrypt_envelope_user(identity_payload, content_private_key)
                    )
                    identity_plain = decoded if isinstance(decoded, dict) else {}
                    identity_decrypted = isinstance(decoded, dict)
                except Exception:  # noqa: BLE001
                    identity_plain = {}
            elif isinstance(identity_payload, dict):
                identity_plain = identity_payload

        chat_status, chat_body = _existing_session_request(
            request, "GET", f"{base}/v1/chat/history?limit=20", api_key, "chat"
        )
        if 300 <= chat_status < 400:
            raise ExistingSessionDistillError("chat", "redirect_rejected", chat_status)
        if chat_status not in (200, 404, 409):
            raise ExistingSessionDistillError("chat", "chat_rejected", chat_status)
        chat = chat_body if chat_status < 400 and isinstance(chat_body, dict) else {}
        (
            decrypted_chat_messages,
            chat_decrypt_error_count,
            decrypted_agent_message_count,
        ) = _decrypt_chat_history(
            chat.get("messages") or [], content_private_key, user_id
        )
        greeting_ok = any(
            str(message.get("content") or message.get("text") or "").strip()
            and str(message.get("role") or "").lower() in _AGENT_CHAT_ROLES
            for message in decrypted_chat_messages
            if isinstance(message, dict)
        )
        if str(identity_plain.get("self_introduction") or "").strip() and greeting_ok:
            break
        time.sleep(max(0.0, poll))

    memory_status, memory_body = _existing_session_request(
        request,
        "GET",
        f"{base}/v1/memory/list?limit={bounded_memory_limit}",
        api_key,
        "memory",
    )
    _require_success_status(memory_status, "memory", "memory_rejected")
    memories = _decrypt_memory_rows(
        (
            (memory_body.get("moments") or [])
            if memory_status < 400 and isinstance(memory_body, dict)
            else []
        ),
        content_private_key,
        user_id,
    )
    memory_decrypt_error_count = sum(
        1
        for memory in memories
        if isinstance(memory, dict) and memory.get("decrypt_error")
    )
    validate_status, validate_body = _existing_session_request(
        request, "GET", f"{base}/v1/onboarding/validate", api_key, "validate"
    )
    _require_success_status(validate_status, "validate", "validate_rejected")
    validate = (
        validate_body
        if validate_status < 400 and isinstance(validate_body, dict)
        else {}
    )

    persona_text = ""
    persona_decrypted = False
    persona = (
        job_body.get("persona") if isinstance(job_body.get("persona"), dict) else {}
    )
    persona_env = (
        persona.get("content_envelope")
        if isinstance(persona.get("content_envelope"), dict)
        else {}
    )
    if persona_env:
        _require_envelope_owner(
            persona_env,
            user_id,
            stage="persona",
            code="persona_owner_mismatch",
        )
        try:
            persona_text = _decrypt_envelope_user(persona_env, content_private_key)
            persona_decrypted = True
        except Exception:  # noqa: BLE001
            persona_text = ""

    capture_checks = {
        "archive_receipts_verified": len(archive_receipts) == len(_UPLOAD_MATERIALS),
        "genesis_upload_metadata_verified": bool(
            job_upload_metadata["client_job_id_exposed"]
            and job_upload_metadata["client_job_id_matched"]
            and job_upload_metadata["file_count_exposed"]
            and job_upload_metadata["source_counts_exposed"]
        ),
        "identity_envelope_decrypted": identity_decrypted,
        "persona_envelope_decrypted": persona_decrypted,
        "memory_envelopes_decrypted": memory_decrypt_error_count == 0,
        "chat_envelopes_decrypted": (
            chat_decrypt_error_count == 0 and decrypted_agent_message_count > 0
        ),
    }
    transport = {
        "used_existing_session": True,
        "created_user": False,
        "configured_provider": False,
        "job_status": terminal,
        "archive_upload_count": len(archive_receipts),
        "archive_receipts": archive_receipts,
        "genesis_upload_metadata": job_upload_metadata,
        "upload_http_status": status,
        "memory_http_status": memory_status,
        "validate_http_status": validate_status,
        "memory_decrypt_error_count": memory_decrypt_error_count,
        "chat_decrypt_error_count": chat_decrypt_error_count,
        "decrypted_agent_message_count": decrypted_agent_message_count,
    }
    ground_truth = (
        fixture.get("ground_truth")
        if isinstance(fixture.get("ground_truth"), dict)
        else {}
    )
    expected_fact_ids = [
        str(fact.get("id") or "")
        for fact in (ground_truth.get("facts") or [])
        if isinstance(fact, dict)
    ]
    fixture_sha256 = _sha256_hex(_canonical_json_bytes(fixture))
    private_evidence = {
        "schema_version": 1,
        "fixture_sha256": fixture_sha256,
        "expected_fact_ids": expected_fact_ids,
        "identity": identity_plain,
        "identity_meta": {
            "days_with_user": identity_meta.get("days_with_user"),
            "relationship_started_at": identity_meta.get("relationship_started_at"),
        },
        "persona_text": persona_text,
        "memories": memories,
        "greeting_messages": decrypted_chat_messages,
        "validate": validate,
        "voice_text": "",
        "job": job,
        "capture_checks": capture_checks,
        "transport": transport,
    }
    evidence_sha256 = _write_private_evidence(
        private_evidence_path, artifact_dir, private_evidence
    )
    return {
        "ok": True,
        "phase": "CAPTURED",
        "evidence_sha256": evidence_sha256,
        "fixture_sha256": fixture_sha256,
        "expected_fact_ids": expected_fact_ids,
        "job_id": job_id,
        "capture_checks": capture_checks,
        "transport": transport,
    }


_PRIVATE_EVIDENCE_KEYS = {
    "schema_version",
    "fixture_sha256",
    "expected_fact_ids",
    "identity",
    "identity_meta",
    "persona_text",
    "memories",
    "greeting_messages",
    "validate",
    "voice_text",
    "job",
    "capture_checks",
    "transport",
}


def _valid_existing_session_transport(value: object, fixture: dict) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "used_existing_session",
        "created_user",
        "configured_provider",
        "job_status",
        "archive_upload_count",
        "archive_receipts",
        "genesis_upload_metadata",
        "upload_http_status",
        "memory_http_status",
        "validate_http_status",
        "memory_decrypt_error_count",
        "chat_decrypt_error_count",
        "decrypted_agent_message_count",
    }:
        return False
    if (
        value["used_existing_session"] is not True
        or value["created_user"] is not False
        or value["configured_provider"] is not False
        or value["job_status"] != "done"
        or value["archive_upload_count"] != len(_UPLOAD_MATERIALS)
    ):
        return False
    integer_fields = (
        "upload_http_status",
        "memory_http_status",
        "validate_http_status",
        "memory_decrypt_error_count",
        "chat_decrypt_error_count",
        "decrypted_agent_message_count",
    )
    if any(type(value.get(field)) is not int for field in integer_fields):
        return False
    receipts = value.get("archive_receipts")
    if not isinstance(receipts, list) or len(receipts) != len(_UPLOAD_MATERIALS):
        return False
    try:
        expected_uploads = _qualification_material_uploads(fixture)
    except ExistingSessionDistillError:
        return False
    archive_ids: set[str] = set()
    for expected_upload, receipt in zip(expected_uploads, receipts, strict=True):
        if not isinstance(receipt, dict) or set(receipt) != {
            "material",
            "filename",
            "content_type",
            "content_sha256",
            "size_bytes",
            "http_status",
            "archive_id",
            "upload_accepted",
            "storage_key_scope_verified",
        }:
            return False
        archive_id = str(receipt.get("archive_id") or "")
        if (
            receipt.get("material") != expected_upload["material"]
            or receipt.get("filename") != expected_upload["filename"]
            or receipt.get("content_type") != expected_upload["content_type"]
            or receipt.get("content_sha256") != expected_upload["content_sha256"]
            or receipt.get("size_bytes") != expected_upload["size_bytes"]
            or receipt.get("http_status") != 201
            or not _QUALIFICATION_ARCHIVE_ID_RE.fullmatch(archive_id)
            or archive_id in archive_ids
            or receipt.get("upload_accepted") is not True
            or receipt.get("storage_key_scope_verified") is not True
        ):
            return False
        archive_ids.add(archive_id)
    metadata = value.get("genesis_upload_metadata")
    if not isinstance(metadata, dict) or set(metadata) != {
        "client_job_id_exposed",
        "client_job_id_matched",
        "file_count_exposed",
        "file_count",
        "source_counts_exposed",
        "source_families",
    }:
        return False
    if (
        type(metadata["client_job_id_exposed"]) is not bool
        or type(metadata["client_job_id_matched"]) is not bool
        or type(metadata["file_count_exposed"]) is not bool
        or type(metadata["source_counts_exposed"]) is not bool
    ):
        return False
    if (
        metadata["client_job_id_exposed"] is not True
        or metadata["client_job_id_matched"] is not True
        or metadata["file_count_exposed"] is not True
        or metadata["file_count"] != len(_UPLOAD_MATERIALS)
    ):
        return False
    expected_families = ["history", "ai_persona", "user_profile", "memory_summary"]
    if (
        metadata["source_counts_exposed"] is not True
        or metadata["source_families"] != expected_families
    ):
        return False
    return True


def _decode_private_evidence(raw: bytes, fixture: dict) -> dict:
    try:
        evidence = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ExistingSessionDistillError(
            "finalize", "private_evidence_invalid"
        ) from None
    ground_truth = (
        fixture.get("ground_truth")
        if isinstance(fixture.get("ground_truth"), dict)
        else {}
    )
    expected_fact_ids = [
        str(fact.get("id") or "")
        for fact in (ground_truth.get("facts") or [])
        if isinstance(fact, dict)
    ]
    fixture_sha256 = _sha256_hex(_canonical_json_bytes(fixture))
    valid = (
        isinstance(evidence, dict)
        and set(evidence) == _PRIVATE_EVIDENCE_KEYS
        and evidence.get("schema_version") == 1
        and evidence.get("fixture_sha256") == fixture_sha256
        and evidence.get("expected_fact_ids") == expected_fact_ids
        and isinstance(evidence.get("identity"), dict)
        and isinstance(evidence.get("identity_meta"), dict)
        and set(evidence["identity_meta"])
        == {"days_with_user", "relationship_started_at"}
        and type(evidence["identity_meta"]["days_with_user"]) is int
        and isinstance(evidence["identity_meta"]["relationship_started_at"], str)
        and _relationship_checks(
            (
                fixture.get("relationship")
                if isinstance(fixture.get("relationship"), dict)
                else {}
            ),
            evidence["identity_meta"],
        )
        == (True, True)
        and isinstance(evidence.get("persona_text"), str)
        and isinstance(evidence.get("memories"), list)
        and isinstance(evidence.get("greeting_messages"), list)
        and isinstance(evidence.get("validate"), dict)
        and isinstance(evidence.get("voice_text"), str)
        and isinstance(evidence.get("job"), dict)
        and isinstance(evidence.get("capture_checks"), dict)
        and set(evidence["capture_checks"])
        == {
            "archive_receipts_verified",
            "genesis_upload_metadata_verified",
            "identity_envelope_decrypted",
            "persona_envelope_decrypted",
            "memory_envelopes_decrypted",
            "chat_envelopes_decrypted",
        }
        and all(type(value) is bool for value in evidence["capture_checks"].values())
        and _valid_existing_session_transport(evidence.get("transport"), fixture)
    )
    if not valid:
        raise ExistingSessionDistillError(
            "finalize", "private_evidence_contract_invalid"
        )
    return evidence


def finalize_existing_session_distill_acceptance(
    *,
    private_evidence_path: str,
    semantic_judgment_path: str,
    fixture: dict,
    artifact_dir: str,
) -> dict:
    """Bind an agent judgment to captured plaintext, sanitize, then destroy it."""
    _require_private_path_outside_artifacts(private_evidence_path, artifact_dir)
    try:
        raw_evidence = _read_owner_only_file(
            private_evidence_path,
            stage="finalize",
            code_prefix="private_evidence",
        )
        evidence_sha256 = _sha256_hex(raw_evidence)
        evidence = _decode_private_evidence(raw_evidence, fixture)

        raw_judgment = _read_owner_only_file(
            semantic_judgment_path,
            stage="semantic",
            code_prefix="semantic_judgment",
        )
        try:
            judgment = json.loads(raw_judgment.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ExistingSessionDistillError(
                "semantic", "semantic_judgment_invalid"
            ) from None
        if not isinstance(judgment, dict):
            raise ExistingSessionDistillError("semantic", "semantic_judgment_invalid")
        if judgment.get("evidence_sha256") != evidence_sha256:
            raise ExistingSessionDistillError(
                "semantic", "semantic_judgment_evidence_hash_mismatch"
            )
        semantic = _semantic_judgment_summary(
            judgment,
            evidence["expected_fact_ids"],
            evidence_sha256,
        )
        if not semantic["provided"]:
            raise ExistingSessionDistillError(
                "semantic", "semantic_judgment_contract_invalid"
            )

        report = evaluate_distill_acceptance(
            fixture,
            identity=evidence["identity"],
            identity_meta=evidence["identity_meta"],
            memories=evidence["memories"],
            validate=evidence["validate"],
            persona_text=evidence["persona_text"],
            voice_text=evidence["voice_text"],
            greeting_messages=evidence["greeting_messages"],
            job=evidence["job"],
            semantic_judgment=judgment,
            evidence_sha256=evidence_sha256,
        )
        report["checks"].update(evidence["capture_checks"])
        report["ok"] = all(report["checks"].values())
        report["transport"] = evidence["transport"]
        report["evidence"] = {
            "sha256": evidence_sha256,
            "semantic_judgment_bound": True,
            "private_evidence_deleted": True,
        }
        return report
    finally:
        _delete_private_evidence(private_evidence_path)


_IMPORT_READINESS_DETERMINISTIC_CHECKS = (
    "identity_agent_name",
    "identity_category",
    "identity_dimensions",
    "identity_self_introduction",
    "memory_count_reasonable",
    "ground_truth_recall",
    "no_explicit_contradictions",
    "no_duplicate_memories",
    "relationship_started_at",
    "relationship_days",
    "greeting_non_empty",
    "persona_non_empty",
    "voice_non_empty",
    "validate_passing",
    "privacy_identity_clear",
    "privacy_persona_clear",
    "privacy_self_introduction_clear",
)
_IMPORT_READINESS_CAPTURE_CHECKS = (
    "archive_receipts_verified",
    "genesis_upload_metadata_verified",
    "identity_envelope_decrypted",
    "persona_envelope_decrypted",
    "memory_envelopes_decrypted",
    "chat_envelopes_decrypted",
)


def finalize_existing_session_import_readiness(
    *,
    private_evidence_path: str,
    fixture: dict,
    artifact_dir: str,
) -> dict:
    """Verify deterministic import readiness, sanitize, then destroy plaintext.

    This is deliberately narrower than distill acceptance: it proves that the
    locked fixture reached the expected deterministic and decryptable surfaces,
    but it does not claim that an independent semantic review occurred.
    """
    _require_private_path_outside_artifacts(private_evidence_path, artifact_dir)
    try:
        raw_evidence = _read_owner_only_file(
            private_evidence_path,
            stage="finalize",
            code_prefix="private_evidence",
        )
        evidence_sha256 = _sha256_hex(raw_evidence)
        evidence = _decode_private_evidence(raw_evidence, fixture)
        report = evaluate_distill_acceptance(
            fixture,
            identity=evidence["identity"],
            identity_meta=evidence["identity_meta"],
            memories=evidence["memories"],
            validate=evidence["validate"],
            persona_text=evidence["persona_text"],
            voice_text=evidence["voice_text"],
            greeting_messages=evidence["greeting_messages"],
            job=evidence["job"],
            evidence_sha256=evidence_sha256,
        )
        evaluated_checks = report.get("checks")
        deterministic_check_names = (
            {
                check
                for check in evaluated_checks
                if not check.startswith("semantic_")
            }
            if isinstance(evaluated_checks, dict)
            else set()
        )
        if (
            not isinstance(evaluated_checks, dict)
            or deterministic_check_names
            != set(_IMPORT_READINESS_DETERMINISTIC_CHECKS)
            or any(
                type(evaluated_checks.get(check)) is not bool
                for check in _IMPORT_READINESS_DETERMINISTIC_CHECKS
            )
        ):
            raise ExistingSessionDistillError(
                "finalize", "import_readiness_checks_invalid"
            )
        checks = {
            check: evaluated_checks[check]
            for check in _IMPORT_READINESS_DETERMINISTIC_CHECKS
        }
        checks.update(
            {
                check: evidence["capture_checks"][check]
                for check in _IMPORT_READINESS_CAPTURE_CHECKS
            }
        )
        return {
            "schema_version": 1,
            "kind": "existing_session_import_readiness",
            "ok": all(checks.values()),
            "fixture_sha256": evidence["fixture_sha256"],
            "evidence_sha256": evidence_sha256,
            "checks": checks,
            "private_evidence_deleted": True,
        }
    finally:
        _delete_private_evidence(private_evidence_path)


def _load_manifest_session(
    manifest_path: str, profile_id: str
) -> tuple[str, str, bytes]:
    path = Path(manifest_path)
    try:
        before = path.lstat()
    except OSError:
        raise ExistingSessionDistillError("session", "manifest_unreadable") from None
    if not stat.S_ISREG(before.st_mode):
        raise ExistingSessionDistillError("session", "manifest_not_regular")

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        raise ExistingSessionDistillError("session", "manifest_unreadable") from None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise ExistingSessionDistillError("session", "manifest_not_regular")
        if opened.st_uid != os.geteuid():
            raise ExistingSessionDistillError("session", "manifest_owner_mismatch")
        if stat.S_IMODE(opened.st_mode) != 0o600:
            raise ExistingSessionDistillError("session", "manifest_permissions_invalid")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            raw_manifest = handle.read()
    except ExistingSessionDistillError:
        raise
    except OSError:
        raise ExistingSessionDistillError("session", "manifest_unreadable") from None
    finally:
        if fd >= 0:
            os.close(fd)
    try:
        manifest = json.loads(raw_manifest)
    except json.JSONDecodeError:
        raise ExistingSessionDistillError("session", "manifest_unreadable") from None
    profiles = manifest.get("profiles") if isinstance(manifest, dict) else None
    if not isinstance(profiles, list):
        raise ExistingSessionDistillError("session", "manifest_invalid")
    matches = [
        entry
        for entry in profiles
        if isinstance(entry, dict) and str(entry.get("profile_id") or "") == profile_id
    ]
    if len(matches) != 1:
        raise ExistingSessionDistillError("session", "profile_session_not_unique")
    entry = matches[0]
    try:
        private_key = base64.b64decode(
            str(entry.get("secret_key_b64") or ""), validate=True
        )
    except Exception:
        raise ExistingSessionDistillError(
            "session", "content_private_key_invalid"
        ) from None
    if len(private_key) != 32:
        raise ExistingSessionDistillError("session", "content_private_key_invalid")
    return str(entry.get("api_key") or ""), str(entry.get("user_id") or ""), private_key


def cmd_distill_existing_session(args):
    """Capture phase: run the import once and write owner-only review evidence."""
    try:
        api_key, user_id, private_key = _load_manifest_session(
            args.session_manifest, args.profile_id
        )
        receipt = capture_existing_session_distill_evidence(
            api_url=args.api_url,
            api_key=api_key,
            user_id=user_id,
            content_private_key=private_key,
            fixture=_load_fixture(args.fixture),
            private_evidence_path=args.private_evidence,
            artifact_dir=args.artifact_dir,
            timeout=args.timeout,
            poll=args.poll,
            intro_timeout=args.intro_timeout,
            memory_limit=args.memory_limit,
            client_job_id=args.client_job_id,
        )
    except ExistingSessionDistillError as exc:
        print(json.dumps(exc.as_result(), ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_distill_existing_session_finalize(args):
    """Finalize phase: validate the hash-bound judgment and destroy plaintext."""
    try:
        report = finalize_existing_session_distill_acceptance(
            private_evidence_path=args.private_evidence,
            semantic_judgment_path=args.semantic_judgment,
            fixture=_load_fixture(args.fixture),
            artifact_dir=args.artifact_dir,
        )
    except ExistingSessionDistillError as exc:
        print(json.dumps(exc.as_result(), ensure_ascii=False, sort_keys=True))
        return 1
    rendered = render_distill_acceptance_report(report)
    try:
        if args.report:
            _write_private_report(
                args.report,
                args.artifact_dir,
                rendered
                + "\n```json\n"
                + json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n```\n",
            )
    except ExistingSessionDistillError as exc:
        print(json.dumps(exc.as_result(), ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("ok") else 1


def cmd_distill_acceptance(args):
    import os

    base = args.api_url.rstrip("/")
    fixture = _load_fixture(args.fixture)
    materials = (
        fixture.get("materials") if isinstance(fixture.get("materials"), dict) else {}
    )
    relationship = (
        fixture.get("relationship")
        if isinstance(fixture.get("relationship"), dict)
        else {}
    )
    args.api_key, args.user_id, _enclave_pk, _user_pk = _provision_user(args)
    sk_raw = args._user_sk
    print(
        json.dumps({"provisioned": True, "user_id": args.user_id}, ensure_ascii=False)
    )

    payload = {
        "format": str(materials.get("format") or "auto"),
        "content": str(materials.get("chat_history") or ""),
        "fresh_start": False,
        "client_job_id": "distill_accept_" + os.urandom(8).hex(),
    }
    if relationship.get("relationship_started_at"):
        payload["relationship_started_at"] = str(
            relationship["relationship_started_at"]
        )
    for src_key, payload_key in (
        ("ai_persona", "ai_persona_content"),
        ("personal_profile", "personal_profile_content"),
        ("memory_summary", "memory_summary_content"),
    ):
        value = materials.get(src_key)
        if value:
            payload[payload_key] = str(value)

    s, b = _http(
        "POST", f"{base}/v1/genesis/imports/plaintext", args.api_key, json_body=payload
    )
    job_id = (b.get("job") or {}).get("job_id") or b.get("job_id")
    if s >= 400 or not job_id:
        print(
            json.dumps(
                {"ok": False, "step": "upload", "status": s, "body": b},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps({"upload": "ok", "job_id": job_id}, ensure_ascii=False))

    deadline = time.time() + args.timeout
    job_body: dict = {}
    job: dict = {}
    while time.time() < deadline:
        try:
            _s, job_body = _http(
                "GET", f"{base}/v1/genesis/imports/{job_id}", args.api_key
            )
        except SystemExit:
            time.sleep(args.poll)
            continue
        job = job_body.get("job") if isinstance(job_body.get("job"), dict) else {}
        if str(job.get("status") or "").lower() in ("done", "failed"):
            break
        time.sleep(args.poll)
    if str(job.get("status") or "").lower() != "done":
        print(
            json.dumps(
                {"ok": False, "step": "distill", "job_id": job_id, "job": job},
                ensure_ascii=False,
            )
        )
        return 1

    identity_plain: dict = {}
    identity_meta: dict = {}
    chat: dict = {}
    intro_deadline = time.time() + args.intro_timeout
    while time.time() < intro_deadline:
        _s, identity_body = _http("GET", f"{base}/v1/identity/get", args.api_key)
        identity_payload = identity_body.get("identity") or {}
        identity_meta = (
            dict(identity_payload) if isinstance(identity_payload, dict) else {}
        )
        if isinstance(identity_payload, dict) and identity_payload.get("body_ct"):
            identity_plain = json.loads(
                _decrypt_envelope_user(identity_payload, sk_raw)
            )
        elif isinstance(identity_payload, dict):
            identity_plain = identity_payload
        _s, chat = _http("GET", f"{base}/v1/chat/history?limit=20", args.api_key)
        greeting_ok = any(
            str(m.get("content") or m.get("text") or "").strip()
            and str(m.get("role") or "").lower() not in {"", "user"}
            for m in (chat.get("messages") or [])
            if isinstance(m, dict)
        )
        if str(identity_plain.get("self_introduction") or "").strip() and greeting_ok:
            break
        time.sleep(args.poll)

    _s, memory_body = _http(
        "GET", f"{base}/v1/memory/list?limit={args.memory_limit}", args.api_key
    )
    memories = _decrypt_memory_rows(memory_body.get("moments") or [], sk_raw)
    _s, validate = _http("GET", f"{base}/v1/onboarding/validate", args.api_key)

    persona_text = ""
    persona_env = (job_body.get("persona") or {}).get("content_envelope") or {}
    if persona_env:
        try:
            persona_text = _decrypt_envelope_user(persona_env, sk_raw)
        except Exception as e:  # noqa: BLE001
            persona_text = f"<persona decrypt failed: {e}>"
    report = evaluate_distill_acceptance(
        fixture,
        identity=identity_plain,
        identity_meta=identity_meta,
        memories=memories,
        validate=validate,
        persona_text=persona_text,
        voice_text="",
        greeting_messages=chat.get("messages") or [],
        job={**job, "job_id": job_id},
    )
    rendered = render_distill_acceptance_report(report)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(
            rendered
            + "\n```json\n"
            + json.dumps(report, ensure_ascii=False, indent=2)
            + "\n```\n",
            encoding="utf-8",
        )
    print(rendered)
    print(json.dumps(report, ensure_ascii=False))
    if not getattr(args, "no_cleanup", False):
        try:
            _http("DELETE", f"{base}/v1/model_api/delete", args.api_key)
        except Exception:  # noqa: BLE001
            pass
    return 0 if report.get("ok") else 1


def cmd_acceptance(args):
    """Per-source identity acceptance: upload 4 materials in one plaintext request
    (history + ai_persona card WITH a name + memory + user_profile WITH a firewall
    needle), poll to done, DECRYPT the identity card + persona with the user's key,
    and assert: agent_name present (== expected), dimensions populated (each with a
    description), days_with_user > 0, the user_profile needle NEVER leaks into
    identity/persona (firewall), memories written."""
    import os

    base = args.api_url.rstrip("/")
    args.api_key, args.user_id, _enclave_pk, _user_pk = _provision_user(args)
    sk_raw = args._user_sk
    print(
        json.dumps({"provisioned": True, "user_id": args.user_id}, ensure_ascii=False)
    )

    payload = {
        "format": "auto",
        "content": Path(args.transcript).read_text(encoding="utf-8"),
        "fresh_start": False,
        "relationship_started_at": args.relationship_started_at,
        "client_job_id": "accept_" + os.urandom(8).hex(),
    }
    if args.ai_persona:
        payload["ai_persona_content"] = Path(args.ai_persona).read_text(
            encoding="utf-8"
        )
    if args.personal_profile:
        payload["personal_profile_content"] = Path(args.personal_profile).read_text(
            encoding="utf-8"
        )
    if args.memory_summary:
        payload["memory_summary_content"] = Path(args.memory_summary).read_text(
            encoding="utf-8"
        )
    s, b = _http(
        "POST", f"{base}/v1/genesis/imports/plaintext", args.api_key, json_body=payload
    )
    job_id = (b.get("job") or {}).get("job_id") or b.get("job_id")
    if s >= 400 or not job_id:
        print(
            json.dumps(
                {"ok": False, "step": "upload", "status": s, "body": b},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps({"upload": "ok", "job_id": job_id}, ensure_ascii=False))

    deadline = time.time() + args.timeout
    job, jb = {}, {}
    while time.time() < deadline:
        try:
            _s, jb = _http("GET", f"{base}/v1/genesis/imports/{job_id}", args.api_key)
        except SystemExit:
            time.sleep(args.poll)
            continue  # flaky proxy — keep polling, don't abort the run
        job = jb.get("job") or {}
        if str(job.get("status") or "").lower() in ("done", "failed"):
            break
        time.sleep(args.poll)
    if str(job.get("status")) != "done":
        print(
            json.dumps(
                {
                    "ok": False,
                    "step": "distill",
                    "status": job.get("status"),
                    "error": job.get("error"),
                },
                ensure_ascii=False,
            )
        )
        return 1

    _s, idy = _http("GET", f"{base}/v1/identity/get", args.api_key)
    ident = idy.get("identity") or {}
    try:
        identity_body = json.loads(_decrypt_envelope_user(ident, sk_raw))
    except Exception as e:  # noqa: BLE001
        print(
            json.dumps(
                {"ok": False, "step": "identity_decrypt", "error": str(e)},
                ensure_ascii=False,
            )
        )
        return 1
    persona_text = ""
    persona_env = (jb.get("persona") or {}).get("content_envelope") or {}
    try:
        if persona_env:
            persona_text = _decrypt_envelope_user(persona_env, sk_raw)
    except Exception as e:  # noqa: BLE001
        persona_text = f"<persona decrypt failed: {e}>"

    agent_name = str(identity_body.get("agent_name") or "")
    dims = (
        identity_body.get("dimensions")
        if isinstance(identity_body.get("dimensions"), list)
        else []
    )
    days = ident.get("days_with_user")
    category = str(identity_body.get("category") or "")
    needle = args.firewall_needle
    identity_blob = json.dumps(identity_body, ensure_ascii=False)
    checks = {
        "agent_name_present": bool(agent_name.strip()),
        "agent_name_expected": (
            (args.expect_name in agent_name) if args.expect_name else None
        ),
        "dimensions_present": len(dims) >= 1,
        "dimensions_have_descriptions": bool(dims)
        and all(
            isinstance(d, dict) and str(d.get("description") or "").strip()
            for d in dims
        ),
        # Home 「性格」 tile = identity.category. With dims present it must be non-empty
        # (A: LLM-distilled; B: deterministic top-2-dim fallback). Empty renders as "—".
        "category_present_when_dims": (bool(category.strip()) if dims else None),
        "days_gt_0": isinstance(days, int) and days > 0,
        "firewall_identity": (needle not in identity_blob) if needle else None,
        "firewall_persona": (needle not in persona_text) if needle else None,
        "memories_written": int(job.get("memory_action_count") or 0) > 0,
    }
    if getattr(args, "check_introduction", False):
        # §六 7.D: after genesis done, host-all autodiscover spawns the agent, which
        # should ONCE write its self_introduction (identity.profile_patch) and post a
        # first greeting. Poll for both within the intro window.
        intro_self, greeting = "", False
        intro_deadline = time.time() + args.intro_timeout
        while time.time() < intro_deadline:
            try:
                _s, idy2 = _http("GET", f"{base}/v1/identity/get", args.api_key)
                ib2 = json.loads(
                    _decrypt_envelope_user(idy2.get("identity") or {}, sk_raw)
                )
                intro_self = str(ib2.get("self_introduction") or "")
            except SystemExit:
                time.sleep(args.poll)
                continue  # flaky proxy — keep polling
            except Exception:  # noqa: BLE001
                pass
            try:
                _s, ch = _http("GET", f"{base}/v1/chat/history?limit=12", args.api_key)
                greeting = any(
                    str(m.get("role") or "").lower() not in ("", "user")
                    for m in (ch.get("messages") or [])
                )
            except SystemExit:
                pass  # flaky proxy — retry next iteration
            if intro_self.strip() and greeting:
                break
            time.sleep(args.poll)
        identity_body["self_introduction"] = intro_self
        checks["introduction_self_intro_written"] = bool(intro_self.strip())
        checks["introduction_greeting_posted"] = greeting
    failed = [k for k, v in checks.items() if v is False]
    out = {
        "agent_name": agent_name,
        "dimensions": [
            {
                "name": d.get("name"),
                "value": d.get("value"),
                "has_desc": bool(d.get("description")),
            }
            for d in dims
            if isinstance(d, dict)
        ],
        "self_introduction": str(identity_body.get("self_introduction") or "")[:60],
        "category": category,
        "days_with_user": days,
        "memory_action_count": job.get("memory_action_count"),
        "checks": checks,
        "ok": not failed,
        "failed": failed,
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if not getattr(args, "no_cleanup", False):
        # Stop the throwaway from lingering in host-all discovery: each genesis-done +
        # model_api-ok user keeps a resident consumer, and accumulated test users contend
        # for agent-runner resources (which can starve a real account's introduction).
        # Dropping model_api removes it from autodiscover. Best-effort.
        try:
            _http("DELETE", f"{base}/v1/model_api/delete", args.api_key)
        except Exception:  # noqa: BLE001
            pass
    return 0 if not failed else 1


def main():
    p = argparse.ArgumentParser(
        prog="genesis_e2e", description="Genesis e2e harness (test CVM)."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser(
        "upload",
        help="(optionally register a user, then) seal+upload chunks + finalize",
    )
    up.add_argument("--api-url", required=True)
    up.add_argument(
        "--register",
        action="store_true",
        help="self-provision a throwaway user (register + model_api/setup + whoami); "
        "provider key from GENESIS_E2E_PROVIDER_API_KEY env",
    )
    up.add_argument(
        "--provider",
        default="",
        help="with --register: model provider (e.g. anthropic/openai)",
    )
    up.add_argument("--model", default="", help="with --register: model id")
    up.add_argument(
        "--base-url", default="", help="with --register: optional provider base_url"
    )
    up.add_argument(
        "--api-key", default="", help="existing user api_key (omit with --register)"
    )
    up.add_argument(
        "--user-id", default="", help="owner_user_id (omit with --register)"
    )
    up.add_argument(
        "--transcript", required=True, help="path to a plaintext test transcript"
    )
    up.add_argument(
        "--enclave-pk-hex",
        default="",
        help="enclave content pubkey hex (non-register; else fetch attestation)",
    )
    up.add_argument(
        "--attestation-url", default="", help="defaults to <api-url>/attestation"
    )
    up.add_argument("--source-kind", default="history")
    up.add_argument("--chunk-size", type=int, default=12000)
    up.set_defaults(func=cmd_upload)

    vf = sub.add_parser("verify", help="poll job to done + privacy spot-check")
    vf.add_argument("--api-url", required=True)
    vf.add_argument("--api-key", required=True)
    vf.add_argument("--job-id", required=True)
    vf.add_argument("--timeout", type=float, default=600)
    vf.add_argument("--poll", type=float, default=10)
    vf.add_argument(
        "--privacy-needle",
        default="",
        help="comma-separated distinctive transcript fragments that MUST NOT "
        "appear in the status payload (real leak check, e.g. '蛋子,西湖')",
    )
    vf.set_defaults(func=cmd_verify)

    upp = sub.add_parser(
        "upload-plaintext",
        help="one-shot plaintext genesis ingest (POST /v1/genesis/imports/plaintext); then `verify`",
    )
    upp.add_argument("--api-url", required=True)
    upp.add_argument(
        "--register",
        action="store_true",
        help="self-provision a throwaway user (register + model_api/setup + whoami)",
    )
    upp.add_argument("--provider", default="")
    upp.add_argument("--model", default="")
    upp.add_argument("--base-url", default="")
    upp.add_argument("--api-key", default="")
    upp.add_argument("--user-id", default="")
    upp.add_argument("--transcript", required=True, help="plaintext history file")
    upp.add_argument(
        "--ai-persona", default="", help="optional ai_persona/character file"
    )
    upp.add_argument(
        "--personal-profile", default="", help="optional personal_profile file"
    )
    upp.add_argument(
        "--memory-summary", default="", help="optional memory_summary/support file"
    )
    upp.add_argument("--client-job-id", default="")
    upp.set_defaults(func=cmd_upload_plaintext)

    ac = sub.add_parser(
        "acceptance",
        help="per-source identity acceptance: 4 materials -> done -> decrypt identity -> assert",
    )
    ac.add_argument("--api-url", required=True)
    ac.add_argument("--register", action="store_true", default=True)
    ac.add_argument("--provider", default="anthropic")
    ac.add_argument("--model", default="claude-haiku-4-5-20251001")
    ac.add_argument("--base-url", default="")
    ac.add_argument("--transcript", required=True, help="history file")
    ac.add_argument("--ai-persona", default="", help="角色卡 (ideally with a name)")
    ac.add_argument("--personal-profile", default="", help="个人档案")
    ac.add_argument("--memory-summary", default="", help="长期记忆")
    ac.add_argument(
        "--relationship-started-at",
        default="",
        help="YYYY-MM-DD (tests days_with_user)",
    )
    ac.add_argument(
        "--expect-name", default="", help="assert agent_name contains this (e.g. 小满)"
    )
    ac.add_argument(
        "--firewall-needle",
        default="",
        help="user_profile string that must NOT leak into identity/persona (e.g. 赵铁柱)",
    )
    ac.add_argument("--timeout", type=float, default=900)
    ac.add_argument("--poll", type=float, default=10)
    ac.add_argument(
        "--check-introduction",
        action="store_true",
        help="§六 7.D: after genesis done, wait for the spawned agent to write "
        "self_introduction + post a first greeting (needs agent-runner host-all)",
    )
    ac.add_argument(
        "--intro-timeout",
        type=float,
        default=180,
        help="seconds to wait for the 7.D introduction (default 180)",
    )
    ac.add_argument(
        "--no-cleanup",
        action="store_true",
        help="keep the throwaway's model_api (default: DELETE it after the run "
        "so it stops polluting host-all autodiscover)",
    )
    ac.set_defaults(func=cmd_acceptance)

    da = sub.add_parser(
        "distill-acceptance",
        help="live plaintext genesis distillation acceptance with ground-truth fact scoring",
    )
    da.add_argument("--api-url", required=True)
    da.add_argument("--provider", default="anthropic")
    da.add_argument("--model", default="claude-haiku-4-5-20251001")
    da.add_argument("--base-url", default="")
    da.add_argument(
        "--fixture",
        required=True,
        help="JSON fixture with materials, persona expectations, relationship, and ground_truth facts",
    )
    da.add_argument("--timeout", type=float, default=900)
    da.add_argument("--poll", type=float, default=10)
    da.add_argument(
        "--intro-timeout",
        type=float,
        default=180,
        help="seconds to wait after job done for self_introduction + greeting",
    )
    da.add_argument("--memory-limit", type=int, default=100)
    da.add_argument("--report", default="", help="optional markdown report path")
    da.add_argument(
        "--no-cleanup",
        action="store_true",
        help="keep the throwaway's model_api after the run",
    )
    da.set_defaults(func=cmd_distill_acceptance)

    existing = sub.add_parser(
        "distill-existing-session",
        help="run distillation acceptance on one already-provisioned manifest profile",
    )
    existing.add_argument("--api-url", required=True)
    existing.add_argument(
        "--session-manifest",
        required=True,
        help="0600 qualification manifest; credentials are never printed",
    )
    existing.add_argument("--profile-id", required=True)
    existing.add_argument(
        "--fixture",
        required=True,
        help="JSON fixture with four materials and locked acceptance expectations",
    )
    existing.add_argument(
        "--private-evidence",
        required=True,
        help=(
            "absolute owner-only capture path outside QA_ARTIFACT_DIR; Codex reads it once"
        ),
    )
    existing.add_argument(
        "--artifact-dir",
        required=True,
        help="public QA_ARTIFACT_DIR, used to forbid plaintext capture beneath it",
    )
    existing.add_argument("--timeout", type=float, default=900)
    existing.add_argument("--poll", type=float, default=10)
    existing.add_argument("--intro-timeout", type=float, default=180)
    existing.add_argument("--memory-limit", type=int, default=100)
    existing.add_argument("--client-job-id", default="")
    existing.set_defaults(func=cmd_distill_existing_session)

    finalize = sub.add_parser(
        "distill-existing-session-finalize",
        help="bind Codex judgment to captured evidence, sanitize, and delete plaintext",
    )
    finalize.add_argument("--fixture", required=True)
    finalize.add_argument("--private-evidence", required=True)
    finalize.add_argument("--artifact-dir", required=True)
    finalize.add_argument(
        "--semantic-judgment",
        required=True,
        help="owner-only bounded JSON whose evidence_sha256 matches the capture",
    )
    finalize.add_argument(
        "--report",
        default="",
        help=(
            "optional owner-only sanitized markdown path outside QA_ARTIFACT_DIR; "
            "use private QA_WORK_ROOT or TMPDIR"
        ),
    )
    finalize.set_defaults(func=cmd_distill_existing_session_finalize)

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
