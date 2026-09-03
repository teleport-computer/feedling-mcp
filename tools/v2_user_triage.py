#!/usr/bin/env python3
"""One-shot Runtime V2 triage for a single user — read-only, no decryption.

The tool reports content-free runtime, job, metrics, summary-frontier,
trajectory, provider-ledger, and peer metadata.  It keeps operator-visible
facts separate from causal diagnosis so historical failure shapes do not get
mistaken for the current runtime contract.

Usage:
    python tools/v2_user_triage.py --env prod --user-id usr_7f30d63fb7edb61b
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
from datetime import datetime

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - operator convenience
    sys.exit("psycopg is required: pip install 'psycopg[binary]'")


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PROMPT_COVERAGE_ERROR = "turn_failed:prompt_coverage_incomplete"
# 超过这个天数没有新 job，就提醒操作者「你可能连错库了」。这三个环境都在
# 持续被 P0/真实流量写，静置一整天本身就值得看一眼。
_STALE_DB_DAYS = 1.0


def _is_prompt_coverage_error(error: str) -> bool:
    return error == PROMPT_COVERAGE_ERROR or error.startswith(
        f"{PROMPT_COVERAGE_ERROR}:"
    )


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


def db_freshness(conn) -> dict:
    """这个库最后一次被写是什么时候 —— 用来判「我连对库了吗」。

    2026-08-30 实测:`--env test` 读 .env 的 TEST_DATABASE_URL 指向一个
    2026-08-18 就停更的旧 RDS(users 75 条、max(agent_jobs.id)=6229)，而
    test-api.feedling.app 当天实际写入的 job id 是 10141-10199，命中 0。
    那时的输出是 "user not found in this environment" —— 与「账号被删了」
    完全同形，且失明方向偏向「证据没了」，最容易让人直接放弃追查。
    """
    facts: dict = {}
    for label, sql in (
        ("users_total", "SELECT count(*) AS v FROM users"),
        ("users_newest", "SELECT max(created_at) AS v FROM users"),
        ("jobs_max_id", "SELECT max(id) AS v FROM agent_jobs"),
        ("jobs_newest", "SELECT max(created_at) AS v FROM agent_jobs"),
    ):
        try:
            row = _one(conn, sql, ())
            facts[label] = row["v"] if row else None
        except Exception as e:  # noqa: BLE001 — 自检坏了也不该挡住正常分诊
            facts[label] = f"<unavailable: {type(e).__name__}>"
    return facts


def _freshness_line(facts: dict) -> str:
    return (f"users={facts.get('users_total')} newest_user={facts.get('users_newest')} "
            f"max(agent_jobs.id)={facts.get('jobs_max_id')} "
            f"newest_job={facts.get('jobs_newest')}")


def section_environment(conn, env: str) -> dict:
    """先报「我连的是哪个库、它有多新」，再报单个用户的事。"""
    _head("environment")
    facts = db_freshness(conn)
    print(f"  env               {env}")
    print(f"  db freshness      {_freshness_line(facts)}")
    newest = facts.get("jobs_newest")
    if isinstance(newest, datetime):
        reference = datetime.now(newest.tzinfo) if newest.tzinfo else datetime.now()
        stale_days = (reference - newest).total_seconds() / 86400.0
        if stale_days > _STALE_DB_DAYS:
            print(_warn(
                f"这个库最后一次写入在 {stale_days:.1f} 天前 —— 很可能不是你要的环境。"
                f" 先核对 {env.upper()}_DATABASE_URL 指向的实例，"
                f"再相信下面任何「找不到」的结论。"))
    return facts


def section_runtime(conn, uid: str, freshness: dict | None = None) -> None:
    _head("runtime")
    user = _one(conn, "SELECT created_at FROM users WHERE user_id = %s", (uid,))
    if not user:
        # 「找不到这个用户」和「连错库了」在旧输出里同形。把库的新鲜度贴进
        # 同一句话，读的人才可能分辨这两件事。
        sys.exit(
            f"user {uid} not found in this database.\n"
            f"  db freshness: {_freshness_line(freshness or {})}\n"
            "  这句话有两种可能:①这个账号确实不存在/已删 "
            "②连的不是这个环境实际在写的库。\n"
            "  在把它当成①之前，先确认上面的 max(agent_jobs.id) "
            "与你预期的量级一致。")
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
    """Print job outcomes; return (failed_total, coverage_failures).

    Only the dedicated ``prompt_coverage_incomplete`` code contributes to the
    second value. Generic responder failures remain visible but unclassified.
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
        # Only the dedicated failure code is evidence of incomplete prompt
        # coverage.  The generic responder bucket has many unrelated causes.
        if _is_prompt_coverage_error(row["e"]):
            detail = row["e"].removeprefix(PROMPT_COVERAGE_ERROR).removeprefix(":")
            message = "prompt coverage incomplete"
            if detail:
                message += f": {detail}"
            print("        " + _warn(message))
        elif row["e"] == "turn_failed:responder_error":
            print("        (generic responder bucket — not classified as coverage)")

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
    coverage_failures = sum(
        r["n"] for r in errs
        if _is_prompt_coverage_error(r["e"])
    )
    return failed, coverage_failures


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

    Report stored facts without guessing the live tail budget.  The deployed
    value is environment-specific, so this offline tool cannot infer that a
    motionless watermark is wedged from backlog size alone.
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

    print(f"  since update      {stalled:.1f}h")
    if coverage_failures:
        print(
            "                    "
            + _warn(f"{coverage_failures} exact prompt coverage failure(s) "
                    f"with {backlog} unsummarized messages")
        )
    elif failed_jobs:
        print(
            f"                    (all {failed_jobs} failures are non-coverage; "
            "inspect their exact error codes)"
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
    if len(req_sizes) > 1 and len(set(req_sizes)) == 1:
        print(
            f"  all {len(req_sizes)} provider_request events have the same recorded "
            f"size ({req_sizes[0]}B); size alone does not establish identical requests"
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
    args = parser.parse_args(argv)

    if not re.fullmatch(r"usr_[0-9a-f]{4,32}", args.user_id):
        sys.exit("--user-id must look like usr_<hex>")

    print(f"\033[1mRuntime V2 triage — {args.user_id} @ {args.env}\033[0m")
    with psycopg.connect(_dsn(args.env), connect_timeout=15) as conn:
        freshness = section_environment(conn, args.env)
        section_runtime(conn, args.user_id, freshness)
        failed_jobs, coverage_failures = section_jobs(
            conn, args.user_id, args.jobs
        )
        section_metrics(conn, args.user_id)
        section_summary(
            conn, args.user_id, failed_jobs=failed_jobs,
            coverage_failures=coverage_failures,
        )
        section_trajectory(conn, args.user_id)
        section_provider_ledger(conn, args.user_id)
        section_peers(conn, args.user_id)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
