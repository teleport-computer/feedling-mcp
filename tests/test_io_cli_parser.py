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

IO_CLI = str(TOOLS / "io_cli.py")

# Subcommands that are wired to real handlers (set_defaults(func=...)). These
# must never also appear in the phase-2 stub list.
REAL_SUBCOMMANDS = {
    "schedule-wake",
    "cancel-wake",
    "photo-read",
    "photo-recent",
    "identity-write",
    "add-memory",
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
