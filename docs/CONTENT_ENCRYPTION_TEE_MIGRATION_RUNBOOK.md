# Content encryption dual-track and TEE-primary migration

This is the operator runbook for moving `test` or `prod` to TEE PostgreSQL while
keeping encrypted and plaintext accounts on the same release. Run it once in
`pre` first. Do not reuse database URLs, certificates, buckets, WAL-G prefixes,
or content-key baselines between environments.

## Invariants

- `content_encryption_effective`, not the raw preference, selects every new
  write. Missing or unknown users fail closed to encryption.
- Encrypted uploads remain accepted for plaintext accounts so old clients and
  historical rows continue to work during rollout.
- Plaintext uploads require an open deployment gate and an effectively `off`
  account. `local_only` always requires encryption.
- Reads select by row shape (`body_ct`, then `body_b64`, then `body`), not the
  current preference. A preference change affects new writes; it does not
  silently rewrite history.
- API, Runtime V2 worker/runner, iOS, and Broadcast must ship as one compatible
  set. A worker missing the preference resolver fails safe to encryption and
  leaves unexpected ciphertext in an `off` account.
- Switch `FEEDLING_DATABASE_SCHEMA=tee` and the TEE app DSN on main and runner
  together. The legacy RDS-to-TEE shadow dual-write must be off after promotion.
- A post-promotion plaintext shadow is a separate topology. The promoted TEE
  database remains authoritative; the shadow is never a failover source and
  receives a decrypted projection of every supported content row, including
  rows owned by accounts whose explicit preference is `on`.

## Supported surfaces

Exercise both shapes for Chat text, image/file bodies and captions; Memory and
World Book; Runtime V2 replies, effects, and trajectories; Perception context
and photos; Broadcast frames; Genesis chunks; identities and user blobs. Frame
and attachment plaintext binary uses strict `body_b64`; large objects may use a
`plaintext_v1` pointer. Resident one-shot Genesis deliberately stays
`sealed_v1`: the shared backend must never receive local resident material.

## Release order

1. Deploy backend compatibility with the plaintext gate closed. Verify
   `whoami` reports effective `on`.
2. Release compatible iOS. It fails safe on a missing effective value, reads
   mixed rows, and publishes the effective shape to Broadcast.
3. Provision environment-specific TEE PostgreSQL, owner/app roles, verify-full
   CA, backup prefix, and WAL-G encryption key.
4. Run owner migrations to the exact release's `alembic_tee` head. Derive the
   head from the checked-out release rather than copying a historical value.
5. Replicate/reconcile until strict verification is green. Existing users whose
   v6 preference is absent or `off` transform to plaintext; explicit `on` users
   and unknown users carry verbatim. R2 plaintext backfill follows the same
   default/fail-safe rule.
6. Under the Phase 4 write freeze, preserve any explicitly terminal historical
   ciphertext using the guarded procedure below, run strict verification, then
   perform Phase 4 and switch both units to TEE.
7. Open `FEEDLING_PLAINTEXT_WRITES_ACCEPTED=1` only after compatible clients and
   regression evidence exist, one environment at a time. This per-user write
   gate is independent from the all-plaintext shadow described below.

## Post-promotion plaintext shadow

The release has two explicit production gates:

1. **Gate 1 — TEE primary.** Promote the current TEE PostgreSQL database to
   `DATABASE_URL`, set `FEEDLING_DATABASE_SCHEMA=tee` for every release unit,
   remove the legacy `TEE_DATABASE_URL` dual-write wiring, and verify encrypted
   canaries, backups, the exact migration head, and the prepared marker.
2. **Gate 2 — decrypted shadow.** Only after Gate 1 is healthy, provide an
   independent PostgreSQL 17 app DSN as `PLAINTEXT_SHADOW_DATABASE_URL` and set
   `FEEDLING_PLAINTEXT_SHADOW_ENABLED=1` on the main backend. The protected CI
   environment runs `python -m admin.plaintext_shadow preflight` followed by
   `python -m admin.plaintext_shadow verify --require-green` before deployment.

Gate 2 fails closed unless the source is a TEE primary, source and target are
different databases, both schema heads match the release, TLS is enabled, the
target is writable, capture triggers match the audited inventory, and a recent
restore drill has been recorded. The pooled worker and every independent runner
must keep `FEEDLING_PLAINTEXT_SHADOW_ENABLED=0` and receive no shadow DSN; source
database triggers capture their writes, and the elected main-backend scheduler
is the sole drain owner.

Every release that advances `alembic_tee` must apply that same release head to
the decrypted-shadow target with `PLAINTEXT_SHADOW_MIGRATION_DATABASE_URL`
before, or in the same maintenance window as, the TEE primary migration. This
includes a TEE-primary-only table such as `trace_events`: the table is not added
to shadow replication, but the exact-head gate still requires the target's
schema version to advance. Migrating only the primary makes the elected drain
stop deliberately on head mismatch; treat that stop as a failed deployment
sequence, not as ordinary replication lag.

The source control tables contain keys, generations, counters, timestamps, and
fixed error slugs only. The drain claims dirty keys with short
`FOR UPDATE SKIP LOCKED` transactions, re-reads the authoritative source row,
decrypts every ciphertext-bearing configuration, and idempotently upserts or
deletes the target row. Retry delay is bounded exponential backoff; 20 failed
attempts quarantine the key for operator review. Explicit `content_encryption=on`
does not preserve ciphertext in this target: the target policy is
`plaintext_all`.

Before enabling Gate 2, create separate least-privilege migration and app roles,
prove backup and restore from the new target, record only content-free evidence,
run a full backfill, drain through the captured high-water generation, and run
strict verification. Strict verification requires exact table counts and
content projections, no unexpected ciphertext-envelope shapes, no pending or
quarantined dirty keys, and fresh backup/restore evidence bound to the target
fingerprint, backup artifact digest, declared capacity, connection limit, and
HA attestation. Those infrastructure facts must arrive as an Ed25519-signed
canonical JSON payload from the external backup/provider verification process;
`FEEDLING_PLAINTEXT_SHADOW_INFRA_EVIDENCE_PUBLIC_KEY` contains only the trusted
raw public key in base64. The CLI rejects unsigned operator-entered capacity,
HA, digest, or target-identity values. The primary persists the canonical
payload and full signature, and every preflight re-verifies them before
comparing the signed claims with the live target. The `app` and legacy
`tee_replicator` roles have no direct evidence-table DML; the app can only call
the controlled evidence recorder. After enabling Gate 2, observe at least one green scheduler run
before declaring the rollout complete. Operator output must contain
fingerprints, scalars, and fixed slugs only;
never print DSNs, passwords, keys, or row bodies.

The plaintext shadow and all of its backups are plaintext recipients. Do not
promote it to primary, use it as a production failover, or route application
reads to it. If the target is unavailable, keep Gate 1 serving from the TEE
primary while the durable source-side queue retries. Disable Gate 2 to stop new
drains; removal of capture triggers is a separate explicit operator action.

Rotate any bootstrap or previously disclosed administrator credential only
after replacement least-privilege roles have connected successfully and backup
and restore evidence is green. Rotation is not a substitute for replacing the
application DSN and must not be attempted while the database endpoint is
unreachable.

## Phase 4 maintenance window

Stop API writes, main `serve-worker`, and the independent runner. Drain Genesis
chunks/jobs, voice handoffs, agent jobs, the action queue, both V2 outboxes, and
active requeue work. Take and verify a TEE backup before changing terminal
rows. Migrate the TEE database to the exact release head before running the
release-local preservation command; the command rejects a different head or
database fingerprint.

Before generating a preservation plan, confirm that the migrated destination
has both the release head and the voice lifecycle fence:

```sql
SELECT version_num FROM alembic_tee_version;
SELECT to_regclass('public.voice_call_sessions');
```

The first query must return exactly the release's single TEE head and the second
must return `voice_call_sessions`. Any earlier preservation dry-run was bound to
the previous head and is invalid after this migration; regenerate its count and
SHA-256 from the exact release checkout. Complete a TEST voice session
create/cancel/finalize smoke before entering the production write freeze.

From the exact release being promoted, first obtain a read-only aggregate plan:

```bash
cd backend
PRESERVATION_PLAN="$(python -m admin.tee_terminal_preservation)"
printf '%s\n' "$PRESERVATION_PLAN"
PRESERVATION_COUNT="$(printf '%s' "$PRESERVATION_PLAN" | jq -er '.eligible')"
PRESERVATION_SHA256="$(printf '%s' "$PRESERVATION_PLAN" | jq -er '.plan_sha256')"
```

Do not continue unless `blockers` is empty and the aggregate count matches the
reviewed inventory. Apply only that exact count and digest:

```bash
python -m admin.tee_terminal_preservation \
  --apply \
  --confirm PRESERVE-TERMINAL-CIPHERTEXT \
  --expected-count "$PRESERVATION_COUNT" \
  --expected-plan-sha256 "$PRESERVATION_SHA256"
python -m tee_shadow verify --sample-rate 1
```

The operation copies raw ciphertext rows; it never decrypts them and never
changes `users.doc.content_encryption`. Preserved content remains readable by a
device only when its `K_user` is still valid. Preservation does not synthesize a
working `K_enclave` and therefore does not make previously undecryptable content
readable to hosted agents.

Before the first TEE-primary write, the exact operation can be reversed with the
same reviewed count and digest:

```bash
python -m admin.tee_terminal_preservation \
  --revert \
  --confirm REVERT-PRESERVED-CIPHERTEXT \
  --expected-count "$PRESERVATION_COUNT" \
  --expected-plan-sha256 "$PRESERVATION_SHA256"
```

Revert fails after the Phase 4 prepared marker exists, or if any source,
destination, or audit marker has changed. It deletes only rows whose ownership
is proven by a valid preservation marker.

After strict verification is green, run Phase 4:

```bash
python -m admin.phase4_cutover
python -m admin.phase4_cutover --apply --confirm-writes-frozen
```

The first command leaves no committed writes, but it requires the destination
app role to execute a voice-session create/cancel/finalize smoke inside a
forced-rollback transaction. Apply additionally requires the TEE owner DSN in
`TEE_MIGRATION_DATABASE_URL`; it copies the frame bridge and Chat generation
fences, aligns sequences, enables TEE contracts, and writes the head-bound
prepared marker. The gate reports unresolved pending rows separately from fully
audited preserved ciphertext. Only the former blocks promotion; the preserved
count and aggregate digest are embedded in the prepared marker.

Before traffic resumes, point main and runner at the same TEE app DSN, set
`FEEDLING_DATABASE_SCHEMA=tee`, and remove `TEE_DATABASE_URL`/dual-write. Startup
must assert the TEE head and marker without DDL. After the first TEE-primary
write, frozen RDS is not a lossless rollback target; reverse-reconcile or restore
TEE changes before switching a DSN back.

## Shape inventory

Run counts only; do not print bodies, keys, or DSNs into logs.

```sql
SELECT COALESCE(doc->>'content_encryption', 'off') AS preference, count(*)
FROM users GROUP BY 1 ORDER BY 1;

SELECT
  count(*) FILTER (WHERE doc ? 'body_ct') AS encrypted,
  count(*) FILTER (WHERE doc ? 'body') AS plaintext,
  count(*) FILTER (WHERE doc ? 'body_b64') AS plaintext_binary
FROM chat_messages;

SELECT
  count(*) FILTER (WHERE payload_envelope ? 'body_ct') AS encrypted,
  count(*) FILTER (WHERE payload_envelope ? 'body') AS plaintext
FROM v2_trajectory_events;

SELECT
  count(*) FILTER (WHERE doc ? 'body_ct') AS encrypted_inline,
  count(*) FILTER (WHERE doc ? 'body_b64') AS plaintext_inline,
  count(*) FILTER (WHERE body_key IS NOT NULL) AS object_backed
FROM frame_envelopes;

SELECT count(*) FROM perception_items
WHERE jsonb_path_exists(doc, '$.**.body_ct');

SELECT count(*) FROM v2_effect_outbox
WHERE jsonb_path_exists(payload, '$.**.body_ct');
```

Ciphertext in an `off` account can be old history, an old-client upload, or the
sealed resident lane. Inspect time, producer, and family before rewriting it.
Mixed reads are supported steady state.

## Two-account regression

Create explicit `on` and `off` accounts. On every supported surface, write a
unique canary, read through the public/agent path, and inspect shape plus owner:

For a hosted Runtime V2 canary, configuring a provider is not sufficient. Add
each temporary account to `POST /v1/admin/runtime-allowlist` with
`desired="v2"`, then set `POST /v1/admin/hosted-runtime-mode` to
`db_action_v2`. Wait until the allowlist row reports `converged=true`, actual
state `v2`, and the runtime-mode fence reads `db_action_v2` before sending the
message. Otherwise the reconciler returns the account to `resident_cli`, and a
202 response only proves the V1 ingress path. Remove the temporary allowlist
rows and accounts in a `finally` cleanup.

- `on`: `body_ct` and (for shared records) `K_enclave`; no plaintext body.
- `off`: text `body`; binary `body_b64` or internal `plaintext_v1`; no crypto
  fields.
- both: V2 reply/effect/trajectory follow the account; encrypted history remains
  readable after switching off and plaintext history after switching on;
  cross-user reads fail.
- plaintext frames bypass enclave decrypt/image; encrypted frames still use it.
  Plaintext Perception envelopes normalize without a decrypt credential.
- plaintext upload to `on` is rejected; encrypted old-client upload to `off` is
  accepted; `local_only` is unavailable while effective plaintext.

Finally verify API/worker health, empty active/failed queues, schema head/marker,
backup health, and unchanged frozen-source counts.

Treat the two accounts as separate assertions. For `off`, every newly-created
Chat and trajectory row must be plaintext; for `on`, every newly-created row
must be encrypted. Do not compare lifetime environment totals: mixed historical
rows are valid, and cleanup may cascade-delete the canary evidence. Capture
counts scoped by canary account and creation time before cleanup.

## Environment cadence

- `pre`: gate open; full two-account and physical-device Broadcast/Perception
  regression. Retain frozen RDS for the observation window.
- `test`: TEE primary is promoted and the gate is open. Keep backend, in-CVM
  worker, and independent runner aligned; repeat the dual-account regression
  after every content-shape release.
- `prod`: require test evidence, backup/restore drill, rollback owner, and a
  scheduled freeze. Promote with gate closed. Opening plaintext is a separate,
  reversible configuration step after encrypted canaries stay green.
