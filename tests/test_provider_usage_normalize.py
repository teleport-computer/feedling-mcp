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
