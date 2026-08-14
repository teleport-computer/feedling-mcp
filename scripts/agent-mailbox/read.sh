#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage:
  scripts/agent-mailbox/read.sh <agent> [message_id|latest|--all|--list]
USAGE
  exit 2
}

repo_root() {
  git rev-parse --show-toplevel 2>/dev/null || pwd
}

clean_token() {
  case "$1" in
    ""|*[!A-Za-z0-9_.:-]*) return 1 ;;
    *) printf '%s' "$1" ;;
  esac
}

[ "$#" -ge 1 ] || usage
agent="$(clean_token "$1")" || usage
selector="${2:---list}"

root="$(repo_root)"
mailbox="${AGENT_MAILBOX_DIR:-$root/.agents/mailbox}"

# 投递地址硬闸(2026-08-14 补收信侧):read/ack 同样依赖 cwd 定位 mailbox。
# 在 worktree 里跑 read.sh 会输出 "no inbox for X" 且**退出码 0** ——
# 看起来像「没有新信」,实际是查错了地方。这比发信侧更阴险:
# 发信侧至少写下了文件,读信侧直接让人误以为无事发生。
# (claude3 2026-08-14 复现并报告;它自己没出事只是因为执行环境每次都重置 cwd,
#  保护它的是环境不是纪律。)
_MAIN_TREE="/Users/xiaotingtan/Desktop/feedling-mcp-test"
case "$(cd "$root" && pwd -P)" in
  "$_MAIN_TREE") ;;
  *)
    echo "REFUSED: 你当前在 $root,不是主树。" >&2
    echo "  在这里读信箱会得到「no inbox」的假象(退出码还是 0),让你以为没有新信。" >&2
    echo "  正确做法:cd $_MAIN_TREE 再跑。" >&2
    exit 1 ;;
esac

inbox="$mailbox/inbox/$agent"

if [ ! -d "$inbox" ]; then
  echo "no inbox for $agent"
  exit 0
fi

latest_file() {
  find "$inbox" -maxdepth 1 -type f -name '*.md' | sort | tail -n 1
}

case "$selector" in
  --list)
    find "$inbox" -maxdepth 1 -type f -name '*.md' -print | sort | sed 's#^.*/##; s#\.md$##'
    ;;
  --all)
    found=0
    for file in $(find "$inbox" -maxdepth 1 -type f -name '*.md' -print | sort); do
      found=1
      printf '\n===== %s =====\n' "$(basename "$file" .md)"
      cat "$file"
    done
    [ "$found" -eq 1 ] || echo "no messages for $agent"
    ;;
  latest)
    file="$(latest_file)"
    if [ -z "$file" ]; then
      echo "no messages for $agent"
      exit 0
    fi
    cat "$file"
    ;;
  *)
    id="$(clean_token "$selector")" || usage
    file="$inbox/${id%.md}.md"
    if [ ! -f "$file" ]; then
      echo "message not found: $id" >&2
      exit 1
    fi
    cat "$file"
    ;;
esac
