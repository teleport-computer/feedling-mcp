#!/usr/bin/env python3
"""Read-only deployment gate: compare against a proven complete release unit.

A push's `before` is never deployment evidence. Unknown/partial history requires
normal deployment gates; unreadable GitHub evidence blocks with UNMEASURED.
No workflow, deployment, signature, claim, or remote ref is mutated here.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode


class Unmeasured(RuntimeError):
    pass


ENVIRONMENTS = {
    "test": (
        "deploy CVM (test)",
        "deploy runner CVM (test)",
        "deploy/docker-compose.phala.test.yaml",
        "deploy/docker-compose.phala.runner.yaml",
    ),
    "pre": (
        "deploy CVM (pre)",
        "deploy runner CVM (pre)",
        "deploy/docker-compose.phala.pre.yaml",
        "deploy/docker-compose.phala.pre.runner.yaml",
    ),
    "main": (
        "deploy CVM",
        "deploy runner CVM (prod)",
        "deploy/docker-compose.phala.yaml",
        "deploy/docker-compose.phala.prod.runner.yaml",
    ),
}
SEND = "Send prepared compose authorization once"
SHA = re.compile(r"[0-9a-f]{40}\Z")


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=45)
    if result.returncode:
        # Do not echo transport stderr, tokens, URLs with credentials, or objects.
        raise Unmeasured(f"{args[0]} evidence command exited {result.returncode}")
    return result.stdout


def git(*args):
    return command(["git", *args]).strip()


def api(path):
    try:
        return json.loads(command(["gh", "api", "--method", "GET", path]))
    except (ValueError, subprocess.TimeoutExpired) as exc:
        raise Unmeasured("GitHub metadata unavailable or malformed") from exc


def passed(job, steps):
    if job.get("status") != "completed" or job.get("conclusion") != "success":
        return False
    for name in steps:
        matches = [s for s in job.get("steps", []) if s.get("name") == name]
        if len(matches) != 1 or matches[0].get("conclusion") != "success":
            return False
    return True


def complete_release(branch, jobs, source):
    main_name, runner_name, *_ = ENVIRONMENTS[branch]
    main = [j for j in jobs if j.get("name") == main_name]
    runner = [j for j in jobs if j.get("name") == runner_name]
    if len(main) != 1 or len(runner) != 1:
        return "unknown"
    if all(
        j.get("status") == "completed" and j.get("conclusion") == "skipped"
        for j in main + runner
    ):
        if any(
            j.get("name", "").startswith("authorize prod runner (")
            and j.get("conclusion") != "skipped"
            for j in jobs
        ):
            return "incomplete"
        return "no-deployment"
    if not passed(main[0], ["Deploy CVM via phala", SEND]):
        return "incomplete"
    runner_steps = (
        ["Deploy each prod runner CVM via phala"]
        if branch == "main"
        else ["Deploy runner CVM via phala", SEND]
    )
    if not passed(runner[0], runner_steps):
        return "incomplete"
    if branch == "main":
        # Mirror the canonical inventory's whole-line comment/blank handling.
        ids = {
            line.strip()
            for line in git(
                "show", f"{source}:deploy/prod-runner-cvm-ids.txt"
            ).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        expected = {f"authorize prod runner ({cvm})" for cvm in ids}
        publishers = [
            j for j in jobs if j.get("name", "").startswith("authorize prod runner (")
        ]
        if (
            not ids
            or len(publishers) != len(expected)
            or {j["name"] for j in publishers} != expected
        ):
            return "incomplete"
        if not all(passed(j, [SEND]) for j in publishers):
            return "incomplete"
    return "complete"


def release_tree(branch, source, head, owner):
    """Use only the exact deterministic pin child, never arbitrary image edits.

    Successful jobs check out a generated [skip ci] pin child of the triggering SHA.
    Comparing only that trigger would treat every later docs push as an image change.
    The same exact subject/identity/content proof as pin-runtime-release.sh is used.
    """
    if not SHA.fullmatch(source):
        raise Unmeasured("invalid historical source SHA")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", source, head],
        capture_output=True,
        timeout=45,
    )
    if ancestor.returncode == 1:
        return None
    if ancestor.returncode:
        raise Unmeasured("historical source object/ancestry unavailable")
    paths = ENVIRONMENTS[branch][2:]
    expected = {}
    for path, image in zip(paths, ("feedling", "feedling-agent-runner")):
        raw = command(["git", "show", f"{source}:{path}"])
        pattern = rf"ghcr\.io/[^/]+/{image}:[a-f0-9]+"
        if not re.search(pattern, raw):
            return None
        expected[path] = re.sub(
            pattern, f"ghcr.io/{owner.lower()}/{image}:{source[:7]}", raw
        )
    if all(
        command(["git", "show", f"{source}:{p}"]) == content
        for p, content in expected.items()
    ):
        return source
    # Bounded ancestry walk. Failure to locate a pin is UNKNOWN, never no-op.
    for line in git(
        "rev-list",
        "--max-count=4000",
        "--ancestry-path",
        "--parents",
        f"{source}..{head}",
    ).splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[1] != source:
            continue
        pin = fields[0]
        metadata = git(
            "show", "-s", "--format=%s%n%an <%ae>%n%cn <%ce>", pin
        ).splitlines()
        if metadata != [
            f"deploy({branch}): pin Runtime V2 release :{source[:7]} [skip ci]",
            "feedling-ci <ci@feedling.app>",
            "feedling-ci <ci@feedling.app>",
        ]:
            continue
        changed = set(git("diff", "--name-only", source, pin).splitlines())
        if not changed or not changed.issubset(paths):
            continue
        if all(
            command(["git", "show", f"{pin}:{p}"]) == content
            for p, content in expected.items()
        ):
            return pin
    return None


def run_jobs(repo, run):
    jobs = []
    for page in range(1, 5):  # prod matrix <=256 plus ordinary CI jobs
        payload = api(
            f"repos/{repo}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100&page={page}"
        )
        batch = payload.get("jobs")
        if not isinstance(batch, list) or not isinstance(
            payload.get("total_count"), int
        ):
            raise Unmeasured("malformed deployment jobs page")
        jobs.extend(batch)
        if len(jobs) == payload["total_count"]:
            if len({j.get("id") for j in jobs}) != len(jobs):
                raise Unmeasured("duplicate deployment job metadata")
            return jobs
        if len(batch) != 100:
            break
    raise Unmeasured("incomplete deployment jobs pagination")


def decide(repo, branch, head, run_id, paths):
    if (
        branch not in ENVIRONMENTS
        or not SHA.fullmatch(head)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
    ):
        raise Unmeasured("invalid deployment identity")
    if (
        git("rev-parse", "HEAD") != head
        or git("rev-parse", "--is-shallow-repository") != "false"
    ):
        raise Unmeasured("exact full-history checkout required")
    if not paths or any(
        not p or p.startswith(("-", "/", ":")) or ".." in p for p in paths
    ):
        raise Unmeasured("invalid deployment path list")
    current = api(f"repos/{repo}/actions/runs/{run_id}")
    if (
        current.get("id") != run_id
        or current.get("head_sha") != head
        or current.get("head_branch") != branch
        or current.get("event") != "push"
        or current.get("path") != ".github/workflows/ci.yml"
    ):
        raise Unmeasured("current run binding mismatch")
    if current.get("run_attempt") != 1:
        return True, "rerun-needs-reconciliation", None
    query = urlencode({"branch": branch, "event": "push", "per_page": 50})
    payload = api(
        f"repos/{repo}/actions/workflows/{current['workflow_id']}/runs?{query}"
    )
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise Unmeasured("malformed workflow history")
    runs = sorted(runs, key=lambda r: r["run_number"], reverse=True)
    examined = 0
    for run in runs:
        if run["id"] == run_id:
            continue
        if (
            run.get("head_branch") != branch
            or run.get("event") != "push"
            or run.get("workflow_id") != current["workflow_id"]
        ):
            raise Unmeasured("cross-environment workflow history")
        if run["run_number"] > current["run_number"]:
            raise Unmeasured("deployment-superseded: a newer candidate owns the branch")
        if run.get("status") != "completed":
            raise Unmeasured("earlier release still in progress")
        examined += 1
        if examined > 20:
            break
        source = run.get("head_sha", "")
        if not SHA.fullmatch(source):
            raise Unmeasured("invalid historical source SHA")
        state = complete_release(branch, run_jobs(repo, run), source)
        if state == "no-deployment":
            continue
        if state != "complete":
            return True, "previous-release-" + state, None
        baseline = release_tree(branch, source, head, repo.split("/")[0])
        if baseline is None:
            return True, "successful-release-tree-unknown", None
        # Explicit pathspecs: exact filenames remain literal; ** is a git glob.
        specs = [(":(glob)" if "*" in p else ":(literal)") + p for p in paths]
        result = subprocess.run(
            ["git", "diff", "--quiet", baseline, head, "--", *specs],
            capture_output=True,
            timeout=45,
        )
        if result.returncode not in (0, 1):
            raise Unmeasured("cannot compare release trees")
        return result.returncode == 1, "compared-complete-release", baseline
    return True, "no-proven-release-in-bounded-history", None


def main():
    try:
        changed, reason, baseline = decide(
            os.environ["GITHUB_REPOSITORY"],
            os.environ["GITHUB_REF_NAME"],
            os.environ["GITHUB_SHA"],
            int(os.environ["GITHUB_RUN_ID"]),
            os.environ["DEPLOY_PATHS"].splitlines(),
        )
        result = {
            "cvm": str(changed).lower(),
            "reason": reason,
            "baseline": baseline or "unknown",
        }
        print(json.dumps(result, sort_keys=True))
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
            stream.write("".join(f"{k}={v}\n" for k, v in result.items()))
    except (
        Unmeasured,
        KeyError,
        ValueError,
        TypeError,
        OSError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(
            f"::error::UNMEASURED deployment baseline ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
