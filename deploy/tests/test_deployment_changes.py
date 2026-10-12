"""Run the actual workflow step against local git history and fake read-only GH."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
SCRIPT = ROOT / "deploy/deployment_changes.py"
spec = importlib.util.spec_from_file_location("deployment_changes", SCRIPT)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def commit(root, message):
    git(root, "add", ".")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


def write(root, path, data):
    p = root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data)


@pytest.fixture(params=["test", "pre", "main"])
def release(tmp_path, request):
    branch = request.param
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "feedling-ci")
    git(root, "config", "user.email", "ci@feedling.app")
    for path, image in zip(
        gate.ENVIRONMENTS[branch][2:], ["feedling", "feedling-agent-runner"]
    ):
        write(root, path, f"image: ghcr.io/org/{image}:abcdef0\nother: unchanged\n")
    write(root, "backend/app.py", "old\n")
    write(root, "tools/chat_resident_consumer.py", "old consumer\n")
    write(
        root,
        "deploy/prod-runner-cvm-ids.txt",
        "# comment\n runner-a \nrunner-b\nrunner-a\n",
    )
    source = commit(root, "release source")
    for path, image in zip(
        gate.ENVIRONMENTS[branch][2:], ["feedling", "feedling-agent-runner"]
    ):
        write(
            root, path, f"image: ghcr.io/org/{image}:{source[:7]}\nother: unchanged\n"
        )
    pin = commit(
        root, f"deploy({branch}): pin Runtime V2 release :{source[:7]} [skip ci]"
    )
    jobs = []

    def add(name, steps, conclusion="success"):
        jobs.append(
            {
                "id": len(jobs) + 1,
                "name": name,
                "status": "completed",
                "conclusion": conclusion,
                "steps": [{"name": s, "conclusion": "success"} for s in steps],
            }
        )

    names = gate.ENVIRONMENTS[branch]
    add(names[0], ["Deploy CVM via phala", gate.SEND])
    add(
        names[1],
        (
            ["Deploy each prod runner CVM via phala"]
            if branch == "main"
            else ["Deploy runner CVM via phala", gate.SEND]
        ),
    )
    if branch == "main":
        for cvm in ["runner-a", "runner-b"]:
            add(f"authorize prod runner ({cvm})", [gate.SEND])
    return {
        "root": root,
        "branch": branch,
        "source": source,
        "pin": pin,
        "jobs": jobs,
        "tmp": tmp_path,
    }


def run(release, prior=None, extra_runs=(), job_map=None, api_failure=False, attempt=1):
    r = release
    root, branch = r["root"], r["branch"]
    head = git(root, "rev-parse", "HEAD")
    current = dict(
        id=200,
        head_sha=head,
        head_branch=branch,
        event="push",
        path=".github/workflows/ci.yml",
        workflow_id=7,
        run_number=200,
        run_attempt=attempt,
        status="in_progress",
    )
    prior = (
        prior
        if prior is not None
        else [
            dict(
                id=100,
                head_sha=r["source"],
                head_branch=branch,
                event="push",
                workflow_id=7,
                run_number=100,
                run_attempt=1,
                status="completed",
            )
        ]
    )
    fixture = {
        "current": current,
        "runs": [current, *extra_runs, *prior],
        "jobs": r["jobs"],
        "api_failure": api_failure,
        "job_map": job_map or {},
    }
    fixture_path = r["tmp"] / "github.json"
    fixture_path.write_text(json.dumps(fixture))
    bindir = r["tmp"] / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text("""#!/usr/bin/env python3
import json,os,sys
f=json.load(open(os.environ['FAKE_GH_FIXTURE']))
assert sys.argv[1:4]==['api','--method','GET']
p=sys.argv[4]
if f['api_failure']:sys.exit(1)
if '/attempts/' in p:
 assert '/attempts/1/jobs?' in p
 rid=p.split('/runs/')[1].split('/')[0]
 jobs=f['job_map'].get(rid,f['jobs'])
 print(json.dumps({'total_count':len(jobs),'jobs':jobs}))
elif '/workflows/' in p:print(json.dumps({'workflow_runs':f['runs']}))
else:print(json.dumps(f['current']))
""")
    gh.chmod(0o755)
    key = {
        "main": "detect-cvm-changes",
        "test": "detect-cvm-changes-test",
        "pre": "detect-cvm-changes-pre",
    }[branch]
    job = WORKFLOW["jobs"][key]
    step = next(s for s in job["steps"] if s.get("id") == "filter")
    # This must be the actual checked-in action command, not a hand-written model.
    assert step["run"] == "python3 deploy/deployment_changes.py"
    write(root, "deploy/deployment_changes.py", SCRIPT.read_text())
    output = r["tmp"] / "outputs"
    output.unlink(missing_ok=True)
    env = {
        **os.environ,
        "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
        "FAKE_GH_FIXTURE": str(fixture_path),
        "GITHUB_REPOSITORY": "org/repo",
        "GITHUB_REF_NAME": branch,
        "GITHUB_SHA": head,
        "GITHUB_RUN_ID": "200",
        "GITHUB_OUTPUT": str(output),
        "DEPLOY_PATHS": step["env"]["DEPLOY_PATHS"],
        "GH_TOKEN": "synthetic-unused",
    }
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    outputs = (
        dict(line.split("=", 1) for line in output.read_text().splitlines())
        if output.exists()
        else {}
    )
    return result, outputs


def docs(release):
    write(release["root"], "docs/fix.md", "docs-only fix\n")
    return commit(release["root"], "docs only")


def test_proven_release_then_docs_skips_without_redeploying_pin(release):
    docs(release)
    result, out = run(release)
    assert result.returncode == 0, result.stderr
    assert out == {
        "cvm": "false",
        "reason": "compared-complete-release",
        "baseline": release["pin"],
    }


def test_failed_code_release_followed_by_docs_keeps_pending_change(release):
    write(release["root"], "backend/app.py", "not deployed\n")
    failed = commit(release["root"], "code with CI failure")
    docs(release)
    result, out = run(release)
    assert result.returncode == 0, result.stderr
    assert out["cvm"] == "true"
    assert failed != out["baseline"]


@pytest.mark.parametrize("failed_part", [0, 1])
def test_partial_release_never_becomes_baseline_even_if_code_reverted(
    release, failed_part
):
    docs(release)
    release["jobs"][failed_part]["conclusion"] = "failure"
    result, out = run(release)
    assert result.returncode == 0
    assert out["cvm"] == "true" and out["reason"] == "previous-release-incomplete"


def test_skipped_send_is_not_deployment_evidence(release):
    docs(release)
    release["jobs"][0]["steps"][1]["conclusion"] = "skipped"
    result, out = run(release)
    assert result.returncode == 0 and out["cvm"] == "true"


def test_no_history_is_explicit_conservative_deploy(release):
    docs(release)
    result, out = run(release, prior=[])
    assert result.returncode == 0 and out["cvm"] == "true"
    assert out["baseline"] == "unknown"


def test_unreadable_history_is_not_a_noop(release):
    docs(release)
    result, out = run(release, api_failure=True)
    assert result.returncode == 2 and not out
    assert "UNMEASURED" in result.stderr


def test_newer_candidate_supersedes_old_event(release):
    docs(release)
    newer = dict(
        id=201,
        head_branch=release["branch"],
        event="push",
        workflow_id=7,
        run_number=201,
        status="queued",
    )
    result, out = run(release, extra_runs=[newer])
    assert result.returncode == 2 and not out and "superseded" in result.stderr


def test_consumer_change_triggers_every_current_resident_environment(release):
    write(release["root"], "tools/chat_resident_consumer.py", "new consumer\n")
    commit(release["root"], "consumer only")
    result, out = run(release)
    assert result.returncode == 0 and out["cvm"] == "true"


def test_arbitrary_compose_edit_is_not_normalized_away(release):
    write(
        release["root"],
        gate.ENVIRONMENTS[release["branch"]][2],
        "image: ghcr.io/org/feedling:1111111\nother: changed\n",
    )
    commit(release["root"], "operator compose edit")
    result, out = run(release)
    assert result.returncode == 0 and out["cvm"] == "true"


def test_rerun_never_skips_a_previous_attempt(release):
    docs(release)
    result, out = run(release, attempt=2)
    assert result.returncode == 0 and out["cvm"] == "true"


def test_other_environment_compose_does_not_redeploy(release):
    other = "pre" if release["branch"] != "pre" else "test"
    write(release["root"], gate.ENVIRONMENTS[other][2], "other env\n")
    commit(release["root"], "other env only")
    result, out = run(release)
    assert result.returncode == 0 and out["cvm"] == "false"


def test_prod_requires_every_inventory_authorization(release):
    if release["branch"] != "main":
        pytest.skip("prod matrix only")
    docs(release)
    release["jobs"].pop()
    result, out = run(release)
    assert result.returncode == 0 and out["cvm"] == "true"


def test_workflow_bindings_and_existing_safety_gates():
    jobs = WORKFLOW["jobs"]
    for branch, key in [
        ("main", "detect-cvm-changes"),
        ("test", "detect-cvm-changes-test"),
        ("pre", "detect-cvm-changes-pre"),
    ]:
        j = jobs[key]
        assert (
            j["if"]
            == f"github.ref == 'refs/heads/{branch}' && github.event_name == 'push'"
        )
        assert j["permissions"] == {"actions": "read", "contents": "read"}
        assert j["steps"][0]["with"]["fetch-depth"] == 0
        assert j["outputs"]["cvm"] == "${{ steps.filter.outputs.cvm }}"
        assert "event.before" not in json.dumps(j)
        assert not any("dorny/" in s.get("uses", "") for s in j["steps"])
    assert (
        WORKFLOW["concurrency"]["cancel-in-progress"]
        == "${{ github.event_name == 'pull_request' }}"
    )
    for key in ["deploy-test-cvm", "deploy-test-runner-cvm"]:
        assert (
            jobs[key]["steps"][0]["if"] == "vars.TEST_COMPOSE_RECOVERY_HOLD == 'true'"
        )
    assert "pin-runtime-release.sh" in (ROOT / ".github/workflows/ci.yml").read_text()


def test_failed_validation_then_skipped_jobs_does_not_erase_pending_code(release):
    write(release["root"], "backend/app.py", "pending code\n")
    failed = commit(release["root"], "failed candidate")
    docs(release)
    skipped = [dict(j, conclusion="skipped", steps=[]) for j in release["jobs"]]
    candidate = dict(
        id=150,
        head_sha=failed,
        head_branch=release["branch"],
        event="push",
        workflow_id=7,
        run_number=150,
        run_attempt=1,
        status="completed",
    )
    result, out = run(release, extra_runs=[candidate], job_map={"150": skipped})
    assert result.returncode == 0 and out["cvm"] == "true"
    assert out["baseline"] == release["pin"]


def test_multiple_docs_candidates_keep_the_complete_release_baseline(release):
    earlier = docs(release)
    write(release["root"], "docs/another.md", "another doc\n")
    commit(release["root"], "next docs candidate")
    skipped = [dict(j, conclusion="skipped", steps=[]) for j in release["jobs"]]
    candidate = dict(
        id=150,
        head_sha=earlier,
        head_branch=release["branch"],
        event="push",
        workflow_id=7,
        run_number=150,
        run_attempt=1,
        status="completed",
    )
    result, out = run(release, extra_runs=[candidate], job_map={"150": skipped})
    assert (
        result.returncode == 0
        and out["cvm"] == "false"
        and out["baseline"] == release["pin"]
    )


def test_inflight_release_blocks_noop_claim(release):
    docs(release)
    prior = dict(
        id=150,
        head_sha=release["source"],
        head_branch=release["branch"],
        event="push",
        workflow_id=7,
        run_number=150,
        run_attempt=1,
        status="in_progress",
    )
    result, out = run(release, extra_runs=[prior])
    assert result.returncode == 2 and not out and "still in progress" in result.stderr


def test_forged_pin_with_unrelated_content_cannot_supply_baseline(release):
    root = release["root"]
    git(root, "checkout", "-q", "--detach", release["source"])
    for path, image in zip(
        gate.ENVIRONMENTS[release["branch"]][2:], ["feedling", "feedling-agent-runner"]
    ):
        write(
            root,
            path,
            f"image: ghcr.io/org/{image}:{release['source'][:7]}\nother: unchanged\n",
        )
    write(root, "backend/app.py", "smuggled into pin\n")
    commit(
        root,
        f"deploy({release['branch']}): pin Runtime V2 release :{release['source'][:7]} [skip ci]",
    )
    docs(release)
    result, out = run(release)
    assert (
        result.returncode == 0 and out["cvm"] == "true" and out["baseline"] == "unknown"
    )


def test_missing_deployment_job_is_unknown_not_skipped(release):
    docs(release)
    release["jobs"] = []
    result, out = run(release)
    assert (
        result.returncode == 0
        and out["cvm"] == "true"
        and out["reason"] == "previous-release-unknown"
    )


def test_complete_old_release_with_unrelated_ci_failure_remains_valid(release):
    # The gate intentionally never relies on overall workflow conclusion.
    docs(release)
    prior = dict(
        id=100,
        head_sha=release["source"],
        head_branch=release["branch"],
        event="push",
        workflow_id=7,
        run_number=100,
        run_attempt=1,
        status="completed",
        conclusion="failure",
    )
    result, out = run(release, prior=[prior])
    assert result.returncode == 0 and out["cvm"] == "false"


def test_publisher_activity_prevents_skipped_release_claim(release):
    if release["branch"] != "main":
        pytest.skip("prod matrix only")
    docs(release)
    for job in release["jobs"][:2]:
        job["conclusion"] = "skipped"
        job["steps"] = []
    result, out = run(release)
    assert result.returncode == 0 and out["cvm"] == "true"
    assert out["reason"] == "previous-release-incomplete"
