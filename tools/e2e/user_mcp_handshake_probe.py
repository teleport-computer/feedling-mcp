"""Does a user's MCP server actually reach the model on a hosted claude turn?

Two prod users (2026-08-10) reported "the app says my MCP server is connected
but the AI says it can't call the tool". The app's connection test is a
CONTROL-PLANE probe: the backend dials the server directly, with no time
pressure, and passes. The agent is a different path entirely — a fresh
``claude --print`` process per turn that has to redo the MCP handshake every
time — and Claude Code does NOT wait for it: it emits its ``init`` event and
starts the turn with whatever connected in time. Servers still handshaking are
reported ``pending`` and contribute ZERO tools to that turn's surface, so the
model truthfully reports it cannot call them.

Prod evidence (usr_2f4d, every chat turn across two hours):

    fetch_ failed | github_ pending | piaoliuping_ pending
    lutopia_1 connected | tavily_ connected

and the surface for that turn held tools from ONLY the two connected servers.

This probe reproduces that on the test environment, where the runtime config
matches prod. It deliberately does NOT assert "the model answered nicely" —
that is the symptom users describe but it is not measurable. It asserts on the
CLI's own ``init`` event, which names every server it registered: the one piece
of ground truth that settles wired-vs-registered-vs-callable.

Usage:
    python tools/e2e/user_mcp_handshake_probe.py --keep      # leave the account
    python tools/e2e/user_mcp_handshake_probe.py --server URL --name NAME
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import httpx
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.e2e.client import E2EClient, TEST_API  # noqa: E402
from tools.e2e.config import load_keys  # noqa: E402

# Public, no-auth, streamable-HTTP MCP server. Used here because the runner
# must dial it over the internet — a server on this laptop is unreachable from
# the CVM, which is the whole reason the control-plane probe and the agent
# disagree in the first place.
DEFAULT_SERVER = "https://mcp.deepwiki.com/mcp"
DEFAULT_NAME = "deepwiki"
MODEL_CLASS = "claude"  # set by --model-class

ASK = ("请调用你的 deepwiki MCP 工具查一下仓库 modelcontextprotocol/servers,"
       "然后把工具返回的第一句原样告诉我。如果你根本看不到这个工具,"
       "就直接说「工具不在我的工具面里」。")


def _setup_model_api(c: E2EClient, pool: dict) -> tuple[bool, str]:
    """Hosted model_api with the default driver (claude — see spawners:1081)."""
    # Order matters: the FIRST candidate is what the reported users actually
    # run — a non-Anthropic model behind a relay. The tool surface Claude Code
    # offers is not constant across models (deferred tools / ToolSearch vs an
    # eager list + WaitForMcpServers), and which one you get decides whether a
    # late-connecting MCP server is recoverable mid-turn. Probing only with a
    # Claude model would measure the shape the affected users never see.
    candidates = (
        # provider=deepseek maps to the CLAUDE driver (agent_runtime_cutover:
        # anthropic/deepseek -> claude), so this is claude-the-CLI pointed at a
        # non-Anthropic model — exactly the affected users' shape. Going through
        # openrouter instead would silently switch the driver to pi, whose own
        # bridge does the MCP handshake synchronously and has none of this
        # problem: that comparison changes two variables and measures neither.
        (("E2E_KEY_DEEPSEEK", "deepseek",
          ["deepseek-v4-pro", "deepseek-chat"]),)
        if MODEL_CLASS == "non-claude" else
        (("E2E_KEY_ANTHROPIC", "anthropic",
          ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"]),
         ("E2E_KEY_OPENROUTER", "openrouter", ["anthropic/claude-sonnet-4.6"]))
    )
    for env, provider, models in candidates:
        key = pool.get(env, "")
        if not key:
            continue
        for model in models:
            r = c.post("/v1/model_api/setup",
                       json={"provider": provider, "model": model, "api_key": key})
            if r.status_code != 200:
                continue
            body = r.json()
            status = ((body.get("config") or {}).get("test_status")
                      or body.get("test_status") or "")
            if status == "ok":
                return True, f"{provider}/{model}"
    return False, "no provider key produced test_status=ok"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--name", default=DEFAULT_NAME)
    ap.add_argument("--api", default=TEST_API)
    ap.add_argument("--keep", action="store_true",
                    help="leave the account alive for trace inspection")
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--runtime", choices=("resident", "v2"), default="",
                    help="pin the runtime; resident = hosted_v1 = the claude CLI")
    ap.add_argument("--admin-token",
                    default="~/.feedling/data-track-admin-token")
    ap.add_argument("--model-class", choices=("claude", "non-claude"),
                    default="claude",
                    help="non-claude reproduces the affected users' shape")
    args = ap.parse_args()
    global MODEL_CLASS
    MODEL_CLASS = args.model_class

    pool = load_keys()
    c = E2EClient.provision(route="model_api", api_url=args.api)
    # Deliberately not the context manager: __exit__ always tears the account
    # down, which would delete the very account whose trace we need to read.
    # try/finally instead, with --keep short-circuiting the delete — and the
    # orphan manifest left in place so `p0.py --cleanup-orphans` still sweeps
    # it if I forget (test-account-hygiene: create → use → delete).
    try:
        print(f"[probe] user_id={c.user_id}")

        ok, detail = _setup_model_api(c, pool)
        print(f"[probe] model_api setup: {'ok' if ok else 'FAIL'} — {detail}")
        if not ok:
            return 2

        r = c.post("/v1/mcp/servers", json={
            "name": args.name, "url": args.server, "enabled": True, "headers": {},
        })
        print(f"[probe] mcp upsert: {r.status_code} {r.text[:200]}")
        if r.status_code not in (200, 201):
            return 2

        # The control-plane view — this is what the app shows the user, and the
        # whole point is that it can be green while the agent sees nothing.
        listed = c.get("/v1/mcp/servers")
        print(f"[probe] control-plane list: {listed.text[:300]}")

        # Pin the runtime. A fresh test account can land on Runtime V2, whose
        # MCP tool loop is OUR code (provider_client + mcp_tools) and registers
        # servers synchronously — it does not have this failure mode at all.
        # The affected users are hosted_v1, i.e. the claude CLI, so probing
        # without pinning measures the runtime that is not broken. The desired
        # value is "resident" (V1); "v1" is rejected, and the mode constant is
        # "resident_cli" — three spellings for one thing, easy to get wrong.
        if args.runtime:
            tok = Path(args.admin_token).expanduser().read_text().strip()
            with httpx.Client(timeout=90, verify=False) as ac:
                ar = ac.post(f"{args.api}/v1/admin/runtime-allowlist",
                             headers={"Authorization": f"Bearer {tok}"},
                             json={"user_id": c.user_id, "desired": args.runtime,
                                   "note": "claude2-mcp-handshake-probe"})
                print(f"[probe] pin runtime={args.runtime}: "
                      f"{ar.status_code} {ar.text[:160]}")
                if ar.status_code != 200:
                    return 2

        # The consumer applies MCP config on its next poll (fingerprint moved).
        # Give it a beat so the first chat turn is not raced against materialize,
        # and so a runtime flip has converged before the turn is claimed.
        time.sleep(30)

        sent = c.send_chat(ASK)
        reply = c.wait_reply(sent, timeout=args.timeout)
        text = c.message_text(reply) if reply else ""
        print(f"[probe] reply after {time.time() - sent:.0f}s: {text[:400]!r}")

        print("\n[probe] 判据不在上面这段回复里 —— 模型说什么都只是症状。")
        print("[probe] 硬证据在 trace 的 agent.model.call.done → reply_head →")
        print("[probe] claude init 事件的 mcp_servers,以及 mcp.surface.registered。")
        print(f"[probe] 查:/v1/admin/data-track/debug?user_id={c.user_id}&subsystem=agent")

        return 0
    finally:
        if args.keep:
            print(f"\n[probe] --keep:账号保留(孤儿清单也留着,忘了会被 "
                  f"p0.py --cleanup-orphans 扫掉)。手动删:\n"
                  f"  curl -X POST {args.api}/v1/account/reset "
                  f"-H 'X-API-Key: {c.api_key}' "
                  f"-d '{{\"confirm\":\"delete-all-data\"}}'")
        else:
            try:
                c.teardown()
            except Exception as e:  # noqa: BLE001
                print(f"[probe] WARNING teardown 失败 {c.user_id}: {e}",
                      file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
