# Test to Pre Memory and Genesis Sync Design

## Objective

Promote the complete backend state at `origin/test@8034690eef39104511c31870a33b18eb338d6074` onto `origin/pre@3ebc700ff006bca7d54d141028c4f85596ef72fc` without weakening PRE's per-user plaintext/encrypted content routing, TEE-primary startup contract, or dual enclave-entry deployment topology.

The release must update both the PRE main CVM and PRE runner CVM. The resulting commit must remain a descendant of both frozen inputs.

## Scope

The sync includes the complete TEST history, including:

- Memory Garden extraction and timestamp normalization;
- Runtime V2 prompt, extraction, tool-loop, profile, wake, and observability changes;
- Genesis identity/profile dual publication and field-lock behavior;
- MEMORY/STYLE profile naming and compatibility behavior;
- auditable Runtime V2 wake outcomes;
- associated public documentation, OpenAPI artifacts, CI coverage guards, tools, and tests.

PRE-only behavior remains in scope where required to preserve the deployed contract:

- per-user plaintext or encrypted content shapes;
- strict plaintext/enclave routing boundaries;
- PRE TEE-primary migration and startup guards;
- PRE API and enclave custom domains plus the direct-TLS enclave entry;
- PRE main-CVM and runner-CVM deployment configuration.

No unrelated refactor, production deployment, or iOS change is included.

## Integration Strategy

Perform a regular merge of the frozen TEST input into a branch rooted at the frozen PRE input. Resolve conflicts semantically rather than selecting one side wholesale.

The known content conflicts and required outcomes are:

1. `backend/capabilities/identity.py`
   - retain PRE's `core_envelope` dependency for content-shape-aware reads;
   - retain TEST's `card_policy` dependency and identity validation behavior.
2. `backend/memory/memory_core.py`
   - retain PRE's central uploaded-envelope validation and plaintext/encrypted shape boundary;
   - normalize and validate `occurred_at` with the TEST Memory Garden timestamp helper.
3. `backend/model_api_runtime/v2/jobs_store.py`
   - retain PRE's encrypted/plaintext capture validation;
   - normalize `occurred_at` and persist only the bounded action shape;
   - retain TEST's auditable wake-result behavior.
4. `backend/model_api_runtime/v2/profile_store.py`
   - adopt MEMORY/STYLE as the canonical profile names;
   - preserve the legacy USER read fallback;
   - accept exactly one valid content shape per field: encrypted envelope or plaintext body;
   - preserve untouched profile sides byte-for-byte during partial Genesis publication;
   - retain durable retry metadata.
5. `backend/model_api_runtime/v2/serve_worker.py`
   - adopt TEST's substantive identity-card-first prompt behavior and Genesis persona fallback;
   - read identity through the capability seam so PRE plaintext and encrypted rows both remain supported;
   - retain TEST's trusted-prefix ordering and working-memory behavior.
6. `docs-site/content/docs/architecture.mdx`
   - retain both the PRE managed PostgreSQL/TEE promotion disclosure and the TEST Genesis/profile publication contract.
7. `tests/test_memory_migration.py`
   - use the Memory Garden prompt module while retaining PRE action-path coverage.
8. `tests/test_v2_profile_storage.py`
   - retain plaintext/mixed-shape tests and add TEST's untouched-side, MEMORY/STYLE, and retry tests.
9. `tests/test_v2_jobs_migration.py`
   - retain TEST's derived-head installation checks during the merge;
   - advance PRE's exact release-head assertions to the converged RDS revision in the migration task.

Any additional conflict found by the real merge must be stopped and assessed against these same rules before resolution.

## Database Convergence

TEST adds RDS revision `0089_v2_wake_outcomes`, whose parent is `0088_agent_jobs_available_at`. PRE already has head `0089_merge_pre_test_agent_jobs`, so the combined RDS graph would have two heads.

Add an RDS merge revision named `0090_merge_wake_outcomes` with both `0089_merge_pre_test_agent_jobs` and `0089_v2_wake_outcomes` as parents. It performs no schema operation; the TEST branch migration owns the two new `agent_jobs` columns. The revision identifier remains within Alembic's default 32-character version column.

PRE can run with `FEEDLING_DATABASE_SCHEMA=tee`, so add TEE revision `0022_v2_wake_outcomes` after `0021_agent_jobs_available_at`. It must:

- add `wake_result TEXT` and `wake_result_reason TEXT` to `agent_jobs` using idempotent SQL equivalent to the RDS revision;
- update the frozen PRE migration marker to `0022_v2_wake_outcomes`;
- preserve the TEE chain's fail-safe no-downgrade contract; rollback requires the documented backup/restore path.

Update migration convergence and PRE preflight tests to require exactly one RDS head and one TEE head at these new revisions. RDS and TEE column semantics must be tested for parity.

## Safety Boundaries

- Plaintext content must never be sent to enclave decrypt endpoints merely because TEST code assumed encrypted-only storage.
- Encrypted rows must continue using the existing enclave/decrypt path.
- Unknown users and unset encryption preferences remain fail-safe encrypted.
- Profile validation must reject mixed ciphertext/plaintext fields and torn MEMORY/STYLE pairs.
- Genesis partial publication must not decrypt, reseal, or rewrite the untouched profile side.
- PRE startup must fail closed when the live TEE schema or frozen marker is not at `0022_v2_wake_outcomes`.
- The custom enclave domain remains `attested_ingress`; the Phala app-id entry remains `direct_tls` for certificate pinning.
- No PRE deploy starts until local verification and GitHub image publication succeed.

## Verification

Verification is performed on the exact merged commit:

1. Assert both frozen inputs are ancestors and the worktree has no unresolved conflicts.
2. Run migration graph, upgrade/downgrade, convergence, and PRE startup-preflight tests.
3. Run focused suites for the nine conflict areas, Runtime V2 wake outcomes, Genesis dual publication, Memory Garden, identity, and plaintext/encrypted routing.
4. Run the repository's full PostgreSQL-backed pytest suite; no database-backed module may be silently skipped because PostgreSQL is unavailable.
5. Run OpenAPI contract tests.
6. Regenerate the public OpenAPI artifact and confirm the generated diff is intentional.
7. Run documentation type checking, lint, and production build.
8. Request an independent code review and resolve all Critical or Important findings.

The clean PRE baseline for comparison is `9780 passed, 3 skipped, 9 xfailed, 49 warnings, 3 subtests` on `pre@3ebc700f`.

## Deployment and Live Acceptance

Push the verified merge commit to remote `pre` without force-pushing. GitHub Actions must publish both pinned images and complete CI. Run the PRE TEE migration before allowing the deployment preflight to pass, then deploy both CVMs.

Acceptance requires:

- image publication, CI, and TEE migration workflows complete successfully for the exact release SHA;
- `https://pre-api.feedling.app/healthz` returns HTTP 200 and the exact release SHA;
- `https://pre-enclave.feedling.app/healthz` returns HTTP 200, the exact release SHA, `tls_enabled=false`, and `transport_mode=attested_ingress`;
- the Phala direct enclave health endpoint returns HTTP 200, the exact release SHA, `tls_enabled=true`, and `transport_mode=direct_tls`;
- backend, enclave, enclave-domain, serve-worker, and agent-runner containers use the pinned release images, are running, and have zero deployment-time restarts;
- remote `pre` equals the verified release commit.

The isolated worktree is preserved after deployment for follow-up investigation.
