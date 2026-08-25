# Chat incremental sync rollout

This runbook moves cross-worker chat consistency from full resident-store reloads to durable per-user change events and bounded point/window queries. Public API responses and E2EE boundaries do not change.

## Controls

- `FEEDLING_CHAT_SYNC_MODE=legacy`: v2 wakes still perform the bounded full chat snapshot. This is the rollback setting.
- `FEEDLING_CHAT_SYNC_MODE=observe`: production behavior remains legacy; a deterministic 1% user sample also runs incremental sync and compares only `(id, seq)` fingerprints.
- `FEEDLING_CHAT_SYNC_MODE=incremental`: v2 wakes apply contiguous durable events; gaps, resets, missing rows, or oversized batches fall back to the bounded hot snapshot.

Telemetry is content-free: `chat_sync mode=<enum> result=<enum> reason=<enum> user_hash=<sha256-prefix> hot_rows=<n>`. Alert on `result=error`, `result=mismatch`, `chat_sync_observe_mismatch`, and repeated snapshot fallback. Never add event documents, message IDs, ciphertext, keys, or DSNs to these records.

Snapshot fallbacks have a second content-free record:
`chat_sync_snapshot_fallback reason=<enum> user_hash=<sha256-prefix> hot_rows=<n>`.
The only allowed reasons are `gap`, `reset`, `overflow`, `missing_row`, and
`generation_conflict`.

## Notification protocols

| Payload | Producer | Current receiver | Previous production receiver |
| --- | --- | --- | --- |
| `{v:2,c:chat,u,r}` | Committed `chat_messages` trigger | Apply durable events up to `r`; bounded snapshot only on a safety fallback | Apply the same durable events when `incremental` is enabled |
| `{c:chat,u,o,w:1}` | Committed non-Chat state that must end a parked Chat poll | Wake the local waiter only; do not read Chat version, events, or snapshot | Treat as a generic Chat wake and load the bounded hot snapshot |
| `{u,c:chat,o}` | Legacy application producer | In `incremental`, reconcile the current durable version and always wake the waiter | Load the bounded hot snapshot |

First-party Chat mutations must not emit the legacy payload. Their committed DB
trigger is the single cross-worker mutation notification. Runtime status, MCP
configuration, resident activity, and resident vision-test state use the typed
wake-only payload because they change what a Chat poll returns without changing
`chat_messages`.

## Rollout

1. Verify the change-event tables, three `chat_messages` statement triggers, and migration head before changing the image.
2. Keep `FEEDLING_CHAT_SYNC_MODE=incremental` and `FEEDLING_CHAT_HOT_CACHE_LIMIT=256` in TEST. Deploy the new receiver before judging producer convergence; old and new workers are rolling-compatible.
3. Exercise normal Chat, verify, voice, clear, Runtime V2 status, MCP changes, resident activity, vision test, and worker recycle traffic.
4. Require steady-state `reason=legacy_payload` to reach zero after every old instance drains. Require `reason=sync_failed` to remain zero and investigate recurring snapshot fallback reasons.
5. Hold TEST long enough to compare a representative RDS window before production promotion. Repeat the same checks during the production rolling window and after all old instances drain.

Rollback immediately to `legacy` for mismatch growth, missing chat rows, claim/reply anomalies, or listener instability. The durable transcript remains authoritative, so rollback requires no data conversion.

## Verification

- Exercise append, finalize, delete, clear, listener outage/reconnect, direct DB mutation, worker recycle, and mixed legacy/v2 payloads with four workers (`gunicorn ... -w 4`).
- Compare CloudWatch RDS `NetworkTransmitThroughput`, `ReadIOPS`, `DatabaseConnections`, CPU, and burst-credit metrics with matching weekday/hour windows. Do not promise a fixed whole-RDS percentage: the protocol acceptance criterion is that an ordinary Chat mutation performs event/point reads without a second 256-row legacy snapshot.
- If `pg_stat_statements` is installed, use it to confirm bounded event/point/poll/tail queries dominate and full chat snapshots fall sharply. Check query calls, rows, mean time, and shared blocks; do not log SQL parameters. Its absence must be recorded rather than treated as an empty result.
- Keep durable change events long enough for the maximum worker outage/reconnect window. Diagnose a gap by comparing `chat_change_state.version`, cached version, and contiguous `chat_change_events`; a snapshot fallback is safe, but recurring gaps require investigation before rollout continues.
- Count `reason=wake_only`, `reason=legacy_payload`, `reason=sync_failed`, and every `chat_sync_snapshot_fallback` reason. A normal smoke run should create corresponding wake-only records for status/MCP/activity tests, no steady-state legacy records, and no sync failures.
