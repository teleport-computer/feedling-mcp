# Chat incremental sync rollout

This runbook moves cross-worker chat consistency from full resident-store reloads to durable per-user change events and bounded point/window queries. Public API responses and E2EE boundaries do not change.

## Controls

- `FEEDLING_CHAT_SYNC_MODE=legacy`: v2 wakes still perform the bounded full chat snapshot. This is the rollback setting.
- `FEEDLING_CHAT_SYNC_MODE=observe`: production behavior remains legacy; a deterministic 1% user sample also runs incremental sync and compares only `(id, seq)` fingerprints.
- `FEEDLING_CHAT_SYNC_MODE=incremental`: v2 wakes apply contiguous durable events; gaps, resets, missing rows, or oversized batches fall back to the bounded hot snapshot.

Telemetry is content-free: `chat_sync mode=<enum> result=<enum> reason=<enum> user_hash=<sha256-prefix> hot_rows=<n>`. Alert on `result=error`, `result=mismatch`, `chat_sync_observe_mismatch`, and repeated snapshot fallback. Never add event documents, message IDs, ciphertext, keys, or DSNs to these records.

## Rollout

1. Deploy schema and code with `legacy`; verify migrations and mixed legacy/v2 wake payloads.
2. Switch to `observe` for at least 24 hours. Require zero mismatch/gap errors.
3. Enable `incremental` for stable user buckets at 5%, 25%, 50%, then 100%, holding each stage long enough to cover normal chat, verify, voice, clear, and worker recycle traffic.
4. Keep the hot cache at 5,000 rows initially. After stable incremental operation, reduce to 1,024 and then 256 only after each stage passes correctness checks.

Rollback immediately to `legacy` for mismatch growth, missing chat rows, claim/reply anomalies, or listener instability. The durable transcript remains authoritative, so rollback requires no data conversion.

## Verification

- Exercise append, finalize, delete, clear, listener outage/reconnect, direct DB mutation, worker recycle, and mixed legacy/v2 payloads with four workers (`gunicorn ... -w 4`).
- Compare CloudWatch RDS `NetworkTransmit` and daily egress with the pre-rollout baseline. The acceptance target is at least 85% reduction, or below 200–250 GB/day, before reducing the hot cache to 256.
- Use `pg_stat_statements` to confirm bounded event/point/poll/tail queries dominate and full chat snapshots fall sharply. Check query calls, rows, mean time, and shared blocks; do not log SQL parameters.
- Keep durable change events long enough for the maximum worker outage/reconnect window. Diagnose a gap by comparing `chat_user_change_state.version`, cached version, and contiguous `chat_change_events`; a snapshot fallback is safe, but recurring gaps require investigation before rollout continues.
