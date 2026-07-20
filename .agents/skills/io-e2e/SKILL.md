---
name: io-e2e
description: Plan, start, monitor, inspect, summarize, open, or cancel IO's trusted agent-driven E2E qualifications through the repository's universal CLI. Use when a teammate asks an agent to run or explain the API-key provider matrix, persona and memory regression, COT delivery, latency, cleanup, or qualification artifacts for a deployed test target or a requested branch, pull request, tag, or SHA.
---

# IO E2E

Use the dependency-free `tools.io_e2e` CLI from the repository root. Treat it
as the only control surface: it authenticates with GitHub, resolves references,
and dispatches the trusted controller without exposing qualification secrets to
the calling agent.

## Guardrails

- Keep the controller and workflow source on protected `main`. Treat a requested
  ref as target data, never as trusted harness code.
- Stop on `UNPROTECTED_TRUST_BRANCH`. Both `main` and `test` are trust anchors;
  never bypass this preflight or dispatch the workflow manually.
- Stop on `INSUFFICIENT_TRUST_RULES` or `UNSCOPED_QA_ENVIRONMENT`. A cosmetic
  branch rule or unrestricted Environment is not a safe substitute.
- Never read, request, print, copy, decode, or pass Codex OAuth, admin tokens,
  provider keys, GitHub App keys, private manifests, or protected debug payloads.
- Never dispatch the qualification workflow directly with `gh workflow run`;
  use the CLI so its trust and capability checks remain enforced.
- Do not claim that an arbitrary branch was tested unless `plan` identifies an
  implemented branch-preview lane and binds the run to that branch's immutable
  SHA. `deployed_test` tests the already deployed test service, not un-deployed
  code from the requested ref.
- Do not turn a `BLOCKED_EVIDENCE`, `UNVERIFIED`, `UNSUPPORTED`, skipped cell, or
  missing artifact into a pass. Distinguish product failures, security failures,
  evidence gaps, infrastructure failures, and unsupported requests.
- Treat artifacts as sanitized unless the report explicitly marks them private.
  Do not attempt to decrypt or expose protected evidence.
- Require the controller workflow to exist on protected `main` before the first
  live run. If the CLI reports `TRUSTED_WORKFLOW_UNAVAILABLE`, report that the
  tool still needs review and merge; never work around it by dispatching from a
  feature branch.

## Workflow

1. Confirm the repository root and GitHub authentication:

   ```bash
   git rev-parse --show-toplevel
   gh auth status
   ```

   Authentication must identify a GitHub user with write access to the target
   canonical repository, `teleport-computer/feedling-mcp`. Forks and other
   repositories are not valid control planes. Do not ask the user for a token
   in chat.

2. Plan before dispatching. Supply the exact requested ref; use `--sha` when the
   caller already supplied an immutable commit:

   ```bash
   python3 -m tools.io_e2e plan --ref <branch-tag-or-sha>
   python3 -m tools.io_e2e plan --ref <ref> --sha <40-hex-sha> --json
   ```

   Stop if the plan rejects the lane, cannot resolve the ref, detects an SHA
   mismatch, or says the target is not actually testable. Explain that outcome
   precisely. The only current live lane is `deployed_test`; branch preview is
   not implemented yet.

3. Start only after the plan accurately describes what the user wants tested:

   ```bash
   python3 -m tools.io_e2e run --ref test
   python3 -m tools.io_e2e run --ref test --wait --interval 10
   ```

   Supported qualification options are:

   ```text
   --repo OWNER/REPO
   --lane deployed_test
   --suite full
   --persona-repetitions 1|3
   --runtime-target deployed_current|hosted_resident
   --sha <40-hex-sha>
   --wait
   --interval 3..300
   --json
   ```

   Use `--wait` for the press-button path when the user wants the agent to stay
   through completion. Use `--interval` only together with `--wait`.
   Preserve the returned run ID. Never imply that dispatch success means the
   qualification passed.

4. Monitor without polling GitHub ad hoc:

   ```bash
   python3 -m tools.io_e2e status <run-id>
   python3 -m tools.io_e2e watch <run-id> --interval 10
   ```

   Use `status` for a snapshot and `watch` when the user asks to wait through
   completion. Report queued/running state plainly; do not invent progress from
   elapsed time.

5. Fetch and inspect the sanitized result only after completion:

   ```bash
   python3 -m tools.io_e2e results <run-id>
   python3 -m tools.io_e2e open <run-id>
   ```

   Read the machine-readable summary and matrix before interpreting individual
   failures. Ordinary `results` downloads only the request manifest and
   team-safe report; it never downloads the encrypted exact-ID debug bundle.
   Use `open` only when the user wants the GitHub run page.

6. Cancel only on an explicit request, then verify final cleanup/reaper evidence:

   ```bash
   python3 -m tools.io_e2e cancel <run-id>
   python3 -m tools.io_e2e status <run-id>
   ```

## Report the outcome

Lead with the disposition and the exact target the run actually exercised. Then
include:

- Run ID, immutable target SHA when applicable, lane, deployed endpoint, and
  observed runtime evidence.
- Matrix totals by provider and scenario, including PASS, product failure,
  security failure, blocked evidence, and unsupported counts.
- Persona/import, memory continuity, COT delivery, latency attribution, and
  deterministic cleanup outcomes separately.
- For each failure: profile/scenario, classification, evidence code, concise
  symptom, and the sanitized artifact path or GitHub link that supports it.
- Missing evidence and observability gaps as unknowns, not diagnoses.
- Cleanup completion and any remaining reaper action.

Use JSON mode when another agent must consume the command output. Do not paste a
large raw report into chat; summarize it and link the retained artifacts.
