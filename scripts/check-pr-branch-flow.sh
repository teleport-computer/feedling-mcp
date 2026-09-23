#!/usr/bin/env bash
# PR 分支流向闸。
#
# 常规路线：功能进 test，验过后 test/pre → main 上 prod。
# 目的是「上 prod 的代码必须先在 staging 跑过」。
#
# 2026-08-24 新增 hotfix 通道。为什么需要：一次线上事故（中文花园被判成英文，
# 新落的卡整个变语言）的修复只有 100 行，但常规路线要求它跟着 test 上攒的
# 210 个提交、或 pre 上的 86 个一起走 —— 为一个小修复挟带上万行他人代码，
# 是把风险放大了几个数量级，还要替那些作者签字。
#
# ⚠️ 但 hotfix 通道有一条硬约束：**必须从 main 拉，不许挟带**。
#
# 这条不是形式主义，是踩出来的：那次修复第一版就是从 main 拉的干净分支，
# 后来为了走 pre 路线把 origin/pre 合了进去 —— 分支瞬间从 100 行变成 12806 行、
# 123 个文件。如果那时直接合了，"只上一个 hotfix" 就成了一句空话，而 diff 大到
# 没人会逐行看。祖先检查要求基于最新 main；提交数上限限制额外历史的规模。
# 这不能证明少量提交的来源或内容，仍需人工审阅。
set -euo pipefail

base_branch="${1:-}"
head_branch="${2:-}"
head_sha="${3:-}"

if [[ -z "$base_branch" || -z "$head_branch" ]]; then
  echo "::error title=Invalid PR branch flow::base and head branch names are required" >&2
  exit 2
fi

if [[ "$base_branch" != "main" ]]; then
  echo "Branch flow allowed: $head_branch -> $base_branch"
  exit 0
fi

# main 的常规来源
if [[ "$head_branch" == "test" || "$head_branch" == "pre" ]]; then
  echo "Branch flow allowed: $head_branch -> main"
  exit 0
fi

# hotfix 通道
if [[ "$head_branch" == hotfix/* ]]; then
  # HEAD is the trusted base checkout in pull_request_target, NOT the PR head.
  unable() {
    echo "::error title=Unable to validate hotfix::$1" >&2
    exit 2
  }
  [[ "$head_sha" =~ ^[0-9a-f]{40}$ ]] || unable "An explicit full PR head SHA is required"
  main_sha="$(git rev-parse --verify 'refs/remotes/origin/main^{commit}' 2>/dev/null)" || \
    unable "Missing origin/main commit object"
  git cat-file -e "${head_sha}^{commit}" 2>/dev/null || unable "Missing PR head commit object"
  shallow="$(git rev-parse --is-shallow-repository 2>/dev/null)" || unable "Cannot inspect repository history"
  [[ "$shallow" == "false" ]] || unable "Complete history required; repository is shallow"
  max="${HOTFIX_MAX_COMMITS:-10}"
  [[ "$max" =~ ^[0-9]{1,9}$ ]] || unable "HOTFIX_MAX_COMMITS must be a non-negative integer (at most 9 digits)"
  max=$((10#$max))

  # Latest main must be an ancestor of the actual PR commit.
  if git merge-base --is-ancestor "$main_sha" "$head_sha"; then
    :
  else
    rc=$?
    [[ "$rc" -eq 1 ]] || unable "Cannot determine PR ancestry (git exit $rc)"
    echo "::error title=Hotfix must be rebased on main::'$head_branch' 落后于 origin/main。\
先 rebase 到 main 再提 PR。" >&2
    exit 1
  fi
  extra="$(git rev-list --count "$main_sha..$head_sha")" || unable "Cannot count PR commits"
  if (( extra > max )); then
    echo "::error title=Hotfix carries too much::'${head_branch}' 相对 main 有 ${extra} 个提交\
（上限 ${max}）。hotfix 通道只用于少量提交的紧急修复。\
走常规的 test/pre 路线，或把分支重建干净。" >&2
    exit 1
  fi
  echo "Branch flow allowed: $head_branch -> main (hotfix, $extra 个提交)"
  exit 0
fi

echo "::error title=Invalid PR branch flow::main only accepts pull requests from test, pre, \
or hotfix/*; got '$head_branch'" >&2
exit 1
