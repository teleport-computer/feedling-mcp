# Admin Usage P0-A-only Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the Admin usage project with P0-A as the only shipped scope, verify its existing `test` delivery, publish the scope decision, and preserve P0-B as non-deployable archival/review work.

**Architecture:** [PR #146](https://github.com/teleport-computer/feedling-mcp/pull/146) already merged P0-A into `test` at merge commit `6d65bc7bf09cb8fe04e9e920304b6bcb22b199aa` and remains the only product-code delivery. The remaining work is release verification and documentation: validate the merged P0-A implementation, prove the test environment serves the Usage and Runtime surfaces without business-path degradation, record the completed direct two-document update, and preserve P0-B only through an explicitly non-deliverable Draft archive/review PR.

**Tech Stack:** Python 3, pytest, Ruff, PostgreSQL 16 test container, Git/GitHub CLI, existing Feedling test API and business RDS.

## Execution status (2026-08-04)

[PR #146](https://github.com/teleport-computer/feedling-mcp/pull/146) merged the
P0-A product code into `test` and remains the only product-code delivery.
Docs-only [PR #154](https://github.com/teleport-computer/feedling-mcp/pull/154)
was closed unmerged after unrelated current-`test` Dream policy checks failed.
The maintainer-authorized direct two-document update landed on `test` through
the normal fast-forward sequence culminating in the status correction at
[`953c074d45309448360125753fb231006344eeee`](https://github.com/teleport-computer/feedling-mcp/commit/953c074d45309448360125753fb231006344eeee).
The former follow-up-PR workflow in Task 3 is historical and superseded: it
**MUST NOT** be executed or used to create another PR.

User-authorized [Draft PR #155](https://github.com/teleport-computer/feedling-mcp/pull/155)
is OPEN against `test` from `feat/provider-attempt-accounting` at
`dbfdeed3b4f19a774870fa0e5c1a7cb4f160d1eb`. It exists only for archival and
review; GitHub reports `DIRTY` / `CONFLICTING`, and it is **NOT READY FOR MERGE
OR DEPLOYMENT**. P0-B failed its strict 3-million-turn plus 3-million-attempt
performance gate and requires redesign, rebase, and renewed validation. The
Draft authorizes no `test` or production deployment. Only P0-A is shipped on
`test`; P0-B, P0-C, and billing-grade accounting remain unshipped, production
is untouched, and no new infrastructure or product store was introduced.

## Global Constraints

- P0-A is operational telemetry, not financial billing or provider-invoice truth.
- Do not merge or deploy `feat/provider-attempt-accounting` or its archival
  [Draft PR #155](https://github.com/teleport-computer/feedling-mcp/pull/155).
- Do not start P0-C resident usage upload work.
- Do not deploy or modify production in this plan.
- Use only the existing business RDS and existing deployment units; add no SQLite, local PostgreSQL product dependency, Redis, Kafka, RDS, service, container, or CVM.
- Test-environment checks are read-only except for the repository's normal throwaway local pytest databases.
- Never print admin tokens, database credentials, or complete `.env` values.
- A report or rollup failure must not alter provider calls, replies, retries, heartbeat, or job handling.

---

### Task 1: Re-verify the final P0-A implementation

**Files:**
- Verify: `backend/admin/admin_core.py`
- Verify: `backend/admin/data_track.py`
- Verify: `backend/admin/usage.py`
- Verify: `backend/alembic/versions/0074_runtime_user_delivery_indexes.py`
- Verify: `backend/alembic/versions/0075_v2_usage_rollup.py`
- Verify: `backend/asgi_app.py`
- Verify: `backend/db.py`
- Verify: `backend/model_api_runtime/v2/jobs_store.py`
- Verify: `backend/model_api_runtime/v2/serve_worker.py`
- Verify: `backend/model_api_runtime/v2/usage_reporting.py`
- Verify: `backend/model_api_runtime/v2/usage_rollup.py`
- Verify: `backend/tee_shadow/table_registry.py`
- Test: `tests/test_account_reset_purges_all_tables.py`
- Test: `tests/test_admin_usage.py`
- Test: `tests/test_data_track_runtime_view.py`
- Test: `tests/test_v2_dependency_direction.py`
- Test: `tests/test_v2_jobs_migration.py`
- Test: `tests/test_v2_runtime_health.py`
- Test: `tests/test_v2_usage_rollup.py`

**Interfaces:**
- Consumes: P0-A head `8a42a6d23229be41a36beb1e1a5b2e4b5782b226` and checked-in scale evidence under `docs/superpowers/evidence/2026-08-02-admin-usage-scale.*`.
- Produces: fresh local test, static-analysis, migration, scope, and performance-evidence results suitable for the closeout record.

- [ ] **Step 1: Confirm branch and merge ancestry**

Run:

```bash
git fetch origin test feat/admin-runtime-user-report
git merge-base --is-ancestor 8a42a6d23229be41a36beb1e1a5b2e4b5782b226 origin/test
git status --short --branch
```

Expected: the ancestry command exits `0`; the P0-A worktree contains only the committed scope documents planned here.

- [ ] **Step 2: Run the complete P0-A focused suite against real local PostgreSQL**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_account_reset_purges_all_tables.py \
  tests/test_admin_usage.py \
  tests/test_data_track.py \
  tests/test_data_track_runtime_view.py \
  tests/test_v2_dependency_direction.py \
  tests/test_v2_jobs_migration.py \
  tests/test_v2_runtime_health.py \
  tests/test_v2_usage_rollup.py -q
```

Expected: zero failures and no DB-backed collection skip.

- [ ] **Step 3: Run the repository non-API suite and compare only genuine regressions**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```

Expected: no P0-A-caused failures. Any failure must be compared with the current `origin/test` baseline and reported rather than hidden.

- [ ] **Step 4: Run changed-file static checks**

Run:

```bash
uv run ruff check \
  backend/admin/admin_core.py \
  backend/admin/data_track.py \
  backend/admin/usage.py \
  backend/alembic/versions/0074_runtime_user_delivery_indexes.py \
  backend/alembic/versions/0075_v2_usage_rollup.py \
  backend/asgi_app.py \
  backend/db.py \
  backend/model_api_runtime/v2/jobs_store.py \
  backend/model_api_runtime/v2/serve_worker.py \
  backend/model_api_runtime/v2/usage_reporting.py \
  backend/model_api_runtime/v2/usage_rollup.py \
  backend/tee_shadow/table_registry.py \
  scripts/perf/admin_usage_scale.py \
  tests/test_account_reset_purges_all_tables.py \
  tests/test_admin_usage.py \
  tests/test_data_track_runtime_view.py \
  tests/test_v2_dependency_direction.py \
  tests/test_v2_jobs_migration.py \
  tests/test_v2_runtime_health.py \
  tests/test_v2_usage_rollup.py
git diff --check origin/test...HEAD
```

Expected: Ruff and diff checks exit `0`.

- [ ] **Step 5: Re-read, do not rerun, the accepted formal scale artifact**

Run:

```bash
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m json.tool \
  docs/superpowers/evidence/2026-08-02-admin-usage-scale.json >/dev/null
rg -n "p50|p95|3000|passed|unfiltered|filtered" \
  docs/superpowers/evidence/2026-08-02-admin-usage-scale.md
```

Expected: the committed artifact parses, both rolling-90-day cohorts remain strictly below the 3000 ms p95 gate, and the markdown evidence names the production implementation. Do not launch another 3-million-row run for this scope-only closeout.

### Task 2: Verify the already-merged test delivery without changing business state

**Files:**
- Read: `docs/testing/TESTING.md`
- Read: `backend/admin/routes_asgi.py`
- Read: `deploy/docker-compose.phala.test.yaml`

**Interfaces:**
- Consumes: merged P0-A commit ancestry, `https://test-api.feedling.app`, the existing test admin token, and test RDS configuration.
- Produces: read-only HTTP, schema, page-content, query-latency, and rollup-freshness evidence.

- [ ] **Step 1: Verify the public test API is healthy**

Run:

```bash
curl -fsS -o /dev/null -w '%{http_code}\n' https://test-api.feedling.app/healthz
```

Expected: HTTP `200`.

- [ ] **Step 2: Verify the test RDS migration and rollup objects read-only**

Run without printing the connection string:

```bash
set -a
. /Users/zhengzhihao/Projects/teleport/feedling-mcp/.env
set +a
psql "$TEST_DATABASE_URL" -X -v ON_ERROR_STOP=1 -c \
  "BEGIN READ ONLY;
   SELECT version_num FROM alembic_version;
   SELECT to_regclass('public.v2_usage_daily_users');
   SELECT to_regclass('public.v2_usage_daily_dimensions');
   SELECT to_regclass('public.v2_usage_rollup_watermarks');
   COMMIT;"
```

Expected: migration revision is at or beyond `0075_v2_usage_rollup`; all three
relations resolve. Do not execute DDL or DML.

- [ ] **Step 3: Fetch both Admin surfaces using the existing token without displaying it**

Run:

```bash
umask 077
IFS= read -r TASK_ADMIN_TOKEN \
  < /Users/zhengzhihao/.feedling/data-track-admin-token
curl -fsS -H "Authorization: Bearer $TASK_ADMIN_TOKEN" \
  -o /private/tmp/p0a-test-usage.html \
  'https://test-api.feedling.app/admin/data-track?view=usage'
curl -fsS -H "Authorization: Bearer $TASK_ADMIN_TOKEN" \
  -o /private/tmp/p0a-test-runtime.html \
  'https://test-api.feedling.app/admin/data-track?view=runtime'
```

Save bodies only under `/private/tmp`, with restrictive permissions. Expected:

- both responses return HTTP `200`;
- Usage contains the overview, daily trend, per-user table, provider/model table, filters, and coverage/unknown copy;
- Runtime contains the per-user delivery-reliability section;
- neither page contains an exception traceback or generic `500` page.

- [ ] **Step 4: Record bounded response latency and graceful degradation**

Run five sequential read-only requests to each page:

```bash
for TASK_VIEW in usage runtime; do
  for TASK_SAMPLE in 1 2 3 4 5; do
    curl -fsS -o /dev/null \
      -H "Authorization: Bearer $TASK_ADMIN_TOKEN" \
      -w "$TASK_VIEW $TASK_SAMPLE %{http_code} %{time_total}\n" \
      "https://test-api.feedling.app/admin/data-track?view=$TASK_VIEW"
  done
done
```

Expected: every status is `200`; no request exceeds the report's configured
timeout envelope. The saved HTML from Step 3 must use explicit unavailable
copy for degraded sections rather than a backend-wide failure.

- [ ] **Step 5: Check current test deployment provenance**

Run:

```bash
phala ps feedling-io-test
phala ssh feedling-io-test -- docker inspect feedling-test-backend-1 \
  --format '{{.Config.Image}}'
```

Resolve the running image tag and run:

```bash
TASK_DEPLOYED_IMAGE="$(phala ssh feedling-io-test -- \
  docker inspect feedling-test-backend-1 --format '{{.Config.Image}}')"
TASK_DEPLOYED_SHA7="${TASK_DEPLOYED_IMAGE##*:}"
test "${#TASK_DEPLOYED_SHA7}" -eq 7
TASK_DEPLOYED_FULL_SHA="$(git rev-parse "$TASK_DEPLOYED_SHA7")"
git merge-base --is-ancestor \
  6d65bc7bf09cb8fe04e9e920304b6bcb22b199aa \
  "$TASK_DEPLOYED_FULL_SHA"
```

Expected: the ancestry check exits `0`. If the container name differs, use the
backend name returned by `phala ps`; if the image is not commit-tagged or the
ancestry check fails, stop and request authorization for a test redeploy. Do
not infer deployment from GitHub branch state alone.

### Task 3 (historical — superseded; MUST NOT execute): Former docs-only follow-up PR

**Superseded status:** The maintainer-authorized direct two-document update
landed on `test` through the normal fast-forward sequence culminating in the
status correction at
[`953c074d45309448360125753fb231006344eeee`](https://github.com/teleport-computer/feedling-mcp/commit/953c074d45309448360125753fb231006344eeee).
Docs-only [PR #154](https://github.com/teleport-computer/feedling-mcp/pull/154)
is closed unmerged; the documents did not land through a PR merge. The commands
and expectations below are kept only as historical context; **MUST NOT execute
them, reopen #154, or create another docs-only PR**.

**Files:**
- Create: `docs/superpowers/specs/2026-08-04-admin-usage-p0a-only-delivery-design.md`
- Create: `docs/superpowers/plans/2026-08-04-admin-usage-p0a-only-delivery.md`

**Interfaces:**
- Historical input: successful Task 1 verification and already merged
  [PR #146](https://github.com/teleport-computer/feedling-mcp/pull/146).
- Actual outcome: the two documents landed directly through the authorized
  fast-forward sequence; PR #154 closed unmerged and no product code changed.

- [x] **Archived Step 1 (superseded — MUST NOT execute): Verify the former follow-up diff contains only the two scope documents**

Run:

```bash
git fetch origin test
git diff --name-only origin/test...HEAD
git diff --check origin/test...HEAD
```

Expected: exactly the design and plan files listed above; no backend, migration, infrastructure, generated, or evidence files.

- [x] **Archived Step 2 (superseded — MUST NOT execute): Push the former P0-A branch**

Run:

```bash
git push origin feat/admin-runtime-user-report
```

Expected: remote branch advances to the scope-document commits. This push does not deploy because it does not update `test`.

- [x] **Archived Step 3 (superseded — MUST NOT execute): Open the former docs-only PR against `test`**

Run:

```bash
gh pr create --repo teleport-computer/feedling-mcp \
  --base test \
  --head feat/admin-runtime-user-report \
  --title 'docs(admin): scope usage delivery to P0-A' \
  --body 'Follow-up to #146. Records P0-A as the complete Admin Usage delivery: operational per-user token/model and delivery-reliability telemetry, not financial billing or provider-invoice truth. P0-B and P0-C are deferred; production is unchanged and no new infrastructure is introduced. Verification evidence is recorded in the committed delivery plan.'
```

Expected: the PR file list contains only the two scope documents and branch-flow CI accepts the `test` base.

- [x] **Archived Step 4 (superseded — MUST NOT execute): Wait for and inspect the former PR checks**

Run:

```bash
TASK_FOLLOWUP_PR="$(gh pr view feat/admin-runtime-user-report \
  --repo teleport-computer/feedling-mcp --json number --jq .number)"
gh pr checks "$TASK_FOLLOWUP_PR" \
  --repo teleport-computer/feedling-mcp --watch
```

Expected: all required checks pass. A failure must be inspected with `gh run view`; do not merge around the check.

- [x] **Archived Step 5 (never completed — MUST NOT execute): Former docs-only PR merge**

Run:

```bash
gh pr merge "$TASK_FOLLOWUP_PR" \
  --repo teleport-computer/feedling-mcp --merge --delete-branch=false
```

Historical expectation only: `test` would gain documentation only, with no
production promotion. This merge did not occur: PR #154 was closed unmerged,
and the authorized direct fast-forward sequence delivered the two documents.

### Task 4 (completed archival record): Preserve P0-B without delivery authorization

**Completed status:** The user authorized OPEN
[Draft PR #155](https://github.com/teleport-computer/feedling-mcp/pull/155)
solely to archive and review the deferred P0-B experiment. It targets `test`
from `feat/provider-attempt-accounting` at
`dbfdeed3b4f19a774870fa0e5c1a7cb4f160d1eb`. GitHub reports the Draft as
`DIRTY` / `CONFLICTING`; it is **NOT READY FOR MERGE OR DEPLOYMENT** and
**MUST NOT** be merged or deployed. It authorizes no `test` or production
deployment.

**Files:**
- Preserve branch: `feat/provider-attempt-accounting`
- Preserve committed report: `.superpowers/sdd/2026-08-02-provider-attempt-accounting-p0b/task-5-load-report.md`
- Do not commit: `docs/superpowers/evidence/2026-08-03-admin-usage-attempt-rollup-scale.json`
- Do not commit: `docs/superpowers/evidence/2026-08-03-provider-attempt-business-path.json`

**Interfaces:**
- Consumes: local P0-B head `dbfdeed3b4f19a774870fa0e5c1a7cb4f160d1eb` and the P0-A-only scope decision.
- Produces: a preserved remote branch and an OPEN Draft archival/review PR,
  with no merge, deployment, or implied readiness.

- [x] **Step 1: Record the preserved branch state**

At archival time, the branch head was
`dbfdeed3b4f19a774870fa0e5c1a7cb4f160d1eb`, and the two raw evidence JSON
files listed above remained intentionally uncommitted. The committed report and
design history remain available on the branch.

- [x] **Step 2: Record the user-authorized Draft archive**

Draft PR #155 is OPEN against `test` from
`feat/provider-attempt-accounting` at the preserved head. Its only purpose is
archival/review; opening it did not authorize or trigger a test or production
deployment.

- [x] **Step 3: Record the failed readiness gate and current conflict state**

The strict P0-B 3-million-turn plus 3-million-attempt performance gate failed.
The final narrow-candidate warm runs timed out, recorded full plans remained at
approximately 3.704 seconds and 4.342 seconds because of structural
spill/materialization, and the formal-attempt subsection against the production
shape also timed out. GitHub currently reports the Draft as `DIRTY` and
`CONFLICTING`. Redesign, rebase, and renewed validation are required before any
future delivery decision; the current Draft is **NOT READY FOR MERGE OR
DEPLOYMENT**.

- [x] **Step 4: Record the final delivery boundary**

Final state:

- [PR #146](https://github.com/teleport-computer/feedling-mcp/pull/146)
  remains the only product-code delivery, and only P0-A is shipped on `test`;
- PR #154 is closed unmerged; the two closeout documents landed through the
  authorized normal fast-forward sequence;
- Draft PR #155 preserves P0-B for archival/review only and **MUST NOT** be
  merged or deployed;
- P0-B, P0-C, and billing-grade accounting remain unshipped;
- production remains untouched; and
- no new infrastructure or product telemetry store was introduced.

At this milestone, follow the Router preview gate before writing the durable team summary.
