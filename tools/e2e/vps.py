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

from .client import (
    E2EClient, TEST_API, TEST_ENCLAVE, VERDICT_FALLBACK, VERDICT_OK,
    decrypt_verdict as _decrypt_verdict,
)
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

    def verdict_step(name: str, verdict: str, detail: str) -> bool:
        """三态记录:ok / fallback / fail(与 hosted 同口径)。

        这一格不是理论风险:vps-claude-code 实测被同一个盲区咬中 —— CLI 拒答、
        consumer 发布固定 fallback，而 runner 报整格 PASS。
        """
        steps.append((name, verdict, detail))
        if verdict != VERDICT_OK and active_client is not None:
            active_client.preserve_failure(f"{name}[{verdict}]: {detail or verdict}")
        return verdict == VERDICT_OK

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
                # 同 hosted:只测「非空」会把 consumer 发布的固定 fallback 判成成功。
                chat_verdict, chat_detail = c.classify_reply(reply, text)
                verdict_step(
                    "chat", chat_verdict,
                    f"{time.time() - sent:.0f}s; head={text[:40]!r}; {chat_detail}"
                    if reply else
                    f"no reply in {REPLY_TIMEOUT:.0f}s; log tail={_tail(log_path)}")

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
                    dec_verdict, dec_detail = _decrypt_verdict(dec, dec_err)
                    verdict_step("decrypt", dec_verdict, dec_detail)

                bubbles = c.system_bubbles_since(run_start)
                step("no-error-bubbles", not bubbles, f"{len(bubbles)} system notice(s)")

            # fallback 单列：它阻断发版(用户实际收到的是失败话术)，但不并进
            # "fail"，否则「交付了失败话术」和「压根没回来」在报表上同形。
            if any(s[1] == "fail" for s in steps):
                result = "fail"
            elif any(s[1] == VERDICT_FALLBACK for s in steps):
                result = VERDICT_FALLBACK
            else:
                result = "ok"
            return {"cell": cell.name, "result": result,
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
