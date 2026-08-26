"""Release pinning is one compare-and-swap unit for both Runtime V2 images."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).parent.parent
PIN_SCRIPT = ROOT / "deploy" / "pin-runtime-release.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
MAIN_COMPOSE = "deploy/docker-compose.phala.pre.yaml"
RUNNER_COMPOSE = "deploy/docker-compose.phala.pre.runner.yaml"


def _run(
    *args: str | Path,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        env=env,
        check=check,
        text=True,
        capture_output=True,
    )


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = _run("git", *args, cwd=repo, check=check)
    return result.stdout.strip()


@dataclass(frozen=True)
class ReleaseRepo:
    origin: Path
    checkout: Path
    trigger_sha: str


def _make_release_repo(tmp_path: Path, *, branch: str = "pre") -> ReleaseRepo:
    origin = tmp_path / "origin.git"
    checkout = tmp_path / "release"
    _run("git", "init", "--bare", origin, cwd=tmp_path)
    _run("git", "init", "-b", branch, checkout, cwd=tmp_path)
    _git(checkout, "config", "user.name", "test-author")
    _git(checkout, "config", "user.email", "test@example.com")

    deploy = checkout / "deploy"
    deploy.mkdir()
    (checkout / MAIN_COMPOSE).write_text(
        "services:\n"
        "  backend:\n"
        "    image: ghcr.io/old-owner/feedling:deadbee\n"
        "    environment:\n"
        "      PLAINTEXT_SHADOW_DATABASE_URL: \"${PLAINTEXT_SHADOW_DATABASE_URL:-}\"\n"
        "      FEEDLING_PLAINTEXT_SHADOW_ENABLED: \"${FEEDLING_PLAINTEXT_SHADOW_ENABLED:-0}\"\n"
        "      FEEDLING_PLAINTEXT_SHADOW_INFRA_EVIDENCE_PUBLIC_KEY: \"${FEEDLING_PLAINTEXT_SHADOW_INFRA_EVIDENCE_PUBLIC_KEY:-}\"\n"
        "  cpu-recorder:\n"
        "    image: ghcr.io/old-owner/feedling:deadbee\n"
        "  cpu-socket-proxy:\n"
        "    image: ghcr.io/wollomatic/socket-proxy:1.13.0@sha256:proxy-pin\n"
    )
    (checkout / RUNNER_COMPOSE).write_text(
        "services:\n"
        "  serve-worker:\n"
        "    image: ghcr.io/old-owner/feedling-agent-runner:cafe123\n"
        "    environment:\n"
        "      FEEDLING_PLAINTEXT_SHADOW_ENABLED: \"0\"\n"
    )
    _git(checkout, "add", MAIN_COMPOSE, RUNNER_COMPOSE)
    _git(checkout, "commit", "-m", "trigger release")
    _git(checkout, "remote", "add", "origin", str(origin))
    _git(checkout, "push", "-u", "origin", branch)
    return ReleaseRepo(origin, checkout, _git(checkout, "rev-parse", "HEAD"))


def _pin(
    repo: Path,
    trigger_sha: str,
    output: Path,
    *,
    branch: str = "pre",
    preview: bool = False,
    check: bool = True,
):
    env = {
        **os.environ,
        "GITHUB_SHA": trigger_sha,
        "GITHUB_REPOSITORY_OWNER": "Teleport-Computer",
        "GITHUB_OUTPUT": str(output),
    }
    if preview:
        env.update(
            FEEDLING_TEST_PREVIEW_DEPLOY="1",
            GITHUB_EVENT_NAME="workflow_dispatch",
            GITHUB_REF="refs/heads/codex/file-preview",
        )
    return _run(
        "bash",
        PIN_SCRIPT,
        branch,
        MAIN_COMPOSE,
        RUNNER_COMPOSE,
        cwd=repo,
        env=env,
        check=check,
    )


def _clone_at_trigger(release: ReleaseRepo, destination: Path) -> Path:
    _run("git", "clone", release.origin, destination, cwd=destination.parent)
    _git(destination, "checkout", "--detach", release.trigger_sha)
    return destination


def test_release_pin_commits_and_pushes_both_images_as_one_unit(tmp_path: Path):
    release = _make_release_repo(tmp_path)
    output = tmp_path / "github-output"

    _pin(release.checkout, release.trigger_sha, output)

    pinned_sha = _git(release.checkout, "rev-parse", "HEAD")
    assert pinned_sha != release.trigger_sha
    assert _git(release.checkout, "rev-list", "--count", "pre") == "2"
    assert _git(release.checkout, "diff", "--name-only", "HEAD^") == (
        f"{RUNNER_COMPOSE}\n{MAIN_COMPOSE}"
    )
    assert _git(release.checkout, "log", "-1", "--format=%s") == (
        "deploy(pre): pin Runtime V2 release "
        f":{release.trigger_sha[:7]} [skip ci]"
    )
    assert (
        f"ghcr.io/teleport-computer/feedling:{release.trigger_sha[:7]}"
        in (release.checkout / MAIN_COMPOSE).read_text()
    )
    main_source = (release.checkout / MAIN_COMPOSE).read_text()
    expected_feedling = (
        f"ghcr.io/teleport-computer/feedling:{release.trigger_sha[:7]}"
    )
    assert main_source.count(expected_feedling) == 2
    assert "ghcr.io/wollomatic/socket-proxy:1.13.0@sha256:proxy-pin" in main_source
    assert (
        "ghcr.io/teleport-computer/feedling-agent-runner:"
        f"{release.trigger_sha[:7]}"
        in (release.checkout / RUNNER_COMPOSE).read_text()
    )
    assert output.read_text().splitlines() == [f"pinned_sha={pinned_sha}"]
    assert _git(release.checkout, "ls-remote", "origin", "refs/heads/pre").split()[
        0
    ] == pinned_sha


def test_same_workflow_rerun_reuses_the_exact_release_pin(tmp_path: Path):
    release = _make_release_repo(tmp_path)
    first_output = tmp_path / "first-output"
    _pin(release.checkout, release.trigger_sha, first_output)
    first_pin = _git(release.checkout, "rev-parse", "HEAD")

    retry = _clone_at_trigger(release, tmp_path / "retry")
    second_output = tmp_path / "second-output"
    result = _pin(retry, release.trigger_sha, second_output)

    assert f"reusing release pin {first_pin}" in result.stdout
    assert _git(retry, "rev-parse", "HEAD") == first_pin
    assert second_output.read_text().splitlines() == [f"pinned_sha={first_pin}"]
    assert _git(retry, "rev-list", "--count", "origin/pre") == "2"
    assert _git(retry, "ls-remote", "origin", "refs/heads/pre").split()[0] == (
        first_pin
    )


def test_unrelated_branch_advance_fails_closed_without_changing_origin(
    tmp_path: Path,
):
    release = _make_release_repo(tmp_path)
    stale_workflow = _clone_at_trigger(release, tmp_path / "stale-workflow")
    advancer = _clone_at_trigger(release, tmp_path / "advancer")
    _git(advancer, "switch", "pre")
    _git(advancer, "config", "user.name", "other-author")
    _git(advancer, "config", "user.email", "other@example.com")
    (advancer / "README.md").write_text("newer release\n")
    _git(advancer, "add", "README.md")
    _git(advancer, "commit", "-m", "unrelated branch advance")
    _git(advancer, "push", "origin", "pre")
    advanced_sha = _git(advancer, "rev-parse", "HEAD")
    origin_before = _git(
        stale_workflow, "ls-remote", "origin", "refs/heads/pre"
    )

    result = _pin(
        stale_workflow,
        release.trigger_sha,
        tmp_path / "failed-output",
        check=False,
    )

    assert result.returncode == 1
    assert (
        "::error title=deployment-superseded::pre advanced from trigger "
        f"{release.trigger_sha} to {advanced_sha}; "
        "the newer workflow owns deployment"
    ) in result.stdout
    assert _git(stale_workflow, "rev-parse", "HEAD") == release.trigger_sha
    assert _git(stale_workflow, "status", "--porcelain") == ""
    assert _git(stale_workflow, "ls-remote", "origin", "refs/heads/pre") == (
        origin_before
    )
    assert origin_before.split()[0] == advanced_sha
    assert not (tmp_path / "failed-output").exists()


def test_true_release_contract_failure_is_not_marked_as_superseded(
    tmp_path: Path,
):
    release = _make_release_repo(tmp_path)
    runner = release.checkout / RUNNER_COMPOSE
    runner.write_text(
        runner.read_text().replace(
            'FEEDLING_PLAINTEXT_SHADOW_ENABLED: "0"',
            'FEEDLING_PLAINTEXT_SHADOW_ENABLED: "1"',
        )
    )

    result = _pin(
        release.checkout,
        release.trigger_sha,
        tmp_path / "failed-output",
        check=False,
    )

    assert result.returncode == 1
    assert "must keep the plaintext-shadow drain disabled" in result.stdout
    assert "title=deployment-superseded" not in result.stdout
    assert not (tmp_path / "failed-output").exists()


def test_test_preview_pins_locally_without_advancing_test_branch(
    tmp_path: Path,
):
    release = _make_release_repo(tmp_path, branch="test")
    origin_before = _git(
        release.checkout, "ls-remote", "origin", "refs/heads/test"
    )
    _git(release.checkout, "switch", "-c", "codex/file-preview")
    (release.checkout / "README.md").write_text("preview\n")
    _git(release.checkout, "add", "README.md")
    _git(release.checkout, "commit", "-m", "preview release")
    preview_sha = _git(release.checkout, "rev-parse", "HEAD")
    output = tmp_path / "preview-output"

    result = _pin(
        release.checkout,
        preview_sha,
        output,
        branch="test",
        preview=True,
    )

    assert "test preview images pinned locally" in result.stdout
    assert _git(release.checkout, "rev-parse", "HEAD") == preview_sha
    assert _git(
        release.checkout, "ls-remote", "origin", "refs/heads/test"
    ) == origin_before
    assert output.read_text().splitlines() == [f"pinned_sha={preview_sha}"]
    assert (
        f"ghcr.io/teleport-computer/feedling:{preview_sha[:7]}"
        in (release.checkout / MAIN_COMPOSE).read_text()
    )
    assert (
        "ghcr.io/teleport-computer/feedling-agent-runner:"
        f"{preview_sha[:7]}"
        in (release.checkout / RUNNER_COMPOSE).read_text()
    )


def test_test_preview_fails_closed_outside_codex_dispatch(tmp_path: Path):
    release = _make_release_repo(tmp_path, branch="test")
    output = tmp_path / "failed-preview-output"
    env = {
        **os.environ,
        "GITHUB_SHA": release.trigger_sha,
        "GITHUB_REPOSITORY_OWNER": "Teleport-Computer",
        "GITHUB_OUTPUT": str(output),
        "FEEDLING_TEST_PREVIEW_DEPLOY": "1",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": "refs/heads/test",
    }

    result = _run(
        "bash",
        PIN_SCRIPT,
        "test",
        MAIN_COMPOSE,
        RUNNER_COMPOSE,
        cwd=release.checkout,
        env=env,
        check=False,
    )

    assert result.returncode == 1
    assert "requires workflow_dispatch from a codex/* branch" in result.stdout
    assert not output.exists()


def _job(source: str, name: str, next_name: str) -> str:
    return source.split(f"\n  {name}:\n", 1)[1].split(
        f"\n  {next_name}:\n", 1
    )[0]


def test_workflow_consumes_one_pinned_release_without_runner_branch_writes():
    source = WORKFLOW.read_text()
    assert source.count("./deploy/pin-runtime-release.sh") == 6
    assert "git reset --hard origin/" not in source

    main_jobs = (
        ("deploy-cvm", "deploy-test-cvm", "main"),
        ("deploy-test-cvm", "deploy-test-runner-cvm", "test"),
        ("deploy-pre-cvm", "deploy-pre-runner-cvm", "pre"),
    )
    for name, next_name, branch in main_jobs:
        job = _job(source, name, next_name)
        assert f"./deploy/pin-runtime-release.sh {branch}" in job

    runner_jobs = (
        (
            "deploy-test-runner-cvm",
            "deploy-pre-cvm",
            "test",
            "deploy/docker-compose.phala.test.yaml",
            "deploy/docker-compose.phala.runner.yaml",
        ),
        (
            "deploy-pre-runner-cvm",
            "deploy-prod-runner-cvm",
            "pre",
            "deploy/docker-compose.phala.pre.yaml",
            "deploy/docker-compose.phala.pre.runner.yaml",
        ),
        (
            "deploy-prod-runner-cvm",
            "notify-lark-prod-deploy",
            "main",
            "deploy/docker-compose.phala.yaml",
            "deploy/docker-compose.phala.prod.runner.yaml",
        ),
    )
    for name, next_name, branch, main_compose, runner_compose in runner_jobs:
        job = _job(source, name, next_name)
        permissions = job.split("\n    concurrency:", 1)[0]
        assert "contents: read" in permissions
        assert "contents: write" not in permissions
        assert "ref: ${{ needs." not in job
        assert "git push" not in job
        assert f"./deploy/pin-runtime-release.sh {branch}" in job
        assert main_compose in job
        assert runner_compose in job
