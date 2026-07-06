"""io_cli add-memory: payload builder + poll helper (pure, no network)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import io_cli  # noqa: E402


def test_add_memory_payload_memory_mode():
    p = io_cli._add_memory_payload("I drink oat milk.", "diet.md", "memory", "vps-add-memory-1")
    assert p["mode"] == "add_memory"
    assert p["format"] == "auto"
    assert p["content"] == ""
    assert p["fresh_start"] is False
    assert p["client_job_id"] == "vps-add-memory-1"
    assert p["memory_summary_content"] == "I drink oat milk."
    assert p["memory_summary_filename"] == "diet.md"
    # identity-only keys must be absent in memory mode
    assert "ai_persona_content" not in p
    assert "character_content" not in p


def test_add_memory_payload_identity_mode():
    p = io_cli._add_memory_payload("Be blunter, use lowercase.", "persona.md", "identity", "vps-update-identity-1")
    assert p["mode"] == "update_identity"
    assert p["ai_persona_content"] == "Be blunter, use lowercase."
    assert p["character_content"] == "Be blunter, use lowercase."
    assert p["ai_persona_filename"] == "persona.md"
    assert p["character_filename"] == "persona.md"
    # memory-only key must be absent in identity mode
    assert "memory_summary_content" not in p


def test_add_memory_payload_no_filename_omits_filename_keys():
    p = io_cli._add_memory_payload("some text", "", "memory", "vps-add-memory-2")
    assert "memory_summary_filename" not in p
    assert p["memory_summary_content"] == "some text"
