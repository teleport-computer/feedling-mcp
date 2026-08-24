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
