"""Embedding contract tests use deterministic fakes, never model downloads."""
import math
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest
from memory.embedding import e5_onnx, fake, projection, protocol


def test_protocol_determinism_dimension_and_norm():
    encoder = fake.FakeEmbedder(dim=64)
    assert isinstance(encoder, protocol.Embedder)
    vectors = encoder.encode_passages(["hello garden", "hello garden", ""])
    assert vectors[0] == vectors[1] == fake.FakeEmbedder(64).encode_query("hello garden")
    for vector in vectors:
        assert len(vector) == 64
        assert math.sqrt(sum(v * v for v in vector)) == pytest.approx(1)


def test_controlled_similarity():
    encoder = fake.FakeEmbedder(384, aliases={"container": "pot"})
    query = encoder.encode_query("plant container")
    target, distractor = encoder.encode_passages(["plant pot", "train ticket"])
    assert sum(a*b for a,b in zip(query,target)) > sum(a*b for a,b in zip(query,distractor))


@pytest.mark.parametrize("method,texts,expected", [
    ("encode_query", "你好", ["query: 你好"]),
    ("encode_passages", ["你好", "query: quoted"], ["passage: 你好", "passage: query: quoted"]),
])
def test_prefixes_used_by_both_adapters(monkeypatch, method, texts, expected):
    for cls in (fake.FakeEmbedder, e5_onnx.E5SmallOnnxEmbedder):
        encoder = cls()
        seen = []
        def capture(values):
            seen.extend(values)
            return [[1.0]] * len(values)
        monkeypatch.setattr(encoder, "_encode", capture)
        getattr(encoder, method)(texts)
        assert seen == expected


def test_fake_truncation_counter_counts_rows_not_tokens():
    encoder = fake.FakeEmbedder()
    encoder.encode_passages(["word " * 700, "short", "long " * 1000])
    assert encoder.truncated_count == 2
    encoder.encode_query("word " * 700)
    assert encoder.readiness()["truncated_count"] == 3


def test_missing_model_is_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv("FEEDLING_EMBED_MODEL_DIR", str(tmp_path / "missing"))
    encoder = e5_onnx.E5SmallOnnxEmbedder()
    assert not encoder.available
    assert encoder.unavailable_reason == "model_directory_missing"
    with pytest.raises(protocol.EmbeddingUnavailable):
        protocol.warmup(encoder)
    monkeypatch.setenv("FEEDLING_EMBED_MODEL_DIR", str(tmp_path))
    assert e5_onnx.E5SmallOnnxEmbedder().unavailable_reason == "model_files_missing"


def test_import_does_not_load_optional_dependencies():
    code = """
import sys
from memory.embedding import e5_onnx
assert 'onnxruntime' not in sys.modules
assert 'tokenizers' not in sys.modules
assert 'numpy' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code], check=True,
                   env={**__import__('os').environ, "PYTHONPATH": str(Path(__file__).parent.parent / 'backend')})


def test_projection_missing_duplicates_and_cues():
    assert projection.card_projection_text({}) == ""
    card = {"summary": " same ", "content": "same", "bucket": "plants", "threads": ["same", None],
            "retrieval_cues": ["pot", "pot", 123], "search_text": "must not override"}
    assert projection.card_projection_text(card) == "same\nplants\npot"
    assert len(projection.projection_hash(card)) == 16
    assert projection.projection_hash(card) != projection.projection_hash({**card, "content": "changed"})


def test_projection_version_in_model_identity(monkeypatch):
    before = fake.FakeEmbedder().model_id
    assert projection.PROJECTION_VERSION in before
    monkeypatch.setattr(projection, "PROJECTION_VERSION", "next-projection")
    assert fake.FakeEmbedder().model_id != before
    assert "next-projection" in e5_onnx.E5SmallOnnxEmbedder().model_id


def test_warmup_and_readiness():
    encoder = fake.FakeEmbedder()
    assert protocol.warmup(encoder) >= 0
    assert encoder.readiness() == {"available": True, "unavailable_reason": None,
        "model_id": encoder.model_id, "dim": 64, "load_seconds": 0.0, "truncated_count": 0}


@pytest.mark.parametrize("vector", [[0., 0.], [1., float('nan')], [float('inf'), 0.], [1.]])
def test_invalid_vectors_rejected(vector):
    with pytest.raises(ValueError):
        protocol.normalize(vector, 2)


def test_missing_inference_package_is_unavailable(monkeypatch, tmp_path):
    (tmp_path / 'model_int8.onnx').write_bytes(b'not loaded without dependency')
    (tmp_path / 'tokenizer.json').write_text('{}')
    monkeypatch.setenv('FEEDLING_EMBED_MODEL_DIR', str(tmp_path))
    monkeypatch.setitem(sys.modules, 'onnxruntime', None)
    encoder = e5_onnx.E5SmallOnnxEmbedder()
    assert not encoder.available
    assert encoder.unavailable_reason == 'inference_dependencies_missing'
