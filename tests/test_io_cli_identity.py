"""io_cli identity-write / identity-init payload builders (pure).

The hosted agent uses `io_cli.py identity-write` in post-respawn (7.D) to write its
own self_introduction / signature / etc via /v1/identity/actions (identity.profile_patch).
The server does the crypto (decrypt existing → merge → re-encrypt), so the CLI just
shapes the action body.

`_identity_write_payload` (single-action `{"action": {...}}` shape, no local
pre-checks) was superseded by `_identity_write_payload_v2` (spec 3.1 full field
set: 9 string fields + 4 list fields' add/remove/replace + 七维 nudge, `{"actions":
[...]}` shape, D4 改名成对 pre-check) — see test_io_cli_identity_write_full.py for
that surface's dedicated coverage. These tests keep asserting the same underlying
behaviors through the new builder so this file still reads as "does identity-write
basics work", not just "does the full-field-set expansion work".
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import io_cli  # noqa: E402


def _ns(**overrides) -> argparse.Namespace:
    base = dict(
        agent_name=None, self_introduction=None, category=None,
        user_preferred_name=None, agent_role=None, tone_style=None,
        custom_persona_prompt=None, language_preference=None,
        relationship_anchor=None,
        signature=[], add_signature=[], remove_signature=[], replace_signatures=[],
        add_boundary=[], remove_boundary=[], replace_boundaries=[],
        add_do_not_say=[], remove_do_not_say=[], replace_do_not_say=[],
        add_stable_definition=[], remove_stable_definition=[], replace_stable_definitions=[],
        nudge_dimension=[],
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_identity_write_payload_self_introduction_and_signature():
    p = io_cli._identity_write_payload_v2(_ns(
        self_introduction="I keep you honest.",
        signature=["always direct", "never coddles"],
    ))
    assert p == {"actions": [{"type": "identity.profile_patch", "patch": {
        "self_introduction": "I keep you honest.",
        "signature": ["always direct", "never coddles"],
    }}]}


def test_identity_write_payload_partial_fields():
    assert io_cli._identity_write_payload_v2(
        _ns(self_introduction="hi"))["actions"][0]["patch"] == {"self_introduction": "hi"}
    assert io_cli._identity_write_payload_v2(
        _ns(signature=["sig"]))["actions"][0]["patch"] == {"signature": ["sig"]}


def test_identity_write_payload_empty_is_none():
    assert io_cli._identity_write_payload_v2(_ns()) is None


def test_identity_write_payload_agent_name():
    """Renaming is the whole point of --agent-name.

    Regression: the agent had no way to express a rename. Asked "改个名字叫老6"
    it could only rewrite self_introduction ("我是老6…"), so the card kept the old
    agent_name while the agent cheerfully reported success — a silent no-op the
    user only catches by looking at the identity card.

    D4 改名成对 (added alongside the full-field-set expansion): a rename must
    carry self_introduction in the SAME call, so agent_name-only now raises a
    local pre-check instead of silently building a name-only patch — see
    test_io_cli_identity_write_full.py::test_rename_without_intro_* for that
    behavior's dedicated coverage. Here we assert the paired form still works.
    """
    p = io_cli._identity_write_payload_v2(_ns(agent_name="老6", self_introduction="我是老6"))
    assert p == {"actions": [{"type": "identity.profile_patch", "patch": {
        "agent_name": "老6", "self_introduction": "我是老6",
    }}]}


def test_identity_write_payload_agent_name_with_other_fields():
    patch = io_cli._identity_write_payload_v2(_ns(
        self_introduction="我是老6，老Z的专属伙伴",
        signature=["随时候着"],
        agent_name="老6",
    ))["actions"][0]["patch"]
    assert patch == {
        "agent_name": "老6",
        "self_introduction": "我是老6，老Z的专属伙伴",
        "signature": ["随时候着"],
    }


def test_identity_write_payload_agent_name_omitted_is_unchanged():
    """Not passing --agent-name must leave the patch byte-identical to before."""
    assert io_cli._identity_write_payload_v2(
        _ns(self_introduction="hi"))["actions"][0]["patch"] == {"self_introduction": "hi"}


def test_identity_init_payload_fresh_start_and_sanitize():
    from io_cli import _identity_init_payload
    body = _identity_init_payload(
        agent_name="阿锐", self_introduction="hi",
        dimensions=[{"name": "锐利", "value": 150, "description": "x"}],
        days_with_user=None, anchor=None, fresh_start=True)
    assert body["days_with_user"] == 0
    assert len(body["relationship_anchor_evidence"]) >= 8  # fresh-start 标准证据
    assert body["identity"]["dimensions"][0]["value"] == 100  # sanitize 夹过
