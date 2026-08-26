---
document_lifecycle: current
canonical_owner: self
---

# Metadata-first UserStore rollout

This runbook rolls worker-local per-user state from eager construction to
explicit section loading. PostgreSQL remains authoritative. The public API,
encryption and trust boundaries, RDS-to-TEE replication, selected primary, and
the 256-row Chat hot-cache limit do not change.

The expected production result for this phase is to reduce the current whole-RDS
Network TX baseline of approximately **10.10 MB/s** into the **4–6 MB/s** range.
This phase alone does not claim the final whole-RDS target of **2.64 MB/s**.
Memory, `user_logs`, and Runtime V2 state-query optimization remain separate
follow-up work.

## Controls and invariants

- `FEEDLING_STORE_LOAD_MODE=legacy`: load every section for an ordinary Store;
  deploy the candidate in this mode first and use it for immediate rollback.
- `FEEDLING_STORE_LOAD_MODE=selective`: explicit callers load their declared
  sections while reviewed shell-only callers remain SQL-free at construction.
- `FEEDLING_STORE_LOAD_MODE=lazy`: the final mode; ordinary `get_store()` creates
  only metadata, locks, and waiters.
- `FEEDLING_CHAT_SYNC_MODE=incremental` remains pinned.
- `FEEDLING_CHAT_HOT_CACHE_LIMIT=256` remains pinned.

Promote modes only through reviewed tracked compose changes. Do not edit a
running container. Rollback uses the same artifact and a tracked change back to
`legacy`; no data migration or restore is required.

The following are release-blocking invariants in every mode:

- Chat send, history, poll, finalize, delete, and clear preserve membership,
  order, deduplication, and waiter latency.
- Runtime V2 prompt membership and durable reply cursor behavior are unchanged.
- Cold writes commit and wake without installing a partial local Chat cache.
- A clear concurrent with first load cannot resurrect pre-clear rows.
- An unloaded section receiving NOTIFY executes no section SQL; reconnect wakes
  every cached shell but refreshes only sections that were already loaded.
- Store telemetry never includes raw user IDs, content, ciphertext, keys,
  tokens, private configuration, or database URLs.

## Evidence header

Start every observation window with this block. Use UTC and Asia/Shanghai for
all timestamps so CloudWatch, database, deploy, and smoke evidence can be
correlated.

| Field | Value |
| --- | --- |
| Environment | TEST / PROD |
| Window | legacy / selective / lazy; 1h / 6h / 24h |
| UTC start/end | pending |
| Asia/Shanghai start/end | pending |
| Git commit | pending |
| Image digest/tag | pending |
| Compose/on-chain release identifier | pending |
| Backend/serve-worker count and PIDs | pending |
| Store load mode | pending |
| Chat sync mode / hot-cache limit | `incremental` / `256` |
| Operator and evidence links | pending |

Never paste credentials, DSNs, raw Chat rows, message IDs, or user identifiers
into this document.

## Preflight and local verification

1. Confirm the migration head and the enabled Chat lifecycle triggers.
2. Run the focused PostgreSQL-backed suites with a real local PostgreSQL:

   ```bash
   FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
     .venv-test/bin/python -m pytest \
       tests/test_store_sections.py \
       tests/test_store_cache.py \
       tests/test_store_load_contract.py \
       tests/test_chat_incremental_sync.py \
       tests/test_chat_poll_cross_worker_staleness.py \
       tests/test_store_append_chat_file.py \
       tests/test_v2_chat_clear_fence.py \
       tests/test_wake_bus.py -q
   ```

3. Run the repository test selection, full PostgreSQL-backed suite (excluding
   the separately managed API suite), and documentation checks:

   ```bash
   ~/fleet/bus/which_tests.sh --vs origin/test
   FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
     .venv-test/bin/python -m pytest tests -q --ignore=tests/test_api.py
   cd docs-site
   npm run types:check
   npm run lint
   npm run build
   ```

4. Run a four-worker lazy-mode exercise. Cover append, history, poll, finalize,
   delete, clear, cross-worker read-after-write, one intentionally dropped
   notification, listener reconnect, worker replacement, one first-load DB
   failure followed by retry, and 100 concurrent first Chat consumers. Record
   worker PIDs, fixed-enum Store logs, response status/body hashes, and snapshot
   counts. Shell-only paths must produce zero snapshots; concurrent first use
   must produce exactly one.

## Measurements

Use matching weekday/hour windows and the exact same artifact when comparing
modes.

### CloudWatch

Record average, p50 where available, p95, and maximum for:

- RDS `NetworkTransmitThroughput` (convert bytes/s to MB/s consistently);
- `ReadIOPS`;
- `CPUUtilization`;
- `DatabaseConnections` and burst-credit metrics when applicable;
- API Chat send/history/poll p50, p95, request count, and 5xx count.

### Store load telemetry

The fixed format is:

```text
store_section_load section=<enum> reason=<enum> cache_state=<cold|stale> rows=<n> duration_ms=<n> outcome=<enum>
```

Example CloudWatch Logs Insights query:

```text
fields @timestamp, @message
| filter @message like /store_section_load/
| parse @message /section=(?<section>\S+) reason=(?<reason>\S+) cache_state=(?<cache_state>\S+) rows=(?<rows>\d+) duration_ms=(?<duration_ms>[0-9.]+) outcome=(?<outcome>\S+)/
| stats count(*) as loads, sum(rows) as rows, pct(duration_ms, 95) as p95_ms by section, reason, cache_state, outcome
| sort loads desc
```

Also count `store_section_unavailable`, Chat snapshot-fallback reasons,
`chat_sync ... result=error`, wake-bus reconnect errors, and clear-generation
conflicts. Any new sustained error is a rollback trigger.

### PostgreSQL attribution

Capture `pg_stat_user_tables` at both window boundaries and compute deltas for
`chat_messages`, especially `seq_scan`, `idx_scan`, `n_tup_ins`,
`n_tup_upd`, `n_tup_del`, and `n_live_tup`. Where `pg_stat_statements` is
available, separately record bounded Chat hot-snapshot calls/rows and the total
`chat_messages` rows returned. If an extension or metric is unavailable, write
`unavailable`; never interpret a missing counter as zero.

Acceptance compares deltas, not lifetime counters:

```text
Chat hot snapshots <= 10% of same-version legacy
chat_messages tuple fetch <= 20% of same-version legacy
key-path p95 regression <= 10%
correctness mismatches = 0
clear resurrection = 0
new 5xx = 0
new Store/DB/wake errors = 0
```

## TEST rollout

1. Merge through the repository's `test` branch flow and deploy the exact
   candidate with `legacy`. Run Chat send/history/poll, Runtime V2 prompt
   tail/reply, voice finalize/cancel, perception wake, Genesis, World Book,
   push/live activity, admin eviction, reconnect, and worker recycle.
2. Hold and record the **1h, 6h, and 24h** legacy windows.
3. Commit a tracked switch to `selective`, deploy the same artifact, repeat the
   smoke matrix and the 1h/6h/24h windows.
4. Require zero compatibility calls and all acceptance thresholds, then commit
   a tracked switch to `lazy`. Repeat the same smoke matrix and windows.
5. Append the TEST measurements to the evidence table and stop before
   production promotion. A maintainer promotes only the tested `test` or `pre`
   state to `main`.

## PROD rollout

1. Deploy the exact TEST-validated artifact from `test` or `pre` with
   `legacy`; establish a same-version 1h baseline before changing mode.
2. Promote to `selective` with a reviewed tracked-config commit. Check the full
   smoke matrix and evidence at **1h, 6h, and 24h**.
3. Promote to `lazy` only after selective passes. Repeat the 1h/6h/24h gates.
4. Require the correctness/query/latency criteria above and target whole-RDS
   Network TX of **4–6 MB/s** from the prior approximately **10.10 MB/s**.
5. Record the remaining distance to **2.64 MB/s**. If correctness is stable but
   Network TX remains above 6 MB/s, retain the measured result and open a
   separate attributed design; do not add Memory or user-log work here.

## Rollback

Immediately deploy the tracked `legacy` configuration on any missing,
duplicated, reordered, or resurrected Chat result; new 5xx; first-load retry
failure; sustained Store/DB/wake error; or p95 regression above 10%. Record the
exact trigger, UTC and Asia/Shanghai timestamp, commit/image, affected mode,
smoke result after rollback, and follow-up decision. Keep PostgreSQL data in
place; rollback does not run DDL or data conversion.

## Observation evidence

| Env | Mode | Window | Commit/image | Network TX avg/p95 | ReadIOPS | CPU | Hot snapshots/calls/rows | `chat_messages` tuple-fetch delta | Chat p50/p95/5xx | Correctness/errors | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| TEST | legacy | 1h | pending | pending | pending | pending | pending | pending | pending | pending | pending |

## Local verification evidence — 2026-08-26

This evidence was collected from commit `19e342f8` (the candidate code was
unchanged between the successful run and commit; only formatting followed the
run). No production or shared TEST service was used.

| Field | Value |
| --- | --- |
| Environment | isolated local PostgreSQL + backend |
| Mode | `lazy`; Chat sync `incremental`; hot cache `256` |
| UTC window | 2026-08-26 14:03:26–14:08:30 |
| Asia/Shanghai window | 2026-08-26 22:03:26–22:08:30 |
| Initial backend worker PIDs | `57512`, `57513`, `57514`, `57515` |
| Replacement | `57512` terminated; Gunicorn started `57721` |
| Temporary resources | local database, probe module, and data directory deleted after the run |

Verification results:

- Focused PostgreSQL-backed metadata-first suite: **129 passed**, no DB-backed
  skip.
- Cold-maintenance regression files: **190 passed**.
- Full repository suite excluding `tests/test_api.py`: **11729 passed, 3
  skipped, 9 xfailed, 3 subtests passed** in 507.88 seconds. Exit code 0.
- Post-commit combined core plus previously-failing regression run: **320
  passed** in 16.55 seconds. Exit code 0.
- Documentation verification on the same branch: `types:check`, `lint`, and
  `build` all exited 0; the build rendered 582 pages.
- `~/fleet/bus/which_tests.sh --vs origin/test` was unavailable because the
  script does not exist on this workstation; this is recorded as unavailable,
  not as a pass or zero selection.

Four-worker exercise results:

| Check | Result |
| --- | --- |
| Cold Chat append | all four workers remained unloaded; snapshot calls `0/0/0/0` |
| 100 concurrent `/history` reads | all status 200; shell-only snapshot calls `0/0/0/0` |
| 100 concurrent explicit first Chat loads | request distribution `16/28/19/37`; exactly one snapshot per worker (`1/1/1/1`) |
| First-load failure and retry | injected first load returned 503; same worker later returned 200 |
| Dropped notification / listener recovery | all four LISTEN connections terminated; gap append returned 200; all four listeners reconnected and catch-up refreshed one loaded store each; post-reconnect history returned 200 |
| Worker replacement | replacement worker `57721` started and served history with status 200 |
| Chat lifecycle | append, poll, finalize CAS, delete, clear, and post-clear history returned 200; registration returned 201 |
| Clear invariant | post-clear history was empty; no capture-state resurrection regression in focused/full suites |

Content-free response hashes from the successful run:

| Operation | Status | SHA-256 prefix |
| --- | ---: | --- |
| register | 201 | `a78b11ad9f8cc534` |
| append | 200 | `519e7fab711d1469` |
| poll | 200 | `08c823721de1ae2e` |
| finalize CAS | 200 | `9710a9fdc8795641` |
| delete | 200 | `89eb4fc118661ac7` |
| clear | 200 | `4f7d23820457f990` |
| post-clear history | 200 | `6ee353af0f513545` |
| replacement-worker history | 200 | `4150923790f8adfc` |
| first-load retry | 200 | `5800f0030634e2ef` |

The four-worker run used the real assembled `asgi_app` under Gunicorn. A local
temporary wrapper added only worker-PID headers, snapshot counters, and the
fault-injection/direct-finalize probes. Product append/history/poll/clear paths
were exercised through their public HTTP routes. The finalize CAS was called
through `UserStore.finalize_chat_reply_once` because a standalone local backend
correctly rejects `/v1/chat/response` until an official resident consumer has
completed onboarding; bypassing that independent safety gate was outside this
rollout's scope.
