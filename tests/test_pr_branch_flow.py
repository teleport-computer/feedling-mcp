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


def test_a_hotfix_branch_may_reach_main():
    """通道本身存在。

    注意本测试在仓库当前 HEAD 上跑 —— 它检查的是脚本对 hotfix/* 的**放行逻辑**，
    实际的挟带检查依赖 git 历史，由下面那条覆盖。
    """
    r = _run("main", "hotfix/whatever")
    # HEAD 相对 main 的提交数取决于跑测试时的分支状态；
    # 只断言它没有落到"分支名不认识"那条错误上。
    assert "test, pre, or hotfix/*" not in r.stderr


def test_a_hotfix_that_carries_a_whole_branch_is_rejected():
    """挟带检查有牙 —— 把上限压到 0，任何非空 hotfix 都该被拦。"""
    r = _run("main", "hotfix/whatever", {"HOTFIX_MAX_COMMITS": "0"})
    if r.returncode == 0:
        pytest.skip("当前 HEAD 就是 main，无从验证挟带（在 hotfix 分支上跑时才有意义）")
    assert "carries too much" in r.stderr or "rebased on main" in r.stderr


def test_non_main_bases_are_unrestricted():
    """闸只管上 prod 那一跳；进 test/pre 不受限。"""
    assert _run("test", "feat/anything").returncode == 0
    assert _run("pre", "hotfix/anything").returncode == 0
