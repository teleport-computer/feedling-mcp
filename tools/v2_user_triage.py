#!/usr/bin/env python3
"""One-shot Runtime V2 triage for a single user — read-only, no decryption.

Written after the 2026-07-27 incident (usr_7f30…, three days of silent
failures) where reconstructing the picture took twenty-odd ad-hoc queries.
Everything here is CONTENT-FREE metadata that any operator can read directly;
nothing needs the break-glass trajectory decrypt.

Four of the checks exist because they are what actually broke that case, and
none of them is obvious enough that the next person would think to run it:

  * `[summary]`   how long the summary watermark has been FROZEN. A stalled
                  frontier precedes the visible job failures and is the
                  earliest, most direct signal of a compaction deadlock.
  * `[backlog]`   the shape of the queue head — specifically an R2-offloaded
                  row (`body_key`, no `body_ct`) whose hydrated body exceeds
                  the whole batch char budget. Such a row can never be folded
                  and blocks every message behind it.
  * `[traj]`      per-event elapsed time and repeated request sizes. Identical
                  `payload_bytes` across retries proves the same batch is being
                  re-sent, i.e. a self-locking loop rather than flaky luck.
  * `[ledger]`    the same user's provider outcomes under V1 and V2 together.
                  A relay succeeding hundreds of times under one runtime while
                  failing every time under the other rules out the relay and
                  points at what we send (usr_90184…: 902 V1 successes, peak
                  70,926 input tokens, against 14/14 V2 failures).

Usage:
    python tools/v2_user_triage.py --env prod --user-id usr_7f30d63fb7edb61b
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - operator convenience
    sys.exit("psycopg is required: pip install 'psycopg[binary]'")


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Mirrors worker._COMPACTION_BATCH_CHARS' default. A row larger than this on
# its own can never sit inside any batch, whatever the batch size.
COMPACTION_BATCH_CHARS = 120_000
# Mirrors worker._TAIL_BUDGET's default: a backlog at or under this fits
# entirely in the verbatim tail, so there is no gap to close.
TAIL_BUDGET_MSGS = 20


def _dsn(env: str) -> str:
    """Read <ENV>_DATABASE_URL out of the repo .env without echoing secrets."""
    key = f"{env.upper()}_DATABASE_URL"
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        sys.exit(f"{env_file} not found — run from the repo, or export {key}")
    for line in env_file.read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    sys.exit(f"{key} not found in .env")


def _fetch(conn, sql: str, params: tuple = ()) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _one(conn, sql: str, params: tuple = ()) -> dict | None:
    rows = _fetch(conn, sql, params)
    return rows[0] if rows else None


def _head(title: str) -> None:
    print(f"\n\033[1m[{title}]\033[0m")


def _warn(text: str) -> str:
    return f"\033[33m⚠ {text}\033[0m"


def section_runtime(conn, uid: str) -> None:
    _head("runtime")
    user = _one(conn, "SELECT created_at FROM users WHERE user_id = %s", (uid,))
    if not user:
        sys.exit(f"user {uid} not found in this environment")
    print(f"  created           {user['created_at']}")

    # V1/V2 is decided by the per-user fence, never by the allowlist: the
    # allowlist is the admin's intent, the fence is what the send path obeys.
    fence = _one(
        conn,
        "SELECT hosted_runtime_state, runtime_generation, updated_at "
        "FROM v2_runtime_state WHERE user_id = %s",
        (uid,),
    )
    if fence:
        print(
            f"  fence             {fence['hosted_runtime_state']} "
            f"(generation {fence['runtime_generation']}, {fence['updated_at']})"
        )
    else:
        print("  fence             (no row → V1)")

    allow = _one(
        conn,
        "SELECT desired, updated_by, updated_at, note "
        "FROM v2_user_allowlist WHERE user_id = %s",
        (uid,),
    )
    if allow:
        print(f"  allowlist desired {allow['desired']}  by={allow['updated_by'] or '-'}")
        if allow["note"]:
            print(f"  note              {allow['note'][:160]}")
    else:
        print("  allowlist         (no row → default runtime)")


def section_jobs(conn, uid: str, limit: int) -> tuple[int, int]:
    """Print job outcomes; return (failed_total, coverage_shaped_failures).

    The split matters: a coverage stall and a provider error are both
    "failed" and look identical in a count, but only one of them says
    anything about compaction (usr_90184… failed 14/14 with a perfectly
    healthy frontier).
    """
    _head("jobs")
    agg = _fetch(
        conn,
        "SELECT status, count(*) AS n FROM agent_jobs WHERE user_id = %s "
        "GROUP BY 1 ORDER BY 2 DESC",
        (uid,),
    )
    if not agg:
        print("  (no V2 jobs — this user has never run on Runtime V2)")
        return 0, 0
    total = sum(r["n"] for r in agg)
    done = next((r["n"] for r in agg if r["status"] == "completed"), 0)
    print(f"  {total} jobs: " + ", ".join(f"{r['status']}={r['n']}" for r in agg))
    if total and done == 0:
        print("  " + _warn("zero completed jobs — every turn this user ran has failed"))

    errs = _fetch(
        conn,
        "SELECT coalesce(last_error,'') AS e, count(*) AS n FROM agent_jobs "
        "WHERE user_id = %s AND last_error IS NOT NULL GROUP BY 1 ORDER BY 2 DESC",
        (uid,),
    )
    for row in errs:
        print(f"    {row['n']:>3} × {row['e']}")
        # Since 2026-07-27 the coverage stall carries the compaction reject
        # code that caused it; older rows collapse into responder_error.
        if row["e"].startswith("turn_failed:prompt_coverage_incomplete:"):
            code = row["e"].split(":", 2)[-1]
            print("        " + _warn(f"compaction refused: {code}"))
        elif row["e"] == "turn_failed:responder_error":
            print("        (bucket shared by ~12 TurnError sites — see reject codes above)")

    recent = _fetch(
        conn,
        "SELECT id, reason, status, created_at, finished_at, "
        "coalesce(last_error,'') AS e FROM agent_jobs "
        "WHERE user_id = %s ORDER BY id DESC LIMIT %s",
        (uid, limit),
    )
    print("  recent:")
    for row in recent:
        print(
            f"    #{row['id']:<5} {row['status']:<10} {row['reason'] or '-':<12} "
            f"{row['created_at']:%m-%d %H:%M:%S}  {row['e']}"
        )
    failed = next((r["n"] for r in agg if r["status"] == "failed"), 0)
    # Both the current code and everything logged before 2026-07-27, when a
    # coverage stall was still folded into the shared responder_error bucket.
    coverage_shaped = sum(
        r["n"] for r in errs
        if "prompt_coverage_incomplete" in r["e"]
        or r["e"] == "turn_failed:responder_error"
    )
    return failed, coverage_shaped


def section_metrics(conn, uid: str) -> None:
    _head("metrics")
    rows = _fetch(
        conn,
        "SELECT provider, model, status, count(*) AS n, "
        "  min(prompt_tokens) AS pmin, max(prompt_tokens) AS pmax, "
        "  avg(completion_tokens)::int AS cavg, avg(model_calls)::numeric(4,1) AS calls, "
        "  avg(latency_ms)::int AS lat "
        "FROM v2_turn_metrics WHERE user_id = %s GROUP BY 1,2,3 ORDER BY 4 DESC",
        (uid,),
    )
    if not rows:
        print("  (no turn metrics)")
        return
    for r in rows:
        print(
            f"  {r['n']:>3} × {r['provider']}/{r['model']} → {r['status']}\n"
            f"        prompt {r['pmin']}–{r['pmax']}, completion avg {r['cavg']}, "
            f"{r['calls']} calls, {r['lat']}ms"
        )
        # Relay routes are the ones that skip every audited context-window
        # family and inherit the unaudited default, so flag them explicitly.
        if r["provider"] == "openai_compatible":
            print("        (relay route: unaudited context window, format compliance varies)")


def section_summary(
    conn, uid: str, *, failed_jobs: int, coverage_failures: int
) -> tuple[int | None, int]:
    """Print the summary frontier; return (watermark_seq, backlog_count).

    A frozen watermark is only a symptom when the frontier is BEHIND and turns
    are failing. On its own it means nothing: a healthy user who simply has not
    spoken since yesterday has an equally motionless watermark, and flagging
    that trains everyone to ignore the warning that matters.
    """
    _head("summary")
    row = _one(
        conn,
        "SELECT watermark_seq, updated_at, version, "
        "  extract(epoch FROM (now() - updated_at))/3600 AS stalled_h "
        "FROM v2_conversation_summary WHERE user_id = %s",
        (uid,),
    )
    if not row:
        print("  (no summary row — nothing folded yet)")
        return None, 0
    watermark = int(row["watermark_seq"] or 0)
    stalled = float(row["stalled_h"] or 0)
    print(
        f"  watermark_seq     {watermark}  (v{row['version']}, "
        f"updated {row['updated_at']:%Y-%m-%d %H:%M})"
    )

    counts = _one(
        conn,
        "SELECT count(*) AS total, "
        "  count(*) FILTER (WHERE seq > %s) AS backlog "
        "FROM chat_messages WHERE user_id = %s",
        (watermark, uid),
    )
    backlog = int(counts["backlog"] or 0)

    # Three witnesses, all required. Stalled-and-behind with no failures is a
    # user who stopped talking; failures with a moving frontier are a problem
    # somewhere else entirely (see usr_90184…, whose frontier was healthy
    # while every turn died in the provider).
    wedged = (
        stalled > 6 and backlog > TAIL_BUDGET_MSGS and coverage_failures > 0
    )
    if wedged:
        print(
            "  since update      "
            + _warn(
                f"frozen {stalled:.1f}h with {backlog} unsummarized and "
                f"{coverage_failures} coverage-shaped failures — compaction is wedged"
            )
        )
    else:
        print(f"  since update      {stalled:.1f}h")
        if failed_jobs and not coverage_failures:
            # Worth saying out loud: it rules compaction out and points the
            # next question at the provider instead.
            print(
                f"                    (frontier is healthy; all {failed_jobs} "
                "failures are non-coverage — look at the provider)"
            )
    print(f"  messages          {counts['total']} total, {backlog} unsummarized")

    segs = _fetch(
        conn,
        "SELECT segment_id, level, coverage_kind, start_seq, end_seq, "
        "  source_message_count, created_at FROM v2_conversation_summary_segments "
        "WHERE user_id = %s ORDER BY created_at DESC LIMIT 3",
        (uid,),
    )
    if segs:
        print(f"  segments (newest {len(segs)}):")
        for s in segs:
            print(
                f"    #{s['segment_id']:<4} L{s['level']} {s['coverage_kind']:<13} "
                f"seq {s['start_seq']}→{s['end_seq']} "
                f"({s['source_message_count']} msgs, {s['created_at']:%m-%d %H:%M})"
            )
    return watermark, backlog


def section_backlog(conn, uid: str, watermark: int, rows_n: int) -> None:
    """Shape of the queue head — where an unfoldable row hides."""
    _head("backlog head")
    rows = _fetch(
        conn,
        "SELECT seq, doc->>'role' AS role, "
        "  (doc ? 'body_key') AS offloaded, "
        "  coalesce((doc->>'body_ct_len')::bigint, 0) AS ext_len, "
        "  length(coalesce(doc->>'body_ct','')) AS ct_len "
        "FROM chat_messages WHERE user_id = %s AND seq > %s "
        "ORDER BY seq LIMIT %s",
        (uid, watermark, rows_n),
    )
    if not rows:
        print("  (no unsummarized messages)")
        return
    blocked = False
    for r in rows:
        size = int(r["ext_len"] or 0) if r["offloaded"] else int(r["ct_len"] or 0)
        tag = " R2-offloaded" if r["offloaded"] else ""
        note = ""
        # This is the wall: `_bounded_compaction_prefix` refuses to skip an
        # oversized head row (skipping would make seq coverage dishonest), so
        # it raises before any provider call and no batch size can help.
        if size > COMPACTION_BATCH_CHARS:
            note = "  " + _warn(f"exceeds batch char budget ({COMPACTION_BATCH_CHARS})")
            blocked = True
        print(f"  {r['seq']:<10} {r['role'] or '?':<10} {size:>9,}B{tag}{note}")
    if blocked:
        print(
            "  → this row can never be folded; before 2026-07-27 it stalled the "
            "frontier permanently, now it should be quarantined and skipped"
        )


def section_trajectory(conn, uid: str) -> None:
    """Event timeline of the newest failed job — timing and repeated sizes."""
    _head("trajectory (newest failed job)")
    job = _one(
        conn,
        "SELECT id FROM agent_jobs WHERE user_id = %s AND status = 'failed' "
        "ORDER BY id DESC LIMIT 1",
        (uid,),
    )
    if not job:
        print("  (no failed jobs)")
        return
    events = _fetch(
        conn,
        "SELECT event_index, event_kind, payload_bytes, created_at "
        "FROM v2_trajectory_events WHERE user_id = %s AND job_id = %s "
        "ORDER BY event_index",
        (uid, job["id"]),
    )
    if not events:
        print(f"  job #{job['id']}: (no trajectory events captured)")
        return
    print(f"  job #{job['id']}:")
    prev_ts = None
    req_sizes: list[int] = []
    for e in events:
        gap = ""
        if prev_ts is not None:
            secs = (e["created_at"] - prev_ts).total_seconds()
            gap = f"  +{secs:.1f}s"
        prev_ts = e["created_at"]
        print(
            f"    {e['event_index']:>3} {e['event_kind']:<26} "
            f"{e['payload_bytes'] or 0:>7}B{gap}"
        )
        if e["event_kind"] == "provider_request":
            req_sizes.append(int(e["payload_bytes"] or 0))
    # Identical request sizes across retries mean the SAME batch was re-sent:
    # the failure is deterministic and self-locking, not a flaky provider.
    if len(req_sizes) > 1 and len(set(req_sizes)) == 1:
        print(
            "  " + _warn(
                f"all {len(req_sizes)} provider_request payloads are identical "
                f"({req_sizes[0]}B) — the same batch is being re-sent every retry"
            )
        )


def section_provider_ledger(conn, uid: str) -> None:
    """Plaintext provider outcomes, V1 and V2 side by side.

    The single most useful question about a broken user is "does their relay
    work at all", and the answer is only convincing as a comparison: the same
    credentials succeeding under one runtime and failing under the other rules
    out the relay itself and points at what we send. V1 rows come from the
    resident consumer's own reports; V2 rows are written server-side (both
    carry `runtime`, absent on the older V1 rows).
    """
    _head("provider ledger (plaintext, V1 vs V2)")
    rows = _fetch(
        conn,
        "SELECT coalesce(doc->>'runtime','v1') AS runtime, "
        "  coalesce(doc->>'lane','-') AS lane, "
        "  doc->>'outcome' AS outcome, count(*) AS n, "
        "  max((doc->>'ts')::float) AS last_ts, "
        "  max((doc->'usage'->>'input_tokens')::int) AS max_in "
        "FROM user_logs WHERE user_id = %s AND stream = 'provider_attempts' "
        "GROUP BY 1,2,3 ORDER BY 1, 4 DESC",
        (uid,),
    )
    if not rows:
        print("  (no provider attempts recorded)")
        return
    for r in rows:
        when = ""
        if r["last_ts"]:
            import datetime as _dt

            when = _dt.datetime.fromtimestamp(
                float(r["last_ts"]), _dt.timezone.utc
            ).strftime("  last %m-%d %H:%M")
        peak = f"  max_in={r['max_in']:,}" if r["max_in"] else ""
        print(
            f"  {r['runtime']:<3} {r['lane']:<14} {r['outcome']:<16} "
            f"{r['n']:>4}{when}{peak}"
        )
    runtimes = {r["runtime"] for r in rows}
    if "v2" not in runtimes:
        print("  (no V2 rows — either never ran V2, or predates the V2 ledger)")


def section_peers(conn, uid: str) -> None:
    """What healthy V2 users look like right now, for contrast."""
    _head("peers (same environment)")
    rows = _fetch(
        conn,
        "SELECT j.user_id, "
        "  count(*) FILTER (WHERE j.status = 'completed') AS ok, "
        "  count(*) AS total, "
        "  (SELECT count(*) FROM chat_messages c WHERE c.user_id = j.user_id) AS msgs "
        "FROM agent_jobs j GROUP BY 1 ORDER BY total DESC LIMIT 8",
        (),
    )
    for r in rows:
        mark = " ← this user" if r["user_id"] == uid else ""
        health = "✅" if r["ok"] == r["total"] else ("❌" if r["ok"] == 0 else "⚠")
        # Full id, never truncated: the whole point of this section is to give
        # the operator the next user_id to run this tool against.
        print(
            f"  {r['user_id']:<22} {r['ok']:>3}/{r['total']:<3} {health} "
            f"({r['msgs']} msgs){mark}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Runtime V2 triage for one user (no decryption).",
    )
    parser.add_argument("--env", required=True, choices=("test", "pre", "prod"))
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--jobs", type=int, default=8, help="recent jobs to list")
    parser.add_argument("--backlog", type=int, default=8, help="backlog head rows")
    args = parser.parse_args(argv)

    if not re.fullmatch(r"usr_[0-9a-f]{4,32}", args.user_id):
        sys.exit("--user-id must look like usr_<hex>")

    print(f"\033[1mRuntime V2 triage — {args.user_id} @ {args.env}\033[0m")
    with psycopg.connect(_dsn(args.env), connect_timeout=15) as conn:
        section_runtime(conn, args.user_id)
        failed_jobs, coverage_failures = section_jobs(
            conn, args.user_id, args.jobs
        )
        section_metrics(conn, args.user_id)
        watermark, backlog = section_summary(
            conn, args.user_id, failed_jobs=failed_jobs,
            coverage_failures=coverage_failures,
        )
        if watermark is not None and backlog:
            section_backlog(conn, args.user_id, watermark, args.backlog)
        section_trajectory(conn, args.user_id)
        section_provider_ledger(conn, args.user_id)
        section_peers(conn, args.user_id)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
