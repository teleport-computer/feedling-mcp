#!/usr/bin/env python3
"""End-to-end test of the API mode (model_api) real link, as the app uses it.

Brings up the real backend + enclave (against the dstack simulator), then for
each provider whose key is present in the environment walks the *real* path:

  register (with content public key)
    -> POST /v1/model_api/setup     (backend encrypts provider key into an
                                      envelope via enclave attestation pubkey,
                                      then runs a live self-test against vendor)
    -> POST /v1/model_api/chat/send (backend decrypts key via enclave
                                      /v1/envelope/decrypt, calls the vendor,
                                      returns the reply)

This proves the enclave wrap/unwrap + auth + storage + vendor call all work,
not just a direct vendor connection. Provider keys are read from the
environment ONLY and never printed (only masked).

Prereqs (started by the caller):
  - postgres reachable via DATABASE_URL
  - phala simulator running (DSTACK_SIMULATOR_ENDPOINT)

Run from repo root with keys loaded, e.g.:
  set -a; . .env; set +a
  DATABASE_URL=postgresql://... DSTACK_SIMULATOR_ENDPOINT=<sock> \
    python3 tools/e2e_model_api_test.py
"""
from __future__ import annotations

import base64
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
BACKEND = "http://127.0.0.1:5001"
ENCLAVE = "http://127.0.0.1:5003"

PASS = "\033[92mOK \033[0m"
FAIL = "\033[91mFAIL\033[0m"
WARN = "\033[93mWARN\033[0m"


# provider -> (env key var, [candidate models to try in order])
PROVIDERS = [
    # openai: still 429 (no quota) — proves an unusable key is still rejected.
    ("openai", "OPENAI_API_KEY", ["gpt-4o-mini"]),
    # DeepSeek deprecated deepseek-chat / deepseek-reasoner after 2026-07-24.
    # Live setup tests should use the V4 model id directly.
    ("deepseek", "DEEPSEEK_API_KEY", ["deepseek-v4-flash"]),
    ("anthropic", "ANTHROPIC_API_KEY", ["claude-haiku-4-5"]),
    ("gemini", "GEMINI_API_KEY", ["gemini-2.5-flash", "gemini-2.5-pro"]),
    ("openrouter", "OPENROUTER_API_KEY", ["openai/gpt-4o-mini"]),
]


def mask(key: str) -> str:
    return f"{key[:4]}...{key[-4:]}" if len(key) > 10 else "***"


class Proc:
    def __init__(self, label, cmd, env, log_path):
        self.label, self.cmd, self.env, self.log_path = label, cmd, env, log_path
        self.proc = None

    def start(self):
        self.logf = open(self.log_path, "w")
        merged = os.environ.copy()
        merged.update(self.env)
        self.proc = subprocess.Popen(
            self.cmd, env=merged, stdout=self.logf, stderr=subprocess.STDOUT, cwd=ROOT
        )

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if hasattr(self, "logf"):
            self.logf.close()

    def tail(self, n=2500):
        try:
            return Path(self.log_path).read_text()[-n:]
        except Exception:
            return "(no log)"


def wait_for(url, timeout_s=25.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if requests.get(url, timeout=2).status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def gen_pubkey_b64() -> str:
    sk = X25519PrivateKey.generate()
    raw = sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return base64.b64encode(raw).decode("ascii")


def register_user() -> tuple[str, str]:
    pk = gen_pubkey_b64()
    r = requests.post(
        f"{BACKEND}/v1/users/register",
        json={"public_key": pk, "access_mode": "model_api"},
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    return body["user_id"], body["api_key"]


def main() -> int:
    sim = os.environ.get("DSTACK_SIMULATOR_ENDPOINT", "")
    if not sim or not Path(sim).exists():
        print(f"{FAIL} simulator socket missing: {sim!r}; run `phala simulator start`")
        return 2
    if not os.environ.get("DATABASE_URL"):
        print(f"{FAIL} DATABASE_URL not set")
        return 2

    data_dir = "/tmp/feedling-modelapi-e2e"
    os.makedirs(data_dir, exist_ok=True)

    backend = Proc(
        "backend",
        [sys.executable, "backend/serve_dev.py"],
        {"FEEDLING_PORT": "5001", "FEEDLING_ENCLAVE_URL": ENCLAVE, "FEEDLING_WS_PORT": "29998"},
        f"{data_dir}/backend.log",
    )
    enclave = Proc(
        "enclave",
        [sys.executable, "backend/enclave_app.py"],
        {
            "DSTACK_SIMULATOR_ENDPOINT": sim,
            "FEEDLING_FLASK_URL": BACKEND,
            "FEEDLING_ENCLAVE_PORT": "5003",
        },
        f"{data_dir}/enclave.log",
    )

    results = []
    try:
        print("── Bring up backend + enclave " + "─" * 30)
        backend.start()
        if not wait_for(f"{BACKEND}/healthz"):
            print(f"{FAIL} backend not healthy on :5001")
            print(backend.tail())
            return 1
        print(f"{PASS} backend healthy on :5001")

        enclave.start()
        if not wait_for(f"{ENCLAVE}/healthz"):
            print(f"{FAIL} enclave not ready on :5003")
            print(enclave.tail())
            return 1
        print(f"{PASS} enclave ready on :5003")

        att = requests.get(f"{ENCLAVE}/attestation", timeout=5).json()
        pk_hex = att.get("enclave_content_pk_hex", "")
        print(f"{PASS} attestation pubkey {pk_hex[:16]}…  compose_hash={str(att.get('compose_hash'))[:12]}…")

        for provider, key_var, models in PROVIDERS:
            key = (os.environ.get(key_var) or "").strip()
            print("\n── " + provider + " " + "─" * (46 - len(provider)))
            if not key:
                print(f"{WARN} {key_var} not set — skipping")
                results.append((provider, "skip", "no key"))
                continue
            print(f"     key={mask(key)}")

            user_id, api_key = register_user()
            hdr = {"X-API-Key": api_key}
            print(f"     registered {user_id}")

            # setup: try candidate models until one passes the live self-test
            setup_ok, used_model, setup_detail = False, None, ""
            for model in models:
                t0 = time.monotonic()
                r = requests.post(
                    f"{BACKEND}/v1/model_api/setup",
                    headers=hdr,
                    json={"provider": provider, "model": model, "api_key": key},
                    timeout=60,
                )
                dt = (time.monotonic() - t0) * 1000
                if r.status_code == 200:
                    setup_ok, used_model = True, model
                    print(f"{PASS} setup model={model} ({dt:.0f}ms) → enclave-encrypted + self-test passed")
                    break
                else:
                    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                    setup_detail = f"{r.status_code} {body.get('error')}: {str(body.get('detail'))[:90]}"
                    print(f"{WARN} setup model={model} ({dt:.0f}ms) → {setup_detail}")

            if not setup_ok:
                print(f"{FAIL} {provider}: setup failed for all models")
                results.append((provider, "setup-fail", setup_detail))
                continue

            # chat/send: real decrypt-via-enclave + vendor call
            t0 = time.monotonic()
            r = requests.post(
                f"{BACKEND}/v1/model_api/chat/send",
                headers=hdr,
                json={"message": "用一句话友好地介绍你自己，并说明你是哪个模型。", "max_tokens": 200},
                timeout=120,
            )
            dt = (time.monotonic() - t0) * 1000
            if r.status_code == 200:
                body = r.json()
                reply = (body.get("reply") or "").replace("\n", " ")[:120]
                usage = body.get("usage") or {}
                print(f"{PASS} chat/send model={used_model} ({dt:.0f}ms) provider={body.get('provider')}")
                print(f"       reply: {reply!r}")
                print(f"       usage: {usage}")
                results.append((provider, "ok", f"model={used_model}"))
            else:
                body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                detail = f"{r.status_code} {body.get('error')}: {str(body.get('detail'))[:90]}"
                print(f"{FAIL} chat/send ({dt:.0f}ms) → {detail}")
                results.append((provider, "chat-fail", detail))

        print("\n" + "═" * 60 + "\n  SUMMARY\n" + "═" * 60)
        for provider, status, detail in results:
            mark = {"ok": PASS, "skip": WARN}.get(status, FAIL)
            print(f"  {mark}  {provider:12s} {status:12s} {detail}")
        return 0

    finally:
        enclave.stop()
        backend.stop()


if __name__ == "__main__":
    sys.exit(main())
