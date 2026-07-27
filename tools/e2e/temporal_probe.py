"""Temporal-grounding probe for Runtime V2 (model-touching, V2-only).

Unit tests prove the temporal block is BUILT. They cannot prove the model
actually READS it — the block ships the per-message ages as a JSON array keyed
by ``index``, and the model has to correlate that index back to the verbatim
tail it saw earlier. LLMs are unreliable at exactly that kind of cross
reference, so this asks a live model two questions whose answers are
unobtainable from anything else in the prompt:

  Q1 "现在几点"  -> only ``temporal_context.current_local_time`` carries it.
                    A model that ignores the block has to guess or refuse.
  Q2 "上一条多久前" -> needs ``seconds_since_last_genuine_user_message`` or the
                    ``tail_timestamps`` mapping; the probe sleeps a measured
                    gap first so "just now" is a wrong answer.

Q1 failing means the block is not being read at all. Q1 passing and Q2 failing
means the index mapping is the weak part — that is the signal to fall back to
inline immutable ``sent_at`` per tail row (pre-agreed with codex2, 2026-07-26).

Runs ONLY against the test family and pins the account to Runtime V2 through
the admin allowlist, because the temporal block is V2-only code. Always tears
the account down (test-account hygiene).

Run:  python3 -m tools.e2e.temporal_probe [--gap-sec 240] [--keep]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .client import TEST_API, E2EClient
from .config import load_keys
from .hosted import _hosted_send

ADMIN_TOKEN_FILE = Path.home() / ".feedling" / "data-track-admin-token"
CONVERGE_TIMEOUT_SEC = 180.0
REPLY_TIMEOUT_SEC = 240.0


def _admin_token() -> str:
    return ADMIN_TOKEN_FILE.read_text().strip() if ADMIN_TOKEN_FILE.exists() else ""


def _admin(api_url: str, path: str, *, token: str, method: str = "GET", body=None):
    with httpx.Client(timeout=40, verify=False) as h:
        return h.request(method, f"{api_url}{path}",
                         headers={"X-Admin-Token": token},
                         json=body)


def _pin_v2(api_url: str, user_id: str, token: str, note: str) -> tuple[bool, str]:
    r = _admin(api_url, "/v1/admin/runtime-allowlist", token=token, method="POST",
               body={"user_id": user_id, "desired": "v2", "note": note})
    if r.status_code != 200:
        return False, f"pin failed: {r.status_code} {r.text[:160]}"
    deadline = time.time() + CONVERGE_TIMEOUT_SEC
    last = ""
    while time.time() < deadline:
        g = _admin(api_url, "/v1/admin/runtime-allowlist", token=token)
        if g.status_code == 200:
            for row in (g.json().get("allowlist") or []):
                if row.get("user_id") != user_id:
                    continue
                actual = (row.get("actual") or {}).get("mode", "")
                last = f"desired={row.get('desired')} actual={actual} converged={row.get('converged')}"
                if row.get("converged") and actual == "db_action_v2":
                    return True, last
        time.sleep(5)
    return False, f"not converged in {CONVERGE_TIMEOUT_SEC:.0f}s ({last})"


def _unpin(api_url: str, user_id: str, token: str) -> None:
    _admin(api_url, "/v1/admin/runtime-allowlist", token=token, method="POST",
           body={"user_id": user_id, "desired": "resident", "note": "temporal probe cleanup"})


def _retry_transport(fn, *, attempts: int = 4, label: str = ""):
    """Run ``fn`` through transient TLS/connect flaps on the test CVM.

    The test gateway drops TLS during deploy windows (observed 2026-07-26:
    handshake fails while TCP still connects in 4ms). Letting that abort the
    probe is bad twice over — the run is lost, AND the ``finally`` cleanup hits
    the same dead gateway and leaks a provisioned account. Everything that
    talks to the API goes through here.
    """
    delay, last = 4.0, None
    for _ in range(attempts):
        try:
            return fn()
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
            last = exc
            if label:
                print(f"[temporal] {label}: transport flap ({exc}); retrying in {delay:.0f}s",
                      file=sys.stderr)
            time.sleep(delay)
            delay *= 2
    raise last  # type: ignore[misc]


def _ask(c: E2EClient, text: str) -> tuple[str, float]:
    """Send one hosted turn, return (decrypted_reply_text, round_trip_sec).

    Goes through ``_hosted_send`` (the model_api endpoint with the V2-readiness
    retry) and ``message_text`` — reply rows are sealed, so reading ``content``
    off the row yields nothing.
    """
    t0 = time.time()
    sent, err = _retry_transport(lambda: _hosted_send(c, text), label="send")
    if err:
        return f"[send failed: {err}]", time.time() - t0
    reply = _retry_transport(
        lambda: c.wait_reply(sent, timeout=REPLY_TIMEOUT_SEC), label="wait_reply")
    return (c.message_text(reply) if reply else ""), time.time() - t0


def _judge_clock(reply: str, expected_utc: datetime) -> tuple[bool, str]:
    """Accept any HH:MM in the reply within ±20min of expected (UTC or +08:00).

    The probe account has no registry timezone, so the block ships UTC; a model
    that helpfully converts to Beijing time is still reading the block, so both
    are accepted rather than scored as a miss.
    """
    hits = re.findall(r"(\d{1,2})\s*[:点]\s*(\d{1,2})?", reply)
    if not hits:
        return False, "no HH:MM in reply"
    for hh, mm in hits:
        try:
            h, m = int(hh), int(mm or 0)
        except ValueError:
            continue
        for label, ref in (("UTC", expected_utc),
                           ("UTC+8", expected_utc + timedelta(hours=8))):
            cand = ref.replace(hour=h % 24, minute=m % 60, second=0, microsecond=0)
            if abs((cand - ref).total_seconds()) <= 20 * 60:
                return True, f"{h:02d}:{m:02d} matches {label} {ref:%H:%M}"
    return False, f"clock values {hits} match neither UTC {expected_utc:%H:%M} nor +08:00"


def _judge_gap(reply: str, gap_sec: float) -> tuple[bool, str]:
    """The reply must land near the real gap, and must NOT say 'just now'."""
    minutes = gap_sec / 60.0
    if re.search(r"刚刚|刚才|just now|马上|秒前", reply):
        return False, f"claims 'just now' but the real gap was {minutes:.1f}min"
    nums = [float(n) for n in re.findall(r"(\d+(?:\.\d+)?)\s*(?:分钟|分|minutes?|mins?)", reply)]
    if not nums:
        return False, "no minute figure in reply"
    if any(abs(n - minutes) <= max(2.0, minutes * 0.5) for n in nums):
        return True, f"reply {nums} ≈ real {minutes:.1f}min"
    return False, f"reply {nums} far from real {minutes:.1f}min"


def _cleanup_orphans(api_url: str, token: str) -> int:
    """Unpin every leaked account, then hand deletion to p0's existing sweeper.

    Deletion logic is deliberately NOT duplicated here: ``p0._cleanup_orphans``
    already encodes the rules that matter (``_refuse_prod`` on every manifest,
    remove the entry only on proof of deletion, keep it on 401 as an evidence
    trail). Copying that would mean two sweepers drifting apart. This adds only
    the step p0 has no reason to know about — releasing the Runtime V2 allowlist
    pin this probe applied.
    """
    from tools.e2e.client import _ORPHANS_DIR
    from tools.e2e.p0 import _cleanup_orphans as sweep

    files = sorted(_ORPHANS_DIR.glob("*.json")) if _ORPHANS_DIR.exists() else []
    for path in files:
        try:
            rec = json.loads(path.read_text())
            uid = str(rec["user_id"])
            api = str(rec.get("api_url") or api_url)
        except Exception as exc:  # noqa: BLE001 — p0's sweep reports bad manifests
            print(f"[temporal] {path.name}: unreadable ({exc}); leaving to sweeper",
                  file=sys.stderr)
            continue
        try:
            _retry_transport(lambda: _unpin(api, uid, token), label=f"unpin {uid}")
            print(f"[temporal] {uid}: allowlist pin released")
        except Exception as exc:  # noqa: BLE001 — deletion still worth attempting
            print(f"[temporal] {uid}: unpin failed ({exc}); deleting anyway",
                  file=sys.stderr)
    return sweep()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cleanup-orphans", action="store_true",
                    help="unpin+delete accounts left by a crashed run, then exit")
    ap.add_argument("--gap-sec", type=float, default=240.0,
                    help="measured silence before Q2 (default 240s)")
    ap.add_argument("--api", default=os.environ.get("FEEDLING_E2E_API", TEST_API))
    ap.add_argument("--model", default="deepseek-v4-flash")
    ap.add_argument("--provider", default="deepseek")
    ap.add_argument("--key-env", default="E2E_KEY_DEEPSEEK")
    ap.add_argument("--keep", action="store_true", help="skip teardown (debugging)")
    args = ap.parse_args()

    token = _admin_token()
    if not token:
        print(f"[temporal] no admin token at {ADMIN_TOKEN_FILE}", file=sys.stderr)
        return 2
    if args.cleanup_orphans:
        return _cleanup_orphans(args.api, token)
    key = load_keys().get(args.key_env, "")
    if not key:
        print(f"[temporal] no {args.key_env} in the key pool", file=sys.stderr)
        return 2

    results: list[tuple[str, bool, str]] = []
    user_id = ""
    c = E2EClient.provision(route="model_api", api_url=args.api)
    try:
        user_id = c.user_id
        print(f"[temporal] user={user_id} api={args.api}")

        r = c.post("/v1/model_api/setup",
                   json={"provider": args.provider, "model": args.model, "api_key": key})
        ok = r.status_code == 200 and (
            ((r.json().get("config") or {}).get("test_status") or r.json().get("test_status"))
            == "ok")
        results.append(("setup", ok, f"{args.provider}/{args.model} {r.status_code}"))
        if not ok:
            return _report(results)

        pinned, detail = _pin_v2(args.api, user_id, token, "temporal probe (P3③)")
        results.append(("pin_v2", pinned, detail))
        if not pinned:
            return _report(results)

        # Turn 1 — establishes a real tail row whose age Q2 will ask about.
        first, rt = _ask(c, "你好呀,我在测试一个功能,先随便聊两句。")
        results.append(("turn1", bool(first), f"{rt:.0f}s reply={first[:60]!r}"))
        if not first:
            return _report(results)

        # Q1 — current time. Nothing else in the prompt carries it.
        asked_at = datetime.now(timezone.utc)
        q1, rt = _ask(c, "现在几点了?只回时间就行。")
        ok, why = _judge_clock(q1, asked_at)
        results.append(("Q1_clock", ok, f"{why} | {rt:.0f}s | {q1[:80]!r}"))

        # Q2 — age of the previous user message, after a measured silence.
        print(f"[temporal] sleeping {args.gap_sec:.0f}s to create a measurable gap…")
        time.sleep(args.gap_sec)
        q2, rt = _ask(c, "我上一条消息是多久以前发的?只回一个大概的分钟数。")
        ok, why = _judge_gap(q2, args.gap_sec)
        results.append(("Q2_gap", ok, f"{why} | {rt:.0f}s | {q2[:80]!r}"))

        return _report(results)
    finally:
        if user_id and not args.keep:
            try:
                _retry_transport(lambda: _unpin(args.api, user_id, token), label="unpin")
            except Exception as exc:  # noqa: BLE001 — cleanup must not mask results
                print(f"[temporal] unpin failed (non-fatal): {exc}", file=sys.stderr)
        if not args.keep:
            try:
                _retry_transport(c.teardown, label="teardown")
                print("[temporal] account torn down")
            except Exception as exc:  # noqa: BLE001
                # The orphan manifest (~/.feedling-e2e-orphans/) still holds the
                # credentials; say so explicitly instead of leaving a bare stack
                # trace, because a leaked account is a hygiene violation someone
                # has to clean up by hand.
                print(f"[temporal] teardown failed — ORPHAN {user_id} "
                      f"(creds in ~/.feedling-e2e-orphans/{user_id}.json, "
                      f"rerun with --cleanup-orphans once the env is back): {exc}",
                      file=sys.stderr)


def _report(results: list[tuple[str, bool, str]]) -> int:
    print("\n| step | result | detail |")
    print("|---|---|---|")
    for name, ok, detail in results:
        safe = detail.replace("|", "\\|")[:110]
        print(f"| {name} | {'✅ PASS' if ok else '❌ FAIL'} | {safe} |")
    q1 = next((ok for n, ok, _ in results if n == "Q1_clock"), None)
    q2 = next((ok for n, ok, _ in results if n == "Q2_gap"), None)
    print()
    if q1 is False:
        print("判读:Q1 失败 = 时间块根本没被读到。整个方案要重做,不是回退 inline 就能救。")
    elif q1 and q2 is False:
        print("判读:Q1 过 / Q2 挂 = 块被读到了,但 index 映射用不上 → "
              "走预先约定的回退:每条 tail inline 不可变 sent_at,相对年龄留尾块。")
    elif q1 and q2:
        print("判读:两问全过 = index 映射方案可用,不需要回退。")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
