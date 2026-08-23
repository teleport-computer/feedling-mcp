"""能力表搬进内核之后，io 侧的导入路径与取值必须一字不差。"""
from __future__ import annotations

import pathlib
import sys

# Self-contained sys.path bootstrap (mirrors tests/test_perception_prompt_golden.py):
# conftest.py only adds backend/ to sys.path inside its DB-provisioning try-block,
# so on a no-Postgres machine this file must add backend/ itself.
_BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import perception.catalog as io_catalog
import perception_kernel.catalog as kernel_catalog

_NAMES = (
    "CAPABILITIES", "SIGNALS", "KEY_ALIASES", "IGNORED_KEYS", "COMPOSITE_KEYS",
    "KIND_CAPABILITY", "PHOTO_CLUSTER_SEC", "SCENE_HINTS", "SENSITIVE_PHOTO_SCENES",
    "PHOTO_METADATA_FIELDS", "UNLOCK_BACK_THRESHOLD_SEC", "RECENT_APPS_LIMIT",
    "RECENT_APPS_TOOL_LIMIT", "RECENT_APPS_TOOL_MAX",
)


def test_io_shell_reexports_the_same_objects():
    for name in _NAMES:
        assert getattr(io_catalog, name) is getattr(kernel_catalog, name), name


def test_capability_and_signal_counts_match_baseline():
    # 基线值：21 个能力、20 个信号（origin/test@d9a54e00）。
    # 这两个数字变了就是加/删了能力 —— 本批不许发生。
    assert len(kernel_catalog.CAPABILITIES) == 21
    assert len(kernel_catalog.SIGNALS) == 20


def test_every_signal_points_at_a_declared_capability():
    for signal in kernel_catalog.SIGNALS.values():
        assert signal.capability in kernel_catalog.CAPABILITIES, signal.input
