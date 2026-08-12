import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import compaction


def test_deterministic_fold_renders_exact_count_without_source_text():
    assert compaction.deterministic_fold(source_message_count=7) == (
        "- [7 条更早的消息已由长期记忆覆盖]"
    )


def test_deterministic_fold_renders_zero_count_legacy_opaque_coverage():
    assert compaction.deterministic_fold(
        source_message_count=0,
        includes_legacy_opaque=True,
    ) == "- [更早的历史摘要已由长期记忆覆盖]"


def test_deterministic_fold_renders_mixed_legacy_and_exact_coverage():
    assert compaction.deterministic_fold(
        source_message_count=9,
        includes_legacy_opaque=True,
    ) == "- [更早的历史摘要及 9 条消息已由长期记忆覆盖]"


@pytest.mark.parametrize("count", [0, -1])
def test_deterministic_fold_requires_proven_coverage(count):
    with pytest.raises(ValueError, match="source_message_count"):
        compaction.deterministic_fold(source_message_count=count)


def test_deterministic_fold_rejects_negative_legacy_count():
    with pytest.raises(ValueError, match="source_message_count"):
        compaction.deterministic_fold(
            source_message_count=-1,
            includes_legacy_opaque=True,
        )
