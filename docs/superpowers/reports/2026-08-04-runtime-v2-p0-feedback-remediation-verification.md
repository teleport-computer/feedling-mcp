# Runtime V2 P0 feedback remediation verification

Date: 2026-08-04 (Asia/Shanghai)

Last updated: 2026-08-05 (Asia/Shanghai)

Branch: `fix/runtime-v2-feedback-p0-p1`

Original implementation base: `953c074d45309448360125753fb231006344eeee`

Verified candidate after merging current `origin/test`:
`a1576dd39e2eeb1d02422bc0d73b25517d3e162d`

Test deployment release:
`7de8abfe7ea8df76717f801e5693f0933801b15f`

## Scope

This report covers the approved P0 remediation only:

- durable, ordered, fingerprint-only state for wake-capable perception signals;
- one-generation fingerprint-secret rotation plus an explicit, non-waking
  dormant-row convergence mode after old workers drain;
- no baseline, duplicate, stale, equal-time conflict, or storage-error wake;
- no synthetic provider `user` turn for proactive work;
- no provider call for an ordinary heartbeat without genuine conversation history;
- scheduled/manual wake authorization without inventing a user request;
- Runtime V1-equivalent speak/silence semantics for heartbeat and screen watch;
- public architecture, perception, reliability, and changelog updates.

P1 response-start latency, profile-readiness gates, and new production metrics are
not implemented in this branch.

## Local evidence

All commands used the repository test interpreter and PostgreSQL at
`127.0.0.1:55432`. No prompt text, reminder text, location value, or stored
fingerprint is recorded here.

### Focused P0 suite

Command: the twelve test modules listed in Task 7.1 of the implementation plan.

Final post-review/post-merge result: `202 passed, 30 warnings in 7.29s`.

This includes durable decision concurrency, ingress, wake lanes, migrations,
TEE registry, account deletion, reset cleanup, mixed-secret workers, dormant
old-key convergence, authoritative empty-history gating, and anchor-metadata
privacy.

### Wake and provider compatibility

- Wake/context/reconciler cluster: `115 passed`.
- Context, prompt-cache/frontier, optional-anchor, and multimodal cluster:
  `154 passed`.
- Native Anthropic, Bedrock, Gemini, OpenAI, and provider transcript tests:
  `102 passed`.
- Legacy FakeStore perception plus Runtime V2 worker modules after fixture
  migration: `147 passed`.
- Final production dependency assembly, genuine-user cursor, and reconciler
  cluster before the final merge: `108 passed`.

### Public contract and documentation

- Public OpenAPI contract: `23 passed`.
- `docs-site` `npm run types:check`: passed.
- `docs-site` `npm run lint`: passed.
- `docs-site` `npm run build`: passed and generated 552 static pages. Two
  immediately preceding attempts failed only while fetching JetBrains Mono from
  Google Fonts; the successful retry compiled, type-checked, and generated all
  pages with exit code 0.

No public request or response schema changed, so the generated OpenAPI document
was not modified.

### Python lint

Pyflakes was run over every modified Python file. It reported three existing
warnings:

- `backend/db.py:2471`: f-string without placeholders;
- `backend/model_api_runtime/v2/serve_worker.py:1563`: imported
  `WakeControlDecisionV2` is unused;
- `tests/test_tee_table_registry.py:18`: unused `pytest` import.

Running pyflakes against those same files from base commit `953c074d` through
`git show ... | pyflakes /dev/stdin` reproduces all three warnings at the same
lines.
The branch introduces no new pyflakes finding.

### Full L1

Repository-standard command:

```text
pytest tests -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py
```

Final result on candidate `a1576dd3` after migrating affected legacy fixtures:

```text
4 failed, 8207 passed, 1 skipped, 9 xfailed, 36 warnings,
3 subtests passed in 387.74s
```

The four failures seen in that full run were present in files with no diff from
base commit `953c074d` and reproduced in an isolated focused run:

1. `test_asgi_genesis.py::test_plaintext_update_identity_without_identity_enqueues_202_parity`
   expects the same client job to receive 202 from both sequential adapters;
   the second call receives the existing 409 conflict.
2. `test_hosted_runtime_policy.py::test_main_compose_serve_worker_wires_memory_maintenance_producers`
   expected test Dream default `${FEEDLING_V2_DREAM_ENABLED:-0}` while the test
   compose intentionally pins `1`.
3. `test_v2_profile_lane.py::test_compose_memory_lane_defaults_match_environment_policy`
   had the same stale expectation.
4. `test_v2_status_poll.py::test_build_response_defaults_are_empty_and_backward_compatible`
   omits the already-returned additive `web_policy` key from its exact key set.

The first full run exposed five branch-related stale fixtures. After updating
them, all five pass and their two complete modules pass (`147 passed`). The two
Dream-policy expectations above were subsequently aligned with the intentional
test policy in commit `b58cfcfa`; both exact tests pass, and all four Runtime V2
rollout matrices passed in CI. The full L1 command was not rerun after that
final test-only correction, so this report does not claim a newer full-suite
total.

## Review evidence

Two read-only whole-diff review passes were completed. The first found four
implementation issues: `changed=false` location reports skipped the baseline,
summary/assistant-only history could authorize an automatic provider call,
fingerprint rotation lacked mixed-worker key identity, and anchor values entered
durable wake metadata. It also identified a dormant-row rotation lifecycle gap.
All were addressed with failing-first regression tests. The final review found
no remaining Critical or Important code issue; `git diff --check` is clean.

## Test-environment evidence

### Release and health

- GitHub Actions run `30924843846` completed successfully for release
  `7de8abfe7ea8df76717f801e5693f0933801b15f`; both `deploy CVM (test)` and
  `deploy runner CVM (test)` passed.
- `GET https://test-api.feedling.app/healthz` returned HTTP 200, reported that
  exact release, and showed healthy DB, pool, registry, and wake-bus checks.
- The main test CVM ran the ingress, backend, serve-worker, and both enclave
  containers from image tag `7de8abf`; the runner CVM ran the matching
  `feedling-agent-runner:7de8abf` image.
- `docker top` on the deployed backend showed one Gunicorn master and two live
  ASGI worker processes.
- The test RDS Alembic head is `0077_perception_signal_state_v2`.

### TEE schema remediation

The first post-deploy audit found the test TEE at `0009_provider_latency`, so
the release was not considered production-ready. TEE migration run
`30926604857` failed closed before DDL because the repository lacked
`TEST_TEE_PG_CA_PEM`; its upgrade and assertion steps were skipped.

The existing test TEE owner DSN and matching CA were recovered from their
documented local custody source without printing either value. Repository
secrets `TEST_TEE_MIGRATION_DSN` and `TEST_TEE_PG_CA_PEM` were populated, and
test-only migration run `30927354655` then completed successfully, including
the code-head assertion.

Independent app-role queries confirmed:

- TEE head `0011_perception_signal_state_v2`;
- `public.perception_signal_state_v2` exists;
- app-role `SELECT`, `INSERT`, `UPDATE`, `DELETE`, and `TRUNCATE` privileges are
  all granted.

No production schema was changed during this remediation. The documented
production owner DSN and matching CA were used to provision
`PROD_TEE_MIGRATION_DSN` and `PROD_TEE_PG_CA_PEM` without printing their values,
so both TEST and PROD migration channels now have their required secrets.
Production remains on RDS head `0073_merge_tail_anchor_deepseek` and TEE head
`0009_provider_latency`; the production TEE migration must run only after this
code reaches `main`, so the workflow checks out the matching production schema
head.

### Content-free cross-process decision probe

A temporary verification-only user was created in the deployed test RDS. Four
independent Python processes, using two opaque non-location values, observed the
same signal in sequence. Results were:

```text
baseline_created False
unchanged False
changed True
unchanged False
```

The durable row count was one and both stored digest fields were 64 characters;
no raw value was queried or recorded. The temporary user and its cascading
signal state were deleted immediately after the probe.

The automatic empty-history provider-call boundary was not exercised against a
real external provider in the shared test environment. Its zero-call guarantee
is covered by the focused local suite and the green Runtime V2 rollout matrices,
avoiding an unnecessary provider request during deployment verification.

## Rollback

The implementation is additive at the schema boundary. Runtime rollback uses
the existing per-user Runtime V1 fence; the new state table can remain in place
while V1 owns execution. A code rollback must not drop the table during the
incident because its durable baselines prevent a later false first-transition
wake when Runtime V2 is re-enabled.
