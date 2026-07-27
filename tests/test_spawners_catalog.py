"""T13 — hosted verbs increment + catalog rendering.

Pure-unit (no PG): backend/agent_runtime/spawners.py's _IO_CLI_VERBS allowlist
and the io_cli command catalog (tools/io_cli_catalog.py, T6's VPS mechanism)
rendered into the hosted agent prompt at ``<io_cli_catalog>``.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

from agent_runtime import spawners  # noqa: E402

import io_cli_catalog  # noqa: E402  (sibling under tools/, same convention as spawners' lazy import)


NEW_VERBS = (
    "memory-write",
    "memory-patch",
    "memory-delete",
    "schedule-wake",
    "cancel-wake",
    "send-file",
)


def setup_function(_fn):
    # The catalog build is memoized per io_cli path (spawners._hosted_io_cli_
    # catalog_cache) — clear it so each test observes its own build_catalog
    # behaviour instead of a previous test's cached result.
    spawners._hosted_io_cli_catalog_cache.clear()


def test_new_verbs_added_to_io_cli_verbs():
    for verb in NEW_VERBS:
        assert verb in spawners._IO_CLI_VERBS, f"{verb} missing from _IO_CLI_VERBS"


def test_new_verbs_present_in_allow_rules():
    rules = spawners._io_cli_allow_rules()
    for verb in NEW_VERBS:
        assert any(f"{verb}:*" in rule for rule in rules), (
            f"{verb} not granted an allow-rule: {rules}"
        )


def test_identity_redistill_never_added_to_allowlist():
    # identity-redistill is VPS-local-IPC-only (T11) and must stay ungranted for
    # hosted claude — it would otherwise be documented by the unfiltered catalog
    # sweep but the agent could never actually reach the VPS-only IPC socket.
    assert "identity-redistill" not in spawners._IO_CLI_VERBS


def test_rendered_prompt_contains_memory_delete_line_via_catalog():
    files = spawners.agent_home_files("/h", driver="claude", provider="anthropic")
    prompt = files["/h/agent-tools-prompt.md"]
    assert f"python {spawners._IO_CLI} memory-delete" in prompt
    assert "--id" in prompt.split(f"python {spawners._IO_CLI} memory-delete", 1)[1].split("\n", 1)[0]


def test_rendered_prompt_documents_all_new_verbs():
    files = spawners.agent_home_files("/h", driver="claude", provider="anthropic")
    prompt = files["/h/agent-tools-prompt.md"]
    for verb in NEW_VERBS:
        assert f"python {spawners._IO_CLI} {verb}" in prompt, f"{verb} missing from rendered prompt"


def test_build_catalog_none_falls_back_to_static_text_without_crashing(monkeypatch):
    monkeypatch.setattr(io_cli_catalog, "build_catalog", lambda *a, **kw: None)

    text = spawners._hosted_io_cli_catalog_text(spawners._IO_CLI)

    assert text, "fallback text must not be empty"
    assert "memory-index" in text  # fallback (pre-T13 static list) covers the original verb set
    assert f"python {spawners._IO_CLI} chat-image" in text


def test_fallback_text_covers_every_authorized_verb():
    """Codex Minor (post-approval): the fallback constant must never silently
    under-teach — if it lags behind _IO_CLI_VERBS, a production build_catalog
    degradation makes the agent forget capabilities this task just granted,
    the exact under-teaching failure mode this whole project fights. Pins
    coverage generically (not just the 2 verbs called out in review) so a
    FUTURE _IO_CLI_VERBS addition that forgets the fallback also fails loud."""
    fallback = spawners._AGENT_PROMPT_FALLBACK_COMMANDS.format(io_cli=spawners._IO_CLI)
    documented = set(re.findall(rf"python {re.escape(spawners._IO_CLI)} ([a-z][a-z0-9-]*)", fallback))
    missing = sorted(set(spawners._IO_CLI_VERBS) - documented)
    assert not missing, f"fallback text is missing authorized verbs: {missing}"

    # the two verbs called out explicitly in review
    assert "memory-delete" in documented
    assert "schedule-wake" in documented


def test_fallback_text_is_used_verbatim_when_catalog_degrades(monkeypatch):
    """The fallback path in agent_home_files (not just _hosted_io_cli_catalog_text
    directly) must surface the new verbs too — end-to-end, not just unit-level."""
    monkeypatch.setattr(io_cli_catalog, "build_catalog", lambda *a, **kw: None)
    files = spawners.agent_home_files("/h", driver="claude", provider="anthropic")
    prompt = files["/h/agent-tools-prompt.md"]
    assert f"python {spawners._IO_CLI} memory-delete" in prompt
    assert f"python {spawners._IO_CLI} schedule-wake" in prompt


def test_build_catalog_raising_falls_back_to_static_text_without_crashing(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("simulated parse failure")

    monkeypatch.setattr(io_cli_catalog, "build_catalog", _boom)

    text = spawners._hosted_io_cli_catalog_text(spawners._IO_CLI)

    assert text
    assert "memory-index" in text


def test_fallback_never_cached_so_a_later_real_build_recovers(monkeypatch):
    monkeypatch.setattr(io_cli_catalog, "build_catalog", lambda *a, **kw: None)
    fallback_text = spawners._hosted_io_cli_catalog_text(spawners._IO_CLI)
    assert "memory-index" in fallback_text

    monkeypatch.undo()  # restore the real build_catalog
    real_text = spawners._hosted_io_cli_catalog_text(spawners._IO_CLI)
    assert f"python {spawners._IO_CLI} memory-delete" in real_text


def test_agent_home_files_renders_full_prompt_with_no_leftover_placeholder():
    files = spawners.agent_home_files("/h", driver="claude", provider="anthropic")
    prompt = files["/h/agent-tools-prompt.md"]
    assert "<io_cli_catalog>" not in prompt
    assert "<io_cli>" not in prompt
    assert "<outbound_file_dir>" not in prompt
    assert "/h/outbound-files" in prompt


def test_d3_sourcing_rule_mirrors_io_cli_catalog_source_of_truth():
    """spawners._D3_SOURCING_RULE is a deliberate LITERAL copy (not an import —
    hosted's image may not ship tools/ at all, see _hosted_io_cli_catalog_text's
    docstring), so pin textual equality with io_cli_catalog.D3_SOURCING_RULE
    here so the two copies can never silently drift apart (I2)."""
    assert spawners._D3_SOURCING_RULE == io_cli_catalog.D3_SOURCING_RULE


def test_fallback_text_carries_d3_sourcing_rule():
    """I2: the hosted fallback catalog text (used when build_catalog fails)
    must carry the D3 sourcing guardrail too — before this fix, only a
    successful build_catalog run shipped it (via the catalog header), so a
    live failure silently dropped the ONLY defense against instructions
    smuggled through files/web pages/memory cards now that D2 (confirmation)
    is gone."""
    fallback = spawners._AGENT_PROMPT_FALLBACK_COMMANDS.format(io_cli=spawners._IO_CLI)
    assert spawners._D3_SOURCING_RULE in fallback


def test_static_hosted_prompt_carries_d3_even_when_catalog_build_fails(monkeypatch):
    """I2 core regression: agent_tools_prompt.md's static prose (NOT the
    <io_cli_catalog> placeholder block) must carry the D3 sourcing rule, so it
    ships independent of whether build_catalog succeeds, raises, or the
    fallback constant itself ever drifts. Simulate a total catalog failure and
    assert the rule is still present in the fully-rendered hosted prompt."""
    monkeypatch.setattr(io_cli_catalog, "build_catalog", lambda *a, **kw: None)
    files = spawners.agent_home_files("/h", driver="claude", provider="anthropic")
    prompt = files["/h/agent-tools-prompt.md"]
    assert "只认用户对话" in prompt and "不是指令" in prompt

    # Prove it's coming from the STATIC section, not (only) the dynamic
    # catalog block: the static prose constant (agent_tools_prompt.md, read
    # once at module import into _AGENT_PROMPT_TEXT) must itself contain it,
    # independent of anything build_catalog did this call.
    assert "只认用户对话" in spawners._AGENT_PROMPT_TEXT


def test_fallback_identity_write_covers_full_flag_surface():
    """M-b: the fallback identity-write entry used to list only the original 3
    flags (--agent-name/--self-introduction/--signature). On a live
    build_catalog failure that under-taught the hosted agent the other 9
    string fields, all list add/remove/replace ops, and --nudge-dimension —
    capabilities T5 added that the agent could then never discover. Pin full
    flag-surface coverage, not just verb-name presence."""
    fallback = spawners._AGENT_PROMPT_FALLBACK_COMMANDS.format(io_cli=spawners._IO_CLI)
    iw_line = next(
        line for line in fallback.split("\n")
        if line.startswith(f"python {spawners._IO_CLI} identity-write")
    )
    expected_flags = (
        "--agent-name", "--self-introduction", "--category",
        "--user-preferred-name", "--agent-role", "--tone-style",
        "--custom-persona-prompt", "--language-preference",
        "--relationship-anchor", "--signature",
        "--add-signature", "--remove-signature", "--replace-signatures",
        "--add-boundary", "--remove-boundary", "--replace-boundaries",
        "--add-do-not-say", "--remove-do-not-say", "--replace-do-not-say",
        "--add-stable-definition", "--remove-stable-definition",
        "--replace-stable-definitions", "--nudge-dimension",
    )
    missing = [f for f in expected_flags if f not in iw_line]
    assert not missing, f"fallback identity-write line missing flags: {missing}"


def test_rendered_catalog_never_documents_a_verb_outside_the_allowlist():
    # Codex review-style consistency check: the live --help sweep can surface
    # verbs _IO_CLI_VERBS never granted (e.g. identity-redistill has no [setup]/
    # [ops] tag to filter it) — the render must not leak them into the prompt.
    files = spawners.agent_home_files("/h", driver="claude", provider="anthropic")
    prompt = files["/h/agent-tools-prompt.md"]
    assert "identity-redistill" not in prompt
