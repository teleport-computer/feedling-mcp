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

# Keep the address contract in one production source. config.env happens to
# contain pane variables for the same agents, but it is local state and is not
# available in every checkout or CI environment.
VALID_RECIPIENTS=(
  claude claude2 claude3 claude4
  codex codex2 codex3 codex4
  claudeclaude codexcodex
)
readonly VALID_RECIPIENTS

valid_recipient() {
  local candidate="$1" recipient
  for recipient in "${VALID_RECIPIENTS[@]}"; do
    [ "$candidate" = "$recipient" ] && return 0
  done
  return 1
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

# Reject unknown destinations before deriving or creating any mailbox path.
# A typo must not produce a durable message or a poisoned inbox directory.
if ! valid_recipient "$to"; then
  printf "REFUSED: invalid --to recipient '%s'. Valid recipients:" "$to" >&2
  printf ' %s' "${VALID_RECIPIENTS[@]}" >&2
  printf '\n' >&2
  exit 1
fi

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
# 显式 AGENT_MAILBOX_DIR 是调用者选择的投递根,也供隔离测试使用。
_MAIN_TREE="/Users/xiaotingtan/workspace/io/feedling-mcp-test"
if [ -z "${AGENT_MAILBOX_DIR:-}" ]; then
  case "$(cd "$root" && pwd -P)" in
    "$_MAIN_TREE") ;;
    *)
      echo "REFUSED: 你当前在 $root,不是主树。" >&2
      echo "  mailbox 是每棵树独立的目录,在这里发信会投进本树的信箱,收件人永远读不到。" >&2
      echo "  正确做法:cd $_MAIN_TREE 再发。" >&2
      echo "  (确实要投到别处?设 AGENT_MAILBOX_DIR 显式指定。)" >&2
      exit 1 ;;
  esac
fi
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

notice_head="New mailbox message for $to: $id"
notice="$notice_head. Run scripts/agent-mailbox/read.sh $to $id"

# 唤醒道的两个病(2026-08-27 Seven 点名修;T274 在 bus post.sh 修的是同一族):
# ① 假报:`tmux send-keys` 返回 0 只说明 tmux 把按键交给了 pane,不说明 TUI 接住了,
#    更不说明对方读了信。旧文案「woke $to」把这三件事说成一件。
# ② 静默退出:旧写法把第二次 send-keys(Enter)裸放在 then 块里,而本脚本是 set -e。
#    它一失败脚本当场退出:woke 没打印、wake failed 也没打印 —— 信已落盘却零信号,
#    正是本文件 08-14 那条注释说的「最坏的一种失败」。写盘那端堵住了,唤醒这端还开着。
# 于是:每次 tmux 调用都判返回值;打完字回读 pane 确认按键真的落进去了;
# 任何一条路径都要留下一行输出,且失败也写 stdout —— 调用方常常只捕获 stdout。
wake_failed() {
  echo "$1"
  echo "$1" >&2
}

if ! tmux send-keys -t "$target_pane" "$notice"; then
  wake_failed "wake failed for $to at $target_pane: tmux refused the notice keys (信已落盘:$id,请人工通知)"
  exit 0
fi

# Enter 必须是独立的一次按键事件:和正文一起发常常只把字打进输入框、不提交,
# 指令滞留在对方输入框里等下一次回车才引爆。
sleep 0.5
if ! tmux send-keys -t "$target_pane" Enter; then
  wake_failed "wake failed for $to at $target_pane: notice typed but Enter was refused (信已落盘:$id,对方输入框里可能滞留一条未提交的指令)"
  exit 0
fi

# 回读证明按键确实落进了那个窗口。锚点必须是通知的整个抬头,**不能是裸 id**:
# 上面那行 `posted $id` 就打在发信人自己的终端上,一旦目标 pane 就是发信人这一格
# (自己发给自己),裸 id 会被这行喂中,通知被 TUI 吞掉也照报「已送达」——
# 假阳性,codex4 用 fake tmux 实弹打出来的。
# -J 拼接 tmux 层折行。收信端 TUI 有自己的软折行,长抬头在窄 pane 里可能被它折断
# 而漏判;这个方向是安全的(多报一次「未确认」),反过来不是。
pane_text="$(tmux capture-pane -p -J -t "$target_pane" 2>/dev/null || true)"
case "$pane_text" in
  *"$notice_head"*)
    echo "wake notice reached $to at $target_pane; mailbox processing not verified"
    ;;
  *)
    wake_failed "⚠️ wake unverified for $to at $target_pane: send-keys succeeded but the notice never appeared in that pane (信已落盘:$id,请人工核对对方是否收到)"
    ;;
esac
