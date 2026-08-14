"""已搬至 ``memory_garden.types`` —— 此处保留 re-export 以兼容现有 import 路径。

搬迁背景见 ``docs/MEMORY_GARDEN_EXTRACTION_DESIGN.zh.md``：
来源枚举属于「卡长什么样」这一层，是内核的一部分。
"""
from memory_garden.types import *  # noqa: F401,F403
from memory_garden.types import (  # noqa: F401
    MEMORY_CAPTURE_MODE_VALUES,
    MEMORY_SOURCE_VALUES,
    RESIDENT_ABSORB_SOURCE,
    RESIDENT_PATCH_SOURCE,
)
