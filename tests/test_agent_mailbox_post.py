"""Behavior tests for the durable agent-mailbox post script."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


POST_SCRIPT = (
    Path(__file__).parent.parent / "scripts" / "agent-mailbox" / "post.sh"
)

# Independent contract anchor: deriving these cases from post.sh would let a
# removed recipient silently remove its own regression case.
VALID_RECIPIENTS = (
    "claude",
    "claude2",
    "claude3",
    "claude4",
    "codex",
    "codex2",
    "codex3",
    "codex4",
    "claudeclaude",
    "codexcodex",
)


def _post(recipient: str, mailbox: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["AGENT_MAILBOX_DIR"] = str(mailbox)
    env.pop("TMUX", None)
    env.pop("AB_FORCE", None)
    return subprocess.run(
        [
            "bash",
            str(POST_SCRIPT),
            "--from",
            "codex4",
            "--to",
            recipient,
            "--type",
            "test",
            "--subject",
            "recipient validation",
            "--no-wake",
        ],
        input="test body\n",
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )


@pytest.mark.parametrize("recipient", VALID_RECIPIENTS)
def test_every_valid_recipient_is_delivered(recipient: str, tmp_path: Path) -> None:
    mailbox = tmp_path / recipient

    result = _post(recipient, mailbox)

    assert result.returncode == 0, result.stderr
    messages = list((mailbox / "messages").glob("*.md"))
    inbox = list((mailbox / "inbox" / recipient).glob("*.md"))
    outbox = list((mailbox / "outbox" / "codex4").glob("*.md"))
    assert len(messages) == len(inbox) == len(outbox) == 1
    assert messages[0].read_text() == inbox[0].read_text() == outbox[0].read_text()


@pytest.mark.parametrize("recipient", ("supervisor", "sup", "codex1"))
def test_invalid_recipient_is_rejected_before_any_mailbox_write(
    recipient: str, tmp_path: Path
) -> None:
    mailbox = tmp_path / recipient

    result = _post(recipient, mailbox)

    assert result.returncode != 0
    assert "REFUSED" in result.stderr
    stderr_tokens = result.stderr.split()
    for valid_recipient in VALID_RECIPIENTS:
        assert valid_recipient in stderr_tokens
    assert not mailbox.exists()


# The wake path had no coverage at all: every case above passes --no-wake, so
# both the "woke" false report and the silent set -e abort survived untested.
FAKE_TMUX = """#!/usr/bin/env bash
mode="${FAKE_TMUX_MODE:-ok}"
typed="$FAKE_TMUX_TYPED"
cmd="$1"
shift
case "$cmd" in
  send-keys)
    key="$3"
    if [ "$key" = "Enter" ]; then
      [ "$mode" = "enter_refused" ] && exit 1
    else
      [ "$mode" = "keys_refused" ] && exit 1
      # "pane_drops" models a TUI that swallows the keystrokes (a modal prompt
      # was up): tmux still reports success, but nothing lands in the pane.
      # "pane_shows_only_posted_line" is the same swallow, except the target
      # pane is the sender's own terminal, so post.sh's own `posted <id>` line
      # is on screen and any bare-id match would call that a delivery.
      case "$mode" in
        pane_drops) ;;
        pane_shows_only_posted_line)
          posted_id="${key#*: }"
          printf 'posted %s\\n' "${posted_id%%.*}" >> "$typed"
          ;;
        *) printf '%s\\n' "$key" >> "$typed" ;;
      esac
    fi
    exit 0
    ;;
  capture-pane)
    cat "$typed" 2>/dev/null
    exit 0
    ;;
esac
exit 0
"""


def _post_with_wake(
    tmp_path: Path, mode: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    (mailbox / "config.env").write_text("CLAUDE4_PANE='fake:0.0'\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_tmux = bin_dir / "tmux"
    fake_tmux.write_text(FAKE_TMUX)
    fake_tmux.chmod(0o755)

    env = os.environ.copy()
    env["AGENT_MAILBOX_DIR"] = str(mailbox)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["FAKE_TMUX_MODE"] = mode
    env["FAKE_TMUX_TYPED"] = str(tmp_path / "typed.txt")
    env.pop("TMUX", None)
    env.pop("AB_FORCE", None)

    result = subprocess.run(
        [
            "bash",
            str(POST_SCRIPT),
            "--from",
            "codex4",
            "--to",
            "claude4",
            "--type",
            "test",
            "--subject",
            "wake reporting",
        ],
        input="test body\n",
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    return result, mailbox


def _wake_lines(stream: str) -> list[str]:
    return [line for line in stream.splitlines() if "wake" in line]


def test_wake_never_claims_the_recipient_was_woken(tmp_path: Path) -> None:
    """send-keys returning 0 proves tmux took the keys, nothing more."""
    result, _ = _post_with_wake(tmp_path, "ok")

    assert result.returncode == 0, result.stderr
    assert "woke" not in result.stdout + result.stderr
    assert "reached claude4 at fake:0.0" in result.stdout
    assert "mailbox processing not verified" in result.stdout
    assert len(_wake_lines(result.stdout)) == 1
    assert _wake_lines(result.stderr) == []


def test_refused_enter_is_reported_instead_of_aborting_silently(
    tmp_path: Path,
) -> None:
    """The mirror of the case above: a failing Enter must not exit in silence."""
    result, mailbox = _post_with_wake(tmp_path, "enter_refused")

    # stdout specifically: a caller that captures only stdout saw nothing at all
    # when set -e aborted between the Enter and the outcome line.
    wake_lines = _wake_lines(result.stdout)
    assert len(wake_lines) == 1, result.stdout
    assert "Enter" in wake_lines[0]
    assert "wake failed" in wake_lines[0]
    assert len(_wake_lines(result.stderr)) == 1
    # A wake failure must not fail the post: the message is durably written, and
    # a non-zero exit would push callers into re-posting duplicates.
    assert result.returncode == 0
    assert len(list((mailbox / "messages").glob("*.md"))) == 1


def test_refused_notice_keys_are_reported_on_stdout_too(tmp_path: Path) -> None:
    result, _ = _post_with_wake(tmp_path, "keys_refused")

    assert result.returncode == 0
    assert len(_wake_lines(result.stdout)) == 1
    assert len(_wake_lines(result.stderr)) == 1
    assert "wake failed" in result.stdout
    assert "wake failed" in result.stderr


def test_notice_that_never_reaches_the_pane_is_reported_as_unverified(
    tmp_path: Path,
) -> None:
    result, _ = _post_with_wake(tmp_path, "pane_drops")

    assert result.returncode == 0
    assert len(_wake_lines(result.stdout)) == 1
    assert len(_wake_lines(result.stderr)) == 1
    assert "wake unverified" in result.stdout
    assert "wake unverified" in result.stderr
    assert "reached" not in result.stdout


def test_posted_line_in_the_target_pane_is_not_read_as_delivery(
    tmp_path: Path,
) -> None:
    """Posting to one's own pane puts `posted <id>` on screen; that is not the
    notice, and matching a bare id would call a swallowed wake a delivery."""
    result, _ = _post_with_wake(tmp_path, "pane_shows_only_posted_line")

    assert result.returncode == 0
    assert "wake unverified" in result.stdout
    assert "reached" not in result.stdout
