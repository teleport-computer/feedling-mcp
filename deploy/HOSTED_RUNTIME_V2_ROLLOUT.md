# Hosted Runtime V2 — Gated Rollout & Kill-Resident Runbook

> Ops runbook (D4 Task 5). Not TDD — a human/ops executes this sequence. Cross-reference `deploy/DEPLOYMENTS.md` for the CVM deploy/re-auth mechanics. Companion code: §6 admission ceiling + D0 rollout infra + D3 proactive/wake lanes + D4 load-test harness (`scripts/loadtest/`).

## What "done" means

RAM and process count become a function of the worker-pool size, not the user count. That win is only realized at the END of this runbook — when the resident per-user consumers are shut down. Everything before that is making V2 safe to switch on.

## Prerequisites (must be deployed first)

- **D0 landed**: worker pool container (`serve-worker`) added to the runner compose; discovery exclusivity guard live (`db.list_agent_runtime_enabled_users` excludes `db_action_v2` users); admin mode-setter (`/v1/admin/hosted-runtime-mode` + `io_cli set-/list-runtime-mode`); per-turn metrics (`v2_turn_metrics` + `/v1/admin/v2-metrics`).
- **D3 landed**: proactive/wake migrated to lanes (scheduler + wake handler). **Full kill-resident cannot happen until D3 is deployed** — before that, `db_action_v2` only reroutes interactive chat; a flipped user's proactive wakes would be lost.
- Both runner composes carry the `serve-worker` service (`deploy/docker-compose.phala.runner.yaml`, `deploy/docker-compose.phala.prod.runner.yaml`).

## Step 0 — Deploy the worker pool

1. Adding the `serve-worker` service is a **compose change** → new `compose_hash` → **on-chain `addComposeHash()` re-auth** (pre `FeedlingAppAuth 0x6584…`, prod `0x6c8A…`). See `deploy/DEPLOYMENTS.md` "How to re-run the deploy".
2. Build `feedling-agent-runner:<sha>`, bump the compose tag, `phala deploy --cvm-id <runner>`, `deploy/publish-compose-hash.sh`.
3. Env (encrypted channel, no re-auth): `FEEDLING_V2_MAX_WORKERS` (default 4), `FEEDLING_V2_CHAT_RESERVED_SLOTS` (default `max(1, MAX_WORKERS//2)`), `FEEDLING_V2_SCHEDULER_INTERVAL_SEC` (default 30).
4. **Verify**: `v2_worker_heartbeats` has fresh rows (`jobs_store.workers_alive()` True); `GET /v1/admin/v2-metrics` returns live_workers ≥ 1.

## Step 1 — Load test (LOCAL, before flipping real users)

Runs locally; RSS/latency are **indicative** on a dev box, not CVM-authoritative. Authoritative numbers, if needed, come from a rerun on a 4c/8GB box near cutover.

1. Start the mock provider: `python scripts/loadtest/mock_provider.py --port 8099 --prompt-tokens 100 --completion-tokens 20 --latency-ms 200`.
2. Drive load: `python scripts/loadtest/run_loadtest.py --users 100 --workers 16` (for the real run, point the driver's processor at a live `serve_worker` pool configured against the mock; the in-CI smoke uses the simulated drain). Collect: queue-wait P95, turn latency, tokens/turn, stuck jobs, RSS.
3. **tokens/turn vs resident (rollback gate)**: `python scripts/loadtest/compare_tokens.py --resident-baseline <tokens/turn measured on the resident runtime for the same fixtures> --threshold 0.10`. Exit code 1 = regression > +10% = **do not roll out** (investigate prompt-shape/caching drift first).
4. Sanity vs the capacity model: 100 users, 16 workers × ~20 s/turn ≈ 50 turns/min → the everyone-at-once spike clears in ~2 min; queue-wait P95 should not contradict this.

## Step 2 — Gated rollout (evidence-first)

1. **Internal first**: `io_cli set-runtime-mode <internal_uid> db_action_v2`. Watch `/v1/admin/v2-metrics` (inflight/pending/wake success/tokens-per-turn) + error chips + subjective chat quality for 24–48 h.
2. **Ramp cohorts**: 5 → 20 → 50 → all. `io_cli list-runtime-mode` to track who's on what. Each batch: confirm tokens/turn not regressed, queue-wait P95 within SLA, `wake.success_rate` healthy, `stuck_jobs ≈ 0`.
3. **Mixed-fleet safety**: the D0 exclusivity guard keeps each user on exactly ONE path — a flipped user runs on V2 and their resident consumer is reaped (~15 s, next supervisor tick); an un-flipped user stays resident. No double-run.

## Step 3 — Kill resident (the actual cost win)

- **Automatic**: flipping a user to `db_action_v2` drops them from `db.list_agent_runtime_enabled_users` → `Supervisor.tick()` reaps their consumer next tick and releases the lease. Kill-resident IS the flip; no separate teardown.
- **Precondition per user**: their proactive path is covered by D3 (else wakes are lost). So do NOT broadly flip until D3 is deployed.
- **Fleet retirement**: once all users are `db_action_v2`, the roster is empty. Keep the supervisor running with an empty roster for a while (rollback headroom), then retire it. **This is the moment RAM/process count decouples from user count.**
- **Verify**: `agent_runtime_instances` lease table has no `db_action_v2` users; pool RSS flat; no user appears on both paths.

## Rollback

- **Fastest** (no on-chain re-auth): revert a user's flag — `io_cli set-runtime-mode <uid> resident_cli`. They fall back to the default path; the supervisor re-spawns their resident next tick.
- **Pool off**: `gh variable set DEPLOY_*_RUNNER_CVM --body false` → runner-CVM job goes dormant, CVM keeps its last image; the send guard 503s `workers_unavailable` for still-flipped users until you also revert their flags.
- **Image rollback**: re-pin an older `:<sha>` in the runner compose and redeploy.

## Deferred (explicit, out of this round)

- **Default flip** (changing the `resident_cli` fallback default) — a separate one-line change once the fleet is stable and fully migrated; easy to revert.
- **Authoritative 4c/8GB load run** — rerun Step 1 on CVM-class hardware if the local indicative numbers are borderline.
- **capture lane** wake execution (D3 scoped to heartbeat/scheduled/manual_wake; capture = memory-extraction, follow-up).
