"""T13 — hosted verbs increment + catalog rendering.

Pure-unit (no PG): backend/agent_runtime/spawners.py's _IO_CLI_VERBS allowlist
and the io_cli command catalog (tools/io_cli_catalog.py, T6's VPS mechanism)
rendered into the hosted agent prompt at ``<io_cli_catalog>``.
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

from agent_runtime import spawners  # noqa: E402

import io_cli_catalog  # noqa: E402  (sibling under tools/, same convention as spawners' lazy import)


NEW_VERBS = ("memory-write", "memory-patch", "memory-delete", "schedule-wake", "cancel-wake")


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


def test_rendered_catalog_never_documents_a_verb_outside_the_allowlist():
    # Codex review-style consistency check: the live --help sweep can surface
    # verbs _IO_CLI_VERBS never granted (e.g. identity-redistill has no [setup]/
    # [ops] tag to filter it) — the render must not leak them into the prompt.
    files = spawners.agent_home_files("/h", driver="claude", provider="anthropic")
    prompt = files["/h/agent-tools-prompt.md"]
    assert "identity-redistill" not in prompt
