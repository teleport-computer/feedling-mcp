"""Metadata-only history coverage rendering for Hosted Runtime V2.

This module never accepts conversation plaintext and never calls a model.
Persisted seq/count provenance is authoritative; the rendered bullet is only
the bounded prompt-facing representation of that coverage.
"""


def deterministic_fold(
    *,
    source_message_count: int,
    includes_legacy_opaque: bool = False,
) -> str:
    """Render a content-free coverage witness from persisted metadata only."""

    count = int(source_message_count)
    if count < 0 or (count == 0 and not includes_legacy_opaque):
        raise ValueError("source_message_count must prove coverage")
    if includes_legacy_opaque and count == 0:
        return "- [更早的历史摘要已由长期记忆覆盖]"
    if includes_legacy_opaque:
        return f"- [更早的历史摘要及 {count} 条消息已由长期记忆覆盖]"
    return f"- [{count} 条更早的消息已由长期记忆覆盖]"
