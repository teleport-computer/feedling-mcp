# Branch Promotion Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Document and enforce that pull requests into `main` originate from `test` or `pre`, while development work defaults to test-environment validation first.

**Architecture:** A small shell script owns the branch predicate so it can be tested locally and called unchanged by GitHub Actions. The CI workflow invokes it for pull requests, while `CONTRIBUTING.md` and `AGENTS.md` describe the human and coding-agent responsibilities.

**Tech Stack:** Bash, GitHub Actions YAML, pytest, Markdown

## Global Constraints

- Pull requests targeting `main` accept only exact source branch names `test` and `pre`.
- Do not constrain merge direction between `test` and `pre`.
- Development branches default to pull requests against `test` and test-environment verification.
- Emergency bypasses are explicit maintainer actions recorded in the pull request.
- Do not modify public product documentation under `docs-site/content/docs/`.

---

### Task 1: Testable branch-flow predicate

**Files:**
- Create: `scripts/check-pr-branch-flow.sh`
- Create: `tests/test_branch_flow_guard.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: positional arguments `<base-branch> <head-branch>`.
- Produces: exit code `0` for allowed flows and non-zero with a GitHub annotation for rejected flows.

- [x] **Step 1: Write the failing predicate tests**

Add parametrized subprocess tests covering `test -> main`, `pre -> main`, a development branch targeting `main`, and development branches targeting `test` or `pre`. Register the module in `_PURE_UNIT`.

- [x] **Step 2: Run the test and verify RED**

Run: `PYTHONPATH=backend .venv-test/bin/python -m pytest tests/test_branch_flow_guard.py -q`

Expected: failures because `scripts/check-pr-branch-flow.sh` does not exist.

- [x] **Step 3: Implement the minimal predicate**

Create an executable Bash script that rejects only when base is `main` and head is neither `test` nor `pre`. It must reject missing arguments rather than silently passing.

- [x] **Step 4: Run the test and verify GREEN**

Run: `PYTHONPATH=backend .venv-test/bin/python -m pytest tests/test_branch_flow_guard.py -q`

Expected: all cases pass.

### Task 2: Workflow and guidance integration

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `CONTRIBUTING.md`
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: GitHub Actions `github.base_ref` and `github.head_ref`.
- Produces: required-check candidate named `branch flow` on pull requests.

- [x] **Step 1: Add the CI job**

Add a pull-request-only job near the top of `ci.yml`, check out the repository, and call:

```bash
scripts/check-pr-branch-flow.sh "$GITHUB_BASE_REF" "$GITHUB_HEAD_REF"
```

- [x] **Step 2: Add the canonical contributor policy**

Add a leading `CONTRIBUTING.md` section describing the normal development-to-test path, allowed `test`/`pre` sources for `main`, non-enforcement between environment branches, emergency exception records, and required GitHub ruleset configuration.

- [x] **Step 3: Add the coding-agent rule**

Add a concise `AGENTS.md` section forbidding agents from opening ordinary development PRs directly against `main`, defaulting ambiguous targets to `test`, and requiring test-environment evidence before proposing promotion.

- [x] **Step 4: Verify all changed artifacts**

Run:

```bash
PYTHONPATH=backend .venv-test/bin/python -m pytest tests/test_branch_flow_guard.py -q
ruby -e 'require "yaml"; YAML.load_file(".github/workflows/ci.yml", aliases: true); puts "workflow yaml ok"'
git diff --check
```

- [x] **Step 5: Commit only task files**

Commit the script, test, conftest registration, workflow, and two guidance files without including unrelated pre-existing staged files.
