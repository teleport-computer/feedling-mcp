#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage:
  scripts/agent-mailbox/post.sh --from codex --to claude --type review_request --subject "..." [--no-wake] < body.md

Writes a durable mailbox message under .agents/mailbox and, when configured,
wakes the recipient tmux pane with a fixed read command.
USAGE
  exit 2
}

repo_root() {
  git rev-parse --show-toplevel 2>/dev/null || pwd
}

clean_token() {
  case "$1" in
    ""|*[!A-Za-z0-9_-]*) return 1 ;;
    *) printf '%s' "$1" ;;
  esac
}

clean_line() {
  printf '%s' "$1" | tr '\r\n' '  '
}

yaml_quote() {
  clean_line "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/^/"/; s/$/"/'
}

from=""
to=""
msg_type="message"
subject=""
no_wake=0
ack_ids=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --from) [ "$#" -ge 2 ] || usage; from="$2"; shift 2 ;;
    --to) [ "$#" -ge 2 ] || usage; to="$2"; shift 2 ;;
    --type) [ "$#" -ge 2 ] || usage; msg_type="$2"; shift 2 ;;
    --subject) [ "$#" -ge 2 ] || usage; subject="$2"; shift 2 ;;
    --no-wake) no_wake=1; shift ;;
    --ack) [ "$#" -ge 2 ] || usage; ack_ids="${ack_ids} $2"; shift 2 ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done

from="$(clean_token "$from")" || usage
to="$(clean_token "$to")" || usage
msg_type="$(clean_token "$msg_type")" || usage
[ -n "$subject" ] || usage

# Identity guard (2026-07-22): --from must match the sender's own tmux session.
# Stale shared memory repeatedly made agents claim the wrong identity (claude vs
# claude2 incidents 07-19/21/22). The session name is ground truth. Deliberate
# on-behalf posts can bypass with AB_FORCE=1.
if [ -n "${TMUX:-}" ] && [ "${AB_FORCE:-0}" != "1" ]; then
  actual="$(tmux display-message -p '#S' 2>/dev/null || true)"
  if [ -n "$actual" ] && [ "$actual" != "$from" ]; then
    echo "REFUSED: --from '$from' but you are tmux session '$actual'." >&2
    echo "Run: bash ~/fleet/bus/whoami.sh   (old memories about pairing are invalid)" >&2
    echo "Posting on someone's behalf intentionally? Set AB_FORCE=1." >&2
    exit 1
  fi
fi

root="$(repo_root)"
mailbox="${AGENT_MAILBOX_DIR:-$root/.agents/mailbox}"

# 投递地址硬闸(2026-08-14):有 agent 的 cwd 落在生产 checkout 或 feature
# worktree 里,post.sh 就把信投进那棵树自己的 .agents/mailbox —— 写入成功、
# 无人读取、**静默丢件**。当晚在 feedling-mcp-main 里发现 29 封被吞的信,
# 其中 2 封是发给 Supervisor 的,它一直不知道。
# 这是最坏的一种失败:没有报错、没有信号。所以宁可拒发,不许静默。
_MAIN_TREE="/Users/xiaotingtan/Desktop/feedling-mcp-test"
case "$(cd "$root" && pwd -P)" in
  "$_MAIN_TREE") ;;
  *)
    echo "REFUSED: 你当前在 $root,不是主树。" >&2
    echo "  mailbox 是每棵树独立的目录,在这里发信会投进本树的信箱,收件人永远读不到。" >&2
    echo "  正确做法:cd $_MAIN_TREE 再发。" >&2
    echo "  (确实要投到别处?设 AGENT_MAILBOX_DIR 显式指定。)" >&2
    exit 1 ;;
esac
messages_dir="$mailbox/messages"
inbox_dir="$mailbox/inbox/$to"
outbox_dir="$mailbox/outbox/$from"
tmp_dir="$mailbox/tmp"
mkdir -p "$messages_dir" "$inbox_dir" "$outbox_dir" "$tmp_dir"

body_tmp="$tmp_dir/body.$$"
tmp_msg=""
trap 'rm -f "$body_tmp" "$tmp_msg"' EXIT
cat > "$body_tmp"

ts="$(date -u '+%Y%m%dT%H%M%SZ')"
iso_ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
nonce="$(uuidgen 2>/dev/null | tr 'A-F' 'a-f' | tr -d '-' | cut -c1-8 || true)"
if [ -z "$nonce" ]; then
  nonce="$$"
fi
id="${ts}_${from}_to_${to}_${nonce}"
file="$messages_dir/$id.md"
tmp_msg="$tmp_dir/$id.tmp"

{
  printf '%s\n' '---'
  printf 'id: %s\n' "$(yaml_quote "$id")"
  printf 'from: %s\n' "$(yaml_quote "$from")"
  printf 'to: %s\n' "$(yaml_quote "$to")"
  printf 'type: %s\n' "$(yaml_quote "$msg_type")"
  printf 'subject: %s\n' "$(yaml_quote "$subject")"
  printf 'created_at: %s\n' "$(yaml_quote "$iso_ts")"
  printf '%s\n\n' '---'
  cat "$body_tmp"
  printf '\n'
} > "$tmp_msg"

mv "$tmp_msg" "$file"
cp "$file" "$inbox_dir/$id.md"
cp "$file" "$outbox_dir/$id.md"

echo "posted $id"

# 自动 ack(2026-08-15):`--ack <收到的信id>` 在发信成功后顺手归档那几封。
# 由来:claude3 连续三次漏 ack,两次都发生在连续深度工作时 —— 注意力在任务上,
# 「回完顺手 ack」这个附加动作整批掉。它自己的结论:「靠自律的机制不是机制」。
# 所以把 ack 变成发信的一部分,而不是一个要记得做的额外步骤。
for _aid in $ack_ids; do
  [ -n "$_aid" ] || continue
  if "$(dirname "$0")/ack.sh" "$from" "$_aid" >/dev/null 2>&1; then
    echo "  acked $_aid"
  else
    echo "  ⚠️ ack 失败:$_aid(信已发出,只是没归档)" >&2
  fi
done

if [ "$no_wake" -eq 1 ]; then
  exit 0
fi

config="$mailbox/config.env"
if [ -f "$config" ]; then
  # shellcheck disable=SC1090
  . "$config"
fi

target_var="$(printf '%s_PANE' "$to" | tr '[:lower:]' '[:upper:]' | tr '-' '_')"
target_pane="${!target_var:-}"
if [ -z "$target_pane" ]; then
  echo "wake skipped: $target_var is not configured in $config" >&2
  exit 0
fi

if ! command -v tmux >/dev/null 2>&1; then
  echo "wake skipped: tmux not found" >&2
  exit 0
fi

notice="New mailbox message for $to: $id. Run scripts/agent-mailbox/read.sh $to $id"
# Submit reliably: type the notice, then send Enter as a SEPARATE key event
# after a short delay. Sending "$notice" Enter in one call often types the text
# but doesn't submit it in the Claude/Codex TUI (the Enter arrives as part of the
# same paste), leaving the read command stuck in the input box.
if tmux send-keys -t "$target_pane" "$notice"; then
  sleep 0.5
  tmux send-keys -t "$target_pane" Enter
  echo "woke $to at $target_pane"
else
  echo "wake failed for $to at $target_pane" >&2
fi
