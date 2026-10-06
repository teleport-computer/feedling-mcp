"""Local-only multilingual-e5-small ONNX CPU adapter (no model downloads).

Expected directory: tokenizer.json and model.onnx (fp32) or model_int8.onnx.
All files beneath that operator-provisioned directory participate in identity,
including ONNX external weight files. Treat the directory as immutable in use.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path

from memory.embedding import projection, protocol


class E5SmallOnnxEmbedder(protocol.PrefixedEmbedder):
    def __init__(self, *, precision: str = "int8", threads: int = 4, batch_size: int = 32):
        if precision not in {"int8", "fp32"}:
            raise ValueError("embedding_invalid_precision")
        if any(type(n) is not int or n < 1 for n in (threads, batch_size)):
            raise ValueError("embedding_invalid_capacity")
        self.dim = 384
        self.available = False
        self.unavailable_reason = "model_directory_missing"
        self.model_id = f"intfloat/multilingual-e5-small:{precision}:unloaded:{projection.PROJECTION_VERSION}"
        self.truncated_count = 0
        self.load_seconds = 0.0
        self.batch_size = batch_size
        self._lock = threading.Lock()
        started = time.perf_counter()
        try:
            self._load(precision, threads)
        finally:
            self.load_seconds = time.perf_counter() - started

    def _load(self, precision: str, threads: int) -> None:
        directory = os.environ.get("FEEDLING_EMBED_MODEL_DIR", "").strip()
        if not directory or not Path(directory).is_dir():
            return
        root = Path(directory)
        model = root / ("model_int8.onnx" if precision == "int8" else "model.onnx")
        tokenizer = root / "tokenizer.json"
        if not model.is_file() or not tokenizer.is_file():
            self.unavailable_reason = "model_files_missing"
            return
        try:
            import numpy as np
            import onnxruntime as ort
            import tokenizers
        except ImportError:
            self.unavailable_reason = "inference_dependencies_missing"
            return
        try:
            digest = hashlib.sha256()
            for path in sorted(p for p in root.rglob("*") if p.is_file()):
                digest.update(str(path.relative_to(root)).encode() + b"\0")
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
            self.model_id = (f"intfloat/multilingual-e5-small:{precision}:{digest.hexdigest()}:"
                             f"{projection.PROJECTION_VERSION}:mean-l2-512-v1")
            self._np = np
            self._tokenizer = tokenizers.Tokenizer.from_file(str(tokenizer))
            self._tokenizer.enable_truncation(max_length=512)
            self._tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
            options = ort.SessionOptions()
            options.intra_op_num_threads = threads
            options.inter_op_num_threads = 1
            self._session = ort.InferenceSession(str(model), sess_options=options,
                                                 providers=["CPUExecutionProvider"])
            self._inputs = {node.name for node in self._session.get_inputs()}
            if (not {"input_ids", "attention_mask"} <= self._inputs
                    or self._inputs - {"input_ids", "attention_mask", "token_type_ids"}
                    or "last_hidden_state" not in {n.name for n in self._session.get_outputs()}):
                self.unavailable_reason = "model_interface_invalid"
                return
        except Exception:
            # Never log paths, contents or third-party exception strings.
            self.unavailable_reason = "model_load_failed"
            return
        self.available = True
        self.unavailable_reason = None

    def _encode(self, texts: list[str]) -> list[list[float]]:
        if not self.available:
            raise protocol.EmbeddingUnavailable(self.unavailable_reason)
        result = []
        np = self._np
        with self._lock:
            for offset in range(0, len(texts), self.batch_size):
                encodings = self._tokenizer.encode_batch(texts[offset:offset + self.batch_size])
                self.truncated_count += sum(bool(e.overflowing) for e in encodings)
                feed = {"input_ids": np.asarray([e.ids for e in encodings], dtype=np.int64),
                        "attention_mask": np.asarray([e.attention_mask for e in encodings], dtype=np.int64)}
                if "token_type_ids" in self._inputs:
                    feed["token_type_ids"] = np.asarray([e.type_ids for e in encodings], dtype=np.int64)
                hidden = self._session.run(["last_hidden_state"], feed)[0]
                if hidden.shape != (*feed["input_ids"].shape, self.dim):
                    raise ValueError("embedding_invalid_output_shape")
                mask = feed["attention_mask"][..., None]
                pooled = (hidden * mask).sum(axis=1) / mask.sum(axis=1).clip(min=1)
                result.extend(protocol.normalize(row.tolist(), self.dim) for row in pooled)
        return result
