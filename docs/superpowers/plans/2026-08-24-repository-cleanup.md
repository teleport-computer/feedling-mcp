---
document_lifecycle: current
canonical_owner: self
---
# Feedling Repository Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce stale repository surface and contradictory guidance so an agent can identify the current runtime, authoritative implementation, and correct diagnostic path without being misled by historical plans or retired architecture.

**Architecture:** Treat cleanup as an evidence-gated program, not a single deletion pass. First establish current truth and document lifecycle, then inventory and classify candidates, remove only candidates with production-consumer and compatibility evidence, and only afterwards consider structural decomposition of large modules. Each removal or behavior-preserving decomposition is delivered as its own reviewable PR against `test`.

**Tech Stack:** Python 3, pytest, Git, ripgrep, Markdown, YAML compose files, GitHub Actions, Feedling test/pre/prod deployment evidence.

**Spec:** This document contains the approved program design and execution gates. Candidate-specific code deletions and large-module decompositions require child implementation plans produced from the evidence artifacts defined below.

## Global Constraints

- Ordinary branches target `test`; production promotion follows `test`/`pre` to `main` and must not bypass the branch-flow check.
- Public API, architecture, trust-boundary, security, or deployment changes update `docs-site/content/docs/`, public OpenAPI artifacts when applicable, and the `Unreleased` public changelog in the same PR.
- Alembic history is immutable cleanup evidence, not a deletion target.
- Encrypted envelope shapes, persisted data, wire compatibility, TEE trust boundaries, rollback controls, and active V1/V2/Resident coexistence are protected until explicit production evidence proves retirement.
- Generated and vendored files are excluded from ordinary dead-code findings unless their generator or dependency is the candidate.
- Static analysis proposes candidates; it never proves that removal is safe.
- Documentation/history moves and runtime behavior changes must be separate PRs.
- `tools/chat_resident_consumer.py` remains a single-file VPS distribution boundary; this cleanup program must not decompose it into additional Python modules.
- No phase has a deletion quota. Net reduction, simpler ownership, and agent diagnostic accuracy matter more than candidate count.

---

## 1. Why this program is needed

The repository mixes historical state with live state in the same search surface:

- `README.md` says hosted resident supervisors and per-user CLI processes are retired.
- `docs/PROJECT_OVERVIEW.md` says the hosted manifest is `v2_only` and no hosted resident rollback exists.
- `docs/testing/README.md` says hosted V1 is no longer maintained.
- `deploy/docker-compose.phala.yaml` currently sets `FEEDLING_HOSTED_RUNTIME_POLICY: "dual"` and `FEEDLING_RUNTIME_DEFAULT_DESIRED: "resident"`.
- `backend/agent_runtime/` remains an active, recently changed hosted supervisor implementation.

An agent can therefore read a plausible but superseded document, classify active code as legacy, and investigate or delete the wrong runtime.

Baseline observed on 2026-08-24:

- 1,962 tracked files and 286 tracked Markdown files.
- 195 files under `docs/superpowers/plans/` and `docs/superpowers/specs/`.
- 61 plan/spec files contain retirement or supersession language, while none use a consistent retired/superseded status header.
- 746 top-level Python test files.
- Large execution surfaces include `tools/chat_resident_consumer.py` (20,475 lines), `backend/db.py` (17,739 lines), `backend/model_api_runtime/v2/worker.py` (16,462 lines), and `backend/model_api_runtime/v2/jobs_store.py` (12,903 lines).

These numbers are a snapshot, not acceptance targets. Recompute them at execution time.

## 2. Target information model

Interpret repository facts in this order:

1. Live test/pre/prod evidence and the exact deployed commit.
2. Deployment configuration and runtime wiring in that commit.
3. Production code and schema/wire contracts.
4. Contract and deployment tests.
5. Explicitly current architecture, operations, and testing documents.
6. Decision records whose constraints are still active.
7. Historical specs, plans, incident reports, changelogs, and git history.

Lower levels may explain why the system exists but cannot override higher levels when describing what currently runs.

Every tracked document receives one lifecycle:

- `current`: operational or architectural truth that must change with implementation.
- `decision`: durable rationale or constraint that remains binding.
- `historical`: implemented, rejected, superseded, or point-in-time evidence.
- `generated`: reproducible machine output whose generator is authoritative.

## 3. Candidate evidence record

Each simplification candidate records status, class, owner, exact symbols/paths/configuration/wire strings, production and non-production consumers, persistence obligations, proposal, net simplification, behavior given up, local verification, and test/pre/prod evidence.

Allowed outcomes are:

- `delete`: proven unused with no surviving compatibility obligation.
- `archive`: historical information that must leave default grounding.
- `retain-protected`: evidence shows the surface is intentional or still load-bearing.
- `feature-decision`: production consumers exist, so removal is a product/architecture decision rather than cleanup.

Candidates live under `docs/repository-cleanup/candidates/`. An accepted code candidate gets a child implementation plan before code changes begin.

---

### Task 1: Capture a reproducible baseline and agent benchmark

**Files:**
- Create: `docs/repository-cleanup/README.md`
- Create: `docs/repository-cleanup/baseline.md`
- Create: `docs/repository-cleanup/agent-diagnostic-benchmark.md`
- Create: `tools/repository_inventory.py`
- Test: `tests/test_repository_inventory.py`

**Interfaces:**
- Consumes: Git index, tracked paths, Markdown contents, compose files.
- Produces: deterministic counts grouped by production, test, documentation, generated/vendor, migration, tool/script, and ignored-local surfaces.

- [ ] Write failing classification tests covering migrations, historical plans, generated OpenAPI, runtime source, and ignored worktrees.
- [ ] Run `FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_repository_inventory.py -q`; expect import failure before implementation.
- [ ] Implement the inventory with `git ls-files` as the tracked corpus. It must never scan `.worktrees`, virtual environments, build output, or secrets, and must never label a file dead.
- [ ] Record benchmark questions covering runtime selection, `agent_runtime` ownership, resident memory lookup, decrypt ownership, self-update files, migration retention, test entry points, and deployed-versus-merged evidence.
- [ ] For each benchmark question record correct path, evidence, irrelevant files opened, elapsed time, and wrong-runtime selection.
- [ ] Run the inventory, review output, update `baseline.md`, and run `git diff --check`.
- [ ] Commit as `docs: baseline repository cleanup audit`.

### Task 2: Establish one current-state entry point

**Files:**
- Create: `docs/CURRENT_STATE.md`
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`
- Modify: `README.md`
- Modify: `docs/PROJECT_OVERVIEW.md`
- Modify: `docs/testing/README.md`
- Modify: `docs/testing/RUNTIME_MAP.md`
- Modify: `docs/CHANGELOG.md`
- Test: `tests/test_current_state_docs.py`

**Interfaces:**
- Consumes: prod/test/pre compose policies, runtime reconciler defaults, service entry points, current deployment documentation.
- Produces: one short current-state map and explicit truth precedence for agents.

- [ ] Write a failing test that parses production compose and rejects current docs whose hosted policy/default runtime disagree.
- [ ] Add a guard rejecting claims that `backend/agent_runtime/` is retired while its service wiring remains active.
- [ ] Run the test and capture the current `v2_only`/retired versus `dual`/`resident` contradiction.
- [ ] Write `docs/CURRENT_STATE.md` naming current runtime paths, data/trust boundaries, authoritative deployment files, and test entry points.
- [ ] Replace volatile duplicated summaries with links to `CURRENT_STATE.md`; retain old statements only inside clearly historical sections.
- [ ] Change session-start guidance to read `AGENTS.md` and `CURRENT_STATE.md` before the changelog.
- [ ] Run `tests/test_current_state_docs.py` and `git diff --check`.
- [ ] Commit as `docs: establish current repository truth map`.

### Task 3: Add document lifecycle enforcement

**Files:**
- Create: `docs/DOCUMENT_LIFECYCLE.md`
- Create: `tools/check_document_lifecycle.py`
- Test: `tests/test_document_lifecycle.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: tracked Markdown inventory and inbound links.
- Produces: lifecycle metadata, canonical-owner links, and incremental CI enforcement.

- [ ] Define `current`, `decision`, `historical`, and `generated`, including transition and rationale-transfer rules.
- [ ] Write failing tests for missing/invalid lifecycle and current docs whose sole authority is archived content.
- [ ] Implement `--changed-vs <git-ref>` so CI initially enforces new or modified documents only.
- [ ] Classify one active spec, one implemented plan, one superseded plan, and one generated report.
- [ ] Run the validator, exact inbound-link searches, and `git diff --check`.
- [ ] Commit as `docs: classify document lifecycle`.

### Task 4: Classify and archive historical plans by subsystem

**Files:**
- Modify: `docs/superpowers/plans/*.md`
- Modify: `docs/superpowers/specs/*.md`
- Create: `docs/archive/plans/`
- Create: `docs/archive/specs/`
- Modify: exact inbound references discovered during classification

**Interfaces:**
- Consumes: lifecycle rules, shipped implementation evidence, inbound links.
- Produces: a small active design surface and explicitly historical archive.

- [ ] Build a worksheet recording status, owner, inbound links, shipped evidence, compatibility obligations, and destination for every plan/spec.
- [ ] Review in separate PRs for hosted runtime, resident runtime, storage/TEE, memory/perception, API/product, and operations.
- [ ] Archive only fully superseded material; retain partial supersessions as linked decisions.
- [ ] Transfer unique rationale, alternatives, risks, and reintroduction conditions to the current owner before moving a file.
- [ ] Replace production-code citations to execution plans with current contracts, decisions, or focused module documentation.
- [ ] Run lifecycle validation, exact filename searches, and `git diff --check` for every batch.

### Task 5: Inventory tools, scripts, and test-only surfaces

**Files:**
- Create: `docs/repository-cleanup/tool-script-inventory.md`
- Create: strong candidates under `docs/repository-cleanup/candidates/`
- Modify: `tools/README.md`, relevant runbooks, and CI references

**Interfaces:**
- Consumes: `tools/`, `scripts/`, `ops/`, CI, deploy files, runbooks, imports, operator commands.
- Produces: one owner and lifecycle for every tracked tool/script.

- [ ] Classify every tool/script as production companion, deployment, recovery, migration, active diagnostic, test support, generated helper, historical, or unowned candidate.
- [ ] Search exact paths, module names, CLI basenames, configuration, systemd/compose, CI, and runbook references.
- [ ] Inspect ambiguous operator use manually; a missing Python import is not deletion proof.
- [ ] Write candidate records only for strong findings.
- [ ] Delete accepted candidates in small PRs, selecting verification from `docs/testing/TESTING.md`.

### Task 6: Audit runtime and compatibility surfaces

**Files:**
- Create: candidate records under `docs/repository-cleanup/candidates/`
- Modify: candidate-specific source/tests/docs/deploy/OpenAPI only through approved child plans

**Interfaces:**
- Consumes: hosted V1 resident, pooled V2, self-hosted resident, enclave, database, deploy, wire, and live environment evidence.
- Produces: evidence-backed delete/retain/feature-decision outcomes.

- [ ] Survey each runtime lane separately, including capability catalogs, provider adapters, background lanes, and selectors.
- [ ] Protect `FEEDLING_HOSTED_RUNTIME_POLICY`, desired-runtime rows, access-mode routing, parity metrics, and rollback paths until environment evidence proves retirement.
- [ ] Inspect columns, migrations, encrypted envelopes, public API shapes, stored state, event names, and compatibility readers before any removal.
- [ ] Produce one child plan per accepted candidate with exact red/green tests and deployment evidence.
- [ ] Merge into `test`, verify the deployed commit, and exercise the affected real lane before promotion.

### Task 7: Protect the VPS consumer as a stable single-file boundary

**Files:**
- Review: `tools/chat_resident_consumer.py`
- Create: `docs/repository-cleanup/resident-consumer-source-map.md`
- Review: `tests/test_chat_resident_self_update.py`
- Review: `tests/test_agent_runtime_resident_contract.py`
- Review: `tools/e2e/vps.py`
- Modify: `tools/README.md`
- Review: `deploy/feedling-chat-resident.service`
- Review: `deploy/Dockerfile.agent-runner`

**Interfaces:**
- Consumes: backend-advertised `expected_consumer_commit`, Git checkout, update relevance, requirements installation, `execv`, systemd restart, hosted image build, and existing import seams.
- Produces: a documented stable boundary around the unchanged `python tools/chat_resident_consumer.py` single-file distribution.

#### Decision and rationale

Do not split `tools/chat_resident_consumer.py` into additional Python modules.

This file is distributed directly to user VPS machines, launched by a stable systemd command, imported through several test seams, and also baked into the hosted agent-runner image. Its updater decides release relevance from the current runtime dependency set, checks out an advertised commit, installs changed requirements, and re-execs the same script. Decomposition would add import and update-discovery obligations across machines the team does not directly control. The repository-cleanup goal does not justify that operational risk.

The file may still receive evidence-backed deletion of obsolete code and local readability improvements that do not change its distribution shape. File length alone is not a reason to refactor it.

- [ ] Record the direct script, systemd, VPS E2E, environment-variable, checkpoint/session, exit, self-update, and hosted-image contracts in `resident-consumer-source-map.md`.
- [ ] Map major responsibilities to existing section headers and stable symbols so agents can navigate without loading the whole file.
- [ ] Mark the file as excluded from decomposition in the simplification skill and candidate template.
- [ ] Permit removal only when exact call-site, configuration, wire, persistence, and VPS/hosted distribution evidence proves the responsibility obsolete.
- [ ] Keep the executable path, process model, import behavior, update-relevance logic, and checkpoint/session formats unchanged in documentation-only cleanup batches.
- [ ] After any accepted internal deletion, run `tests/test_chat_resident_self_update.py`, `tests/test_agent_runtime_resident_contract.py`, and the affected `tests/test_chat_resident_consumer*.py` tests.
- [ ] For behavior-affecting deletions, run the applicable VPS P0 path and prove checkout/re-exec, checkpoint preservation, and the next chat turn on `test`.

### Task 8: Decompose other large modules after deletion audits

**Files:**
- Create child plans for `backend/db.py`, `backend/model_api_runtime/v2/worker.py`, `backend/model_api_runtime/v2/jobs_store.py`, and `backend/admin/data_track.py`

**Interfaces:**
- Consumes: accepted candidates, dependency direction, transaction/lifecycle ownership.
- Produces: smaller ownership units without changed behavior or unnecessary public APIs.

- [ ] Measure responsibilities, imports, call sites, transaction/lifecycle boundaries, and recent co-change history; length alone is insufficient.
- [ ] Delete obsolete responsibilities before moving survivors.
- [ ] Define exact destination files, compatibility exports, dependencies, red/green tests, and rollback in one plan per module.
- [ ] Preserve database transaction/lock ownership and asynchronous cancellation/terminal/publication ownership.
- [ ] Compare routes, entry points, transitions, and schema access before and after test-environment verification.

### Task 9: Add a Feedling simplification skill and guardrails

**Files:**
- Create: `.agents/skills/feedling-find-simplifications/SKILL.md`
- Modify: `.gitignore`
- Modify: `AGENTS.md`
- Modify: `.github/workflows/ci.yml`
- Modify: `docs/repository-cleanup/agent-diagnostic-benchmark.md`

**Interfaces:**
- Consumes: evidence template, protected surfaces, lifecycle, test matrix, deployment rules.
- Produces: repeatable audits that prefer strong evidence over candidate volume.

- [ ] Add narrow `.gitignore` exceptions for `.agents/skills/feedling-find-simplifications/SKILL.md`; keep `.agents/mailbox/` and all other local agent runtime state ignored.
- [ ] Retain the DeepSeek skill's consumer classification, exact searches, net-deletion accounting, lifecycle analysis, dependency scrutiny, and rejection rules.
- [ ] Replace DeepSeek Agent Notes, Node validation, and adapter assumptions with Feedling runtimes, TEE/data boundaries, Alembic, branch flow, docs synchronization, and environment evidence.
- [ ] Enforce deterministic facts in CI: lifecycle on changed docs, current-to-archive links, current-state/compose consistency, tool ownership, and resident update-manifest coverage.
- [ ] Keep unused-symbol output advisory instead of making it an automatic deletion/failure gate.
- [ ] Re-run the agent benchmark and record before/after evidence.

## Delivery sequence

Use this PR order, targeting `test` unless explicitly directed otherwise:

1. Baseline inventory and benchmark.
2. Current-state truth map and contradiction removal.
3. Document lifecycle rules and incremental CI.
4. Historical document batches by subsystem.
5. Tool/script ownership and strong deletion candidates.
6. Runtime/data/deploy candidate child plans and deletions.
7. VPS consumer source map, protected-boundary rules, and evidence-backed internal deletion only.
8. Other large-module decomposition plans and execution.
9. Feedling simplification skill, full lifecycle ratchet, and post-cleanup benchmark.

Do not combine stages 2 through 8 into one cleanup branch. Reviewers must be able to reject a deletion or decomposition without blocking truth-map and documentation improvements.

## Program completion criteria

- Current runtime selection is consistent across live evidence, compose, code, tests, and `docs/CURRENT_STATE.md`.
- Historical material is classified and excluded from default agent grounding.
- Every tracked tool/script has explicit ownership and lifecycle.
- Every removed production surface has consumer, persistence, compatibility, and deployment evidence.
- Public or architectural behavior changes include required docs/OpenAPI/changelog updates.
- `tools/chat_resident_consumer.py` remains a single-file distribution boundary, and any internal deletion preserves checkout/re-exec, checkpoint, hosted-image, and real VPS chat behavior.
- Agent benchmark results contain no wrong-runtime diagnosis.
- Candidate-specific checks and required test-environment evidence are recorded in their PRs.
