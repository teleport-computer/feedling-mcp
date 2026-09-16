"""IO's single-card write limit, not a storage or model-context budget.

Product decision 2026-09-16: keep the existing 5000-character limit, reject
oversize new content explicitly, and never truncate a persisted card on read.
Units are Python Unicode code points after stripping outer whitespace.
"""
MAX_CONTENT_CHARS = 5000


class ContentTooLong(ValueError):
    code = "memory_content_too_long"

    def __init__(self, actual: int):
        super().__init__(self.code)
        self.actual = actual


def validated_content(value: object) -> str:
    content = str(value or "").strip()
    if len(content) > MAX_CONTENT_CHARS:
        raise ContentTooLong(len(content))
    return content
