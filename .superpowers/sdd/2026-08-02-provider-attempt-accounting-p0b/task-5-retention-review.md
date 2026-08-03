# P0-B Task 5 retention independent review

Reviewed commit: `0b52a55586b3327ebd842e2512b0567687ce0587`

## Verdict

- **Spec: FAIL**
- **Quality: FAIL**
- **Ready: NO**

The implementation has good policy, locking, cascade, migration-recovery, and
operator-visibility foundations, but it is not safe to ship yet. Three
correctness paths can either retain expired personal rows indefinitely or make
deleted history look like complete zero. The maintenance row budget is also not
global despite its public parameter name.

## Critical findings

### C1. Rows that become old after `retained_from` is published are permanently skipped

Locations:

- `backend/model_api_runtime/v2/provider_attempt_rollup.py:921-935`
- `backend/model_api_runtime/v2/provider_attempt_rollup.py:962-997`
- `backend/model_api_runtime/v2/provider_attempt_rollup.py:1034-1064`
- `backend/alembic/versions/0077_llm_usage_attempt_rollups.py:201-238`

The same-cutoff fast path returns before inspecting rows whenever
`cutoff <= published`. On a later day, both parent deletion and the completion
proof add a lower bound at the already-published cutoff. That assumes rows can
never newly enter the expired interval after publication, but both supported
write paths violate the assumption:

1. a delayed/replayed canonical attempt can be inserted with `started_at`
   before `retained_from`; and
2. the migration explicitly supports a rare `v2_turn_metrics.created_at`
   correction, including a move from a retained day to a day before
   `retained_from`.

The trigger inserts the old dirty day, but maintenance deliberately fences old
dirty days out. The old attempt is therefore excluded from deletion and from
the `remaining` proof forever while the watermark keeps advancing. This breaks
the authoritative-day policy and user-data retention guarantee.

Minimal regression tests:

1. Publish `retained_from=P`, insert a runtime-recorder orphan with
   `started_at<P`, run retention first at `P` and then at `P+1`; assert the row
   is deleted before `P+1` is published.
2. Publish `retained_from=P`, seed a matched attempt whose turn starts at `P`,
   update the turn to `P-1`, then run retention at `P+1`; assert the attempt and
   correction cascade are deleted and the old dirty claim cannot be silently
   stranded.

The fix needs a bounded late-old lane (or equivalent indexed invariant) that
continues checking below the published boundary. The published lower bound may
optimize normal forward pruning, but cannot be the only eligibility/proof
range.

### C2. Multi-batch retention deletes rollups before publishing any partial boundary

Locations:

- `backend/model_api_runtime/v2/provider_attempt_rollup.py:967-1032`
- `backend/model_api_runtime/v2/provider_attempt_rollup.py:1066-1085`
- `backend/model_api_runtime/v2/jobs_store.py:6016-6053`

Every page commits deletion from attempts and independently deletes up to
`max_rows` dimensions and memberships. If more old attempts remain (or one is
locked), `retained_from` intentionally stays unchanged. Readers still classify
the affected historical days as completed rollup days because there is no
published pending-retention fence. They can therefore read already-deleted
rollup rows as zero while the known whole-turn denominator remains, with no
`retention_truncated` marker.

This is observable across ordinary multi-page retention, not only a failure
case: page one may delete derived data and commit while parent cleanup needs
page two. The existing locked-row test asserts late watermark publication but
does not query Admin/Runtime between pages.

Minimal regression test:

- Seed at least two expired attempts and a matching daily dimension/membership,
  run a page with `max_rows=1` (or lock one parent), then query the Usage report
  before the next page. Assert the report either falls back to surviving raw
  facts or exposes an explicit pending-retention partial reason; it must not
  render complete zero/coverage.

Safe designs include delaying derived deletion until all parents are gone, or
publishing a separate `retention_pending_from` before the first destructive
page and making every reader treat the affected range as raw/partial. Advancing
`retained_from` early would be incorrect because it claims completion.

### C3. Non-Shanghai Admin queries silently bypass retention coverage

Locations:

- `backend/admin/usage.py:21-32,59-65,107-140`
- `backend/model_api_runtime/v2/jobs_store.py:6056-6060`
- `backend/model_api_runtime/v2/jobs_store.py:6383-6400`

Admin accepts any valid IANA display timezone. The hybrid path only builds a
partition for `Asia/Shanghai`; other timezones fall back to raw. The raw
snapshot reads `retained_from`, but only applies it when
`usage_reporting.rollup_partition(query)` returns a partition, which it does
not outside Shanghai. Consequently, a UTC/custom-zone query crossing the
published cutoff scans only surviving ledger rows, reports no
`retained_from`/`retention_truncated`, and may calculate `0 / known denominator`
as real coverage.

Minimal regression test:

- Publish `retained_from=P`; issue equivalent custom ranges crossing `P` using
  `timezone="UTC"` and another valid non-Shanghai zone. Assert the same
  retained boundary, partial reason, preserved whole-turn denominator, and
  `logical_call_coverage is None` as the Shanghai query.

The retention boundary is defined in Shanghai, but coverage detection can be
timezone-independent by comparing the UTC query interval with Shanghai
midnight for `retained_from`; raw predicates must also exclude the pruned
interval explicitly.

## Important findings

### I1. `max_retention_rows` is a per-table limit, not a global maintenance budget

Locations:

- `backend/model_api_runtime/v2/provider_attempt_rollup.py:970-1027`
- `backend/model_api_runtime/v2/provider_attempt_rollup.py:1185-1190`

Parent deletion is globally capped at `N` across job-backed and orphan rows,
which is good. The same `N` is then independently applied to dimensions,
memberships, and dirty days. One tick can therefore mutate up to `4N` rows
(plus correction cascades), although the API/config name communicates one
bounded row budget. At the default this is up to 2,000 explicit rows rather
than 500, and at the accepted maximum up to 40,000 plus cascades.

Use one remaining budget across all retention targets, with reserved/fair
progress so a large parent backlog cannot permanently starve derived cleanup.
Correction cascades should be measured separately and covered in the load
proof because one parent can own multiple corrections.

### I2. The new orphan index adds write amplification to every canonical attempt; 3M proof is still pending

Locations:

- `backend/alembic/versions/0077_llm_usage_attempt_rollups.py:258-261`
- `tests/test_provider_attempt_rollup_reconciler.py:276-353`
- `tests/test_provider_attempt_rollup_migration.py:385-425`

The exact migration recovery/downgrade ownership checks are sound, and the
query shape is appropriately driven by `ix_v2_turn_metrics_created_at`,
`ix_llm_provider_attempts_runtime_job`, and the new orphan index. However, the
new `(started_at, attempt_id) INCLUDE (job_id)` partial index covers effectively
every canonical runtime-recorder row, so it adds a B-tree write on every start
and relevant update in addition to the existing attempt indexes. The committed
EXPLAIN fixture has only 3,001 rows and disables sequential scans; it is a plan
shape regression, not the requested 3M-scale cost/business-impact proof.

Do not remove the index without an alternative orphan lookup, but require the
formal 3M attempts EXPLAIN and recorder/provider load proof to include index
size/write overhead, normal planner behavior (without forcing
`enable_seqscan=off`), maintenance latency, pool occupancy, and provider-path
latency/results.

## Minor findings

### M1. Retention configuration silently caps longer policies at 36,500 days

`retention_days()` correctly defaults to 400, clamps shorter/malformed values,
and accepts tested longer values. The undocumented 100-year maximum means the
environment cannot increase retention without limit. This is unlikely to
matter operationally, but either document the safety cap or reject an
out-of-range value visibly instead of silently changing policy.

## Confirmed strengths

- Default-on maintenance with explicit false-like opt-out; no new service,
  database, queue, thread, or deployment unit.
- Shanghai half-open cutoff keeps the cutoff day and newer.
- Job-backed rows use `v2_turn_metrics.created_at`; only unmatched rows use
  `started_at`.
- Parent selection uses `FOR UPDATE ... SKIP LOCKED`; job/orphan parent deletes
  share one cap.
- Correction deletion correctly relies on the parent FK cascade; rate cards,
  turns, and users are not direct retention targets.
- Transaction errors, cancellation, CAS loss, and SQL timeout are caught and
  remain fail-open to the business path.
- Replay/bootstrap/dirty selection fences an already-published boundary.
- Shanghai Admin and Runtime payloads preserve the whole-turn denominator,
  set logical coverage to `None`, and expose an explicit partial reason when a
  query crosses a published boundary.
- The new concurrent index participates in the migration's exact-definition
  recovery, unrelated-owner preflight, and downgrade path.

## Verification evidence

- Implementation report: 331 focused PostgreSQL/Admin/Runtime tests passed;
  final Admin + Runtime rerun 188 passed; Ruff, `py_compile`, and diff check
  passed.
- Independent static review inspected the complete retention implementation,
  coverage paths, migration index recovery/downgrade, and focused tests.
- An independent local PostgreSQL rerun could not execute in this reviewer
  sandbox because TCP access to `127.0.0.1:55432` was denied with
  `Operation not permitted`; this is an environment limitation, not a test
  failure attributed to the commit.

## Ready verdict

**Not Ready.** Fix C1-C3 and I1, add the stated regression tests, then rerun the
focused PostgreSQL/Admin/Runtime suite and independent review. I2 must be
closed by the already-planned formal 3M + 3M and no-business-impact proof before
P0-B is considered complete.
