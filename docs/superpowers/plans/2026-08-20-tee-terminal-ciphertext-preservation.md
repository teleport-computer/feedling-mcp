# TEE Terminal Ciphertext Preservation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve terminal ciphertext rows byte-for-byte in the promoted TEE database without changing user encryption preferences, while keeping strict verification and Phase 4 fail-closed.

**Architecture:** A focused `tee_replicator.terminal_preservation` module owns marker parsing, canonical row hashing, table contracts, dry-run planning, guarded apply, and guarded pre-cutover revert. Verification audits every preserved marker against its raw source and destination projection; Phase 4 blocks only unresolved/requeue/malformed pending rows and reports valid preserved rows as evidence.

**Tech Stack:** Python 3.11, psycopg 3, PostgreSQL 17-compatible SQL, pytest, Alembic TEE head checks, MDX documentation.

**Spec:** `docs/superpowers/specs/2026-08-20-tee-terminal-ciphertext-preservation-design.md`

## Global Constraints

- Work from release baseline `a9be073a0787f9f561548ee7e9239f3c4a4249c7` in the isolated `fix/tee-terminal-ciphertext-preservation` worktree.
- Do not modify `users.doc.content_encryption`.
- Do not call the enclave or fetch frame bodies from R2.
- Never log user IDs, item IDs, bodies, keys, original reasons, or DSNs.
- Apply is allowed only at the checked-out release's exact TEE Alembic head and with exact count/digest compare-and-apply guards.
- Unknown tables, unknown marker versions, missing source rows/users, schema drift, and differing destination rows fail closed.
- A destination write and its preserved marker update must commit in the same TEE transaction.
- No new Alembic revision is introduced; audit state remains in the versioned pending reason.
- Every production behavior change follows RED → GREEN → focused regression before commit.
- PROD execution remains dry-run-only until a separate explicit write confirmation.

---

### Task 1: Marker Codec, Canonical Digests, and Table Contracts

**Files:**
- Create: `backend/tee_replicator/terminal_preservation.py`
- Create: `tests/test_tee_terminal_preservation.py`

**Interfaces:**
- Produces: `is_terminal_reason(reason: str) -> bool`
- Produces: `encode_preserved_reason(row_sha256: str, original_reason: str) -> str`
- Produces: `parse_preserved_reason(reason: str) -> tuple[str, str] | None`
- Produces: `canonical_row_sha256(table: str, row: tuple) -> str`
- Produces: immutable `_Contract` values in `CONTRACTS` for `chat_messages`, `memory_moments`, `identity`, and `frame_envelopes`

- [ ] **Step 1: Add failing pure tests for strict terminal and marker classification**

```python
def test_preserved_marker_round_trips_original_reason():
    reason = "decrypt_failed:enclave_http_403"
    encoded = preservation.encode_preserved_reason("a" * 64, reason)
    assert preservation.parse_preserved_reason(encoded) == ("a" * 64, reason)
    assert preservation.is_terminal_reason(encoded) is False


@pytest.mark.parametrize("reason", [
    "decrypt_failed:old-key", "pdm:local-only", "visibility_local_only",
])
def test_only_unpreserved_terminal_reasons_are_eligible(reason):
    assert preservation.is_terminal_reason(reason) is True


@pytest.mark.parametrize("reason", [
    "requeue", "requeue:source_updated", "preserved_ciphertext:v2:bad:bad",
    "preserved_ciphertext:v1:not-a-digest:bad", "",
])
def test_nonterminal_or_malformed_reasons_are_not_eligible(reason):
    assert preservation.is_terminal_reason(reason) is False
    assert preservation.parse_preserved_reason(reason) is None
```

- [ ] **Step 2: Run marker tests and verify RED**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
PYTHONPATH=backend \
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest -p no:cacheprovider \
  tests/test_tee_terminal_preservation.py -q
```

Expected: collection fails because `tee_replicator.terminal_preservation` does not exist.

- [ ] **Step 3: Implement the strict codec and canonical serializer**

```python
PRESERVED_PREFIX = "preserved_ciphertext:v1:"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def is_terminal_reason(reason: str) -> bool:
    value = str(reason or "")
    return (
        value.startswith("decrypt_failed:")
        or value.startswith("pdm:")
        or value == "visibility_local_only"
    )


def encode_preserved_reason(row_sha256: str, original_reason: str) -> str:
    if not _DIGEST_RE.fullmatch(row_sha256) or not is_terminal_reason(original_reason):
        raise ValueError("invalid_preserved_marker_input")
    encoded = base64.urlsafe_b64encode(original_reason.encode()).decode().rstrip("=")
    return f"{PRESERVED_PREFIX}{row_sha256}:{encoded}"


def parse_preserved_reason(reason: str) -> tuple[str, str] | None:
    value = str(reason or "")
    if not value.startswith(PRESERVED_PREFIX):
        return None
    digest, separator, encoded = value[len(PRESERVED_PREFIX):].partition(":")
    if not separator or not _DIGEST_RE.fullmatch(digest) or not encoded:
        return None
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(
            encoded + padding, altchars=b"-_", validate=True)
        original_reason = raw.decode("utf-8", "strict")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None
    if not is_terminal_reason(original_reason):
        return None
    return digest, original_reason


def canonical_row_sha256(table: str, row: tuple) -> str:
    payload = json.dumps(
        [table, *row], sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, default=str,
    ).encode()
    return hashlib.sha256(payload).hexdigest()
```

The parser restores URL-safe base64 padding, validates the alphabet, decodes
UTF-8 strictly, and returns `None` for every malformed input.

- [ ] **Step 4: Add and pass deterministic digest and contract tests**

```python
def test_canonical_digest_is_stable_across_json_key_order():
    first = preservation.canonical_row_sha256(
        "chat_messages", ("u", "m", 1.0, {"b": 2, "a": 1}, 7, 0))
    second = preservation.canonical_row_sha256(
        "chat_messages", ("u", "m", 1.0, {"a": 1, "b": 2}, 7, 0))
    assert first == second


def test_contracts_are_exactly_the_four_approved_families():
    assert set(preservation.CONTRACTS) == {
        "chat_messages", "memory_moments", "identity", "frame_envelopes",
    }
```

Run the Task 1 test command. Expected: all Task 1 tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add backend/tee_replicator/terminal_preservation.py \
  tests/test_tee_terminal_preservation.py
git commit -m "feat: define terminal ciphertext preservation contracts"
```

---

### Task 2: Read-Only Plan and Guarded Apply

**Files:**
- Modify: `backend/tee_replicator/terminal_preservation.py`
- Modify: `tests/test_tee_terminal_preservation.py`

**Interfaces:**
- Consumes: `CONTRACTS`, marker codec, and canonical row digest from Task 1
- Produces: `build_plan(source: psycopg.Connection, destination: psycopg.Connection) -> PreservationPlan`
- Produces: `apply_plan(source, destination, plan, *, expected_count: int, expected_plan_sha256: str) -> dict`
- Produces: `PreservationPlan.rows: tuple[PlannedRow, ...]`, `.sha256: str`, `.counts: dict[str, int]`, `.blockers: tuple[str, ...]`

- [ ] **Step 1: Add a failing integration test for dry-run planning**

Seed isolated RDS/TEE databases with one parent user plus one terminal row for
each contract. Keep the user's `doc` as `{}` and snapshot it before planning.

```python
plan = preservation.build_plan(source, destination)
assert plan.counts == {
    "chat_messages": 1, "frame_envelopes": 1,
    "identity": 1, "memory_moments": 1,
}
assert len(plan.rows) == 4
assert re.fullmatch(r"[0-9a-f]{64}", plan.sha256)
assert destination.execute(
    "SELECT count(*) FROM tee_pending_device_migration"
).fetchone()[0] == 4
assert destination.execute(
    "SELECT count(*) FROM chat_messages"
).fetchone()[0] == 0
assert source.execute(
    "SELECT doc FROM users WHERE user_id=%s", (uid,)
).fetchone()[0] == {}
```

- [ ] **Step 2: Run the dry-run test and verify RED**

Run the Task 1 command narrowed to
`tests/test_tee_terminal_preservation.py::test_build_plan_is_read_only_and_stable`.

Expected: FAIL because `build_plan` and `PreservationPlan` do not exist.

- [ ] **Step 3: Implement stable planning with fail-closed blockers**

Implement `_Contract` SQL for these exact projections:

```python
"chat_messages":
    SELECT user_id,msg_id,ts,doc,seq,storage_generation
"memory_moments":
    SELECT user_id,moment_id,occurred_at,doc
"identity":
    SELECT user_id,kind,doc FROM user_blobs
    WHERE user_id=%s AND kind='identity'
"frame_envelopes":
    SELECT user_id,frame_id,ts,doc,env_meta,body_key
```

Planning must query pending rows in `(table_name,user_id,item_id)` order, accept
only `is_terminal_reason`, confirm the destination parent `users` row exists,
fetch exactly one source row, and detect destination state as `absent`, `exact`,
or `conflict`. Unknown terminal tables and every missing/conflicting condition
append a redacted blocker code such as `unknown_table:1` or
`missing_source:frame_envelopes:31`; blockers never include identifiers.

Compute the plan digest from sorted `(table, row_digest, original_reason_class)`
tuples, not from report ordering or connection-specific values.

- [ ] **Step 4: Add failing apply guard tests**

```python
@pytest.mark.parametrize("count,digest", [
    (3, "a" * 64), (4, "b" * 64),
])
def test_apply_rejects_stale_compare_and_apply_guard(db_pair, count, digest):
    before = snapshot_destination(db_pair.destination)
    with pytest.raises(preservation.PreservationRefused):
        preservation.apply_plan(
            db_pair.source, db_pair.destination,
            preservation.build_plan(*db_pair),
            expected_count=count, expected_plan_sha256=digest,
        )
    assert snapshot_destination(db_pair.destination) == before
```

Also add tests that unknown tables, missing parents, missing source rows, and a
different destination row make `plan.blockers` non-empty and prevent all
writes. An unpreserved terminal marker with an exact pre-existing destination
row must also block: without an existing valid preservation marker, revert
cannot prove that the preservation operation owns that row.

- [ ] **Step 5: Run guard tests and verify RED**

Expected: FAIL because `apply_plan` and `PreservationRefused` do not exist.

- [ ] **Step 6: Implement one-transaction apply**

`apply_plan` must:

1. reject `plan.blockers`;
2. compare exact row count and plan SHA-256;
3. re-run `build_plan` inside a repeatable-read, read-only source transaction;
4. compare the recomputed count/digest again;
5. open one destination transaction;
6. insert only absent rows using explicit column lists and
   `OVERRIDING SYSTEM VALUE` for Chat `seq`;
7. leave exact destination rows untouched only when the current pending reason
   is already a valid preservation marker; reject unmarked exact rows as
   unowned;
8. update each pending row with
   `encode_preserved_reason(row.row_sha256, row.original_reason)` using a
   `WHERE reason = %s` compare-and-swap;
9. require every marker update to affect exactly one row;
10. return only aggregate counts and digests.

Any exception rolls back the entire destination transaction.

- [ ] **Step 7: Add passing exact-copy, idempotence, mutation, and privacy tests**

Assert all selected source columns equal destination columns, markers parse to
the original reasons, Frames do not call R2/enclave helpers, user documents are
unchanged, and `caplog` contains none of the seeded user/item identifiers. Then
invoke the real `tee_shadow.mirror.mark_pending` path for a preserved key and
assert its reason becomes `requeue`, proving a later source mutation is blocking
again instead of being hidden by the preservation marker.
Run all of `tests/test_tee_terminal_preservation.py`. Expected: pass.

- [ ] **Step 8: Commit Task 2**

```bash
git add backend/tee_replicator/terminal_preservation.py \
  tests/test_tee_terminal_preservation.py
git commit -m "feat: add guarded terminal ciphertext preservation"
```

---

### Task 3: Guarded Pre-Cutover Revert and Release-Local CLI

**Files:**
- Create: `backend/admin/tee_terminal_preservation.py`
- Modify: `backend/tee_replicator/terminal_preservation.py`
- Modify: `tests/test_tee_terminal_preservation.py`
- Create: `tests/test_tee_terminal_preservation_cli.py`

**Interfaces:**
- Produces: `build_revert_plan(source: psycopg.Connection, destination: psycopg.Connection) -> PreservationPlan`
- Produces: `revert_plan(source, destination, plan, *, expected_count: int, expected_plan_sha256: str) -> dict`
- Produces: `admin.tee_terminal_preservation.run(*, apply: bool, revert: bool, confirm: str | None, expected_count: int | None, expected_plan_sha256: str | None) -> dict`
- CLI default: dry-run; apply confirm is `PRESERVE-TERMINAL-CIPHERTEXT`; revert confirm is `REVERT-PRESERVED-CIPHERTEXT`

- [ ] **Step 1: Add failing tests for exact revert safety**

```python
plan = preservation.build_plan(source, destination)
applied = preservation.apply_plan(
    source, destination, plan,
    expected_count=len(plan.rows), expected_plan_sha256=plan.sha256,
)
report = preservation.revert_plan(
    source, destination, preservation.build_revert_plan(source, destination),
    expected_count=applied["preserved"],
    expected_plan_sha256=applied["plan_sha256"],
)
assert report["reverted"] == 4
assert destination.execute("SELECT count(*) FROM chat_messages").fetchone()[0] == 0
assert pending_reasons(destination) == original_reasons
```

Add refusal tests for an existing Phase 4 prepared marker, a destination row
whose digest changed, wrong guard values, malformed preserved markers, and an
unmarked exact destination row whose ownership cannot be proven.

- [ ] **Step 2: Run revert tests and verify RED**

Expected: FAIL because revert planning and execution do not exist.

- [ ] **Step 3: Implement revert with marker-embedded original reasons**

Revert may select only valid `preserved_ciphertext:v1` markers. It verifies the
current source and destination digests against the marker, refuses when the
Phase 4 prepared marker exists, deletes only exact preserved destination rows,
and restores the decoded original reason with compare-and-swap in one TEE
transaction. It does not delete parents or RDS data.

- [ ] **Step 4: Add failing CLI guard tests**

Patch connection creation and core calls, then assert:

```python
assert cli.run(apply=False, revert=False, confirm=None,
               expected_count=None, expected_plan_sha256=None)["mode"] == "dry-run"
with pytest.raises(RuntimeError, match="confirm mismatch"):
    cli.run(apply=True, revert=False, confirm="MIGRATE",
            expected_count=4, expected_plan_sha256="a" * 64)
with pytest.raises(RuntimeError, match="mutually exclusive"):
    cli.run(apply=True, revert=True, confirm="PRESERVE-TERMINAL-CIPHERTEXT",
            expected_count=4, expected_plan_sha256="a" * 64)
```

Also assert the CLI rejects missing/invalid count or digest, same source/TEE
fingerprints, mismatched TEE app/owner fingerprints, and a TEE head different
from the checkout's `ScriptDirectory.get_heads()`.

- [ ] **Step 5: Implement CLI and JSON aggregate output**

Use `argparse` flags `--apply`, `--revert`, `--confirm`, `--expected-count`, and
`--expected-plan-sha256`. Dry-run connects only from `DATABASE_URL` and the
read-only/app `TEE_DATABASE_URL`; apply and revert additionally require
`TEE_MIGRATION_DATABASE_URL`. Set the source transaction read-only and compare
the applicable database fingerprints plus TEE heads before dispatch. Print one JSON report
containing mode, counts by table/reason class, blockers, plan digest, and
database fingerprints only.

- [ ] **Step 6: Run Task 3 tests and focused regression**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
PYTHONPATH=backend \
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest -p no:cacheprovider \
  tests/test_tee_terminal_preservation.py \
  tests/test_tee_terminal_preservation_cli.py -q
```

Expected: pass with no secret/identifier output.

- [ ] **Step 7: Commit Task 3**

```bash
git add backend/admin/tee_terminal_preservation.py \
  backend/tee_replicator/terminal_preservation.py \
  tests/test_tee_terminal_preservation.py \
  tests/test_tee_terminal_preservation_cli.py
git commit -m "feat: add reversible TEE preservation CLI"
```

---

### Task 4: Strict Verification of Every Preserved Row

**Files:**
- Modify: `backend/tee_replicator/terminal_preservation.py`
- Modify: `backend/tee_shadow/verify.py:230-470`
- Modify: `tests/test_tee_terminal_preservation.py`
- Modify: `tests/test_tee_verify.py`

**Interfaces:**
- Consumes: `parse_preserved_reason`, `canonical_row_sha256`, and contracts from Task 1
- Produces: `_split_pending(pending_rows: list[tuple[str, str, str]]) -> PendingSplit` with `.terminal`, `.preserved`, and `.requeue_backlog`
- Produces in `tee_replicator.terminal_preservation`: `audit_preserved(source: psycopg.Connection, destination: psycopg.Connection, markers: Sequence[PreservedPending]) -> PreservationAudit`
- Adds report fields: `preserved_ciphertext`, `preserved_mismatches`, `blocking_pending`

- [ ] **Step 1: Add failing split and row-equation tests**

Seed one terminal marker, one requeue marker, and one valid preserved marker.
Assert the preserved row is not added to `rds == tee + terminal`, requeue remains
backlog, and terminal remains missing-row compensation.

```python
split = verify._split_pending(rows)
assert split.terminal == [(uid_terminal, item_terminal)]
assert len(split.preserved) == 1
assert split.requeue_backlog == 1
```

- [ ] **Step 2: Run the new verify tests and verify RED**

Expected: FAIL because `_split_pending` returns the old two-tuple and does not
recognize preserved markers.

- [ ] **Step 3: Implement strict pending split without weakening malformed markers**

Use a frozen `PendingSplit` dataclass. Valid v1 markers enter `.preserved`;
malformed `preserved_ciphertext` strings remain in `.terminal` and therefore
block. Preserve the existing requeue semantics.

- [ ] **Step 4: Add failing raw-equality tests for all four families**

For each family seed a source row, exact destination raw row, and valid marker.
Assert strict verify is green and reports one preserved row. Then mutate one
destination field and assert strict verify returns a red mismatch with only
table and field labels. For Frames, seed `frame_envelopes` bridge only and
assert the test never reads `frames` for that preserved key.

- [ ] **Step 5: Implement deterministic full preserved audit in the shared core**

Audit every marker, fetch its exact source and target projection through the
shared preservation contracts, recompute the source digest, compare it with the
marker and canonical destination digest, and return redacted mismatches. Exclude
preserved keys from random plaintext/decrypt sampling because they have already
received a stronger full raw audit.

Keep `audit_preserved` in `tee_replicator.terminal_preservation`, so strict
verify and Phase 4 consume the same public result instead of importing each
other's private helpers.

For frame row counts use:

```text
effective TEE rows = projected `frames` rows + valid preserved bridge rows
```

Deduplicate by primary key and treat a key present in both forms as a mismatch,
not as two valid rows.

- [ ] **Step 6: Run verification and carry-verbatim regression**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
PYTHONPATH=backend \
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest -p no:cacheprovider \
  tests/test_tee_verify.py tests/test_tee_carry_verbatim.py \
  tests/test_tee_terminal_preservation.py -q
```

Expected: pass.

- [ ] **Step 7: Commit Task 4**

```bash
git add backend/tee_replicator/terminal_preservation.py \
  backend/tee_shadow/verify.py \
  tests/test_tee_terminal_preservation.py tests/test_tee_verify.py
git commit -m "feat: verify preserved TEE ciphertext exactly"
```

---

### Task 5: Phase 4 Blocking Gate and Prepared Evidence

**Files:**
- Modify: `backend/admin/phase4_cutover.py:37-110,300-370`
- Modify: `tests/test_phase4_cutover.py`

**Interfaces:**
- Consumes: marker parser and `terminal_preservation.audit_preserved` from Task 4
- Produces: `_pending_gate(destination) -> dict[str, object]`
- Phase 4 report fields: `tee_pending_device_migration_blocking`, `tee_terminal_ciphertext_preserved`, `preserved_plan_sha256`

- [ ] **Step 1: Add failing Phase 4 gate tests**

Extend the isolated database test with exact preserved Chat and Frame rows plus
valid markers. Assert dry-run and apply permit them, report their count/digest,
and retain markers after `_copy_frame_bridge`.

Add parametrized refusal tests for `requeue`, unpreserved terminal, malformed
preserved marker, missing preserved destination row, digest drift, and duplicate
Frame representation.

- [ ] **Step 2: Run Phase 4 tests and verify RED**

Expected: valid preserved markers still fail because current code counts every
physical pending row as blocking.

- [ ] **Step 3: Implement audited blocking count**

`_pending_gate` invokes the same full preserved audit used by strict verify and
returns only aggregate evidence. Set
`tee_pending_device_migration_blocking` to requeue + unpreserved + malformed +
preserved mismatches. Keep `tee_terminal_ciphertext_preserved` informational.

Embed the preserved count and aggregate digest in the Phase 4 result that is
serialized into the prepared marker. Do not alter other drain gates.

- [ ] **Step 4: Run Phase 4 and startup contract regression**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
PYTHONPATH=backend \
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest -p no:cacheprovider \
  tests/test_phase4_cutover.py tests/test_tee_verify.py \
  tests/test_tee_terminal_preservation.py -q
```

Expected: pass.

- [ ] **Step 5: Commit Task 5**

```bash
git add backend/admin/phase4_cutover.py tests/test_phase4_cutover.py
git commit -m "feat: admit audited ciphertext at Phase 4 gate"
```

---

### Task 6: Operator Runbook and Public Trust-Boundary Documentation

**Files:**
- Modify: `docs/CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md:45-85`
- Modify: `docs-site/content/docs/architecture.mdx:430-470`
- Modify: `docs-site/content/docs/self-hosting.mdx`
- Modify: `docs-site/content/docs/changelog.mdx`
- Modify: `.github/workflows/ci.yml:640-670,770-790`

**Interfaces:**
- Documents the exact CLI, dry-run/apply/revert confirmations, schema-before-
  preservation ordering, blocking-vs-preserved gate, and no-preference-mutation invariant
- Adds new preservation tests to focused CI groups

- [ ] **Step 1: Update internal runbook and CI**

Document this exact sequence: stop old writer → back up → migrate TEE to exact
release head → dry-run preservation → approve exact digest/count → apply →
strict verify → Phase 4 dry-run/apply. State that preserved ciphertext remains
device-readable only if `K_user` is valid and does not become enclave-readable.

Add both new tests beside existing TEE reflow/Phase 4 CI coverage.

- [ ] **Step 2: Update public architecture, self-hosting trust model, and changelog**

Explain that mixed historical ciphertext can be preserved in a TEE-primary
database without silently changing account preference, and that the migration
gate distinguishes cryptographically audited preservation from unresolved
pending work. Add an `Unreleased` changelog item. Do not expose PROD counts or
incident-specific dates in public docs.

- [ ] **Step 3: Run behavioral documentation and configuration checks**

```bash
PYTHONPATH=backend \
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest -p no:cacheprovider tests/test_deploy_yaml_strict.py -q
cd docs-site
npm run types:check
npm run lint
npm run build
```

Expected: all pass.

- [ ] **Step 4: Commit Task 6**

```bash
git add .github/workflows/ci.yml \
  docs/CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md \
  docs-site/content/docs/architecture.mdx \
  docs-site/content/docs/self-hosting.mdx \
  docs-site/content/docs/changelog.mdx
git commit -m "docs: add terminal ciphertext preservation runbook"
```

---

### Task 7: Full Verification and PROD Read-Only Inventory

**Files:**
- Modify only if verification finds a defect covered by a new failing test
- Record evidence in the PR body; do not commit secrets or production identifiers

**Interfaces:**
- Consumes all prior tasks
- Produces release evidence: focused suite, full backend suite, docs build,
  OpenAPI contract checks, static checks, and a redacted PROD dry-run plan

- [ ] **Step 1: Run focused TEE suite**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
PYTHONPATH=backend \
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest -p no:cacheprovider \
  tests/test_tee_terminal_preservation.py \
  tests/test_tee_terminal_preservation_cli.py \
  tests/test_tee_replicator_frames.py \
  tests/test_tee_reflow.py \
  tests/test_tee_verify.py \
  tests/test_tee_carry_verbatim.py \
  tests/test_tee_replicator_worker.py \
  tests/test_tee_replicator_transforms.py \
  tests/test_phase4_cutover.py \
  tests/test_deploy_yaml_strict.py -q
```

Expected: all pass with only known Alembic deprecation warnings.

- [ ] **Step 2: Run full backend and contract suites**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest tests -q --ignore=tests/e2e_model_api_test.py
PYTHONPATH=backend \
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest -p no:cacheprovider tests/test_openapi_contract.py -q
```

Expected: no new failures relative to the verified release baseline.

- [ ] **Step 3: Run repository hygiene checks**

```bash
git diff --check
git status --short
PYTHONPATH=backend \
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m py_compile \
  backend/tee_replicator/terminal_preservation.py \
  backend/admin/tee_terminal_preservation.py
```

Expected: clean output except intentional tracked changes before their commit.

- [ ] **Step 4: Run PROD inventory only**

After TEE schema is at the release head, run the CLI without `--apply` or
`--revert`. Capture only aggregate table counts, blocker classes, database
fingerprints, and the plan SHA-256. Confirm the report contains no user/item
identifiers, content, key material, or DSNs.

Expected: dry-run performs zero writes. If schema is not yet at the release
head, record that explicit precondition failure and do not bypass it.

- [ ] **Step 5: Request production write approval**

Present the exact dry-run count, plan digest, blockers, backup status, and revert
command. Do not run apply from this plan without a new explicit user confirmation.
