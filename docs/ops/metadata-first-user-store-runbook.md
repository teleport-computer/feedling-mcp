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

