# Hosted Runtime V2 — Gated Rollout & Kill-Resident Runbook

> Ops runbook (D4 Task 5). Not TDD — a human/ops executes this sequence. Cross-reference `deploy/DEPLOYMENTS.md` for the CVM deploy/re-auth mechanics. Companion code: §6 admission ceiling + D0 rollout infra + D3 proactive/wake lanes + D4 load-test harness (`scripts/loadtest/`).

## What "done" means

RAM and process count become a function of the worker-pool size, not the user count. That win is only realized at the END of this runbook — when the resident per-user consumers are shut down. Everything before that is making V2 safe to switch on.

## Prerequisites (must be deployed first)

- **D0 landed**: worker pool container (`serve-worker`) added to the runner compose; discovery exclusivity guard live (`db.list_agent_runtime_enabled_users` excludes `db_action_v2` users); admin mode-setter (`/v1/admin/hosted-runtime-mode` + `io_cli set-/list-runtime-mode`); per-turn metrics (`v2_turn_metrics` + `/v1/admin/v2-metrics`).
- **D3 landed**: proactive/wake migrated to lanes (scheduler + wake handler). **Full kill-resident cannot happen until D3 is deployed** — before that, `db_action_v2` only reroutes interactive chat; a flipped user's proactive wakes would be lost.
- Both runner composes carry the `serve-worker` service (`deploy/docker-compose.phala.runner.yaml`, `deploy/docker-compose.phala.prod.runner.yaml`).
- **Audit blockers closed first**: do not enroll even an internal user until the cutover cursor, transactional reply boundary, hard wedged-turn recovery, live turn-pool kill switch, and fault-injection/rollback drill in `docs/HOSTED_RUNTIME_V2_AUDIT_HANDOFF_2026-07-11.md` are complete.
- Capture/dream producers are currently default-off. Any early infrastructure soak is capability-incomplete and is not evidence that V2 can replace resident.

## Step 0 — Deploy the worker pool

1. Adding the `serve-worker` service is a **compose change** → new `compose_hash` → **on-chain `addComposeHash()` re-auth** (pre `FeedlingAppAuth 0x6584…`, prod `0x6c8A…`). See `deploy/DEPLOYMENTS.md` "How to re-run the deploy".
2. Build `feedling-agent-runner:<sha>`, bump the compose tag, `phala deploy --cvm-id <runner>`, `deploy/publish-compose-hash.sh`.
3. Env (encrypted channel, no re-auth): `FEEDLING_V2_MAX_WORKERS` (default 4), `FEEDLING_V2_CHAT_RESERVED_SLOTS` (default `max(1, MAX_WORKERS//2)`, clamped so at least one slot remains unrestricted), `FEEDLING_V2_SCHEDULER_INTERVAL_SEC` (default 30). Non-positive/invalid worker, enclave, TTL, poll, and scheduler settings now fail startup.
4. **Verify**: `v2_worker_heartbeats` has fresh rows (`jobs_store.workers_alive()` True); `GET /v1/admin/v2-metrics` returns live_workers ≥ 1.
5. **Verify genesis rehome** (2026-07-10): the genesis import worker now runs inside `serve-worker`, not `agent-runner` — `genesis_import_jobs` has exactly one drain in the codebase, so if this container is unhealthy, every new user's onboarding distillation stalls silently. Confirm `GET /v1/admin/v2-metrics` returns `genesis_alive: true`, then drive **one real genesis import end-to-end** and confirm it decrypts (the runner CVM reaches the main enclave over the passthrough URL with `verify=False`; `deploy/DEPLOYMENTS.md` has always flagged this as a post-cutover check). A `genesis_alive: false` with `live_workers ≥ 1` means the genesis thread died while the turn loops kept beating — check the serve-worker logs for `[genesis:daemon]`.

## Step 1 — Load test (LOCAL, before flipping real users)

Runs locally; RSS/latency are **indicative** on a dev box, not CVM-authoritative. Authoritative numbers, if needed, come from a rerun on a 4c/8GB box near cutover.

1. Start the mock provider: `python scripts/loadtest/mock_provider.py --port 8099 --prompt-tokens 100 --completion-tokens 20 --latency-ms 200`.
2. Drive load: `python scripts/loadtest/run_loadtest.py --users 100 --workers 16` (for the real run, point the driver's processor at a live `serve_worker` pool configured against the mock; the in-CI smoke uses the simulated drain). Collect: queue-wait P95, turn latency, tokens/turn, stuck jobs, RSS.
3. **tokens/turn vs resident (rollback gate)**: the resident baseline is **measured, not assumed** — `python scripts/loadtest/measure_resident.py` (spawns the real `codex` CLI against MockProvider; see `docs/HOSTED_RUNTIME_V2_TOKEN_BASELINE.md`). The current shared-fixture baseline is **9303.0 tokens/turn**. Then run `python scripts/loadtest/compare_tokens.py --resident-baseline 9303.0 --threshold 0.10`. Exit code 1 = regression > +10%; exit code 2 = invalid/non-finite gate input; either means **do not roll out**. On 2026-07-11 the loop-aware command reports **574.0 tokens/turn, 2.3333 LLM calls/turn, -93.83% vs resident**. Re-measure both sides whenever the fixtures, loop, prompts, tools, model, or agent CLI version changes. This offline mock result is a regression gate, not production capacity evidence.
4. Sanity vs the capacity model: 100 users, 16 workers × ~20 s/turn ≈ 50 turns/min → the everyone-at-once spike clears in ~2 min; queue-wait P95 should not contradict this.

## Step 2 — Gated rollout (evidence-first)

1. **Current state: HOLD.** The audit verdict is NO-GO for user flips until the prerequisites above and the handoff's P0 gates are closed. Once they pass, start with one explicitly consented internal user: `io_cli set-runtime-mode <internal_uid> db_action_v2`. Watch `/v1/admin/v2-metrics` (inflight/pending/wake success and current responder-only token samples) + error chips + subjective chat quality for 24–48 h. Treat production token metrics as non-authoritative until whole-turn aggregation lands.
2. **Ramp cohorts**: 5 → 20 → 50 → all. `io_cli list-runtime-mode` to track who's on what. Each batch: confirm tokens/turn not regressed, queue-wait P95 within SLA, `wake.success_rate` healthy, `stuck_jobs ≈ 0`.
3. **Mixed-fleet safety**: the D0 exclusivity guard keeps each user on exactly ONE path — only an explicit `db_action_v2` value enrolls V2; missing/invalid values remain resident. A flipped user runs on V2 and their resident consumer is reaped (~15 s, next supervisor tick); an un-flipped user stays resident. A runtime-control read failure must refuse routing, and a resident-discovery query failure must skip reconciliation rather than look like an empty roster. No double-run and no outage-driven fleet teardown.

## Step 3 — Kill resident (the actual cost win)

- **Automatic**: flipping a user to `db_action_v2` drops them from `db.list_agent_runtime_enabled_users` → `Supervisor.tick()` reaps their consumer next tick and releases the lease. Kill-resident IS the flip; no separate teardown.
- **Precondition per user**: their proactive path is covered by D3 (else wakes are lost). So do NOT broadly flip until D3 is deployed.
- **Fleet retirement**: once all users are `db_action_v2`, the roster is empty. Keep the supervisor running with an empty roster for a while (rollback headroom), then retire it. **This is the moment RAM/process count decouples from user count.**
- **Verify**: `agent_runtime_instances` lease table has no `db_action_v2` users; pool RSS flat; no user appears on both paths.

## Rollback

- **Fastest** (no on-chain re-auth): revert a user's flag — `io_cli set-runtime-mode <uid> resident_cli`. They fall back to the default path; the supervisor re-spawns their resident next tick.
- **Drain before image rollback.** Stop new V2 admission, move enrolled users to `resident_cli`, wait for claimed/running V2 rows to terminalize, and verify no V2 reply is in flight before deploying an older worker image. The schema keeps separate queue and execution deadlines for mixed-version safety, but an old worker does not have the new ownership/effect fences.
- **Do not mistake the CI deploy gate for a live kill switch.** `gh variable set DEPLOY_*_RUNNER_CVM --body false` only prevents future deploy jobs; the already-running CVM keeps serving its last image. Roll back every flipped user first. A true pool-wide stop/drain control that leaves Genesis alive is still an explicit follow-up.
- **Image rollback**: re-pin an older `:<sha>` in the runner compose and redeploy.

## Deferred (explicit, out of this round)

- **Default flip** — do not infer fleet membership from a missing blob. After every gate is complete, backfill existing users to an explicit mode and make onboarding persist its intended mode transactionally; only then consider changing new-user policy. A missing/invalid value remains a safety fallback, not a cutover signal.
- **Authoritative 4c/8GB load run** — rerun Step 1 on CVM-class hardware if the local indicative numbers are borderline.
- **Promoting genesis to its own container.** It is now a dedicated thread inside `serve-worker` rather than inside `agent-runner` — better, but still a thread in someone else's process. Its own service would give it a restart policy and a real crash domain. Costs one more `addComposeHash()`, so it was not bundled here.
