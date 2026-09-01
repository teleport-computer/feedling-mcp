# TEE App-Role Automatic Alembic Design

## Goal

Make a TEE-primary backend upgrade its `alembic_tee` schema automatically at
process startup, matching the existing RDS startup behavior. The migration must
finish before a main backend or runner is ready to serve traffic.

## Decision

The TEE `app` role will inherit `feedling_owner` so it can alter the existing
objects owned by `feedling_owner`. A one-time owner operation performs:

```sql
GRANT feedling_owner TO app;
```

This deliberately widens the app-role boundary: a compromise of the app DSN
can perform schema DDL as well as normal business CRUD. The decision is
intentional so TEE follows the same automatic migration model as RDS. The
backend will still connect with `current_user = app`; no owner DSN is injected
into the running backend or runner.

## Startup Flow

```text
Gunicorn master / standalone runner starts
  -> app connection runs alembic_tee upgrade head
  -> app connection verifies the exact TEE head and primary triggers
  -> process becomes ready and serves traffic
```

`gunicorn_conf.on_starting()` already calls `db.init_schema()` exactly once
before workers are forked. The standalone runner also calls the same function.
In TEE mode, `db.init_schema()` will run the TEE Alembic chain before retaining
the existing fail-closed assertions. The RDS branch remains unchanged and
continues to run its existing Alembic chain.

`alembic_tee.connection.migration_database_url()` currently refuses
`DATABASE_URL` and selects only owner/legacy shadow migration DSNs. In explicit
`FEEDLING_DATABASE_SCHEMA=tee` mode it will instead select `DATABASE_URL`, which
is the promoted app-role primary. Legacy shadow and plaintext-shadow modes keep
their existing owner-DSN selection and must never fall back to `DATABASE_URL`.

The migration call is idempotent. Concurrent startup attempts may each invoke
Alembic, but PostgreSQL/Alembic version-table serialization remains the same
operational model used by the existing RDS startup path. No new CI migration job
or runtime owner credential is introduced.

## Failure Behavior and Observability

If an app-role migration, head assertion, or primary-trigger assertion fails,
the process must fail startup before it reports ready or serves traffic. Existing
instances continue until a replacement starts successfully.

Use the project `log(...)` helper for concise startup state transitions. Logs
may include the schema mode and migration head but must never include a DSN,
password, or other credential.

## Tests

Tests must prove all of the following:

1. In `tee` mode, `db.init_schema()` invokes the `alembic_tee` upgrade path
   before checking the database head and primary triggers.
2. The TEE path still rejects an incorrect head or incomplete trigger set after
   the upgrade attempt.
3. In `rds` mode, `db.init_schema()` retains the existing RDS Alembic upgrade
   behavior.
4. In explicit TEE-primary mode, the TEE Alembic connection selector uses
   `DATABASE_URL`; outside that mode it retains the owner/legacy selection and
   rejects a missing migration DSN.

The integration/operations procedure separately verifies that the real TEE
`app` role can perform the intended migration after the one-time role grant.

## Rollout and Rollback

Before the first app-driven TEE migration, run the owner-side role grant and
verify with the app DSN that the role can create/alter a release migration.
Deploy the code only after the normal Phase-4 cutover gates are complete; this
change does not waive terminal-ciphertext preservation, strict verification,
backup/restore evidence, or the write-freeze requirement.

Code rollback does not automatically downgrade schema, matching the existing
RDS model. Schema changes must remain backward-compatible or use a separate
owner-controlled rollback procedure. Revoke with:

```sql
REVOKE feedling_owner FROM app;
```

only after all running/releasable TEE-primary code no longer depends on app-role
automatic migrations.

## Non-Goals

- Do not add a migration step to CI.
- Do not inject `PROD_TEE_MIGRATION_DSN` into backend or runner CVMs.
- Do not change the TEE-primary data cutover process or plaintext-shadow gate.
- Do not automatically run schema downgrades.
