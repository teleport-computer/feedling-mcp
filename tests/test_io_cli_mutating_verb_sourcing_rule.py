"""I2 (final whole-branch review) — every MUTATING io_cli verb's --help must
carry the D3 sourcing rule (io_cli_catalog.D3_SOURCING_RULE), not just the
dynamic catalog header.

Why per-verb, not just the catalog header: the catalog header (built by
io_cli_catalog.build_catalog) is the model's FIRST line of defense, but it
disappears entirely if catalog generation fails this turn (see
test_consumer_capability_inject.py / test_spawners_catalog.py for that
failure-path coverage). A model that falls back to running `<verb> --help`
directly — which io_cli's own agent_tools_prompt.md explicitly recommends via
the D8 soft-guidance line ("写操作前建议先跑对应命令 --help 看使用规则") — must
still see the sourcing guardrail there. Before this fix, identity-write's
epilog carried a rule mislabeled "D3 来源规则" that was actually about
whole-card-overwrite scope (patch vs identity.replace), and every other
mutating verb had no sourcing rule in --help at all.

Pure subprocess invocation of io_cli.py --help — no network, no DB.
"""
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).parent.parent / "tools"
sys.path.insert(0, str(TOOLS))

import io_cli_catalog  # noqa: E402

IO_CLI = str(TOOLS / "io_cli.py")

# Every mutating verb (writes/patches/deletes state) — the ones D2
# (confirmation) used to gate before it was removed from the design.
MUTATING_VERBS = (
    "identity-write",
    "identity-init",
    "identity-redistill",
    "memory-write",
    "memory-patch",
    "memory-delete",
    "send-file",
    "send-image",
)


def _help_text(verb):
    r = subprocess.run(
        [sys.executable, IO_CLI, verb, "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0, f"{verb} --help exited {r.returncode}: {r.stderr}"
    return r.stdout


def test_every_mutating_verb_help_contains_the_d3_sourcing_rule():
    missing = []
    for verb in MUTATING_VERBS:
        help_text = _help_text(verb)
        if io_cli_catalog.D3_SOURCING_RULE not in help_text:
            missing.append(verb)
    assert not missing, f"--help missing D3 sourcing rule for: {missing}"


def test_identity_write_epilog_no_longer_mislabels_the_scope_rule_as_d3():
    """Regression pin: identity-write's epilog used to label the
    whole-card-overwrite SCOPE rule (patch vs identity.replace) as "D3 来源规则"
    — conflating it with the actual sourcing guardrail. The scope rule must
    still be documented, but not under the D3 label; D3 must point at the real
    sourcing sentence."""
    help_text = _help_text("identity-write")
    assert io_cli_catalog.D3_SOURCING_RULE in help_text
    # The scope rule (整卡覆盖 reserved for the distill lane) must still be
    # present, just not mislabeled as D3.
    assert "整卡覆盖" in help_text
    # The line immediately introducing D3 must be the real sourcing sentence,
    # not the scope rule — i.e. D3's own line contains the sourcing text.
    d3_lines = [l for l in help_text.split("\n") if "D3" in l]
    assert d3_lines, "no line labeled D3 in identity-write --help"
    assert any(io_cli_catalog.D3_SOURCING_RULE in l for l in d3_lines)
