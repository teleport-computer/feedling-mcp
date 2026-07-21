"""io_cli identity-write payload builder (pure).

The hosted agent uses `io_cli.py identity-write` in post-respawn (7.D) to write its
own self_introduction / signature via /v1/identity/actions (identity.profile_patch).
The server does the crypto (decrypt existing → merge → re-encrypt), so the CLI just
shapes the action body.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import io_cli  # noqa: E402


def test_identity_write_payload_self_introduction_and_signature():
    p = io_cli._identity_write_payload("I keep you honest.", ["always direct", "never coddles"])
    assert p == {"action": {"type": "identity.profile_patch", "patch": {
        "self_introduction": "I keep you honest.",
        "signature": ["always direct", "never coddles"],
    }}}


def test_identity_write_payload_partial_fields():
    assert io_cli._identity_write_payload("hi", [])["action"]["patch"] == {"self_introduction": "hi"}
    assert io_cli._identity_write_payload(None, ["sig"])["action"]["patch"] == {"signature": ["sig"]}


def test_identity_write_payload_empty_is_none():
    assert io_cli._identity_write_payload(None, []) is None


def test_identity_write_payload_agent_name():
    """Renaming is the whole point of --agent-name.

    Regression: the agent had no way to express a rename. Asked "改个名字叫老6"
    it could only rewrite self_introduction ("我是老6…"), so the card kept the old
    agent_name while the agent cheerfully reported success — a silent no-op the
    user only catches by looking at the identity card.
    """
    p = io_cli._identity_write_payload(None, [], "老6")
    assert p == {"action": {"type": "identity.profile_patch", "patch": {"agent_name": "老6"}}}


def test_identity_write_payload_agent_name_with_other_fields():
    patch = io_cli._identity_write_payload("我是老6，老Z的专属伙伴", ["随时候着"], "老6")["action"]["patch"]
    assert patch == {
        "agent_name": "老6",
        "self_introduction": "我是老6，老Z的专属伙伴",
        "signature": ["随时候着"],
    }


def test_identity_write_payload_agent_name_omitted_is_unchanged():
    """Not passing --agent-name must leave the patch byte-identical to before."""
    assert io_cli._identity_write_payload("hi", [], None)["action"]["patch"] == {"self_introduction": "hi"}


def test_identity_init_payload_fresh_start_and_sanitize():
    from io_cli import _identity_init_payload
    body = _identity_init_payload(
        agent_name="阿锐", self_introduction="hi",
        dimensions=[{"name": "锐利", "value": 150, "description": "x"}],
        days_with_user=None, anchor=None, fresh_start=True)
    assert body["days_with_user"] == 0
    assert len(body["relationship_anchor_evidence"]) >= 8  # fresh-start 标准证据
    assert body["identity"]["dimensions"][0]["value"] == 100  # sanitize 夹过
