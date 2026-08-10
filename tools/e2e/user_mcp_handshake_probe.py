"""Does a user's MCP server actually reach the model, and does the trace say so?

Two prod users (2026-08-10) reported "the app says my MCP server is connected
but the AI says it can't call the tool". Those are two different paths:

  - the app's test is a CONTROL-PLANE probe — the backend dials the server
    directly, with no time pressure, and passes;
  - the agent is a fresh ``claude --print`` process per turn that redoes the
    MCP handshake every time, and Claude Code does NOT wait for it. It emits
    its ``init`` event and starts the turn with whatever connected in time.

Prod evidence (usr_2f4d, every chat turn across two hours):

    fetch_ failed | github_ pending | piaoliuping_ pending
    lutopia_1 connected | tavily_ connected

with the turn's tool surface holding tools from ONLY the two connected servers.

⚠️ ``init`` is a SNAPSHOT of when the run opened, NOT a verdict. Measured here:
a server reported ``pending``, contributed zero tools to the opening surface,
and was then discovered and called successfully later in the same turn. So this
probe judges the way the consumer now does — init state PLUS whether a real
``mcp__<server>__*`` call succeeded — and never treats the model's prose as
evidence, because a model will happily claim it called a tool it never touched
(observed while writing this).

What it asserts, in order:
  1. the control-plane test actually passes (the premise of the whole report);
  2. the runtime and driver under test are the ones we meant to measure;
  3. the trace carries ``mcp.materialize.applied`` with the servers enabled;
  4. ``mcp.surface.wired`` says wired;
  5. ``mcp.surface.registered``'s per-server verdict matches ``--expect``.

Exit codes: 0 match, 1 verdict mismatch, 2 setup failure, 3 no trace/timeout.
A missing trace is deliberately NOT success: this probe exists because absence
of a signal was read as absence of a problem for a whole day.

Usage:
    python tools/e2e/user_mcp_handshake_probe.py                 # affected shape
    python tools/e2e/user_mcp_handshake_probe.py --expect failed
    python tools/e2e/user_mcp_handshake_probe.py --runtime v2 --model-class claude
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.e2e.client import E2EClient, TEST_API  # noqa: E402
from tools.e2e.config import load_keys  # noqa: E402

# Public, no-auth, streamable-HTTP MCP server. It has to be reachable from the
# CVM — a server on this laptop is invisible to the runner, which is the whole
# reason the control-plane probe and the agent can disagree.
DEFAULT_SERVER = "https://mcp.deepwiki.com/mcp"
DEFAULT_NAME = "deepwiki"

# Defaults reproduce the AFFECTED shape, not a convenient one:
#   - runtime "resident" (hosted_v1, the claude CLI). A fresh test account can
#     land on Runtime V2, whose MCP loop is our own code and registers servers
#     synchronously — probing that measures the runtime that is not broken.
#   - provider deepseek, which maps to the CLAUDE driver (agent_runtime_cutover:
#     anthropic/deepseek -> claude) while running a non-Anthropic model, exactly
#     like the users who reported it. Going through openrouter instead silently
#     switches the driver to pi, whose own bridge handshakes synchronously and
#     has none of this problem — that swap changes two variables and measures
#     neither. Both defaults cost me a wrong conclusion before they were pinned.
PROVIDERS = {
    "non-claude": [("E2E_KEY_DEEPSEEK", "deepseek", ["deepseek-v4-pro", "deepseek-chat"])],
    "claude": [("E2E_KEY_ANTHROPIC", "anthropic", ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"]),
               ("E2E_KEY_OPENROUTER", "openrouter", ["anthropic/claude-sonnet-4.6"])],
}


def _setup_model_api(c: E2EClient, pool: dict, model_class: str) -> tuple[bool, str]:
    for env, provider, models in PROVIDERS[model_class]:
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
    return False, f"no {model_class} provider key produced test_status=ok"


def _admin(api: str, token_path: str):
    token = Path(token_path).expanduser().read_text().strip()
    client = httpx.Client(timeout=90, verify=False,
                          headers={"Authorization": f"Bearer {token}"})
    return client


def _agent_events(admin: httpx.Client, api: str, user_id: str) -> list[dict]:
    r = admin.get(f"{api}/v1/admin/data-track/debug",
                  params={"user_id": user_id, "subsystem": "agent"})
    r.raise_for_status()
    d = r.json()
    return [row for t in d.get("turns") or [] for row in t.get("rows") or []]



# V1 and V2 answer different questions, so they can express different verdicts.
# "recovered"/"inconclusive" only exist for the claude CLI, whose surface can
# change mid-turn; V2 loads the catalogue synchronously, so a server either made
# it or did not. Asking V2 for a verdict it cannot produce must be refused
# outright rather than silently passing (codex 审出).
_V2_EXPECTATIONS = ("ok", "failed", "any")


def terminal_event_type(runtime: str) -> str:
    """Which MCP event means "this turn's surface has been reported".

    V2 never emits ``mcp.surface.registered`` — waiting for it made every
    healthy V2 run burn the full --trace-timeout before classifying.
    """
    return "mcp.surface.registered" if runtime == "resident" else "mcp.surface.resolved"


def observation_complete(events: list[dict], runtime: str) -> bool:
    """True once BOTH receipts are in.

    The driver receipt matters as much as the MCP event: trace posts are
    independent daemon threads, so stopping at the MCP event alone can leave us
    classifying a run whose runtime we never confirmed.
    """
    types = {str(e.get("type") or "") for e in events}
    return terminal_event_type(runtime) in types and "agent.model.call.done" in types


def classify(events: list[dict], *, runtime: str, expect: str,
             server_count: int) -> tuple[int, list[str]]:
    """Turn the trace into an exit code. Pure, so it can be tested without a CVM.

    Kept out of ``main`` on purpose: the previous version inlined every check,
    which is how fixes it claimed to make regressed unnoticed — there was
    nothing a test could call.

    Exit codes: 0 match, 1 real mismatch, 2 misuse, 3 no usable observation.
    The 1-vs-3 split is the point: "the servers misbehaved" and "we never
    managed to look" must not be reported the same way.
    """
    out: list[str] = []
    seen = {str(e.get("type") or ""): e for e in events}
    driver = ""
    for e in events:
        if str(e.get("type") or "") == "agent.model.call.done":
            driver = str((e.get("detail") or {}).get("driver") or "")
    kinds = sorted({str(e.get("type") or "") for e in events
                    if str(e.get("type") or "").startswith("mcp.")})
    out.append(f"observed driver={driver or '?'} events={kinds}")

    want_driver = "claude" if runtime == "resident" else "v2"
    if not driver:
        # No model.call.done at all: we never confirmed which runtime answered,
        # so nothing below can be attributed. Not a pass (codex 审出).
        out.append("FAIL 没有 agent.model.call.done —— 没确认这一轮是哪个运行时跑的,"
                   "后面的判定无从归属")
        return 3, out
    if driver != want_driver:
        out.append(f"FAIL 实际跑在 driver={driver},要测的是 {want_driver} —— 结果不能用")
        return 1, out

    if runtime != "resident":
        if expect not in _V2_EXPECTATIONS:
            out.append(f"FAIL --expect {expect} 是 V1 才有的判定,V2 表达不了")
            return 2, out
        if "mcp.surface.resolved" not in seen:
            out.append("FAIL 没有 mcp.surface.resolved(V2 的工具面埋点)")
            return 3, out
        detail = seen["mcp.surface.resolved"].get("detail") or {}
        out.append(f"resolved: {json.dumps(detail, ensure_ascii=False)}")
        if "expected" not in detail:
            # Pre-97511545 shape: the event exists but carries no baseline.
            # Same distinction as the V1 side — "ship it first", not "MCP broke".
            out.append("FAIL resolved 里没有 expected 字段 —— 部署的 backend 早于"
                       "整台失败可观测那批,先发版再测")
            return 3, out
        failure_kind = str(detail.get("surface_failure_kind") or "")
        if failure_kind:
            # The config list itself could not be read, so this turn observed
            # nothing about the servers. Reporting it as a per-server verdict
            # would be inventing evidence.
            out.append(f"FAIL 配置列表本轮读不出来({failure_kind}) —— "
                       f"这一轮对服务器没有任何观测")
            return 3, out
        expected = int(detail.get("expected") or 0)
        resolved = int(detail.get("resolved") or 0)
        skipped = [s for s in (detail.get("skipped") or []) if isinstance(s, dict)]
        if expected != server_count:
            out.append(f"FAIL 运行时看到 {expected} 台,我们配了 {server_count} 台")
            return 1, out
        if resolved + len(skipped) != expected:
            # An internally inconsistent summary means the numbers cannot be
            # trusted at all — reading a verdict out of them would be worse
            # than admitting we have nothing.
            out.append(f"FAIL 计数对不上:resolved={resolved} + skipped="
                       f"{len(skipped)} != expected={expected}")
            return 3, out
        names = ",".join(f"{s.get('name')}:{s.get('kind')}" for s in skipped[:10])
        if expect == "ok" and skipped:
            out.append(f"FAIL 有整台服务器没进工具面:{names}")
            return 1, out
        if expect == "failed" and not skipped:
            out.append("FAIL 期望有整台失败,但每台都进了工具面")
            return 1, out
        out.append(f"PASS resolved={resolved}/{expected}"
                   + (f",未就绪:{names}" if skipped else ""))
        return 0, out

    if "mcp.materialize.applied" not in seen:
        out.append("FAIL 配置没有落到 agent 侧(没有 mcp.materialize.applied)")
        return 3, out
    applied = seen["mcp.materialize.applied"].get("detail") or {}
    out.append(f"materialize: {json.dumps(applied, ensure_ascii=False)}")
    if int(applied.get("enabled_count") or 0) != server_count:
        out.append(f"FAIL 启用数 {applied.get('enabled_count')} != {server_count}")
        return 1, out

    if "mcp.surface.wired" not in seen:
        out.append("FAIL 没有 mcp.surface.wired")
        return 3, out
    wired = seen["mcp.surface.wired"].get("detail") or {}
    out.append(f"wired: {json.dumps(wired, ensure_ascii=False)}")
    if not wired.get("wired"):
        out.append("FAIL 服务器没有交给 CLI")
        return 1, out
    if wired.get("has_grant_rule") is False:
        # This probe was built for the wired-but-unapproved failure too. A
        # connected init with no call does not prove the model may invoke it —
        # and has_grant_rule=false is positive proof a grant is MISSING, even
        # though true cannot prove one is effective (codex 审出).
        out.append(f"FAIL 有服务器没有任何授权规则:{wired.get('ungranted')}")
        return 1, out

    if "mcp.surface.registered" not in seen:
        out.append("FAIL 没等到 mcp.surface.registered —— 没有观测不等于没有问题")
        return 3, out
    reg = seen["mcp.surface.registered"].get("detail") or {}
    out.append(f"registered: {json.dumps(reg, ensure_ascii=False)}")
    verdicts = reg.get("verdict") or {}
    if not verdicts:
        # The event exists but carries the pre-C-prime shape. That means the
        # DEPLOYED consumer is older than this probe, not that the servers
        # misbehaved — calling it a verdict mismatch would send someone
        # debugging MCP when the answer is "ship the consumer first".
        out.append("FAIL registered 里没有 verdict 字段 —— 部署的 consumer 早于"
                   "两阶段判据(C-prime),先发版再测")
        return 3, out
    if len(verdicts) != server_count:
        out.append(f"FAIL 判定覆盖 {len(verdicts)} 台,我们配了 {server_count} 台")
        return 1, out
    if expect != "any":
        bad = {n: v for n, v in verdicts.items() if v != expect}
        if bad:
            out.append(f"FAIL 判定 {verdicts} 与 --expect {expect} 不符")
            return 1, out
    out.append(f"PASS 每台判定均为 {expect}:{verdicts}")
    return 0, out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--name", default=DEFAULT_NAME)
    ap.add_argument("--api", default=TEST_API)
    ap.add_argument("--ask", default="",
                    help="override the prompt (default is built from --name)")
    ap.add_argument("--expect", default="ok",
                    choices=("ok", "recovered", "failed", "inconclusive", "any"),
                    help="required per-server verdict from mcp.surface.registered")
    ap.add_argument("--runtime", default="resident", choices=("resident", "v2"))
    ap.add_argument("--model-class", default="non-claude",
                    choices=("claude", "non-claude"))
    ap.add_argument("--admin-token", default="~/.feedling/data-track-admin-token")
    ap.add_argument("--copies", type=int, default=1,
                    help="register N entries pointing at the same url")
    ap.add_argument("--keep", action="store_true",
                    help="preserve the account for manual inspection; the verdict "
                         "does NOT depend on this")
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--trace-timeout", type=float, default=90.0)
    args = ap.parse_args()
    if args.copies < 1:
        print("[probe] --copies 至少为 1", file=sys.stderr)
        return 2

    # Built from --name: the old hardcoded deepwiki question meant passing a
    # different --server produced a turn about a repo that server knows nothing
    # about, and a guaranteed-meaningless result.
    ask = args.ask or (
        f"请调用你的 {args.name} MCP 工具做一次最简单的查询,"
        f"然后把工具返回的第一句原样告诉我。"
        f"如果你根本看不到这个工具,就直接说「工具不在我的工具面里」。")

    pool = load_keys()
    admin = _admin(args.api, args.admin_token)
    c = E2EClient.provision(route="model_api", api_url=args.api)
    # Not the context manager: __exit__ always tears down, which would delete
    # the account whose trace is the verdict.
    try:
        print(f"[probe] user_id={c.user_id} runtime={args.runtime} "
              f"model_class={args.model_class} expect={args.expect}")

        ok, detail = _setup_model_api(c, pool, args.model_class)
        print(f"[probe] model_api setup: {'ok' if ok else 'FAIL'} — {detail}")
        if not ok:
            return 2

        names = [args.name if i == 0 else f"{args.name}_{i}"
                 for i in range(args.copies)]
        for name in names:
            r = c.post("/v1/mcp/servers", json={
                "name": name, "url": args.server, "enabled": True, "headers": {}})
            if r.status_code not in (200, 201):
                print(f"[probe] mcp upsert {name} FAIL: {r.status_code} {r.text[:160]}")
                return 2
        print(f"[probe] registered {len(names)} server(s): {', '.join(names)}")

        # The premise of the entire bug report is "the app's test is green".
        # Listing the server only proves it was SAVED; the test endpoint is the
        # control-plane probe the user actually sees. If it fails, this run
        # cannot speak to the reported scenario at all.
        t = c.post(f"/v1/mcp/servers/{names[0]}/test")
        # Strictly True. `is not False` would take a missing or null field as
        # green — in a probe whose entire job is catching false greens.
        t_ok = t.status_code == 200 and (t.json() or {}).get("ok") is True
        print(f"[probe] control-plane test: {t.status_code} {t.text[:200]}")
        if not t_ok:
            print("[probe] FAIL 控制面测试就没通过 —— 这一轮无法说明"
                  "「测试是绿的但 agent 用不了」那个场景")
            return 2

        ar = admin.post(f"{args.api}/v1/admin/runtime-allowlist",
                        json={"user_id": c.user_id, "desired": args.runtime,
                              "note": "claude2-mcp-handshake-probe"})
        print(f"[probe] pin runtime={args.runtime}: {ar.status_code} {ar.text[:160]}")
        if ar.status_code != 200:
            return 2

        # Let the consumer pick up the moved fingerprint and the runtime flip
        # converge before the turn is claimed.
        time.sleep(30)

        sent = c.send_chat(ask)
        reply = c.wait_reply(sent, timeout=args.timeout)
        if reply is None:
            # Without a completed turn there is nothing to judge: the traces
            # that follow would describe a turn that never finished. The
            # previous version printed an empty string and carried on, so a
            # timeout could still exit 0 whenever the verdict happened to
            # match — the "it really exits non-zero" claim only ever covered
            # the mismatch path (codex 审出).
            print(f"[probe] FAIL {args.timeout:.0f}s 内没有拿到回复 —— "
                  f"这一轮根本没跑完,后面的判据无从谈起")
            return 3
        text = c.message_text(reply)
        print(f"[probe] reply after {time.time() - sent:.0f}s: {text[:200]!r}")
        print("[probe] ↑ 仅供人看。判据在下面的 trace —— 模型会声称自己调用了"
              "从没碰过的工具。")

        # Traces are emitted fire-and-forget from a daemon thread, so they land
        # after the reply. Poll rather than sleep-once: a fixed sleep is how a
        # probe starts reporting "no trace" as a finding.
        deadline = time.time() + args.trace_timeout
        events: list[dict] = []
        while time.time() < deadline:
            events = _agent_events(admin, args.api, c.user_id)
            # Both receipts, and the right one per runtime: V2 never emits
            # `registered`, so waiting on it burned the whole timeout on every
            # healthy V2 run; and stopping at the MCP event alone can leave the
            # driver receipt still in flight (codex 审出).
            if observation_complete(events, args.runtime):
                break
            time.sleep(5)

        code, lines = classify(events, runtime=args.runtime,
                               expect=args.expect, server_count=len(names))
        for line in lines:
            print(f"[probe] {line}")
        return code
    finally:
        admin.close()
        if args.keep:
            print(f"\n[probe] --keep:账号保留。手动删:\n"
                  f"  curl -X POST {args.api}/v1/account/reset "
                  f"-H 'X-API-Key: {c.api_key}' "
                  f"-d '{{\"confirm\":\"delete-all-data\"}}'")
        else:
            try:
                c.teardown()
            except Exception as e:  # noqa: BLE001
                print(f"[probe] WARNING teardown 失败 {c.user_id}: {e}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
