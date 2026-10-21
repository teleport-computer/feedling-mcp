import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client as pc


def test_openai_usage_passthrough():
    raw = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert pc._normalize_usage("openai", raw) == {
        "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
        "cache_read_tokens": None, "cache_write_tokens": None,
        "cache_miss_tokens": None}


def test_anthropic_usage_mapped():
    raw = {"input_tokens": 12, "output_tokens": 8}
    assert pc._normalize_usage("anthropic", raw) == {
        "prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20,
        "cache_read_tokens": None, "cache_write_tokens": None,
        "cache_miss_tokens": None}


def test_gemini_usage_mapped():
    raw = {"promptTokenCount": 30, "candidatesTokenCount": 9, "totalTokenCount": 39}
    assert pc._normalize_usage("gemini", raw) == {
        "prompt_tokens": 30, "completion_tokens": 9, "total_tokens": 39,
        "cache_read_tokens": None, "cache_write_tokens": None,
        "cache_miss_tokens": None}


def test_empty_usage_yields_nones():
    assert pc._normalize_usage("anthropic", None) == {
        "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
        "cache_read_tokens": None, "cache_write_tokens": None,
        "cache_miss_tokens": None}


def test_usage_rejects_nonfinite_fractional_boolean_and_bigint_overflow():
    raw = {
        "prompt_tokens": float("inf"),
        "completion_tokens": 1.5,
        "total_tokens": (1 << 63),
        "prompt_tokens_details": {
            "cached_tokens": True,
            "cache_write_tokens": float("nan"),
        },
        "prompt_cache_miss_tokens": "not-a-number",
    }

    assert pc._normalize_usage("openai", raw) == {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "cache_miss_tokens": None,
    }


def test_usage_derived_totals_never_overflow_postgres_bigint():
    maximum = (1 << 63) - 1
    usage = pc._normalize_usage(
        "anthropic",
        {
            "input_tokens": maximum,
            "cache_creation_input_tokens": 1,
            "cache_read_input_tokens": 0,
            "output_tokens": maximum,
        },
    )

    assert usage["prompt_tokens"] is None
    assert usage["completion_tokens"] == maximum
    assert usage["total_tokens"] is None
    assert usage["cache_miss_tokens"] is None


def _diagnostic_usage(provider, raw):
    observation = [None]
    token = pc._CACHE_USAGE_OBSERVATION.set(observation)
    try:
        normalized = pc._observed_usage(provider, raw)
        return {**normalized, "cache_diagnostic": observation[0][1]}
    finally:
        pc._CACHE_USAGE_OBSERVATION.reset(token)


def test_cache_raw_numeric_diagnostic_maps_protocols_without_content():
    import json
    import provider_cache_diagnostics as diag
    cases = [
        ("anthropic", {"input_tokens": 3, "cache_read_input_tokens": 7}, 10, 7),
        ("bedrock", {"inputTokens": 3, "cacheReadInputTokens": 7}, 10, 7),
        ("gemini", {"promptTokenCount": 10, "cachedContentTokenCount": 0}, 10, 0),
        ("openai", {"input_tokens": 10, "input_tokens_details": {"cached_tokens": 7}}, 10, 7),
        ("openai_compatible", {"prompt_tokens": 10, "prompt_cache_hit_tokens": 0}, 10, 0),
    ]
    for provider, raw, prompt, read in cases:
        raw["private_prompt"] = "PRIVATE-CONTENT"
        usage = _diagnostic_usage(provider, raw)
        evidence = diag.project(usage["cache_diagnostic"])
        assert evidence["normalized"]["prompt_tokens"] == prompt
        assert evidence["normalized"]["cache_read_tokens"] == read
        assert "PRIVATE-CONTENT" not in json.dumps(evidence)
        assert "private_prompt" not in evidence["raw"]


def test_cache_raw_missing_zero_invalid_and_normalized_are_distinct():
    import provider_cache_diagnostics as diag
    for value in [None, False, "0", 0.0, float("nan"), -1, 1 << 63]:
        usage = _diagnostic_usage("openai", {"prompt_tokens_details": {"cached_tokens": value}})
        field = diag.project(usage["cache_diagnostic"])["raw"]["prompt_tokens_details.cached_tokens"]
        assert field == {"present": True, "valid": False, "value": None}
    missing = diag.project(_diagnostic_usage("openai", {})["cache_diagnostic"])
    assert missing["raw"]["prompt_tokens_details.cached_tokens"] == {"present": False, "valid": None, "value": None}
    zero = diag.project(_diagnostic_usage("openai", {"prompt_tokens_details": {"cached_tokens": 0}})["cache_diagnostic"])
    assert zero["raw"]["prompt_tokens_details.cached_tokens"] == {"present": True, "valid": True, "value": 0}
    assert zero["normalized"]["cache_read_tokens"] == 0


def test_cache_diagnostic_failure_preserves_normalization(monkeypatch):
    def fail(*args):
        raise RuntimeError("private-error")
    monkeypatch.setattr(pc.provider_cache_diagnostics, "usage", fail)
    raw = {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 0}}
    observation = [None]
    token = pc._CACHE_USAGE_OBSERVATION.set(observation)
    try:
        assert pc._observed_usage("openai", raw) == pc._normalize_usage("openai", raw)
        assert observation[0][1] is None
    finally:
        pc._CACHE_USAGE_OBSERVATION.reset(token)
    assert pc._CACHE_USAGE_OBSERVATION.get() is None
