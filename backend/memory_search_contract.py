"""Wire/resource contract shared by search callers and the enclave.

No tokenizer or user-content processing belongs in the backend caller.
"""

import json

VERSION = "bm25-jieba-0.42.1-v1"
LEGACY = "substring-legacy"
MAX_CARDS = 4096
MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_TEXT_BYTES = 16 * 1024 * 1024


class SearchLimitExceeded(RuntimeError):
    def __init__(self):
        super().__init__("memory_search_resource_limit")


def check_request(payload: dict) -> None:
    """Bound the complete corpus, never silently slice it into a smaller one."""
    if len(payload.get("moments", [])) > MAX_CARDS:
        raise SearchLimitExceeded()
    size = 0
    # Match httpx JSON encoding, without allocating another whole request copy.
    for chunk in json.JSONEncoder(ensure_ascii=False, separators=(",", ":"),
                                  allow_nan=False).iterencode(payload):
        size += len(chunk.encode("utf-8"))
        if size > MAX_REQUEST_BYTES:
            raise SearchLimitExceeded()
