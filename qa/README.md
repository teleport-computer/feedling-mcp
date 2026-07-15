# Agent-driven API-key qualification

This directory contains the first deployed-runtime qualification slice for
Feedling API-key users. It intentionally covers API-key users only. VPS/OAuth,
iOS UI automation, and customer-incident replay remain separate follow-up
workstreams.

## What runs

There are two targets:

- **baseline** (the local driver's default) tests the runtime currently deployed
  on `test-api.feedling.app`, proves its protected backend build identity, records
  its reported mode/version, and does not
  claim that a legacy `runtime_version: 2` label proves the new Hosted Runtime V2 architecture;
- **strict Hosted Runtime V2** is an opt-in target of the protected GitHub
  workflow and additionally requires admin mode selection, homogeneous
  worker/backend build identity, and V2 receipts.

The protected workflow is deliberately split across explicit trust zones:

- Any collaborator with repository write access may press **Run workflow** on
  `ci.yml` at protected `main`; there is no per-run Environment reviewer. The
  controller and all secret-bearing harness code are fixed to the immutable
  `main` SHA selected when the run starts.
- A GitHub-hosted resolver checks out `test` without QA Environment, provider,
  admin, or OAuth secrets and derives the currently deployed backend SHA from
  the serialized compose image pin. The resolved SHA is data passed into
  qualification, never code executed by the secret-bearing runner.
- Only the ephemeral evaluator job enters `feedling-e2e-test`. It checks out
  the immutable controller SHA from `main`, receives the Environment secrets,
  and verifies the live backend against the resolver's expected SHA before
  provisioning any account or using any provider key. The reusable workflow is
  called without `secrets: inherit`.

1. `verify_deployment.py` uses the test admin credential before Codex starts and
   again after the agent finishes. Every mode requires the image-baked source SHA
   to equal the SHA injected by the serialized test deployment. Strict V2 mode
   additionally requires homogeneous live-worker builds. Both receipts remain
   outside the public artifact directory.
2. `provision_profiles.py` is a deterministic credential boundary. It creates
   eight fresh provider-profile accounts plus one dedicated memory-contract
   account, proves invalid-key rejection and valid-key
   recovery without accepting echoed credentials, enables user-scoped trace
   access, requires a server-side synthetic-account TTL/reaper before the first
   registration, and reads the configured runtime through the user API. In
   strict V2 mode it also uses the test admin token to set and independently
   verify `hosted_resident`. A present-but-expired provider key becomes a fixed-code
   blocked row while provisioning continues through the other profiles, so a
   failed credential still produces a complete eight-row diagnostic matrix.
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
   provider or admin credentials.
4. Every profile agent returns one structured `profileResult`. For P0-02–P0-05
   and P0-07–P0-11, trusted launcher code accepts only the exact
   `request_live_scenario_probe.py` command and scenario/attempt-bound paths.
   The unprivileged helper creates a one-shot request; the parent performs the
   fixed live mutation, owns a sanitized `live-scenario-receipts.json`, and
   binds its status, IDs, turns, duplicate/order observations, and latencies to
   the result. P0-06 retains separate exact capture, evidence-review, and
   finalization calls. The launcher validates the result against a
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
   receipts, strict Runtime V2 identity when selected, exact binding to the owner-only read-only
   provisioning manifest, PASS statuses, and required artifact paths.
9. The workflow always resets all nine synthetic accounts and uploads only the
   public artifact directory after cleanup succeeds and an exact secret scan.

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
`--require-runtime-v2` only after the new Hosted Runtime V2 candidate is deployed.

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
  --codex-model gpt-5.4 \
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
`~/.codex/feedling-e2e-debug/<run-id>/` quarantine, explicitly excluding the
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
fixed parent-cleanup deferral; it is never rewritten. The diagnostic becomes
green only after the deterministic parent publishes a separate exact cleanup
verification and a parent-finalized per-profile projection. Attempted and
cleaned counts must equal the selected profile count, failed IDs must be empty,
and the provisioning manifest must be deleted and not missing.
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

Persona qualification is deliberately two-phase. The existing-session capture
imports once and writes decrypted live evidence only to an isolated worker's
owner-mode `0600` temp file. That profile agent reads it and writes a bounded
semantic judgment tied to the capture SHA-256; deterministic finalization checks
the hash and judgment contracts, emits only sanitized evidence, and deletes the
plaintext on every exit path.

Codex is intentionally the semantic-judgment trust boundary, not an adversarial
program being cryptographically proved to have "thought." Deterministic code
proves ordered successful evidence access, rejects a fixed-path persona judgment
that already exists at REVIEW, binds the reviewed capture hash through the
Genesis finalizer, and validates the resulting schema/evidence. It cannot prove
the model's internal reasoning or defeat a deliberately deceptive judge that
manufactures an alternate prefill and copies it later; that would require a
second independent judge or a different trust model.

All eight profiles lock reasoning effort to `medium`. A provider default, omitted
setting, or disabled reasoning cannot produce a release PASS.

P0-12 also guards the failure chain recorded in
[Router entry mrj6pdgl-6dppch](https://router.feedling.app/entry?id=mrj6pdgl-6dppch):
a route merely reporting `medium` is insufficient. The exact
correlated turn must prove reasoning capability enabled, requested/configured/
effective effort all equal to `medium`, a positive provider-visible
reasoning/thinking event count, token metadata, and a nonempty user-visible
summary/disclosure. `reasoning:false`, an effective `off` clamp, or zero events
fails even if setup echoed the requested effort. The suite never requests or
stores a model's hidden private chain-of-thought.
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

## `QA_TEST_ADMIN_TOKEN`

`QA_TEST_ADMIN_TOKEN` is the client-side name for the credential accepted by
the **test** backend's admin routes. Its value must match that deployment's
`FEEDLING_ADMIN_TOKEN`. This is not issued by Feedling: the operator chooses one
strong random value, for example with `openssl rand -hex 32`, and stores it only
in secret managers. Every qualification mode uses it only to read the protected
test build identity before Codex or provisioning begins. Protected self-service
qualification also uses it for the test-account reaper and cleanup; strict V2
mode additionally uses it to call:

- `POST /v1/admin/hosted-runtime-mode`, to select `hosted_resident` for a newly
  created synthetic user; and
- `GET /v1/admin/hosted-runtime-mode`, to independently read the selection
  back;
- `GET /v1/admin/v2-metrics`, before and after Codex runs, to produce the
  trusted deployment receipts from `backend_sha`, homogeneous `worker_shas`,
  and live worker count; and
- `GET /v1/admin/data-track/users/{id}`, only after an ambiguous cleanup `401`,
  to prove that the synthetic account is absent before treating it as already
  reset; and
- `GET /v1/admin/qa/synthetic-account-reaper`, before creating any account, to
  require an enabled `agent-e2e-` label reaper with a maximum TTL no greater
  than four hours.

This token has broader test-admin authority because the backend shares one
admin credential across admin routes. Keep it in the protected
`feedling-e2e-test` GitHub Environment, never use the production token, and
never expose it to Codex, prompts, logs, or uploaded artifacts.

The same random **test-only** value has three names at three boundaries:

- `TEST_FEEDLING_ADMIN_TOKEN`: repository or organization Actions secret used
  by the `test` deployment job;
- `FEEDLING_ADMIN_TOKEN`: environment variable injected into the deployed test
  backend; and
- `QA_TEST_ADMIN_TOKEN`: protected `feedling-e2e-test` Environment secret used
  by deterministic qualification steps.

The production deployment continues to use the separate Actions secret named
`FEEDLING_ADMIN_TOKEN`. Its value MUST differ from the test value. Configure
`TEST_FEEDLING_ADMIN_TOKEN` before merging the CI change or the next test deploy
will intentionally fail closed.

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
`feedling-test-environment` concurrency lock. Pre/post build receipts still
catch a deployment made outside those workflows. The complete on-demand AWS
setup is documented in [`qa/aws/README.md`](aws/README.md).

## One-time GitHub setup

Create a protected GitHub Environment named `feedling-e2e-test`. Configure its
deployment branch policy to allow only protected `main`. Do **not** configure a
required reviewer: every collaborator with repository write access is meant to
be able to run this evaluation without waiting for a particular person. The
security boundary is protected `main`, not human approval on every run: changes
to the controller/harness must pass the repository's normal protected-branch
review, while a workflow dispatch can only use an already trusted immutable
`main` revision. The workflow also rejects any controller ref other than
`refs/heads/main`; that in-repository guard is defense in depth and does not
replace the Environment branch restriction. Add these environment **secrets**:

- `QA_CODEX_AUTH_JSON_B64`
- `QA_TEST_ADMIN_TOKEN`
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

- `QA_CODEX_MODEL` (pin to `gpt-5.4` for the qualified Codex CLI contract)
- `QA_DEEPSEEK_MODEL`
- `QA_ANTHROPIC_MODEL`
- `QA_OPENAI_MODEL`
- `QA_GEMINI_MODEL`
- `QA_OPENROUTER_CLAUDE_MODEL`
- `QA_OPENROUTER_OPENAI_MODEL`
- `QA_OPENROUTER_GLM_MODEL`
- `QA_KONGBEIQIE_MODEL`
- `QA_KONGBEIQIE_BASE_URL` (the normalized HTTPS OpenAI-compatible endpoint)

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
and login shells, and managed-proxy traffic only to `test-api.feedling.app`.
The aggregation supervisor has no manifest or raw-output access and runs with
network proxying disabled. Before provisioning, the workflow verifies OAuth,
strict profile selection, no configured MCP server, filesystem boundaries,
allowed test-API egress, and denied external/raw-IP bypasses. After provisioning,
it probes all eight exact mode-`0600` rows for own-read/other-deny isolation.

Keep an independent runner/VPC egress policy as a second boundary: the Codex
parent needs OpenAI/ChatGPT service access, while model-driven subprocesses should
reach only `test-api.feedling.app`. Prompt rules and artifact scanning are not
credential-isolation controls.

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

## Before a live run

For a baseline local run, the deployed endpoint needs the existing API-key
onboarding, chat, persona, trace, and authenticated runtime-status contracts. It
also needs the test-only `GET /v1/admin/qa/build-identity` route, with the image's
full `FEEDLING_GIT_COMMIT` equal to the serialized deploy's
`FEEDLING_TEST_DEPLOY_SHA`. This branch must therefore be deployed to `test` once
before the hardened local driver can run; absence or mismatch fails before any
provider key is used. This path does not wait for the new Hosted Runtime V2
feature branch.

For the strict Hosted Runtime V2 GitHub release run, the deployed candidate must additionally provide:

- the Runtime V2 admin set/readback routes;
- the admin-gated V2 metrics contract with `backend_sha`, `worker_shas`, and a
  positive live-worker count matching the candidate SHA;
- `hosted_resident` Runtime V2 workers and queues;
- deploy-enabled, user-scoped traces; and
- the admin-gated synthetic-account reaper status contract, backed by a real
  server-side TTL/janitor for `agent-e2e-` labels; and
- observable backend and worker build identity that can be matched to the
  candidate commit.

Trigger **CI** manually at protected `main`. Any collaborator with repository
write access may do this; no Environment reviewer needs to be online:

```bash
gh workflow run ci.yml --ref main -f runtime_target=deployed_current
```

The manual-only `api-key-e2e-manual` job calls the reusable E2E workflow from
that selected immutable `main` revision and does not inherit caller secrets. A
separate GitHub-hosted job, outside `feedling-e2e-test`, checks out `test`
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
Use `runtime_target=deployed_current` for today's runtime and reserve
`hosted_resident` for the future strict Runtime V2 proof. Any controller ref
other than protected `main` fails before the Environment or its secrets are
reached. Manual mode is intentional for the first stabilization phase; the
qualification itself has no push, schedule, or deployment trigger. Only the
secretless AWS expiry reaper runs hourly.

The baseline target requires authoritative backend image identity plus the
currently deployed API-key/user contracts. The optional strict Runtime V2 target
additionally fails before semantic testing unless worker build identity and all
strict control-plane receipts exist. Those strict requirements are future
product/deployment prerequisites, not claims made by this testing branch.

Runner cleanup has four independent paths: normal one-job shutdown with EC2
terminate-on-shutdown, a GitHub-hosted `always()` cleanup job, a root-owned
five-hour hard-expiry timer, and an hourly GitHub-hosted AWS tag reaper. The
backend still needs its separate server-side TTL/reaper for `agent-e2e-*`
synthetic accounts so an abruptly lost runner cannot strand test users.

## Artifacts and qualification result

`QA_ARTIFACT_DIR` is already the unique run directory. Codex returns only the
authoritative result JSON; the trusted publisher installs `run-result.json`, and
`render_artifacts.py` then derives `matrix.md`,
`latency.csv` (including numeric acknowledgement, reply, per-turn five-stage,
and profile-summary rows), `junit.xml`, and exact `profiles/<profile-id>.json`
copies directly beneath the same directory. The deterministic memory probe adds
`memory-contract.json`; it is not authored or adjudicated by a profile agent.
No second run-ID directory is
created. Public files must never contain provider keys, Feedling account keys,
private content keys, raw chat, raw traces, raw private reasoning, or free-form
evidence/failure text.

The seven summary fields count the exact terminal statuses of the eight profiles
and must sum to eight. The gate is green only when all eight profiles and all
thirteen scenarios per profile are present in order and PASS with their locked
assertions, evidence codes, required IDs, and preserved attempt history; pre/post
endpoint liveness and backend candidate identity are proven in every mode, with
worker identity and strict V2 controls required only when that target is
selected; all chat turns have the
five required trace stages and numeric per-turn stage timing; cleanup succeeds;
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
