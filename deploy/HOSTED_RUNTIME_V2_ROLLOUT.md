# Hosted Runtime V2 — Deployment and Recovery Runbook

> **Current source of truth — 2026-07-19.** Hosted model-API execution is
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

Encrypted trajectory capture is always on. Provider-backed offline review is
separate, defaults off, and must stay off unless
`FEEDLING_V2_TRAJECTORY_REVIEW_ENABLED=1` and a valid
`FEEDLING_V2_TRAJECTORY_REVIEW_MAX_ACTIVE` fleet ceiling are configured. The
ceiling bounds pending plus running provider reviews; it is not a retention
policy. There is no automatic trajectory/review GC yet, so keep review opt-in
until BYOK budget and retention/export policy are approved.

Do not set `FEEDLING_HOST_ALL`, `AGENT_RUNTIME_USERS`,
`AGENT_RUNTIME_AUTODISCOVER`, `AGENT_RUNTIME_MAX_CHILDREN`, or a
`resident_only` policy. They are retired hosted controls.

After production is running the same V2-only source and the release checklist
below is green, remove the retired repository-level controls. Do not remove
`FEEDLING_RUNTIME_TOKEN_SECRET`; the pooled V2 workers still require it.

```bash
repo=teleport-computer/feedling-mcp
legacy_names=(
  FEEDLING_HOST_ALL
  AGENT_RUNTIME_USERS
  AGENT_RUNTIME_AUTODISCOVER
  AGENT_RUNTIME_MAX_CHILDREN
  DEPLOY_TEST_RUNNER_CVM
  DEPLOY_PRE_RUNNER_CVM
  DEPLOY_PROD_RUNNER_CVM
)

# Inventory first. An empty result is valid.
gh variable list --repo "$repo" | rg "$(IFS='|'; echo "${legacy_names[*]}")"
gh secret list --repo "$repo" | rg "$(IFS='|'; echo "${legacy_names[*]}")"

# Delete only the exact retired names; tolerate names that were never present.
for name in "${legacy_names[@]}"; do
  gh variable delete "$name" --repo "$repo" 2>/dev/null || true
  gh secret delete "$name" --repo "$repo" 2>/dev/null || true
done

# Postcondition: both commands should print nothing.
gh variable list --repo "$repo" | rg "$(IFS='|'; echo "${legacy_names[*]}")" || true
gh secret list --repo "$repo" | rg "$(IFS='|'; echo "${legacy_names[*]}")" || true
```

If the repository uses GitHub environment-scoped values, repeat the same
inventory and deletion with `--env test`, `--env pre`, and `--env prod` only
after confirming those environment names in repository settings. Never use a
prefix or wildcard delete.

## Deployment order

For a CVM-affecting release:

1. Build and publish both the backend image and the historically named worker
   image from the same commit.
2. Deploy the main CVM and publish/authorize its measured Compose hash.
3. Deploy every runner CVM with the same database and runtime-token secret, then
   publish/authorize each runner Compose hash. Production CI injects that
   target's inventory CVM ID plus the exact seven-character image build; the
   worker refuses to start if either value is missing or the build disagrees
   with the commit baked into the image.
4. Do not admit real-device testing until the Runtime V2 readiness gates below
   are green.

The `deploy-test-runner-cvm`, `deploy-pre-runner-cvm`, and
`deploy-prod-runner-cvm` CI jobs are mandatory when their corresponding hosted
CVM source changes. A missing test/pre runner ID or an empty production runner
ID list is a deployment error, not a reason to skip the worker job. Production
requires at least two distinct worker CVM IDs so rolling one failure domain
cannot remove the fleet's only hosted executor. The checked-in inventory still
contains only one real production runner, so the topology preflight—and thus
the deploy and identity proof—intentionally cannot close until a second CVM is
actually provisioned and added.

## Readiness gates

Check `GET /v1/admin/v2-metrics` with an admin token and require:

- at least one fresh turn-worker heartbeat and positive live capacity;
- `genesis_alive: true`;
- no runtime-policy inconsistencies for eligible active model routes;
- bounded pending/oldest-job age and no expired job backlog;
- healthy effect-outbox and wake statistics; and
- expected provider/model prompt-cache telemetry.

Production does not accept the aggregate checks alone. After every listed CVM
has deployed, CI waits 65 seconds—longer than both the old turn and Genesis
heartbeat freshness windows—and requires the exact current-build heartbeat
pair for **every** non-comment identity in `prod-runner-cvm-ids.txt`:

- `v2-fleet-cvm-<CVM_ID>-build-<7-char-build>-boot-<nonce>` with positive turn capacity; and
- the same identity plus `:genesis` with a fresh Genesis heartbeat.

The CVM/build portion is stable while the boot nonce remains process-unique so
job ownership and orphan recovery never confuse a replacement process with the
one that crashed. Turn and Genesis must share the exact full boot identity.
Missing identities, duplicate live boots, unlisted current-build identities,
previous-build rows, and stale rows fail the deployment. The same gate requires literal
`v2_only`, target mode `db_action_v2`, `ready_count == eligible_count`, and zero
inconsistent routes. This is an application-level deployment proof bound to
the trusted Phala target/env injection; Compose-hash publication and CVM
attestation remain the cryptographic deployment evidence.
Under the managed one-service-per-CVM compose, one claimed runner identity
emits only its own CVM/build prefix and therefore cannot satisfy a different
inventory entry.
`FEEDLING_V2_RUNNER_CVM_ID` is a CI-injected claimed identity, not a value
cryptographically derived from dstack attestation. The exact-set gate prevents
ordinary missing/stale/extra rollout false-greens, but a compromised runner
with direct write access to the shared heartbeat table could forge another
claimed ID; preventing that requires binding heartbeat writes to attested CVM
identity rather than trusting the injected environment.

Then send a real encrypted test turn through the same public API used by iOS.
The send must return `202 processing`, commit one user row and one durable job,
and later produce an encrypted assistant row. Retry the request with the same
`client_msg_id`; it must return the original user-message pointer without a
second message or execution.

Pre additionally runs the prompt-cache canary after worker liveness. Keep the
canary route/model stable so the second request can reuse an identical provider
prefix, and treat missing cache-read tokens as a failed gate rather than
evidence that caching is merely “best effort.”

The checked-in canary currently proves OpenRouter with one OpenAI-family and one
Anthropic-family model over a long stable synthetic conversation prefix. It
does not prove native Bedrock or a deployed trusted `/skills` mutation.
Editable `/memory/WORKING.md` is deliberately pull-only and is not an eager
cache-prefix layer.
After those source paths change, add route-specific live probes rather than
using the existing OpenRouter success as proxy evidence.

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
- [ ] Trajectory review is intentionally disabled, or its fleet ceiling, BYOK budget, and retention policy are verified.
- [ ] Worker capacity, Genesis, policy, queue, wake, and effect gates are green.
- [ ] Encrypted real-device turn and `client_msg_id` retry pass.
- [ ] Prompt-cache canary passes where configured.
- [ ] Production lists at least two independent runner CVMs before rollout.
- [ ] Production's exact per-CVM/current-build turn + Genesis fleet proof passes.
- [ ] Live process inventory shows no hosted resident supervisor or per-user CLI process.
- [ ] Previous database-compatible V2 image and scale-out procedure are known.

Implementation parity and remaining non-retirement work are tracked in
`docs/HOSTED_RUNTIME_V2_PARITY_MATRIX.md`.
