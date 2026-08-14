"""VPS (resident) P0 cell: provision → launch LOCAL consumer with the
harness CLI → verify_loop → chat roundtrip → teardown.

Runs tools/chat_resident_consumer.py as a subprocess against the test env —
the exact deployment shape of a self-hosted user, driven by whichever local
CLI (claude / codex / hermes) the cell names. A cell whose binary is absent
locally reports SKIP, not failure.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .client import E2EClient, TEST_API, TEST_ENCLAVE
from .config import VpsCell
from .unlock import verify_loop, wait_resident_consumer_passing

_REPO = Path(__file__).resolve().parent.parent.parent
REPLY_TIMEOUT = 240.0


def run_vps_cell(cell: VpsCell) -> dict:
    if not shutil.which(cell.needs_binary):
        return {"cell": cell.name, "result": "skip",
                "steps": [("binary", "skip", f"{cell.needs_binary} not on PATH")]}

    steps: list[tuple[str, str, str]] = []
    active_client: E2EClient | None = None

    def step(name: str, ok: bool, detail: str = "") -> bool:
        steps.append((name, "ok" if ok else "fail", detail))
        if not ok and active_client is not None:
            active_client.preserve_failure(f"{name}: {detail or 'failed'}")
        return ok

    workdir = Path(tempfile.mkdtemp(prefix=f"feedling_e2e_{cell.name}_"))
    proc: subprocess.Popen | None = None
    log_path = workdir / "consumer.log"
    try:
        with E2EClient.provision(route="resident") as c:
            active_client = c
            c.configure_failure_evidence(
                cell=f"vps:{cell.name}", artifacts={"consumer_log": str(log_path)},
            )
            # No identity/genesis needed — the resident gate looks only at the
            # consumer heartbeat + verify_loop (see unlock.py docstring).
            env = os.environ.copy()
            env.update({
                "FEEDLING_API_URL": TEST_API,
                "FEEDLING_API_KEY": c.api_key,
                "FEEDLING_ENCLAVE_URL": TEST_ENCLAVE,
                "AGENT_MODE": "cli",
                "AGENT_CLI_CMD": cell.agent_cli_cmd,
                "FEEDLING_AGENT_CLI_CWD": str(workdir / "agent_home"),
                "CHECKPOINT_FILE": str(workdir / "checkpoint.json"),
                "AGENT_SESSION_FILE": str(workdir / "agent-session.txt"),
                "IMAGE_TEMP_DIR": str(workdir / "images"),
                "FEEDLING_AUTO_UPDATE": "0",   # never let a test run self-mutate the repo
                "NO_PROXY": "*", "no_proxy": "*",
            })
            (workdir / "agent_home").mkdir(parents=True, exist_ok=True)
            with open(log_path, "w") as log_f:
                proc = subprocess.Popen(
                    [sys.executable, str(_REPO / "tools" / "chat_resident_consumer.py")],
                    env=env, stdout=log_f, stderr=subprocess.STDOUT,
                    cwd=str(_REPO),
                )
                # The gate's signal (a): the consumer's official-header poll
                # heartbeat must be seen server-side before verify_loop can pass.
                passing = wait_resident_consumer_passing(c, timeout=90)
                if proc.poll() is not None:
                    step("consumer-start", False,
                         f"exited rc={proc.returncode}; tail={_tail(log_path)}")
                    return {"cell": cell.name, "result": "fail", "steps": steps,
                            "user_id": c.user_id, "log": str(log_path)}
                if not step("consumer-heartbeat", passing,
                            "" if passing else f"no official poll seen; tail={_tail(log_path)}"):
                    return {"cell": cell.name, "result": "fail", "steps": steps,
                            "user_id": c.user_id, "log": str(log_path)}

                # The gate's signal (b): verify_loop exercises the REAL agent
                # path (hidden ping → consumer reply → chat_loop_verified).
                if not step("verify-loop", verify_loop(c, timeout=120), ""):
                    return {"cell": cell.name, "result": "fail", "steps": steps,
                            "user_id": c.user_id, "log": str(log_path)}

                run_start = time.time()
                sent = c.send_chat("你好，请用一句话回应我。")
                reply = c.wait_reply(sent, timeout=REPLY_TIMEOUT)
                text = c.message_text(reply) if reply else ""
                step("chat", reply is not None and bool(text.strip()),
                     f"{time.time() - sent:.0f}s; head={text[:40]!r}" if reply
                     else f"no reply in {REPLY_TIMEOUT:.0f}s; log tail={_tail(log_path)}")

                # Tier/readability continuity (HARD P0): encrypted replies must
                # decrypt with the user's key; plaintext replies must be canonical
                # `body` rows without residual crypto fields. Only meaningful once
                # a reply arrived.
                if reply is not None:
                    try:
                        dec = c.read_reply_strict(reply)
                        dec_err = ""
                    except Exception as de:  # noqa: BLE001
                        dec, dec_err = "", f"{type(de).__name__}: {de}"
                    step("decrypt", bool(dec.strip()),
                         f"len={len(dec)}" if dec.strip() else (dec_err or "empty plaintext"))

                bubbles = c.system_bubbles_since(run_start)
                step("no-error-bubbles", not bubbles, f"{len(bubbles)} system notice(s)")

            hard_fail = any(s[1] == "fail" for s in steps)
            return {"cell": cell.name, "result": "fail" if hard_fail else "ok",
                    "steps": steps, "user_id": c.user_id, "log": str(log_path)}
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


def _tail(path: Path, lines: int = 4) -> str:
    try:
        return " | ".join(path.read_text(errors="replace").splitlines()[-lines:])[-300:]
    except Exception:  # noqa: BLE001
        return "<no log>"
