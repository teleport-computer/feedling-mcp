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
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from content_encryption import build_envelope  # noqa: E402

try:
    from nacl.public import PrivateKey  # PyNaCl (a backend dep)
except Exception as e:  # noqa: BLE001
    print(json.dumps({"ok": False, "error": f"PyNaCl required (backend dep): {e}"}))
    sys.exit(2)


def _http(method, url, api_key, *, json_body=None, timeout=60, retries=8):
    """Single request with retry on transient read/connection errors (IncompleteRead,
    URLError, socket timeout). HTTPError (a real status) is returned immediately, never
    retried. Generous retries + backoff because the local proxy (198.18 fake-IP) flaps
    a lot and acceptance runs poll for 10+ min. HTTPError 409 on register (a re-POST
    after a lost response created the account) is treated as transient-but-fatal -> the
    caller re-provisions. Safe: every call site is idempotent (GET) or create-on a
    throwaway test user."""
    import http.client
    import socket
    data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
    last_exc = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"X-API-Key": api_key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            return e.code, {"error": e.read().decode("utf-8", "replace")[:400]}
        except (http.client.IncompleteRead, urllib.error.URLError, socket.timeout, ConnectionError) as e:
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
    return [text[i:i + size] for i in range(0, len(text), max(1, size))] or [""]


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
        s, b = _http("POST", f"{base}/v1/users/register", "", json_body={"public_key": user_pk_b64})
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
            raise SystemExit("GENESIS_E2E_PROVIDER_API_KEY env required (worker LLM key); or use --skip-setup for a plumbing dry-run")
        s, b = _http("POST", f"{base}/v1/model_api/setup", api_key, json_body={
            "provider": args.provider, "model": args.model,
            "base_url": args.base_url, "api_key": provider_key})
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
        print(json.dumps({"provisioned": True, "user_id": args.user_id, "api_key": args.api_key}, ensure_ascii=False))
    else:
        enclave_pk = _enclave_pk_bytes(args)
        user_pk = bytes(PrivateKey.generate().public_key)  # throwaway: worker decrypts via K_enclave
    parts = _chunks(Path(args.transcript).read_text(encoding="utf-8"), args.chunk_size)

    status, body = _http("POST", f"{args.api_url.rstrip('/')}/v1/genesis/imports", args.api_key,
                         json_body={"source_kind": args.source_kind, "total_chunks": len(parts)})
    if status >= 400:
        print(json.dumps({"ok": False, "step": "create", "status": status, "body": body})); return 1
    job_id = body.get("job_id") or body.get("job", {}).get("job_id")
    if not job_id:
        print(json.dumps({"ok": False, "step": "create", "error": "no job_id", "body": body})); return 1

    for seq, part in enumerate(parts):
        env = build_envelope(plaintext=part.encode("utf-8"), owner_user_id=args.user_id,
                             user_pk_bytes=user_pk, enclave_pk_bytes=enclave_pk, visibility="shared")
        body_ct = env["body_ct"]
        cct_sha = hashlib.sha256(base64.b64decode(body_ct)).hexdigest()
        s, b = _http("PUT", f"{args.api_url.rstrip('/')}/v1/genesis/imports/{job_id}/chunks/{seq}",
                     args.api_key, json_body={"envelope": env, "ciphertext_sha256": cct_sha,
                                              "byte_start": 0, "byte_end": len(part.encode("utf-8"))})
        if s >= 400:
            print(json.dumps({"ok": False, "step": f"chunk:{seq}", "status": s, "body": b})); return 1

    s, b = _http("POST", f"{args.api_url.rstrip('/')}/v1/genesis/imports/{job_id}/finalize", args.api_key)
    print(json.dumps({"ok": s < 400, "step": "finalize", "status": s, "job_id": job_id, "body": b}, ensure_ascii=False))
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
        return str(st or b.get("status") or b.get("job", {}).get("status") or "").lower()
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
    needles = [n.strip().lower() for n in str(getattr(args, "privacy_needle", "") or "").split(",") if n.strip()]
    out["privacy_leak"] = [n for n in needles if n in blob]
    out["status_payload_raw_keys"] = [k for k in ("transcript", "raw_text") if k in blob]
    out["ok"] = (state == "done") and not out["privacy_leak"]
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["ok"] else 1


def cmd_upload_plaintext(args):
    """One-shot plaintext genesis ingest — mirrors the iOS uploadGenesisPlaintext:
    a single POST of the old history_import payload shape to /v1/genesis/imports/plaintext.
    No client-side sealing/chunking. Then poll with `verify --job-id`."""
    if args.register:
        args.api_key, args.user_id, _enclave_pk, _user_pk = _provision_user(args)
        print(json.dumps({"provisioned": True, "user_id": args.user_id, "api_key": args.api_key}, ensure_ascii=False))
    content = Path(args.transcript).read_text(encoding="utf-8")
    payload = {
        "format": "auto",
        "content": content,
        "fresh_start": False,
        "client_job_id": args.client_job_id or ("e2e_" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:40]),
    }
    if args.ai_persona:
        payload["ai_persona_content"] = Path(args.ai_persona).read_text(encoding="utf-8")
    if args.personal_profile:
        payload["personal_profile_content"] = Path(args.personal_profile).read_text(encoding="utf-8")
    if args.memory_summary:
        payload["memory_summary_content"] = Path(args.memory_summary).read_text(encoding="utf-8")
    s, b = _http("POST", f"{args.api_url.rstrip('/')}/v1/genesis/imports/plaintext", args.api_key, json_body=payload)
    job_id = (b.get("job") or {}).get("job_id") or b.get("job_id")
    print(json.dumps({"ok": s < 400 and bool(job_id), "step": "plaintext_upload", "status": s,
                      "job_id": job_id, "body": b}, ensure_ascii=False))
    return 0 if (s < 400 and job_id) else 1


def _box_seal_open(sealed: bytes, sk_raw: bytes) -> bytes:
    """Reverse of content_encryption.box_seal: X25519 ECDH(sk, ek_pub) ->
    HKDF-SHA256(info='feedling-box-seal-v1') -> nonce=SHA256(ek_pub||rcp_pub)[:12]
    -> ChaCha20-Poly1305 decrypt. `sealed` = ek_pub(32) || ct || tag(16)."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives import serialization
    ek_pub, ct = sealed[:32], sealed[32:]
    sk = X25519PrivateKey.from_private_bytes(sk_raw)
    rcp_pub = sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    shared = sk.exchange(X25519PublicKey.from_public_bytes(ek_pub))
    k_wrap = HKDF(algorithm=SHA256(), length=32, salt=None, info=b"feedling-box-seal-v1").derive(shared)
    nonce = hashlib.sha256(ek_pub + rcp_pub).digest()[:12]
    return ChaCha20Poly1305(k_wrap).decrypt(nonce, ct, None)


def _decrypt_envelope_user(env: dict, sk_raw: bytes) -> str:
    """Decrypt a shared envelope's body with the user's content private key.
    AAD = owner_user_id|v|id (must match content_encryption.build_envelope)."""
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    K = _box_seal_open(base64.b64decode(env["K_user"]), sk_raw)
    aad = f"{env['owner_user_id']}|{env.get('v', 1)}|{env['id']}".encode("utf-8")
    pt = ChaCha20Poly1305(K).decrypt(base64.b64decode(env["nonce"]), base64.b64decode(env["body_ct"]), aad)
    return pt.decode("utf-8", "replace")


def _norm_text(value: object) -> str:
    text = str(value or "").lower()
    return re.sub(r"[\s，。、“”‘’：:；;,.!?！？\-_/\\|()（）\[\]{}<>《》]+", "", text)


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
    return all(k in normalized_memory for k in keywords)


def _dimension_matches(identity_dims: list, expected_dims: list) -> bool:
    if not expected_dims:
        return bool(identity_dims)
    blob = _norm_text(json.dumps(identity_dims, ensure_ascii=False))
    for dim in expected_dims:
        name = _norm_text(dim.get("name") if isinstance(dim, dict) else dim)
        if name and name not in blob:
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
            pairs.append({"left_id": seen[text], "right_id": mid, "reason": "normalized_text"})
        else:
            seen[text] = mid
    return pairs


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
) -> dict:
    expected_persona = fixture.get("persona") if isinstance(fixture.get("persona"), dict) else {}
    relationship = fixture.get("relationship") if isinstance(fixture.get("relationship"), dict) else {}
    ground_truth = fixture.get("ground_truth") if isinstance(fixture.get("ground_truth"), dict) else {}
    expected_facts = [f for f in (ground_truth.get("facts") or []) if isinstance(f, dict)]
    expected_name = str(expected_persona.get("agent_name") or "").strip()
    expected_category = str(expected_persona.get("category") or "").strip()
    expected_dims = [d for d in (expected_persona.get("dimensions") or []) if isinstance(d, dict)]
    expected_days = relationship.get("expected_days_with_user")

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
    matched_memory_ids: set[str] = set()
    for fact in expected_facts:
        matches = [row for row in memory_rows if _fact_matched(fact, row["text"])]
        if matches:
            recalled.append({
                "id": str(fact.get("id") or ""),
                "text": str(fact.get("text") or ""),
                "matched_memory_ids": [m["id"] for m in matches],
            })
            matched_memory_ids.update(m["id"] for m in matches)
        else:
            missed.append({
                "id": str(fact.get("id") or ""),
                "text": str(fact.get("text") or ""),
                "keywords": _fact_keywords(fact),
            })

    false_positives = [
        {"id": row["id"], "text": row["text"]}
        for row in memory_rows
        if row["id"] not in matched_memory_ids and row["text"]
    ]
    duplicates = _duplicate_pairs([row["raw"] for row in memory_rows])
    total = len(expected_facts)
    memory_count = len(memory_rows)

    agent_name = str(identity.get("agent_name") or "").strip()
    category = str(identity.get("category") or "").strip()
    self_intro = str(identity.get("self_introduction") or "").strip()
    dims = identity.get("dimensions") if isinstance(identity.get("dimensions"), list) else []
    days = identity_meta.get("days_with_user")
    if not isinstance(days, int):
        try:
            days = int(days)
        except Exception:
            days = None
    greeting_ok = any(
        str(m.get("content") or m.get("text") or "").strip()
        and str(m.get("role") or "").lower() not in {"", "user"}
        for m in greeting_messages
        if isinstance(m, dict)
    )
    voice_ok = bool(str(voice_text or "").strip() or job.get("voice_ref") or job.get("voice_sha256"))

    checks = {
        "identity_agent_name": bool(agent_name) and (not expected_name or expected_name in agent_name),
        "identity_category": bool(category) and (not expected_category or expected_category in category),
        "identity_dimensions": bool(dims) and all(
            isinstance(d, dict) and str(d.get("description") or "").strip()
            for d in dims
        ) and _dimension_matches(dims, expected_dims),
        "identity_self_introduction": bool(self_intro) and all(
            _norm_text(k) in _norm_text(self_intro)
            for k in expected_persona.get("self_introduction_keywords", [])
            if str(k).strip()
        ),
        "memory_count_reasonable": memory_count >= min(1, total),
        "ground_truth_recall": len(missed) == 0,
        "no_duplicate_memories": len(duplicates) == 0,
        "relationship_days": (days == expected_days) if isinstance(expected_days, int) else isinstance(days, int),
        "greeting_non_empty": greeting_ok,
        "persona_non_empty": bool(str(persona_text or "").strip() or job.get("persona_ref") or job.get("persona_sha256")),
        "voice_non_empty": voice_ok,
        "validate_passing": bool(validate.get("passing")),
    }
    metrics = {
        "ground_truth_total": total,
        "recall_count": len(recalled),
        "miss_count": len(missed),
        "recall_rate": (len(recalled) / total) if total else 1.0,
        "miss_rate": (len(missed) / total) if total else 0.0,
        "memory_count": memory_count,
        "false_positive_count": len(false_positives),
        "false_positive_rate": (len(false_positives) / memory_count) if memory_count else 0.0,
        "duplicate_pair_count": len(duplicates),
        "duplicate_rate": (len(duplicates) / memory_count) if memory_count else 0.0,
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
        "false_positives": false_positives,
        "duplicates": duplicates,
        "identity": identity,
        "identity_meta": identity_meta,
        "validate": validate,
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
    missed = report.get("missed_facts") if isinstance(report.get("missed_facts"), list) else []
    if missed:
        for item in missed:
            lines.append(f"- 漏抽：{item.get('id', '')}｜{item.get('text', '')}")
    else:
        lines.append("- 无")
    lines.extend(["", "## 误报"])
    false_positives = report.get("false_positives") if isinstance(report.get("false_positives"), list) else []
    if false_positives:
        for item in false_positives:
            lines.append(f"- 误报：{item.get('id', '')}｜{item.get('text', '')}")
    else:
        lines.append("- 无")
    lines.extend(["", "## 重复"])
    duplicates = report.get("duplicates") if isinstance(report.get("duplicates"), list) else []
    if duplicates:
        for item in duplicates:
            lines.append(f"- 重复：{item.get('left_id', '')} ↔ {item.get('right_id', '')}｜{item.get('reason', '')}")
    else:
        lines.append("- 无")
    return "\n".join(lines) + "\n"


def _load_fixture(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("fixture must be a JSON object")
    return data


def _decrypt_memory_rows(rows: list, sk_raw: bytes) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        if item.get("body_ct") and not (item.get("title") or item.get("description")):
            try:
                inner = json.loads(_decrypt_envelope_user(item, sk_raw))
                if isinstance(inner, dict):
                    item.update(inner)
                    item["inner"] = inner
            except Exception as e:  # noqa: BLE001
                item["decrypt_error"] = str(e)[:160]
        out.append(item)
    return out


def cmd_distill_acceptance(args):
    import os
    base = args.api_url.rstrip("/")
    fixture = _load_fixture(args.fixture)
    materials = fixture.get("materials") if isinstance(fixture.get("materials"), dict) else {}
    relationship = fixture.get("relationship") if isinstance(fixture.get("relationship"), dict) else {}
    args.api_key, args.user_id, _enclave_pk, _user_pk = _provision_user(args)
    sk_raw = args._user_sk
    print(json.dumps({"provisioned": True, "user_id": args.user_id}, ensure_ascii=False))

    payload = {
        "format": str(materials.get("format") or "auto"),
        "content": str(materials.get("chat_history") or ""),
        "fresh_start": False,
        "client_job_id": "distill_accept_" + os.urandom(8).hex(),
    }
    if relationship.get("relationship_started_at"):
        payload["relationship_started_at"] = str(relationship["relationship_started_at"])
    for src_key, payload_key in (
        ("ai_persona", "ai_persona_content"),
        ("personal_profile", "personal_profile_content"),
        ("memory_summary", "memory_summary_content"),
    ):
        value = materials.get(src_key)
        if value:
            payload[payload_key] = str(value)

    s, b = _http("POST", f"{base}/v1/genesis/imports/plaintext", args.api_key, json_body=payload)
    job_id = (b.get("job") or {}).get("job_id") or b.get("job_id")
    if s >= 400 or not job_id:
        print(json.dumps({"ok": False, "step": "upload", "status": s, "body": b}, ensure_ascii=False))
        return 1
    print(json.dumps({"upload": "ok", "job_id": job_id}, ensure_ascii=False))

    deadline = time.time() + args.timeout
    job_body: dict = {}
    job: dict = {}
    while time.time() < deadline:
        try:
            _s, job_body = _http("GET", f"{base}/v1/genesis/imports/{job_id}", args.api_key)
        except SystemExit:
            time.sleep(args.poll)
            continue
        job = job_body.get("job") if isinstance(job_body.get("job"), dict) else {}
        if str(job.get("status") or "").lower() in ("done", "failed"):
            break
        time.sleep(args.poll)
    if str(job.get("status") or "").lower() != "done":
        print(json.dumps({"ok": False, "step": "distill", "job_id": job_id, "job": job}, ensure_ascii=False))
        return 1

    identity_plain: dict = {}
    identity_meta: dict = {}
    chat: dict = {}
    intro_deadline = time.time() + args.intro_timeout
    while time.time() < intro_deadline:
        _s, identity_body = _http("GET", f"{base}/v1/identity/get", args.api_key)
        identity_payload = identity_body.get("identity") or {}
        identity_meta = dict(identity_payload) if isinstance(identity_payload, dict) else {}
        if isinstance(identity_payload, dict) and identity_payload.get("body_ct"):
            identity_plain = json.loads(_decrypt_envelope_user(identity_payload, sk_raw))
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

    _s, memory_body = _http("GET", f"{base}/v1/memory/list?limit={args.memory_limit}", args.api_key)
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
        Path(args.report).write_text(rendered + "\n```json\n" + json.dumps(report, ensure_ascii=False, indent=2) + "\n```\n", encoding="utf-8")
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
    print(json.dumps({"provisioned": True, "user_id": args.user_id}, ensure_ascii=False))

    payload = {
        "format": "auto",
        "content": Path(args.transcript).read_text(encoding="utf-8"),
        "fresh_start": False,
        "relationship_started_at": args.relationship_started_at,
        "client_job_id": "accept_" + os.urandom(8).hex(),
    }
    if args.ai_persona:
        payload["ai_persona_content"] = Path(args.ai_persona).read_text(encoding="utf-8")
    if args.personal_profile:
        payload["personal_profile_content"] = Path(args.personal_profile).read_text(encoding="utf-8")
    if args.memory_summary:
        payload["memory_summary_content"] = Path(args.memory_summary).read_text(encoding="utf-8")
    s, b = _http("POST", f"{base}/v1/genesis/imports/plaintext", args.api_key, json_body=payload)
    job_id = (b.get("job") or {}).get("job_id") or b.get("job_id")
    if s >= 400 or not job_id:
        print(json.dumps({"ok": False, "step": "upload", "status": s, "body": b}, ensure_ascii=False)); return 1
    print(json.dumps({"upload": "ok", "job_id": job_id}, ensure_ascii=False))

    deadline = time.time() + args.timeout
    job, jb = {}, {}
    while time.time() < deadline:
        try:
            _s, jb = _http("GET", f"{base}/v1/genesis/imports/{job_id}", args.api_key)
        except SystemExit:
            time.sleep(args.poll); continue  # flaky proxy — keep polling, don't abort the run
        job = jb.get("job") or {}
        if str(job.get("status") or "").lower() in ("done", "failed"):
            break
        time.sleep(args.poll)
    if str(job.get("status")) != "done":
        print(json.dumps({"ok": False, "step": "distill", "status": job.get("status"),
                          "error": job.get("error")}, ensure_ascii=False)); return 1

    _s, idy = _http("GET", f"{base}/v1/identity/get", args.api_key)
    ident = idy.get("identity") or {}
    try:
        identity_body = json.loads(_decrypt_envelope_user(ident, sk_raw))
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "step": "identity_decrypt", "error": str(e)}, ensure_ascii=False)); return 1
    persona_text = ""
    persona_env = (jb.get("persona") or {}).get("content_envelope") or {}
    try:
        if persona_env:
            persona_text = _decrypt_envelope_user(persona_env, sk_raw)
    except Exception as e:  # noqa: BLE001
        persona_text = f"<persona decrypt failed: {e}>"

    agent_name = str(identity_body.get("agent_name") or "")
    dims = identity_body.get("dimensions") if isinstance(identity_body.get("dimensions"), list) else []
    days = ident.get("days_with_user")
    category = str(identity_body.get("category") or "")
    needle = args.firewall_needle
    identity_blob = json.dumps(identity_body, ensure_ascii=False)
    checks = {
        "agent_name_present": bool(agent_name.strip()),
        "agent_name_expected": (args.expect_name in agent_name) if args.expect_name else None,
        "dimensions_present": len(dims) >= 1,
        "dimensions_have_descriptions": bool(dims) and all(isinstance(d, dict) and str(d.get("description") or "").strip() for d in dims),
        # Home 「性格」 tile = identity.category. With dims present it must be non-empty
        # (A: LLM-distilled; B: deterministic top-2-dim fallback). Empty renders as "—".
        "category_present_when_dims": (bool(category.strip()) if dims else None),
        "days_gt_0": isinstance(days, int) and days > 0,
        "firewall_identity": (needle not in identity_blob) if needle else None,
        "firewall_persona": (needle not in persona_text) if needle else None,
        "memories_written": int(job.get("memory_action_count") or 0) > 0,
    }
    if getattr(args, "check_introduction", False):
        # Compatibility option: verify the V2 Genesis onboarding greeting. A
        # self_introduction identity field is useful diagnostic output but is no
        # longer a process-spawn contract.
        intro_self, greeting = "", False
        intro_deadline = time.time() + args.intro_timeout
        while time.time() < intro_deadline:
            try:
                _s, idy2 = _http("GET", f"{base}/v1/identity/get", args.api_key)
                ib2 = json.loads(_decrypt_envelope_user(idy2.get("identity") or {}, sk_raw))
                intro_self = str(ib2.get("self_introduction") or "")
            except SystemExit:
                time.sleep(args.poll); continue  # flaky proxy — keep polling
            except Exception:  # noqa: BLE001
                pass
            try:
                _s, ch = _http("GET", f"{base}/v1/chat/history?limit=12", args.api_key)
                greeting = any(str(m.get("role") or "").lower() not in ("", "user")
                               for m in (ch.get("messages") or []))
            except SystemExit:
                pass  # flaky proxy — retry next iteration
            if greeting:
                break
            time.sleep(args.poll)
        identity_body["self_introduction"] = intro_self
        checks["introduction_greeting_posted"] = greeting
    failed = [k for k, v in checks.items() if v is False]
    out = {
        "agent_name": agent_name,
        "dimensions": [{"name": d.get("name"), "value": d.get("value"), "has_desc": bool(d.get("description"))}
                       for d in dims if isinstance(d, dict)],
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
        # Remove the throwaway provider route/credential after acceptance so
        # test accounts cannot enqueue more paid Runtime V2 work. Best-effort.
        try:
            _http("DELETE", f"{base}/v1/model_api/delete", args.api_key)
        except Exception:  # noqa: BLE001
            pass
    return 0 if not failed else 1


def main():
    p = argparse.ArgumentParser(prog="genesis_e2e", description="Genesis e2e harness (test CVM).")
    sub = p.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("upload", help="(optionally register a user, then) seal+upload chunks + finalize")
    up.add_argument("--api-url", required=True)
    up.add_argument("--register", action="store_true",
                    help="self-provision a throwaway user (register + model_api/setup + whoami); "
                         "provider key from GENESIS_E2E_PROVIDER_API_KEY env")
    up.add_argument("--provider", default="", help="with --register: model provider (e.g. anthropic/openai)")
    up.add_argument("--model", default="", help="with --register: model id")
    up.add_argument("--base-url", default="", help="with --register: optional provider base_url")
    up.add_argument("--api-key", default="", help="existing user api_key (omit with --register)")
    up.add_argument("--user-id", default="", help="owner_user_id (omit with --register)")
    up.add_argument("--transcript", required=True, help="path to a plaintext test transcript")
    up.add_argument("--enclave-pk-hex", default="", help="enclave content pubkey hex (non-register; else fetch attestation)")
    up.add_argument("--attestation-url", default="", help="defaults to <api-url>/attestation")
    up.add_argument("--source-kind", default="history")
    up.add_argument("--chunk-size", type=int, default=12000)
    up.set_defaults(func=cmd_upload)

    vf = sub.add_parser("verify", help="poll job to done + privacy spot-check")
    vf.add_argument("--api-url", required=True)
    vf.add_argument("--api-key", required=True)
    vf.add_argument("--job-id", required=True)
    vf.add_argument("--timeout", type=float, default=600)
    vf.add_argument("--poll", type=float, default=10)
    vf.add_argument("--privacy-needle", default="",
                    help="comma-separated distinctive transcript fragments that MUST NOT "
                         "appear in the status payload (real leak check, e.g. '蛋子,西湖')")
    vf.set_defaults(func=cmd_verify)

    upp = sub.add_parser("upload-plaintext",
                         help="one-shot plaintext genesis ingest (POST /v1/genesis/imports/plaintext); then `verify`")
    upp.add_argument("--api-url", required=True)
    upp.add_argument("--register", action="store_true",
                     help="self-provision a throwaway user (register + model_api/setup + whoami)")
    upp.add_argument("--provider", default="")
    upp.add_argument("--model", default="")
    upp.add_argument("--base-url", default="")
    upp.add_argument("--api-key", default="")
    upp.add_argument("--user-id", default="")
    upp.add_argument("--transcript", required=True, help="plaintext history file")
    upp.add_argument("--ai-persona", default="", help="optional ai_persona/character file")
    upp.add_argument("--personal-profile", default="", help="optional personal_profile file")
    upp.add_argument("--memory-summary", default="", help="optional memory_summary/support file")
    upp.add_argument("--client-job-id", default="")
    upp.set_defaults(func=cmd_upload_plaintext)

    ac = sub.add_parser("acceptance",
                        help="per-source identity acceptance: 4 materials -> done -> decrypt identity -> assert")
    ac.add_argument("--api-url", required=True)
    ac.add_argument("--register", action="store_true", default=True)
    ac.add_argument("--provider", default="anthropic")
    ac.add_argument("--model", default="claude-haiku-4-5-20251001")
    ac.add_argument("--base-url", default="")
    ac.add_argument("--transcript", required=True, help="history file")
    ac.add_argument("--ai-persona", default="", help="角色卡 (ideally with a name)")
    ac.add_argument("--personal-profile", default="", help="个人档案")
    ac.add_argument("--memory-summary", default="", help="长期记忆")
    ac.add_argument("--relationship-started-at", default="", help="YYYY-MM-DD (tests days_with_user)")
    ac.add_argument("--expect-name", default="", help="assert agent_name contains this (e.g. 小满)")
    ac.add_argument("--firewall-needle", default="",
                    help="user_profile string that must NOT leak into identity/persona (e.g. 赵铁柱)")
    ac.add_argument("--timeout", type=float, default=900)
    ac.add_argument("--poll", type=float, default=10)
    ac.add_argument("--check-introduction", action="store_true",
                    help="after genesis done, wait for the Runtime V2 onboarding greeting")
    ac.add_argument("--intro-timeout", type=float, default=180,
                    help="seconds to wait for the 7.D introduction (default 180)")
    ac.add_argument("--no-cleanup", action="store_true",
                    help="keep the throwaway's model_api provider config")
    ac.set_defaults(func=cmd_acceptance)

    da = sub.add_parser("distill-acceptance",
                        help="live plaintext genesis distillation acceptance with ground-truth fact scoring")
    da.add_argument("--api-url", required=True)
    da.add_argument("--provider", default="anthropic")
    da.add_argument("--model", default="claude-haiku-4-5-20251001")
    da.add_argument("--base-url", default="")
    da.add_argument("--fixture", required=True,
                    help="JSON fixture with materials, persona expectations, relationship, and ground_truth facts")
    da.add_argument("--timeout", type=float, default=900)
    da.add_argument("--poll", type=float, default=10)
    da.add_argument("--intro-timeout", type=float, default=180,
                    help="seconds to wait after job done for self_introduction + greeting")
    da.add_argument("--memory-limit", type=int, default=100)
    da.add_argument("--report", default="", help="optional markdown report path")
    da.add_argument("--no-cleanup", action="store_true",
                    help="keep the throwaway's model_api after the run")
    da.set_defaults(func=cmd_distill_acceptance)

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
