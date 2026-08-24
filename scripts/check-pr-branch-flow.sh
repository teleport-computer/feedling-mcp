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
# 没人会逐行看。所以这里用祖先关系机械地卡住：hotfix 分支的历史里不许出现
# main 之外的东西。
set -euo pipefail

base_branch="${1:-}"
head_branch="${2:-}"

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
  # 必须从 main 拉：main 的头必须是这个分支的祖先，且分支不含 main 之外的历史。
  # 前者保证"基于最新 main"，后者保证"没合进别的线"。
  if ! git merge-base --is-ancestor "origin/main" "HEAD" 2>/dev/null; then
    echo "::error title=Hotfix must be rebased on main::'$head_branch' 落后于 origin/main。\
先 rebase 到 main 再提 PR。" >&2
    exit 1
  fi
  # 分支相对 main 的提交数。hotfix 就该是少量提交；挟带整条线会立刻超标。
  extra="$(git rev-list --count origin/main..HEAD 2>/dev/null || echo 999)"
  max="${HOTFIX_MAX_COMMITS:-10}"
  if (( extra > max )); then
    echo "::error title=Hotfix carries too much::'${head_branch}' 相对 main 有 ${extra} 个提交\
（上限 ${max}）。hotfix 通道只用于自成一体的紧急修复 —— 这个数字说明它合进了别的分支，\
那样等于用 hotfix 的名义放行整条线。走常规的 test/pre 路线，或把分支重建干净。" >&2
    exit 1
  fi
  echo "Branch flow allowed: $head_branch -> main (hotfix, $extra 个提交)"
  exit 0
fi

echo "::error title=Invalid PR branch flow::main only accepts pull requests from test, pre, \
or hotfix/*; got '$head_branch'" >&2
exit 1
