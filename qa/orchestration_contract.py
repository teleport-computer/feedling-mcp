"""Locked profile-to-Codex-agent mapping for API-key qualification."""

from __future__ import annotations


PROFILE_AGENT_TYPES: tuple[tuple[str, str], ...] = (
    ("official-deepseek", "profile_official_deepseek"),
    ("official-anthropic", "profile_official_anthropic"),
    ("official-openai", "profile_official_openai"),
    ("official-gemini", "profile_official_gemini"),
    ("openrouter-claude", "profile_openrouter_claude"),
    ("openrouter-openai", "profile_openrouter_openai"),
    ("openrouter-glm", "profile_openrouter_glm"),
    ("openrouter-kimi", "profile_openrouter_kimi"),
    ("relay-kongbeiqie", "profile_relay_kongbeiqie"),
)

PROFILE_IDS = tuple(profile_id for profile_id, _ in PROFILE_AGENT_TYPES)
AGENT_TYPES = tuple(agent_type for _, agent_type in PROFILE_AGENT_TYPES)
PROFILE_TO_AGENT_TYPE = dict(PROFILE_AGENT_TYPES)
AGENT_TYPE_TO_PROFILE = {
    agent_type: profile_id for profile_id, agent_type in PROFILE_AGENT_TYPES
}

MEMORY_CONTRACT_PROFILE_ID = "memory-contract"
