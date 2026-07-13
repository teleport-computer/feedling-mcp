import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client as pc


def test_openai_usage_passthrough():
    raw = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert pc._normalize_usage("openai", raw) == {
        "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def test_anthropic_usage_mapped():
    raw = {"input_tokens": 12, "output_tokens": 8}
    assert pc._normalize_usage("anthropic", raw) == {
        "prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}


def test_gemini_usage_mapped():
    raw = {"promptTokenCount": 30, "candidatesTokenCount": 9, "totalTokenCount": 39}
    assert pc._normalize_usage("gemini", raw) == {
        "prompt_tokens": 30, "completion_tokens": 9, "total_tokens": 39}


def test_empty_usage_yields_nones():
    assert pc._normalize_usage("anthropic", None) == {
        "prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
