# IO agent-driven E2E tool

The IO E2E tool is an on-demand qualification service for deployed user
behavior. It is deliberately separate from push and pull-request CI: a test run
starts only when a repository writer (or that writer's coding agent) requests
one.

## Current capability

The implemented lane is `deployed_test`. Once the one-time trust configuration
below is active, it exercises the backend already
deployed at the protected test endpoint with the locked nine-provider matrix,
the formal persona-memory arm, deterministic cleanup, and the existing
agentic/deterministic evidence gates.

`branch_preview` is a reserved lane and fails closed. Selecting an arbitrary
branch does **not** make the shared test endpoint run that branch. A secure
branch-preview lane still requires both:

1. a disposable target host, database, enclave simulator, and Runtime V2 runner
   built from the immutable candidate SHA; and
2. a trusted provider broker that exchanges per-run, per-profile, expiring,
   budget-capped tokens for the reusable provider keys.

Running candidate code with raw reusable provider keys would let that code read
or exfiltrate them. Until both controls exist, the tool refuses arbitrary-branch
live qualification rather than reporting a misleading result. Exact raw-BYOK
storage/validation remains a property of the protected deployed-test lane even
after a brokered preview lane is added.

## Architecture

```text
teammate or coding agent
        |
        |  gh-authenticated io-e2e command
        v
tools.io_e2e CLI
        |  verifies repository write permission
        |  resolves test -> immutable SHA
        v
protected main: io-e2e-control.yml
        |  validates UUID/ref/SHA and binds deployed image
        |  stores request-manifest.json
        v
protected reusable evaluator: api-key-e2e.yml
        |  disposable AWS JIT runner
        |  deterministic provisioner (owns provider/admin credentials)
        |  isolated headless Codex workers and blinded persona judge
        |  deterministic validation, report build, and cleanup
        v
GitHub run summary + team-safe/encrypted artifacts
```

The chosen target is data. The secret-bearing controller and evaluator always
come from protected `main`; a target branch never supplies workflow, QA harness,
or cleanup code. GitHub Actions is the first execution and state backend for the
tool, not an automatic release gate.

## Agent and operator UX

Requirements:

- GitHub CLI is installed and `gh auth status` succeeds;
- the authenticated account has write access to the canonical
  `teleport-computer/feedling-mcp` repository; and
- the checked-in workflow and skill have reached protected `main`.

Both `main` and `test` must report as protected branches before the client or
controller will run. `main` must require reviewed pull requests and block direct,
force, and deletion pushes for ordinary writers; governance bypass must be
limited to trusted organization owners. `test` must allow only the reviewed
deployment path and any narrowly named deployment-bot bypass.
Repository write access grants permission to request a run, not permission to
replace its trusted controller or silently deploy an unreviewed target.

The CLI deliberately rejects forks, renamed repositories, case variants, and
other repositories. A run is accepted only when GitHub reports the canonical
repository, `workflow_dispatch`, controller branch `main`, the exact control
workflow path, and a full lowercase controller SHA. These checks happen before
request-UUID correlation so an unrelated run cannot impersonate a request by
copying its title.

Plan without spending provider credits:

```bash
python3 -m tools.io_e2e plan --ref test
python3 -m tools.io_e2e plan --ref test --json
```

Start the smoke-depth full matrix and wait for its terminal result:

```bash
python3 -m tools.io_e2e run --ref test --wait
```

Use release-depth persona repetitions only when needed:

```bash
python3 -m tools.io_e2e run --ref test --persona-repetitions 3 --wait
```

Follow or inspect an existing run:

```bash
python3 -m tools.io_e2e status RUN_ID
python3 -m tools.io_e2e watch RUN_ID
python3 -m tools.io_e2e results RUN_ID
python3 -m tools.io_e2e open RUN_ID
python3 -m tools.io_e2e cancel RUN_ID
```

Every command supports stable JSON output for coding agents. Machine-readable
data goes to stdout and operational diagnostics go to stderr. The CLI uses the
caller's existing `gh` credential but never prints, copies, or persists its
token. The checked-in `.agents/skills/io-e2e/SKILL.md` teaches Codex, Claude,
and other shell-capable agents to use the same commands; it contains no parallel
orchestration implementation.

## Trust and secret configuration

The `io-e2e-agent-driven-test` GitHub Environment must allow the protected
controller/evaluator workflow from `main` only. This restriction does not limit
which commit a future preview lane can test; it limits which code can receive
the evaluator secrets.

The dedicated organization runner group must select exactly the protected
top-level caller
`teleport-computer/feedling-mcp/.github/workflows/io-e2e-control.yml@refs/heads/main`.
GitHub evaluates reusable self-hosted jobs in the caller context; retaining the
old direct evaluator selection prevents the JIT runner job from starting.

Provider keys, Codex OAuth, and the narrow synthetic-account admin token stay in
the protected `io-e2e-agent-driven-test` Environment. The test deployment reads
the same logical admin token from a separate protected `io-test-deploy`
Environment. There is no repository-wide copy and `secrets: inherit` is
forbidden. Candidate code, profile agents, result renderers, and the CLI receive
none of those secrets.

Anyone with repository write access can request a run without a human
Environment reviewer. GitHub authorizes the caller, and the control workflow
independently verifies its repository, controller ref, UUID, target ref, and
immutable target SHA before entering the secret-bearing evaluator.

## Results and debugging

GitHub is the initial run database. The run summary is the team panel and the
CLI renders the same state. A successful request retains a small
`request-manifest.json` beside the existing team-safe report, binding the request
UUID, controller SHA, test head, deployed backend SHA, suite, runtime contract,
and persona depth.

The team-safe report retains the coverage matrix, per-profile result documents,
latency CSV, JUnit XML, cleanup receipt, persona-memory summary, fixed failure
codes, and sanitized next probes. It contains no provider/admin credential,
OAuth data, raw chat, or raw trace. Failure-only exact identifiers and bounded
debug evidence remain in the separately encrypted protected bundle and use the
existing shorter retention policy.

`io-e2e results` downloads only the request manifest and team-safe report into
the git-ignored `io-e2e-results/` tree. It does not download the encrypted
protected bundle; obtaining and decrypting that bundle is a separate, explicit
authorized-debug operation documented in `qa/README.md`.

Canceling a run asks GitHub to stop it, but cancellation never replaces cleanup:
the evaluator's `always()` cleanup, independent hosted account sweep, EC2
terminate-on-shutdown, and hourly reaper remain the authoritative safety net.

## Cost and concurrency

A smoke-depth full-provider run has historically cost roughly USD 1–3 across
all providers plus the temporary evaluator compute. That number is an estimate,
not a budget guarantee. The evaluator runs at most three provider-profile agents
concurrently and backfills freed slots; the formal persona-memory arm adds its
own work after the API-key matrix. Only start release-depth repetitions when the
extra semantic confidence is worth the added time and token spend.

## Adding branch previews later

Do not add a free-form base URL to the CLI or workflow. The preview controller
must create and sign a target descriptor containing the repository, controller
SHA, candidate SHA, exact HTTPS origin, image digest, expiry, nonce, runtime
requirement, and signer key. Every probe, report, cleanup action, and canonical
result must bind the same descriptor hash.

The preview host must be separate from the evaluator, have no evaluator role or
credential, and be destroyed by both normal cleanup and an independent TTL
reaper. The provider broker must enforce provider, model, request count, token
budget, expiry, and revocation for each profile token. A brokered preview is
functional branch qualification; it must not claim exact raw-BYOK storage
qualification or Phala/TEE release attestation.
