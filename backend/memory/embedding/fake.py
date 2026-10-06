"""Deterministic test double; never used as a fallback for an unavailable model."""
import hashlib
import json
import re

from memory.embedding import projection, protocol


class FakeEmbedder(protocol.PrefixedEmbedder):
    def __init__(self, dim: int = 64, *, aliases: dict[str, str] | None = None):
        if type(dim) is not int or dim < 2:
            raise ValueError("embedding_invalid_dimension")
        self.dim = dim
        self.aliases = dict(aliases or {})
        digest = hashlib.sha256(json.dumps(self.aliases, sort_keys=True).encode()).hexdigest()[:16]
        self.model_id = f"fake-v1:{dim}:{digest}:{projection.PROJECTION_VERSION}"
        self.available = True
        self.unavailable_reason = None
        self.load_seconds = 0.0
        self.truncated_count = 0

    def _encode(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            # The fake uses whitespace/character tokens, not E5's tokenizer.
            # Dropping the prefix lets tests compare query and passage meaning.
            words = re.findall(r"[a-z0-9_]+|[^\W\x00-\x7f]", text.split(": ", 1)[1].casefold())
            if len(words) > 510:  # reserve the two E5-style special-token slots
                self.truncated_count += 1
            values = [0.0] * self.dim
            for word in words[:510] or ["<empty>"]:
                word = self.aliases.get(word, word)
                digest = hashlib.sha256(word.encode()).digest()
                values[int.from_bytes(digest[:8], "little") % self.dim] += 1.0
            vectors.append(protocol.normalize(values, self.dim))
        return vectors
