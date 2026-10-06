"""Embedding interface. Importing it does not load inference dependencies."""
from __future__ import annotations

import math
import time
from typing import Protocol, runtime_checkable


class EmbeddingUnavailable(RuntimeError):
    pass


@runtime_checkable
class Embedder(Protocol):
    model_id: str
    dim: int
    available: bool
    unavailable_reason: str | None
    load_seconds: float
    truncated_count: int

    def encode_query(self, text: str) -> list[float]: ...
    def encode_passages(self, texts: list[str]) -> list[list[float]]: ...
    def readiness(self) -> dict: ...


def normalize(values: list[float], dim: int) -> list[float]:
    if len(values) != dim or any(not math.isfinite(v) for v in values):
        raise ValueError("embedding_invalid_vector")
    norm = math.sqrt(sum(v * v for v in values))
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("embedding_invalid_vector")
    return [v / norm for v in values]


class PrefixedEmbedder:
    """Prefixes are applied exactly once to raw inputs, even non-English text."""
    def encode_query(self, text: str) -> list[float]:
        if not isinstance(text, str):
            raise TypeError("embedding_text_must_be_string")
        return self._encode(["query: " + text])[0]

    def encode_passages(self, texts: list[str]) -> list[list[float]]:
        if isinstance(texts, str) or any(not isinstance(text, str) for text in texts):
            raise TypeError("embedding_texts_must_be_strings")
        return self._encode(["passage: " + text for text in texts])

    def readiness(self) -> dict:
        return {"available": self.available, "unavailable_reason": self.unavailable_reason,
                "model_id": self.model_id, "dim": self.dim, "load_seconds": self.load_seconds,
                "truncated_count": self.truncated_count}


def warmup(embedder: Embedder) -> float:
    """Perform real inference; unavailable/errors propagate rather than timing a no-op."""
    started = time.perf_counter()
    embedder.encode_query("warmup")
    return time.perf_counter() - started
