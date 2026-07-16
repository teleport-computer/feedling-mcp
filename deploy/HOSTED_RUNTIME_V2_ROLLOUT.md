# Hosted Runtime V2 — Gated Rollout & Kill-Resident Runbook

> Ops runbook (D4 Task 5). Not TDD — a human/ops executes this sequence. Cross-reference `deploy/DEPLOYMENTS.md` for the CVM deploy/re-auth mechanics. Companion code: §6 admission ceiling + D0 rollout infra + D3 proactive/wake lanes + D4 load-test harness (`scripts/loadtest/`).

## What "done" means

RAM and process count become a function of the worker-pool size, not the user count. That win is only realized at the END of this runbook — when the resident per-user consumers are shut down. Everything before that is making V2 safe to switch on.

### Pre acceptance policy (2026-07-15)

`pre` is now the explicit V2 acceptance environment. Its backend defaults
`FEEDLING_HOSTED_RUNTIME_POLICY=v2_only`, synchronously backfills every active,
tested, supported account before serving, and persists V2 ownership after fresh
setup/test/activation. Requests fail closed on a policy mismatch; they never
self-repair into a delete race or fall through to resident. The Pre runner deploy
and its build-specific worker/Genesis/policy-coverage gate are mandatory for every
CVM-affecting `pre` push. Therefore an iOS tester only selects Pre, configures a
model, and chats—there is no per-user flip. This is a bounded acceptance soak,
not permission to apply the same fleet policy to test or production.

## Prerequisites (must be deployed first)

- **D0 landed**: worker pool container (`serve-worker`) added to the runner compose; discovery exclusivity guard live (`db.list_agent_runtime_enabled_users` excludes `db_action_v2` users); admin mode-setter (`/v1/admin/hosted-runtime-mode` + `io_cli set-/list-runtime-mode`); per-turn metrics (`v2_turn_metrics` + `/v1/admin/v2-metrics`).
- **D3 landed**: proactive/wake migrated to lanes (scheduler + wake handler). **Full kill-resident cannot happen until D3 is deployed** — before that, `db_action_v2` only reroutes interactive chat; a flipped user's proactive wakes would be lost.
- Both runner composes carry the `serve-worker` service (`deploy/docker-compose.phala.runner.yaml`, `deploy/docker-compose.phala.prod.runner.yaml`).
- Apply Alembic through `0038_v2_prompt_cache_metrics` before starting the new
  worker image. Old workers ignore the additive columns; the new worker keeps
  cache telemetry nullable so an older relay cannot masquerade as a zero-hit
  provider.
- **Production-promotion gate**: the branch now contains the stable cutover cursor, transactional reply boundary, hard wedged-turn recovery, and database-backed live turn-pool kill switch. Before copying Pre's fleet policy to test or production, the deployed image must also pass the fault-injection/rollback drill in `docs/HOSTED_RUNTIME_V2_AUDIT_HANDOFF_2026-07-11.md`. Ordinary Pre iOS use is the bounded acceptance test that gathers evidence; it does not require the tester to run that operator drill.
- **Before promotion, prove process recovery, not only Python recovery.** `_run_forever` relaunches
  `_serve` after an escaping Python exception, and the send heartbeat/deadline
  guards turn a dead pool into a visible error. A SIGKILL/OOM of the worker PID
  cannot relaunch itself. The prior `docker kill serve-worker` observation is
  not evidence against the compose restart policy: Docker treats that as a
  manual stop and suppresses policy-driven restart. Before the first canary,
  cause an unexpected PID-1 death from inside the container (for example,
  `docker exec serve-worker sh -c 'kill -KILL 1'`) and require the container to
  restart, a fresh worker identity/heartbeat to appear, and pending work to
  terminalize or resume exactly once. If that drill fails, install and retest
  an external liveness repair (or equivalent parent supervisor); an alert
  without automatic repair is not a completed availability gate. This drill is
  an operator-owned promotion requirement, not an iOS Pre-testing step.
- Capture/dream producers are currently default-off. Any early infrastructure soak is capability-incomplete and is not evidence that V2 can replace resident.
- Web results are untrusted: after `web_search`/`web_fetch` returns, the native loop removes every durable-write tool and blocks new searches/fresh outbound URLs for the remainder of that turn. A `web_search` result may be fetched once only by its exact returned URL; search-and-save still needs a fresh user turn. Do not widen that boundary without per-result taint tracking, outbound data-loss controls, and an explicit confirmation protocol.

## Step 0 — Deploy the worker pool

1. Adding the `serve-worker` service is a **compose change** → new `compose_hash` → **on-chain `addComposeHash()` re-auth** (pre `FeedlingAppAuth 0x6584…`, prod `0x6c8A…`). See `deploy/DEPLOYMENTS.md` "How to re-run the deploy".
2. Build `feedling-agent-runner:<sha>`, bump the compose tag, `phala deploy --cvm-id <runner>`, `deploy/publish-compose-hash.sh`.
3. Env (encrypted channel, no re-auth): `FEEDLING_V2_MAX_WORKERS` (default 4), `FEEDLING_V2_CHAT_RESERVED_SLOTS` (default `max(1, MAX_WORKERS//2)`, clamped so at least one slot remains unrestricted), `FEEDLING_V2_SCHEDULER_INTERVAL_SEC` (default 30). Watchdog budgets are deliberately split: `FEEDLING_V2_TURN_STALL_TIMEOUT_SEC` defaults to 240s (minimum 210s), `FEEDLING_V2_TURN_ABSOLUTE_TIMEOUT_SEC` defaults to 1500s and must cover the configured prompt-catch-up/provider envelope (1440s minimum at current defaults), `FEEDLING_V2_CHILD_LIVENESS_TIMEOUT_SEC` defaults to 45s, and `FEEDLING_V2_WATCHDOG_DB_TIMEOUT_SEC` defaults to 5s. **Remove any existing `FEEDLING_V2_TURN_HARD_TIMEOUT_SEC=180` override before deploy**: the legacy key is now only a stall-timeout alias, and values below 210s intentionally fail startup. Do not set the absolute budget back to 180s; legitimate `FEEDLING_V2_PROMPT_CATCHUP_DEADLINE_SEC=600` catch-up plus bounded provider rounds can exceed it while still making progress. `serve-worker` automatically sizes its process-local `FEEDLING_DB_POOL_MAX_SIZE` floor to `max(16, 2*MAX_WORKERS+4)` because every generation-fenced effect drain may hold one outer transaction while its sink borrows a second connection; an explicit lower override fails startup instead of deadlocking all slots. Non-positive/invalid worker, pool, enclave, TTL, poll, scheduler, and watchdog settings fail startup.
4. **Verify**: `v2_worker_heartbeats` has fresh rows (`jobs_store.workers_alive()` True); `GET /v1/admin/v2-metrics` returns live_workers ≥ 1.
5. **Verify prompt caching with a real provider**: record a start epoch, send
   exactly two canary-user requests whose exact shared prefix is comfortably
   above that model's minimum, then record an end epoch. First query with
   `cache_provider`, `cache_model`, `cache_user_id`, `cache_since_ts`, and
   `cache_until_ts`; require `sampled_turns == 2`,
   `route_identity_coverage == 1`, and `route_fingerprint_count == 1`. Repeat
   the query with the returned opaque `route_fingerprint` supplied as
   `cache_route_fingerprint`. The bounded user/time query includes a
   chronological `turns` list: require exactly two successful rows,
   `turns[0].model_calls == turns[1].model_calls == 1` (use a simple no-tool
   prompt), and
   `turns[1].cache_read_tokens > 0`. A non-zero aggregate is insufficient: it
   could be a hit in the first turn or a later tool round inside that turn. Do not accept an
   unfiltered fleet aggregate, an open-ended time window, or another user's
   traffic as proof for one route. Also confirm `cache_telemetry_coverage`: a
   missing metric is `null`/unreported, never a fabricated zero. Exercise one
   relay that rejects cache fields and verify the bounded cache-off retry still
   preserves the native tool catalog.
6. **Verify genesis rehome** (2026-07-10): the genesis import worker now runs inside `serve-worker`, not `agent-runner` — `genesis_import_jobs` has exactly one drain in the codebase, so if this container is unhealthy, every new user's onboarding distillation stalls silently. Confirm `GET /v1/admin/v2-metrics` returns `genesis_alive: true`, then drive **one real genesis import end-to-end** and confirm it decrypts (the runner CVM reaches the main enclave over the passthrough URL with `verify=False`; `deploy/DEPLOYMENTS.md` has always flagged this as a post-cutover check). A `genesis_alive: false` with `live_workers ≥ 1` means the genesis thread died while the turn loops kept beating — check the serve-worker logs for `[genesis:daemon]`.

## Step 1 — Load test (LOCAL, before flipping real users)

Runs locally; RSS/latency are **indicative** on a dev box, not CVM-authoritative. Authoritative numbers, if needed, come from a rerun on a 4c/8GB box near cutover.

1. Start the mock provider: `python scripts/loadtest/mock_provider.py --port 8099 --prompt-tokens 100 --completion-tokens 20 --latency-ms 200`.
2. Drive load: `python scripts/loadtest/run_loadtest.py --users 100 --workers 16` (for the real run, point the driver's processor at a live `serve_worker` pool configured against the mock; the in-CI smoke uses the simulated drain). Collect: queue-wait P95, turn latency, tokens/turn, stuck jobs, RSS.
3. **tokens/turn vs resident (rollback gate)**: the resident baseline is **measured, not assumed** — `python scripts/loadtest/measure_resident.py` (spawns the real `codex` CLI against MockProvider; see `docs/HOSTED_RUNTIME_V2_TOKEN_BASELINE.md`). The current shared-fixture baseline is **9303.0 tokens/turn**. Then run `python scripts/loadtest/compare_tokens.py --resident-baseline 9303.0 --threshold 0.10`. Exit code 1 = regression > +10%; exit code 2 = invalid/non-finite gate input; either means **do not roll out**. On 2026-07-14 the production-native-loop command reports **1707.0 tokens/turn, 1.3333 LLM calls/turn, -81.65% vs resident**; the workload mixes two one-shot replies with one native tool-call round and reserved tools-disabled final reply. Re-measure both sides whenever the fixtures, loop, prompts, tools, model, or agent CLI version changes. This offline mock result is a regression gate, not production capacity evidence.
4. Sanity vs the capacity model: 100 users, 16 workers × ~20 s/turn ≈ 50 turns/min → the everyone-at-once spike clears in ~2 min; queue-wait P95 should not contradict this.

## Step 2 — Gated rollout (evidence-first)

1. **Test/prod state: HOLD.** The remaining gate is deployed operational evidence, not the former P0 code omissions: finish the prerequisites above and the handoff's fault/rollback checks before any test/prod user flip. Once they pass, start with one explicitly consented internal user: `io_cli set-runtime-mode <internal_uid> db_action_v2`. Watch `/v1/admin/v2-metrics` (inflight/pending/wake success, whole-turn usage across chat/wake/extraction/compaction, and prompt-cache coverage/hit ratio) + error chips + subjective chat quality for 24–48 h. **Pre is the exception described above:** it automatically enrolls every eligible account so ordinary iOS testing supplies the operational evidence.
2. **Ramp cohorts**: 5 → 20 → 50 → all. `io_cli list-runtime-mode` to track who's on what. Each batch: confirm tokens/turn not regressed, queue-wait P95 within SLA, `wake.success_rate` healthy, `stuck_jobs ≈ 0`.
3. **Mixed-fleet safety (per-user test/prod policy)**: the D0 exclusivity guard keeps each user on exactly ONE path — only an explicit `db_action_v2` value enrolls V2; missing/invalid values remain resident. A flipped user runs on V2 and their resident consumer is reaped (~15 s, next supervisor tick); an un-flipped user stays resident. Under Pre's `v2_only` policy, startup/setup materializes that same explicit tuple for every eligible account and refuses mismatches. A runtime-control read failure must refuse routing, and a resident-discovery query failure must skip reconciliation rather than look like an empty roster. No double-run and no outage-driven fleet teardown.

## Step 3 — Kill resident (the actual cost win)

- **Automatic**: flipping a user to `db_action_v2` drops them from `db.list_agent_runtime_enabled_users` → `Supervisor.tick()` reaps their consumer next tick and releases the lease. Kill-resident IS the flip; no separate teardown.
- **Precondition per user**: their proactive path is covered by D3 (else wakes are lost). So do NOT broadly flip until D3 is deployed.
- **Fleet retirement**: once all users are `db_action_v2`, the roster is empty. Keep the supervisor running with an empty roster for a while (rollback headroom), then retire it. **This is the moment RAM/process count decouples from user count.**
- **Verify**: `agent_runtime_instances` lease table has no `db_action_v2` users; pool RSS flat; no user appears on both paths.

## Rollback

- **Per-user test/prod policy:** revert a user's flag with `io_cli set-runtime-mode <uid> resident_cli`; the supervisor re-spawns their resident next tick.
- **Pre fleet rollback:** set the repository variable `PRE_HOSTED_RUNTIME_POLICY=resident_only` and redeploy `pre`. The value is injected through the encrypted environment channel (no compose-hash change). Startup fences every V2/split control—including users whose route was removed or failed—and the post-deploy gate requires zero policy inconsistencies. The Pre `v2_only` policy deliberately rejects ad-hoc per-user resident flips.
- **Drain before image rollback.** Stop new V2 admission, move enrolled users to `resident_cli`, wait for claimed/running V2 rows to terminalize, and verify no V2 reply is in flight before deploying an older worker image. The schema keeps separate queue and execution deadlines for mixed-version safety, but an old worker does not have the new ownership/effect fences.
- **Encrypted write-effect compatibility.** New memory/identity/schedule rows use
  versioned durable types (`*_encrypted_v1`). An older worker cannot interpret
  them and therefore leaves them pending instead of applying an empty payload;
  the current worker's parent sweeper drains them after upgrade. Do not leave an
  old image running as the only worker: pending encrypted writes will remain
  visible in `effects.pending` until a current worker returns. Rows that require
  human judgment surface as `effects.needs_reconciliation`.
- **Do not mistake the CI deploy gate for the live turn switch.** `gh variable set DEPLOY_*_RUNNER_CVM --body false` only prevents future deploy jobs; the already-running CVM keeps serving its last image. The database-backed `v2_runtime_control.turns_halted` switch is the live control: setting it true fail-closes new V2 admission, stops new turn claims, and fences writes while leaving Genesis alive. It does not cancel an already-running provider request, so roll back every flipped user and drain terminal jobs before an image rollback.
- **Image rollback**: re-pin an older `:<sha>` in the runner compose and redeploy.

## Deferred (explicit, out of this round)

- **D follow-up ledger (do not read this runbook as claiming completion)**:
  provider prompt caching and cache telemetry are implemented (OpenAI/OpenRouter
  affinity, Anthropic automatic ephemeral caching, Gemini/DeepSeek implicit
  caching, and cache-off compatibility fallback), and whole-turn usage now
  includes extraction/compaction calls. A real two-request cache-hit proof and
  a concurrent CVM load run still remain;
  admission ceiling is implemented; typing-signal pre-warm is not implemented;
  encrypted full-trajectory storage plus side-effect-disabled dream-lane failure
  replay is not implemented; and fleet-wide resident retirement is an operator
  outcome only after wake/capture/dream parity and the rollout gates above pass.
- **Long-horizon conversation frontier is not implemented.** The current
  encrypted itemized summary is append-only and is sent in full on every turn,
  so it preserves coverage and fails visibly rather than dropping history, but
  its prompt footprint still grows without bound until a BYOK model's context
  limit is reached. This is acceptable for bounded test soaking, not a
  fleet-wide production cutover claim. Before broad rollout, add immutable
  encrypted summary segments plus append-only higher-level checkpoints and a
  CAS-managed non-overlapping active frontier, then budget the complete rendered
  prompt (system + frontier + verbatim tail + tool transcript) against provider
  context limits. Do not "fix" this by truncating the summary or silently
  advancing its watermark: either approach loses the full-conversation
  invariant.
- **Default flip outside Pre** — Pre now performs an explicit startup backfill and persists the intended mode at setup/test/activation; it does not reinterpret a missing blob at read time. Test and production remain `per_user`: do not infer fleet membership from a missing/invalid value or copy Pre's policy until their rollout gates are complete.
- **Authoritative 4c/8GB load run** — rerun Step 1 on CVM-class hardware if the local indicative numbers are borderline.
- **Promoting genesis to its own container.** It is now a dedicated thread inside `serve-worker` rather than inside `agent-runner` — better, but still a thread in someone else's process. Its own service would give it a restart policy and a real crash domain. Costs one more `addComposeHash()`, so it was not bundled here.
