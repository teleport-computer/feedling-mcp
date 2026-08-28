"""Legacy Hosted Resident provider/relay smoke matrix.

This surface is intentionally separate from the release P0 matrix in
``tools.e2e.config`` until canonical E2E covers Resident route switching and
runner respawn.  Do not treat these model ids as the release truth-map.
"""

# provider -> static spec. `models` is a candidate list tried in order at setup.
PROVIDER_MATRIX: dict[str, dict] = {
    "anthropic":         {"env_var": "ANTHROPIC_API_KEY",  "models": ["claude-haiku-4-5"],          "base_url": ""},
    "openai":            {"env_var": "OPENAI_API_KEY",     "models": ["gpt-4o-mini"],               "base_url": ""},
    "deepseek":          {"env_var": "DEEPSEEK_API_KEY",   "models": ["deepseek-v4-flash"],         "base_url": ""},
    "gemini":            {"env_var": "GEMINI_API_KEY",     "models": ["gemini-2.5-flash", "gemini-2.5-pro"], "base_url": ""},
    "openrouter":        {"env_var": "OPENROUTER_API_KEY", "models": ["openai/gpt-4o-mini"],        "base_url": ""},
    # openai_compatible 用 KIMI 填；base_url/model 是最易随中转站变动的值，按需改这里。
    "openai_compatible": {"env_var": "KIMI_API_KEY",       "models": ["kimi-k2-0905-preview", "moonshot-v1-8k"], "base_url": "https://api.moonshot.ai/v1"},
}


def all_providers() -> list[str]:
    return list(PROVIDER_MATRIX.keys())


def load_matrix(env: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for provider, spec in PROVIDER_MATRIX.items():
        key = (env.get(spec["env_var"]) or "").strip()
        if not key:
            continue
        out[provider] = {
            "models": list(spec["models"]),
            "base_url": spec["base_url"],
            "api_key": key,
        }
    return out
