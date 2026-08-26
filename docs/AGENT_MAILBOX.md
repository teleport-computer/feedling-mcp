# Agent Mailbox

Codex and Claude Code can coordinate through a local repo mailbox plus tmux
wakeups.

The mailbox has two separate responsibilities:

- `.agents/mailbox/` is the durable local message store. It is ignored by git.
- `tmux send-keys` is only a wake transport. It injects a fixed read command
  into the recipient pane; message bodies are never injected into a terminal.

## Setup

Run both agents in tmux panes, then record pane targets:

```sh
tmux list-panes -a -F '#S:#I.#P #{pane_current_command}'
scripts/agent-mailbox/setup.sh --codex-pane <target> --claude-pane <target>
```

If an agent is running the setup command from its own pane, it can register
itself:

```sh
scripts/agent-mailbox/setup.sh --self codex
scripts/agent-mailbox/setup.sh --self claude
```

The config is written to `.agents/mailbox/config.env` and is intentionally not
committed.

## Send

```sh
scripts/agent-mailbox/post.sh \
  --from codex \
  --to claude \
  --type review_request \
  --subject "PR1 DB substrate ready" <<'EOF'
Please audit PR1.

Focus:
- lease CAS
- stale turn recovery
- no proactive_jobs state leakage
EOF
```

`post.sh` writes the message under:

- `.agents/mailbox/messages/<id>.md`
- `.agents/mailbox/inbox/<recipient>/<id>.md`
- `.agents/mailbox/outbox/<sender>/<id>.md`

Then it wakes the recipient pane with:

```txt
New mailbox message for <recipient>: <id>. Run scripts/agent-mailbox/read.sh <recipient> <id>
```

If no pane is configured, the message is still written and the wake is skipped.

The wake outcome is always one line on stdout, mirrored to stderr when it is a
failure, and it never claims more than was observed. `wake notice reached
<recipient> at <pane>` means the notice was found in that pane afterwards — it
does not mean the recipient read it. `wake failed` and `⚠️ wake unverified` both
mean the message is on disk but the recipient probably never saw the notice:
tell them yourself. A failed wake still exits 0, because the post itself
succeeded and re-running it would only duplicate the message.

## Read And Ack

```sh
scripts/agent-mailbox/read.sh claude --list
scripts/agent-mailbox/read.sh claude latest
scripts/agent-mailbox/read.sh claude <message_id>
scripts/agent-mailbox/ack.sh claude <message_id>
```

Ack moves the recipient inbox copy to `.agents/mailbox/archive/<agent>/`.
The canonical copy remains in `.agents/mailbox/messages/`.

## Message Rules

Use short, actionable messages:

- `review_request`: ask for audit.
- `review_result`: report pass/fail and blockers.
- `pushback`: challenge a design or implementation choice.
- `decision`: record a resolved engineering decision.
- `status`: summarize progress.

Formal decisions still need to land in docs, PR notes, or commits. The mailbox
is a local coordination channel, not a replacement for repo history.

## Safety Rules

- Never commit `.agents/mailbox/`.
- Do not paste long message bodies through `tmux send-keys`.
- Treat mailbox contents as local scratch; do not put secrets in messages.
- If an agent is busy, wakeup may be delayed, but the message is already on disk.
