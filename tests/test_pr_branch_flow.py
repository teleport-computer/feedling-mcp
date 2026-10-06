"""PR 分支流向闸：hotfix 通道存在，但不许挟带。

## 为什么加 hotfix 通道（2026-08-24）

一次线上事故的修复只有 100 行，但原来的闸要求它跟着 test 上攒的 210 个提交、
或 pre 上的 86 个一起走 —— 为一个小修复挟带上万行他人代码，风险放大几个数量级，
还得替那些作者签字。

## 为什么必须同时加「不许挟带」的约束

**这条是当场踩出来的。** 那次修复第一版就是从 main 拉的干净分支（100 行），
后来为了改走 pre 路线，把 origin/pre 合了进去 —— 分支瞬间变成 12806 行、123 个
文件，而分支名还叫 hotfix/*。如果那时直接合了，"只上一个 hotfix" 就是一句空话，
而 diff 大到没人会逐行看。

所以通道和约束必须同时存在：只开通道不设约束，等于给「用 hotfix 的名义放行整条
线」开了一扇门。
"""
from __future__ import annotations

import pathlib
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "check-pr-branch-flow.sh"


def _run(base: str, head: str, extra_env: dict | None = None):
    import os
    env = {**os.environ, **(extra_env or {})}
    return subprocess.run(["bash", str(SCRIPT), base, head],
                          capture_output=True, text=True, env=env,
                          cwd=str(SCRIPT.parent.parent))


@pytest.mark.parametrize("head", ["test", "pre"])
def test_the_normal_lanes_still_pass(head):
    assert _run("main", head).returncode == 0


@pytest.mark.parametrize("head", ["feat/whatever", "fix/something", "codex/x"])
def test_ordinary_branches_still_cannot_reach_main(head):
    r = _run("main", head)
    assert r.returncode == 1
    assert "test, pre, or hotfix/*" in r.stderr


def test_non_main_bases_are_unrestricted():
    assert _run("test", "feat/anything").returncode == 0
    assert _run("pre", "hotfix/anything").returncode == 0


def git(repo, *args, input=None):
    return subprocess.run(
        ["git", *args], cwd=repo, input=input, capture_output=True, text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def history(tmp_path):
    """CI shape: HEAD is trusted main; PR commits are objects, never checked out."""
    repo = tmp_path / "origin"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Branch flow test")
    git(repo, "config", "user.email", "branch-flow@example.invalid")
    scripts = repo / "scripts"
    scripts.mkdir()
    guard = scripts / SCRIPT.name
    guard.write_bytes(SCRIPT.read_bytes())
    guard.chmod(0o755)
    git(repo, "add", ".")
    tree = git(repo, "write-tree")

    def commit(parent=None, *, tree_id=tree):
        args = ["commit-tree", tree_id]
        if parent:
            args += ["-p", parent]
        return git(repo, *args, input="fixture commit\n")

    root = commit()
    main = commit(root)
    git(repo, "update-ref", "refs/heads/main", main)
    git(repo, "update-ref", "refs/remotes/origin/main", main)
    # The untrusted head contains a replacement guard that would falsely pass
    # and create a marker. Tests execute the YAML steps and check it never runs.
    guard.write_text("#!/bin/sh\ntouch PR_CODE_EXECUTED\nexit 0\n")
    git(repo, "add", ".")
    hostile_tree = git(repo, "write-tree")
    clean = commit(main, tree_id=hostile_tree)
    stale = commit(root, tree_id=hostile_tree)
    excessive = clean
    for _ in range(10):
        excessive = commit(excessive, tree_id=hostile_tree)
    git(repo, "update-ref", "refs/pull/9/head", clean)
    git(repo, "read-tree", "--reset", "-u", main)
    assert git(repo, "rev-parse", "HEAD") == main
    return repo, main, clean, stale, excessive


def run_hotfix(history, sha, **env):
    import os
    repo = history[0]
    return subprocess.run(
        ["bash", str(SCRIPT), "main", "hotfix/example", sha], cwd=repo,
        env={**os.environ, **env}, capture_output=True, text=True,
    )


def test_clean_hotfix_passes_while_head_is_base(history):
    result = run_hotfix(history, history[2])
    assert result.returncode == 0, result.stderr
    assert "1 个提交" in result.stdout
    assert git(history[0], "rev-parse", "HEAD") == history[1]


def test_stale_hotfix_rejected_even_though_checkout_is_current_main(history):
    result = run_hotfix(history, history[3])
    assert result.returncode == 1
    assert "rebased on main" in result.stderr


def test_excessive_hotfix_is_counted_from_pr_head(history):
    result = run_hotfix(history, history[4])
    assert result.returncode == 1
    assert "11 个提交" in result.stderr
    assert "carries too much" in result.stderr


def test_configured_limit_is_enforced(history):
    result = run_hotfix(history, history[2], HOTFIX_MAX_COMMITS="0")
    assert result.returncode == 1
    assert "carries too much" in result.stderr


@pytest.mark.parametrize("missing", ["main", "head", "argument"])
def test_missing_objects_are_not_reported_as_stale(history, missing):
    sha = history[2]
    if missing == "main":
        git(history[0], "update-ref", "-d", "refs/remotes/origin/main")
    elif missing == "head":
        sha = "f" * 40
    else:
        sha = ""
    result = run_hotfix(history, sha)
    assert result.returncode == 2
    assert "Unable to validate hotfix" in result.stderr
    assert "rebased on main" not in result.stderr
    assert "carries too much" not in result.stderr


@pytest.mark.parametrize("value", ["HEAD", "--all", "abc", "$(touch INJECTED)"])
def test_head_must_be_a_full_object_id(history, value):
    result = run_hotfix(history, value)
    assert result.returncode == 2
    assert "explicit full PR head SHA" in result.stderr


def test_shallow_history_cannot_claim_stale_or_success(history, tmp_path):
    shallow = tmp_path / "shallow"
    git(tmp_path, "clone", "--depth=1", "--branch=main", history[0].as_uri(), str(shallow))
    assert git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    result = run_hotfix((shallow,), history[1])
    assert result.returncode == 2
    assert "repository is shallow" in result.stderr


@pytest.mark.parametrize("value", ["-1", "x", "1+1", "9999999999"])
def test_invalid_limit_is_an_inability_to_validate(history, value):
    result = run_hotfix(history, history[2], HOTFIX_MAX_COMMITS=value)
    assert result.returncode == 2
    assert "Unable to validate hotfix" in result.stderr


def test_workflow_fetches_fork_head_object_without_executing_it(history, tmp_path):
    import os
    import yaml

    workflow = yaml.load(
        (SCRIPT.parents[1] / ".github/workflows/branch-flow.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    steps = workflow["jobs"]["branch-flow"]["steps"]
    checkout = steps[0]
    assert checkout["with"]["fetch-depth"] == "0"
    runner = tmp_path / "runner"
    # Only main is advertised as a branch; head is reachable via refs/pull/9.
    # This models fork commits served by the base repository's PR refs.
    git(tmp_path, "clone", "--single-branch", "--branch=main", history[0].as_uri(), str(runner))
    git(runner, "checkout", "--detach", history[1])
    before = subprocess.run(["git", "cat-file", "-e", history[2]], cwd=runner, capture_output=True)
    assert before.returncode != 0
    git(runner, "update-ref", "-d", "refs/remotes/origin/main")
    env = {**os.environ, "BASE_BRANCH": "main", "HEAD_BRANCH": "hotfix/example",
           "HEAD_SHA": history[2], "GH_TOKEN": "local-fixture-unused"}
    for step in steps[1:]:
        result = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
            cwd=runner, env=env, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    assert "1 个提交" in result.stdout
    assert git(runner, "rev-parse", "HEAD") == history[1]
    assert not (runner / "PR_CODE_EXECUTED").exists()
    assert (runner / "scripts" / SCRIPT.name).read_bytes() == SCRIPT.read_bytes()
    assert "local-fixture-unused" not in (runner / ".git/config").read_text()


def test_workflow_fetch_failure_is_explicit(history, tmp_path):
    import os
    import yaml

    workflow = yaml.load(
        (SCRIPT.parents[1] / ".github/workflows/branch-flow.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    step = next(s for s in workflow["jobs"]["branch-flow"]["steps"]
                if s.get("name") == "Fetch hotfix history")
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]], cwd=history[0],
        env={**os.environ, "HEAD_SHA": history[2], "GH_TOKEN": "unused"},
        capture_output=True, text=True,
    )
    # No origin remote is configured here. The fetch must fail closed explicitly.
    assert result.returncode == 2
    assert "Unable to validate hotfix" in result.stderr
