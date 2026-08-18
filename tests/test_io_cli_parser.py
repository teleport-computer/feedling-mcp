"""Guards against argparse setup crashes in io_cli.

Regression context: schedule-wake was added as a real subparser but left in
PHASE2_VERBS (the "not implemented yet" stub loop), so `sub.add_parser` raised
`conflicting subparser: schedule-wake` at startup — crashing EVERY io_cli
invocation, which broke every OpenClaw native tool that shells out to io_cli.
"""
import json
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).parent.parent / "tools"
sys.path.insert(0, str(TOOLS))

import io_cli  # noqa: E402
from memory.source_policy import (  # noqa: E402
    MEMORY_SOURCE_VALUES,
    RESIDENT_ABSORB_SOURCE,
    RESIDENT_PATCH_SOURCE,
)

IO_CLI = str(TOOLS / "io_cli.py")

# Subcommands that are wired to real handlers (set_defaults(func=...)). These
# must never also appear in the phase-2 stub list.
REAL_SUBCOMMANDS = {
    "schedule-wake",
    "cancel-wake",
    "photo-read",
    "photo-recent",
    "identity-write",
    "memory-delete",
    "memory-patch",
    "memory-organize",
}


def test_phase2_verbs_do_not_collide_with_real_subcommands():
    assert not (set(io_cli.PHASE2_VERBS) & REAL_SUBCOMMANDS), (
        "a real subcommand is also listed in PHASE2_VERBS -> argparse "
        "'conflicting subparser' crash on every invocation"
    )


def _run(*argv):
    return subprocess.run(
        [sys.executable, IO_CLI, *argv],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},  # deliberately no FEEDLING_* -> clean error
    )


def test_schedule_wake_reaches_handler_not_argparse_crash():
    r = _run("schedule-wake", "--at", "2026-01-01T00:00", "--reason", "t")
    assert "conflicting subparser" not in r.stderr
    # reached the handler: it emits a JSON error about missing env, not a traceback
    payload = json.loads(r.stdout.strip().splitlines()[-1])
    assert payload.get("ok") is False


def test_photo_read_reaches_handler_not_argparse_crash():
    r = _run("photo-read", "--id", "abc")
    assert "conflicting subparser" not in r.stderr
    payload = json.loads(r.stdout.strip().splitlines()[-1])
    assert payload.get("ok") is False


def test_identity_write_agent_name_reaches_handler_not_argparse_crash():
    """--agent-name must be a real flag, not an 'unrecognized arguments' exit 2.

    Without it the agent has no way to rename itself, and argparse rejects the
    attempt on stderr — which the agent never surfaces to the user.
    """
    r = _run("identity-write", "--agent-name", "老6")
    assert "unrecognized arguments" not in r.stderr
    payload = json.loads(r.stdout.strip().splitlines()[-1])
    assert payload.get("ok") is False


def test_identity_read_reaches_handler_not_argparse_crash():
    r = _run("identity-read")
    assert "conflicting subparser" not in r.stderr
    payload = json.loads(r.stdout.strip().splitlines()[-1])
    assert payload.get("ok") is False


def test_memory_delete_reaches_handler_not_argparse_crash():
    r = _run("memory-delete", "--id", "abc")
    assert "conflicting subparser" not in r.stderr
    payload = json.loads(r.stdout.strip().splitlines()[-1])
    assert payload.get("ok") is False


def test_memory_patch_reaches_handler_not_argparse_crash():
    r = _run("memory-patch", "--id", "abc", "--summary", "corrected")
    assert "conflicting subparser" not in r.stderr
    payload = json.loads(r.stdout.strip().splitlines()[-1])
    assert payload.get("ok") is False


def test_memory_organize_reaches_handler_not_argparse_crash():
    r = _run("memory-organize")
    assert "conflicting subparser" not in r.stderr
    payload = json.loads(r.stdout.strip().splitlines()[-1])
    assert payload.get("ok") is False


def test_memory_patch_payload_builds_supersede_of_the_given_id():
    payload = io_cli._memory_patch_payload(
        memory_id="mem_1", summary="dog is actually a cat", content="",
        bucket=None, threads=[], importance=None, pulse=None,
        mem_type="fact", source="resident_patch", reason="user correction",
    )
    action = payload["actions"][0]
    assert action["type"] == "memory.supersede"
    assert action["supersedes"] == "mem_1"
    assert action["memory"]["summary"] == "dog is actually a cat"
    assert action["reason"] == "user correction"


def test_memory_write_and_patch_defaults_share_server_source_policy():
    write_payload = io_cli._memory_write_payload(
        summary="a durable fact", content="", bucket=None, threads=[],
        importance=None, pulse=None, mem_type="fact", source=None,
    )
    patch_payload = io_cli._memory_patch_payload(
        memory_id="mem_1", summary="a correction", content="",
        bucket=None, threads=[], importance=None, pulse=None,
        mem_type="fact", source=None, reason=None,
    )

    assert write_payload["actions"][0]["memory"]["source"] == RESIDENT_ABSORB_SOURCE
    assert patch_payload["actions"][0]["memory"]["source"] == RESIDENT_PATCH_SOURCE
    assert RESIDENT_ABSORB_SOURCE in MEMORY_SOURCE_VALUES
    assert RESIDENT_PATCH_SOURCE in MEMORY_SOURCE_VALUES


def test_memory_source_rejects_arbitrary_cli_and_direct_builder_values():
    result = _run(
        "memory-write",
        "--summary", "a durable fact",
        "--source", "conversation_2026",
    )
    assert result.returncode == 2
    assert "invalid choice" in result.stderr

    try:
        io_cli._memory_write_payload(
            summary="a durable fact", content="", bucket=None, threads=[],
            importance=None, pulse=None, mem_type="fact",
            source="conversation_2026",
        )
    except ValueError as exc:
        assert "not allowed" in str(exc)
    else:
        raise AssertionError("direct payload builder accepted an arbitrary source")


def test_memory_patch_payload_is_none_without_id_or_content():
    # no id -> nothing to patch
    assert io_cli._memory_patch_payload(
        memory_id="", summary="x", content="", bucket=None, threads=[],
        importance=None, pulse=None, mem_type="fact", source="s", reason="r",
    ) is None
    # id but no new content -> nothing to write
    assert io_cli._memory_patch_payload(
        memory_id="mem_1", summary="", content="", bucket=None, threads=[],
        importance=None, pulse=None, mem_type="fact", source="s", reason="r",
    ) is None
