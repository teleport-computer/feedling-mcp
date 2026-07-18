# Hosted Runtime V2 — Deployment and Recovery Runbook

> **Current source of truth — 2026-07-18.** Hosted model-API execution is
> Runtime V2-only in local, test, pre, and production manifests. There is no
> hosted resident rollback, per-user runtime selector, supervisor service, or
> empty resident roster to preserve. The independent user-operated
> `feedling-chat-resident` path is separate and is not a hosted fallback.

## Topology

Each managed environment has two deployment units:

1. the main CVM (`ingress`, `backend`, `enclave`); and
2. one or more runner CVMs containing only the pooled `serve-worker` service.

The worker claims durable PostgreSQL jobs with `SKIP LOCKED`, publishes capacity
heartbeats, runs the native provider/tool loop in bounded slots, and hosts the
Genesis import drain. Durable turn state, deadlines, reply cursors, effects, and
terminal errors remain in PostgreSQL. The historically named
`feedling-agent-runner` image package is retained for registry/deployment
compatibility, but its image contains no resident supervisor, agent CLI, per-user
home, checkpoint, lease, or data volume.

Authoritative manifests:

- test: `deploy/docker-compose.phala.test.yaml` plus
  `deploy/docker-compose.phala.runner.yaml`;
- pre: `deploy/docker-compose.phala.pre.yaml` plus
  `deploy/docker-compose.phala.pre.runner.yaml`;
- production: `deploy/docker-compose.phala.yaml` plus
  `deploy/docker-compose.phala.prod.runner.yaml`; and
- local: `deploy/docker-compose.yaml` plus
  `deploy/docker-compose.agent-runner.yaml`.

Every backend manifest sets `FEEDLING_HOSTED_RUNTIME_POLICY: "v2_only"`
literally. Any other policy value fails startup. Every runner manifest exposes
exactly one service named `serve-worker`.

## Required configuration

The backend and its workers must share:

- `DATABASE_URL`;
- `FEEDLING_RUNTIME_TOKEN_SECRET`; and
- the correct main-CVM `FEEDLING_API_URL` and `FEEDLING_ENCLAVE_URL` routes.

`FEEDLING_V2_MAX_WORKERS` bounds concurrent slots per container. Size it with
PostgreSQL connection capacity and provider limits. `serve-worker` enforces a
minimum database-pool floor derived from slot count and fails startup when an
explicit pool is too small.

The artifact sandbox is optional and fail-closed. Worker manifests default
`FEEDLING_V2_SANDBOX_PROVIDER=disabled`; text-only turns, virtual text reads,
and Markdown edits require no sandbox. To enable E2B, set the environment's
`FEEDLING_V2_SANDBOX_PROVIDER=e2b`, encrypted `E2B_API_KEY`, and versioned
`FEEDLING_V2_E2B_TEMPLATE` together. Deployment fails before rollout if the
provider is `e2b` but either credential is missing. Internet remains disabled
unless the deployment explicitly sets `FEEDLING_V2_E2B_ALLOW_INTERNET=1`.
Decrypted artifact bytes then cross from Feedling's CVM into E2B, so consent,
egress, retention, and billing policy must be approved before activation.

Do not set `FEEDLING_HOST_ALL`, `AGENT_RUNTIME_USERS`,
`AGENT_RUNTIME_AUTODISCOVER`, `AGENT_RUNTIME_MAX_CHILDREN`, or a
`resident_only` policy. They are retired hosted controls.

## Deployment order

For a CVM-affecting release:

1. Build and publish both the backend image and the historically named worker
   image from the same commit.
2. Deploy the main CVM and publish/authorize its measured Compose hash.
3. Deploy every runner CVM with the same database and runtime-token secret, then
   publish/authorize each runner Compose hash.
4. Do not admit real-device testing until the Runtime V2 readiness gates below
   are green.

The `deploy-test-runner-cvm`, `deploy-pre-runner-cvm`, and
`deploy-prod-runner-cvm` CI jobs are mandatory when their corresponding hosted
CVM source changes. A missing test/pre runner ID or an empty production runner
ID list is a deployment error, not a reason to skip the worker job. Production
requires at least two distinct worker CVM IDs so rolling one failure domain
cannot remove the fleet's only hosted executor.

## Readiness gates

Check `GET /v1/admin/v2-metrics` with an admin token and require:

- at least one fresh turn-worker heartbeat and positive live capacity;
- `genesis_alive: true`;
- no runtime-policy inconsistencies for eligible active model routes;
- bounded pending/oldest-job age and no expired job backlog;
- healthy effect-outbox and wake statistics; and
- expected provider/model prompt-cache telemetry.

Then send a real encrypted test turn through the same public API used by iOS.
The send must return `202 processing`, commit one user row and one durable job,
and later produce an encrypted assistant row. Retry the request with the same
`client_msg_id`; it must return the original user-message pointer without a
second message or execution.

Pre additionally runs the prompt-cache canary after worker liveness. Keep the
canary route/model stable so the second request can reuse an identical provider
prefix, and treat missing cache-read tokens as a failed gate rather than
evidence that caching is merely “best effort.”

## Failure behavior

Hosted send is fail-closed before persistence when:

- the ownership tuple is not exactly `db_action_v2` + `v2`;
- no fresh worker heartbeat exists;
- the live kill switch halts turns; or
- estimated queue wait exceeds the admission SLA.

Once accepted, message persistence and job enqueue are one transaction. Pending
jobs have deadlines; a reaper terminalizes expired work. Turn exceptions and
watchdog failures become a terminal error/status plus `last_runtime_error`
instead of leaving a message permanently in `processing`.

## Recovery — no resident rollback

Do not try to recover a hosted incident by selecting `resident_cli` or starting
an old supervisor image. Those paths are deliberately unavailable.

Use this order instead:

1. Halt new turn admission with the Runtime V2 kill switch if the fleet is
   producing unsafe effects or repeatedly failing.
2. Preserve PostgreSQL and effect-outbox state; do not delete pending/running
   rows to make a dashboard green.
3. Restore or scale `serve-worker` capacity. PostgreSQL claims allow another
   healthy worker/CVM to take over without a per-user process migration.
4. If the release itself is bad, deploy the last database-compatible V2 image.
   Never roll application code across an incompatible schema migration.
5. Confirm heartbeats, capacity, Genesis, queue age, terminal-error reporting,
   and effect recovery; then clear the kill switch.
6. Run an encrypted chat canary and same-`client_msg_id` retry before reopening
   broad traffic.

An independently operated `/v1/chat/*` resident consumer may continue serving
accounts deliberately configured for that separate product route. It cannot
claim hosted V2 accounts and is not part of this incident procedure.

## Release checklist

- [ ] Backend manifests render literal `v2_only`.
- [ ] Runner manifests render only `serve-worker` with no volumes.
- [ ] Backend and worker image tags point to the intended commit.
- [ ] Main and runner Compose hashes are authorized.
- [ ] Runtime-token secret and database URL match across deployment units.
- [ ] Sandbox is intentionally disabled, or its provider/key/template and data-boundary policy are verified.
- [ ] Worker capacity, Genesis, policy, queue, wake, and effect gates are green.
- [ ] Encrypted real-device turn and `client_msg_id` retry pass.
- [ ] Prompt-cache canary passes where configured.
- [ ] Previous database-compatible V2 image and scale-out procedure are known.

Implementation parity and remaining non-retirement work are tracked in
`docs/HOSTED_RUNTIME_V2_PARITY_MATRIX.md`.
