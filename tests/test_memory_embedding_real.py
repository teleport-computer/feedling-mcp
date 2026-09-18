"""Opt-in real ONNX check; absent artifacts are explicitly skipped, never downloaded."""
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import pytest
from memory.embedding import e5_onnx


@pytest.mark.parametrize("precision", ["fp32", "int8"])
def test_real_e5_query_121_passages_semantic_order(precision):
    if not os.environ.get("FEEDLING_EMBED_MODEL_DIR"):
        pytest.skip("FEEDLING_EMBED_MODEL_DIR is unset: local ONNX weights required; no download")
    model = e5_onnx.E5SmallOnnxEmbedder(precision=precision)
    assert model.available, model.unavailable_reason
    query = model.encode_query("阳台上种花的容器是什么颜色？")
    passages = ["阳台上的花盆是蓝色的。", "火车票订在下周五早上。"] + ["今天开会讨论项目进度。"] * 119
    vectors = model.encode_passages(passages)
    assert len(vectors) == 121
    for v in [query, *vectors]:
        assert len(v) == 384
        assert math.sqrt(sum(x*x for x in v)) == pytest.approx(1, abs=1e-5)
    scores = [sum(x*y for x,y in zip(query, v)) for v in vectors[:2]]
    print(f"{precision} semantic_cosine={scores}")
    assert scores[0] > scores[1]
    count = model.truncated_count
    model.encode_passages(["long text " * 600, "short text"])
    assert model.truncated_count == count + 1
