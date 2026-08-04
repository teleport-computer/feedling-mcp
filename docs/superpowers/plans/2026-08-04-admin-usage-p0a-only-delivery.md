# Admin Usage P0-A-only Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the Admin usage project with P0-A as the only shipped scope, verify its existing `test` delivery, publish the scope decision, and preserve P0-B as non-deployable experimental work.

**Architecture:** PR #146 already merged P0-A into `test` at merge commit `6d65bc7bf09cb8fe04e9e920304b6bcb22b199aa`. The remaining work is release verification and documentation: validate the merged P0-A implementation, prove the test environment serves the Usage and Runtime surfaces without business-path degradation, merge a docs-only scope record, and push P0-B only as an archived branch with no PR or deployment.

**Tech Stack:** Python 3, pytest, Ruff, PostgreSQL 16 test container, Git/GitHub CLI, existing Feedling test API and business RDS.

## Global Constraints

- P0-A is operational telemetry, not financial billing or provider-invoice truth.
- Do not merge or deploy `feat/provider-attempt-accounting`.
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
- Produces: fresh local test, static-analysis, migration, scope, and performance-evidence results suitable for the follow-up PR.

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

### Task 3: Publish the P0-A-only decision as a docs-only follow-up PR

**Files:**
- Create: `docs/superpowers/specs/2026-08-04-admin-usage-p0a-only-delivery-design.md`
- Create: `docs/superpowers/plans/2026-08-04-admin-usage-p0a-only-delivery.md`

**Interfaces:**
- Consumes: successful Task 1 verification and the already merged PR #146.
- Produces: one docs-only PR against `test` recording the final product boundary.

- [ ] **Step 1: Verify the follow-up diff contains only the two scope documents**

Run:

```bash
git fetch origin test
git diff --name-only origin/test...HEAD
git diff --check origin/test...HEAD
```

Expected: exactly the design and plan files listed above; no backend, migration, infrastructure, generated, or evidence files.

- [ ] **Step 2: Push the existing P0-A branch**

Run:

```bash
git push origin feat/admin-runtime-user-report
```

Expected: remote branch advances to the scope-document commits. This push does not deploy because it does not update `test`.

- [ ] **Step 3: Open the docs-only PR against `test`**

Run:

```bash
gh pr create --repo teleport-computer/feedling-mcp \
  --base test \
  --head feat/admin-runtime-user-report \
  --title 'docs(admin): scope usage delivery to P0-A' \
  --body 'Follow-up to #146. Records P0-A as the complete Admin Usage delivery: operational per-user token/model and delivery-reliability telemetry, not financial billing or provider-invoice truth. P0-B and P0-C are deferred; production is unchanged and no new infrastructure is introduced. Verification evidence is recorded in the committed delivery plan.'
```

Expected: the PR file list contains only the two scope documents and branch-flow CI accepts the `test` base.

- [ ] **Step 4: Wait for and inspect required checks**

Run:

```bash
TASK_FOLLOWUP_PR="$(gh pr view feat/admin-runtime-user-report \
  --repo teleport-computer/feedling-mcp --json number --jq .number)"
gh pr checks "$TASK_FOLLOWUP_PR" \
  --repo teleport-computer/feedling-mcp --watch
```

Expected: all required checks pass. A failure must be inspected with `gh run view`; do not merge around the check.

- [ ] **Step 5: Merge the docs-only PR only after green checks**

Run:

```bash
gh pr merge "$TASK_FOLLOWUP_PR" \
  --repo teleport-computer/feedling-mcp --merge --delete-branch=false
```

Expected: `test` gains documentation only; no production promotion is
triggered.

### Task 4: Preserve P0-B as an explicitly non-deliverable experiment

**Files:**
- Preserve branch: `feat/provider-attempt-accounting`
- Preserve committed report: `.superpowers/sdd/2026-08-02-provider-attempt-accounting-p0b/task-5-load-report.md`
- Do not commit: `docs/superpowers/evidence/2026-08-03-admin-usage-attempt-rollup-scale.json`
- Do not commit: `docs/superpowers/evidence/2026-08-03-provider-attempt-business-path.json`

**Interfaces:**
- Consumes: local P0-B head `dbfdeed3b4f19a774870fa0e5c1a7cb4f160d1eb` and the P0-A-only scope decision.
- Produces: a remote archival branch with no PR, merge, deployment, or implied readiness.

- [ ] **Step 1: Verify the P0-B worktree has no tracked modifications**

Run:

```bash
git status --short --branch
git diff --check
```

Expected: only the two explicitly listed untracked JSON artifacts remain.

- [ ] **Step 2: Push the P0-B branch for preservation**

Run:

```bash
git push -u origin feat/provider-attempt-accounting
```

Expected: remote branch points to `dbfdeed3`; no PR is created and no deployment workflow is triggered.

- [ ] **Step 3: Confirm no P0-B or P0-C delivery PR exists**

Run:

```bash
gh pr list --repo teleport-computer/feedling-mcp \
  --state open --head feat/provider-attempt-accounting
gh pr list --repo teleport-computer/feedling-mcp \
  --state open --head feat/resident-usage-rds-upload
```

Expected: both lists are empty.

- [ ] **Step 4: Report the final boundary**

Report:

- P0-A code is merged and verified on `test`;
- the docs-only scope PR is merged to `test`;
- P0-B exists only as a non-deployable remote archive;
- P0-C was not started;
- production was not modified;
- exact financial billing remains explicitly unsupported.

At this milestone, follow the Router preview gate before writing the durable team summary.
