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
  together. Shadow dual-write must be off after promotion.

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
4. Run owner migrations to the release's `alembic_tee` head. It is
   `0013_primary_runtime_contracts` at this revision; derive it from the actual
   release rather than copying this value blindly.
5. Replicate/reconcile until strict verification is green. Existing users whose
   v6 preference is absent or `off` transform to plaintext; explicit `on` users
   and unknown users carry verbatim. R2 plaintext backfill follows the same
   default/fail-safe rule.
6. Perform Phase 4 under a write freeze, switch both units to TEE, then run the
   two-account regression.
7. Open `FEEDLING_PLAINTEXT_WRITES_ACCEPTED=1` only after compatible clients and
   regression evidence exist, one environment at a time.

## Phase 4 maintenance window

Stop API writes, main `serve-worker`, and the independent runner. Drain Genesis
chunks/jobs, voice handoffs, agent jobs, the action queue, both V2 outboxes, and
pending TEE device migrations. From the exact release being promoted:

```bash
cd backend
python -m admin.phase4_cutover
python -m admin.phase4_cutover --apply --confirm-writes-frozen
```

The first command is read-only. Apply requires the TEE owner DSN in
`TEE_MIGRATION_DATABASE_URL`; it copies the frame bridge and Chat generation
fences, aligns sequences, enables TEE contracts, and writes the head-bound
prepared marker.

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

## Environment cadence

- `pre`: gate open; full two-account and physical-device Broadcast/Perception
  regression. Retain frozen RDS for the observation window.
- `test`: repeat with test-only secrets. Promote TEE with gate closed, smoke,
  then open and repeat dual-account regression.
- `prod`: require test evidence, backup/restore drill, rollback owner, and a
  scheduled freeze. Promote with gate closed. Opening plaintext is a separate,
  reversible configuration step after encrypted canaries stay green.
