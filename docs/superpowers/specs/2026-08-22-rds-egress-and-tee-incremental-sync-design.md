# RDS Egress and TEE Incremental Sync Design

**Date:** 2026-08-22
**Status:** Approved in conversation
**Scope:** Production PostgreSQL read amplification, cross-worker chat freshness, and TEE/plaintext-shadow synchronization

## 1. Context and evidence

The production RDS database contains about 4.47 GiB of logical data, but recent RDS network transmit is roughly 1–2 TiB per day. August Cost Explorer attributes about USD 2,207 of data-transfer cost to the production instance. The dominant active SQL repeatedly loads the newest 5,000 chat rows for one user:

```sql
SELECT doc FROM (
  SELECT seq, doc
  FROM chat_messages
  WHERE user_id = $1
  ORDER BY seq DESC
  LIMIT $2
) recent
ORDER BY seq ASC;
```

Production evidence also shows approximately 71.3 billion cumulative `chat_messages` index tuple fetches. A short sample observed about 34,249 index fetches per second and approximately 22.5 MB/s of database network transmit.

The amplification comes from several interacting behaviors:

1. Any `chat`, `proactive`, `frames`, or `blob` wake calls `core.store._evict_store()`, which reloads the complete cached `UserStore`.
2. A chat reload reads up to `MAX_CHAT_MESSAGES=5000`, even if one row changed.
3. `verify_loop` reloads the complete Store every two seconds while waiting for one synthetic reply.
4. Runtime V2 tail construction reloads up to 5,000 rows before selecting its much smaller prompt tail.
5. TEE synchronization performs full SNAPSHOT-table COPY work every normal tick even though those tables now contain tens of thousands of rows.
6. TEE verification includes full per-user aggregation and random-order sampling.

The problem is read and replication amplification, not excessive durable storage. This design does not shorten transcript retention or delete user history.

## 2. Goals

- Preserve public chat, history, resident polling, Runtime V2, and verify-loop behavior.
- Make cross-worker synchronization proportional to actual changed rows.
- Treat PostgreSQL as the durable source of truth; treat `LISTEN/NOTIFY` only as a low-latency hint.
- Preserve correctness when notifications are lost, duplicated, reordered, or delivered during mixed-version rolling deploys.
- Reduce the process-local chat hot window without reducing history or model prompt coverage.
- Make TEE synchronization key-level during normal operation.
- Retain explicit full snapshot/reconcile as bootstrap and disaster-recovery tools.
- Provide observable correctness gates and reversible feature-flag cutovers.

## 3. Non-goals

- No Redis, external message broker, or new infrastructure service.
- No public API contract change.
- No change to chat retention, account-clear semantics, encryption boundaries, or TEE trust assumptions.
- No replacement of PostgreSQL as the durable chat source.
- No removal of cursor replication or full snapshot recovery commands.

## 4. Architecture decision

Use a durable per-user chat version and compact change-event log. PostgreSQL statement-level triggers record affected message identifiers in the same transaction as each `chat_messages` mutation and issue a version-only `NOTIFY`. Workers apply exact point updates when events are continuous and rebuild only the chat hot window when they encounter a reset or retained-event gap.

For TEE synchronization, make the existing durable dirty-key outbox the real-time path for CIPHERTEXT, MIRROR, and SNAPSHOT lanes. Change SNAPSHOT dirty-key handling from full-table replacement to batched key-level current-state reconciliation. Full snapshots remain explicit recovery operations.

## 5. Chat change-control schema

Add primary-database migration `0098_chat_change_events` with:

```sql
CREATE TABLE chat_change_state (
    user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    version BIGINT NOT NULL CHECK (version >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE chat_change_events (
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    version BIGINT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('upsert', 'delete', 'reset')),
    message_ids TEXT[] NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, version)
);
```

Install separate AFTER STATEMENT INSERT, UPDATE, and DELETE triggers using transition relations. For each affected user and statement:

- increment `chat_change_state.version` once;
- insert one event;
- use `upsert` for INSERT/UPDATE;
- use `delete` for DELETE affecting at most 64 message IDs;
- use `reset` with an empty ID array for a larger mutation;
- call transactional `pg_notify` with `{v:2,c:"chat",u:<user>,r:<version>}`.

The trigger records changes only while the corresponding `users` row still exists. A `users` deletion already removes all cached business state and cascades through `chat_messages`; its internal cascade must not recreate change-control rows or fail its foreign-key checks.

No `doc` or ciphertext is placed in the event table or notification. Trigger DDL is additive. The two change-control tables are registered as `SKIP + required_in_tee`: their RDS history is not replicated because versions are meaningful only within one primary database's write sequence, while TEE migration `0034_chat_poll_index` installs the same tables, triggers, and poll index so a promoted TEE primary produces its own local sequence.

## 6. Chat worker consistency model

`UserStore` gains:

```python
chat_version: int
chat_max_seq: int
chat_messages_by_id: dict[str, dict]
```

Initial chat load reads the current version and newest hot rows inside one read-only repeatable-read transaction. A later commit therefore either belongs to that snapshot or produces a higher version that reconciliation observes.

The new `ensure_chat_fresh()` algorithm:

1. Read `chat_change_state.version`, coalesced locally for at most one second.
2. If it equals `store.chat_version`, return without reading chat rows.
3. Fetch events after the local version, in version order, with a limit of 256.
4. If versions are continuous, batch-fetch current rows for all `upsert` IDs, remove `delete` IDs, preserve sequence ordering, and trim the hot window.
5. If any event is `reset`, the event sequence has a gap, or more than 256 events are pending, reload only the chat hot window and advance to the snapshot version.
6. Wake local chat waiters only after the cache has reached the target version.

Version/event state provides correctness. NOTIFY reduces latency but is not relied on for replay.

## 7. Mixed-version compatibility

- Old workers accept v2 notifications because the existing dispatch reads `c` and `u` and ignores additional chat keys.
- New workers receiving a legacy `{c,u,o}` notification rebuild only the chat hot window.
- New workers receiving v2 reconcile durable events.
- Local writes continue updating the local cache immediately. Reprocessing the trigger notification is idempotent.
- Existing application-side chat notifications remain during the observe phase and are removed only after all workers understand v2 events.

## 8. Query-path separation

Replace the single `MAX_CHAT_MESSAGES` responsibility with:

```text
CHAT_HOT_CACHE_LIMIT=256
CHAT_HISTORY_PAGE_LIMIT_MAX=200
CHAT_INCREMENTAL_BATCH_MAX=256
CHAT_CHANGE_EVENT_IDS_MAX=64
```

The hot cache serves immediate local state only. Durable history remains unbounded and directly paged by sequence. The public history limit and response shape remain unchanged.

Resident poll candidate discovery moves from scanning the hot cache to a bounded database query covering new rows plus the existing one-hour redelivery window. The existing database CAS remains the authoritative claim and superseded-tail decision.

Runtime V2 prompt-tail reads use the existing durable `seq` primitives and `through_seq` snapshot boundary directly. The exact prompt membership remains unchanged; only the unnecessary preceding 5,000-row reload is removed.

## 9. Verify-loop behavior

Independent resident verification keeps the same request and response contract and the same maximum timeout. It changes internally to:

1. append one synthetic ping;
2. wait on the per-user chat change event;
3. point-read the ping by `(user_id,msg_id)`;
4. if it has a reply pointer, point-read that reply and validate role, source, parent ID, and timestamp;
5. perform a two-second point-query fallback when no notification arrives;
6. delete only this invocation's ping and matching reply.

Hosted Runtime V2 heartbeat verification remains unchanged. Concurrent independent verify requests no longer delete each other's synthetic rows.

## 10. TEE key-level synchronization

The dirty-key outbox remains generation-safe and current-state based. Change SNAPSHOT handling from:

```python
snapshot.snapshot_table(table)
```

to:

```python
snapshot.reconcile_keys(table, keys, target_policy=policy)
```

For each table batch:

- read the current source rows for the claimed primary keys;
- batch UPSERT rows that still exist;
- delete target rows whose claimed keys no longer exist at the source;
- commit target mutations atomically;
- acknowledge each dirty generation only after success;
- leave failures for lease expiry, retry, and eventual quarantine.

Process tables in registry-derived parent-first order. A child FK failure remains pending and retries after its parent converges.

CIPHERTEXT dirty keys continue through `tee_replicator.worker.run_keys`; MIRROR dirty keys continue through `tee_shadow.reconciler.reconcile_keys`. Cursor replication remains a lower-frequency backfill and missed-trigger safety net.

## 11. TEE scheduler and verification

Normal operation becomes:

- every 30 seconds: audit triggers, drain dirty keys, probe target, and record bounded metrics;
- every 5 minutes: run bounded CIPHERTEXT cursor replication only;
- hourly: inspect backlog, watermark, quarantine, and a small deterministic key sample;
- daily: verify one deterministic hash bucket;
- weekly or manual recovery: allow budgeted full reconcile/snapshot.

Remove unconditional full snapshot work from normal `_sync_tick()` and remove the post-replicate snapshot retry. Full snapshot remains available through the existing confirmed admin action.

Replace `ORDER BY random()` with stable hash-bucket key selection followed by identical primary-key reads on source and target. Compare normalized existence and content hashes. A seven-bucket daily audit covers the complete key space weekly without random full-table sorts.

## 12. Observability and privacy

Add bounded metrics for chat sync mode/result, rows applied, version lag, event gaps, reset fallbacks, hot rows, verify lookups, poll candidates, dirty-key backlog/age, per-table apply results, sample mismatches, rolling-audit progress, and full-snapshot invocation reason.

Logs may contain table, version, row counts, duration, and fixed reason slugs. User identifiers must be hashed. Logs and metrics must not contain chat documents, ciphertext, API keys, connection strings, or TEE endpoint secrets.

## 13. Rollout

### Release 1: foundation and observe

- Apply additive chat migration and indexes.
- Deploy v1/v2 notification parsing and observe-mode reconciliation.
- Enable channel-specific Store refresh.
- Enable verify point lookup and exact cleanup.
- Keep hot cache at 5,000 and TEE in legacy mode.
- Require 24 hours with zero event gaps, observe mismatches, or trigger errors.

### Release 2: chat cutover

- Enable incremental sync by stable user hash: 5%, 25%, 50%, 100%.
- Switch resident poll and Runtime V2 tail reads to direct bounded queries.
- Reduce hot cache 5,000 → 1,024 → 256 after incremental sync reaches 100%.
- Roll back with chat mode and cache-size flags; keep additive schema.

### Release 3: TEE cutover

- Deploy key-level SNAPSHOT reconciliation in observe mode.
- Enable incremental apply after comparison is clean.
- Drain backlog and run strict audit.
- Disable automatic full snapshots.
- Enable deterministic daily rolling audit.
- Roll back by restoring legacy scheduling for affected tables; preserve dirty markers.

## 14. Acceptance criteria

Correctness gates:

- no missing or duplicate cross-worker messages;
- no dangling reply pointers;
- clear does not allow old chat to reappear;
- resident claim/redelivery and Runtime V2 prompt membership match the legacy implementation;
- history sequence pagination remains exact;
- verify supports concurrency and cleans only its own rows;
- dirty trigger audit is green, quarantine is zero, and sampled/rolling TEE mismatch is zero.

Performance and cost gates:

- the 5,000-row chat reload is absent from production top queries;
- chat index-fetch growth falls by at least 90% from the measured baseline;
- first full day after chat cutover is below 200–250 GB of RDS NetworkTransmit or at least 85% below baseline;
- expected final steady state is below 50 GB/day, treated as a target rather than a single-release blocker;
- normal TEE ticks report zero snapshot-copied rows;
- normal scheduler duration returns from minutes to seconds;
- request p95 does not regress by more than 10%.

## 15. Documentation

Update `docs-site/content/docs/architecture.mdx`, the relevant self-hosting/trust documentation, `docs-site/content/docs/changelog.mdx`, and an operator runbook covering flags, rollback, bootstrap, quarantine, and audit. The public API contract is unchanged, so no OpenAPI schema change is expected; contract tests and the documentation type-check, lint, and build remain required.
