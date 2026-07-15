# Agent-driven API-key qualification

This directory contains the first deployed-runtime qualification slice for
Feedling API-key users. It intentionally covers API-key users only. VPS/OAuth,
iOS UI automation, and customer-incident replay remain separate follow-up
workstreams.

The dependency-light persona/memory baseline-candidate harness lives in
[`qa/regression/README.md`](regression/README.md). Its deterministic contract
tests run in ordinary CI; live execution remains an explicit, credentialed QA
operation.

## What runs

There are two targets:

- **baseline** (`deployed_current`, and the local driver's default) tests the
  runtime currently deployed on `test-api.feedling.app`, proves its protected
  backend build identity, records its reported mode/version, and does not
  claim that a legacy `runtime_version: 2` label proves the new Hosted Runtime V2 architecture;
- **strict Hosted Runtime V2** (`hosted_resident`) qualifies the deployed V2
  **user path**: every synthetic provider profile must independently read back
  exact mode `hosted_resident` and version `2` from `/v1/model_api/runtime`, and
  parent-owned P0-05 and P0-07 probes must re-confirm that target before and
  after hosted-loop activation. It does not claim worker-binary or queue-topology
  attestation because those identities are not exposed by the current runtime.

The protected workflow is deliberately split across explicit trust zones:

- Any collaborator with repository write access may press **Run workflow** on
  `ci.yml` at protected `main`; there is no per-run Environment reviewer. The
  controller and all secret-bearing harness code are fixed to the immutable
  `main` SHA selected when the run starts.
- A GitHub-hosted resolver checks out `test` without QA Environment, provider,
  admin, or OAuth secrets and derives the currently deployed backend SHA from
  the serialized compose image pin. The resolved SHA is data passed into
  qualification, never code executed by the secret-bearing runner.
- Only the ephemeral evaluator job enters `io-e2e-agent-driven-test`. It checks out
  the immutable controller SHA from `main`, receives the Environment secrets,
  and verifies the live backend against the resolver's expected SHA before
  provisioning any account or using any provider key. The reusable workflow is
  called without `secrets: inherit`.

1. `verify_deployment.py` uses the test admin credential before Codex starts and
   again after the agent finishes. Every mode requires the image-baked source SHA
   to equal the SHA injected by the serialized test deployment. Worker SHA and
   live-worker count are explicitly unavailable and remain `null`; strict V2
   behavior is proven by the per-profile user path described below. Both
   deployment receipts remain outside the public artifact directory.
2. `provision_profiles.py` is a deterministic credential boundary. It creates
   eight fresh provider-profile accounts plus one dedicated memory-contract
   account, proves invalid-key rejection and valid-key
   recovery without accepting echoed credentials, enables user-scoped trace
   access, requires a server-side synthetic-account TTL/reaper before the first
   registration, and reads the configured runtime through the user API. In
   strict V2 mode that authenticated readback must already report exact mode
   `hosted_resident` and version `2`; qualification does not mutate runtime mode
   through a test-only admin endpoint. A present-but-expired provider key becomes
   a fixed-code blocked row while provisioning continues through the other
   profiles, so a failed credential still produces a complete eight-row
   diagnostic matrix.
   P0-06 uses four checked-in representative onboarding files: each profile
   archives all four through the deployed multipart endpoint before submitting
   the exact same bytes and filenames to Genesis for agent-judged distillation.
3. The provisioner output is deterministically split into eight owner-only
   provider manifests and one owner-only memory manifest. Every provider
   worker explicitly denies the memory manifest as well as all seven sibling
   provider manifests. `run_codex_profile_workers.py` launches exactly eight
   independent top-level `codex exec` processes in three fixed batches (3+3+2),
   with at most three running concurrently. Each selected Codex profile exposes
   only its matching row and isolated home/temp/work roots; no process receives
   provider or admin credentials. Every profile worker and the aggregation
   supervisor have tool/shell network fully disabled—no allowed domains, no
   local binding, and no network proxy. Codex's authenticated OAuth model
   transport remains a separate trusted process boundary. All Feedling/provider
   I/O is performed by deterministic parent code after fixed local request
   markers; workers only review bounded facts and make semantic judgments.
4. Every profile agent returns one structured `profileResult`. For P0-02–P0-05
   and P0-07–P0-11, trusted launcher code accepts only the exact
   `request_live_scenario_probe.py` command and scenario/attempt-bound paths.
   The unprivileged helper creates a one-shot request; the parent performs the
   fixed live mutation, owns a sanitized `live-scenario-receipts.json`, and
   binds its status, IDs, turns, duplicate/order observations, and latencies to
   the result. For P0-06, the agent's CAPTURE command only requests work; the
   parent performs uploads and Genesis capture, keeps an authoritative copy
   outside the worker-readable roots, and gives the agent a byte-identical
   review copy. The agent runs an offline REVIEW and local Genesis FINALIZE;
   after it exits, the parent independently finalizes the authoritative copy
   with the same judgment and binds only that projection. P0-13 is also parent-owned: the
   parent reads/correlates trace evidence, derives five-stage latency, and owns
   cleanup while the worker copies and judges the bounded projection. The
   launcher validates the result against a
   profile-locked Structured Outputs schema, validates and binds the private
   P0-12 receipt to the result's exact
   request/turn/trace IDs and bounded reasoning fields, and binds the result
   hash, event hash, COT receipt hash, and root Codex thread ID into an
   owner-only lifecycle receipt. Raw command text and events/stderr stay
   quarantined; only validated JSON enters the separate aggregation-input
   directory. A structurally valid COT product failure is preserved in the
   receipt and artifacts for the deterministic final gate to reject rather than
   being erased by an early launcher exception.
5. A separate headless Codex qualification supervisor reads only those eight
   validated profile results and the trusted receipt. It preserves each profile
   judgment, computes the run summary and orchestration projection, and returns
   the canonical JSON final message against
   `schemas/codex-run-result.schema.json`. Its parent writes to a fresh private
   path, and `publish_agent_result.py` installs `run-result.json` exclusively
   without following or replacing an agent-created link.
6. `render_artifacts.py` validates that canonical result against the richer,
   authoritative gate schema at `schemas/run-result.schema.json` and
   mechanically derives the coverage matrix, numeric latency CSV, body-free
   JUnit XML, and exact per-profile JSON documents.
7. A separate deterministic memory-contract probe uses only the ninth account.
   It always requires fresh empty recall, encrypted v1 index/fetch, a real
   quiet-window capture write, exact route-trace correlation, disposable-chat
   capture no-op, duplicate-fact no-growth, local-only exclusion, and supersede
   visibility. Capture uses the checked-in resident parser/executor against the
   deployed endpoints with deterministic agent output; this proves execution
   and storage behavior without pretending to evaluate a live model's semantic
   choice. Legacy stable-ID migration and stale CAS preservation must either
   pass or be explicitly `NOT_EXERCISED` because the deployed migration kill
   switch is disabled, according to the checked-in policy. It writes only the
   bounded `memory-contract.json` receipt.
8. `validate_run.py` is a deterministic fail-closed gate. It checks the schema,
   exact profile/scenario order, scenario-specific assertions/evidence/IDs,
   preserved retry observations, per-turn five-stage trace and numeric latency
   evidence, and nearest-rank p50/p95 summaries recomputed from those turns,
   one supervisor plus exactly eight uniquely assigned independent profile
   workers with no more than three observed concurrently, exact agreement with
   the trusted process/thread/hash receipt, unchanged trusted pre/post liveness
   receipts, strict Runtime V2 user-path evidence when selected, exact binding
   to the owner-only read-only provisioning manifest, PASS statuses, and required
   artifact paths. A missing or uncorrelatable trace is
   `BLOCKED_EVIDENCE / TRACE_UNAVAILABLE`; a correlated turn missing any numeric
   `routing`, `queue`, `provider`, `persistence`, or `delivery` duration is
   `BLOCKED_EVIDENCE / TRACE_INCOMPLETE`. Neither can be inferred from history
   or agent prose.
9. The workflow always runs deterministic cleanup across all nine synthetic
   accounts. For every provider profile it proves the configured route existed,
   deletes the route when it is still authenticated, verifies the public
   projection and encrypted key envelope are gone, resets the account, confirms
   lease-attested PostgreSQL absence, and confirms the old Feedling key returns
   `401`. Profile workers never call cleanup endpoints. The P0-13 parent probe
   owns release cleanup; in the local adminless diagnostic it defers mutation so
   the outer deterministic parent can perform the sole reset. In both modes the
   same database-authoritative proof binds provider cleanup to the account
   deletion cascade. `validate_cleanup_receipt.py` requires the exact
   locked matrix and binds this sanitized receipt to the agent's cleanup
   fields. The private manifests remain available for the final secret scan,
   and only a scanned, cleanup-bound public artifact directory is uploaded.

`codex_output_schema.py --check` proves offline that the checked-in Codex
authoring schema is the exact compatible projection of the gate schema plus
the locked per-scenario assertion maps. The authoring schema intentionally
drops constraints unsupported by Structured Outputs; it does not replace or
weaken deterministic release validation.

The locked matrix is:

- official DeepSeek
- official Anthropic/Claude
- official OpenAI/ChatGPT
- official Google Gemini
- OpenRouter Claude
- OpenRouter OpenAI/ChatGPT
- OpenRouter GLM
- Kongbeiqie OpenAI-compatible relay

## Run the currently deployed test build locally

`run_local_diagnostic.py` is the operator path for testing the existing
`https://test-api.feedling.app` deployment without changing the `test` branch,
deploying another Feedling backend, or provisioning a special VPS. The headless
Codex workers run on the operator's machine and use the existing ChatGPT OAuth
session in `~/.codex/auth.json`. Provider keys remain confined to the
deterministic provisioner and are never placed in a Codex prompt or worker
environment.

The default is baseline qualification: it accepts any configured runtime status,
records the observed mode/version, and runs the full user-behavior journey. Add
`--require-runtime-v2` when the deployed target is expected to expose exact
`hosted_resident` version `2` through the full user path.

Before copying that OAuth bundle, the local driver treats PATH only as a package
locator, derives the native binary from the pinned official npm layout, and
verifies the exact platform package file set, ownership/modes, version, and
whole-tree digest. It invokes the verified native binary rather than the PATH
wrapper and rejects an installation beneath the checkout, run-private roots,
OAuth directory, public artifacts, or system temporary directory. The first
local operator slice pins Codex `0.144.3` on macOS arm64; `--codex-bin` can name
that installation's npm wrapper or native binary explicitly, but cannot bypass
the provenance check.

The dotenv file must be an owner-only regular file:

```sh
chmod 600 /absolute/path/.env.test
```

A repository-local `.env.test` is supported, but the live checkout is never a
Codex read root. Before configuring the workers, the deterministic parent makes
an owner-only source snapshot containing only `qa/`, `tools/provider_smoke/`,
`tools/genesis_e2e.py`, and `backend/content_encryption.py`. Within that
allowlist it excludes `.env*`, dependency caches, prior qualification artifacts,
and the exact dotenv and OAuth source paths. It also rejects any copied source
file containing any provider or admin credential loaded from the dotenv,
including credentials for profiles omitted from a subset run. Workers receive
read access only to that sanitized snapshot.

First prove that the pinned Codex CLI, copied OAuth session, model selection,
isolated config, and one real headless `codex exec` invocation work. This step
does not create Feedling users or call provider endpoints:

```sh
python3 qa/run_local_diagnostic.py \
  --env-file /absolute/path/.env.test \
  --codex-model gpt-5.6 \
  --profile official-gemini \
  --preflight-only
```

Then remove `--preflight-only` to create one fresh synthetic account and run the
live Gemini canary. Repeat `--profile` to select a bounded subset, or omit it to
run the locked eight-profile matrix. By default, the driver discovers the full
source SHA from the protected test-backend identity endpoint before Codex or
provisioning starts. `--candidate-sha <full-sha>` is an optional extra assertion:
if supplied, it must exactly match that authoritative identity. It is never a
way to label the live deployment.

For the future strict runtime candidate, append `--require-runtime-v2` to the
same command.

Local output is written under `qualification-artifacts/<run-id>/`. The sanitized
source snapshot, manifests, and copied OAuth material stay under a run-scoped
owner-only directory. After verified account cleanup, a passing run removes that
directory. A non-passing worker run first copies a bounded,
credential-scanned subset of raw
worker events, stderr, scratch files, and Codex session evidence to the owner-only
`~/.codex/io-e2e-agent-driven-test-debug/<run-id>/` quarantine, explicitly excluding the
provisioning manifests and any file containing known provider, synthetic-user,
content, or OAuth credentials; it then removes the original private run. The
summary records only `private_debug_retained` and its run ID. If account cleanup
fails, the private run directory is reduced to exactly the owner-only original
provisioning manifest required for cleanup retry. The source snapshot, copied
OAuth, worker outputs, raw events, profile manifests, and every other private
file are deleted, and `private_cleanup_retry_retained` is true.
If private finalization itself fails, the run fails closed and attempts to
remove the entire original private root instead of retaining partially scrubbed
manifests or raw evidence. If rendering or the public secret scan fails, every
would-be public artifact is quarantined by deleting the artifact directory and
rebuilding it with only a fixed, sanitized `SECURITY_FAIL` summary.

The public diagnostic summary and matrix always say
`release_qualified: false`: this path proves deployed end-user behavior and
captures partial evidence, but it cannot substitute for server-side reaper and
full-matrix release attestations.
`DIAGNOSTIC_PASS` additionally requires every selected profile's trusted COT
receipt to prove the correct final answer, one correlated reasoning event,
reasoning metadata, and a delivered user-visible disclosure. A profile agent
cannot override a missing, failed, or mismatched receipt with a PASS judgment.
For P0-13, the profile artifact deliberately remains `BLOCKED_EVIDENCE` with the
fixed parent-cleanup deferral; it is never rewritten. The worker must also copy
the parent probe's actual trace assertions and five-stage timing. The diagnostic
becomes green only when all five stages are correlated and numeric and the
deterministic parent publishes a separate exact cleanup verification plus a
parent-finalized per-profile projection. Missing trace evidence remains blocked
after cleanup. Attempted and cleaned counts must equal the selected profile
count, failed IDs must be empty, and the provisioning manifest must be deleted
and not missing.
When an otherwise valid receipt disagrees with the agent-authored projection,
the matrix reports the gate failure (`COT_RESULT_BINDING_MISMATCH`) separately
from the receipt's trusted observation status/code, so the underlying product
failure is not hidden by an agent reporting mistake.
The summary also records the exact harness Git HEAD, dirty state, whole-harness
source digest, worker-source digest, and exact copied worker-snapshot digest;
the run aborts before Codex if the snapshot bytes differ from the measured
source bytes.

Every profile runs `P0-01` through `P0-13`, including fresh onboarding, key
validation, four-part persona import/distillation, basic and ten-turn chat,
memory/persona consistency, model identity, reasoning disclosure, latency
attribution, trace correlation, and cleanup.

Persona qualification deliberately separates network capture from semantic
review. The worker's CAPTURE helper writes only a local request; deterministic
parent code imports once, keeps the authoritative decrypted evidence under the
worker-inaccessible output root, and writes a byte-identical owner-mode `0600`
review copy into the worker root. The profile agent reads that copy offline and
writes a bounded semantic judgment tied to the capture SHA-256. Its local
Genesis finalizer provides the bounded result needed for authoring, while the
parent independently applies the same judgment to the authoritative copy and
binds that sanitized projection into the receipt set. Both plaintext copies and
the judgment are deleted on every terminal path. Because the REVIEW tool prints
the copy for offline model inspection, the deterministic launcher also scrubs
that command's output from `events.jsonl` immediately after the worker exits,
before event hashing or any private diagnostic retention.

Codex is intentionally the semantic-judgment trust boundary, not an adversarial
program being cryptographically proved to have "thought." Deterministic code
proves ordered successful evidence access, rejects a fixed-path persona judgment
that already exists at REVIEW, independently finalizes an immutable parent copy,
binds the reviewed capture hash through the Genesis finalizer, and validates the
resulting schema/evidence. It cannot prove
the model's internal reasoning or defeat a deliberately deceptive judge that
manufactures an alternate prefill and copies it later; that would require a
second independent judge or a different trust model.

All eight profiles request reasoning effort `medium`, and the authenticated live
route readback must attest that `medium` is configured for the same
provider/model/base URL. A provider default, omitted setting, route mismatch, or
disabled reasoning cannot produce a release PASS.

P0-12 also guards the failure chain recorded in
[Router entry mrj6pdgl-6dppch](https://router.feedling.app/entry?id=mrj6pdgl-6dppch):
a route merely reporting `medium` is insufficient. The exact correlated turn
must prove a positive provider-visible reasoning/thinking signal, explicit
positive reasoning-token metadata, valid reasoning metadata, and a nonempty
user-visible summary/disclosure. The current deployed runtime does not expose a
trustworthy per-turn applied-effort field, so effective effort is deliberately
reported as `unknown` and unattested instead of being inferred from configured
state. This catches disabled or dropped reasoning, metadata, token, and
disclosure regressions without making the false claim that configured `medium`
was necessarily the model's applied effort. Exact applied-effort qualification
requires separate runtime trace instrumentation and is outside this QA-only
change. The suite never requests or stores a model's hidden private
chain-of-thought.
At P0-12 the worker writes a fixed request marker, and the trusted launcher runs
`cot_delivery_probe.py` once. The authoritative private receipt lives in that
profile's directory beneath the worker-output root, which the profile's
permission denies; the worker receives only a sanitized facts copy in its work
root. The receipt binds
the exact model-call trace, parsed-agent trace, stored reply ID, and decryptable
thinking envelope; the launcher validates and hashes that receipt before the
profile can be accepted as agent-authored diagnostic evidence. Missing provider
reasoning-token accounting remains explicitly unverified instead of being
invented from ordinary input/output token counts.
The launcher resolves one owner-controlled, crypto-capable Python executable,
fixes it as `QA_PYTHON_BIN` in every worker profile, grants only its narrow
runtime roots, and proves `"$QA_PYTHON_BIN" -I -B` can load the probe inside the
real sandbox before any synthetic account is provisioned. Workers may not build
their own virtual environments or install dependencies during qualification.

## `IO_E2E_ADMIN_TOKEN`

`IO_E2E_ADMIN_TOKEN` is the single credential accepted only by the **test**
backend's `/v1/admin/qa/*` routes. This is not issued by the application: the operator chooses one
strong random value, for example with `openssl rand -hex 32`, and stores it only
in secret managers. Protected self-service qualification uses it to read the
test build identity before Codex, verify the synthetic-account reaper, register
short-lived accounts, and prove cleanup. The adminless local diagnostic does not
use it. Runtime selection and runtime readback are authenticated user operations,
not admin operations. The bounded QA admin calls include:

- `POST /v1/admin/qa/synthetic-accounts/register`, with a normalized run ID
  and an exact `agent-e2e-<run-id>-` label prefix. The backend mints the signed,
  short-lived lease and rejects registration after that run enters terminal
  cleanup;

- `POST /v1/admin/qa/synthetic-accounts/absence` after cleanup, with the
  private `user_id`, `lease_id`, and server-issued HMAC absence token from the
  registration receipt. The endpoint verifies that lease attestation and reads
  PostgreSQL directly; a stale worker registry or database outage can never
  become a false absence. This proof is required before accepting either a
  fresh reset or an already-reset `401`; and
- `GET /v1/admin/qa/synthetic-account-reaper`, before creating any account, to
  require an enabled `agent-e2e-` label reaper with a maximum TTL no greater
  than four hours; and
- `POST /v1/admin/qa/synthetic-accounts/cleanup-run`, which atomically closes a
  normalized run against late registration, deletes every server-signed row
  under its exact label prefix, reloads PostgreSQL authoritatively, and returns
  only aggregate hashes and counts. This manifest-independent zero-remaining
  proof covers a lost registration response or a destroyed runner.

It does not authorize the application's ordinary admin routes or admin login.
Store it once as the repository or organization Actions secret
`IO_E2E_ADMIN_TOKEN`. Both the `test` deployment and trusted qualification
workflow reference that one secret and pass the same variable name into the
backend/client boundary. Never expose it to Codex, prompts, logs, or uploaded
artifacts. The application's existing legacy admin credential is unchanged and
is not accepted by these QA routes. Configure `IO_E2E_ADMIN_TOKEN` before merging
the CI change or the next test deploy will intentionally fail closed.

## Test backend and runner infrastructure

“Test backend” means the existing non-production deployment behind
`https://test-api.feedling.app`, including its backend, database, and whichever
runtime workers and queues are currently deployed. If that environment is already isolated from production,
you do **not** need another Feedling VPS just for this suite. The system under
test remains the existing test deployment.

The headless test driver is separate infrastructure. The workflow creates one
single-job JIT GitHub Actions runner on AWS with a unique per-run label, gives it
the Codex OAuth bundle only for that job, and destroys it afterward. It does not
host Feedling or replace a Runtime V2 worker. The test app deploy, test runner
deploy, test Postgres deploy, and qualification workflow share the
`io-e2e-agent-driven-test` concurrency lock. Pre/post build receipts still
catch a deployment made outside those workflows. The complete on-demand AWS
setup is documented in [`qa/aws/README.md`](aws/README.md).

## One-time GitHub setup

Create a protected GitHub Environment named `io-e2e-agent-driven-test`. Configure its
deployment branch policy to allow only protected `main`. Do **not** configure a
required reviewer: every collaborator with repository write access is meant to
be able to run this evaluation without waiting for a particular person. The
security boundary is protected `main`, not human approval on every run: changes
to the controller/harness must pass the repository's normal protected-branch
review, while a workflow dispatch can only use an already trusted immutable
`main` revision. The workflow also rejects any controller ref other than
`refs/heads/main`; that in-repository guard is defense in depth and does not
replace the Environment branch restriction.

First add the one repository or organization Actions secret used by both test
deployment and qualification:

- `IO_E2E_ADMIN_TOKEN`

Create it once, without `--env`, so both the protected `test` deployment and
the trusted `main` qualification controller read the same repository secret:

```bash
token="$(openssl rand -hex 32)"
printf '%s' "$token" |
  gh secret set IO_E2E_ADMIN_TOKEN --repo teleport-computer/feedling-mcp
unset token
```

The backend deliberately accepts this credential only when it is exactly the
64-character lowercase hexadecimal value produced above. It is authorized only
through `X-Admin-Token` or `Authorization: Bearer`; query-string and cookie
authentication are rejected so the credential cannot enter URL logs.

Then add these protected environment **secrets**:

- `QA_CODEX_AUTH_JSON_B64`
- `QA_DEEPSEEK_API_KEY`
- `QA_ANTHROPIC_API_KEY`
- `QA_OPENAI_PROVIDER_API_KEY`
- `QA_OPENROUTER_API_KEY`
- `QA_GEMINI_API_KEY`
- `QA_KONGBEIQIE_API_KEY`
- `QA_RUNNER_GITHUB_APP_PRIVATE_KEY`

Protect `test` as well as `main`: human code changes must arrive through the
normal reviewed path, force-push and deletion must be blocked, and only the
existing deployment automation may bypass protection for its serialized image-
pin commit. This is not a per-run approval. It is required because the deployed
backend necessarily receives the direct provider keys during exact BYOK tests.
Use dedicated QA-provider keys with the lowest practical balance, spend/rate
limits, and no production privileges; never use founder or production keys.

Add these non-secret environment **variables** with explicit, reasoning-capable
model IDs that the deployed candidate supports. Each selection must return the
reasoning metadata and token accounting required by `P0-12`; a model without
that capability correctly fails the release gate rather than silently reducing
coverage:

- `QA_CODEX_MODEL=gpt-5.6`
- `QA_DEEPSEEK_MODEL=deepseek-v4-flash`
- `QA_ANTHROPIC_MODEL=claude-sonnet-5`
- `QA_OPENAI_MODEL=gpt-5.6-terra`
- `QA_GEMINI_MODEL=gemini-3.5-flash`
- `QA_OPENROUTER_CLAUDE_MODEL=anthropic/claude-sonnet-5`
- `QA_OPENROUTER_OPENAI_MODEL=openai/gpt-5.6-terra`
- `QA_OPENROUTER_GLM_MODEL=z-ai/glm-5.2`
- `QA_KONGBEIQIE_MODEL=[特价纯血]claude-sonnet-5`
- `QA_KONGBEIQIE_BASE_URL` (the normalized HTTPS OpenAI-compatible endpoint)

These are explicit recommended pins, not hidden defaults. Verify every exact ID
against the configured provider endpoint before a live run; availability and
reasoning/token metadata can differ by account, region, or relay catalog. Keep
the variables required so a provider rename fails preflight instead of silently
changing the measuring instrument.

Add the non-secret disposable-runner variables described in
[`qa/aws/README.md`](aws/README.md):

- `QA_AWS_ROLE_ARN`
- `QA_AWS_REGION`
- `QA_AWS_AMI_ID`
- `QA_AWS_SUBNET_ID`
- `QA_AWS_SECURITY_GROUP_ID`
- `QA_RUNNER_GITHUB_APP_ID`
- `QA_RUNNER_GROUP_ID`
- `QA_RUNNER_GROUP_NAME`

`QA_CODEX_AUTH_JSON_B64` is the base64 encoding of a complete ChatGPT
`auth.json`. The initial version may use an operator's regular ChatGPT account;
a dedicated QA account remains the preferable long-term choice because this
bundle contains refreshable OAuth credentials. Never paste the bundle into a
workflow input or expose it to application-under-test code. Routine successful
runs do **not** require the operator to use ChatGPT's **Log out all devices**:
the run-scoped copy is deleted with the ephemeral VM. Use account-wide logout
only as incident response when exposure is suspected or the copied session must
be forcibly revoked; doing so may also require the operator to sign back in on
their normal devices. `codex login
--with-access-token` is not a substitute for this bundle in the pinned CLI: that
flag accepts Codex PAT/agent-identity credentials, not an ordinary ChatGPT OAuth
access token.

The workflow validates the bundle as refreshable ChatGPT auth, rejects API-key,
PAT, Bedrock, and agent-identity modes, installs it as mode `0600` under a
run-scoped `CODEX_HOME`, and masks each decoded token. The base64 bundle, decoded
JSON, ID token, access token, and refresh token are all included in the post-run
artifact secret scan.

Do not register or keep a persistent runner. The GitHub-hosted controller mints
a one-job JIT configuration and launches the checked-in AWS controller. That
controller pins `codex-cli 0.144.3` and the GitHub runner archives by digest,
supports the Codex Linux bubblewrap sandbox, creates an unprivileged runner
account without a persistent ChatGPT login, and gives the instance no SSH key
or IAM role. `actions/setup-python` installs Python 3.12 into a narrow tool-cache
runtime owned by that runner account: `sys.prefix`, `sys.base_prefix`, the
runtime `bin` directory, and the resolved executable must be owner-controlled
and not group/world writable, and the executable must resolve directly beneath
a runtime `bin`. A root-owned system Python or broad `/usr` prefix is
unsupported. The workflow validates this boundary before decoding the QA OAuth
bundle or provisioning synthetic accounts, so a misconfigured runner fails
safely.

The runner VM is ephemeral, and the workflow creates a fresh owner-only
`CODEX_HOME` for every run. Pinned Codex 0.144.3 does not reliably apply a
permission profile to native custom subagents, so this suite does not use that
mechanism. Every profile is instead a separate top-level invocation selected
with `-p <profile>`; its top-level `default_permissions` binding is checked by
strict config and real sandbox probes. Raw sessions, events, OAuth material, and
stderr remain private and disappear with the single-job runner.

The configured OAuth bundle is inside the trusted Codex-process boundary. Its
path is excluded from model-controlled shell environments and prompts, but the
suite does not pretend that Codex's own home can be sandboxed away from the
Codex process that must refresh it. Provider and admin keys remain wholly
outside that boundary. Each profile process receives a fresh empty
`HOME`/`TMPDIR`/work root and a deny-by-default permission profile: read-only
checkout access, read-only access to exactly one one-row synthetic-account
manifest, writes only to that worker's disposable roots, denial of public
artifacts, sibling manifests, raw worker outputs, aggregation inputs, the full
cleanup manifest, and the lifecycle receipt, disabled web/browser/apps/plugins
and login shells, and fully disabled tool/shell networking. There is no domain
allowlist and local binding is false. The aggregation supervisor has no manifest
or raw-output access and the same disabled-network boundary. Before
provisioning, the workflow verifies OAuth, strict profile selection, no
configured MCP server, filesystem boundaries, and that a real worker sandbox
cannot reach either the test API or a public endpoint. After provisioning, it
probes all eight exact mode-`0600` rows for own-read/other-deny isolation.

Keep an independent runner/VPC egress policy as a second boundary: the Codex
parent needs OpenAI/ChatGPT service access and deterministic parent probes need
the test API, while model-driven tool subprocesses must have no network path.
Codex's authenticated model transport is separate from that tool sandbox.
Prompt rules and artifact scanning are not credential-isolation controls.

### Scope of self-service branch testing

The implemented self-service path evaluates the backend that is **already
deployed to the shared protected `test` environment**. It does not yet build an
arbitrary feature branch, create a per-SHA preview deployment, or broker provider
requests for untrusted candidate code. To evaluate a code change today, deploy
that change through the normal protected `test` process, then dispatch the
trusted `main` controller. The artifact identifies the exact compose-pinned and
live-reported deployment SHA that was evaluated.

Do not attach these raw provider/admin keys to a workflow that checks out or
executes an arbitrary candidate branch. The current keys are acceptable only
for this protected deployed-test topology: untrusted branch code never runs on
the evaluator, and the secret-bearing harness always comes from protected
`main`. True "evaluate any branch" support needs a separate preview-deployment
and credential-broker design (for example, short-lived scoped credentials or a
trusted provider proxy) before it can preserve the same boundary. That preview
and broker layer is explicitly not implemented by this version.

The same dispatch also runs candidate-only persona and memory qualification
after the mandatory eight-provider P0 matrix. `persona_repetitions=1` is the
default developer smoke lane and uses eight fresh official-OpenAI synthetic
accounts; `persona_repetitions=3` is release depth and uses 24. The deep lane is
formal only for `runtime_target=hosted_resident`, where every account must prove
the expected Hosted Runtime V2 user-path mode and version. Worker binary SHA and
live-worker count remain unavailable and are not claimed. `deployed_current`
does not require that strict V2 proof and records an explicit
`NOT_FORMALLY_QUALIFIED` artifact rather than claiming missing coverage.

The semantic judge reuses the run-scoped ChatGPT OAuth and `QA_CODEX_MODEL`; no
`QA_EVAL_JUDGE_API_KEY` exists. Provision/import, Codex evaluation, and cleanup
are separate workflow steps, and provider/admin secrets never enter the Codex
step. Finalized cleanup must prove every synthetic account absent before the
allowlisted `persona-memory-summary.json` and `persona-memory-matrix.md` can be
uploaded. Raw conversations, judge rationales, evidence IDs, and account
fingerprints remain private and are deleted with the disposable runner.

An independent `cleanup-synthetic-accounts` job then runs on GitHub-hosted
infrastructure under the same protected `io-e2e-agent-driven-test` environment. It
uses only `IO_E2E_ADMIN_TOKEN`, derives the exact base and `-persona-memory`
run IDs, and retries the idempotent `cleanup-run` admin operation at most two
times per ID. The workflow fails unless the database-authoritative receipts
prove zero operation failures and zero remaining accounts. Because this sweep
does not depend on the disposable runner or a credential manifest, it also
covers runner crashes and registration responses lost before checkpointing;
private runner scratch is always removed instead of being retained for recovery.
The backend closes each run ID under the same cross-worker database lock used by
registration before it scans, so an in-flight registration either commits in
time to be swept or is rejected after closure.
On success, the hosted job uploads a separate 14-day
`io-e2e-synthetic-cleanup-<run>-<attempt>` artifact and writes the same exact
aggregate counts and hashes to the GitHub step summary; neither output contains
run IDs, account identities, credentials, or response text.

Release-depth timing is bounded explicitly: the qualification job may run for
330 minutes, the disposable evaluator self-terminates after six hours, persona
prepare/live/cleanup steps are capped at 60/120/30 minutes, the hosted account
sweep at 20 minutes, and runner teardown at 20 minutes. The instance hard expiry
is independent: it enforces the six-hour cap even if the serial GitHub-hosted
cleanup controller is still finishing after the qualification deadline. The
default smoke lane normally finishes substantially sooner. Persona readiness
additionally refuses to start unless every account lease has at least 9,000
seconds remaining, covering the 120-minute live step plus 30 minutes of post-run
verification and local cleanup.

## Before a live run

For a baseline local run, the deployed endpoint needs the existing API-key
onboarding, chat, persona, trace, and authenticated runtime-status contracts. It
also needs the test-only `GET /v1/admin/qa/build-identity` route, with the image's
full `FEEDLING_GIT_COMMIT` equal to the serialized deploy's
`IO_E2E_TEST_DEPLOY_SHA`. This branch must therefore be deployed to `test` once
before the hardened local driver can run; absence or mismatch fails before any
provider key is used. This baseline path does not require the deployed target to
identify itself as Hosted Runtime V2.

For the strict Hosted Runtime V2 GitHub qualification run, the deployed
candidate must additionally provide:

- an authenticated `/v1/model_api/runtime` readback of exact mode
  `hosted_resident` and version `2` for every configured synthetic profile;
- a functioning hosted-loop path that preserves that exact readback through
  parent-owned P0-05 discovery and P0-07 activation probes;
- deploy-enabled, user-scoped traces; and
- the admin-gated synthetic-account reaper status contract, backed by a real
  server-side TTL/janitor for `agent-e2e-` labels.

The pre/post deployment receipts still match the observable backend build to
the candidate. They deliberately record `observed_worker_sha: null` and
`live_worker_count: null`: current Runtime V2 does not expose trustworthy worker
binary identity, so this suite does not fabricate that stronger attestation.

Trigger **CI** manually at protected `main`. Any collaborator with repository
write access may do this; no Environment reviewer needs to be online:

```bash
gh workflow run ci.yml --ref main -f runtime_target=deployed_current
```

The manual-only `api-key-e2e-manual` job calls the reusable E2E workflow from
that selected immutable `main` revision and does not inherit caller secrets. A
separate GitHub-hosted job, outside `io-e2e-agent-driven-test`, checks out `test`
without QA Environment secrets, reads the backend image tag pinned in
`deploy/docker-compose.phala.test.yaml`, and resolves the short tag to a full Git
commit. The secret-bearing evaluator then checks out only the trusted `main`
harness and receives that resolved commit as expected-deployment metadata. This
binds the result to the deployed image, not to a later
`deploy(test): bump ... [skip ci]` branch-head commit or an operator-entered
SHA. The run fails closed if the compose file has mixed tags, the tag does not
resolve inside current `test` history, or the protected live backend reports a
different full SHA.

There is deliberately no free-form deployment-SHA or candidate-branch input.
Use `runtime_target=deployed_current` to qualify whichever configured runtime is
currently deployed, or `hosted_resident` to require the exact V2 user path. Any
controller ref other than protected `main` fails before the Environment or its
secrets are reached. Manual mode is intentional for the first stabilization
phase; the qualification itself has no push, schedule, or deployment trigger.
Only the secretless AWS expiry reaper runs hourly.

The baseline target requires authoritative backend image identity plus the
currently deployed API-key/user contracts. The strict Runtime V2 target requires
all eight provisioner readbacks and parent-owned P0-05/P0-07 receipts to prove
exact `hosted_resident` version `2`. This is end-user-path qualification, not a
claim about an unobservable worker binary or internal queue topology.

Runner cleanup has four independent paths: normal one-job shutdown with EC2
terminate-on-shutdown, a GitHub-hosted `always()` cleanup job, a root-owned
six-hour hard-expiry timer, and an hourly GitHub-hosted AWS tag reaper. The
synthetic accounts have separate protection: manifest-bound local cleanup,
the GitHub-hosted run-wide database sweep, and the backend TTL reaper. The
hosted sweep is ordered before AWS teardown and does not need the JIT evaluator
to survive.

## Artifacts and qualification result

`QA_ARTIFACT_DIR` is already the unique run directory. Codex returns only the
authoritative result JSON; the trusted publisher installs `run-result.json`, and
`render_artifacts.py` then derives `matrix.md`,
`latency.csv` (including numeric acknowledgement, reply, per-turn five-stage,
and profile-summary rows), `junit.xml`, and exact `profiles/<profile-id>.json`
copies directly beneath the same directory. The deterministic memory probe adds
`memory-contract.json`; it is not authored or adjudicated by a profile agent.
After all profile work, deterministic cleanup adds `cleanup-receipt.json`. That
receipt contains only locked IDs, booleans, a deletion-source enum, and a run ID;
it never contains account IDs, account keys, provider keys, or response bodies.
No second run-ID directory is
created. Public files must never contain provider keys, Feedling account keys,
private content keys, raw chat, raw traces, raw private reasoning, or free-form
evidence/failure text.

The seven summary fields count the exact terminal statuses of the eight profiles
and must sum to eight. The gate is green only when all eight profiles and all
thirteen scenarios per profile are present in order and PASS with their locked
assertions, evidence codes, required IDs, and preserved attempt history; pre/post
endpoint liveness and backend candidate identity are proven in every mode, with
worker identity explicitly unavailable and strict V2 per-profile user-path
evidence required when that target is selected; all chat turns have the
five required trace stages and numeric per-turn stage timing; every parent-owned
P0-13 receipt binds the exact 15 prior turns and matches the profile's bounded
trace/latency/cleanup projection; missing correlation is
`BLOCKED_EVIDENCE / TRACE_UNAVAILABLE` and any absent or null stage is
`BLOCKED_EVIDENCE / TRACE_INCOMPLETE`; deterministic
cleanup proves all nine accounts absent and all old keys rejected, and its exact
receipt agrees with the eight agent cleanup projections;
each worker has a completed qualification-tool event and a valid, passing,
result-bound P0-12 receipt; every parent-probed scenario has an exact helper
command plus a valid result-bound parent receipt, while P0-06 has its three
exact semantic phases; required files exist; and the redaction scan
is clean. The eight always-required memory checks must pass, and the two migration
checks must satisfy the locked migration policy. A blocked prerequisite is
useful evidence, but it is never a release
PASS.

A green result is evidence about that exact deployed test snapshot. It does not
approve or merge a release automatically; the team may use it as an on-demand
evaluation signal for the change currently deployed to `test`.
