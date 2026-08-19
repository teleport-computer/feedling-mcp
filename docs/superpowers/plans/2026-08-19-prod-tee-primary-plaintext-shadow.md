# PROD TEE Primary and Plaintext Shadow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote the existing PROD TEE PostgreSQL database to primary and add Teleport PG17 as a fully decrypted, observable, recoverable plaintext shadow behind a separate activation gate.

**Architecture:** Keep the existing TEE database authoritative and introduce a purpose-specific plaintext-shadow configuration boundary instead of reversing `TEE_DATABASE_URL`. PostgreSQL triggers record content-free dirty keys in the TEE primary; one elected worker re-reads the current authoritative row, decrypts encrypted content through the enclave, and idempotently applies it to TEE SQL. Existing table registry, translators, snapshot copier, verifier, and Phase 4 tooling are reused behind an explicit `plaintext_all` target policy.

**Tech Stack:** Python 3.12, psycopg 3, psycopg-pool, PostgreSQL 17, Alembic, Starlette ASGI, pytest, GitHub Actions, Phala/dstack CVMs.

**Spec:** `docs/superpowers/specs/2026-08-19-prod-tee-primary-plaintext-shadow-design.md`

## Global Constraints

- The existing PROD TEE database is the only authority after Gate 1.
- `content_encryption=on` rows are decrypted through the enclave before entering the new shadow; the shadow never carries their ciphertext envelope as its stored content representation.
- A shadow failure never rolls back or rejects a successfully committed primary write.
- Durable change records contain table names, primary keys, operation metadata, retry state, and timestamps only; they never contain bodies, envelopes, content keys, passwords, or DSNs.
- `FEEDLING_PLAINTEXT_SHADOW_ENABLED` defaults to `0` and Gate 1 must be stable with it disabled.
- Primary and shadow DSNs must identify different PostgreSQL databases; startup and CI both fail closed on aliasing.
- The independent runner and main CVM use the same primary DSN and `FEEDLING_DATABASE_SCHEMA=tee` at Gate 1.
- TEE SQL Gate 2 remains blocked until verified TLS connectivity, least-privilege roles, schema head, capacity, HA, backup, and restore evidence are green.
- The old `teleport_admin` credential is treated as exposed and must be rotated only after replacement credentials have been verified and deployed.
- Public architecture, trust-boundary, deployment, and changelog documentation ship with the implementation.

---

## File structure

- Create `backend/plaintext_shadow/config.py`: target policy, environment parsing, DSN identity validation, and pool settings.
- Create `backend/plaintext_shadow/change_capture.py`: trigger installation/audit and content-free dirty-key schema contract.
- Create `backend/plaintext_shadow/outbox.py`: claim, route, retry, quarantine, and generation-safe acknowledgement.
- Create `backend/admin/plaintext_shadow.py`: operator CLI for preflight, trigger installation, backfill, drain, verify, and status.
- Create `backend/admin/plaintext_shadow_scheduler.py`: elected background drain/reconcile loop and structured metrics.
- Modify `backend/tee_shadow/mirror.py`: select the legacy or new target without disabling the new target in TEE-primary mode.
- Modify `backend/tee_shadow/reconciler.py`, `snapshot.py`, and `verify.py`: accept an explicit target pool/policy and support keyed refresh.
- Modify `backend/tee_replicator/worker.py`: add `plaintext_all` transformation and keyed current-row replay.
- Modify `backend/tee_shadow/table_registry.py`: declare exact source keys and destination table mappings.
- Create `backend/alembic_tee/versions/0026_plaintext_shadow_control.py`: source-side outbox/control tables and trigger function.
- Modify `backend/alembic_tee/env.py`: allow the purpose-specific migration DSN without removing legacy compatibility.
- Modify `backend/asgi/lifespan.py` and admin routes: start the new singleton and expose redacted health/status.
- Modify deploy compose files and `.github/workflows/ci.yml`: carry the new secrets/gates and validate topology.
- Modify runbooks and public docs under `docs-site/content/docs/`: document the new trust boundary and two-gate release.

---

### Task 1: Plaintext-shadow configuration and topology guard

**Files:**
- Create: `backend/plaintext_shadow/__init__.py`
- Create: `backend/plaintext_shadow/config.py`
- Test: `tests/test_plaintext_shadow_config.py`

**Interfaces:**
- Produces: `TargetPolicy`, `load_target() -> TargetPolicy | None`, `validate_startup() -> None`, and `same_database(a: str, b: str) -> bool`.
- Consumes: `DATABASE_URL`, `FEEDLING_DATABASE_SCHEMA`, `PLAINTEXT_SHADOW_DATABASE_URL`, `FEEDLING_PLAINTEXT_SHADOW_ENABLED`, and existing pool tuning variables.

- [ ] **Step 1: Write failing configuration tests**

```python
def test_disabled_without_target(monkeypatch):
    monkeypatch.setenv("FEEDLING_PLAINTEXT_SHADOW_ENABLED", "0")
    monkeypatch.delenv("PLAINTEXT_SHADOW_DATABASE_URL", raising=False)
    assert config.load_target() is None

def test_enabled_requires_tee_primary(monkeypatch):
    monkeypatch.setenv("FEEDLING_PLAINTEXT_SHADOW_ENABLED", "1")
    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "rds")
    monkeypatch.setenv("PLAINTEXT_SHADOW_DATABASE_URL", "postgresql://u:p@shadow/db")
    with pytest.raises(RuntimeError, match="requires FEEDLING_DATABASE_SCHEMA=tee"):
        config.validate_startup()

def test_primary_shadow_alias_rejected(monkeypatch):
    dsn = "postgresql://u:p@db.example:5432/feedling?sslmode=require"
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("PLAINTEXT_SHADOW_DATABASE_URL", dsn)
    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "tee")
    monkeypatch.setenv("FEEDLING_PLAINTEXT_SHADOW_ENABLED", "1")
    with pytest.raises(RuntimeError, match="different PostgreSQL databases"):
        config.validate_startup()
```

- [ ] **Step 2: Run the tests and confirm the missing module failure**

Run: `.venv-test/bin/python -m pytest tests/test_plaintext_shadow_config.py -q`

Expected: FAIL because `plaintext_shadow.config` does not exist.

- [ ] **Step 3: Implement immutable target policy and normalized identity comparison**

```python
@dataclass(frozen=True)
class TargetPolicy:
    dsn: str
    mode: Literal["plaintext_all"] = "plaintext_all"
    enabled: bool = True

def same_database(a: str, b: str) -> bool:
    left = conninfo_to_dict(a)
    right = conninfo_to_dict(b)
    keys = ("host", "hostaddr", "port", "dbname")
    return tuple(left.get(k, "") for k in keys) == tuple(right.get(k, "") for k in keys)
```

`validate_startup()` must reject values other than `0`/`1`, enabled-without-DSN, non-TEE primary mode, and primary/shadow aliasing. It must not log either DSN.

- [ ] **Step 4: Run focused tests**

Run: `.venv-test/bin/python -m pytest tests/test_plaintext_shadow_config.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/plaintext_shadow tests/test_plaintext_shadow_config.py
git commit -m "feat: add plaintext shadow topology guard"
```

---

### Task 2: Content-free durable dirty-key control plane

**Files:**
- Create: `backend/alembic_tee/versions/0026_plaintext_shadow_control.py`
- Modify: `backend/alembic_tee/env.py`
- Modify: `backend/tee_shadow/table_registry.py`
- Test: `tests/test_plaintext_shadow_schema.py`
- Modify: `tests/test_tee_table_registry.py`

**Interfaces:**
- Produces: `plaintext_shadow_dirty_keys`, `plaintext_shadow_sync_runs`, `plaintext_shadow_restore_evidence`, `feedling_capture_plaintext_shadow_change()`, and `Entry.key_columns`/`Entry.destination_table`.
- Consumes: TEE migration head `0025_lane_rollup_voice`.

- [ ] **Step 1: Add failing migration and registry contract tests**

```python
def test_every_synced_table_declares_key_columns():
    for name, entry in table_registry.REGISTRY.items():
        if entry.lane not in (table_registry.SKIP, table_registry.LOGICAL):
            assert entry.key_columns, name

def test_dirty_key_table_contains_no_content_columns(tee_conn):
    cols = column_names(tee_conn, "plaintext_shadow_dirty_keys")
    assert cols == {
        "table_name", "key_json", "operation", "generation", "created_at",
        "attempts", "next_attempt_at", "last_error_slug", "quarantined_at",
    }
    assert not ({"body", "doc", "payload", "envelope", "dsn"} & cols)
```

- [ ] **Step 2: Run schema tests and verify they fail at revision 0025**

Run: `.venv-test/bin/python -m pytest tests/test_plaintext_shadow_schema.py tests/test_tee_table_registry.py -q`

Expected: FAIL because the new revision, columns, and registry metadata do not exist.

- [ ] **Step 3: Add registry key metadata**

Extend the existing immutable registry entry without changing lane semantics:

```python
@dataclass(frozen=True)
class Entry:
    lane: str
    reason: str
    manual: bool = False
    key_columns: tuple[str, ...] = ()
    destination_table: str | None = None
```

Declare the real primary key for every non-skipped table. Set
`destination_table="frames"` for `frame_envelopes` and preserve same-name
defaults elsewhere. Add a guard comparing declared keys with PostgreSQL
catalog primary keys in the integration fixture.

- [ ] **Step 4: Create the TEE migration**

Create revision `0026_plaintext_shadow_control`, down revision
`0025_lane_rollup_voice`. The dirty-key primary key is
`(table_name, key_json)`. Use a sequence-backed `generation`; on conflict the
trigger replaces the operation, advances generation, resets retry state, and
never copies source columns other than declared key values.

`plaintext_shadow_sync_runs` stores only scalar run metrics and a JSON object
whose schema is restricted to per-table counts/slugs. `plaintext_shadow_restore_evidence`
stores restore timestamp, source backup timestamp, schema head, verifier digest,
operator identifier, and expiry timestamp; it has no free-form content column.

The trigger function receives key column names through `TG_ARGV` and builds
`key_json` from `to_jsonb(NEW)` or `to_jsonb(OLD)`. It raises if no keys were
provided. Do not install table triggers in the migration; Gate 2 installs them
only on the authoritative primary.

- [ ] **Step 5: Add the purpose-specific migration DSN fallback**

In `backend/alembic_tee/env.py`, resolve in this order:

```python
url = (
    os.environ.get("PLAINTEXT_SHADOW_MIGRATION_DATABASE_URL", "").strip()
    or os.environ.get("TEE_MIGRATION_DATABASE_URL", "").strip()
    or os.environ.get("TEE_DATABASE_URL", "").strip()
)
```

Never fall back to `DATABASE_URL`.

- [ ] **Step 6: Run migration and registry tests**

Run: `.venv-test/bin/python -m pytest tests/test_plaintext_shadow_schema.py tests/test_tee_table_registry.py tests/test_tee_schema.py -q`

Expected: PASS at TEE head `0026_plaintext_shadow_control`.

- [ ] **Step 7: Commit**

```bash
git add backend/alembic_tee backend/tee_shadow/table_registry.py tests/test_plaintext_shadow_schema.py tests/test_tee_table_registry.py
git commit -m "feat: add plaintext shadow dirty-key schema"
```

---

### Task 3: Idempotent trigger installation and audit

**Files:**
- Create: `backend/plaintext_shadow/change_capture.py`
- Test: `tests/test_plaintext_shadow_change_capture.py`

**Interfaces:**
- Produces: `install(conn) -> InstallReport`, `audit(conn) -> AuditReport`, and `remove(conn) -> None`.
- Consumes: `table_registry.REGISTRY` key metadata and `feedling_capture_plaintext_shadow_change()`.

- [ ] **Step 1: Write failing install/audit tests**

```python
def test_install_covers_every_synced_table(primary_conn):
    report = change_capture.install(primary_conn)
    assert report.missing_tables == ()
    expected = {
        name for name, entry in table_registry.REGISTRY.items()
        if entry.lane not in (table_registry.SKIP, table_registry.LOGICAL)
    }
    assert set(report.installed) == expected
    assert change_capture.audit(primary_conn).ok is True

def test_update_coalesces_without_content(primary_conn):
    insert_user(primary_conn, "usr_shadow_capture", body="secret-a")
    update_user(primary_conn, "usr_shadow_capture", body="secret-b")
    row = fetch_dirty_key(primary_conn, "users")
    assert row.key_json == {"user_id": "usr_shadow_capture"}
    assert "secret" not in json.dumps(row._asdict())
```

- [ ] **Step 2: Run tests and confirm missing implementation**

Run: `.venv-test/bin/python -m pytest tests/test_plaintext_shadow_change_capture.py -q`

Expected: FAIL because `change_capture` does not exist.

- [ ] **Step 3: Implement quoted, idempotent trigger DDL**

Use `psycopg.sql.Identifier` for table, trigger, and key identifiers. Each
trigger is `AFTER INSERT OR UPDATE OR DELETE`. Refuse installation if a
declared table or primary key does not match the live catalog. `audit()` must
report missing, unexpected, disabled, and mis-argumented triggers without
mutating the database.

- [ ] **Step 4: Test rollback and concurrent coalescing**

Add tests proving a rolled-back source transaction creates no dirty key, and
that the consumer's generation-guarded delete cannot remove a newer update.

Run: `.venv-test/bin/python -m pytest tests/test_plaintext_shadow_change_capture.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/plaintext_shadow/change_capture.py tests/test_plaintext_shadow_change_capture.py
git commit -m "feat: capture plaintext shadow dirty keys"
```

---

### Task 4: Target pool and fully decrypted transformation policy

**Files:**
- Modify: `backend/tee_shadow/mirror.py`
- Modify: `backend/tee_replicator/worker.py`
- Modify: `backend/tee_shadow/reconciler.py`
- Modify: `backend/tee_shadow/snapshot.py`
- Modify: `backend/tee_shadow/verify.py`
- Test: `tests/test_plaintext_shadow_target.py`
- Modify: `tests/test_tee_carry_verbatim.py`
- Modify: `tests/test_tee_mirror.py`

**Interfaces:**
- Produces: `mirror.get_target_pool()`, `worker.run_keys(table, keys, *, target_policy)`, `reconciler.reconcile_keys(table, keys, *, target_pool)`, and policy-aware verification.
- Consumes: `TargetPolicy(mode="plaintext_all")` and existing `_TABLES` translators.

- [ ] **Step 1: Write failing policy tests for explicit-on content**

```python
def test_plaintext_all_decrypts_explicit_on_user(monkeypatch):
    monkeypatch.setattr(worker, "_effective_encryption", lambda _uid: "on")
    decrypt = Mock(return_value=b"plain text")
    result = worker.transform_for_target(
        "usr_on", encrypted_doc(), decrypt=decrypt,
        target_policy=TargetPolicy(dsn="postgresql://shadow/db"),
    )
    assert result["body"] == "plain text"
    assert "body_ct" not in result
    assert "K_enclave" not in result

def test_legacy_target_still_carries_explicit_on_verbatim():
    assert legacy_transform_for_explicit_on(encrypted_doc()) == encrypted_doc()
```

- [ ] **Step 2: Run focused tests and observe the current carry-verbatim failure**

Run: `.venv-test/bin/python -m pytest tests/test_plaintext_shadow_target.py tests/test_tee_carry_verbatim.py -q`

Expected: FAIL because `_carries_verbatim()` currently returns true for explicit-on users without a target policy.

- [ ] **Step 3: Separate target selection from legacy TEE naming**

Keep `get_tee_pool()` as a compatibility alias for the legacy path. Add
`get_target_pool(policy)` and make pool construction consume `policy.dsn`.
`mirror.enabled()` must keep rejecting stale legacy `TEE_DATABASE_URL` when
`FEEDLING_DATABASE_SCHEMA=tee`, while allowing the independently validated
plaintext target:

```python
def enabled() -> bool:
    if load_target() is not None:
        return True
    return legacy_tee_shadow_enabled()
```

Pool failures remain bounded and must not raise into the primary request path.

- [ ] **Step 4: Add explicit target-policy transformation**

Replace the implicit `_carries_verbatim(user_id)` decision at transformation
call sites with:

```python
def _carries_verbatim(user_id: str, target_policy) -> bool:
    if target_policy is not None and target_policy.mode == "plaintext_all":
        return False
    return content_encryption_effective(user_id) == "on"
```

Thread the policy through retry, frame, identity, credential, summary,
trajectory, workspace, and voice paths. Encrypted input to `plaintext_all`
must always invoke enclave decryption; plaintext input remains byte-preserving.

- [ ] **Step 5: Add keyed replay primitives**

`worker.run_keys()` reuses each `_Table.requeue_fetch_sql`, transform, upsert,
and delete contract. If the source row no longer exists, apply the configured
destination delete. `reconciler.reconcile_keys()` performs the equivalent
same-shape current-row refresh for MIRROR tables. SNAPSHOT tables are coalesced
to one `snapshot_table()` invocation per drain batch.

- [ ] **Step 6: Run transformation and existing regression tests**

Run: `.venv-test/bin/python -m pytest tests/test_plaintext_shadow_target.py tests/test_tee_carry_verbatim.py tests/test_tee_mirror.py tests/test_tee_replicator_worker.py tests/test_tee_reconciler.py tests/test_tee_snapshot.py tests/test_tee_verify.py -q`

Expected: PASS; legacy tests retain their previous explicit-on behavior.

- [ ] **Step 7: Commit**

```bash
git add backend/tee_shadow backend/tee_replicator/worker.py tests/test_plaintext_shadow_target.py tests/test_tee_carry_verbatim.py tests/test_tee_mirror.py
git commit -m "feat: add fully decrypted shadow target policy"
```

---

### Task 5: Dirty-key drain, retry, and quarantine

**Files:**
- Create: `backend/plaintext_shadow/outbox.py`
- Test: `tests/test_plaintext_shadow_outbox.py`

**Interfaces:**
- Produces: `drain_once(*, limit: int = 500) -> DrainReport` and `retry_delay(attempts: int) -> timedelta`.
- Consumes: registry lanes, `worker.run_keys()`, `reconciler.reconcile_keys()`, `snapshot.snapshot_table()`, primary pool, and target pool.

- [ ] **Step 1: Write failing drain behavior tests**

```python
def test_success_acknowledges_only_claimed_generation(primary_conn):
    dirty = seed_dirty_key(primary_conn, generation=10)
    report = outbox.drain_once(limit=1)
    assert report.applied == 1
    assert fetch_dirty(primary_conn, dirty.key) is None

def test_newer_generation_survives_old_ack(primary_conn, monkeypatch):
    dirty = seed_dirty_key(primary_conn, generation=10)
    monkeypatch.setattr(outbox, "apply_key", lambda row: bump_generation(primary_conn, row, 11))
    outbox.drain_once(limit=1)
    assert fetch_dirty(primary_conn, dirty.key).generation == 11

def test_failure_records_slug_not_plaintext(primary_conn, monkeypatch):
    monkeypatch.setattr(outbox, "apply_key", Mock(side_effect=RuntimeError("secret body")))
    outbox.drain_once(limit=1)
    row = fetch_only_dirty(primary_conn)
    assert row.last_error_slug == "shadow_apply_failed"
    assert "secret body" not in json.dumps(row._asdict())
```

- [ ] **Step 2: Run tests and confirm missing consumer**

Run: `.venv-test/bin/python -m pytest tests/test_plaintext_shadow_outbox.py -q`

Expected: FAIL because `plaintext_shadow.outbox` does not exist.

- [ ] **Step 3: Implement non-blocking claim and generation-safe acknowledgement**

Claim ready rows with `FOR UPDATE SKIP LOCKED`, copy immutable key metadata,
and release the primary transaction before network/decryption work. On success:

```sql
DELETE FROM plaintext_shadow_dirty_keys
WHERE table_name = %s AND key_json = %s AND generation = %s
```

On failure, update retry state only under the same generation predicate. Use
bounded exponential delays of 30s, 60s, 120s, 240s, then 300s. Quarantine after
20 consecutive failures while keeping the row queryable and health-red.

- [ ] **Step 4: Route lanes and preserve current-state semantics**

- MIRROR: re-read current primary row and keyed-upsert or delete.
- CIPHERTEXT: re-read, enclave-decrypt, and keyed-upsert or delete.
- SNAPSHOT: deduplicate table names in the batch and run one atomic snapshot.
- SKIP/LOGICAL: reject as registry/configuration errors; never acknowledge.

The consumer must never use the original operation as authority. It re-reads
the current source: a delete followed by reinsertion becomes an upsert, and an
update followed by deletion becomes a delete.

- [ ] **Step 5: Add fault and ordering tests**

Cover duplicate replay, stale replay, delete/reinsert, update/delete, target
timeout, credential rejection, malformed ciphertext, quarantine, recovery,
and process restart with pending rows.

Run: `.venv-test/bin/python -m pytest tests/test_plaintext_shadow_outbox.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/plaintext_shadow/outbox.py tests/test_plaintext_shadow_outbox.py
git commit -m "feat: drain plaintext shadow changes durably"
```

---

### Task 6: Operator CLI, full copy, high-water drain, and strict verification

**Files:**
- Create: `backend/admin/plaintext_shadow.py`
- Modify: `backend/admin/tee_replication.py`
- Test: `tests/test_admin_plaintext_shadow.py`
- Modify: `tests/test_admin_tee_replication.py`

**Interfaces:**
- Produces CLI commands `preflight`, `record-restore-evidence`, `install-triggers`, `backfill`, `drain`, `verify`, `status`, and `remove-triggers`.
- Consumes: config validation, trigger audit/install, existing reconcile/replicate/snapshot/verify entry points, and dirty-key drain.

- [ ] **Step 1: Write failing CLI gate tests**

```python
def test_preflight_redacts_dsns(monkeypatch, capsys):
    monkeypatch.setenv("PLAINTEXT_SHADOW_DATABASE_URL", "postgresql://user:secret@host/db")
    rc = plaintext_shadow.main(["preflight"])
    output = capsys.readouterr().out
    assert "secret" not in output
    assert rc in (0, 2)

def test_enable_gate_requires_strict_verify(monkeypatch):
    monkeypatch.setattr(plaintext_shadow, "strict_report", lambda: {"ok": False})
    with pytest.raises(SystemExit):
        plaintext_shadow.main(["verify", "--require-green"])
```

- [ ] **Step 2: Run tests and confirm the command is absent**

Run: `.venv-test/bin/python -m pytest tests/test_admin_plaintext_shadow.py -q`

Expected: FAIL because the operator module does not exist.

- [ ] **Step 3: Implement redacted preflight and status**

Preflight checks configuration, independent DB identity, TLS verification,
server version, writable sync role, migration head, trigger audit, target
capacity, and an unexpired backup/restore evidence row. The
`record-restore-evidence` command accepts timestamps, schema head, a verifier
digest, operator identifier, and expiry; it rejects free-form notes. Output host/db fingerprints only,
never a conninfo string.

- [ ] **Step 4: Implement resumable initial population**

`install-triggers` runs before the copy. `backfill` records the current maximum
dirty-key generation as its high-water mark, executes existing MIRROR
reconcile, CIPHERTEXT replicate with `plaintext_all`, and SNAPSHOT copy, then
drains through and beyond that mark until no ready rows remain. Existing table
checkpoints remain resumable across process replacement.

- [ ] **Step 5: Implement strict activation verification**

Strict verification fails on migration mismatch, trigger audit failure,
pending/quarantined keys, table/key-count mismatch, stale versions, unexpected
ciphertext shape, decrypt failure, snapshot failure, backup staleness, or failed
restore evidence. It returns scalar counts and slugs only.

- [ ] **Step 6: Run CLI and existing admin tests**

Run: `.venv-test/bin/python -m pytest tests/test_admin_plaintext_shadow.py tests/test_admin_tee_replication.py tests/test_tee_verify.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/admin/plaintext_shadow.py backend/admin/tee_replication.py tests/test_admin_plaintext_shadow.py tests/test_admin_tee_replication.py
git commit -m "feat: add plaintext shadow operator gates"
```

---

### Task 7: Elected scheduler, health, and redacted admin visibility

**Files:**
- Create: `backend/admin/plaintext_shadow_scheduler.py`
- Modify: `backend/asgi/lifespan.py`
- Modify: `backend/admin/routes_asgi.py`
- Modify: `backend/db.py`
- Test: `tests/test_plaintext_shadow_scheduler.py`
- Modify: `tests/test_admin_tee_replication.py`

**Interfaces:**
- Produces: `_sync_tick() -> TickReport`, singleton name `plaintext-shadow-sync`, and `GET /v1/admin/plaintext-shadow/status`.
- Consumes: `outbox.drain_once()`, strict verification, `core.leader.run_singleton`, and `plaintext_shadow_sync_runs`.

- [ ] **Step 1: Write failing scheduler/health tests**

```python
def test_scheduler_does_not_start_when_disabled(monkeypatch):
    monkeypatch.setenv("FEEDLING_PLAINTEXT_SHADOW_ENABLED", "0")
    assert scheduler.should_start() is False

def test_status_contains_no_content_or_dsn(admin_client):
    payload = admin_client.get("/v1/admin/plaintext-shadow/status").json()
    rendered = json.dumps(payload)
    assert "postgresql://" not in rendered
    assert "body_ct" not in rendered
```

- [ ] **Step 2: Run tests and confirm missing scheduler/route**

Run: `.venv-test/bin/python -m pytest tests/test_plaintext_shadow_scheduler.py tests/test_admin_tee_replication.py -q`

Expected: FAIL because the scheduler and route do not exist.

- [ ] **Step 3: Implement one elected drain loop**

Start only after `config.validate_startup()` and only when enabled. Use the
existing advisory-lock leader helper, a 30-second default drain interval, a
five-minute verify interval, and bounded per-table backoff. Exceptions make the
tick unhealthy but never terminate ASGI startup after configuration validation
has passed.

- [ ] **Step 4: Persist scalar run metrics in the primary**

Record last success, duration, applied/deleted/retried/quarantined counts,
oldest pending age, pending count, target probe latency, verification status,
and per-table scalar summaries. Do not write reports containing content.

- [ ] **Step 5: Add redacted admin status**

Require the existing admin authentication. Return enabled state, schema head,
trigger audit, queue metrics, last run, target health, and latest backup/restore
evidence. Never return hosts, usernames, DSNs, SQL parameters, or exception
messages.

- [ ] **Step 6: Run scheduler and lifecycle tests**

Run: `.venv-test/bin/python -m pytest tests/test_plaintext_shadow_scheduler.py tests/test_admin_tee_replication.py tests/test_gunicorn_conf.py -q`

Expected: PASS and exactly one scheduler under a simulated multi-worker start.

- [ ] **Step 7: Commit**

```bash
git add backend/admin/plaintext_shadow_scheduler.py backend/asgi/lifespan.py backend/admin/routes_asgi.py backend/db.py tests/test_plaintext_shadow_scheduler.py tests/test_admin_tee_replication.py
git commit -m "feat: schedule and observe plaintext shadow sync"
```

---

### Task 8: CI, compose, and environment topology wiring

**Files:**
- Create: `deploy/check-database-topology.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `deploy/docker-compose.phala.yaml`
- Modify: `deploy/docker-compose.phala.runner.yaml`
- Modify: `deploy/docker-compose.phala.prod.runner.yaml`
- Modify: `deploy/docker-compose.phala.test.yaml`
- Modify: `deploy/docker-compose.phala.pre.yaml`
- Modify: `deploy/docker-compose.phala.pre.runner.yaml`
- Modify: `deploy/pin-runtime-release.sh`
- Test: `tests/test_database_topology_gate.py`
- Modify: `tests/test_pre_runtime_preflight.py`

**Interfaces:**
- Produces: a redacted topology validator invoked before each deploy.
- Consumes environment-specific `*_PLAINTEXT_SHADOW_DATABASE_URL` secrets and `*_FEEDLING_PLAINTEXT_SHADOW_ENABLED` variables.

- [ ] **Step 1: Write failing topology-gate tests**

```python
def test_gate_accepts_tee_primary_with_disabled_shadow():
    assert check(schema="tee", primary=PRIMARY, shadow="", enabled="0").ok

def test_gate_rejects_enabled_alias():
    result = check(schema="tee", primary=PRIMARY, shadow=PRIMARY, enabled="1")
    assert result.slug == "primary_shadow_alias"

def test_gate_rejects_legacy_shadow_in_tee_mode():
    result = check(schema="tee", primary=PRIMARY, tee_database_url=OLD, tee_dual_write="1")
    assert result.slug == "stale_legacy_shadow_config"
```

- [ ] **Step 2: Run tests and confirm the validator is absent**

Run: `.venv-test/bin/python -m pytest tests/test_database_topology_gate.py tests/test_pre_runtime_preflight.py -q`

Expected: FAIL because `deploy/check-database-topology.py` does not exist.

- [ ] **Step 3: Implement redacted topology validation**

The script parses DSNs with psycopg, prints only slugs and database identity
hashes, and enforces the same rules as `config.validate_startup()`. Add a drift
test that imports both rule sets and runs identical cases.

- [ ] **Step 4: Wire TEST and PRE with Gate 2 disabled**

Add encrypted secret inputs and explicit variables to both main and runner
deploy jobs. Default each environment's enable variable to `0`. Preserve legacy
TEE-shadow variables only for environments still on RDS primary; reject them
once their schema selector is `tee`.

- [ ] **Step 5: Wire PROD as two deploy gates**

Gate 1 passes the existing PROD TEE app DSN as `PROD_DATABASE_URL`, sets
`PROD_FEEDLING_DATABASE_SCHEMA=tee`, requires legacy TEE-shadow variables empty,
and forces plaintext shadow disabled. Gate 2 is a separate workflow dispatch or
protected environment step that requires the preflight/strict-verification
artifact before it may pass `PROD_FEEDLING_PLAINTEXT_SHADOW_ENABLED=1`.

- [ ] **Step 6: Verify compose rendering and CI tests**

Run: `.venv-test/bin/python -m pytest tests/test_database_topology_gate.py tests/test_pre_runtime_preflight.py -q`

Run: `docker compose -f deploy/docker-compose.phala.test.yaml config --quiet`

Run: `docker compose -f deploy/docker-compose.phala.pre.yaml config --quiet`

Run: `docker compose -f deploy/docker-compose.phala.yaml config --quiet`

Expected: all PASS without printing secret values.

- [ ] **Step 7: Commit**

```bash
git add deploy .github/workflows/ci.yml tests/test_database_topology_gate.py tests/test_pre_runtime_preflight.py
git commit -m "ci: gate tee primary plaintext shadow topology"
```

---

### Task 9: Runbooks, public architecture, and changelog

**Files:**
- Modify: `docs/CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md`
- Modify: `deploy/DEPLOYMENTS.md`
- Modify: `docs-site/content/docs/architecture.mdx`
- Modify: `docs-site/content/docs/self-hosting.mdx`
- Modify: every workflow page returned by `rg -l "TEE_DATABASE_URL|TEE primary" docs-site/content/docs/workflows`
- Modify: `docs-site/content/docs/changelog.mdx`
- Modify: `docs-site/openapi/public.json` only if the public OpenAPI source changes

**Interfaces:**
- Produces: operator-ready Gate 1/Gate 2 instructions and an accurate public trust-boundary description.
- Consumes: finalized configuration names, CLI commands, metrics, and rollback semantics from Tasks 1–8.

- [ ] **Step 1: Update the operator runbook**

Document exact preflight, freeze, Phase 4, main/runner switch, Gate 1 canaries,
trigger installation, full copy, high-water drain, strict verification, Gate 2
activation, observation, disable, and recovery commands. State that TEE SQL is
a derived plaintext copy and not an automatic failover target.

- [ ] **Step 2: Update trust-boundary and architecture documentation**

The diagram must show both databases, enclave decryption on the replication
edge, sensitive plaintext backups, least-privilege roles, and asynchronous
failure isolation. Explicitly document that explicit-on content is plaintext in
TEE SQL even though it remains encrypted in the authoritative primary.

- [ ] **Step 3: Add an Unreleased changelog entry**

Record the topology, new deployment variables, admin status surface, two-gate
rollout, and security implications. Do not include private hostnames or role
names.

- [ ] **Step 4: Regenerate OpenAPI only if route visibility changes public API**

The planned route is admin-only, so public OpenAPI should remain unchanged. Run
the generator and require a clean diff if the source was touched:

Run: `cd docs-site && npm run openapi:generate`

- [ ] **Step 5: Verify documentation**

Run: `cd docs-site && npm run types:check`

Run: `cd docs-site && npm run lint`

Run: `cd docs-site && npm run build`

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add docs deploy/DEPLOYMENTS.md docs-site
git commit -m "docs: document tee primary plaintext shadow rollout"
```

---

### Task 10: Full verification and release-candidate evidence

**Files:**
- Modify: `docs/superpowers/plans/2026-08-19-prod-tee-primary-plaintext-shadow.md` only to check completed steps during execution.
- Create: no release evidence file containing secrets; attach redacted command outputs to the promotion PR.

**Interfaces:**
- Produces: a reviewable release candidate and redacted TEST/PRE evidence.
- Consumes: all prior tasks.

- [ ] **Step 1: Run the focused plaintext-shadow suite**

Run: `.venv-test/bin/python -m pytest tests/test_plaintext_shadow_config.py tests/test_plaintext_shadow_schema.py tests/test_plaintext_shadow_change_capture.py tests/test_plaintext_shadow_target.py tests/test_plaintext_shadow_outbox.py tests/test_admin_plaintext_shadow.py tests/test_plaintext_shadow_scheduler.py tests/test_database_topology_gate.py -q`

Expected: PASS.

- [ ] **Step 2: Run existing TEE regression coverage**

Run: `.venv-test/bin/python -m pytest tests/test_tee_mirror.py tests/test_tee_reconciler.py tests/test_tee_replicator_worker.py tests/test_tee_requeue.py tests/test_tee_snapshot.py tests/test_tee_verify.py tests/test_tee_table_registry.py tests/test_phase4_cutover.py -q`

Expected: PASS.

- [ ] **Step 3: Run the local backend suite**

Run: `FEEDLING_TEST_PG=postgresql://postgres:test@127.0.0.1:55432/postgres .venv-test/bin/python -m pytest tests -q --ignore=tests/test_api.py`

Expected: PASS.

- [ ] **Step 4: Run secret and content leakage scans**

Run: `git diff --cached -- . ':(exclude)docs/superpowers/plans/2026-08-19-prod-tee-primary-plaintext-shadow.md' | rg -n "gateway\\.attestmesh\\.xyz|teleport_admin|postgresql://[^[:space:]]+:[^[:space:]@]+@"`

Expected: no private gateway hostname or administrator role in the staged diff;
only clearly synthetic DSNs inside tests may match the DSN pattern. Inspect and
account for every match before committing.

- [ ] **Step 5: Request code review and target the correct branch**

Create or update a PR targeting `test`, not `main`, from the implementation
branch. Resolve review findings and preserve the repository's test→pre→main
promotion flow.

- [ ] **Step 6: Deploy and validate TEST**

Provision two independent TEST databases, run migrations, install triggers,
backfill, drain, strict verify, and enable the shadow. Exercise explicit-on and
off accounts across Chat, Memory, World Book, Runtime V2, Perception, Broadcast,
Genesis, identity, BYOK credentials, voice, deletes, updates, and process
restart. Inject a target outage and verify primary success plus later recovery.

- [ ] **Step 7: Promote and validate PRE**

Repeat the TEST procedure in PRE, including backup creation, restore to an
isolated database, multi-worker election, independent runner parity, and an
observation interval long enough to include scheduled reconciliation.

- [ ] **Step 8: Produce the PROD change sheet**

Record the exact release SHA, current and target migration heads, redacted DB
identity fingerprints, queue-drain commands, freeze owner, rollback owner,
backup/restore evidence, Gate 1 and Gate 2 commands, canary user IDs, observation
duration, and abort thresholds. Obtain maintainer approval in the production
promotion PR.

- [ ] **Step 9: Commit verification-only adjustments**

```bash
git add docs/superpowers/plans/2026-08-19-prod-tee-primary-plaintext-shadow.md
git commit -m "test: record plaintext shadow release evidence"
```

---

### Task 11: PROD Gate 1 — promote the existing TEE database

**Files:**
- No repository file changes during the maintenance operation.
- GitHub environment secrets/variables and Phala deployments change externally under the approved promotion workflow.

**Interfaces:**
- Produces: existing PROD TEE PostgreSQL as authoritative primary with plaintext shadow disabled.
- Consumes: approved PROD change sheet, release SHA, Phase 4 tooling, and verified backups.

- [ ] **Step 1: Verify Gate 1 prerequisites read-only**

Run Phase 4 dry-run from the exact release and confirm schema heads, prepared
marker readiness, backups, restore evidence, drained queues, runner count, and
`FEEDLING_PLAINTEXT_SHADOW_ENABLED=0`.

- [ ] **Step 2: Freeze writes and drain durable work**

Stop API writes, main worker writes, and independent runner writes. Drain
Genesis, voice, agent jobs, action queue, Runtime V2 outboxes, and pending TEE
device migrations using the commands documented in the updated runbook.

- [ ] **Step 3: Apply Phase 4 and switch all units together**

Run:

```bash
cd backend
python -m admin.phase4_cutover --apply --confirm-writes-frozen
```

Deploy main and every runner with the same existing TEE app DSN as
`DATABASE_URL`, `FEEDLING_DATABASE_SCHEMA=tee`, legacy TEE shadow variables
empty, and plaintext shadow disabled.

- [ ] **Step 4: Validate before and after resuming traffic**

Confirm startup marker/head, API and worker health, runner fleet identity,
empty failed queues, encrypted explicit-on canaries, plaintext off canaries,
cross-user denial, backups, and unchanged frozen-source counts. Resume traffic
only after all checks pass.

- [ ] **Step 5: Declare the authority boundary**

After the first TEE-primary write, record that the existing TEE database is the
only authority. Do not switch back to the frozen managed database without a
separate reverse-reconcile or restore plan.

---

### Task 12: PROD Gate 2 — provision, rotate credentials, backfill, and activate TEE SQL

**Files:**
- Update the private operator record at `/Users/zhengzhihao/documents/teleport/tee-sql/db.md`; never commit it.
- GitHub environment secrets/variables change externally under protected PROD approval.

**Interfaces:**
- Produces: TEE SQL as the converged decrypted plaintext shadow with old administrator access rotated.
- Consumes: working verified gateway, bootstrap administrator access, new operator CLI, strict verification, and Gate 1 stability evidence.

- [ ] **Step 1: Stop if the TLS endpoint is still unhealthy**

Run the documented `openssl s_client` hostname-verification probe and the new
`python -m admin.plaintext_shadow preflight`. Expected: verified TLS handshake,
PostgreSQL 17 response, distinct DB identity, and no secret output. Any EOF,
certificate error, or identity alias blocks Gate 2.

- [ ] **Step 2: Create replacement least-privilege roles**

Using the bootstrap administrator through the verified tunnel, create one
owner/migration role and one sync role. Generate independent random passwords
with `openssl rand -hex 32`; place them only in the approved secret store. Grant
the sync role CONNECT, schema USAGE, required table DML, and sequence usage; do
not grant role creation, database creation, replication, bypass RLS, or
superuser.

- [ ] **Step 3: Validate new credentials before touching the old password**

Run migrations with `PLAINTEXT_SHADOW_MIGRATION_DATABASE_URL`, then run
preflight with the sync DSN. Confirm owner-only DDL is denied to the sync role
and routine DML is allowed.

- [ ] **Step 4: Install primary triggers and run initial population**

With the runtime gate disabled, run `install-triggers`, `backfill`, repeated
`drain`, and `verify --require-green`. Confirm zero pending/quarantined keys,
all table/key and content-shape checks green, and explicit-on canaries plaintext
only in TEE SQL.

- [ ] **Step 5: Prove backup and restore before activation**

Create a TEE SQL backup, restore it into an isolated database, run schema and
sampled content-digest verification, then destroy access to the temporary
restore according to the infrastructure retention procedure. Record redacted
timestamps and checksums in the promotion evidence.

- [ ] **Step 6: Activate and observe Gate 2**

Set `PROD_FEEDLING_PLAINTEXT_SHADOW_ENABLED=1` through the protected workflow.
Verify live inserts, updates, deletes, explicit-on decryption, retry recovery,
oldest-pending age, target health, pool pressure, and strict convergence during
the approved observation interval.

- [ ] **Step 7: Rotate the exposed administrator credential last**

Confirm no active session or secret references the old bootstrap credential.
Rotate or disable it, test the new emergency administrator credential from the
verified operator path, update the private credential file with mode `0600`,
and correct the inconsistent paths in `db.md`. Do not print either password.

- [ ] **Step 8: Exercise the reversible Gate 2 disable path**

Disable the runtime gate, create a primary canary, confirm the primary remains
healthy and its dirty key persists, re-enable the gate, and verify the canary
converges. This proves TEE SQL failure is isolated from production writes.
