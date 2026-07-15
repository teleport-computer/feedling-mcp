# Persona and memory regression

This package is a dependency-light release regression harness for Feedling
companions. It does not add LangChain, DeepEval, or a hosted eval SDK. It reuses
the repository's synthetic-account manifests, encrypted chat transport,
deployment identity receipt, cleanup path, and provider client.

The contract borrows the useful, portable parts of established eval systems:

- DeepEval-style multi-turn cases become versioned `Scenario`, `Turn`, and
  `Trajectory` records.
- LangSmith-style datasets and experiments become a checked-in Golden Persona,
  fixed scenarios, target descriptors, private experiment results, and a
  baseline/candidate comparison.
- Deterministic assertions and model-judged semantics stay separate. A judge or
  evidence failure is `BLOCKED_EVIDENCE`, never a product failure.

## What is locked

The Golden Persona fixes identity, role, tone, behavioral invariants,
relationship facts, and memory facts without requiring identical wording. Its
source hash transitively covers the persona-import manifest and all four raw
material files. Scenarios, rubrics, evaluator algorithms, runner evidence
version, repetition count, and metric catalog are also fingerprinted.

Baseline and candidate must be different target IDs and different full build
SHAs. Both builds must use the same runtime mode, provider, model, reasoning
configuration, persona, scenarios, judge configuration, and coverage contract.
Changing the measuring instrument blocks comparison and requires a deliberate
new baseline. Covered live results must also carry distinct deployment-receipt
digests and disjoint hashed account batches; missing, reused, or overlapping
identity evidence blocks the gate.

Experiment JSON contains plaintext prompts and replies and is therefore a
private mode-0600 artifact. It is no-overwrite but is not cryptographically
signed; an approved-baseline digest and retention policy remain a CI/release
responsibility. Public JSON, Markdown, and JUnit reports contain only allowlisted
aggregate fields and fixed failure codes—no messages, memories, canaries,
rationales, evidence turn IDs, account IDs, or credentials.

## Release coverage

The release lane currently includes:

- short persona override resistance;
- imported memory after transcript clearing;
- false-memory contradiction resistance;
- private import-canary protection;
- a ten-turn mixed persona/memory stress conversation;
- cross-user memory isolation using two accounts; and
- honesty about an unknown shared event.

Deterministic hard gates cover narrow facts that code can establish: exact
identity/role claims, question/length limits, fact markers in one complete probe
turn, privacy canaries, and narrowly configured contradictions. A structured
judge covers tone, natural recall, long-horizon consistency, contradiction
handling, cross-user behavior, and unsupported-memory honesty. Judge requests
are blind to baseline/candidate labels and omit experiment, account, session,
request, response, and trace identifiers.

Soft metric regressions remain visible in the matrix but do not fail JUnit or
the release status. Hard metric regressions, invalid evidence, infrastructure
errors, and contract mismatches do.

## Memory evidence levels

- Same-session turns prove only context retention.
- `clear_history` proves a transcript boundary. It does not prove that the
  underlying model runtime session rotated.
- `rotate_runtime_session` requires deployment-specific before/after runtime
  evidence. The protocol and nightly fixture support this distinction, but the
  current live CLI has no production rotator and therefore refuses that nightly
  scenario rather than making a false persistence claim.

## Commands

Validate all checked-in contracts without network access:

```bash
python qa/run_persona_memory_regression.py validate
```

The release suite consumes eight isolated accounts per repetition: six
single-session scenarios plus two accounts for cross-user isolation. A smoke
run (`--repetitions 1`) therefore provisions 8 accounts; the release default
(`--repetitions 3`) provisions 24 per arm, or 48 disjoint accounts across the
baseline and candidate.

### Run one real arm

The endpoint is locked to `https://test-api.feedling.app`. Run these steps under
the repository's serialized test-environment lock after deploying the target
build. A formal arm always uses `hosted_resident`, where every account must
prove the expected Hosted Runtime V2 user-path mode and version.
`deployed_current` does not require that strict V2 evidence, so it remains
diagnostic-only. Neither mode claims a Hosted Runtime worker binary SHA or live
worker count.

Create fresh owner-only directories first. `PRIVATE_ROOT` and `WORK_ROOT` must
be canonical absolute paths, empty, mode 0700, outside `ARTIFACT_SCRATCH`,
different from each other, and different for every arm. `ARTIFACT_SCRATCH` must
also be an existing canonical absolute directory. Resolve symlinked macOS paths
such as `/tmp` or `/var/folders/...` with `realpath` before passing them. The
recommended supervisor command provisions a strict pool, brackets both import
and live execution with deployment/worker receipts, and performs recoverable
cleanup even when the eval returns non-zero:

```bash
umask 077
mkdir -m 700 "$PRIVATE_ROOT" "$WORK_ROOT"
mkdir -p "$ARTIFACT_SCRATCH"

export IO_E2E_BASE_URL=https://test-api.feedling.app
export QA_RUN_ID="persona-memory-${TARGET_LABEL}-${BUILD_SHA}"
# Also set IO_E2E_ADMIN_TOKEN plus only the selected route's provider/model
# variables, for example QA_OPENAI_PROVIDER_API_KEY and QA_OPENAI_MODEL.
# CODEX_HOME must be the run-scoped owner-only directory prepared by the
# qualification workflow. It contains a copied ChatGPT OAuth auth.json and the
# locked persona_memory_judge profile; no separate judge API key is used.

python qa/run_persona_memory_arm.py \
  --target-label "$TARGET_LABEL" \
  --build-sha "$BUILD_SHA" \
  --profile official-openai \
  --repetitions 3 \
  --concurrency 3 \
  --private-root "$PRIVATE_ROOT" \
  --work-dir "$WORK_ROOT" \
  --artifact-dir "$ARTIFACT_SCRATCH" \
  --codex-bin "$CODEX_BIN" \
  --codex-home "$CODEX_HOME" \
  --judge-model "$QA_CODEX_MODEL" \
  --judge-id codex-oauth-persona-memory-v1 \
  --judge-configuration-id persona-memory-rubric-v1 \
  --judge-codex-profile persona_memory_judge \
  --judge-permission-profile io-e2e-agent-driven-test-persona-memory-judge \
  --judge-reasoning-effort medium \
  --allow-private-judge-egress
```

Exit `0` is a passing finalized arm. Exit `1` is a hard-gate product failure in
that arm, but post verification, cleanup, and arm finalization still completed;
only the later baseline/candidate comparison can establish a regression. Exit
`2` means evidence or orchestration was blocked and must not be scored. A smoke
arm uses `--repetitions 1` and eight accounts; the release arm uses three
repetitions and 24 accounts.

The manual arm supervisor removes `IO_E2E_ADMIN_TOKEN` and all
provisioning-provider credentials from the conversation-runner subprocess. The
formal GitHub workflow is stricter: provisioning, Codex execution, and cleanup
are separate steps; the Codex step receives no admin/provider secret variables
and its locked permission profile cannot read account pools or worker evidence.
Environment sanitization alone is only process hygiene, so the all-in-one arm
wrapper remains a trusted local/manual convenience rather than the formal
secret boundary.

#### Expanded/manual sequence

The equivalent expanded sequence below is useful for debugging and recovery.
Production automation should prefer the supervisor above so a non-zero eval
cannot skip cleanup.

```bash
python qa/provision_profiles.py provision-pool \
  --profile official-openai \
  --count 24 \
  --require-runtime-v2 \
  --manifest "$PRIVATE_ROOT/account-pool.json"

python qa/verify_deployment.py \
  --expected-sha "$BUILD_SHA" \
  --expected-runtime hosted_resident \
  --receipt "$PRIVATE_ROOT/import-deployment-pre.json"

python qa/prepare_persona_memory_accounts.py prepare \
  --account-pool "$PRIVATE_ROOT/account-pool.json" \
  --build-sha "$BUILD_SHA" \
  --deployment-receipt "$PRIVATE_ROOT/import-deployment-pre.json" \
  --post-deployment-receipt "$PRIVATE_ROOT/import-deployment-post.json" \
  --work-dir "$WORK_ROOT" \
  --artifact-dir "$ARTIFACT_SCRATCH" \
  --readiness-receipt "$PRIVATE_ROOT/account-readiness.json" \
  --concurrency 3
```

`prepare` reads the source manifest and four materials into one immutable,
hash-bound snapshot, imports that exact snapshot into every account, verifies
the encrypted identity/persona/memory/chat surfaces, clears import chat/trace
state, and creates the post-import worker receipt plus a content-free,
30-minute readiness receipt. At readiness time every synthetic lease must also
have at least 9,000 seconds remaining: 7,200 seconds for the bounded live judge
step plus 1,800 seconds for post-run verification and local cleanup.

```bash
python qa/verify_deployment.py \
  --expected-sha "$BUILD_SHA" \
  --expected-runtime hosted_resident \
  --receipt "$PRIVATE_ROOT/deployment-pre.json"

python qa/run_persona_memory_regression.py run-live \
  --target-id "${TARGET_LABEL}-${BUILD_SHA}" \
  --target-label "$TARGET_LABEL" \
  --build-sha "$BUILD_SHA" \
  --deployment-receipt "$PRIVATE_ROOT/deployment-pre.json" \
  --account-pool "$PRIVATE_ROOT/account-pool.json" \
  --readiness-receipt "$PRIVATE_ROOT/account-readiness.json" \
  --external-cleanup-guaranteed \
  --repetitions 3 \
  --concurrency 3 \
  --codex-bin "$CODEX_BIN" \
  --codex-home "$CODEX_HOME" \
  --judge-work-root "$PRIVATE_ROOT/codex-judge" \
  --judge-model "$QA_CODEX_MODEL" \
  --judge-id codex-oauth-persona-memory-v1 \
  --judge-configuration-id persona-memory-rubric-v1 \
  --judge-codex-profile persona_memory_judge \
  --judge-permission-profile io-e2e-agent-driven-test-persona-memory-judge \
  --judge-reasoning-effort medium \
  --allow-private-judge-egress \
  --output "$PRIVATE_ROOT/result.json"
```

Whether the arm passes or fails, the trusted outer workflow must continue with
post-deployment verification and full-pool cleanup. Cleanup reserves the
owner-only pool manifest before mutation and emits one sanitized row per leased
account. Every passing row proves provider projection and key-envelope removal,
old-credential rejection, and authoritative PostgreSQL absence using the
server-signed lease attestation. It then finalizes a small arm receipt that
proves both import and execution were bracketed and no synthetic account was
left behind.

```bash
python qa/verify_deployment.py \
  --expected-sha "$BUILD_SHA" \
  --expected-runtime hosted_resident \
  --receipt "$PRIVATE_ROOT/deployment-post.json"

python qa/prepare_persona_memory_accounts.py cleanup \
  --account-pool "$PRIVATE_ROOT/account-pool.json" \
  --receipt "$PRIVATE_ROOT/account-cleanup.json"

python qa/run_persona_memory_regression.py finalize-arm \
  --result "$PRIVATE_ROOT/result.json" \
  --readiness-receipt "$PRIVATE_ROOT/account-readiness.json" \
  --import-pre-deployment-receipt "$PRIVATE_ROOT/import-deployment-pre.json" \
  --import-post-deployment-receipt "$PRIVATE_ROOT/import-deployment-post.json" \
  --pre-deployment-receipt "$PRIVATE_ROOT/deployment-pre.json" \
  --post-deployment-receipt "$PRIVATE_ROOT/deployment-post.json" \
  --cleanup-receipt "$PRIVATE_ROOT/account-cleanup.json" \
  --output "$PRIVATE_ROOT/arm-receipt.json"

# Candidate-only on-demand runs publish only this strict aggregate projection.
# The raw result and arm receipt remain private.
python qa/publish_persona_memory_summary.py \
  --result "$PRIVATE_ROOT/result.json" \
  --arm-receipt "$PRIVATE_ROOT/arm-receipt.json" \
  --artifact-dir "$ARTIFACT_SCRATCH"
```

The formal semantic judge is a fresh, one-shot `codex exec` process for each
blinded trajectory. It reuses the run-scoped ChatGPT OAuth credential already
installed for the qualification supervisor and never receives an admin token,
provider key, account-pool manifest, or deployment URL. The locked permission
profile disables tools and tool-network egress; any tool event, malformed
structured output, timeout, OAuth outage, or rate-limit failure becomes
`BLOCKED_EVIDENCE`, never a product regression. The judge's model, Codex
version, prompt/schema contract, permission configuration, and reasoning
effort are bound into its configuration digest.

### Compare baseline and candidate

The shared test endpoint cannot host both builds at once. Complete the full arm
sequence for the baseline, deploy the candidate, then repeat it with a newly
provisioned pool and different target ID. Compare only finalized arms:

```bash
python qa/run_persona_memory_regression.py compare \
  --baseline "$BASELINE_PRIVATE/result.json" \
  --candidate "$CANDIDATE_PRIVATE/result.json" \
  --baseline-arm-receipt "$BASELINE_PRIVATE/arm-receipt.json" \
  --candidate-arm-receipt "$CANDIDATE_PRIVATE/arm-receipt.json" \
  --output-dir artifacts/persona-memory
```

Comparison exits `0` for parity/pass, `1` for a scored candidate regression,
and `2` for blocked evidence, infrastructure failure, or a contract mismatch.
Do not let `set -e` stop the workflow on a baseline arm exit `1`: retain the
code, continue through the candidate arm and comparison, and stop immediately
only on exit `2`. A minimal serialized CI shape is:

```bash
set -u

run_one_arm() {
  label="$1"
  sha="$2"
  private_root="$3"
  work_root="$4"
  artifact_scratch="$5"

  umask 077
  mkdir -m 700 "$private_root" "$work_root"
  mkdir -p "$artifact_scratch"
  QA_RUN_ID="persona-memory-${label}-${sha}" \
  python qa/run_persona_memory_arm.py \
    --target-label "$label" \
    --build-sha "$sha" \
    --profile official-openai \
    --repetitions 3 \
    --concurrency 3 \
    --private-root "$private_root" \
    --work-dir "$work_root" \
    --artifact-dir "$artifact_scratch" \
    --codex-bin "$CODEX_BIN" \
    --codex-home "$CODEX_HOME" \
    --judge-model "$QA_CODEX_MODEL" \
    --judge-id codex-oauth-persona-memory-v1 \
    --judge-configuration-id persona-memory-rubric-v1 \
    --judge-codex-profile persona_memory_judge \
    --judge-permission-profile io-e2e-agent-driven-test-persona-memory-judge \
    --judge-reasoning-effort medium \
    --allow-private-judge-egress
}

# Deploy BASELINE_SHA and verify that it is the active hosted_resident build.
set +e
run_one_arm baseline "$BASELINE_SHA" \
  "$BASELINE_PRIVATE" "$BASELINE_WORK" "$BASELINE_SCRATCH"
baseline_rc=$?
set -e
case "$baseline_rc" in 0|1) ;; *) exit 2 ;; esac

# Deploy CANDIDATE_SHA and verify that it is the active hosted_resident build.
set +e
run_one_arm candidate "$CANDIDATE_SHA" \
  "$CANDIDATE_PRIVATE" "$CANDIDATE_WORK" "$CANDIDATE_SCRATCH"
candidate_rc=$?
set -e
case "$candidate_rc" in 0|1) ;; *) exit 2 ;; esac

python qa/run_persona_memory_regression.py compare \
  --baseline "$BASELINE_PRIVATE/result.json" \
  --candidate "$CANDIDATE_PRIVATE/result.json" \
  --baseline-arm-receipt "$BASELINE_PRIVATE/arm-receipt.json" \
  --candidate-arm-receipt "$CANDIDATE_PRIVATE/arm-receipt.json" \
  --output-dir "$COMPARE_OUTPUT"
```

The deployment steps and both arms must belong to one lock owner. In GitHub
Actions, put the full baseline-deploy/run → candidate-deploy/run → compare
sequence in one workflow or job with, for example:

```yaml
concurrency:
  group: feedling-shared-test-persona-memory
  cancel-in-progress: false
```

Every workflow capable of mutating the shared test deployment must use the same
group (or the equivalent lease/mutex in another CI system); otherwise the
deployment receipts cannot exclude an intervening rollout.

Comparison fails closed before scoring if either arm lacks readiness, post-run
deployment identity, or complete cleanup proof; if account sets overlap; or if
route, Persona/source, evaluator, rubric, judge, repetition, or scenario
contracts differ. `run-result.json`, `matrix.md`, and `junit.xml` remain
allowlisted public artifacts; the two experiment JSON files stay private.

## Operational boundary

`run-live` concurrently starts isolated conversation sessions; it does not
create Linux containers. `provision-pool` owns admin registration and synthetic
leases; `prepare` owns Persona import and deterministic encrypted readback;
`run-live` owns plaintext trajectories and sends semantic judgments only through
the OAuth-backed headless Codex judge; the outer workflow owns post verification
and cleanup. No separate judge API key is used. Keeping those authorities
separate prevents the model-facing runner from acquiring the admin token.

Formal GitHub CI also has a manifest-independent durability sweep on a separate
GitHub-hosted job. After the JIT qualification attempt, it calls
`qa/provision_profiles.py cleanup-run` for the exact base run ID and its
`-persona-memory` suffix with bounded retries, using only
`IO_E2E_ADMIN_TOKEN`. Its aggregate receipt must be database-authoritative,
complete, and report both `operation_failure_count=0` and `remaining_count=0`.
This catches a registration response lost before a local manifest checkpoint;
runner-private manifests are therefore deleted even when local cleanup fails.
Cleanup first persists a hashed run-closed marker under the same cross-worker
database lock used by registration, preventing a late registration from racing
the final zero-row proof.
The successful hosted sweep retains a separate sanitized aggregate artifact and
GitHub step summary containing only fixed fields, counts, completion booleans,
and run/label hashes—never raw run IDs, account identities, or credentials.

If formal cleanup exits non-zero, retry the same command with the original
public argument paths; it automatically resumes from the owner-only hidden
`.account-pool.json.cleanup-pending` and
`.account-pool.json.cleanup-outcome.json` journals:

```bash
python qa/prepare_persona_memory_accounts.py cleanup \
  --account-pool "$PRIVATE_ROOT/account-pool.json" \
  --receipt "$PRIVATE_ROOT/account-cleanup.json"
```

Do not rename, copy, edit, publish, or delete either hidden journal. If initial
`provision-pool` itself failed and left `account-pool.json`, retry its lower-level
reaper instead:

```bash
python qa/provision_profiles.py cleanup \
  --manifest "$PRIVATE_ROOT/account-pool.json"
```

If provisioning fails before any credential manifest can be checkpointed, an
immediate manifest-bound reset cannot be proven: the supervisor reports
`provision_reaper_pending: true`. Formal CI then relies on the run-wide database
sweep, with the server-signed synthetic-account TTL reaper as a final fallback.
Do not interpret absence of a local manifest as proof that no account was
registered.

The legacy repeated `--manifest-profile ... --accounts-ready` path remains for
manual diagnostics only. It is operator-attested, lacks a pool-bound readiness
receipt, and cannot produce a finalized release arm. Formal CI must use
`--account-pool` through `run_persona_memory_arm.py` (or an equivalent protected
`always()` supervisor), pair it with the hosted run-wide cleanup job, and
serialize all mutations of the shared test deployment. The manual arm retains
its private recovery journal when cleanup is incomplete; formal CI removes JIT
scratch because the run-wide database sweep is its independent recovery path.
Local cleanup first reserves the credential manifest, checkpoints a
content-free successful outcome, then deletes credentials and publishes the
final receipt, so a manual receipt publication failure can be resumed without
recreating evidence.

References: [DeepEval multi-turn test cases](https://deepeval.com/docs/evaluation-multiturn-test-cases),
[DeepEval conversation simulator](https://deepeval.com/docs/conversation-simulator),
[LangSmith evaluation](https://docs.langchain.com/langsmith/evaluation), and
[LangSmith experiment comparison](https://docs.langchain.com/langsmith/compare-experiment-results).
